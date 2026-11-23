from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import people_intel.light_tracker as light
import people_tracking_feishu.cli as cli
import people_tracking_feishu.reporting as reporting
import people_tracking_feishu.state as state_module
import people_tracking_feishu.tracking as tracking
from people_tracking_feishu.state import PortableState
from test_feishu_portable import _enabled_scan_config


BASE = (
    '<h1>Alice Zhang</h1><p>I am a researcher in reliable machine learning.</p>'
    '<h2>Publications</h2><p>Reliable Systems Paper A, ICML 2025</p>'
)
NEW = BASE + '<p>Reliable Systems Paper B, NeurIPS 2026</p>'


def test_source_due_after_empty_weekly_scan_runs_on_next_tick(tmp_path, monkeypatch):
    paths, config = _enabled_scan_config(tmp_path)
    config['runtime']['mode'] = 'openclaw'
    config['sources'] = []
    config['schedule']['scan'] = 'weekly'
    config['master_database'] = {'mode': 'none'}
    state = PortableState(paths.database)
    state.tracker.add_person('Alice Zhang', urls=['https://alice.example/'])
    state.close()
    start = datetime(2026, 11, 9, 8, tzinfo=timezone.utc)
    clock = [start]
    body = [BASE]
    fetches = []

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    def fetch(source):
        clock[0] += timedelta(seconds=30)
        fetches.append(source['source_id'])
        return light.FetchObservation(body=body[0], observed_at=clock[0].isoformat())

    for module in (cli, tracking):
        monkeypatch.setattr(module, 'datetime', Frozen)
    for module in (light, reporting, state_module):
        monkeypatch.setattr(module, 'utc_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(cli, 'load_config', lambda _: config)
    monkeypatch.setattr(cli, 'command_sync', lambda *a, **kw: {'apply': True, 'imports': [], 'scan_source_ids': []})
    monkeypatch.setattr(cli, '_sync_or_queue_visible_master', lambda *a, **kw: None)
    monkeypatch.setattr(cli, '_queue_scheduled_authoritative_roster_refresh', lambda *a, **kw: None)
    monkeypatch.setattr(cli, '_digest_due_now', lambda *a, **kw: False)
    monkeypatch.setattr(tracking, '_fetch', fetch)

    initial = cli.command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert initial['scan_due'] is True and len(fetches) == 1
    clock[0] = start + timedelta(days=7)
    body[0] = NEW
    early = cli.command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert early['scan_due'] is True and len(fetches) == 1
    clock[0] = start + timedelta(days=7, minutes=15)
    due = cli.command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert due['scan_due'] is True and len(fetches) == 2
    assert due['validation_failed'] is False
    state = PortableState(paths.database)
    try:
        row = state.db.execute('SELECT candidate_count,last_checked_at FROM sources').fetchone()
        assert row['candidate_count'] == 1
        assert row['last_checked_at'] == clock[0].isoformat()
    finally:
        state.close()
    clock[0] = start + timedelta(days=7, minutes=30)
    idle = cli.command_schedule_tick(argparse.Namespace(lark_cli=None), paths)
    assert idle['scan_due'] is False and len(fetches) == 2


def test_due_wakeup_respects_disabled_source_and_regular_scan_cadence(tmp_path):
    paths, _ = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    try:
        source = state.tracker.add_person('Disabled Person', urls=['https://disabled.example/'])['sources'][0]
        now = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)
        state.set_meta('scheduler_last_scan_at', now.isoformat())
        state.db.execute('UPDATE sources SET tracking_enabled=0 WHERE source_id=?', (source['source_id'],))
        state.db.commit()
        assert cli._scheduled_scan_due(state, {'scan': 'weekly'}, now=now)[0] is False
        assert cli._scheduled_scan_due(state, {'scan': 'weekly'}, now=now + timedelta(days=7))[0] is True
        state.db.execute('UPDATE sources SET tracking_enabled=1,last_checked_at=?,next_check_at=? WHERE source_id=?', ((now - timedelta(days=7)).isoformat(), (now + timedelta(hours=1)).isoformat(), source['source_id']))
        state.db.commit()
        assert cli._scheduled_scan_due(state, {'scan': 'weekly'}, now=now)[0] is False
        assert tracking.has_due_source_work(state, {}, now=now + timedelta(hours=1)) is True
    finally:
        state.close()


def test_scholar_wakeup_still_obeys_persistent_host_budget(tmp_path, monkeypatch):
    paths, config = _enabled_scan_config(tmp_path)
    config['sources'] = []
    state = PortableState(paths.database)
    now = datetime.now(timezone.utc)
    try:
        state.tracker.add_person('Budget Person', urls=['https://scholar.google.com/citations?user=synthetic'])
        state.set_meta('scheduler_last_scan_at', now.isoformat())
        # Never-checked Scholar sources can be waiting behind the batch cap.
        assert tracking.has_due_source_work(state, config, now=now) is True
        state.update_host_budget(tracking._SCHOLAR_HOST_KEY, circuit_state='open',
                                 blocked_until=(now + timedelta(hours=1)).isoformat(),
                                 canary_required=True, reason='synthetic429')
        assert tracking.has_due_source_work(state, config, now=now) is False
        monkeypatch.setattr(tracking, '_fetch', lambda _: (_ for _ in ()).throw(AssertionError('circuit must block fetch')))
        result = tracking.scan_due(state, paths, config)
        assert result['validation']['coverage']['sources_attempted'] == 0
        assert state.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0
    finally:
        state.close()


def test_due_wakeup_excludes_manual_attention_and_future_recovery(tmp_path):
    paths, config = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    now = datetime.now(timezone.utc)
    try:
        sid = state.tracker.add_person('Attention Person', urls=['https://attention.example/'])['sources'][0]['source_id']
        state.db.execute('UPDATE sources SET last_checked_at=?', ((now - timedelta(days=7)).isoformat(),))
        state.db.commit()
        assert tracking.has_due_source_work(state, config, now=now) is True
        state.set_meta('network_recovery_queue', {sid: {'status': 'needs_attention'}})
        assert tracking.has_due_source_work(state, config, now=now) is False
        state.set_meta('network_recovery_queue', {sid: {'status': 'pending', 'next_retry_at': (now + timedelta(hours=1)).isoformat()}})
        assert tracking.has_due_source_work(state, config, now=now) is False
        assert tracking.has_due_source_work(state, config, now=now + timedelta(hours=1)) is True
    finally:
        state.close()


def test_scholar_due_preview_has_no_circuit_or_metadata_writes(tmp_path, monkeypatch):
    paths, config = _enabled_scan_config(tmp_path)
    state = PortableState(paths.database)
    now = datetime.now(timezone.utc)
    try:
        state.tracker.add_person('Canary Person', urls=['https://scholar.google.com/citations?user=canary'])
        state.db.execute('UPDATE sources SET last_checked_at=?', ((now - timedelta(days=7)).isoformat(),))
        state.db.commit()
        state.update_host_budget(tracking._SCHOLAR_HOST_KEY, circuit_state='open',
                                 blocked_until=(now - timedelta(seconds=1)).isoformat(),
                                 canary_required=True, reason='synthetic429')
        before = state.get_host_budget(tracking._SCHOLAR_HOST_KEY)
        changes = state.db.total_changes
        assert tracking.has_due_source_work(state, config, now=now) is True
        assert state.get_host_budget(tracking._SCHOLAR_HOST_KEY) == before
        assert state.db.total_changes == changes
        selected, _ = tracking._apply_scholar_run_budget(state, state.tracker.list_due_sources(), config, now=now)
        assert len(selected) == 1
        assert state.get_host_budget(tracking._SCHOLAR_HOST_KEY)['circuit_state'] == 'half_open'
        state.update_host_budget(tracking._SCHOLAR_HOST_KEY, circuit_state='closed',
                                 canary_required=False,
                                 metadata={'request_timestamps': [(now - timedelta(seconds=1)).isoformat()] * 64})
        assert tracking.has_due_source_work(state, config, now=now) is False
    finally:
        state.close()
