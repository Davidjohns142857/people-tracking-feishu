#!/usr/bin/env python3
"""Read-only, anonymous coverage export for an explicit (since, until] window.

Standard library only; never imports the runtime, performs migrations, fetches
URLs, or exports names, URLs, page bodies, review prose, config or credentials.
Current registration/configuration and cutoff-time observations are deliberately
reported separately. This is an evidence inventory, not a recall estimate.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

SCHEMA = "people-tracking-coverage-audit-v1"
FIELDS = {
    "people": "person_key created_at".split(),
    "sources": "source_id person_key kind tracking_enabled cadence_days health_status".split(),
    "source_roster_records": "person_key status".split(),
    "observations": "observation_id run_id source_id observed_at health_status decision_status confirmation_count confirmations_required delta_json".split(),
    "runs": "run_id started_at completed_at status purpose".split(),
    "runtime_runs": "run_id action status started_at completed_at".split(),
    "event_ledger": "event_id audience event_type occurred_at source_id person_key disposition reason_code updated_at".split(),
    "agent_review_requests": "request_id event_id status created_at updated_at decision_json".split(),
    "report_items": "event_id report_key".split(),
    "report_ledger": "report_key audience output_kind status window_start window_end completed_at".split(),
}
KINDS = {"homepage", "linkedin", "github", "scholar"}
CAREER_KINDS = {"homepage", "linkedin"}
HEALTH = {"healthy", "blocked", "degraded", "gone", "never_checked", "transport_error", "rate_limited"}
DECISIONS = {"baseline", "unchanged", "noise", "candidate", "changed", "ambiguous", "binding_review", "binding_conflict", "source_issue", "source_issue_pending", "parser_anomaly"}
COMPARABLE = {"baseline", "unchanged", "noise", "candidate", "changed"}
DISPOSITIONS = {"publish", "suppress", "pending_review", "developer"}
RUN_STATUSES = {"running", "completed", "partial", "failed", "cancelled"}
RUNTIME_ACTIONS = {"scan", "scheduler_agent_required", "master_bridge_pending", "roster_sync", "visible_master_sync"}
REASONS = {
    "execution_agent_publish", "execution_agent_suppress", "execution_agent_defer",
    "awaiting_execution_agent", "new_publication", "new_repository_or_project",
    "ui_control_noise", "academic_year_rollover", "description_only_edit",
    "same_value", "same_to_same", "missing_concrete_added_fact",
    "concrete_position_change", "current_role_or_company_change",
    "new_position", "new_affiliation", "new_award", "concrete_affiliation_change",
    "concrete_award_change", "parser_anomaly", "source_issue", "source_issue_pending",
    "low_value", "insufficient_evidence", "not_material", "manual",
    "parenthetical_text_only", "formatting_or_punctuation_only",
    "page_removal_is_not_a_real_world_event", "career_signal_requires_execution_agent",
    "grey_zone_requires_execution_agent", "missing_concrete_before_or_after",
    "important_publication_status",
}
LIMITATIONS = [
    "Current registered/enabled/roster-active counts describe the database at export time, not historical enrollment at the cutoff. Historical registration and enablement are unavailable.",
    "Cutoff health and last-check times come only from retained production observations at or before until; current sources.health_status/last_checked_at/next_check_at are never used as historical evidence.",
    "Overdue is an explicit counterfactual using the current cadence and latest retained check at cutoff; the historical cadence, original due plan and actual scheduler eligibility are unavailable.",
    "Homepage/LinkedIn are potential career channels, not proof that Experience/employment sections were returned or parsed. News/social/name searches are outside the four registered-source channels.",
    "Comparable means healthy with baseline/unchanged/noise/candidate/changed. It does not prove every relevant page section was present; failures and parser/identity anomalies are excluded.",
    "Candidate reset clues compare retained rows/counts/deltas only; they do not establish first-ever discovery, continuity of an individual fact, or the real event date.",
    "Ledger occurred_at is used solely as ledger selection time. Older runtimes store the confirming observation time there, not the event occurrence time.",
    "Review/event dispositions are current mutable stored values. Historical dispositions at until are unavailable unless a separate immutable decision history is provided.",
    "Run completion times after until are excluded, but a current run status cannot be reconstructed at historical until. Counts are not evidence of a live worker.",
    "Tracker runs and portable runtime_runs are separate logging layers and must not be added as independent scan counts. Portable action counts identify recorded pipeline steps only; no cron registration, scheduler installation or future execution evidence is available from these tables.",
    "Anonymous P/S/E/R IDs are stable truncated hashes, with no mapping exported. Unknown reason codes are hashed rather than copied. No raw snapshot, delta, review prose or config is exported.",
    "SQLite mode=ro, query_only and one read transaction; no runtime imports, migrations, network calls or data-modifying SQL.",
]


class AuditError(ValueError):
    pass


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise AuditError("timestamps must use ISO 8601 with an explicit timezone") from exc
    if result.tzinfo is None:
        raise AuditError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def maybe_time(value):
    try:
        return timestamp(value) if value else None
    except AuditError:
        return None


def anonymous(prefix, value):
    return prefix + hashlib.sha256(str(value).encode()).hexdigest()[:20] if value is not None else None


def enum(value, allowed):
    return value if value in allowed else "unknown"


def reason(value):
    return value if value in REASONS else anonymous("reason_", value) if value else "unknown"


def counts(values):
    return dict(sorted(Counter(values).items()))


def safe_number(value):
    return value if type(value) in (int, float) and value == value and abs(value) != float("inf") else None


def delta_fingerprint(value):
    try:
        parsed = json.loads(value) if value else None
        if not isinstance(parsed, dict):
            return None
        encoded = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()
    except (TypeError, ValueError):
        return None


class AuditDatabase:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve(strict=True)
        if not self.path.is_file():
            raise AuditError("database must be an existing file")
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("BEGIN")
        existing = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.columns = {t: {r[1] for r in self.db.execute('PRAGMA table_info("' + t + '")')} for t in FIELDS if t in existing}
        self.unavailable = []

    def close(self):
        self.db.close()

    def has(self, table, fields=()):
        missing = [f for f in fields if f not in self.columns.get(table, set())]
        if table not in self.columns or missing:
            message = table + (" missing columns: " + ",".join(missing) if table in self.columns else " unavailable")
            if message not in self.unavailable:
                self.unavailable.append(message)
            return False
        return True

    def rows(self, table, required=(), where="", parameters=()):
        if not self.has(table, required):
            return None
        fields = FIELDS[table]
        columns = self.columns[table]
        select = ",".join('"' + f + '"' if f in columns else 'NULL AS "' + f + '"' for f in fields)
        query = 'SELECT ' + select + ' FROM "' + table + '"' + (" WHERE " + where if where else "")
        return [dict(row) for row in self.db.execute(query, parameters)]

    def export(self, since, until):
        start, end = timestamp(since), timestamp(until)
        if start >= end:
            raise AuditError("--since must be earlier than --until")
        self.unavailable = []
        for table, fields in FIELDS.items():
            self.has(table, fields)
        sources = self.rows("sources", ("source_id", "person_key", "kind"))
        people = self.rows("people", ("person_key",))
        all_runs = self.rows("runs", ("run_id", "started_at"), "julianday(started_at)<=julianday(?)", (end.isoformat(),))
        run_map = {r["run_id"]: r for r in all_runs or []}
        production_filter = self.has("runs", ("run_id", "purpose"))
        observations = self.rows("observations", ("observation_id", "source_id", "observed_at"), "julianday(observed_at)<=julianday(?)", (end.isoformat(),))
        excluded_nonproduction = 0
        unknown_purpose = 0
        usable_observations = []
        for row in observations or []:
            run = run_map.get(row["run_id"])
            if production_filter and run and run["purpose"] not in {None, "production"}:
                excluded_nonproduction += 1
                continue
            if not production_filter or run is None or run["purpose"] is None:
                unknown_purpose += 1
            row["_time"] = maybe_time(row["observed_at"])
            if row["_time"]:
                usable_observations.append(row)
        usable_observations.sort(key=lambda r: (r["_time"], str(r["observation_id"])))
        window_obs = [r for r in usable_observations if start < r["_time"] <= end]
        by_source = defaultdict(list)
        for row in usable_observations:
            by_source[row["source_id"]].append(row)
        source_map = {s["source_id"]: s for s in sources or []}
        person_keys = {p["person_key"] for p in people or []} | {s["person_key"] for s in sources or [] if s["person_key"] is not None}
        enabled_known = self.has("sources", ("tracking_enabled",))
        cadence_known = self.has("sources", ("cadence_days",))
        health_known = self.has("observations", ("health_status", "decision_status"))
        obs_counts_known = observations is not None

        def comparable(row):
            return row["health_status"] == "healthy" and row["decision_status"] in COMPARABLE

        registry_kinds = {}
        for kind in sorted(KINDS):
            selected = [s for s in sources or [] if s["kind"] == kind]
            registry_kinds[kind] = {"registered": len(selected) if sources is not None else None,
                                    "enabled": sum(s["tracking_enabled"] == 1 for s in selected) if enabled_known else None}
        enabled_sources = [s for s in sources or [] if s["tracking_enabled"] == 1] if enabled_known else []
        enabled_people = {s["person_key"] for s in enabled_sources}
        career_people = {s["person_key"] for s in enabled_sources if s["kind"] in CAREER_KINDS}
        active_roster = self.rows("source_roster_records", ("person_key", "status"))
        registry = {
            "time_basis": "current_export_snapshot_not_historical_cutoff",
            "people_registered": len(people) if people is not None else None,
            "sources_registered": len(sources) if sources is not None else None,
            "sources_enabled": len(enabled_sources) if enabled_known else None,
            "people_with_enabled_sources": len(enabled_people) if enabled_known else None,
            "active_roster_people": len({r["person_key"] for r in active_roster or [] if r["status"] == "active"}) if active_roster is not None else None,
            "active_people_definition": "current people with at least one currently enabled source; authoritative roster-active count reported separately when available",
            "by_source_kind": registry_kinds,
            "people_without_enabled_career_source": len(person_keys - career_people) if enabled_known and sources is not None else None,
            "enabled_people_without_enabled_career_source": len(enabled_people - career_people) if enabled_known else None,
            "historical_registration_enablement": "unavailable",
        }
        observed_source_ids = {o["source_id"] for o in window_obs}
        comparable_sources = {o["source_id"] for o in window_obs if comparable(o)} if health_known else set()
        observed_people = {source_map[s]["person_key"] for s in observed_source_ids if s in source_map}
        comparable_people = {source_map[s]["person_key"] for s in comparable_sources if s in source_map}
        window = {
            "observations": len(window_obs) if obs_counts_known else None,
            "sources_observed": len(observed_source_ids) if obs_counts_known else None,
            "people_observed": len(observed_people) if obs_counts_known and sources is not None else None,
            "healthy_comparable_sources": len(comparable_sources) if health_known else None,
            "healthy_comparable_people": len(comparable_people) if health_known and sources is not None else None,
            "current_enabled_sources_not_observed": len({s["source_id"] for s in enabled_sources} - observed_source_ids) if enabled_known and obs_counts_known else None,
            "observation_status_counts": counts(enum(o["decision_status"], DECISIONS) for o in window_obs) if self.has("observations", ("decision_status",)) else None,
            "excluded_nonproduction_observations_through_until": excluded_nonproduction,
            "observations_with_unknown_run_purpose_through_until": unknown_purpose,
            "production_filter": "recorded production plus separately counted unknown-purpose rows" if production_filter else "unavailable; includes retained rows with unknown purpose",
        }
        funnel = {status: sum(o["decision_status"] == status for o in window_obs) if self.has("observations", ("decision_status",)) else None
                  for status in ("baseline", "unchanged", "noise", "source_issue", "source_issue_pending", "parser_anomaly", "binding_review", "binding_conflict", "ambiguous", "candidate", "changed")}
        source_diagnostics = []
        for source in sources or []:
            history = by_source[source["source_id"]]
            period = [o for o in history if start < o["_time"] <= end]
            last = history[-1] if history else None
            last_comparable = next((o for o in reversed(history) if comparable(o)), None) if health_known else None
            candidates = [o for o in history if o["decision_status"] == "candidate"]
            decreases = delta_changes = boundaries = 0
            previous_candidate = None
            previous = None
            for observation in history:
                if observation["decision_status"] == "candidate":
                    if start < observation["_time"] <= end:
                        if previous_candidate:
                            old_count, new_count = safe_number(previous_candidate["confirmation_count"]), safe_number(observation["confirmation_count"])
                            decreases += bool(old_count is not None and new_count is not None and new_count < old_count)
                            old_delta = delta_fingerprint(previous_candidate["delta_json"])
                            new_delta = delta_fingerprint(observation["delta_json"])
                            delta_changes += bool(old_delta is not None and new_delta is not None and old_delta != new_delta)
                        boundaries += bool(previous and previous["decision_status"] in {"baseline", "unchanged", "noise", "parser_anomaly", "changed"})
                    previous_candidate = observation
                elif observation["decision_status"] in {"baseline", "unchanged", "noise", "parser_anomaly", "changed"}:
                    previous_candidate = None
                previous = observation
            cadence = safe_number(source["cadence_days"])
            age_days = (end - last["_time"]).total_seconds() / 86400 if last else None
            source_diagnostics.append({
                "source": anonymous("S", source["source_id"]), "person": anonymous("P", source["person_key"]),
                "kind": enum(source["kind"], KINDS),
                "current_enabled": source["tracking_enabled"] == 1 if enabled_known else None,
                "current_health_status": enum(source["health_status"], HEALTH) if source["health_status"] is not None else None,
                "current_cadence_days": cadence,
                "observations_in_window": len(period) if obs_counts_known else None,
                "healthy_comparable_in_window": any(comparable(o) for o in period) if health_known else None,
                "last_observed_at_as_of_until": last["_time"].isoformat() if last else None,
                "last_health_as_of_until": enum(last["health_status"], HEALTH) if last and health_known else None,
                "last_decision_as_of_until": enum(last["decision_status"], DECISIONS) if last else None,
                "latest_healthy_comparable_at_as_of_until": last_comparable["_time"].isoformat() if last_comparable else None,
                "days_since_last_retained_check_at_cutoff": age_days,
                "not_observed_in_window": not bool(period) if obs_counts_known else None,
                "overdue_under_current_cadence_assumption": age_days > cadence if last and cadence is not None and cadence > 0 else None,
                "historical_enabled": "unavailable",
                "candidate_reset_clues": {
                    "retained_candidate_observations_through_until": len(candidates) if self.has("observations", ("decision_status",)) else None,
                    "oldest_retained_candidate_observed_at": candidates[0]["_time"].isoformat() if candidates else None,
                    "latest_retained_candidate_observed_at": candidates[-1]["_time"].isoformat() if candidates else None,
                    "first_ever_detected_at": None,
                    "confirmation_count_decreases_in_window": decreases if self.has("observations", ("confirmation_count",)) else None,
                    "candidate_delta_changes_in_window": delta_changes if self.has("observations", ("delta_json",)) else None,
                    "candidate_after_reset_boundary_in_window": boundaries,
                },
            })
        person_diagnostics = []
        for person_key in sorted(person_keys, key=str):
            person_sources = [s for s in source_diagnostics if s["person"] == anonymous("P", person_key)]
            person_diagnostics.append({
                "person": anonymous("P", person_key), "registered_sources_current": len(person_sources),
                "enabled_sources_current": sum(s["current_enabled"] is True for s in person_sources) if enabled_known else None,
                "has_enabled_career_channel_current": any(s["current_enabled"] and s["kind"] in CAREER_KINDS for s in person_sources) if enabled_known else None,
                "observed_in_window": person_key in observed_people if obs_counts_known else None,
                "healthy_comparable_in_window": person_key in comparable_people if health_known else None,
                "healthy_comparable_career_channel_in_window": any(s["kind"] in CAREER_KINDS and s["healthy_comparable_in_window"] for s in person_sources) if health_known else None,
            })
        events = self.rows("event_ledger", ("event_id", "occurred_at", "audience", "event_type"), "audience='public' AND event_type='person_signal' AND julianday(occurred_at)>julianday(?) AND julianday(occurred_at)<=julianday(?)", (start.isoformat(), end.isoformat()))
        reports = self.rows("report_ledger", ("report_key", "audience", "output_kind"))
        report_map = {r["report_key"]: r for r in reports or []}
        links = self.rows("report_items", ("report_key", "event_id"))
        weekly_links = defaultdict(list)
        for link in links or []:
            report = report_map.get(link["report_key"])
            if report and report["audience"] == "public" and report["output_kind"] == "weekly":
                weekly_links[link["event_id"]].append(report)
        event_diagnostics = []
        for event in events or []:
            linked = weekly_links[event["event_id"]]
            completed = [r for r in linked if r["status"] == "completed" and maybe_time(r["completed_at"]) and timestamp(r["completed_at"]) <= end]
            event_diagnostics.append({"event": anonymous("E", event["event_id"]), "source": anonymous("S", event["source_id"]),
                                      "person": anonymous("P", event["person_key"]), "ledger_selected_at": timestamp(event["occurred_at"]).isoformat(),
                                      "current_disposition": enum(event["disposition"], DISPOSITIONS), "current_reason": reason(event["reason_code"]),
                                      "weekly_report_memberships_current": len(linked) if links is not None and reports is not None else None,
                                      "completed_weekly_reports_by_until": len(completed) if self.has("report_ledger", ("completed_at", "status")) else None})
        publication = {
            "events_in_window": len(events) if events is not None else None,
            "current_disposition_counts": counts(enum(e["disposition"], DISPOSITIONS) for e in events or []) if self.has("event_ledger", ("disposition",)) else None,
            "current_reason_counts": counts(reason(e["reason_code"]) for e in events or []) if self.has("event_ledger", ("reason_code",)) else None,
            "currently_publish_events_in_window": sum(e["disposition"] == "publish" for e in events or []) if events is not None and self.has("event_ledger", ("disposition",)) else None,
            "window_events_linked_to_weekly_reports": sum(bool(e["weekly_report_memberships_current"]) for e in event_diagnostics) if links is not None and reports is not None else None,
            "window_events_in_completed_weekly_reports": sum(bool(e["completed_weekly_reports_by_until"]) for e in event_diagnostics) if links is not None and reports is not None and self.has("report_ledger", ("completed_at", "status")) else None,
            "historical_dispositions_at_until": "unavailable",
            "membership_basis": "current report_items; only completed_at<=until is eligible as completion evidence",
        }
        review_rows = self.rows("agent_review_requests", ("event_id", "status", "updated_at"), "julianday(updated_at)>julianday(?) AND julianday(updated_at)<=julianday(?)", (start.isoformat(), end.isoformat()))
        review_decisions = []
        for row in review_rows or []:
            try:
                decision = json.loads(row["decision_json"] or "{}")
            except (ValueError, TypeError):
                decision = {}
            review_decisions.append(enum(decision.get("decision") if isinstance(decision, dict) else None, {"publish", "suppress", "defer"}))
        reviews = {"review_rows_updated_in_window": len(review_rows) if review_rows is not None else None,
                   "current_status_counts": counts(enum(r["status"], {"pending", "decided", "cancelled"}) for r in review_rows or []) if review_rows is not None else None,
                   "current_decision_counts": counts(review_decisions) if self.has("agent_review_requests", ("decision_json",)) else None,
                   "reason_counts": publication["current_reason_counts"],
                   "reason_counts_basis": "current event reason codes for ledger-selected events, not review prose",
                   "historical_dispositions_at_until": "unavailable"}
        selected_runs = [r for r in all_runs or [] if maybe_time(r["started_at"]) and start < timestamp(r["started_at"]) <= end]
        completion_times = [timestamp(r["completed_at"]) for r in all_runs or [] if maybe_time(r["completed_at"]) and timestamp(r["completed_at"]) <= end]
        runs = {"started_in_window": len(selected_runs) if all_runs is not None else None,
                "status_counts": counts(enum(r["status"], RUN_STATUSES) for r in selected_runs) if self.has("runs", ("status",)) else None,
                "purpose_counts": counts(enum(r["purpose"], {"production", "acceptance", "validation"}) for r in selected_runs) if self.has("runs", ("purpose",)) else None,
                "latest_started_at": max((timestamp(r["started_at"]) for r in all_runs or [] if maybe_time(r["started_at"])), default=None),
                "latest_completed_at": max(completion_times, default=None),
                "status_as_of_until_unavailable": True,
                "status_counts_basis": "current status of runs whose started_at lies in the window"}
        runs["latest_started_at"] = runs["latest_started_at"].isoformat() if runs["latest_started_at"] else None
        runs["latest_completed_at"] = runs["latest_completed_at"].isoformat() if runs["latest_completed_at"] else None
        portable_rows = self.rows("runtime_runs", ("run_id", "action", "started_at"), "julianday(started_at)<=julianday(?)", (end.isoformat(),))
        portable_window = [r for r in portable_rows or [] if maybe_time(r["started_at"]) and start < timestamp(r["started_at"]) <= end]

        def portable_action(value):
            return value if value in RUNTIME_ACTIONS else anonymous("action_", value) if value else "unknown"

        portable_starts = [timestamp(r["started_at"]) for r in portable_rows or [] if maybe_time(r["started_at"])]
        portable_completions = [timestamp(r["completed_at"]) for r in portable_rows or [] if maybe_time(r["completed_at"]) and timestamp(r["completed_at"]) <= end]
        runtime_runs = {
            "logging_layer": "portable_pipeline",
            "started_in_window": len(portable_window) if portable_rows is not None else None,
            "action_counts": counts(portable_action(r["action"]) for r in portable_window) if portable_rows is not None else None,
            "status_counts": counts(enum(r["status"], RUN_STATUSES) for r in portable_window) if self.has("runtime_runs", ("status",)) else None,
            "action_status_counts": counts(portable_action(r["action"]) + ":" + enum(r["status"], RUN_STATUSES) for r in portable_window) if portable_rows is not None and self.has("runtime_runs", ("status",)) else None,
            "latest_started_at": max(portable_starts).isoformat() if portable_starts else None,
            "latest_completed_at": max(portable_completions).isoformat() if portable_completions else None,
            "status_as_of_until_unavailable": True,
            "diagnostics": [{"run": anonymous("R", r["run_id"]), "action": portable_action(r["action"]),
                             "current_status": enum(r["status"], RUN_STATUSES),
                             "started_at": timestamp(r["started_at"]).isoformat(),
                             "completed_at_by_until": timestamp(r["completed_at"]).isoformat() if maybe_time(r["completed_at"]) and timestamp(r["completed_at"]) <= end else None}
                            for r in sorted(portable_window, key=lambda r: (timestamp(r["started_at"]), str(r["run_id"])))],
        }
        return {"schema_version": SCHEMA,
                "selection": {"since_exclusive": start.isoformat(), "until_inclusive": end.isoformat(), "historical_registration_enablement": "unavailable"},
                "current_registry": registry, "window_coverage": window, "funnel": funnel,
                "reviews": reviews, "publication": publication, "runs": runs, "runtime_runs": runtime_runs,
                "execution_layers": {"tracker_scans": "runs", "portable_pipeline": "runtime_runs", "cron_registration_evidence": "unavailable", "counts_are_additive": False},
                "source_diagnostics": sorted(source_diagnostics, key=lambda r: r["source"]),
                "person_diagnostics": person_diagnostics, "event_diagnostics": event_diagnostics,
                "unavailable": sorted(self.unavailable), "limitations": LIMITATIONS}


def write_output(value, output, database):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if not output:
        sys.stdout.write(encoded)
        return
    path = Path(output).expanduser()
    db_path = Path(database).expanduser().resolve()
    if path.resolve() in {db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")}:
        raise AuditError("output must not overwrite the database or SQLite sidecar files")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.absolute().parents):
        raise AuditError("output must not be a symbolic link or use symbolic-link directories")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--since", required=True, help="exclusive ISO 8601 timestamp, timezone required")
    parser.add_argument("--until", required=True, help="inclusive ISO 8601 timestamp, timezone required")
    parser.add_argument("--output", type=Path, help="create a new 0600 JSON file, never overwrite")
    args = parser.parse_args(argv)
    database = None
    try:
        database = AuditDatabase(args.database)
        value = database.export(args.since, args.until)
        write_output(value, args.output, args.database)
    except (AuditError, OSError, sqlite3.Error, ValueError) as exc:
        print("coverage audit export failed: " + str(exc), file=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
