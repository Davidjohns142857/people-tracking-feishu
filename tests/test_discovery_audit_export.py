"""Synthetic, standard-library tests of the read-only production evidence exporter."""
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
SPEC = importlib.util.spec_from_file_location("discovery_audit_export", ROOT / "scripts/export_discovery_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
ITEM = {"category": "publication", "stable_id": "paper:A", "text": "A concrete paper", "attributes": {"date": "2026-10-27"}}
DELTA = {"additions": [ITEM], "modifications": [], "removals": []}


class DiscoveryAuditTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "artifacts/temporal-audit-20261116/discovery-latency"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="export-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "synthetic.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
        CREATE TABLE event_ledger(event_id TEXT PRIMARY KEY,audience TEXT,event_type TEXT,occurred_at TEXT,observation_id TEXT,run_id TEXT,source_id TEXT,person_key TEXT,disposition TEXT,reason_code TEXT,payload_json TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE observations(observation_id TEXT PRIMARY KEY,run_id TEXT,source_id TEXT,observed_at TEXT,health_status TEXT,semantic_hash TEXT,decision_status TEXT,confirmation_count INTEGER,confirmations_required INTEGER,extractor_version TEXT,delta_json TEXT);
        CREATE TABLE sources(source_id TEXT PRIMARY KEY,person_key TEXT,kind TEXT,url TEXT,cadence_days INTEGER,snapshot_json TEXT,health_status TEXT);
        CREATE TABLE people(person_key TEXT PRIMARY KEY,canonical_name TEXT);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY,started_at TEXT,completed_at TEXT,trigger TEXT,status TEXT,purpose TEXT,observation_errors INTEGER);
        CREATE TABLE runtime_runs(run_id TEXT PRIMARY KEY,action TEXT,status TEXT,started_at TEXT,completed_at TEXT,metrics_json TEXT);
        CREATE TABLE agent_review_requests(request_id TEXT PRIMARY KEY,event_id TEXT,snapshot_id TEXT,status TEXT,created_at TEXT,updated_at TEXT,decision_json TEXT,request_json TEXT);
        CREATE TABLE report_ledger(report_key TEXT PRIMARY KEY,audience TEXT,output_kind TEXT,period TEXT,window_start TEXT,window_end TEXT,title TEXT,status TEXT,created_at TEXT,updated_at TEXT,completed_at TEXT);
        CREATE TABLE report_items(report_key TEXT,event_id TEXT,item_order INTEGER);
        CREATE TABLE delivery_outbox(delivery_key TEXT,report_key TEXT,audience TEXT,output_kind TEXT,status TEXT,created_at TEXT,updated_at TEXT,attempts INTEGER);
        CREATE TABLE report_cursors(cursor_name TEXT,audience TEXT,output_kind TEXT,delivered_through TEXT,last_report_key TEXT,updated_at TEXT);
        CREATE TABLE portable_meta(key TEXT,value_json TEXT);
        """)
        self.db.execute("INSERT INTO people VALUES('person-a','Alice')")
        self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)", ("source-a", "person-a", "homepage", "https://example.org/alice", 7, json.dumps({"extractor_version": "v2", "html": "NOT HISTORICAL HTML"}), "healthy"))
        self.observe("baseline", "2026-11-01T00:00:00+00:00", "baseline", {})
        self.observe("candidate", "2026-11-08T00:00:00+00:00", "candidate", DELTA)
        self.observe("confirmed", "2026-11-09T00:00:00+00:00", "changed", DELTA)
        self.event("event-a", "2026-11-09T00:00:00+00:00")
        self.report("report-a", "2026-11-08T00:00:00+00:00", "2026-11-15T00:00:00+00:00")
        self.db.execute("INSERT INTO report_items VALUES('report-a','event-a',0)")
        self.db.execute("INSERT INTO agent_review_requests VALUES(?,?,?,?,?,?,?,?)", ("review-a", "event-a", "snapshot-a", "decided", "2026-11-09T01:00:00+00:00", "2026-11-14T00:00:00+00:00", json.dumps({"decision": "publish", "confidence": .95}), "{}"))
        self.db.commit()

    def observe(self, oid, at, status, delta, source="source-a", parser="v2"):
        rid = "run-" + oid
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (rid, at, at, "synthetic", "completed", "production", 0))
        self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (oid, rid, source, at, "healthy", "hash-" + oid, status, 1 if status == "candidate" else 2, 2, parser, json.dumps(delta)))

    def event(self, eid, at, observation="confirmed", source="source-a", raw=None):
        payload = {"person_name": "Alice", "action": "addition", "source_url": "https://example.org/alice", "headline": "发现论文", "what_changed": "发现论文 A", "raw_item": ITEM if raw is None else raw, "internal_secret": "DO-NOT-EXPORT"}
        self.db.execute("INSERT INTO event_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (eid, "public", "person_signal", at, observation, "run-" + observation, source, "person-a", "publish", "execution_agent_publish", json.dumps(payload), at, at))

    def report(self, key, start, end, audience="public", kind="weekly"):
        self.db.execute("INSERT INTO report_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key, audience, kind, key, start, end, "Synthetic report", "completed", end, end, end))

    def export(self, **kwargs):
        self.db.commit()
        reader = audit.AuditDatabase(self.path)
        try:
            return reader.export(**kwargs)
        finally:
            reader.close()

    def test_exact_report_membership_includes_late_events_and_preserves_hash(self):
        self.event("outside-window-but-member", "2026-10-20T00:00:00+00:00")
        self.event("inside-window-not-member", "2026-11-10T00:00:00+00:00")
        self.db.execute("INSERT INTO report_items VALUES('report-a','outside-window-but-member',1)")
        self.db.commit()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.export(report_key="report-a")
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual([e["event"]["event_id"] for e in result["events"]], ["event-a", "outside-window-but-member"])
        detail = result["events"][0]
        self.assertEqual(detail["confirmation_observation"]["observed_at"], "2026-11-09T00:00:00+00:00")
        self.assertEqual(detail["discovery_evidence"]["earliest_matching_candidate_at"], "2026-11-08T00:00:00+00:00")
        self.assertIsNone(detail["discovery_evidence"]["first_ever_detected_at"])
        self.assertEqual(detail["previous_healthy_observation"]["observation_id"], "candidate")
        self.assertNotIn("NOT HISTORICAL HTML", json.dumps(result))
        reader = audit.AuditDatabase(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM event_ledger")
        finally:
            reader.close()

    def test_window_selection_is_half_open_and_requires_timezone(self):
        self.event("at-start", "2026-11-08T00:00:00+00:00")
        result = self.export(since="2026-11-08T08:00:00+08:00", until="2026-11-09T08:00:00+08:00")
        self.assertEqual([e["event"]["event_id"] for e in result["events"]], ["event-a"])
        with self.assertRaises(audit.AuditError):
            self.export(since="2026-11-08", until="2026-11-09")
        with self.assertRaises(audit.AuditError):
            self.export(since="2026-11-10T00:00:00Z", until="2026-11-09T00:00:00Z")

    def test_reset_and_full_delta_changes_stop_candidate_matching(self):
        self.observe("old-candidate", "2026-11-02T00:00:00+00:00", "candidate", DELTA)
        self.observe("reset", "2026-11-07T00:00:00+00:00", "baseline", {})
        detail = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual([x["observation_id"] for x in detail["matched_candidates"]], ["candidate"])
        self.assertEqual(detail["boundary"]["observation_id"], "reset")
        self.assertIsNone(detail["first_ever_detected_at"])
        replacement = {**DELTA, "removals": [{"category": "publication", "stable_id": "paper:B", "text": "B"}]}
        self.db.execute("UPDATE observations SET delta_json=? WHERE observation_id='candidate'", (json.dumps(replacement),))
        detail = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(detail["matched_candidates"], [])
        self.assertEqual(detail["boundary"]["reason"], "candidate_delta_changed_or_unmatched")

    def test_same_item_id_with_different_fact_or_source_is_not_prior_discovery(self):
        changed_fact = {**ITEM, "text": "An older different fact"}
        self.db.execute("UPDATE observations SET delta_json=? WHERE observation_id='candidate'", (json.dumps({**DELTA, "additions": [changed_fact]}),))
        detail = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(detail["matched_candidates"], [])
        self.db.execute("UPDATE event_ledger SET source_id='another-source' WHERE event_id='event-a'")
        detail = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(detail["status"], "confirmation_source_does_not_match_event")
        self.assertIsNone(detail["earliest_evidenced_at"])

    def test_payload_fact_must_match_confirmation_before_assigning_any_discovery_time(self):
        payload = {"action": "addition", "raw_item": {**ITEM, "text": "Another claimed fact"}}
        self.db.execute("UPDATE event_ledger SET payload_json=?", (json.dumps(payload),))
        evidence = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(evidence["status"], "event_item_does_not_match_confirmation_delta")
        self.assertIsNone(evidence["earliest_evidenced_at"])

    def test_pending_review_update_is_not_presented_as_decision_time(self):
        decided = self.export(report_key="report-a")["events"][0]["reviews"][0]
        self.assertEqual(decided["decision_updated_at"], "2026-11-14T00:00:00+00:00")
        self.db.execute("UPDATE agent_review_requests SET status='pending',decision_json=NULL")
        pending = self.export(report_key="report-a")["events"][0]["reviews"][0]
        self.assertIsNone(pending["decision_updated_at"])
        self.assertIsNotNone(pending["updated_at"])

    def test_parser_change_and_equal_timestamp_order_do_not_invent_candidate_chain(self):
        self.db.execute("UPDATE observations SET extractor_version='v1' WHERE observation_id='candidate'")
        evidence = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(evidence["matched_candidates"], [])
        self.assertEqual(evidence["boundary"]["reason"], "parser_version_changed")
        self.db.execute("UPDATE observations SET extractor_version='v2',observed_at='2026-11-09T00:00:00+00:00' WHERE observation_id='candidate'")
        evidence = self.export(report_key="report-a")["events"][0]["discovery_evidence"]
        self.assertEqual(evidence["matched_candidates"], [])
        self.assertEqual(evidence["boundary"]["reason"], "same_time_order_is_unknown")

    def test_credentials_and_unbounded_metadata_are_not_exported(self):
        self.db.execute("INSERT INTO portable_meta VALUES('secret',?)", (json.dumps({"access_token": "META-SECRET"}),))
        self.db.execute("UPDATE sources SET url=?,snapshot_json=?", ("https://user:URL-PASSWORD@example.org/alice?token=URL-TOKEN&user=public-user#FRAGMENT-SECRET", json.dumps({"extractor_version": "v2", "cookie": "SNAPSHOT-SECRET"})))
        self.db.execute("UPDATE agent_review_requests SET request_json=?", (json.dumps({"cookie": "REQUEST-SECRET"}),))
        self.db.execute("INSERT INTO runtime_runs VALUES(?,?,?,?,?,?)", ("runtime-a", "scan", "completed", "2026-11-08T00:00:00Z", "2026-11-08T00:00:01Z", json.dumps({"sources_due": 0, "access_token": "METRIC-SECRET", "network_recovery": {"secret": "NESTED-SECRET"}})))
        output = json.dumps(self.export(report_key="report-a"))
        for value in ("META-SECRET", "URL-PASSWORD", "URL-TOKEN", "FRAGMENT-SECRET", "SNAPSHOT-SECRET", "REQUEST-SECRET", "METRIC-SECRET", "NESTED-SECRET", "DO-NOT-EXPORT"):
            self.assertNotIn(value, output)
        self.assertIn("public-user", output)
        self.assertIn('"sources_due": 0', output)

    def test_latest_twenty_public_weekly_reports_only(self):
        for i in range(1, 25):
            self.report("weekly-" + str(i), "2026-10-01T00:00:00Z", f"2026-11-{i:02}T00:00:00Z")
        self.report("daily", "2026-11-25T00:00:00Z", "2026-11-26T00:00:00Z", kind="daily")
        self.report("developer", "2026-11-25T00:00:00Z", "2026-11-26T00:00:00Z", audience="developer")
        self.db.commit()
        reader = audit.AuditDatabase(self.path)
        try:
            reports = reader.report_list()
        finally:
            reader.close()
        self.assertEqual(len(reports), 20)
        self.assertEqual(reports[0]["report_key"], "weekly-24")
        self.assertTrue(all(r["audience"] == "public" and r["output_kind"] == "weekly" for r in reports))

    def test_cli_stdout_secure_file_and_dangerous_output_rejection(self):
        self.db.commit()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(audit.main(["--database", str(self.path), "--list-reports"]), 0)
        self.assertEqual(json.loads(stream.getvalue())["reports"][0]["report_key"], "report-a")
        output = self.directory / "audit.json"
        self.assertEqual(audit.main(["--database", str(self.path), "--report-key", "report-a", "--output", str(output)]), 0)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        before = self.path.read_bytes()
        for target in (self.path, Path(str(self.path) + "-wal"), output):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(audit.main(["--database", str(self.path), "--report-key", "report-a", "--output", str(target)]), 2)
        link = self.directory / "linked.json"
        link.symlink_to(output)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(audit.main(["--database", str(self.path), "--report-key", "report-a", "--output", str(link)]), 2)
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_optional_history_does_not_invent_discovery(self):
        self.db.execute("DELETE FROM observations")
        self.db.execute("DROP TABLE runtime_runs")
        result = self.export(report_key="report-a")
        evidence = result["events"][0]["discovery_evidence"]
        self.assertEqual(evidence["status"], "missing_confirmation_or_item_evidence")
        self.assertIsNone(evidence["earliest_evidenced_at"])
        self.assertIn("optional table unavailable: runtime_runs", result["warnings"])


if __name__ == "__main__":
    unittest.main()
