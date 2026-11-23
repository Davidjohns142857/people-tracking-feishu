"""Synthetic standard-library checks of the read-only, anonymous coverage audit."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("coverage_audit_export", ROOT / "scripts/export_coverage_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
SINCE = "2026-11-08T00:00:00+00:00"
UNTIL = "2026-11-15T00:00:00+00:00"


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "artifacts/temporal-audit-20261116/coverage-audit"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="export-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "synthetic.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
        CREATE TABLE people(person_key TEXT PRIMARY KEY,canonical_name TEXT,profile_json TEXT);
        CREATE TABLE sources(source_id TEXT PRIMARY KEY,person_key TEXT,kind TEXT,url TEXT,tracking_enabled INTEGER,health_status TEXT,cadence_days INTEGER,last_checked_at TEXT,last_success_at TEXT,next_check_at TEXT,snapshot_json TEXT,quality_json TEXT,binding_status TEXT,candidate_count INTEGER,candidate_first_seen_at TEXT);
        CREATE TABLE observations(observation_id TEXT PRIMARY KEY,run_id TEXT,source_id TEXT,observed_at TEXT,health_status TEXT,decision_status TEXT,semantic_hash TEXT,delta_json TEXT,summary TEXT,quality_json TEXT,extractor_version TEXT,confirmation_count INTEGER,confirmations_required INTEGER);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,started_at TEXT,completed_at TEXT,trigger TEXT,status TEXT,purpose TEXT,observation_errors INTEGER,error_json TEXT);
        CREATE TABLE runtime_runs(run_id TEXT PRIMARY KEY,action TEXT,status TEXT,started_at TEXT,completed_at TEXT,metrics_json TEXT);
        CREATE TABLE event_ledger(event_id TEXT PRIMARY KEY,audience TEXT,event_type TEXT,occurred_at TEXT,observation_id TEXT,run_id TEXT,source_id TEXT,person_key TEXT,disposition TEXT,reason_code TEXT,payload_json TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE agent_review_requests(request_id TEXT PRIMARY KEY,event_id TEXT,snapshot_id TEXT,status TEXT,created_at TEXT,updated_at TEXT,decision_json TEXT,request_json TEXT);
        CREATE TABLE report_ledger(report_key TEXT PRIMARY KEY,audience TEXT,output_kind TEXT,period TEXT,window_start TEXT,window_end TEXT,title TEXT,status TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT,report_path TEXT);
        CREATE TABLE report_items(report_key TEXT,event_id TEXT,item_order INTEGER);
        CREATE TABLE delivery_outbox(delivery_key TEXT,report_key TEXT,audience TEXT,output_kind TEXT,status TEXT,created_at TEXT,updated_at TEXT,attempts INTEGER,doc_token TEXT,last_error TEXT);
        CREATE TABLE report_cursors(cursor_name TEXT,audience TEXT,output_kind TEXT,delivered_through TEXT,last_report_key TEXT,updated_at TEXT);
        CREATE TABLE portable_meta(key TEXT,value_json TEXT);
        """)
        self.db.executemany("INSERT INTO people VALUES(?,?,?)", [
            ("private-person-a", "PRIVATE ALICE NAME", '{"biography":"PROFILE-SECRET"}'),
            ("private-person-b", "PRIVATE BOB NAME", "{}"),
        ])
        for source, person, health in (("private-source-a", "private-person-a", "blocked"), ("private-source-b", "private-person-b", "healthy")):
            self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (source, person, "homepage", "https://URL-SECRET.example/?token=URL-TOKEN", 1, health, 7, "2026-11-20T00:00:00Z", "2026-11-20T00:00:00Z", "2026-11-27T00:00:00Z", '{"html":"SNAPSHOT-SECRET"}', '{"cookie":"QUALITY-SECRET"}', "verified", 0, None))
        self.observe("before", "2026-11-07T00:00:00Z", "unchanged")
        self.observe("at-start", SINCE, "candidate")
        self.observe("in-window", "2026-11-09T00:00:00Z", "unchanged")
        self.observe("at-end", UNTIL, "source_issue", health="blocked")
        self.observe("future-a", "2026-11-16T00:00:00Z", "changed")
        self.observe("future-b", "2026-11-16T00:00:00Z", "unchanged", source="private-source-b")
        self.event("private-event-start", SINCE)
        self.event("private-event-in", "2026-11-09T00:00:00Z")
        self.event("private-event-end", UNTIL)
        self.event("private-event-future", "2026-11-16T00:00:00Z")
        self.db.execute("INSERT INTO agent_review_requests VALUES(?,?,?,?,?,?,?,?)", ("private-review", "private-event-in", "private-snapshot", "decided", "2026-11-09T00:00:00Z", "2026-11-10T00:00:00Z", '{"decision":"publish","reason":"REVIEW-REASON-SECRET","token":"DECISION-SECRET"}', '{"authorization":"REQUEST-SECRET"}'))
        self.db.execute("INSERT INTO portable_meta VALUES('credential',?)", ('{"access_token":"META-SECRET"}',))
        self.db.execute("INSERT INTO runtime_runs VALUES(?,?,?,?,?,?)", ("private-runtime", "scan", "completed", "2026-11-09T00:00:00Z", "2026-11-09T00:01:00Z", '{"sources_due":2,"token":"RUNTIME-SECRET","network":{"cookie":"NESTED-SECRET"}}'))
        self.db.commit()

    def observe(self, oid, at, decision, *, health="healthy", source="private-source-a"):
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", ("private-run-" + oid, at, at, "synthetic", "completed", "production", 0, '{"token":"RUN-SECRET"}'))
        self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("private-observation-" + oid, "private-run-" + oid, source, at, health, decision, "private-hash", '{"additions":[{"text":"BODY-SECRET"}]}', "SUMMARY-SECRET", '{"token":"OBSERVATION-SECRET"}', "v2", 1, 2))

    def event(self, eid, at):
        self.db.execute("INSERT INTO event_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (eid, "public", "person_signal", at, "private-observation-in-window", "private-run-in-window", "private-source-a", "private-person-a", "publish", "execution_agent_publish", '{"person_name":"PRIVATE ALICE NAME","headline":"EVENT-BODY-SECRET","source_url":"https://EVENT-URL-SECRET.example/","token":"EVENT-TOKEN"}', at, at))

    def export(self, path=None, **kwargs):
        self.db.commit()
        reader = audit.AuditDatabase(path or self.path)
        try:
            return reader.export(since=kwargs.get("since", SINCE), until=kwargs.get("until", UNTIL))
        finally:
            reader.close()

    def args(self, *extra):
        return ["--database", str(self.path), "--since", SINCE, "--until", UNTIL, *extra]

    def test_export_contract_and_read_only_connection_preserve_database_hash(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.export()
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue({"schema_version", "selection", "current_registry", "window_coverage", "funnel", "reviews", "publication", "runs", "source_diagnostics", "person_diagnostics", "unavailable", "limitations"}.issubset(result))
        reader = audit.AuditDatabase(self.path)
        try:
            self.assertEqual(reader.db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM people")
        finally:
            reader.close()

    def test_timezone_equivalent_half_open_window_excludes_future_and_start(self):
        result = self.export(since="2026-11-08T08:00:00+08:00", until="2026-11-15T08:00:00+08:00")
        coverage = result["window_coverage"]
        self.assertEqual(coverage["observations"], 2)
        self.assertEqual(coverage["sources_observed"], 1)
        self.assertEqual(coverage["people_observed"], 1)
        self.assertEqual(coverage["healthy_comparable_sources"], 1)
        self.assertEqual(coverage["healthy_comparable_people"], 1)
        self.assertEqual(result["funnel"].get("candidate", 0), 0)
        self.assertEqual(result["funnel"].get("changed", 0), 0)
        self.assertEqual(result["funnel"]["source_issue"], 1)
        self.assertEqual(result["publication"]["events_in_window"], 2)
        self.assertEqual(result["runs"]["started_in_window"], 2)

    def test_historical_health_and_current_health_are_not_conflated(self):
        result = self.export()
        self.assertEqual(result["current_registry"]["people_registered"], 2)
        self.assertEqual(result["current_registry"]["people_with_enabled_sources"], 2)
        self.assertEqual(result["current_registry"]["by_source_kind"]["homepage"], {"registered": 2, "enabled": 2})
        observed = next(row for row in result["source_diagnostics"] if row["observations_in_window"] == 2)
        self.assertEqual(observed["current_health_status"], "blocked")
        self.assertTrue(observed["healthy_comparable_in_window"])
        self.assertEqual(observed["last_observed_at_as_of_until"], UNTIL)
        self.assertEqual(observed["last_health_as_of_until"], "blocked")
        self.assertEqual(observed["last_decision_as_of_until"], "source_issue")
        future_only = next(row for row in result["source_diagnostics"] if row["observations_in_window"] == 0)
        self.assertEqual(future_only["current_health_status"], "healthy")
        self.assertTrue(future_only["not_observed_in_window"])
        self.assertFalse(future_only["healthy_comparable_in_window"])
        self.assertIsNone(future_only["last_observed_at_as_of_until"])

    def test_anonymous_export_does_not_leak_identity_urls_bodies_or_secrets(self):
        result = self.export()
        output = json.dumps(result, ensure_ascii=False)
        for value in ("PRIVATE ALICE NAME", "PRIVATE BOB NAME", "private-person", "private-source", "private-event", "private-observation", "private-run", "private-review", "private-snapshot", "URL-SECRET", "URL-TOKEN", "PROFILE-SECRET", "SNAPSHOT-SECRET", "QUALITY-SECRET", "BODY-SECRET", "SUMMARY-SECRET", "OBSERVATION-SECRET", "EVENT-URL-SECRET", "EVENT-TOKEN", "REVIEW-REASON-SECRET", "DECISION-SECRET", "REQUEST-SECRET", "META-SECRET", "RUNTIME-SECRET", "NESTED-SECRET", "RUN-SECRET"):
            with self.subTest(secret=value):
                self.assertNotIn(value, output)
        for row in result["source_diagnostics"]:
            self.assertRegex(row["source"], r"^S[0-9a-f]+$")
            self.assertRegex(row["person"], r"^P[0-9a-f]+$")

    def test_arbitrary_kind_status_and_reason_text_is_not_an_export_channel(self):
        self.db.execute("UPDATE sources SET kind='KIND-IDENTITY-SECRET',health_status='HEALTH-IDENTITY-SECRET' WHERE source_id='private-source-b'")
        self.db.execute("UPDATE observations SET decision_status='DECISION-IDENTITY-SECRET' WHERE observation_id='private-observation-in-window'")
        self.db.execute("UPDATE event_ledger SET reason_code='REASON-IDENTITY-SECRET' WHERE event_id='private-event-in'")
        self.db.execute("UPDATE runs SET status='STATUS-IDENTITY-SECRET' WHERE run_id='private-run-in-window'")
        output = json.dumps(self.export())
        for token in ("KIND-IDENTITY-SECRET", "HEALTH-IDENTITY-SECRET", "DECISION-IDENTITY-SECRET", "REASON-IDENTITY-SECRET", "STATUS-IDENTITY-SECRET"):
            self.assertNotIn(token, output)

    def test_invalid_naive_equal_and_reverse_windows_are_rejected(self):
        for since, until in (("2026-11-08", UNTIL), (SINCE, "2026-11-15T00:00:00"), (UNTIL, SINCE), (UNTIL, UNTIL), ("invalid", UNTIL)):
            with self.subTest(since=since, until=until):
                with self.assertRaises(audit.AuditError):
                    self.export(since=since, until=until)

    def test_existing_empty_database_is_not_migrated(self):
        empty = self.directory / "empty.sqlite3"
        conn = sqlite3.connect(empty)
        conn.close()
        before = empty.read_bytes()
        result = self.export(path=empty)
        self.assertTrue(result["unavailable"])
        self.assertEqual(before, empty.read_bytes())
        conn = sqlite3.connect(f"file:{empty}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master").fetchall(), [])
        finally:
            conn.close()

    def test_missing_tables_and_columns_report_unavailable_without_crashing(self):
        self.db.execute("DROP TABLE agent_review_requests")
        self.db.execute("DROP TABLE observations")
        self.db.execute("CREATE TABLE observations(observation_id TEXT PRIMARY KEY)")
        result = self.export()
        unavailable = json.dumps(result["unavailable"])
        self.assertIn("agent_review_requests", unavailable)
        self.assertIn("observations", unavailable)
        self.assertIsNone(result["window_coverage"]["observations"])

    def test_missing_database_is_not_created(self):
        missing = self.directory / "does-not-exist.sqlite3"
        with self.assertRaises((audit.AuditError, sqlite3.OperationalError, OSError)):
            audit.AuditDatabase(missing)
        self.assertFalse(missing.exists())

    def test_volatile_candidates_have_reset_clues_without_invented_first_discovery(self):
        for index in range(3):
            oid = "volatile-" + str(index)
            self.observe(oid, f"2026-11-{10 + index:02}T00:00:00Z", "candidate")
            self.db.execute("UPDATE observations SET confirmation_count=?,delta_json=? WHERE observation_id=?",
                            (2 if index == 0 else 1, json.dumps({"additions": [{"text": "synthetic-" + str(index)}]}), "private-observation-" + oid))
        result = self.export()
        source = next(s for s in result["source_diagnostics"] if s["observations_in_window"] > 0)
        clues = source["candidate_reset_clues"]
        self.assertEqual(clues["confirmation_count_decreases_in_window"], 1)
        self.assertEqual(clues["candidate_delta_changes_in_window"], 2)
        self.assertIsNone(clues["first_ever_detected_at"])

    def test_weekly_membership_does_not_claim_post_cutoff_completion(self):
        for key, completion in (("report-completed", "2026-11-14T00:00:00Z"), ("report-late", "2026-11-16T00:00:00Z")):
            self.db.execute("INSERT INTO report_ledger(report_key,audience,output_kind,status,completed_at) VALUES(?,?,?,?,?)",
                            (key, "public", "weekly", "completed", completion))
        self.db.execute("INSERT INTO report_items VALUES('report-completed','private-event-in',0)")
        self.db.execute("INSERT INTO report_items VALUES('report-late','private-event-end',0)")
        publication = self.export()["publication"]
        self.assertEqual(publication["currently_publish_events_in_window"], 2)
        self.assertEqual(publication["window_events_linked_to_weekly_reports"], 2)
        self.assertEqual(publication["window_events_in_completed_weekly_reports"], 1)
        self.assertEqual(publication["historical_dispositions_at_until"], "unavailable")

    def test_actual_reporting_reason_codes_remain_readable_unknown_text_is_hashed(self):
        known = ("parenthetical_text_only", "formatting_or_punctuation_only",
                 "page_removal_is_not_a_real_world_event", "career_signal_requires_execution_agent",
                 "grey_zone_requires_execution_agent", "missing_concrete_before_or_after",
                 "important_publication_status")
        for index, code in enumerate(known):
            eid = "reason-event-" + str(index)
            self.event(eid, "2026-11-10T00:00:00Z")
            self.db.execute("UPDATE event_ledger SET reason_code=? WHERE event_id=?", (code, eid))
        self.db.execute("UPDATE event_ledger SET reason_code='UNKNOWN-REASON-SECRET' WHERE event_id='private-event-in'")
        result = self.export()
        reason_counts = result["publication"]["current_reason_counts"]
        for code in known:
            self.assertEqual(reason_counts[code], 1)
        self.assertNotIn("UNKNOWN-REASON-SECRET", json.dumps(result))
        self.assertTrue(any(k.startswith("reason_") for k in reason_counts))

    def test_portable_pipeline_counts_are_separate_from_tracker_scans_and_private_metrics(self):
        rows = [
            ("portable-roster", "roster_sync", "failed", "2026-11-10T00:00:00Z", "2026-11-10T00:01:00Z", '{"secret":"PORTABLE-METRIC-SECRET"}'),
            ("portable-unknown", "PORTABLE-ACTION-SECRET", "partial", "2026-11-11T00:00:00Z", "2026-11-16T00:00:00Z", '{"secret":"PORTABLE-METRIC-SECRET"}'),
            ("portable-future", "visible_master_sync", "completed", "2026-11-16T00:00:00Z", "2026-11-16T00:01:00Z", "{}"),
        ]
        self.db.executemany("INSERT INTO runtime_runs VALUES(?,?,?,?,?,?)", rows)
        result = self.export()
        portable = result["runtime_runs"]
        self.assertEqual(result["runs"]["started_in_window"], 2)
        self.assertEqual(portable["started_in_window"], 3)
        self.assertEqual(portable["action_counts"]["scan"], 1)
        self.assertEqual(portable["action_counts"]["roster_sync"], 1)
        self.assertNotIn("visible_master_sync", portable["action_counts"])
        self.assertEqual(portable["action_status_counts"]["roster_sync:failed"], 1)
        self.assertFalse(result["execution_layers"]["counts_are_additive"])
        self.assertEqual(result["execution_layers"]["cron_registration_evidence"], "unavailable")
        self.assertNotIn("PORTABLE-ACTION-SECRET", json.dumps(result))
        self.assertNotIn("PORTABLE-METRIC-SECRET", json.dumps(result))
        unknown = next(r for r in portable["diagnostics"] if r["action"].startswith("action_"))
        self.assertIsNone(unknown["completed_at_by_until"])

    def test_cli_stdout_and_secure_exclusive_output(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(audit.main(self.args()), 0)
        self.assertEqual(json.loads(stream.getvalue())["window_coverage"]["observations"], 2)
        output = self.directory / "coverage.json"
        self.assertEqual(audit.main(self.args("--output", str(output))), 0)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        original = output.read_bytes()
        before = self.path.read_bytes()
        targets = (output, self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))
        for target in targets:
            with self.subTest(target=target), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(self.args("--output", str(target))), 2)
        self.assertEqual(original, output.read_bytes())
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())

    def test_cli_rejects_symlink_and_dangling_symlink_outputs(self):
        target = self.directory / "existing.txt"
        target.write_text("PRESERVE", encoding="utf-8")
        link = self.directory / "linked.json"
        link.symlink_to(target)
        dangling = self.directory / "dangling.json"
        nonexistent = self.directory / "new-target.json"
        dangling.symlink_to(nonexistent)
        for output in (link, dangling):
            with self.subTest(output=output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(self.args("--output", str(output))), 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "PRESERVE")
        self.assertFalse(nonexistent.exists())


if __name__ == "__main__":
    unittest.main()
