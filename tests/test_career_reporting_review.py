"""Career facts must reach review; cosmetic edits and unsupported exits must not."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from people_tracking_feishu.reporting import classify_delta_item, materialize_window_events
from people_tracking_feishu.state import PortableState


class CareerReviewTests(unittest.TestCase):
    def classify(self, item, action="modification"):
        return classify_delta_item(action=action, item=item, source_kind="homepage")

    def test_material_parenthetical_and_description_changes_require_agent_review(self):
        cases = [
            {"category": "position", "before": "Research engineer (Acme)", "after": "Research engineer (NewCo)", "changed_fields": ["company"]},
            {"category": "identity", "before": "Researcher", "after": "Researcher (co-founder of NewCo)", "field": "current_role"},
            {"category": "position", "before": "Research on reliable systems", "after": "I founded NewCo and became CEO", "changed_fields": ["description"]},
            {"category": "research", "before": "I work on reliable systems", "after": "I work on reliable systems (co-founder of NewCo)", "field": "summary"},
            {"category": "project", "before": "Research engineer at Acme; leads the project", "after": "Research engineer at NewCo; leads the project", "changed_fields": ["description"]},
            {"category": "content", "before": "我研究机器学习", "after": "我研究机器学习（现加入新公司担任首席技术官）", "changed_fields": ["description"]},
            # Mixed homepage parsers can classify a founding biography as a paper.
            {"category": "publication", "before": "AI researcher", "after": "AI researcher (Founding Member at Recursive)", "changed_fields": ["description"]},
            {"category": "affiliation", "before": "Example Institute (Lab A)", "after": "Example Institute (Lab B)"},
            # A company can change in structured attributes without changing title text.
            {"category": "position", "before": "Research scientist", "after": "Research scientist", "changed_fields": ["company"], "before_attributes": {"company": "Acme"}, "after_attributes": {"company": "NewCo"}},
        ]
        for item in cases:
            with self.subTest(item=item):
                result = self.classify(item)
                self.assertEqual(result["disposition"], "pending_review")
                self.assertEqual(result["reason_code"], "career_signal_requires_execution_agent")
                self.assertIn("不能据此推断当前任职", result["why_material"])

    def test_cosmetic_and_ordinary_job_description_edits_remain_suppressed(self):
        cases = [
            ({"category": "position", "before": "Research Intern; built model A", "after": "Research Intern; built model B", "changed_fields": ["description"]}, "description_only_edit"),
            ({"category": "research", "before": "Research on startup financing", "after": "Research on startup valuation", "changed_fields": ["description"]}, "description_only_edit"),
            ({"category": "identity", "before": "I study AI", "after": "I study robust AI", "field": "summary"}, "description_only_edit"),
            ({"category": "position", "before": "Engineer at Acme.", "after": "Engineer at Acme!", "changed_fields": ["title"]}, "formatting_or_punctuation_only"),
            ({"category": "position", "before": "First-year PhD student", "after": "Second-year PhD student", "changed_fields": ["description"]}, "academic_year_rollover"),
            ({"category": "identity", "before": "Researcher (he/him)", "after": "Researcher (he/they)"}, "parenthetical_text_only"),
            ({"category": "content", "before": "Former company details: dataset A", "after": "Former company details: dataset B", "changed_fields": ["description"]}, "description_only_edit"),
            ({"category": "affiliation", "before": "Acme; description A", "after": "Acme; description B", "changed_fields": ["description"]}, "description_only_edit"),
        ]
        for item, reason in cases:
            with self.subTest(item=item):
                result = self.classify(item)
                self.assertEqual((result["disposition"], result["reason_code"]), ("suppress", reason))

    def test_historical_role_correction_is_reviewable_without_inferred_new_job(self):
        result = self.classify({"category": "identity", "before": "Researcher at CurrentCo", "after": "Researcher at CurrentCo (Previously intern at OldCo in 2020)", "field": "summary"})
        self.assertEqual(result["disposition"], "pending_review")
        self.assertEqual(result["change_type"], "identity_changed")
        self.assertIn("不能据此推断当前任职", result["why_material"])
        self.assertNotIn("入职 OldCo", result["what_changed"])
        removal = self.classify({"category": "position", "text": "Former intern at OldCo in 2020"}, "removal")
        self.assertEqual(removal["reason_code"], "page_removal_is_not_a_real_world_event")
        self.assertEqual(removal["disposition"], "suppress")

    def test_ledger_never_auto_publishes_protected_or_historical_role_facts(self):
        base = Path(__file__).resolve().parents[1] / "artifacts/coverage-audit-20261123"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="career-review-test-", dir=base) as directory:
            state = PortableState(Path(directory) / "synthetic.sqlite3")
            try:
                person = state.tracker.add_person("Synthetic Career Audit", urls=["https://example.invalid/career-audit"])
                source_id = person["sources"][0]["source_id"]
                at = "2026-11-22T01:00:00+00:00"
                state.db.execute("INSERT INTO runs(run_id,started_at,completed_at,trigger,status,purpose) VALUES('career-test',?,?, 'synthetic','completed','production')", (at, at))
                delta = {
                    "additions": [{"category": "position", "stable_id": "old-internship", "text": "Research Intern at OldCo, 2020", "attributes": {"title": "Research Intern", "company": "OldCo", "start_date": "2020-06", "end_date": "2020-08"}}],
                    "modifications": [
                        {"category": "position", "before": "Research engineer (Acme)", "after": "Research engineer (NewCo)", "changed_fields": ["company"]},
                        {"category": "research", "before": "I research agents", "after": "I research agents (co-founder of NewCo)", "changed_fields": ["description"]},
                        {"category": "position", "before": "Research Intern; built model A", "after": "Research Intern; built model B", "changed_fields": ["description"]},
                    ],
                    "removals": [{"category": "position", "text": "Former intern at AnotherCo, 2019"}],
                }
                state.db.execute("INSERT INTO observations(observation_id,run_id,source_id,observed_at,health_status,semantic_hash,decision_status,score,delta_json,summary,reviewer) VALUES('career-obs','career-test',?,?,'healthy','synthetic','changed',.9,?,'synthetic','deterministic')", (source_id, at, json.dumps(delta)))
                state.db.commit()
                counts = materialize_window_events(state, window_start="2026-11-21T00:00:00Z", window_end="2026-11-23T00:00:00Z")
                self.assertEqual(counts["publish"], 0)
                self.assertEqual(counts["pending_review"], 3)
                self.assertEqual(counts["suppress"], 2)
                self.assertEqual(len(state.pending_agent_reviews()), 3)
                published = state.db.execute("SELECT COUNT(*) FROM event_ledger WHERE audience='public' AND disposition='publish'").fetchone()[0]
                self.assertEqual(published, 0)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
