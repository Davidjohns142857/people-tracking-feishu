from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from people_intel.light_tracker import FetchObservation, extract_snapshot
from people_tracking_feishu.config import RuntimePaths
from people_tracking_feishu.state import PortableState
import people_tracking_feishu.tracking as tracking_module


def _paths(tmp_path) -> RuntimePaths:
    paths = RuntimePaths(
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
        config_file=tmp_path / "config/config.json",
        database=tmp_path / "state/people.sqlite3",
        reports=tmp_path / "state/reports",
        install_state=tmp_path / "config/install-state.json",
    )
    paths.ensure()
    return paths


def _config(**scholar_overrides):
    return {
        "state": "enabled",
        "apis": {},
        "scan_policy": {
            "scholar": {
                "max_requests_per_run": 8,
                "max_requests_per_day": 64,
                "max_requests_per_week": 448,
                "recovery_canary_requests": 1,
                "default_retry_after_hours": 24,
                "maximum_retry_after_hours": 168,
                **scholar_overrides,
            }
        },
    }


def _scholar_html(name: str) -> str:
    return f"""
    <div id="gsc_prf_in">{name}</div>
    <div id="gsc_prf_i">Example University</div>
    <tr class="gsc_a_tr"
        data-href="/citations?view_op=view_citation&amp;citation_for_view=user:paper">
      <td><a class="gsc_a_at">Reliable Agents</a>
      <div class="gs_gray">{name}</div>
      <div class="gs_gray">ICML 2025</div></td>
      <td class="gsc_a_c"><a class="gsc_a_ac">3</a></td>
      <td class="gsc_a_y">2025</td>
    </tr>
    """


def _homepage_html(name: str) -> str:
    return (
        f"<html><head><title>{name}</title></head><body><main>"
        f"<h1>{name}</h1><h2>Research</h2>"
        "<p>Reliable machine learning systems and evaluation.</p>"
        "</main></body></html>"
    )


def _add_mixed_sources(state: PortableState) -> dict[str, dict]:
    rows = {}
    for name, url in (
        ("A Scholar Limited", "https://scholar.google.com/citations?user=limited"),
        ("B Scholar Deferred", "https://scholar.google.com/citations?user=deferred"),
        ("C Homepage Continues", "https://profiles.example/continues"),
    ):
        person = state.tracker.add_person(name, urls=[url])
        rows[name] = person["sources"][0]
    return rows


def test_429_stops_remaining_scholar_but_other_sources_continue_and_persists(
    tmp_path,
    monkeypatch,
):
    paths = _paths(tmp_path)
    state = PortableState(paths.database)
    rows = _add_mixed_sources(state)
    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    calls: list[str] = []

    def fake_fetch(source):
        calls.append(source["source_id"])
        if source["source_id"] == rows["A Scholar Limited"]["source_id"]:
            return FetchObservation(
                body=None,
                status_code=429,
                final_url=source["url"],
                error="HTTP 429; retry_after=7200",
                observed_at=observed_at.isoformat(),
            )
        if source["source_id"] == rows["B Scholar Deferred"]["source_id"]:
            raise AssertionError("a Scholar request after the first 429 is forbidden")
        return FetchObservation(
            body=_homepage_html("C Homepage Continues"),
            status_code=200,
            final_url=source["url"],
            observed_at=observed_at.isoformat(),
        )

    monkeypatch.setattr(tracking_module, "_fetch", fake_fetch)
    result = tracking_module.scan_due(
        state,
        paths,
        _config(),
        force_all=True,
        max_error_rate=1.0,
        homepage_retries=0,
    )

    assert calls == [
        rows["A Scholar Limited"]["source_id"],
        rows["C Homepage Continues"]["source_id"],
    ]
    budget = result["metrics"]["scholar_budget"]
    assert budget["attempted"] == 1
    assert budget["stopped_on_429"] is True
    assert budget["deferred_after_limit"] == 1
    assert budget["circuit_after"]["circuit_state"] == "open"
    assert budget["circuit_after"]["retry_after_seconds"] == 7200
    assert budget["circuit_after"]["blocked_until"] == (
        observed_at + timedelta(hours=2)
    ).isoformat()
    assert {item["kind"] for item in result["outcomes"]} == {
        "scholar",
        "homepage",
    }
    state.close()

    # Reopening the same SQLite file models the next scheduler process.
    state = PortableState(paths.database)
    try:
        persisted = state.get_host_budget(tracking_module._SCHOLAR_HOST_KEY)
        assert persisted["circuit_state"] == "open"
        assert persisted["blocked_until"] == (
            observed_at + timedelta(hours=2)
        ).isoformat()

        monkeypatch.setattr(
            tracking_module,
            "_fetch",
            lambda _source: (_ for _ in ()).throw(
                AssertionError("open Scholar circuit must make no request")
            ),
        )
        blocked = tracking_module.scan_due(
            state,
            paths,
            _config(),
            force_all=True,
            source_kinds=["scholar"],
            max_error_rate=1.0,
        )
        assert blocked["metrics"]["scholar_budget"]["mode"] == "blocked"
        assert blocked["metrics"]["scholar_budget"]["planned"] == 0
        assert blocked["metrics"]["scholar_budget"]["attempted"] == 0
    finally:
        state.close()


def test_retry_after_supports_delta_http_date_fallback_and_maximum():
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    assert tracking_module._retry_after_seconds(
        SimpleNamespace(error="HTTP 429; retry_after=3600"),
        now=now,
        fallback_seconds=86_400,
        maximum_seconds=604_800,
    ) == 3600
    assert tracking_module._retry_after_seconds(
        SimpleNamespace(error="HTTP 429; retry_after=Fri, 04 Sep 2026 14:00:00 GMT"),
        now=now,
        fallback_seconds=86_400,
        maximum_seconds=604_800,
    ) == 7200
    assert tracking_module._retry_after_seconds(
        SimpleNamespace(error="HTTP 429; retry_after=9999999"),
        now=now,
        fallback_seconds=86_400,
        maximum_seconds=604_800,
    ) == 604_800
    assert tracking_module._retry_after_seconds(
        SimpleNamespace(error="HTTP 429; retry_after=invalid"),
        now=now,
        fallback_seconds=86_400,
        maximum_seconds=604_800,
    ) == 86_400


def test_expired_persistent_circuit_allows_only_one_canary_then_closes(
    tmp_path,
    monkeypatch,
):
    paths = _paths(tmp_path)
    state = PortableState(paths.database)
    names_by_source: dict[str, str] = {}
    for index in range(3):
        name = f"Scholar Canary {index}"
        source = state.tracker.add_person(
            name,
            urls=[f"https://scholar.google.com/citations?user=canary{index}"],
        )["sources"][0]
        names_by_source[source["source_id"]] = name
    state.update_host_budget(
        tracking_module._SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        reason="http_429",
        status_code=429,
        retry_after_seconds=60,
        consecutive_limits=1,
        canary_required=True,
    )
    state.close()

    # The recovery decision must survive a process boundary.
    state = PortableState(paths.database)
    calls: list[str] = []

    def successful_canary(source):
        calls.append(source["source_id"])
        return FetchObservation(
            body=_scholar_html(names_by_source[source["source_id"]]),
            status_code=200,
            final_url=source["url"],
        )

    monkeypatch.setattr(tracking_module, "_fetch", successful_canary)
    try:
        result = tracking_module.scan_due(
            state,
            paths,
            _config(recovery_canary_requests=1),
            force_all=True,
            source_kinds=["scholar"],
            max_error_rate=1.0,
        )
        budget = result["metrics"]["scholar_budget"]
        assert budget["mode"] == "half_open"
        assert budget["planned"] == budget["attempted"] == 1
        assert budget["deferred_before_fetch"] == 2
        assert len(calls) == 1
        assert budget["circuit_after"]["circuit_state"] == "closed"
        assert budget["circuit_after"]["canary_required"] is False
        assert budget["circuit_after"]["consecutive_limits"] == 0
    finally:
        state.close()


def test_half_open_limited_baseline_heartbeat_does_not_close_circuit(
    tmp_path,
    monkeypatch,
):
    paths = _paths(tmp_path)
    state = PortableState(paths.database)
    name = "Scholar Limited Canary"
    source = state.tracker.add_person(
        name,
        urls=["https://scholar.google.com/citations?user=limited-canary"],
    )["sources"][0]
    limited_body = (
        f'<div id="gsc_prf_in">{name}</div>'
        '<div id="gsc_prf_i">Example University</div>'
    )
    limited_snapshot = extract_snapshot("scholar", limited_body)
    limited_snapshot.canonical_url = source["url"]
    state.tracker.db.execute(
        "UPDATE sources SET snapshot_json=?,semantic_hash=? WHERE source_id=?",
        (
            limited_snapshot.to_json(),
            limited_snapshot.semantic_hash,
            source["source_id"],
        ),
    )
    state.tracker.db.commit()
    state.update_host_budget(
        tracking_module._SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        canary_required=True,
    )

    monkeypatch.setattr(
        tracking_module,
        "_fetch",
        lambda current: FetchObservation(
            body=limited_body,
            status_code=200,
            final_url=current["url"],
        ),
    )

    try:
        result = tracking_module.scan_due(
            state,
            paths,
            _config(recovery_canary_requests=1),
            force_all=True,
            source_kinds=["scholar"],
            max_error_rate=1.0,
        )
        budget = result["metrics"]["scholar_budget"]
        quality_row = state.tracker.db.execute(
            "SELECT quality_json FROM sources WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()
        quality = json.loads(quality_row["quality_json"])

        assert result["outcomes"][0]["decision"] == "unchanged"
        assert result["outcomes"][0]["health_status"] == "healthy"
        assert quality["usable"] is False
        assert quality["limited_baseline_heartbeat"] is True
        assert budget["mode"] == "half_open"
        assert budget["attempted"] == 1
        assert budget["access_successes"] == 0
        assert budget["circuit_after"]["circuit_state"] == "open"
        assert budget["circuit_after"]["canary_required"] is True
        assert budget["circuit_after"]["reason"] == "unhealthy_canary:unchanged"
    finally:
        state.close()


def test_half_open_transport_failure_consumes_budget_and_does_not_close(
    tmp_path,
    monkeypatch,
):
    paths = _paths(tmp_path)
    state = PortableState(paths.database)
    state.tracker.add_person(
        "Scholar Failed Canary",
        urls=["https://scholar.google.com/citations?user=failed-canary"],
    )
    state.update_host_budget(
        tracking_module._SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        canary_required=True,
    )
    monkeypatch.setattr(
        tracking_module,
        "_fetch",
        lambda _source: (_ for _ in ()).throw(ConnectionError("synthetic failure")),
    )

    try:
        result = tracking_module.scan_due(
            state,
            paths,
            _config(),
            force_all=True,
            source_kinds=["scholar"],
            max_error_rate=1.0,
        )
        budget = result["metrics"]["scholar_budget"]
        persisted = state.get_host_budget(tracking_module._SCHOLAR_HOST_KEY)
        assert budget["mode"] == "half_open"
        assert budget["attempted"] == 1
        assert budget["access_successes"] == 0
        assert budget["circuit_after"]["circuit_state"] == "half_open"
        assert persisted["circuit_state"] == "half_open"
        assert len(persisted["metadata"]["request_timestamps"]) == 1
    finally:
        state.close()


@pytest.mark.parametrize(
    ("history_offsets", "expected_planned", "expected_day", "expected_week"),
    [
        ([], 2, 0, 0),
        ([timedelta(hours=1), timedelta(hours=2), timedelta(hours=3)], 0, 3, 3),
        (
            [
                timedelta(days=2),
                timedelta(days=3),
                timedelta(days=4),
                timedelta(days=5),
                timedelta(days=6),
            ],
            0,
            0,
            5,
        ),
    ],
)
def test_run_day_and_week_budgets_are_enforced_from_persistent_history(
    tmp_path,
    history_offsets,
    expected_planned,
    expected_day,
    expected_week,
):
    state = PortableState(tmp_path / "budget.sqlite3")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    state.update_host_budget(
        tracking_module._SCHOLAR_HOST_KEY,
        circuit_state="closed",
        blocked_until=None,
        canary_required=False,
        metadata={
            "request_timestamps": [
                (now - offset).isoformat() for offset in history_offsets
            ]
        },
    )
    sources = [
        {"kind": "scholar", "source_id": f"scholar-{index}"}
        for index in range(10)
    ]

    selected, metrics = tracking_module._apply_scholar_run_budget(
        state,
        sources,
        _config(
            max_requests_per_run=2,
            max_requests_per_day=3,
            max_requests_per_week=5,
        ),
        now=now,
    )

    assert len(selected) == expected_planned
    assert metrics["planned"] == expected_planned
    assert metrics["requests_used_last_24h"] == expected_day
    assert metrics["requests_used_last_7d"] == expected_week
    state.close()


def test_half_open_canary_never_exceeds_run_day_or_week_budget(tmp_path):
    state = PortableState(tmp_path / "half-open-budget.sqlite3")
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    state.update_host_budget(
        tracking_module._SCHOLAR_HOST_KEY,
        circuit_state="open",
        blocked_until=(now - timedelta(seconds=1)).isoformat(),
        canary_required=True,
        metadata={"request_timestamps": []},
    )
    sources = [
        {"kind": "scholar", "source_id": f"scholar-{index}"}
        for index in range(10)
    ]

    selected, metrics = tracking_module._apply_scholar_run_budget(
        state,
        sources,
        _config(
            max_requests_per_run=1,
            max_requests_per_day=10,
            max_requests_per_week=10,
            recovery_canary_requests=5,
        ),
        now=now,
    )

    assert metrics["mode"] == "half_open"
    assert len(selected) == metrics["planned"] == 1
    state.close()
