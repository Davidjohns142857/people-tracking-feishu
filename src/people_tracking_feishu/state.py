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
            CREATE TABLE IF NOT EXISTS source_roster_state (
              source_ref TEXT PRIMARY KEY,
              source_snapshot_hash TEXT NOT NULL,
              source_complete INTEGER NOT NULL,
              page_cursor TEXT,
              record_count INTEGER NOT NULL,
              last_complete_sync TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_roster_records (
              source_ref TEXT NOT NULL,
              source_record_id TEXT NOT NULL,
              record_version_hash TEXT NOT NULL,
              person_key TEXT NOT NULL,
              canonical_record_json TEXT NOT NULL,
              source_ids_json TEXT NOT NULL,
              owned_source_ids_json TEXT NOT NULL,
              owns_person INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','removed')),
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              removed_at TEXT,
              PRIMARY KEY(source_ref,source_record_id)
            );
            CREATE INDEX IF NOT EXISTS source_roster_person
              ON source_roster_records(person_key,status);
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
            CREATE TABLE IF NOT EXISTS event_ledger (
              event_id TEXT PRIMARY KEY,
              audience TEXT NOT NULL CHECK(audience IN ('public','developer')),
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              observation_id TEXT,
              run_id TEXT,
              source_id TEXT,
              person_key TEXT,
              disposition TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS event_ledger_window
              ON event_ledger(audience,occurred_at,event_id);
            CREATE INDEX IF NOT EXISTS event_ledger_observation
              ON event_ledger(observation_id,event_type);
            CREATE TABLE IF NOT EXISTS agent_review_requests (
              request_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL UNIQUE,
              evidence_hash TEXT NOT NULL,
              request_json TEXT NOT NULL,
              snapshot_id TEXT,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','decided','cancelled')),
              decision_json TEXT,
              decided_by TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(event_id) REFERENCES event_ledger(event_id)
            );
            CREATE TABLE IF NOT EXISTS report_ledger (
              report_key TEXT PRIMARY KEY,
              audience TEXT NOT NULL CHECK(audience IN ('public','developer')),
              output_kind TEXT NOT NULL CHECK(output_kind IN ('daily','weekly')),
              period TEXT NOT NULL,
              window_start TEXT NOT NULL,
              window_end TEXT NOT NULL,
              cursor_name TEXT NOT NULL,
              title TEXT NOT NULL,
              report_path TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'prepared',
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              UNIQUE(audience,output_kind,period)
            );
            CREATE TABLE IF NOT EXISTS report_items (
              report_key TEXT NOT NULL,
              event_id TEXT NOT NULL,
              item_order INTEGER NOT NULL,
              PRIMARY KEY(report_key,event_id),
              FOREIGN KEY(report_key) REFERENCES report_ledger(report_key),
              FOREIGN KEY(event_id) REFERENCES event_ledger(event_id)
            );
            CREATE TABLE IF NOT EXISTS report_cursors (
              cursor_name TEXT PRIMARY KEY,
              audience TEXT NOT NULL CHECK(audience IN ('public','developer')),
              output_kind TEXT NOT NULL CHECK(output_kind IN ('daily','weekly')),
              delivered_through TEXT NOT NULL,
              last_report_key TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS host_budget_circuits (
              host_key TEXT PRIMARY KEY,
              circuit_state TEXT NOT NULL DEFAULT 'closed'
                CHECK(circuit_state IN ('closed','open','half_open')),
              blocked_until TEXT,
              reason TEXT,
              status_code INTEGER,
              retry_after_seconds INTEGER,
              consecutive_limits INTEGER NOT NULL DEFAULT 0,
              canary_required INTEGER NOT NULL DEFAULT 0,
              last_attempt_at TEXT,
              last_success_at TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
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
        self._ensure_column(
            "delivery_outbox", "audience", "TEXT NOT NULL DEFAULT 'public'"
        )
        self._ensure_column("delivery_outbox", "report_key", "TEXT")
        self._ensure_column("delivery_outbox", "window_start", "TEXT")
        self._ensure_column("delivery_outbox", "window_end", "TEXT")
        self._ensure_column("delivery_outbox", "cursor_name", "TEXT")
        self._ensure_column("agent_review_requests", "snapshot_id", "TEXT")
        self.db.execute(
            """CREATE INDEX IF NOT EXISTS agent_review_pending_snapshot
               ON agent_review_requests(status,snapshot_id,created_at,request_id)"""
        )
        self.db.commit()

    def _ensure_column(self, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

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
        authoritative: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        counts = {"input": 0, "admitted": 0, "updated": 0, "needs_anchor": 0, "invalid": 0}
        people: list[str] = []
        issues: list[dict[str, Any]] = []
        for record in records:
            counts["input"] += 1
            if str(record.get("record_type") or "person").casefold() == "review":
                counts["skipped_review"] = counts.get("skipped_review", 0) + 1
                self._audit(
                    source_ref,
                    str(record.get("source_record_id") or ""),
                    None,
                    "skipped_review",
                    {
                        "managed_by": str(record.get("managed_by") or ""),
                        "review_decision_present": bool(record.get("review_decision")),
                    },
                )
                continue
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
                explicit_person = (
                    self.db.execute(
                        "SELECT * FROM people WHERE person_key=?", (secondary_text,)
                    ).fetchone()
                    if authoritative and secondary_text
                    else None
                )
                if explicit_person:
                    # A Person Key previously written to the authoritative Base
                    # is the durable identity even if the user deleted/re-added
                    # the Feishu row, renamed the person, or changed URLs.
                    person = self.tracker.add_person(
                        str(explicit_person["canonical_name"]),
                        aliases=list(record.get("aliases") or []),
                        urls=urls,
                        profile={},
                        secondary_id=(
                            str(explicit_person["secondary_id_type"]),
                            str(explicit_person["secondary_id_value"]),
                        ),
                        commit=False,
                    )
                else:
                    person = self.tracker.add_person(
                        name,
                        aliases=list(record.get("aliases") or []),
                        urls=urls,
                        # Human-visible fields are reconciled below according to
                        # whether this roster is authoritative.
                        profile={},
                        secondary_id=secondary,
                        commit=False,
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
            self._apply_human_fields(
                person["person_key"],
                record,
                source_ref=source_ref,
                source_record_id=record_id,
                authoritative=authoritative,
            )
            person = self.tracker.person(person["person_key"])
            outcome = "updated" if person["person_key"] in before_keys else "admitted"
            counts[outcome] += 1
            people.append(person["person_key"])
            self._audit(source_ref, record_id, person["person_key"], outcome, {"urls": len(urls)})
        if commit:
            self.db.commit()
        return {"counts": counts, "person_keys": people, "issues": issues}

    @staticmethod
    def _record_version_hash(record: dict[str, Any]) -> str:
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _record_scan_signature(record: dict[str, Any]) -> str:
        """Hash only canonical URLs that can change the HTTP fetch scope."""

        payload = {
            "urls": sorted(
                {
                    canonical_url(str(value))
                    for value in record.get("urls") or []
                    if str(value).startswith(("http://", "https://"))
                }
            ),
        }
        return PortableState._record_version_hash(payload)

    def source_roster_state(self, source_ref: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM source_roster_state WHERE source_ref=?", (source_ref,)
        ).fetchone()
        return dict(row) if row else None

    def source_roster_records(
        self, source_ref: str, *, include_removed: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM source_roster_records WHERE source_ref=?"
        if not include_removed:
            sql += " AND status='active'"
        rows = self.db.execute(
            sql + " ORDER BY source_record_id", (source_ref,)
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["canonical_record"] = json.loads(item.pop("canonical_record_json"))
            item["source_ids"] = json.loads(item.pop("source_ids_json"))
            item["owned_source_ids"] = json.loads(item.pop("owned_source_ids_json"))
            item["owns_person"] = bool(item["owns_person"])
            result.append(item)
        return result

    def _active_roster_uses_source(
        self,
        source_id: str,
        *,
        excluding: tuple[str, str] | None = None,
    ) -> bool:
        rows = self.db.execute(
            """SELECT source_ref,source_record_id,source_ids_json
               FROM source_roster_records WHERE status='active'"""
        ).fetchall()
        for row in rows:
            if excluding and (row["source_ref"], row["source_record_id"]) == excluding:
                continue
            try:
                source_ids = json.loads(row["source_ids_json"] or "[]")
            except json.JSONDecodeError:
                continue
            if source_id in source_ids:
                return True
        return False

    def reconcile_source_records(
        self,
        source_ref: str,
        current_records: list[dict[str, Any]],
        *,
        source_complete: bool = True,
        page_cursor: str | None = None,
        authoritative: bool = False,
    ) -> dict[str, Any]:
        savepoint = "reconcile_source_records"
        started_transaction = not self.db.in_transaction
        if started_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(f"SAVEPOINT {savepoint}")
            result = self._reconcile_source_records_in_transaction(
                source_ref,
                current_records,
                source_complete=source_complete,
                page_cursor=page_cursor,
                authoritative=authoritative,
            )
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_transaction:
                self.db.commit()
            return result
        except BaseException:
            try:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                pass
            if started_transaction:
                self.db.rollback()
            raise

    def _reconcile_source_records_in_transaction(
        self,
        source_ref: str,
        current_records: list[dict[str, Any]],
        *,
        source_complete: bool = True,
        page_cursor: str | None = None,
        authoritative: bool = False,
    ) -> dict[str, Any]:
        """Reconcile a named roster without deleting tracker history.

        Complete snapshots may tombstone disappeared records. Partial/paginated
        snapshots only add or update records and can never infer removals. Explicit
        removed rows use the same tombstone path. An authoritative roster owns the
        human-visible person fields it supplies; every resulting change is audited.
        Snapshots and observations stay untouched for audit and restoration.
        """

        source_ref = str(source_ref or "").strip()
        if not source_ref:
            raise ValueError("source_ref is required")
        if not isinstance(current_records, list) or any(
            not isinstance(record, dict) for record in current_records
        ):
            raise ValueError("current_records must be a list of objects")
        if source_complete and page_cursor:
            raise ValueError("a complete source snapshot must not carry a page cursor")
        prepared: list[tuple[str, str, dict[str, Any]]] = []
        skipped_reviews: list[tuple[str, str, dict[str, Any]]] = []
        resolved_review_issue_by_record: dict[str, str] = {}
        closed_review_issue_by_record: dict[str, str] = {}
        snapshot_manifest: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        input_ids: set[str] = set()
        for record in current_records:
            record_id = str(record.get("source_record_id") or "").strip()
            if not record_id:
                raise ValueError("every roster record requires source_record_id")
            if record_id in input_ids:
                raise ValueError(f"duplicate source_record_id in snapshot: {record_id}")
            input_ids.add(record_id)
            version_hash = self._record_version_hash(record)
            snapshot_manifest.append(
                {"source_record_id": record_id, "record_version_hash": version_hash}
            )
            decision = str(record.get("review_decision") or "").strip().casefold()
            close_decisions = {
                "defer",
                "suppress",
                "ignore",
                "不跟踪",
                "暂不跟踪",
                "忽略",
                "无需跟踪",
            }
            if authoritative and decision in close_decisions:
                same_row_issue = self.db.execute(
                    """SELECT * FROM intake_issues
                       WHERE source_ref=? AND source_record_id=?
                       ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END,
                                created_at,issue_id LIMIT 1""",
                    (source_ref, record_id),
                ).fetchone()
                if same_row_issue:
                    # The original authoritative row deliberately remains a
                    # ``人员`` row.  Review decisions therefore have to be
                    # consumed independently of Record Type.
                    seen_ids.add(record_id)
                    closed_review_issue_by_record[record_id] = str(
                        same_row_issue["issue_id"]
                    )
                    skipped_reviews.append((record_id, version_hash, record))
                    continue
            if str(record.get("record_type") or "person").casefold() == "review":
                # Review rows live in the same authoritative Base, but they are
                # not roster entities.  They still count as present rows: omitting
                # them from ``seen_ids`` could falsely tombstone a previously
                # linked roster record during a complete-page reconciliation.
                seen_ids.add(record_id)
                issue = None
                if authoritative:
                    issue = self.db.execute(
                        """SELECT * FROM intake_issues
                           WHERE source_ref=? AND source_record_id=?
                           ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END,
                                    created_at,issue_id LIMIT 1""",
                        (source_ref, record_id),
                    ).fetchone()
                    if not issue:
                        review_key = str(record.get("secondary_id") or "").strip()
                        if review_key.startswith("review:"):
                            issue = self.db.execute(
                                "SELECT * FROM intake_issues WHERE issue_id=?",
                                (review_key.removeprefix("review:"),),
                            ).fetchone()
                if issue and decision in close_decisions:
                    closed_review_issue_by_record[record_id] = str(issue["issue_id"])
                    skipped_reviews.append((record_id, version_hash, record))
                    continue
                urls = [
                    str(value)
                    for value in record.get("urls") or []
                    if str(value).startswith(("http://", "https://"))
                ]
                if (
                    issue
                    and str(issue["issue_type"])
                    in {"missing_required_profile", "missing_name"}
                    and urls
                    and str(record.get("canonical_name") or issue["display_name"] or "").strip()
                ):
                    try:
                        original = json.loads(issue["detail_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        original = {}
                    original = original if isinstance(original, dict) else {}
                    converted = dict(original)
                    for key in (
                        "canonical_name",
                        "aliases",
                        "profile",
                        "human_fields_present",
                        "review_decision",
                    ):
                        if record.get(key) not in (None, "", []):
                            converted[key] = record[key]
                    converted.update(
                        {
                            "canonical_name": str(
                                record.get("canonical_name")
                                or original.get("canonical_name")
                                or issue["display_name"]
                            ).strip(),
                            "urls": urls,
                            "sources": list(record.get("sources") or []),
                            "source_record_id": record_id,
                            "lifecycle_status": "active",
                            "record_type": "person",
                            "managed_by": str(record.get("managed_by") or ""),
                        }
                    )
                    # ``review:<issue>`` is a UI key, never a durable identity.
                    if str(converted.get("secondary_id") or "").startswith("review:"):
                        converted["secondary_id"] = str(
                            original.get("secondary_id") or ""
                        )
                    prepared.append((record_id, version_hash, converted))
                    resolved_review_issue_by_record[record_id] = str(issue["issue_id"])
                    continue
                skipped_reviews.append((record_id, version_hash, record))
                continue
            seen_ids.add(record_id)
            prepared.append((record_id, version_hash, record))
        duplicate_identity_records: dict[str, list[str]] = {}
        if authoritative:
            identity_records: dict[str, list[str]] = {}
            for record_id, _, record in prepared:
                if str(record.get("lifecycle_status") or "active").casefold() == "removed":
                    continue
                identity = str(record.get("secondary_id") or "").strip()
                if identity:
                    identity_records.setdefault(identity, []).append(record_id)
            duplicate_identity_records = {
                identity: sorted(record_ids)
                for identity, record_ids in identity_records.items()
                if len(record_ids) > 1
            }
        duplicate_record_ids = {
            record_id
            for record_ids in duplicate_identity_records.values()
            for record_id in record_ids
        }
        duplicate_identity_by_record = {
            record_id: identity
            for identity, record_ids in duplicate_identity_records.items()
            for record_id in record_ids
        }
        if duplicate_record_ids:
            prepared = [
                item for item in prepared if item[0] not in duplicate_record_ids
            ]
        snapshot_manifest.sort(key=lambda item: item["source_record_id"])
        snapshot_hash = self._record_version_hash(
            {"source_ref": source_ref, "records": snapshot_manifest}
        )
        now = utc_now()
        outcomes: list[dict[str, Any]] = []
        changed_people: set[str] = set()
        changed_sources: set[str] = set()
        scan_sources: set[str] = set()

        by_record_id = {
            str(record.get("source_record_id") or ""): record
            for record in current_records
        }
        for record_id in sorted(duplicate_record_ids):
            record = by_record_id[record_id]
            identity = duplicate_identity_by_record[record_id]
            peer_ids = duplicate_identity_records[identity]
            issue = self._issue(
                source_ref,
                record_id,
                str(record.get("canonical_name") or ""),
                "duplicate_person_key",
                {
                    **record,
                    "duplicate_identity": identity,
                    "conflicting_record_ids": peer_ids,
                },
            )
            existing_roster = self.db.execute(
                """SELECT person_key,status FROM source_roster_records
                   WHERE source_ref=? AND source_record_id=?""",
                (source_ref, record_id),
            ).fetchone()
            outcome = "review_annotation" if existing_roster else "pending_review"
            existing_person_key = (
                str(existing_roster["person_key"]) if existing_roster else None
            )
            if existing_person_key:
                changed_people.add(existing_person_key)
            outcomes.append(
                {
                    "source_record_id": record_id,
                    "outcome": outcome,
                    "person_key": existing_person_key,
                    "source_ids": [],
                    "record_version_hash": self._record_version_hash(record),
                    "scan_required": False,
                    "issues": [issue],
                }
            )

        for record_id, version_hash, record in skipped_reviews:
            # Review rows are a user interface over intake state, not roster
            # entities.  Merely reading one must never resolve its underlying
            # issue: the user may still need to add a URL or choose a decision.
            outcomes.append(
                {
                    "source_record_id": record_id,
                    "outcome": "skipped_review",
                    "person_key": None,
                    "source_ids": [],
                    "record_version_hash": version_hash,
                    "scan_required": False,
                    "managed_by": str(record.get("managed_by") or ""),
                    "review_decision": str(record.get("review_decision") or ""),
                }
            )
            issue_id = closed_review_issue_by_record.get(record_id)
            if issue_id:
                closed = self.db.execute(
                    """UPDATE intake_issues SET status='resolved',updated_at=?
                       WHERE issue_id=? AND status!='resolved'""",
                    (now, issue_id),
                )
                if closed.rowcount:
                    self._audit(
                        source_ref,
                        record_id,
                        None,
                        "review_decision_applied",
                        {
                            "issue_id": issue_id,
                            "decision": str(record.get("review_decision") or ""),
                        },
                    )

        for record_id, version_hash, record in prepared:
            existing = self.db.execute(
                """SELECT * FROM source_roster_records
                   WHERE source_ref=? AND source_record_id=?""",
                (source_ref, record_id),
            ).fetchone()
            record_removed = (
                str(record.get("lifecycle_status") or "active").casefold() == "removed"
            )
            if record_removed:
                if not existing:
                    self.db.execute(
                        """UPDATE intake_issues SET status='resolved',updated_at=?
                           WHERE source_ref=? AND source_record_id=? AND status!='resolved'""",
                        (now, source_ref, record_id),
                    )
                    outcomes.append(
                        {
                            "source_record_id": record_id,
                            "outcome": "removed_untracked",
                            "person_key": None,
                            "source_ids": [],
                            "record_version_hash": version_hash,
                            "scan_required": False,
                        }
                    )
                    continue

                source_ids = json.loads(existing["source_ids_json"] or "[]")
                owned_source_ids = set(
                    json.loads(existing["owned_source_ids_json"] or "[]")
                )
                first_removal = existing["status"] != "removed"
                self.db.execute(
                    """UPDATE source_roster_records
                       SET record_version_hash=?,canonical_record_json=?,status='removed',
                           removed_at=COALESCE(removed_at,?),last_seen_at=?
                       WHERE source_ref=? AND source_record_id=?""",
                    (
                        version_hash,
                        json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False),
                        now,
                        now,
                        source_ref,
                        record_id,
                    ),
                )
                self.db.execute(
                    """UPDATE intake_issues SET status='resolved',updated_at=?
                       WHERE source_ref=? AND source_record_id=? AND status!='resolved'""",
                    (now, source_ref, record_id),
                )
                disabled: list[str] = []
                if first_removal:
                    for source_id in sorted(owned_source_ids):
                        if self._active_roster_uses_source(
                            source_id, excluding=(source_ref, record_id)
                        ):
                            continue
                        self.db.execute(
                            "UPDATE sources SET tracking_enabled=0 WHERE source_id=?",
                            (source_id,),
                        )
                        disabled.append(source_id)
                    changed_people.add(str(existing["person_key"]))
                    changed_sources.update(disabled)
                    self._audit(
                        source_ref,
                        record_id,
                        str(existing["person_key"]),
                        "removed",
                        {
                            "reason": "explicit_lifecycle_status",
                            "disabled_source_ids": disabled,
                        },
                    )
                outcomes.append(
                    {
                        "source_record_id": record_id,
                        "outcome": "removed" if first_removal else "unchanged",
                        "person_key": existing["person_key"],
                        "source_ids": source_ids,
                        "disabled_source_ids": disabled,
                        "record_version_hash": version_hash,
                        "scan_required": False,
                    }
                )
                continue
            if (
                existing
                and existing["status"] == "active"
                and existing["record_version_hash"] == version_hash
            ):
                authoritative_changes: list[dict[str, Any]] = []
                if authoritative:
                    authoritative_changes = self._apply_human_fields(
                        str(existing["person_key"]),
                        record,
                        source_ref=source_ref,
                        source_record_id=record_id,
                        authoritative=True,
                    )
                self.db.execute(
                    """UPDATE source_roster_records SET last_seen_at=?
                       WHERE source_ref=? AND source_record_id=?""",
                    (now, source_ref, record_id),
                )
                if authoritative_changes:
                    changed_people.add(str(existing["person_key"]))
                outcomes.append(
                    {
                        "source_record_id": record_id,
                        "outcome": "updated" if authoritative_changes else "unchanged",
                        "person_key": existing["person_key"],
                        "source_ids": json.loads(existing["source_ids_json"] or "[]"),
                        "scan_required": False,
                        "authoritative_field_changes": authoritative_changes,
                    }
                )
                continue

            before_people = {
                row["person_key"] for row in self.db.execute("SELECT person_key FROM people")
            }
            before_sources = {
                row["source_id"] for row in self.db.execute("SELECT source_id FROM sources")
            }
            authoritative_changes: list[dict[str, Any]] = []
            if existing:
                known = self.db.execute(
                    "SELECT * FROM people WHERE person_key=?", (existing["person_key"],)
                ).fetchone()
                if not known:
                    raise ValueError(
                        f"roster record {record_id} references a missing person"
                    )
                incoming_name = str(record.get("canonical_name") or "").strip()
                aliases = list(record.get("aliases") or [])
                if incoming_name and normalize_key(incoming_name) != normalize_key(
                    known["canonical_name"]
                ):
                    aliases.append(incoming_name)
                person = self.tracker.add_person(
                    known["canonical_name"],
                    aliases=[] if authoritative else aliases,
                    urls=list(record.get("urls") or []),
                    profile={},
                    secondary_id=(
                        known["secondary_id_type"],
                        known["secondary_id_value"],
                    ),
                    commit=False,
                )
                authoritative_changes = self._apply_human_fields(
                    person["person_key"],
                    record,
                    source_ref=source_ref,
                    source_record_id=record_id,
                    authoritative=authoritative,
                )
                imported = {"person_keys": [person["person_key"]], "issues": []}
            else:
                imported = self.import_people(
                    [record],
                    source_ref=source_ref,
                    authoritative=authoritative,
                    commit=False,
                )
            if len(imported["person_keys"]) != 1:
                # A roster row without a usable identity anchor is still a valid
                # authoritative input row.  Keep it in the intake queue instead of
                # aborting the whole roster transaction.  It intentionally has no
                # source_roster_records row yet because that table is keyed to a
                # resolved person; the stable intake issue makes repeated syncs
                # idempotent until a human or the host agent supplies an anchor.
                outcomes.append(
                    {
                        "source_record_id": record_id,
                        "outcome": "pending_review",
                        "person_key": None,
                        "source_ids": [],
                        "record_version_hash": version_hash,
                        "scan_required": False,
                        "issues": list(imported.get("issues") or []),
                    }
                )
                continue
            person_key = str(imported["person_keys"][0])
            self.db.execute(
                """UPDATE intake_issues SET status='resolved',updated_at=?
                   WHERE source_ref=? AND source_record_id=? AND status!='resolved'""",
                (now, source_ref, record_id),
            )
            resolved_review_issue = resolved_review_issue_by_record.get(record_id)
            if resolved_review_issue:
                self.db.execute(
                    "UPDATE intake_issues SET status='resolved',updated_at=? WHERE issue_id=?",
                    (now, resolved_review_issue),
                )
                self._audit(
                    source_ref,
                    record_id,
                    person_key,
                    "review_anchor_applied",
                    {"issue_id": resolved_review_issue},
                )
            urls = {
                canonical_url(str(url))
                for url in record.get("urls") or []
                if str(url).startswith(("http://", "https://"))
            }
            source_rows = self.db.execute(
                "SELECT source_id,url FROM sources WHERE person_key=?",
                (person_key,),
            ).fetchall()
            source_ids = sorted(
                row["source_id"] for row in source_rows if row["url"] in urls
            )
            preference_changes, preference_issues = self._apply_source_preferences(
                person_key,
                record,
                source_ref=source_ref,
                source_record_id=record_id,
            )
            changed_sources.update(preference_changes)
            newly_owned = {
                source_id for source_id in source_ids if source_id not in before_sources
            }
            if authoritative:
                # The Base is the user's source of truth.  When a deleted person
                # is re-added under a new Feishu record_id, its stable tracker
                # source already exists but may be disabled by the old tombstone.
                # The new active row must reclaim and re-enable that source.
                newly_owned.update(
                    source_id
                    for source_id in source_ids
                    if not self._active_roster_uses_source(
                        source_id, excluding=(source_ref, record_id)
                    )
                )
            old_owned = (
                set(json.loads(existing["owned_source_ids_json"] or "[]"))
                if existing
                else set()
            )
            owned_source_ids = sorted(old_owned | newly_owned)
            owns_person = bool(
                (existing and existing["owns_person"])
                or person_key not in before_people
            )
            if existing and existing["status"] == "removed":
                outcome = "restored"
            elif existing:
                outcome = "updated"
            else:
                outcome = "added"
            old_source_ids = (
                set(json.loads(existing["source_ids_json"] or "[]"))
                if existing
                else set()
            )
            scan_required_ids = (
                set(source_ids)
                if outcome in {"added", "restored"}
                else set(source_ids) - old_source_ids
            )
            requires_scan = bool(scan_required_ids)
            removed_owned_sources = (old_source_ids - set(source_ids)) & old_owned
            for source_id in removed_owned_sources:
                if not self._active_roster_uses_source(
                    source_id, excluding=(source_ref, record_id)
                ):
                    self.db.execute(
                        "UPDATE sources SET tracking_enabled=0 WHERE source_id=?",
                        (source_id,),
                    )
                    changed_sources.add(source_id)
            if outcome == "restored":
                for source_id in set(source_ids) & set(owned_source_ids):
                    self.db.execute(
                        "UPDATE sources SET tracking_enabled=1 WHERE source_id=?",
                        (source_id,),
                    )
                    changed_sources.add(source_id)
            elif authoritative:
                for source_id in source_ids:
                    current_source = self.db.execute(
                        "SELECT tracking_enabled FROM sources WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                    if current_source and int(current_source["tracking_enabled"] or 0) != 1:
                        self.db.execute(
                            "UPDATE sources SET tracking_enabled=1 WHERE source_id=?",
                            (source_id,),
                        )
                        changed_sources.add(source_id)
            encoded_record = json.dumps(
                record, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            self.db.execute(
                """INSERT INTO source_roster_records(
                     source_ref,source_record_id,record_version_hash,person_key,
                     canonical_record_json,source_ids_json,owned_source_ids_json,
                     owns_person,status,first_seen_at,last_seen_at,removed_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'active',?,?,NULL)
                   ON CONFLICT(source_ref,source_record_id) DO UPDATE SET
                     record_version_hash=excluded.record_version_hash,
                     person_key=excluded.person_key,
                     canonical_record_json=excluded.canonical_record_json,
                     source_ids_json=excluded.source_ids_json,
                     owned_source_ids_json=excluded.owned_source_ids_json,
                     owns_person=excluded.owns_person,status='active',
                     last_seen_at=excluded.last_seen_at,removed_at=NULL""",
                (
                    source_ref,
                    record_id,
                    version_hash,
                    person_key,
                    encoded_record,
                    json.dumps(source_ids, ensure_ascii=False),
                    json.dumps(owned_source_ids, ensure_ascii=False),
                    int(owns_person),
                    existing["first_seen_at"] if existing else now,
                    now,
                ),
            )
            changed_people.add(person_key)
            changed_sources.update(source_ids)
            scan_sources.update(scan_required_ids)
            outcomes.append(
                {
                    "source_record_id": record_id,
                    "outcome": outcome,
                    "person_key": person_key,
                    "source_ids": source_ids,
                    "record_version_hash": version_hash,
                    "scan_required": requires_scan,
                    "scan_source_ids": sorted(scan_required_ids),
                    "authoritative_field_changes": authoritative_changes,
                    "issues": preference_issues,
                }
            )

        if source_complete:
            # Rows that never acquired an identity anchor have no roster-record
            # entry.  Resolve their intake issues when the authoritative Base row
            # itself disappears so stale review work cannot accumulate forever.
            issue_rows = self.db.execute(
                """SELECT DISTINCT source_record_id FROM intake_issues
                   WHERE source_ref=? AND status!='resolved'""",
                (source_ref,),
            ).fetchall()
            for issue_row in issue_rows:
                issue_record_id = str(issue_row["source_record_id"] or "")
                if issue_record_id and issue_record_id not in seen_ids:
                    self.db.execute(
                        """UPDATE intake_issues SET status='resolved',updated_at=?
                           WHERE source_ref=? AND source_record_id=?
                             AND status!='resolved'""",
                        (now, source_ref, issue_record_id),
                    )
            disappeared = list(self.db.execute(
                """SELECT * FROM source_roster_records
                   WHERE source_ref=? AND status='active'""",
                (source_ref,),
            ).fetchall())
            disappeared = [
                row for row in disappeared if row["source_record_id"] not in seen_ids
            ]
            # Tombstone the complete missing set before deciding exclusivity.  This
            # avoids one disappearing record keeping another disappearing record's
            # shared source artificially active during a sequential reconciliation.
            for row in disappeared:
                self.db.execute(
                    """UPDATE source_roster_records
                       SET status='removed',removed_at=?,last_seen_at=?
                       WHERE source_ref=? AND source_record_id=?""",
                    (now, now, source_ref, row["source_record_id"]),
                )
                self.db.execute(
                    """UPDATE intake_issues SET status='resolved',updated_at=?
                       WHERE source_ref=? AND source_record_id=? AND status!='resolved'""",
                    (now, source_ref, row["source_record_id"]),
                )
            for row in disappeared:
                person_key = str(row["person_key"])
                owned_source_ids = set(
                    json.loads(row["owned_source_ids_json"] or "[]")
                )
                disabled: list[str] = []
                for source_id in owned_source_ids:
                    if self._active_roster_uses_source(
                        source_id,
                        excluding=(source_ref, str(row["source_record_id"])),
                    ):
                        continue
                    self.db.execute(
                        "UPDATE sources SET tracking_enabled=0 WHERE source_id=?",
                        (source_id,),
                    )
                    disabled.append(source_id)
                changed_people.add(person_key)
                changed_sources.update(disabled)
                outcomes.append(
                    {
                        "source_record_id": row["source_record_id"],
                        "outcome": "removed",
                        "person_key": person_key,
                        "source_ids": json.loads(row["source_ids_json"] or "[]"),
                        "disabled_source_ids": sorted(disabled),
                    }
                )

        previous_state = self.source_roster_state(source_ref)
        created_at = previous_state["created_at"] if previous_state else now
        last_complete_sync = (
            now
            if source_complete
            else previous_state.get("last_complete_sync") if previous_state else None
        )
        self.db.execute(
            """INSERT INTO source_roster_state(
                 source_ref,source_snapshot_hash,source_complete,page_cursor,
                 record_count,last_complete_sync,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(source_ref) DO UPDATE SET
                 source_snapshot_hash=excluded.source_snapshot_hash,
                 source_complete=excluded.source_complete,
                 page_cursor=excluded.page_cursor,
                 record_count=excluded.record_count,
                 last_complete_sync=excluded.last_complete_sync,
                 updated_at=excluded.updated_at""",
            (
                source_ref,
                snapshot_hash,
                int(source_complete),
                page_cursor,
                len(current_records),
                last_complete_sync,
                created_at,
                now,
            ),
        )
        counts: dict[str, int] = {}
        for item in outcomes:
            counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
        return {
            "source_ref": source_ref,
            "authoritative": bool(authoritative),
            "source_complete": bool(source_complete),
            "page_cursor": page_cursor,
            "source_snapshot_hash": snapshot_hash,
            "last_complete_sync": last_complete_sync,
            "counts": counts,
            "records": outcomes,
            "changed_person_ids": sorted(changed_people),
            "changed_source_ids": sorted(changed_sources),
            "scan_source_ids": sorted(scan_sources),
            "removals_evaluated": bool(source_complete),
        }

    def _apply_source_preferences(
        self,
        person_key: str,
        record: dict[str, Any],
        *,
        source_ref: str,
        source_record_id: str,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Persist explicit nested-source primary flags without guessing.

        Flat Homepage/Scholar/GitHub/LinkedIn columns have no primary metadata
        and therefore leave the tracker's current preference untouched.
        """

        declared = [
            item
            for item in record.get("sources") or []
            if isinstance(item, dict)
            and str(item.get("lifecycle_status") or "active").casefold() == "active"
            and str(item.get("url") or "").startswith(("http://", "https://"))
        ]
        if not declared:
            return [], []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in declared:
            url = canonical_url(str(item["url"]))
            kind = str(item.get("kind") or classify_url(url) or "")
            if kind:
                grouped.setdefault(kind, []).append({**item, "url": url})
        changed: list[str] = []
        issues: list[dict[str, Any]] = []
        primary_conflicts: dict[str, list[str]] = {}
        for kind, items in sorted(grouped.items()):
            primary_urls = sorted(
                {str(item["url"]) for item in items if bool(item.get("is_primary"))}
            )
            if len(primary_urls) > 1:
                primary_conflicts[kind] = primary_urls
                continue
            target = primary_urls[0] if primary_urls else None
            rows = self.db.execute(
                "SELECT source_id,url,is_primary FROM sources WHERE person_key=? AND kind=?",
                (person_key, kind),
            ).fetchall()
            for row in rows:
                desired = int(target is not None and str(row["url"]) == target)
                if int(row["is_primary"] or 0) == desired:
                    continue
                self.db.execute(
                    "UPDATE sources SET is_primary=? WHERE source_id=?",
                    (desired, row["source_id"]),
                )
                changed.append(str(row["source_id"]))
        if primary_conflicts:
            issues.append(
                self._issue(
                    source_ref,
                    source_record_id,
                    str(record.get("canonical_name") or ""),
                    "multiple_primary_sources",
                    {**record, "primary_conflicts": primary_conflicts},
                )
            )
        return sorted(set(changed)), issues

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
                    else "src_"
                    + stable_hash(
                        "|".join(
                            (
                                str(source["person_key"]),
                                replacement_kind,
                                replacement_url,
                            )
                        ),
                        20,
                    )
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
        source_record_id: str | None = None,
        authoritative: bool = False,
    ) -> list[dict[str, Any]]:
        row = self.db.execute("SELECT * FROM people WHERE person_key=?", (person_key,)).fetchone()
        if not row:
            return []
        canonical = str(record.get("canonical_name") or "").strip()
        aliases = json.loads(row["aliases_json"] or "[]")
        profile = json.loads(row["profile_json"] or "{}")

        if authoritative:
            marker = record.get("human_fields_present")
            if isinstance(marker, list):
                present = {str(value) for value in marker}
            else:
                # Backwards-compatible path for already-canonical callers that
                # predate the presence metadata added by v0.9.
                present = set()
                if "canonical_name" in record:
                    present.add("name")
                if "aliases" in record:
                    present.add("aliases")
                profile_keys = set((record.get("profile") or {}).keys())
                for semantic, stored_keys in {
                    "school": ("school", "affiliations"),
                    "research_focus": ("research_focus", "research_focuses"),
                    "stage": ("stage", "stages_or_roles"),
                    "cohorts": ("cohorts",),
                    "source_documents": ("source_documents",),
                    "employment_status": ("employment_status_claims",),
                    "identity_confidence": ("identity_confidence",),
                }.items():
                    if profile_keys.intersection(stored_keys):
                        present.add(semantic)

            changes: list[dict[str, Any]] = []

            def changed(field: str, before: Any, after: Any) -> None:
                if before != after:
                    changes.append({"field": field, "before": before, "after": after})

            chosen_name = row["canonical_name"]
            if "name" in present and canonical:
                changed("canonical_name", chosen_name, canonical)
                chosen_name = canonical

            chosen_aliases = aliases
            if "aliases" in present:
                chosen_aliases = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in record.get("aliases") or []
                        if str(value).strip()
                    )
                )
                changed("aliases", aliases, chosen_aliases)

            incoming_profile = dict(record.get("profile") or {})
            chosen_profile = dict(profile)
            profile_fields = {
                "school": ("school", "affiliations"),
                "research_focus": ("research_focus", "research_focuses"),
                "stage": ("stage", "stages_or_roles"),
                "cohorts": ("cohorts",),
                "source_documents": ("source_documents",),
                "employment_status": ("employment_status_claims",),
                "identity_confidence": ("identity_confidence",),
            }
            list_fields = {
                "affiliations",
                "research_focuses",
                "stages_or_roles",
                "cohorts",
                "source_documents",
                "employment_status_claims",
            }
            for semantic, stored_keys in profile_fields.items():
                if semantic not in present:
                    continue
                for stored_key in stored_keys:
                    incoming = incoming_profile.get(
                        stored_key, [] if stored_key in list_fields else ""
                    )
                    before = chosen_profile.get(stored_key)
                    if incoming in (None, "", []):
                        chosen_profile.pop(stored_key, None)
                        after = None
                    else:
                        chosen_profile[stored_key] = incoming
                        after = incoming
                    changed(stored_key, before, after)

            self.db.execute(
                """UPDATE people SET canonical_name=?,normalized_name=?,aliases_json=?,
                       profile_json=?,updated_at=? WHERE person_key=?""",
                (
                    chosen_name,
                    normalize_key(chosen_name),
                    json.dumps(chosen_aliases, ensure_ascii=False),
                    json.dumps(chosen_profile, ensure_ascii=False),
                    utc_now(),
                    person_key,
                ),
            )
            confirmed_fields: set[str] = set()
            if "name" in present:
                confirmed_fields.add("canonical_name")
            if "aliases" in present:
                confirmed_fields.add("aliases")
            for semantic, stored_keys in profile_fields.items():
                if semantic in present:
                    confirmed_fields.update(stored_keys)
            resolved_conflicts = 0
            if confirmed_fields:
                ordered_fields = sorted(confirmed_fields)
                placeholders = ",".join("?" for _ in ordered_fields)
                resolved = self.db.execute(
                    f"""UPDATE human_conflicts SET status='resolved'
                        WHERE person_key=? AND status='open'
                          AND field_name IN ({placeholders})""",
                    (person_key, *ordered_fields),
                )
                resolved_conflicts = int(resolved.rowcount or 0)
            if changes:
                self._audit(
                    source_ref,
                    source_record_id or "",
                    person_key,
                    "authoritative_fields_updated",
                    {"changes": changes},
                )
            if resolved_conflicts:
                self._audit(
                    source_ref,
                    source_record_id or "",
                    person_key,
                    "authoritative_conflicts_confirmed",
                    {
                        "confirmed_fields": sorted(confirmed_fields),
                        "resolved_conflicts": resolved_conflicts,
                    },
                )
            return changes

        aliases = list(dict.fromkeys([*aliases, *(record.get("aliases") or [])]))
        if canonical and normalize_key(canonical) != normalize_key(row["canonical_name"]):
            aliases = list(dict.fromkeys([*aliases, row["canonical_name"]]))
        for field, incoming in (record.get("profile") or {}).items():
            if incoming in (None, "", []):
                continue
            current = profile.get(field)
            if current not in (None, "", []) and current != incoming:
                self._conflict(person_key, field, current, incoming, source_ref)
                continue
            profile[field] = incoming
        chosen_name = row["canonical_name"]
        if canonical and normalize_key(canonical) != normalize_key(row["canonical_name"]):
            self._conflict(
                person_key,
                "canonical_name",
                row["canonical_name"],
                canonical,
                source_ref,
            )
        try:
            self.db.execute(
                """UPDATE people SET canonical_name=?,normalized_name=?,aliases_json=?,
                       profile_json=?,updated_at=? WHERE person_key=?""",
                (
                    chosen_name,
                    normalize_key(chosen_name),
                    json.dumps(aliases, ensure_ascii=False),
                    json.dumps(profile, ensure_ascii=False),
                    utc_now(),
                    person_key,
                ),
            )
        except sqlite3.IntegrityError:
            self._conflict(person_key, "canonical_name", row["canonical_name"], chosen_name, source_ref)
        return []

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

    def record_event(
        self,
        *,
        event_id: str,
        audience: str,
        event_type: str,
        occurred_at: str,
        disposition: str,
        reason_code: str,
        payload: dict[str, Any],
        observation_id: str | None = None,
        run_id: str | None = None,
        source_id: str | None = None,
        person_key: str | None = None,
    ) -> dict[str, Any]:
        """Append an immutable source event, while allowing editorial state updates.

        Event identifiers are derived from immutable observation/run evidence.  Replaying
        a report window therefore cannot duplicate an event or replace its original
        evidence.  Only disposition/reason and the rendered payload may be updated by a
        subsequently applied, evidence-bound agent decision.
        """

        if audience not in {"public", "developer"}:
            raise ValueError("event audience must be public or developer")
        if not event_id or not event_type or not occurred_at:
            raise ValueError("event_id, event_type and occurred_at are required")
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.db.execute(
            """INSERT INTO event_ledger(
                 event_id,audience,event_type,occurred_at,observation_id,run_id,
                 source_id,person_key,disposition,reason_code,payload_json,
                 created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO NOTHING""",
            (
                event_id,
                audience,
                event_type,
                occurred_at,
                observation_id,
                run_id,
                source_id,
                person_key,
                disposition,
                reason_code,
                encoded,
                now,
                now,
            ),
        )
        self.db.commit()
        event = self.event(event_id)
        assert event is not None
        return event

    def event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM event_ledger WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def events(
        self,
        *,
        audience: str,
        window_start: str,
        window_end: str,
        dispositions: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        if audience not in {"public", "developer"}:
            raise ValueError("event audience must be public or developer")
        clauses = [
            "audience=?",
            "julianday(occurred_at)>julianday(?)",
            "julianday(occurred_at)<=julianday(?)",
        ]
        parameters: list[Any] = [audience, window_start, window_end]
        if dispositions:
            clauses.append(
                "disposition IN (" + ",".join("?" for _ in dispositions) + ")"
            )
            parameters.extend(dispositions)
        rows = self.db.execute(
            "SELECT * FROM event_ledger WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at,event_id",
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def unreported_public_events(
        self, *, through: str, output_kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Return publishable facts that are not reserved by any public report.

        This intentionally is not bounded by the current cursor start: an execution
        agent may approve an older grey-zone event after that source window closed.
        Such an approval must appear once in the next report instead of being lost.
        """

        rows = self.db.execute(
            """SELECT e.* FROM event_ledger e
               WHERE e.audience='public' AND e.disposition='publish'
                 AND julianday(e.occurred_at)<=julianday(?)
                 AND NOT EXISTS (
                   SELECT 1 FROM report_items i
                   JOIN report_ledger r ON r.report_key=i.report_key
                   WHERE i.event_id=e.event_id AND r.audience='public'
                     AND (? IS NULL OR r.output_kind=?)
                 )
               ORDER BY e.occurred_at,e.event_id""",
            (through, output_kind, output_kind),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            result.append(value)
        return result

    def prepare_agent_review(
        self,
        *,
        request_id: str,
        event_id: str,
        evidence_hash: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.event(event_id):
            raise ValueError("agent review event is unknown")
        now = utc_now()
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.db.execute(
            """INSERT INTO agent_review_requests(
                 request_id,event_id,evidence_hash,request_json,status,
                 decision_json,decided_by,created_at,updated_at
               ) VALUES(?,?,?,?,'pending',NULL,NULL,?,?)
               ON CONFLICT(request_id) DO NOTHING""",
            (request_id, event_id, evidence_hash, encoded, now, now),
        )
        self.db.commit()
        row = self.db.execute(
            "SELECT * FROM agent_review_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        assert row
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        if value.get("decision_json"):
            value["decision"] = json.loads(value["decision_json"])
        return value

    def pending_agent_reviews(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT * FROM agent_review_requests WHERE status='pending'
               ORDER BY created_at,request_id"""
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["request"] = json.loads(value.pop("request_json"))
            value.pop("decision_json", None)
            result.append(value)
        return result

    def apply_agent_review(
        self,
        *,
        request_id: str,
        evidence_hash: str,
        decision: dict[str, Any],
        decided_by: str,
        disposition: str,
        reason_code: str,
        event_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically bind a strict external agent decision to its evidence event."""

        row = self.db.execute(
            "SELECT * FROM agent_review_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if not row:
            raise ValueError("agent review request is unknown")
        if row["evidence_hash"] != evidence_hash:
            raise ValueError("agent review evidence_hash does not match")
        encoded_decision = json.dumps(
            decision, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        if row["status"] == "decided":
            if row["decision_json"] == encoded_decision and row["decided_by"] == decided_by:
                event = self.event(row["event_id"])
                assert event
                return event
            raise ValueError("agent review request already has a different decision")
        encoded_payload = json.dumps(
            event_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        now = utc_now()
        self.db.execute(
            """UPDATE agent_review_requests
               SET status='decided',decision_json=?,decided_by=?,updated_at=?
               WHERE request_id=?""",
            (encoded_decision, decided_by, now, request_id),
        )
        self.db.execute(
            """UPDATE event_ledger SET disposition=?,reason_code=?,payload_json=?,updated_at=?
               WHERE event_id=?""",
            (disposition, reason_code, encoded_payload, now, row["event_id"]),
        )
        self.db.commit()
        event = self.event(row["event_id"])
        assert event
        return event

    def report(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM report_ledger WHERE report_key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def report_events(self, key: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT e.* FROM report_items i
               JOIN event_ledger e ON e.event_id=i.event_id
               WHERE i.report_key=? ORDER BY i.item_order,e.event_id""",
            (key,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            result.append(value)
        return result

    def report_for_period(
        self, *, audience: str, output_kind: str, period: str
    ) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT * FROM report_ledger
               WHERE audience=? AND output_kind=? AND period=?""",
            (audience, output_kind, period),
        ).fetchone()
        return dict(row) if row else None

    def report_cursor(self, audience: str, output_kind: str) -> dict[str, Any] | None:
        name = f"{audience}:{output_kind}"
        row = self.db.execute(
            "SELECT * FROM report_cursors WHERE cursor_name=?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def get_host_budget(self, host_key: str) -> dict[str, Any] | None:
        host_key = str(host_key or "").strip().casefold()
        if not host_key:
            raise ValueError("host_key is required")
        row = self.db.execute(
            "SELECT * FROM host_budget_circuits WHERE host_key=?", (host_key,)
        ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["canary_required"] = bool(value["canary_required"])
        value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        return value

    def update_host_budget(self, host_key: str, **fields: Any) -> dict[str, Any]:
        """Persist a host-wide rate-limit/circuit transition across scan processes."""

        host_key = str(host_key or "").strip().casefold()
        if not host_key:
            raise ValueError("host_key is required")
        allowed = {
            "circuit_state",
            "blocked_until",
            "reason",
            "status_code",
            "retry_after_seconds",
            "consecutive_limits",
            "canary_required",
            "last_attempt_at",
            "last_success_at",
            "metadata",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported host budget fields: {sorted(unknown)}")
        existing = self.get_host_budget(host_key)
        state = str(
            fields.get(
                "circuit_state", (existing or {}).get("circuit_state", "closed")
            )
        )
        if state not in {"closed", "open", "half_open"}:
            raise ValueError("circuit_state must be closed, open or half_open")
        retry_after = fields.get(
            "retry_after_seconds", (existing or {}).get("retry_after_seconds")
        )
        if retry_after is not None and int(retry_after) < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        consecutive = int(
            fields.get(
                "consecutive_limits", (existing or {}).get("consecutive_limits", 0)
            )
            or 0
        )
        if consecutive < 0:
            raise ValueError("consecutive_limits must be non-negative")
        metadata = fields.get("metadata", (existing or {}).get("metadata") or {})
        if not isinstance(metadata, dict):
            raise ValueError("host budget metadata must be an object")
        now = utc_now()
        created_at = existing["created_at"] if existing else now
        self.db.execute(
            """INSERT INTO host_budget_circuits(
                 host_key,circuit_state,blocked_until,reason,status_code,
                 retry_after_seconds,consecutive_limits,canary_required,
                 last_attempt_at,last_success_at,metadata_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(host_key) DO UPDATE SET
                 circuit_state=excluded.circuit_state,
                 blocked_until=excluded.blocked_until,
                 reason=excluded.reason,status_code=excluded.status_code,
                 retry_after_seconds=excluded.retry_after_seconds,
                 consecutive_limits=excluded.consecutive_limits,
                 canary_required=excluded.canary_required,
                 last_attempt_at=excluded.last_attempt_at,
                 last_success_at=excluded.last_success_at,
                 metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                host_key,
                state,
                fields.get("blocked_until", (existing or {}).get("blocked_until")),
                fields.get("reason", (existing or {}).get("reason")),
                fields.get("status_code", (existing or {}).get("status_code")),
                int(retry_after) if retry_after is not None else None,
                consecutive,
                int(
                    bool(
                        fields.get(
                            "canary_required",
                            (existing or {}).get("canary_required", state != "closed"),
                        )
                    )
                ),
                fields.get("last_attempt_at", (existing or {}).get("last_attempt_at")),
                fields.get("last_success_at", (existing or {}).get("last_success_at")),
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False),
                created_at,
                now,
            ),
        )
        self.db.commit()
        result = self.get_host_budget(host_key)
        assert result
        return result

    def reset_host_budget(
        self,
        host_key: str,
        *,
        reason: str = "successful_canary",
        at: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_host_budget(host_key)
        return self.update_host_budget(
            host_key,
            circuit_state="closed",
            blocked_until=None,
            reason=reason,
            status_code=None,
            retry_after_seconds=None,
            consecutive_limits=0,
            canary_required=False,
            last_attempt_at=at or utc_now(),
            last_success_at=at or utc_now(),
            metadata=(existing or {}).get("metadata") or {},
        )

    def prepare_report(
        self,
        *,
        key: str,
        audience: str,
        output_kind: str,
        period: str,
        window_start: str,
        window_end: str,
        title: str,
        report_path: str,
        content_hash: str,
        event_ids: list[str],
    ) -> dict[str, Any]:
        if audience not in {"public", "developer"}:
            raise ValueError("report audience must be public or developer")
        if output_kind not in {"daily", "weekly"}:
            raise ValueError("report output_kind must be daily or weekly")
        existing_period = self.report_for_period(
            audience=audience, output_kind=output_kind, period=period
        )
        if existing_period:
            return existing_period
        cursor_name = f"{audience}:{output_kind}"
        now = utc_now()
        self.db.execute(
            """INSERT INTO report_ledger(
                 report_key,audience,output_kind,period,window_start,window_end,
                 cursor_name,title,report_path,status,content_hash,
                 created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?,?,NULL)
               ON CONFLICT(report_key) DO NOTHING""",
            (
                key,
                audience,
                output_kind,
                period,
                window_start,
                window_end,
                cursor_name,
                title,
                report_path,
                content_hash,
                now,
                now,
            ),
        )
        for index, event_id in enumerate(event_ids):
            self.db.execute(
                """INSERT INTO report_items(report_key,event_id,item_order)
                   VALUES(?,?,?) ON CONFLICT(report_key,event_id) DO NOTHING""",
                (key, event_id, index),
            )
        self.db.commit()
        report = self.report(key)
        assert report
        return report

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
        audience: str = "public",
        report_key: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> dict[str, Any]:
        if audience not in {"public", "developer"}:
            raise ValueError("delivery audience must be public or developer")
        now = utc_now()
        self.db.execute(
            """INSERT INTO delivery_outbox(
                 delivery_key,output_kind,period,title,report_path,status,created_at,updated_at,
                 audience,report_key,window_start,window_end,cursor_name
               ) VALUES(?,?,?,?,?,'prepared',?,?,?,?,?,?,?)
               ON CONFLICT(delivery_key) DO NOTHING""",
            (
                key,
                output_kind,
                period,
                title,
                report_path,
                now,
                now,
                audience,
                report_key,
                window_start,
                window_end,
                f"{audience}:{output_kind}",
            ),
        )
        self.db.commit()
        assert self.delivery(key)
        return self.delivery(key) or {}

    def update_delivery(self, key: str, **fields: Any) -> dict[str, Any]:
        allowed = {"doc_url", "doc_token", "message_id", "status", "last_error", "attempts"}
        values = {field: value for field, value in fields.items() if field in allowed}
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{field}=?" for field in values)
        existing = self.delivery(key)
        if not existing:
            raise ValueError("delivery is unknown")
        self.db.execute(
            f"UPDATE delivery_outbox SET {assignments} WHERE delivery_key=?",
            (*values.values(), key),
        )
        if values.get("status") == "completed" and existing.get("report_key"):
            report = self.report(str(existing["report_key"]))
            if not report:
                raise ValueError("delivery report ledger entry is unknown")
            completed_at = utc_now()
            self.db.execute(
                """UPDATE report_ledger SET status='completed',updated_at=?,completed_at=?
                   WHERE report_key=?""",
                (completed_at, completed_at, report["report_key"]),
            )
            current = self.report_cursor(report["audience"], report["output_kind"])
            if (
                current is None
                or datetime.fromisoformat(str(current["delivered_through"]).replace("Z", "+00:00"))
                < datetime.fromisoformat(str(report["window_end"]).replace("Z", "+00:00"))
            ):
                self.db.execute(
                    """INSERT INTO report_cursors(
                         cursor_name,audience,output_kind,delivered_through,last_report_key,updated_at
                       ) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(cursor_name) DO UPDATE SET
                         delivered_through=excluded.delivered_through,
                         last_report_key=excluded.last_report_key,
                         updated_at=excluded.updated_at""",
                    (
                        report["cursor_name"],
                        report["audience"],
                        report["output_kind"],
                        report["window_end"],
                        report["report_key"],
                        completed_at,
                    ),
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
        savepoint = "set_feishu_record_link"
        started_transaction = not self.db.in_transaction
        if started_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            # One visible Base row represents one logical entity at a time.  A
            # pending ``review:<issue>`` link must therefore be replaced when
            # that same row becomes a resolved person, rather than leaving two
            # keys that could race and overwrite/tombstone each other.
            self.db.execute(
                """DELETE FROM feishu_record_links
                   WHERE entity_kind=? AND base_token=? AND table_id=?
                     AND record_id=? AND entity_key!=?""",
                (entity_kind, base_token, table_id, record_id, entity_key),
            )
            self.db.execute(
                """INSERT INTO feishu_record_links VALUES(?,?,?,?,?,?)
                   ON CONFLICT(entity_kind,entity_key,base_token,table_id) DO UPDATE SET
                     record_id=excluded.record_id,updated_at=excluded.updated_at""",
                (entity_kind, entity_key, base_token, table_id, record_id, utc_now()),
            )
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_transaction:
                self.db.commit()
        except BaseException:
            try:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                if started_transaction:
                    self.db.rollback()
            raise
