from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import people_intel.light_tracker as light_tracker_module
from people_intel.light_tracker import (
    FeatureSnapshot,
    FetchObservation,
    LightTracker,
    assess_snapshot,
    compare_snapshots,
    extract_snapshot,
    _scheduled_next_check_at,
)


BASELINE_HTML = """
<html><head><title>Alice Zhang</title></head><body><main>
<h1>Alice Zhang</h1><h2>Publications</h2>
<p>Paper A, ICML 2025</p><p>Paper B, NeurIPS 2025</p>
<h2>Research</h2><p>Reliable machine learning and evaluation.</p>
</main></body></html>
"""


def _tracker_and_source(tmp_path):
    tracker = LightTracker(tmp_path / "parser-quality.sqlite3")
    person = tracker.add_person(
        "Alice Zhang",
        urls=["https://alice.example/"],
        profile={"registry_person_id": "person-alice"},
    )
    return tracker, person["sources"][0]


def _observe(tracker, source, body, **kwargs):
    run_id = tracker.start_run("parser-quality-v09")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=body, final_url=source["url"], **kwargs),
    )
    tracker.complete_run(run_id)
    return run_id, decision


@pytest.mark.parametrize(
    ("encoding", "content_type", "person_text"),
    [
        ("windows-1252", "text/html", "Café researcher"),
        ("gb18030", "text/html; charset=gb18030", "张三的机器学习研究"),
    ],
)
def test_extract_snapshot_honours_declared_charset(
    encoding,
    content_type,
    person_text,
):
    meta = '<meta charset="windows-1252">' if encoding == "windows-1252" else ""
    html = (
        f"<html><head>{meta}<title>{person_text}</title></head>"
        f"<body><main><h1>{person_text}</h1>"
        f"<p>{person_text} works on reliable evaluation systems.</p>"
        "</main></body></html>"
    )

    snapshot = extract_snapshot(
        "homepage",
        html.encode(encoding),
        content_type=content_type,
    )

    assert snapshot.extractor_version != "parser-anomaly-v1"
    assert "parser_anomaly_codes" not in snapshot.metrics
    assert person_text in " ".join([*snapshot.identity.values(), *(i.text for i in snapshot.items)])


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body><p>invalid byte: \xff</p></body></html>",
        "<html><body><p>replacement: \ufffd</p></body></html>",
        {"profile": {"name": "Alice \ufffd Zhang"}, "sections": []},
    ],
)
def test_decode_corruption_becomes_an_unusable_parser_anomaly(body):
    kind = "linkedin" if isinstance(body, dict) else "homepage"
    snapshot = extract_snapshot(kind, body)
    score, reasons, usable = assess_snapshot(
        snapshot,
        expected_names=["Alice Zhang"],
        expected_url="https://alice.example/",
        curated_binding=True,
    )

    assert snapshot.extractor_version == "parser-anomaly-v1"
    assert snapshot.metrics["parser_anomaly_codes"] in {
        "decode_failed",
        "replacement_character",
    }
    assert score == 0.0
    assert reasons
    assert usable is False


def test_replacement_character_created_by_html_entity_is_also_rejected():
    snapshot = extract_snapshot(
        "homepage",
        "<title>Alice</title><h1>Alice</h1>"
        "<p>Reliable evaluation &#xfffd; publication.</p>",
    )
    _, reasons, usable = assess_snapshot(
        snapshot,
        expected_names=["Alice"],
        expected_url="https://alice.example/",
        curated_binding=True,
    )

    assert "replacement_character" in snapshot.metrics["parser_anomaly_codes"]
    assert reasons
    assert usable is False


def _scholar_html(*, year: str, href: str) -> str:
    return f"""
    <div id="gsc_prf_in">Alice Zhang</div>
    <div id="gsc_prf_i">Example University</div>
    <tr class="gsc_a_tr">
      <td><a class="gsc_a_at" href="{href}">Reliable Agents</a>
      <div class="gs_gray">Alice Zhang</div>
      <div class="gs_gray">ICML</div></td>
      <td class="gsc_a_c"><a class="gsc_a_ac">3</a></td>
      <td class="gsc_a_y">{year}</td>
    </tr>
    """


def test_scholar_rejects_yymm_captured_from_modern_arxiv_id():
    snapshot = extract_snapshot(
        "scholar",
        _scholar_html(year="2010", href="https://arxiv.org/abs/2010.11929"),
    )
    score, reasons, usable = assess_snapshot(
        snapshot,
        expected_names=["Alice Zhang"],
        expected_url="https://scholar.google.com/citations?user=alice",
        curated_binding=True,
    )

    assert "scholar_year_is_arxiv_yymm" in snapshot.metrics["parser_anomaly_codes"]
    assert any("expected calendar year 2020" in reason for reason in reasons)
    assert score == 0.0
    assert usable is False


def test_scholar_delta_keeps_every_new_publication_beyond_twelve() -> None:
    def page(count: int) -> str:
        rows = "".join(
            f"""
            <tr class="gsc_a_tr">
              <td><a class="gsc_a_at" href="/citations?citation_for_view=alice:paper-{index}">
                Reliable Paper {index}
              </a><div class="gs_gray">Alice Zhang</div>
              <div class="gs_gray">Venue {index}</div></td>
              <td class="gsc_a_y">2026</td>
            </tr>
            """
            for index in range(count)
        )
        return (
            '<div id="gsc_prf_in">Alice Zhang</div>'
            '<div id="gsc_prf_i">Example University</div>'
            + rows
        )

    before = extract_snapshot("scholar", page(1))
    after = extract_snapshot("scholar", page(14))
    decision = compare_snapshots(before, after)

    assert decision.status == "changed"
    assert len(decision.additions) == 13
    assert {item["stable_id"] for item in decision.additions} == {
        f"scholar-citation:alice:paper-{index}" for index in range(1, 14)
    }


@pytest.mark.parametrize("year", ["1899", "20O5"])
def test_scholar_rejects_implausible_or_malformed_publication_year(year):
    snapshot = extract_snapshot(
        "scholar",
        _scholar_html(year=year, href="/citations?citation_for_view=alice:paper"),
    )
    codes = snapshot.metrics["parser_anomaly_codes"]

    assert codes in {"scholar_year_out_of_range", "scholar_year_invalid"}


def test_scholar_allows_publication_year_to_differ_from_arxiv_upload_year():
    snapshot = extract_snapshot(
        "scholar",
        _scholar_html(year="2021", href="https://arxiv.org/abs/2010.11929"),
    )

    assert "parser_anomaly_codes" not in snapshot.metrics


def test_stable_id_only_change_has_explicit_old_and_new_values():
    before = extract_snapshot(
        "homepage",
        "<title>Alice</title><h1>Alice</h1><h2>Publications</h2>"
        "<p><a href='https://example.edu/paper-v1'>"
        "A Stable Machine Learning Publication</a></p>",
    )
    after = extract_snapshot(
        "homepage",
        "<title>Alice</title><h1>Alice</h1><h2>Publications</h2>"
        "<p><a href='https://example.edu/paper-v2'>"
        "A Stable Machine Learning Publication</a></p>",
    )

    decision = compare_snapshots(before, after)

    assert decision.status == "changed"
    assert decision.modifications == [
        {
            "category": "publication",
            "stable_id": "https://example.edu/paper-v2",
            "before_stable_id": "https://example.edu/paper-v1",
            "after_stable_id": "https://example.edu/paper-v2",
            "before": "A Stable Machine Learning Publication",
            "after": "A Stable Machine Learning Publication",
            "before_attributes": {},
            "after_attributes": {},
            "changed_fields": ["stable_id"],
        }
    ]


def test_unexplainable_same_text_manifest_delta_cannot_be_changed():
    before = extract_snapshot(
        "homepage",
        "<title>Alice</title><h1>Alice</h1>"
        "<p>Reliable machine learning publication and evaluation.</p>",
    )
    after = FeatureSnapshot.from_json(before.to_json())
    target = next(item for item in after.items if item.category != "identity")
    assert target.stable_id is None
    target.key = "legacy-corrupt-key"
    after.semantic_hash = "legacy-corrupt-semantic-hash"

    decision = compare_snapshots(before, after)

    assert decision.status == "noise"
    assert decision.additions == []
    assert decision.removals == []
    assert decision.modifications == []


def test_parser_anomaly_is_quarantined_without_advancing_baseline(tmp_path):
    tracker, source = _tracker_and_source(tmp_path)
    _observe(tracker, source, BASELINE_HTML)
    baseline = tracker.db.execute(
        "SELECT snapshot_json,semantic_hash FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()

    run_id, decision = _observe(
        tracker,
        source,
        b"<html><body><p>invalid byte: \xff</p></body></html>",
    )
    state = tracker.db.execute(
        """SELECT snapshot_json,semantic_hash,candidate_snapshot_json,
                  candidate_count,candidate_decision_json,health_status,
                  last_full_fetch_at
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    assert decision.status == "parser_anomaly"
    assert state["snapshot_json"] == baseline["snapshot_json"]
    assert state["semantic_hash"] == baseline["semantic_hash"]
    assert state["candidate_count"] == 0
    assert json.loads(state["candidate_snapshot_json"])["extractor_version"] == "parser-anomaly-v1"
    assert json.loads(state["candidate_decision_json"])["status"] == "parser_anomaly"
    assert state["health_status"] == "degraded"
    assert state["last_full_fetch_at"] is None
    tracker.db.execute(
        """UPDATE sources SET candidate_count=3,
                  candidate_first_seen_at='2020-01-01T00:00:00+00:00'
           WHERE source_id=?""",
        (source["source_id"],),
    )
    tracker.db.commit()
    assert "parser_anomaly" not in tracker.render_report(run_id)
    assert "长期待审候选" not in tracker.render_report(run_id)
    tracker.close()


def test_304_cannot_confirm_a_quarantined_parser_anomaly(tmp_path):
    tracker, source = _tracker_and_source(tmp_path)
    _observe(tracker, source, BASELINE_HTML, etag='"v1"')
    _observe(
        tracker,
        source,
        b"<html><body><p>invalid byte: \xff</p></body></html>",
        etag='"bad"',
    )

    _, decision = _observe(tracker, source, None, status_code=304, etag='"bad"')
    state = tracker.db.execute(
        """SELECT candidate_count,candidate_decision_json,snapshot_json
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    assert decision.status == "parser_anomaly"
    assert decision.confirmation_count == 0
    assert state["candidate_count"] == 0
    assert json.loads(state["candidate_decision_json"])["status"] == "parser_anomaly"
    assert json.loads(state["snapshot_json"])["extractor_version"] != "parser-anomaly-v1"
    tracker.close()


def test_parser_exception_is_quarantined_without_advancing_baseline(
    tmp_path,
    monkeypatch,
):
    tracker, source = _tracker_and_source(tmp_path)
    _observe(tracker, source, BASELINE_HTML)
    baseline = tracker.db.execute(
        "SELECT snapshot_json,semantic_hash FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()

    def explode(*_args, **_kwargs):
        raise ValueError("synthetic malformed parser state")

    monkeypatch.setattr(light_tracker_module, "extract_snapshot", explode)
    _, decision = _observe(tracker, source, BASELINE_HTML)
    state = tracker.db.execute(
        """SELECT snapshot_json,semantic_hash,candidate_decision_json
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    assert decision.status == "parser_anomaly"
    assert "parser_exception" in decision.summary
    assert state["snapshot_json"] == baseline["snapshot_json"]
    assert state["semantic_hash"] == baseline["semantic_hash"]
    assert json.loads(state["candidate_decision_json"])["status"] == "parser_anomaly"
    tracker.close()


def test_scholar_healthy_schedule_uses_a_stable_calendar_phase(tmp_path):
    observed_at = "2026-09-01T00:00:00+00:00"
    scheduled = {
        _scheduled_next_check_at(
            observed_at,
            cadence_days=7,
            health_status="healthy",
            consecutive_failures=0,
            source_id=f"scholar-source-{index}",
            kind="scholar",
        )
        for index in range(64)
    }
    observed = datetime.fromisoformat(observed_at)
    delays = [datetime.fromisoformat(value) - observed for value in scheduled]

    assert len(scheduled) > 32
    assert all(timedelta(days=1) <= delay <= timedelta(days=7) for delay in delays)
    assert _scheduled_next_check_at(
        observed_at,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id="scholar-source-stable",
        kind="scholar",
    ) == _scheduled_next_check_at(
        observed_at,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id="scholar-source-stable",
        kind="scholar",
    )
    first_slot = _scheduled_next_check_at(
        observed_at,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id="scholar-source-stable",
        kind="scholar",
    )
    second_slot = _scheduled_next_check_at(
        first_slot,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id="scholar-source-stable",
        kind="scholar",
    )
    third_slot = _scheduled_next_check_at(
        second_slot,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id="scholar-source-stable",
        kind="scholar",
    )
    assert datetime.fromisoformat(third_slot) - datetime.fromisoformat(
        second_slot
    ) == timedelta(days=7)

    tracker = LightTracker(tmp_path / "scholar-schedule.sqlite3")
    source = tracker.add_person(
        "Alice Zhang",
        urls=["https://scholar.google.com/citations?user=alice"],
    )["sources"][0]
    run_id = tracker.start_run("scholar-stagger")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=_scholar_html(
                year="2025",
                href="/citations?citation_for_view=alice:paper",
            ),
            final_url=source["url"],
            observed_at=observed_at,
        ),
    )
    tracker.complete_run(run_id)
    next_check_at = tracker.db.execute(
        "SELECT next_check_at FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()[0]

    assert decision.status == "baseline"
    assert next_check_at == _scheduled_next_check_at(
        observed_at,
        cadence_days=7,
        health_status="healthy",
        consecutive_failures=0,
        source_id=source["source_id"],
        kind="scholar",
    )
    assert next_check_at != "2026-09-08T00:00:00+00:00"
    tracker.close()


def test_observe_rolls_back_source_transition_when_audit_insert_fails(tmp_path):
    tracker, source = _tracker_and_source(tmp_path)
    _observe(tracker, source, BASELINE_HTML)
    run_id = tracker.start_run("atomic-observe")
    tracker.db.execute(
        """CREATE TRIGGER reject_observation_insert
           BEFORE INSERT ON observations
           BEGIN SELECT RAISE(ABORT, 'injected observation failure'); END"""
    )
    tracker.db.commit()
    before = tracker.db.execute(
        "SELECT * FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    changed = BASELINE_HTML.replace(
        "</main>",
        "<p>Paper C, ICLR 2026</p></main>",
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected observation failure"):
        tracker.observe(
            run_id,
            source["source_id"],
            FetchObservation(body=changed, final_url=source["url"]),
        )

    after = tracker.db.execute(
        "SELECT * FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    audit_count = tracker.db.execute(
        "SELECT COUNT(*) FROM observations WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    assert tuple(after) == tuple(before)
    assert audit_count == 0
    tracker.close()


def test_observe_respects_an_existing_outer_transaction(tmp_path):
    tracker, source = _tracker_and_source(tmp_path)
    run_id = tracker.start_run("nested-observe")
    person_key = tracker.db.execute(
        "SELECT person_key FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()[0]
    original_name = tracker.db.execute(
        "SELECT canonical_name FROM people WHERE person_key=?", (person_key,)
    ).fetchone()[0]

    tracker.db.execute("BEGIN")
    tracker.db.execute(
        "UPDATE people SET canonical_name='Staged Name' WHERE person_key=?",
        (person_key,),
    )
    tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=BASELINE_HTML, final_url=source["url"]),
    )
    assert tracker.db.in_transaction is True
    tracker.db.rollback()

    restored_name = tracker.db.execute(
        "SELECT canonical_name FROM people WHERE person_key=?", (person_key,)
    ).fetchone()[0]
    observation_count = tracker.db.execute(
        "SELECT COUNT(*) FROM observations WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    assert restored_name == original_name
    assert observation_count == 0
    tracker.close()


def test_observe_keeps_reviewer_keywords_but_never_invokes_runtime_model(tmp_path):
    class ReviewerProbe:
        def __init__(self):
            self.calls = 0

        def review(self, *_args, **_kwargs):
            self.calls += 1

        def summarize_confirmed(self, *_args, **_kwargs):
            self.calls += 1

    tracker, source = _tracker_and_source(tmp_path)
    _observe(tracker, source, BASELINE_HTML)
    changed = BASELINE_HTML.replace(
        "</main>",
        "<p>Paper C, ICLR 2026</p></main>",
    )
    reviewer = ReviewerProbe()
    usage: dict[str, int] = {}
    for index in range(2):
        run_id = tracker.start_run(f"no-runtime-review-{index}")
        tracker.observe(
            run_id,
            source["source_id"],
            FetchObservation(body=changed, final_url=source["url"]),
            reviewer=reviewer,
            ai_usage_sink=usage,
        )
        tracker.complete_run(run_id)

    assert reviewer.calls == 0
    assert usage == {}
    tracker.close()
