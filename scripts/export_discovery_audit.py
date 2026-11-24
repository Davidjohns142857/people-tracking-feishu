#!/usr/bin/env python3
"""Export bounded discovery evidence without importing or migrating the runtime.

Uses only the standard library. Selection windows are (since, until] in ledger
time, which older runtimes populate with the confirming observation's time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA = "people-tracking-discovery-audit-v1"
HISTORY_LIMIT = 200
RUN_LIMIT = 1000
EVENT_FIELDS = "event_id audience event_type occurred_at observation_id run_id source_id person_key disposition reason_code created_at updated_at".split()
OBS_FIELDS = "observation_id run_id source_id observed_at health_status semantic_hash decision_status confirmation_count confirmations_required extractor_version".split()
SOURCE_FIELDS = "source_id person_key kind url cadence_days next_check_at last_checked_at last_changed_at last_success_at health_status consecutive_failures tracking_enabled full_fetch_interval_days last_full_fetch_at".split()
REPORT_FIELDS = "report_key audience output_kind period window_start window_end title status created_at updated_at completed_at".split()
RUN_FIELDS = "run_id started_at completed_at trigger status purpose observation_errors".split()
ITEM_FIELDS = {"category", "stable_id", "before_stable_id", "after_stable_id", "field", "text", "before", "after", "changed_fields"}
ATTRIBUTE_FIELDS = {"title", "year", "date", "start_date", "end_date", "company", "organization", "employment_type", "authors", "venue", "published_at", "accepted_at"}
SECRET_QUERY = {"access_token", "api_key", "apikey", "auth_token", "auth", "authorization", "id_token", "key", "password", "refresh_token", "secret", "sig", "signature", "token", "x-amz-security-token", "x-amz-credential", "x-amz-signature", "x-goog-credential", "x-goog-signature"}
LIMITATIONS = [
    "Ledger occurred_at is a ledger selection time, not proof of the real-world event date; older runtimes use confirming observed_at.",
    "Earliest matching observations are retained evidence, not proof of first-ever discovery or when a page actually went online.",
    "History contains compact deltas and parser/health metadata; current source snapshots do not reconstruct historical page bodies or HTTP responses.",
    "Candidate matching is conservative: same source, action and exact item fingerprint, with an equal whole-delta fingerprint for continuity; resets and changed deltas stop the chain.",
    "Review updated_at is reported separately; only status=decided permits using it as a recorded decision-update time, not an independently measured execution time.",
    "Source cadence and snapshot parser metadata describe current stored configuration, not necessarily historical configuration.",
    "SQLite is opened with mode=ro and query_only; no runtime classes, migrations, credentials, portable_meta, full snapshots or raw review requests are loaded/exported.",
]


class AuditError(ValueError):
    pass


def parse_json(value, default=None):
    try:
        def invalid_constant(_):
            raise ValueError("non-finite JSON number")

        return json.loads(value, parse_constant=invalid_constant) if value else default
    except (ValueError, TypeError):
        return default


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuditError("timestamps must use ISO 8601 with an explicit timezone") from exc
    if result.tzinfo is None:
        raise AuditError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def comparison_delta(delta):
    return {key: value for key, value in delta.items()
            if key not in {"actionable_signals", "routed_career_items"}}


def safe_url(value):
    try:
        p = urlsplit(str(value))
        if p.scheme not in {"http", "https"} or not p.hostname:
            return "[non-http URL omitted]"
        # Rebuild netloc from hostname/port, dropping username/password.
        host = p.hostname
        if ":" in host:
            host = "[" + host + "]"
        if p.port:
            host += ":" + str(p.port)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.casefold() not in SECRET_QUERY]
        return urlunsplit((p.scheme, host, p.path, urlencode(query), ""))
    except ValueError:
        return "[invalid URL omitted]"


def safe_value(value):
    if isinstance(value, str):
        return re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m.group(0)), value)
    if isinstance(value, list):
        return [safe_value(v) for v in value if isinstance(v, (str, int, float, bool)) or v is None]
    return value if isinstance(value, (int, float, bool)) or value is None else None


def public_item(item):
    if not isinstance(item, dict):
        return None
    result = {k: safe_value(v) for k, v in item.items() if k in ITEM_FIELDS}
    for key in ("attributes", "before_attributes", "after_attributes"):
        if isinstance(item.get(key), dict):
            result[key] = {k: safe_value(v) for k, v in item[key].items() if k in ATTRIBUTE_FIELDS}
    return result


def pick(row, fields):
    return {field: row.get(field) for field in fields}


class AuditDatabase:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve(strict=True)
        if not self.path.is_file():
            raise AuditError("database must be an existing file")
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("BEGIN")  # One consistent read snapshot, even on a live DB.
        self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._columns = {}
        self.warnings = []
        if "event_ledger" not in self.tables:
            self.close()
            raise AuditError("database has no event_ledger; this exporter requires the portable event-ledger schema")

    def close(self):
        self.db.close()

    def columns(self, table):
        if table not in self.tables:
            return set()
        if table not in self._columns:
            self._columns[table] = {r[1] for r in self.db.execute('PRAGMA table_info("' + table + '")')}
        return self._columns[table]

    def rows(self, table, fields, where="", parameters=(), order="", limit=None):
        if table not in self.tables:
            message = "optional table unavailable: " + table
            if message not in self.warnings:
                self.warnings.append(message)
            return []
        # Every identifier and SQL fragment originates in this module, never CLI text.
        columns = self.columns(table)
        select = ",".join('"' + f + '"' if f in columns else 'NULL AS "' + f + '"' for f in fields)
        query = 'SELECT ' + select + ' FROM "' + table + '"'
        if where:
            query += " WHERE " + where
        if order:
            query += " ORDER BY " + order
        if limit is not None:
            query += " LIMIT ?"
            parameters = (*parameters, limit)
        return [dict(r) for r in self.db.execute(query, parameters)]

    def report_list(self):
        if "report_ledger" not in self.tables:
            raise AuditError("database has no report_ledger")
        reports = self.rows("report_ledger", REPORT_FIELDS, "audience='public' AND output_kind='weekly'", order="julianday(window_end) DESC,report_key", limit=20)
        for report in reports:
            report["item_count"] = len(self.rows("report_items", ["event_id"], "report_key=?", (report["report_key"],)))
        return reports

    def select_events(self, report_key=None, since=None, until=None):
        report = None
        if report_key:
            reports = self.rows("report_ledger", REPORT_FIELDS, "report_key=? AND audience='public' AND output_kind='weekly'", (report_key,))
            if not reports:
                raise AuditError("report key does not identify a public weekly report")
            report = reports[0]
            ids = self.rows("report_items", ["event_id", "item_order"], "report_key=?", (report_key,), order="item_order,event_id")
            events = []
            for linked in ids:
                rows = self.rows("event_ledger", EVENT_FIELDS + ["payload_json"], "event_id=? AND audience='public' AND event_type='person_signal'", (linked["event_id"],))
                if rows:
                    events.extend(rows)
                else:
                    self.warnings.append("report item has no public person_signal: " + str(linked["event_id"]))
        else:
            start, end = timestamp(since), timestamp(until)
            if start >= end:
                raise AuditError("--since must be earlier than --until")
            events = self.rows("event_ledger", EVENT_FIELDS + ["payload_json"], "audience='public' AND event_type='person_signal' AND julianday(occurred_at)>julianday(?) AND julianday(occurred_at)<=julianday(?)", (start.isoformat(), end.isoformat()), order="julianday(occurred_at),event_id")
        return report, events

    def observation(self, observation_id):
        rows = self.rows("observations", OBS_FIELDS + ["delta_json"], "observation_id=?", (observation_id,)) if observation_id else []
        return rows[0] if rows else None

    def history(self, source_id, at):
        rows = self.rows("observations", OBS_FIELDS + ["delta_json"], "source_id=? AND julianday(observed_at)<=julianday(?)", (source_id, at), order="julianday(observed_at) DESC,observation_id DESC", limit=HISTORY_LIMIT + 1)
        return rows[:HISTORY_LIMIT], len(rows) > HISTORY_LIMIT

    def run(self, run_id):
        rows = self.rows("runs", RUN_FIELDS, "run_id=?", (run_id,))
        return rows[0] if rows else None

    def candidate_evidence(self, event, payload, anchor, history, truncated):
        raw, action = payload.get("raw_item"), payload.get("action")
        result = {"first_ever_detected_at": None, "earliest_evidenced_at": None, "earliest_matching_candidate_at": None, "matched_candidates": [], "boundary": None, "status": "unknown", "history_truncated": truncated}
        if payload.get("source_kind") == "career_search":
            result.update(status="execution_agent_search_evidence", earliest_evidenced_at=event["occurred_at"])
            return result
        if anchor and anchor.get("source_id") != event.get("source_id"):
            result["status"] = "confirmation_source_does_not_match_event"
            return result
        if not anchor or not isinstance(raw, dict) or action not in {"addition", "modification", "removal"}:
            result["status"] = "missing_confirmation_or_item_evidence"
            return result
        plural = {"addition": "additions", "modification": "modifications", "removal": "removals"}[action]
        anchor_delta = parse_json(anchor.get("delta_json"), {})
        if not isinstance(anchor_delta, dict):
            anchor_delta = {}
        if payload.get("career_evidence") is True and any(
            isinstance(entry, dict) and entry.get("action") == action and entry.get("item") == raw
            for entry in anchor_delta.get("actionable_signals", [])
        ):
            result.update(status="first_reliable_career_observation", earliest_evidenced_at=anchor["observed_at"])
            return result
        anchor_delta = comparison_delta(anchor_delta)
        item_hash = fingerprint(raw)
        result.update({"action": action, "item_fingerprint": item_hash, "stable_item_key": safe_value(raw.get("stable_id") or raw.get("after_stable_id") or raw.get("field")), "confirmation_delta_fingerprint": fingerprint(anchor_delta)})
        anchor_items = anchor_delta.get(plural)
        if not isinstance(anchor_items, list) or not any(isinstance(i, dict) and fingerprint(i) == item_hash for i in anchor_items):
            result["status"] = "event_item_does_not_match_confirmation_delta"
            return result
        result["earliest_evidenced_at"] = anchor["observed_at"]
        for previous in history:
            if previous["observation_id"] == anchor["observation_id"]:
                continue
            if timestamp(previous["observed_at"]) >= timestamp(anchor["observed_at"]):
                result["boundary"] = {"observation_id": previous["observation_id"], "reason": "same_time_order_is_unknown", "observed_at": previous["observed_at"]}
                break
            run = self.run(previous.get("run_id"))
            if run and run.get("purpose") not in (None, "production"):
                continue  # acceptance/validation observations cannot advance production state
            status = previous["decision_status"]
            if status in {"source_issue", "source_issue_pending", "binding_review", "binding_conflict"} or previous["health_status"] != "healthy":
                if status != "parser_anomaly":
                    continue  # these do not establish earlier absence or a reset
            delta = parse_json(previous.get("delta_json"), {})
            item_matches = isinstance(delta, dict) and isinstance(delta.get(plural), list) and any(isinstance(i, dict) and fingerprint(i) == item_hash for i in delta[plural])
            parser_changed = bool(previous.get("extractor_version") and anchor.get("extractor_version") and previous["extractor_version"] != anchor["extractor_version"])
            if status == "candidate" and item_matches and fingerprint(comparison_delta(delta)) == fingerprint(anchor_delta) and not parser_changed:
                result["matched_candidates"].append(pick(previous, OBS_FIELDS))
                result["earliest_matching_candidate_at"] = previous["observed_at"]
                result["earliest_evidenced_at"] = previous["observed_at"]
                continue
            result["boundary"] = {"observation_id": previous["observation_id"], "observed_at": previous["observed_at"], "decision_status": status, "reason": "parser_version_changed" if parser_changed else "baseline_or_candidate_reset" if status in {"baseline", "unchanged", "noise", "parser_anomaly", "changed"} else "candidate_delta_changed_or_unmatched"}
            break
        if result["matched_candidates"]:
            result["status"] = "matching_candidate_evidence_before_confirmation"
        else:
            result["status"] = "only_confirmation_evidenced_no_matching_prior_candidate"
        return result

    def event_detail(self, event):
        payload = parse_json(event.get("payload_json"), {})
        if not isinstance(payload, dict):
            payload = {}
        anchor = self.observation(event.get("observation_id"))
        history, truncated = self.history(event["source_id"], anchor["observed_at"] if anchor else event["occurred_at"]) if event.get("source_id") else ([], False)
        sources = self.rows("sources", SOURCE_FIELDS + ["snapshot_json"], "source_id=?", (event.get("source_id"),))
        source = pick(sources[0], SOURCE_FIELDS) if sources else None
        if source:
            source["url"] = safe_url(source["url"])
            snapshot = parse_json(sources[0].get("snapshot_json"), {})
            source["current_snapshot_metadata"] = {k: safe_value(snapshot[k]) for k in ("extractor_version", "retrieval_mode", "kind") if isinstance(snapshot, dict) and k in snapshot}
        people = self.rows("people", ["person_key", "canonical_name"], "person_key=?", (event.get("person_key"),))
        anchor_time = timestamp(anchor["observed_at"] if anchor else event["occurred_at"])
        prior_healthy = next((pick(o, OBS_FIELDS) for o in history if o["health_status"] == "healthy" and timestamp(o["observed_at"]) < anchor_time), None)
        reviews = self.rows("agent_review_requests", ["request_id", "event_id", "snapshot_id", "status", "created_at", "updated_at", "decision_json"], "event_id=?", (event["event_id"],))
        for review in reviews:
            decision = parse_json(review.pop("decision_json"), {})
            review["decision"] = decision.get("decision") if isinstance(decision, dict) else None
            review["confidence"] = decision.get("confidence") if isinstance(decision, dict) else None
            review["decision_updated_at"] = review["updated_at"] if review["status"] == "decided" else None
        links = self.rows("report_items", ["report_key", "item_order"], "event_id=?", (event["event_id"],), order="report_key,item_order")
        for link in links:
            reports = self.rows("report_ledger", REPORT_FIELDS, "report_key=?", (link["report_key"],))
            link["report"] = reports[0] if reports else None
            link["deliveries"] = self.rows("delivery_outbox", ["delivery_key", "audience", "output_kind", "status", "created_at", "updated_at", "attempts"], "report_key=?", (link["report_key"],)) if "report_key" in self.columns("delivery_outbox") else []
        timeline = []
        for observation in reversed(history):
            entry = pick(observation, OBS_FIELDS)
            run = self.run(observation.get("run_id"))
            entry["run"] = run
            timeline.append(entry)
        return {"event": pick(event, EVENT_FIELDS), "person": people[0] if people else {"person_key": event.get("person_key"), "canonical_name": safe_value(payload.get("person_name"))}, "source": source, "fact": {"action": payload.get("action"), "change_type": safe_value(payload.get("change_type")), "headline": safe_value(payload.get("headline")), "what_changed": safe_value(payload.get("what_changed")), "source_url": safe_url(payload["source_url"]) if payload.get("source_url") else None, "item": public_item(payload.get("raw_item"))}, "confirmation_observation": pick(anchor, OBS_FIELDS) if anchor else None, "discovery_evidence": self.candidate_evidence(event, payload, anchor, history, truncated), "previous_healthy_observation": prior_healthy, "source_observation_timeline": timeline, "reviews": reviews, "report_links": links}

    def export(self, report_key=None, since=None, until=None):
        report, events = self.select_events(report_key, since, until)
        details = [self.event_detail(event) for event in events]
        times = [timestamp(e["occurred_at"]) for e in events]
        if report:
            times.extend([timestamp(report["window_start"]), timestamp(report["window_end"])])
        elif since:
            times.extend([timestamp(since), timestamp(until)])
        start = min(times) - timedelta(days=15) if times else None
        end = max(times) if times else None
        scan_runs = self.rows("runs", RUN_FIELDS, "julianday(started_at)>=julianday(?) AND julianday(started_at)<=julianday(?)", (start.isoformat(), end.isoformat()), order="julianday(started_at),run_id", limit=RUN_LIMIT + 1) if start else []
        runtime_runs = self.rows("runtime_runs", ["run_id", "action", "status", "started_at", "completed_at", "metrics_json"], "julianday(started_at)>=julianday(?) AND julianday(started_at)<=julianday(?)", (start.isoformat(), end.isoformat()), order="julianday(started_at),run_id", limit=RUN_LIMIT + 1) if start else []
        for run in runtime_runs:
            metrics = parse_json(run.pop("metrics_json"), {})
            metrics = metrics if isinstance(metrics, dict) else {}
            fields = {"sources_due", "sources_enabled", "sources_planned", "sources_attempted", "force_all", "force_full_fetch", "full_fetch_planned", "full_fetch_attempted", "homepage_planned", "homepage_attempted", "tracker_run_id", "tracker_run_status"}
            run["scan_metrics"] = {k: v for k, v in metrics.items() if k in fields and isinstance(v, (str, int, float, bool))}
            validation = metrics.get("validation")
            if isinstance(validation, dict):
                run["scan_metrics"]["validation"] = {k: v for k, v in validation.items() if k in {"passed", "observed", "enabled", "coverage", "healthy", "minimum_coverage"} and isinstance(v, (int, float, bool))}
        return {"schema_version": SCHEMA, "selection": {"report_key": report_key, "since_exclusive": since, "until_inclusive": until, "basis": "exact_report_membership" if report_key else "ledger_occurred_at"}, "report": report, "event_count": len(details), "events": details, "run_context_window": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None}, "scan_runs": scan_runs[:RUN_LIMIT], "runtime_runs": runtime_runs[:RUN_LIMIT], "runs_truncated": len(scan_runs) > RUN_LIMIT or len(runtime_runs) > RUN_LIMIT, "public_weekly_cursor": self.rows("report_cursors", ["cursor_name", "audience", "output_kind", "delivered_through", "last_report_key", "updated_at"], "audience='public' AND output_kind='weekly'"), "warnings": self.warnings, "limitations": LIMITATIONS}


def write_output(value, output, database):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if not output:
        sys.stdout.write(encoded)
        return
    path = Path(output).expanduser()
    resolved = path.resolve()
    db_path = Path(database).expanduser().resolve()
    if resolved in {db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")}:
        raise AuditError("output must not overwrite the database or SQLite sidecar files")
    if path.is_symlink():
        raise AuditError("output must not be a symbolic link")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--list-reports", action="store_true", help="list latest 20 public weekly reports")
    selection.add_argument("--report-key")
    selection.add_argument("--since", help="exclusive ledger timestamp; ISO 8601 with timezone")
    parser.add_argument("--until", help="inclusive ledger timestamp, required with --since")
    parser.add_argument("--output", type=Path, help="create a new mode-0600 JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    if bool(args.since) != bool(args.until):
        parser.error("--since and --until must be supplied together")
    database = None
    try:
        database = AuditDatabase(args.database)
        if args.list_reports:
            value = {"schema_version": SCHEMA, "reports": database.report_list(), "warnings": database.warnings, "limitations": LIMITATIONS}
        else:
            value = database.export(args.report_key, args.since, args.until)
        write_output(value, args.output, args.database)
    except (AuditError, OSError, sqlite3.Error, ValueError) as exc:
        print("discovery audit export failed: " + str(exc), file=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
