from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from people_intel.light_tracker import (
    LightTracker,
    canonical_url,
    classify_url,
    normalize_key,
    source_external_id,
)

from .config import SOURCE_ROUTE_FIELDS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class PortableState:
    def __init__(self, database: str | Path):
        self.path = Path(database)
        self.tracker = LightTracker(self.path)
        self.db = self.tracker.db
        self._migrate()

    def close(self) -> None:
        self.tracker.close()

    def backup_database(self, destination: str | Path) -> dict[str, Any]:
        """Create a transactionally consistent SQLite backup without copying a live file."""

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            raise ValueError(f"backup destination already exists: {target}")
        backup = sqlite3.connect(target)
        try:
            self.db.backup(backup)
            integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"backup integrity_check failed: {integrity}")
        except Exception:
            backup.close()
            target.unlink(missing_ok=True)
            raise
        else:
            backup.close()
        target.chmod(0o600)
        return {"path": str(target), "integrity_check": "ok"}

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS portable_meta (
              key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_sync_audit (
              audit_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL,
              source_record_id TEXT, person_key TEXT, outcome TEXT NOT NULL,
              detail_json TEXT NOT NULL, observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intake_issues (
              issue_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL,
              source_record_id TEXT, display_name TEXT, issue_type TEXT NOT NULL,
              detail_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS intake_issue_unique
              ON intake_issues(source_ref,source_record_id,issue_type);
            CREATE TABLE IF NOT EXISTS human_conflicts (
              conflict_id TEXT PRIMARY KEY, person_key TEXT,
              field_name TEXT NOT NULL, current_value_json TEXT,
              incoming_value_json TEXT, source_ref TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_outbox (
              delivery_key TEXT PRIMARY KEY, output_kind TEXT NOT NULL,
              period TEXT NOT NULL, title TEXT NOT NULL, report_path TEXT NOT NULL,
              doc_url TEXT, doc_token TEXT, message_id TEXT,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_runs (
              run_id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
              metrics_json TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS feishu_record_links (
              entity_kind TEXT NOT NULL, entity_key TEXT NOT NULL,
              base_token TEXT NOT NULL, table_id TEXT NOT NULL, record_id TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(entity_kind,entity_key,base_token,table_id)
            );
            """
        )
        self.db.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            """INSERT INTO portable_meta(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                                              updated_at=excluded.updated_at""",
            (key, json.dumps(value, ensure_ascii=False), utc_now()),
        )
        self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value_json FROM portable_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def import_people(
        self,
        records: list[dict[str, Any]],
        *,
        source_ref: str,
    ) -> dict[str, Any]:
        counts = {"input": 0, "admitted": 0, "updated": 0, "needs_anchor": 0, "invalid": 0}
        people: list[str] = []
        issues: list[dict[str, Any]] = []
        for record in records:
            counts["input"] += 1
            name = str(record.get("canonical_name") or "").strip()
            record_id = str(record.get("source_record_id") or stable_hash(json.dumps(record, ensure_ascii=False, sort_keys=True)))
            urls = [str(value) for value in record.get("urls") or [] if str(value).startswith(("http://", "https://"))]
            if not name:
                counts["invalid"] += 1
                issues.append(self._issue(source_ref, record_id, name, "missing_name", record))
                continue
            if not urls:
                counts["needs_anchor"] += 1
                issues.append(self._issue(source_ref, record_id, name, "missing_required_profile", record))
                continue
            before_keys = {person["person_key"] for person in self.tracker.list_people()}
            secondary_text = str(record.get("secondary_id") or "").strip()
            secondary = ("source_table_id", secondary_text) if secondary_text else None
            try:
                person = self.tracker.add_person(
                    name,
                    aliases=list(record.get("aliases") or []),
                    urls=urls,
                    profile=dict(record.get("profile") or {}),
                    secondary_id=secondary,
                )
            except ValueError as exc:
                counts["invalid"] += 1
                issues.append(
                    self._issue(
                        source_ref,
                        record_id,
                        name,
                        "identity_or_source_conflict",
                        {"error": str(exc), "record": record},
                    )
                )
                continue
            self._apply_human_fields(person["person_key"], record, source_ref=source_ref)
            person = self.tracker.person(person["person_key"])
            outcome = "updated" if person["person_key"] in before_keys else "admitted"
            counts[outcome] += 1
            people.append(person["person_key"])
            self._audit(source_ref, record_id, person["person_key"], outcome, {"urls": len(urls)})
        self.db.commit()
        return {"counts": counts, "person_keys": people, "issues": issues}

    @staticmethod
    def _retrieval_patch(route: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            json.dumps(
                {key: route[key] for key in SOURCE_ROUTE_FIELDS if key in route},
                ensure_ascii=False,
            )
        )

    @staticmethod
    def _source_route_state(row: sqlite3.Row) -> dict[str, Any]:
        try:
            retrieval = json.loads(row["retrieval_config_json"] or "{}")
        except json.JSONDecodeError:
            retrieval = {}
        return {
            "source_id": row["source_id"],
            "url": row["url"],
            "tracking_enabled": bool(row["tracking_enabled"]),
            "curation_status": row["curation_status"],
            "curation_note": row["curation_note"],
            "health_status": row["health_status"],
            "last_checked_at": row["last_checked_at"],
            "has_baseline": bool(row["snapshot_json"]),
            "has_candidate": bool(row["candidate_snapshot_json"]),
            "candidate_count": int(row["candidate_count"] or 0),
            "retrieval_config": retrieval if isinstance(retrieval, dict) else {},
        }

    def _source_route_plan(
        self,
        routes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        public: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        claimed_sources: set[str] = set()
        for index, configured in enumerate(routes):
            route = dict(configured)
            action = str(route.get("action") or "configure")
            try:
                target_url = canonical_url(str(route.get("url") or ""))
            except (TypeError, ValueError):
                public.append(
                    {
                        "index": index,
                        "url": route.get("url"),
                        "action": action,
                        "status": "invalid_target",
                    }
                )
                continue
            matches = self.db.execute(
                """SELECT s.*,p.canonical_name FROM sources s
                   JOIN people p ON p.person_key=s.person_key WHERE s.url=?""",
                (target_url,),
            ).fetchall()
            base = {
                "index": index,
                "url": target_url,
                "action": action,
                "baseline_preserved": True,
                "candidate_preserved": True,
            }
            if not matches:
                public.append({**base, "status": "not_found"})
                continue
            if len(matches) != 1:
                public.append({**base, "status": "ambiguous", "matches": len(matches)})
                continue
            source = matches[0]
            if source["source_id"] in claimed_sources:
                public.append(
                    {
                        **base,
                        "status": "duplicate_source_route",
                        "source_id": source["source_id"],
                    }
                )
                continue
            claimed_sources.add(source["source_id"])
            before = self._source_route_state(source)
            patch = self._retrieval_patch(route)
            desired = {**before["retrieval_config"], **patch}
            operation = {
                "route": route,
                "action": action,
                "source": source,
                "before": before,
                "retrieval_config": desired,
            }
            changed_keys = sorted(
                key for key, value in patch.items() if before["retrieval_config"].get(key) != value
            )
            item = {
                **base,
                "source_id": source["source_id"],
                "person_key": source["person_key"],
                "kind": source["kind"],
                "changed_route_fields": changed_keys,
            }
            if action == "configure":
                changed = bool(changed_keys)
            elif action == "soft_retire":
                changed = bool(
                    int(source["tracking_enabled"] or 0) != 0
                    or source["curation_status"] != "retired"
                    or (source["curation_note"] or "") != str(route.get("curation_note") or "")
                )
            elif action == "replace":
                replacement_url = canonical_url(str(route.get("replacement_url") or ""))
                if replacement_url == target_url:
                    public.append({**item, "status": "invalid_replacement"})
                    continue
                replacement_kind = classify_url(replacement_url)
                if not replacement_kind:
                    public.append({**item, "status": "invalid_replacement"})
                    continue
                existing_replacement = self.db.execute(
                    """SELECT * FROM sources
                       WHERE person_key=? AND kind=? AND url=?""",
                    (source["person_key"], replacement_kind, replacement_url),
                ).fetchone()
                replacement_source_id = (
                    existing_replacement["source_id"]
                    if existing_replacement
                    else f"src_{stable_hash(f'{source['person_key']}|{replacement_kind}|{replacement_url}', 20)}"
                )
                replacement_before = (
                    self._source_route_state(existing_replacement)
                    if existing_replacement
                    else None
                )
                replacement_current = (replacement_before or {}).get("retrieval_config") or {}
                desired = {**replacement_current, **patch}
                changed_keys = sorted(
                    key for key, value in patch.items() if replacement_current.get(key) != value
                )
                operation.update(
                    {
                        "replacement_url": replacement_url,
                        "replacement_kind": replacement_kind,
                        "replacement_source_id": replacement_source_id,
                        "replacement_before": replacement_before,
                        "retrieval_config": desired,
                    }
                )
                item.update(
                    {
                        "replacement_url": replacement_url,
                        "replacement_source_id": replacement_source_id,
                        "replacement_source_exists": existing_replacement is not None,
                        "changed_route_fields": changed_keys,
                    }
                )
                note = str(route.get("curation_note") or "")
                changed = bool(
                    int(source["tracking_enabled"] or 0) != 0
                    or source["curation_status"] != "replaced"
                    or (source["curation_note"] or "") != note
                    or existing_replacement is None
                    or int(existing_replacement["tracking_enabled"] or 0) != 1
                    or existing_replacement["curation_status"] != "replacement"
                    or (existing_replacement["curation_note"] or "") != note
                    or (replacement_before or {}).get("retrieval_config") != desired
                )
            else:
                public.append({**item, "status": "invalid_action"})
                continue
            item["status"] = "ready" if changed else "no_change"
            public.append(item)
            operation["changed"] = changed
            operations.append(operation)
        return public, operations

    def sync_source_routes(
        self,
        routes: list[dict[str, Any]],
        *,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Preview or apply exact-URL public retrieval routes without touching evidence."""

        if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
            raise ValueError("source routes must be a list of objects")
        plan, operations = self._source_route_plan(routes)
        blockers = [
            item
            for item in plan
            if item["status"]
            in {
                "ambiguous",
                "duplicate_source_route",
                "invalid_action",
                "invalid_replacement",
                "invalid_target",
                "not_found",
            }
        ]
        counts = {
            "configured": 0,
            "soft_retired": 0,
            "replaced": 0,
            "no_change": sum(item["status"] == "no_change" for item in plan),
            "blocked": len(blockers),
            "audits_written": 0,
        }
        result = {
            "apply": apply,
            "mutated": False,
            "counts": counts,
            "routes": plan,
            "invariants": {
                "snapshot_columns_touched": False,
                "candidate_columns_touched": False,
                "observations_deleted": 0,
                "sources_deleted": 0,
            },
        }
        if not apply:
            return result
        if blockers:
            raise ValueError(
                "source route apply refused: "
                + ", ".join(
                    f"{item['status']}:{item.get('url') or item.get('index')}" for item in blockers
                )
            )
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for operation in operations:
                if not operation["changed"]:
                    continue
                route = operation["route"]
                source = operation["source"]
                action = operation["action"]
                note = str(
                    route.get("curation_note")
                    or "versioned credential-free public retrieval route configuration"
                ).strip()
                retrieval_json = json.dumps(
                    operation["retrieval_config"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if action == "configure":
                    self.db.execute(
                        "UPDATE sources SET retrieval_config_json=? WHERE source_id=?",
                        (retrieval_json, source["source_id"]),
                    )
                    audit_action = "configure_route"
                    counts["configured"] += 1
                    replacement_url = None
                elif action == "soft_retire":
                    self.db.execute(
                        """UPDATE sources SET tracking_enabled=0,curation_status='retired',
                                  curation_note=?,next_check_at=NULL WHERE source_id=?""",
                        (note, source["source_id"]),
                    )
                    audit_action = "soft_retire"
                    counts["soft_retired"] += 1
                    replacement_url = None
                else:
                    replacement_url = operation["replacement_url"]
                    replacement_kind = operation["replacement_kind"]
                    replacement_source_id = operation["replacement_source_id"]
                    self.db.execute(
                        """INSERT INTO sources(source_id,person_key,kind,url,external_id)
                           VALUES(?,?,?,?,?) ON CONFLICT(person_key,kind,url) DO NOTHING""",
                        (
                            replacement_source_id,
                            source["person_key"],
                            replacement_kind,
                            replacement_url,
                            source_external_id(replacement_kind, replacement_url),
                        ),
                    )
                    self.db.execute(
                        """UPDATE sources SET tracking_enabled=1,curation_status='replacement',
                                  curation_note=?,retrieval_config_json=? WHERE source_id=?""",
                        (note, retrieval_json, replacement_source_id),
                    )
                    self.db.execute(
                        """UPDATE sources SET tracking_enabled=0,curation_status='replaced',
                                  curation_note=?,next_check_at=NULL WHERE source_id=?""",
                        (note, source["source_id"]),
                    )
                    audit_action = "replace"
                    counts["replaced"] += 1
                self.db.execute(
                    """INSERT INTO source_curation_audit(
                         audit_id,source_id,person_key,registry_person_id,action,url,
                         replacement_url,target_identity,note,source_state_json,
                         decided_by,decided_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"aud_{uuid.uuid4().hex[:20]}",
                        source["source_id"],
                        source["person_key"],
                        None,
                        audit_action,
                        source["url"],
                        replacement_url,
                        source["canonical_name"],
                        note,
                        json.dumps(operation["before"], ensure_ascii=False, sort_keys=True),
                        "portable_config",
                        utc_now(),
                    ),
                )
                counts["audits_written"] += 1
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        result["mutated"] = bool(counts["audits_written"])
        return result

    def _apply_human_fields(
        self,
        person_key: str,
        record: dict[str, Any],
        *,
        source_ref: str,
    ) -> None:
        row = self.db.execute("SELECT * FROM people WHERE person_key=?", (person_key,)).fetchone()
        if not row:
            return
        canonical = str(record.get("canonical_name") or "").strip()
        aliases = json.loads(row["aliases_json"] or "[]")
        aliases = list(dict.fromkeys([*aliases, *(record.get("aliases") or [])]))
        if canonical and normalize_key(canonical) != normalize_key(row["canonical_name"]):
            aliases = list(dict.fromkeys([*aliases, row["canonical_name"]]))
        profile = json.loads(row["profile_json"] or "{}")
        for field, incoming in (record.get("profile") or {}).items():
            if incoming in (None, "", []):
                continue
            current = profile.get(field)
            if current not in (None, "", []) and current != incoming:
                self._conflict(person_key, field, current, incoming, source_ref)
            profile[field] = incoming
        try:
            self.db.execute(
                """UPDATE people SET canonical_name=?,normalized_name=?,aliases_json=?,
                       profile_json=?,updated_at=? WHERE person_key=?""",
                (
                    canonical or row["canonical_name"],
                    normalize_key(canonical or row["canonical_name"]),
                    json.dumps(aliases, ensure_ascii=False),
                    json.dumps(profile, ensure_ascii=False),
                    utc_now(),
                    person_key,
                ),
            )
        except sqlite3.IntegrityError:
            self._conflict(person_key, "canonical_name", row["canonical_name"], canonical, source_ref)

    def _issue(
        self,
        source_ref: str,
        source_record_id: str,
        display_name: str,
        issue_type: str,
        detail: Any,
    ) -> dict[str, Any]:
        issue_id = f"iss_{stable_hash(f'{source_ref}|{source_record_id}|{issue_type}') }"
        now = utc_now()
        self.db.execute(
            """INSERT INTO intake_issues(
                 issue_id,source_ref,source_record_id,display_name,issue_type,
                 detail_json,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?, 'open',?,?)
               ON CONFLICT(source_ref,source_record_id,issue_type) DO UPDATE SET
                 display_name=excluded.display_name,detail_json=excluded.detail_json,
                 status='open',updated_at=excluded.updated_at""",
            (
                issue_id,
                source_ref,
                source_record_id,
                display_name,
                issue_type,
                json.dumps(detail, ensure_ascii=False),
                now,
                now,
            ),
        )
        return {
            "issue_id": issue_id,
            "source_record_id": source_record_id,
            "display_name": display_name,
            "issue_type": issue_type,
        }

    def _conflict(self, person_key: str, field: str, current: Any, incoming: Any, source_ref: str) -> None:
        conflict_id = f"con_{uuid.uuid4().hex[:20]}"
        self.db.execute(
            """INSERT INTO human_conflicts VALUES(?,?,?,?,?,?, 'open',?)""",
            (
                conflict_id,
                person_key,
                field,
                json.dumps(current, ensure_ascii=False),
                json.dumps(incoming, ensure_ascii=False),
                source_ref,
                utc_now(),
            ),
        )

    def _audit(
        self,
        source_ref: str,
        source_record_id: str,
        person_key: str | None,
        outcome: str,
        detail: Any,
    ) -> None:
        self.db.execute(
            "INSERT INTO source_sync_audit VALUES(?,?,?,?,?,?,?)",
            (
                f"aud_{uuid.uuid4().hex[:20]}",
                source_ref,
                source_record_id,
                person_key,
                outcome,
                json.dumps(detail, ensure_ascii=False),
                utc_now(),
            ),
        )

    def begin_run(self, action: str) -> str:
        run_id = f"ptf_{stable_hash(f'{action}|{utc_now()}') }"
        self.db.execute(
            "INSERT INTO runtime_runs VALUES(?,?, 'running','{}',?,NULL)",
            (run_id, action, utc_now()),
        )
        self.db.commit()
        return run_id

    def complete_run(self, run_id: str, metrics: dict[str, Any], *, status: str = "completed") -> None:
        self.db.execute(
            "UPDATE runtime_runs SET status=?,metrics_json=?,completed_at=? WHERE run_id=?",
            (status, json.dumps(metrics, ensure_ascii=False), utc_now(), run_id),
        )
        self.db.commit()

    def delivery(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM delivery_outbox WHERE delivery_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def prepare_delivery(
        self,
        *,
        key: str,
        output_kind: str,
        period: str,
        title: str,
        report_path: str,
    ) -> dict[str, Any]:
        now = utc_now()
        self.db.execute(
            """INSERT INTO delivery_outbox(
                 delivery_key,output_kind,period,title,report_path,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,'prepared',?,?)
               ON CONFLICT(delivery_key) DO NOTHING""",
            (key, output_kind, period, title, report_path, now, now),
        )
        self.db.commit()
        assert self.delivery(key)
        return self.delivery(key) or {}

    def update_delivery(self, key: str, **fields: Any) -> dict[str, Any]:
        allowed = {"doc_url", "doc_token", "message_id", "status", "last_error", "attempts"}
        values = {field: value for field, value in fields.items() if field in allowed}
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{field}=?" for field in values)
        self.db.execute(
            f"UPDATE delivery_outbox SET {assignments} WHERE delivery_key=?",
            (*values.values(), key),
        )
        self.db.commit()
        return self.delivery(key) or {}

    def status(self) -> dict[str, Any]:
        counts = {
            "people": self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0],
            "sources": self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "open_intake_issues": self.db.execute(
                "SELECT COUNT(*) FROM intake_issues WHERE status!='resolved'"
            ).fetchone()[0],
            "open_human_conflicts": self.db.execute(
                "SELECT COUNT(*) FROM human_conflicts WHERE status='open'"
            ).fetchone()[0],
            "completed_deliveries": self.db.execute(
                "SELECT COUNT(*) FROM delivery_outbox WHERE status='completed'"
            ).fetchone()[0],
        }
        return {"database": str(self.path), "counts": counts}

    def open_anchor_issues(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT issue_id,source_ref,source_record_id,display_name,detail_json
               FROM intake_issues
               WHERE status='open' AND issue_type='missing_required_profile'
               ORDER BY created_at,issue_id LIMIT ?""",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        issues: list[dict[str, Any]] = []
        for row in rows:
            detail = json.loads(row["detail_json"] or "{}")
            issues.append(
                {
                    "issue_id": row["issue_id"],
                    "source_ref": row["source_ref"],
                    "source_record_id": row["source_record_id"],
                    "canonical_name": row["display_name"],
                    "secondary_id": detail.get("secondary_id"),
                    "aliases": detail.get("aliases") or [],
                    "profile": detail.get("profile") or {},
                }
            )
        return issues

    def apply_anchor_resolutions(
        self,
        payload: dict[str, Any],
        *,
        minimum_evidence_links: int = 1,
    ) -> dict[str, Any]:
        """Apply agent-discovered anchors without trusting name-only matches."""

        if not isinstance(payload, dict) or not payload.get("all_reviewed"):
            raise ValueError("anchor bridge input must contain all_reviewed=true")
        resolutions = payload.get("resolutions") or []
        unresolved = payload.get("unresolved") or []
        if not isinstance(resolutions, list) or not isinstance(unresolved, list):
            raise ValueError("anchor bridge resolutions and unresolved must be lists")
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for candidate in resolutions:
            if not isinstance(candidate, dict):
                rejected.append({"reason": "resolution_not_object"})
                continue
            issue_id = str(candidate.get("issue_id") or "")
            row = self.db.execute(
                """SELECT * FROM intake_issues
                   WHERE issue_id=? AND status='open' AND issue_type='missing_required_profile'""",
                (issue_id,),
            ).fetchone()
            if not row:
                rejected.append({"issue_id": issue_id, "reason": "unknown_or_closed_issue"})
                continue
            detail = json.loads(row["detail_json"] or "{}")
            expected_names = {
                normalize_key(str(value))
                for value in [row["display_name"], *(detail.get("aliases") or [])]
                if str(value).strip()
            }
            claimed_name = normalize_key(str(candidate.get("canonical_name") or row["display_name"] or ""))
            if claimed_name not in expected_names:
                rejected.append({"issue_id": issue_id, "reason": "name_or_alias_mismatch"})
                continue
            urls = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in candidate.get("urls") or []
                    if str(value).strip().startswith(("http://", "https://"))
                )
            )
            evidence = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in candidate.get("evidence_urls") or []
                    if str(value).strip().startswith(("http://", "https://"))
                )
            )
            if not urls:
                rejected.append({"issue_id": issue_id, "reason": "no_supported_profile_url"})
                continue
            if len(evidence) < max(1, int(minimum_evidence_links)):
                rejected.append({"issue_id": issue_id, "reason": "insufficient_evidence_links"})
                continue
            resolved_record = dict(detail)
            resolved_record["urls"] = urls
            imported = self.import_people(
                [resolved_record],
                source_ref=f"anchor-discovery:{row['source_ref']}",
            )
            if imported["counts"]["admitted"] + imported["counts"]["updated"] != 1:
                rejected.append(
                    {
                        "issue_id": issue_id,
                        "reason": "identity_or_source_conflict",
                        "import": imported,
                    }
                )
                continue
            self.db.execute(
                "UPDATE intake_issues SET status='resolved',updated_at=? WHERE issue_id=?",
                (utc_now(), issue_id),
            )
            accepted.append(
                {
                    "issue_id": issue_id,
                    "person_key": imported["person_keys"][0],
                    "urls": urls,
                    "evidence_urls": evidence,
                }
            )
        deferred: list[str] = []
        for item in unresolved:
            issue_id = str(item.get("issue_id") if isinstance(item, dict) else item)
            if not issue_id:
                continue
            updated = self.db.execute(
                """UPDATE intake_issues SET status='deferred',updated_at=?
                   WHERE issue_id=? AND status='open'
                     AND issue_type='missing_required_profile'""",
                (utc_now(), issue_id),
            )
            if updated.rowcount:
                deferred.append(issue_id)
        self.db.commit()
        return {
            "accepted": accepted,
            "rejected": rejected,
            "deferred": deferred,
            "all_reviewed": True,
        }

    def feishu_record_link(
        self,
        *,
        entity_kind: str,
        entity_key: str,
        base_token: str,
        table_id: str,
    ) -> str | None:
        row = self.db.execute(
            """SELECT record_id FROM feishu_record_links
               WHERE entity_kind=? AND entity_key=? AND base_token=? AND table_id=?""",
            (entity_kind, entity_key, base_token, table_id),
        ).fetchone()
        return str(row["record_id"]) if row else None

    def set_feishu_record_link(
        self,
        *,
        entity_kind: str,
        entity_key: str,
        base_token: str,
        table_id: str,
        record_id: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO feishu_record_links VALUES(?,?,?,?,?,?)
               ON CONFLICT(entity_kind,entity_key,base_token,table_id) DO UPDATE SET
                 record_id=excluded.record_id,updated_at=excluded.updated_at""",
            (entity_kind, entity_key, base_token, table_id, record_id, utc_now()),
        )
        self.db.commit()
