from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from people_intel.light_tracker import (
    FetchObservation,
    ChangeDecision,
    LightTracker,
    is_actionable_career_item,
)


BASE = (
    '<html><head><title>Alice Zhang</title></head><body><main>'
    '<h1>Alice Zhang</h1><h2>Research</h2><p>Reliable learning and evaluation.</p>'
    '<h2>Publications</h2><p>Reliable Systems Paper A, ICML 2025</p>'
    '</main></body></html>'
)
CAREER = '<h2>News</h2><p>I co-founded Acme Robotics as CTO.</p>'


def append_page(extra: str) -> str:
    return BASE.replace('</main>', extra + '</main>')


class ActionableCareerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'tracker.sqlite3'
        self.tracker = LightTracker(self.database)
        self.person = self.tracker.add_person('Alice Zhang', urls=['https://alice.example/'])
        self.source_id = self.person['sources'][0]['source_id']
        self.sequence = 0

    def tearDown(self):
        self.tracker.close()
        self.directory.cleanup()

    def observe(self, body, *, purpose='production', status_code=200, retrieval_mode='direct'):
        self.sequence += 1
        at = f'2026-11-{self.sequence:02}T08:00:00+00:00'
        run_id = self.tracker.start_run('focused-career-test', purpose=purpose)
        decision = self.tracker.observe(
            run_id, self.source_id,
            FetchObservation(body=body, observed_at=at, status_code=status_code,
                             retrieval_mode=retrieval_mode, etag='controlled'),
        )
        self.tracker.complete_run(run_id)
        row = self.tracker.db.execute(
            'SELECT delta_json FROM observations WHERE run_id=?', (run_id,),
        ).fetchone()
        return decision, json.loads(row['delta_json'])

    def source(self):
        return dict(self.tracker.db.execute(
            'SELECT * FROM sources WHERE source_id=?', (self.source_id,),
        ).fetchone())

    def test_first_career_observation_routes_without_advancing_paper_baseline(self):
        self.observe(BASE)
        baseline = self.source()['snapshot_json']
        page = append_page(CAREER + '<h2>Publications</h2><p>New Paper B, NeurIPS 2026</p>')
        decision, delta = self.observe(page)
        self.assertEqual(decision.status, 'candidate')
        self.assertEqual(decision.confirmations_required, 2)
        self.assertEqual(self.source()['snapshot_json'], baseline)
        self.assertEqual(len(decision.actionable_signals), 1)
        signal = decision.actionable_signals[0]
        self.assertIn('co-founded Acme', signal['item']['text'])
        self.assertEqual(signal['first_observed_at'], '2026-11-02T08:00:00+00:00')
        self.assertEqual(delta['actionable_signals'], [signal])
        self.assertEqual(delta['routed_career_items'], [signal])
        self.assertTrue(any('Paper B' in item['text'] for item in delta['additions']))

    def test_current_employment_identity_routes_before_three_observations(self):
        before = BASE.replace('</head>', '<meta name="description" content="Scientist at Alpha Labs"></head>')
        after = before.replace('Scientist at Alpha Labs', 'Co-founder of Beta Robotics')
        self.observe(before)
        baseline = self.source()['snapshot_json']
        decision, _ = self.observe(after)
        self.assertEqual(decision.status, 'candidate')
        self.assertEqual(decision.confirmations_required, 3)
        self.assertEqual(len(decision.actionable_signals), 1)
        self.assertEqual(self.source()['snapshot_json'], baseline)

    def test_other_page_changes_do_not_reset_or_repeat_career_route(self):
        self.observe(BASE)
        first, _ = self.observe(append_page(CAREER))
        first_signal = first.actionable_signals[0]
        later_page = append_page(CAREER + '<h2>Publications</h2><p>Paper C, ICLR 2027</p>')
        second, delta = self.observe(later_page)
        self.assertEqual(second.status, 'candidate')
        self.assertEqual(second.confirmation_count, 1)
        self.assertEqual(second.actionable_signals, [])
        self.assertEqual(second.routed_career_items[0]['signal_id'], first_signal['signal_id'])
        self.assertEqual(second.routed_career_items[0]['first_observed_at'], first_signal['first_observed_at'])
        third, _ = self.observe(later_page)
        self.assertEqual(third.status, 'changed')
        self.assertEqual(third.actionable_signals, [])
        self.assertEqual(third.routed_career_items[0]['signal_id'], first_signal['signal_id'])
        self.assertEqual(delta['actionable_signals'], [])

    def test_two_distinct_facts_route_without_waiting_for_whole_page(self):
        self.observe(BASE)
        first, _ = self.observe(append_page(CAREER))
        second, _ = self.observe(append_page(CAREER + '<p>I joined Gamma Labs as a research scientist.</p>'))
        self.assertEqual(second.status, 'candidate')
        self.assertEqual(second.confirmation_count, 1)
        self.assertEqual(len(second.routed_career_items), 2)
        self.assertEqual(len(second.actionable_signals), 1)
        self.assertIn('joined Gamma', second.actionable_signals[0]['item']['text'])
        self.assertNotEqual(first.actionable_signals[0]['signal_id'], second.actionable_signals[0]['signal_id'])

    def test_real_reversal_and_reappearance_get_new_occurrence(self):
        self.observe(BASE)
        first, _ = self.observe(append_page(CAREER))
        # The source visibly reverts before whole-page confirmation.
        reverted, _ = self.observe(BASE)
        self.assertEqual(reverted.status, 'unchanged')
        self.assertEqual(reverted.actionable_signals, [])
        second, _ = self.observe(append_page(CAREER))
        self.assertNotEqual(first.actionable_signals[0]['signal_id'], second.actionable_signals[0]['signal_id'])
        route = self.tracker.db.execute('SELECT occurrence FROM career_signal_routes').fetchone()
        self.assertEqual(route['occurrence'], 2)

    def test_routes_survive_process_restart_and_conditional_confirmation(self):
        self.observe(BASE)
        first, _ = self.observe(append_page(CAREER))
        self.tracker.close()
        self.tracker = LightTracker(self.database)
        second, delta = self.observe(None, status_code=304)
        self.assertEqual(second.status, 'changed')
        self.assertEqual(second.actionable_signals, [])
        self.assertEqual(delta['routed_career_items'][0]['signal_id'], first.actionable_signals[0]['signal_id'])

    def test_initial_baseline_and_ordinary_paper_do_not_use_fast_route(self):
        baseline, _ = self.observe(BASE)
        self.assertEqual(baseline.status, 'baseline')
        self.assertEqual(baseline.actionable_signals, [])
        paper = append_page('<h2>Publications</h2><p>Paper B, NeurIPS 2026</p>')
        first, _ = self.observe(paper)
        second, _ = self.observe(paper)
        self.assertEqual([first.status, second.status], ['candidate', 'changed'])
        self.assertEqual(first.actionable_signals + second.actionable_signals, [])
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM career_signal_routes').fetchone()[0], 0)

    def test_initial_career_baseline_is_silent_and_bare_removal_stays_conservative(self):
        first, _ = self.observe(append_page(CAREER))
        removed, _ = self.observe(BASE)
        self.assertEqual(first.status, 'baseline')
        self.assertEqual(first.actionable_signals, [])
        self.assertEqual(removed.status, 'candidate')
        self.assertEqual(removed.confirmations_required, 3)
        self.assertEqual(removed.actionable_signals, [])

    def test_acceptance_validation_health_and_identity_gates_cannot_create_routes(self):
        self.observe(BASE)
        baseline = self.source()['snapshot_json']
        for purpose in ('acceptance', 'validation'):
            decision, _ = self.observe(append_page(CAREER), purpose=purpose)
            self.assertEqual(decision.actionable_signals, [])
        for body, status in ((append_page(CAREER), 503),
                             (append_page(CAREER).replace('Alice Zhang', 'Other Person'), 200),
                             (append_page(CAREER) + '\ufffd', 200),
                             ('<h1>Alice Zhang</h1>', 200)):
            decision, _ = self.observe(body, status_code=status)
            self.assertEqual(decision.actionable_signals, [])
        self.assertEqual(self.source()['snapshot_json'], baseline)
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM career_signal_routes').fetchone()[0], 0)
        production, _ = self.observe(append_page(CAREER))
        self.assertEqual(len(production.actionable_signals), 1)

    def test_retrieval_mode_rebaseline_does_not_invent_fast_route(self):
        self.observe(BASE)
        changed_mode, _ = self.observe(append_page(CAREER), retrieval_mode='browser_public')
        self.assertEqual(changed_mode.status, 'baseline')
        self.assertEqual(changed_mode.actionable_signals, [])

    def test_ambiguous_comparison_neither_routes_nor_closes_existing_episode(self):
        self.observe(BASE)
        ambiguous = ChangeDecision('ambiguous', .4, 'unresolved comparison', additions=[
            {'category':'content','text':'I co-founded Acme Robotics as CTO.'},
        ])
        with patch('people_intel.light_tracker.compare_snapshots', return_value=ambiguous):
            first, _ = self.observe(append_page(CAREER))
        self.assertEqual(first.status, 'candidate')
        self.assertEqual(first.actionable_signals, [])
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM career_signal_routes').fetchone()[0], 0)
        routed, _ = self.observe(append_page(CAREER))
        with patch('people_intel.light_tracker.compare_snapshots', return_value=ChangeDecision('ambiguous', .4, 'unresolved')):
            neutral, _ = self.observe(append_page(CAREER + '<p>Unclassified edit</p>'))
        self.assertEqual(neutral.actionable_signals, [])
        self.assertEqual(self.tracker.db.execute('SELECT active FROM career_signal_routes').fetchone()[0], 1)
        repeat, _ = self.observe(append_page(CAREER))
        self.assertEqual(repeat.actionable_signals, [])
        self.assertEqual(self.tracker.db.execute('SELECT signal_id FROM career_signal_routes').fetchone()[0],
                         routed.actionable_signals[0]['signal_id'])

    def test_route_and_observation_write_are_atomic(self):
        self.observe(BASE)
        self.tracker.db.execute("""CREATE TRIGGER fail_career_observation BEFORE INSERT ON observations
            BEGIN SELECT RAISE(ABORT, 'synthetic observation failure'); END""")
        self.tracker.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.observe(append_page(CAREER))
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM career_signal_routes').fetchone()[0], 0)
        self.assertEqual(self.source()['candidate_count'], 0)

    def test_claim_detection_keeps_description_facts_but_ignores_generic_startup_text(self):
        self.assertTrue(is_actionable_career_item(action='modification', source_kind='homepage',
            item={'category':'identity','field':'primary_heading','before':'Alice Zhang',
                  'after':'Alice Zhang — Co-founder'}))
        self.assertTrue(is_actionable_career_item(action='modification', source_kind='linkedin', item={
            'category':'position', 'before':'Scientist at Alpha', 'after':'Scientist at Alpha',
            'before_attributes':{'description':'Research on robotics.'},
            'after_attributes':{'description':'I co-founded Beta Robotics.'},
        }))
        for text in ('A new paper on startup financing.', 'The founder effect in machine learning.',
                     'Research on entrepreneurship and company formation.'):
            self.assertFalse(is_actionable_career_item(action='addition', source_kind='homepage',
                item={'category':'publication','text':text}))
        self.assertFalse(is_actionable_career_item(action='removal', source_kind='homepage',
            item={'category':'position','text':'Founder of Beta Robotics'}))
        for before, after in (('first-year PhD student', 'second-year PhD student'),
                              ('Scientist', 'Show more'), ('Scientist', 'Loading')):
            self.assertFalse(is_actionable_career_item(action='modification', source_kind='linkedin',
                item={'category':'identity','field':'current_role','before':before,'after':after}))


if __name__ == '__main__':
    unittest.main()
