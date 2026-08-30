from __future__ import annotations

import json
from dataclasses import asdict

from people_intel.light_tracker import (
    ChangeDecision,
    FetchObservation,
    LightTracker,
    extract_snapshot,
)


BASE = """
<html><head><title>Alice Zhang</title></head><body><main>
<h1>Alice Zhang</h1><h2>Publications</h2>
<p>Paper A, ICML 2025</p><p>Paper B, NeurIPS 2025</p>
<h2>Research</h2><p>Reliable machine learning and evaluation.</p>
</main></body></html>
"""

ADDED = BASE.replace(
    "</main>",
    "<p>Paper C, ICLR 2026</p></main>",
)


def make_tracker(tmp_path, *, url: str = "https://alice.example/"):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person("Alice Zhang", urls=[url])
    source = person["sources"][0]
    return tracker, source


def observe(tracker, source, body, *, status_code=200, etag=None, error=None):
    run_id = tracker.start_run("reliability-test")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=body, status_code=status_code, etag=etag, error=error),
    )
    tracker.complete_run(run_id)
    return run_id, decision


def identity_only_linkedin_projection(*, headline: str = "Username: yihchun"):
    return {
        "canonical_url": "https://linkedin.com/in/yihchun",
        "profile": {
            "name": "Yih-Chun Hu",
            "headline": headline,
            "location": "Urbana, Illinois, United States (US)",
            "summary": "Total Contributions: 13",
        },
        "sections": [],
        "sentinels": ["search-index-exact-url", "profile-name"],
    }


def test_identical_limited_linkedin_baseline_is_a_safe_heartbeat(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "Yih-Chun Hu",
        urls=["https://www.linkedin.com/in/yihchun"],
    )
    source = person["sources"][0]
    baseline = extract_snapshot(
        "linkedin",
        identity_only_linkedin_projection(),
    )
    baseline.retrieval_mode = "search_index"
    tracker.db.execute(
        """UPDATE sources SET snapshot_json=?,semantic_hash=?,
           health_status='degraded',consecutive_failures=6
           WHERE source_id=?""",
        (baseline.to_json(), baseline.semantic_hash, source["source_id"]),
    )
    tracker.db.commit()

    run_id = tracker.start_run("limited-heartbeat")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=identity_only_linkedin_projection(),
            final_url=source["url"],
            retrieval_mode="search_index",
        ),
    )
    tracker.complete_run(run_id)
    state = tracker.db.execute(
        """SELECT snapshot_json,semantic_hash,health_status,
                  consecutive_failures,quality_json
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    assert decision.status == "unchanged"
    assert "不放宽人物变化确认门" in decision.summary
    assert state["snapshot_json"] == baseline.to_json()
    assert state["semantic_hash"] == baseline.semantic_hash
    assert state["health_status"] == "healthy"
    assert state["consecutive_failures"] == 0
    assert json.loads(state["quality_json"])["limited_baseline_heartbeat"] is True
    tracker.close()


def test_changed_identity_only_linkedin_projection_remains_incomplete(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "Yih-Chun Hu",
        urls=["https://www.linkedin.com/in/yihchun"],
    )
    source = person["sources"][0]
    baseline = extract_snapshot(
        "linkedin",
        identity_only_linkedin_projection(),
    )
    baseline.retrieval_mode = "search_index"
    tracker.db.execute(
        "UPDATE sources SET snapshot_json=?,semantic_hash=? WHERE source_id=?",
        (baseline.to_json(), baseline.semantic_hash, source["source_id"]),
    )
    tracker.db.commit()

    run_id = tracker.start_run("limited-heartbeat-change")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=identity_only_linkedin_projection(
                headline="Associate Professor at UIUC",
            ),
            final_url=source["url"],
            retrieval_mode="search_index",
        ),
    )
    tracker.complete_run(run_id)
    state = tracker.db.execute(
        "SELECT snapshot_json,semantic_hash,health_status FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()

    assert decision.status == "source_issue_pending"
    assert state["snapshot_json"] == baseline.to_json()
    assert state["semantic_hash"] == baseline.semantic_hash
    assert state["health_status"] == "degraded"
    tracker.close()


def test_transient_change_never_poisoned_confirmed_baseline(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    _, candidate = observe(tracker, source, ADDED)
    assert candidate.status == "candidate"
    _, recovered = observe(tracker, source, BASE)
    assert recovered.status == "unchanged"
    stored = tracker.db.execute(
        "SELECT snapshot_json,candidate_snapshot_json,candidate_count FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    assert extract_snapshot("homepage", BASE).semantic_hash == json.loads(stored["snapshot_json"])["semantic_hash"]
    assert stored["candidate_snapshot_json"] is None
    assert stored["candidate_count"] == 0
    tracker.close()


def test_identical_candidate_is_confirmed_on_second_healthy_observation(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    _, first = observe(tracker, source, ADDED, etag='"v2"')
    run_id, second = observe(tracker, source, ADDED, etag='"v2"')
    assert first.status == "candidate"
    assert second.status == "changed"
    assert second.confirmation_count == second.confirmations_required == 2
    assert "Paper C" in tracker.render_report(run_id)
    tracker.close()


def test_candidate_confirmation_counts_distinct_runs_only(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    run_id = tracker.start_run("production-rescan")
    first = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=ADDED, final_url=source["url"]),
    )
    repeated = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=ADDED, final_url=source["url"]),
    )
    tracker.complete_run(run_id)

    state = tracker.db.execute(
        "SELECT candidate_count,candidate_last_run_id FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    assert first.status == repeated.status == "candidate"
    assert first.confirmation_count == repeated.confirmation_count == 1
    assert state["candidate_count"] == 1
    assert state["candidate_last_run_id"] == run_id

    _, confirmed = observe(tracker, source, ADDED)
    assert confirmed.status == "changed"
    tracker.close()


def test_acceptance_run_never_advances_baseline_or_candidate(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    _, first = observe(tracker, source, ADDED)
    before = tracker.db.execute(
        """SELECT snapshot_json,semantic_hash,candidate_snapshot_json,
                  candidate_hash,candidate_count,candidate_last_run_id
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    run_id = tracker.start_run("homepage-acceptance", purpose="acceptance")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=ADDED, final_url=source["url"]),
    )
    tracker.complete_run(run_id)
    after = tracker.db.execute(
        """SELECT snapshot_json,semantic_hash,candidate_snapshot_json,
                  candidate_hash,candidate_count,candidate_last_run_id
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()

    assert first.status == "candidate"
    assert decision.status == "candidate"
    assert "未推进正式候选或基线" in decision.summary
    assert tuple(after) == tuple(before)
    tracker.close()


def test_validation_run_cannot_create_first_baseline(tmp_path):
    tracker, source = make_tracker(tmp_path)
    run_id = tracker.start_run("route-validation")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=BASE, final_url=source["url"]),
    )
    tracker.complete_run(run_id)
    state = tracker.db.execute(
        "SELECT snapshot_json,candidate_snapshot_json FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    assert decision.status == "baseline"
    assert state["snapshot_json"] is None
    assert state["candidate_snapshot_json"] is None
    tracker.close()


def test_configure_linkedin_weekly_reads_enabled_state(tmp_path):
    tracker, source = make_tracker(
        tmp_path,
        url="https://www.linkedin.com/in/alice-zhang",
    )
    state = tracker.configure_linkedin_weekly(source["source_id"], cadence_days=5)
    assert state["cadence_days"] == 5
    tracker.db.execute(
        "UPDATE sources SET tracking_enabled=0 WHERE source_id=?",
        (source["source_id"],),
    )
    tracker.db.commit()
    try:
        tracker.configure_linkedin_weekly(source["source_id"])
    except ValueError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled source must be rejected")
    tracker.close()


def test_report_names_candidates_repeated_three_times(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    run_id, decision = observe(tracker, source, ADDED)
    assert decision.status == "candidate"
    tracker.db.execute(
        "UPDATE sources SET candidate_count=3 WHERE source_id=?",
        (source["source_id"],),
    )
    tracker.db.commit()
    report = tracker.render_report(run_id)
    assert "长期待审候选" in report
    assert "Alice Zhang · homepage" in report
    assert "已重复 3 次" in report
    tracker.close()


def test_304_can_confirm_same_candidate_representation(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE, etag='"v1"')
    _, first = observe(tracker, source, ADDED, etag='"v2"')
    run_id, second = observe(tracker, source, None, status_code=304, etag='"v2"')
    assert first.status == "candidate"
    assert second.status == "changed"
    assert "Paper C" in tracker.render_report(run_id)
    tracker.close()


def test_304_revalidates_stale_candidate_before_confirmation(tmp_path):
    tracker, source = make_tracker(tmp_path)
    legacy = BASE.replace(
        "Paper A, ICML 2025",
        '<a href="https://example.org/paper-a">Paper A, ICML 2025</a>',
    )
    migrated = legacy.replace(
        "https://example.org/paper-a",
        "https://www.example.org/paper-a",
    )
    observe(tracker, source, legacy, etag='"v1"')
    original = tracker.db.execute(
        "SELECT semantic_hash FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()["semantic_hash"]
    candidate = extract_snapshot("homepage", migrated)
    stale_decision = ChangeDecision(
        "candidate",
        0.65,
        "legacy comparator treated the item-ID migration as a change",
        confirmation_count=1,
        confirmations_required=2,
    )
    tracker.db.execute(
        """UPDATE sources SET candidate_snapshot_json=?,candidate_hash=?,
           candidate_count=1,candidate_decision_json=?,quality_json=?
           WHERE source_id=?""",
        (
            candidate.to_json(),
            candidate.comparison_hash,
            json.dumps(asdict(stale_decision), ensure_ascii=False),
            json.dumps({"score": 1.0, "reasons": [], "usable": True}),
            source["source_id"],
        ),
    )
    tracker.db.commit()

    _, decision = observe(
        tracker,
        source,
        None,
        status_code=304,
        etag='"v2"',
    )

    stored = tracker.db.execute(
        """SELECT semantic_hash,candidate_snapshot_json,candidate_count
           FROM sources WHERE source_id=?""",
        (source["source_id"],),
    ).fetchone()
    assert decision.status == "noise"
    assert stored["semantic_hash"] == original
    assert stored["candidate_snapshot_json"] is None
    assert stored["candidate_count"] == 0
    tracker.close()


def test_304_confirmation_runs_configured_summary_and_records_opportunity(tmp_path):
    class SummaryReviewer:
        def __init__(self):
            self.calls = 0

        def summarize_confirmed(self, decision, *, context):
            self.calls += 1
            assert context["change_confirmed"] is True
            assert context["source_kind"] == "homepage"
            decision.reviewer = "deepseek:deepseek-v4-flash:confirmed-summary"
            decision.summary = "已确认变化摘要：新增一篇 ICLR 2026 论文。"
            return decision

    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE, etag='"v1"')
    _, first = observe(tracker, source, ADDED, etag='"v2"')
    reviewer = SummaryReviewer()
    usage: dict[str, int] = {}
    run_id = tracker.start_run("304-deepseek-summary")
    second = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(body=None, status_code=304, etag='"v2"'),
        reviewer=reviewer,
        ai_usage_sink=usage,
    )
    tracker.complete_run(run_id)
    assert first.status == "candidate"
    assert second.status == "changed"
    assert second.reviewer.endswith(":confirmed-summary")
    assert reviewer.calls == 1
    assert usage == {
        "confirmed_summary_eligible": 1,
        "confirmed_summary_attempted": 1,
        "confirmed_summary_completed": 1,
    }
    tracker.close()


def test_partial_200_render_is_health_issue_and_does_not_replace_baseline(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    original = tracker.db.execute(
        "SELECT semantic_hash FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()["semantic_hash"]
    _, first = observe(tracker, source, "<html><h1>Alice Zhang</h1><div>Loading</div></html>")
    _, second = observe(tracker, source, "<html><h1>Alice Zhang</h1><div>Loading</div></html>")
    current = tracker.db.execute(
        "SELECT semantic_hash FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()["semantic_hash"]
    assert first.status == "source_issue_pending"
    assert second.status == "source_issue"
    assert current == original
    tracker.close()


def test_removals_need_three_consistent_observations(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    reduced = BASE.replace("<p>Paper B, NeurIPS 2025</p>", "")
    _, first = observe(tracker, source, reduced)
    _, second = observe(tracker, source, reduced)
    _, third = observe(tracker, source, reduced)
    assert [first.status, second.status, third.status] == ["candidate", "candidate", "changed"]
    assert third.confirmations_required == 3
    tracker.close()


def test_low_distance_stable_url_removal_never_advances_confirmed_baseline(tmp_path):
    tracker, source = make_tracker(tmp_path)
    linked = BASE.replace(
        "<p>Paper A, ICML 2025</p>",
        '<p><a href="https://example.org/paper-a">Paper A, ICML 2025</a></p>',
    ).replace(
        "<p>Paper B, NeurIPS 2025</p>",
        '<p><a href="https://example.org/paper-b">Paper B, NeurIPS 2025</a></p>',
    )
    observe(tracker, source, linked)
    original = tracker.db.execute(
        "SELECT semantic_hash FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()["semantic_hash"]
    reduced = linked.replace(
        '<p><a href="https://example.org/paper-b">Paper B, NeurIPS 2025</a></p>',
        "",
    )
    _, decision = observe(tracker, source, reduced)
    stored = tracker.db.execute(
        "SELECT semantic_hash,candidate_snapshot_json FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    assert decision.status == "candidate"
    assert decision.removals
    assert stored["semantic_hash"] == original
    assert stored["candidate_snapshot_json"] is not None
    tracker.close()


def test_source_failures_are_dampened_but_reported_on_repeat(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    run1, first = observe(tracker, source, None, status_code=500, error="temporary")
    run2, second = observe(tracker, source, None, status_code=500, error="temporary")
    assert first.status == "source_issue_pending"
    assert "来源不可用" not in tracker.render_report(run1)
    assert "首次来源异常：1" in tracker.render_report(run1)
    assert second.status == "source_issue"
    assert "暂时不可达" in tracker.render_report(run2)
    tracker.close()


def linkedin_projection(*, include_new_job: bool = False):
    jobs = [
        {
            "stable_id": "urn:li:fsd_profilePosition:100",
            "text": "Research Scientist · Example AI Lab · 2024–Present",
        }
    ]
    if include_new_job:
        jobs.append(
            {
                "stable_id": "urn:li:fsd_profilePosition:200",
                "text": "Visiting Researcher · Example University · 2026–Present",
            }
        )
    return {
        "canonical_url": "https://www.linkedin.com/in/alice-zhang",
        "profile": {
            "name": "Alice Zhang",
            "headline": "Research Scientist",
            "location": "San Francisco Bay Area",
        },
        "sections": [
            {"name": "experience", "items": jobs},
            {
                "name": "education",
                "items": [
                    {
                        "stable_id": "linkedin:education:300",
                        "text": "Example University · PhD, Computer Science",
                    }
                ],
            },
        ],
        "sentinels": ["main", "experience", "education"],
    }


def test_logged_in_linkedin_projection_uses_stable_section_ids(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice-zhang")
    _, baseline = observe(tracker, source, linkedin_projection())
    run_id, changed = observe(tracker, source, linkedin_projection(include_new_job=True))
    assert baseline.status == "baseline"
    assert changed.status == "changed"
    assert changed.confirmations_required == 1
    assert "Visiting Researcher" in tracker.render_report(run_id)
    tracker.close()


def test_storage_contains_semantic_manifests_not_raw_html(tmp_path):
    tracker, source = make_tracker(tmp_path)
    noisy = BASE.replace("<head>", "<head><script nonce='secret-random-value'>runtime()</script>")
    observe(tracker, source, noisy)
    stored = tracker.db.execute(
        "SELECT snapshot_json FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()["snapshot_json"]
    assert "secret-random-value" not in stored
    manifest = json.loads(stored)
    assert manifest["items"] and manifest["semantic_hash"] and manifest["sentinels"]
    tracker.close()


def test_extractor_upgrade_rebuilds_baseline_without_reporting_change(tmp_path):
    tracker, source = make_tracker(tmp_path)
    observe(tracker, source, BASE)
    stored = tracker.db.execute(
        "SELECT snapshot_json FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()["snapshot_json"]
    old_manifest = json.loads(stored)
    old_manifest["extractor_version"] = "legacy-v1"
    tracker.db.execute(
        "UPDATE sources SET snapshot_json=? WHERE source_id=?",
        (json.dumps(old_manifest), source["source_id"]),
    )
    tracker.db.commit()
    run_id, decision = observe(tracker, source, ADDED)
    assert decision.status == "baseline"
    assert "Paper C" not in tracker.render_report(run_id)
    tracker.close()


def public_linkedin_html(canonical_url: str) -> str:
    return f"""
    <html><head>
      <link rel="canonical" href="{canonical_url}">
      <meta property="og:title" content="Fengshuo Liu">
      <script type="application/ld+json">
      {{
        "@context": "https://schema.org",
        "@type": "Person",
        "name": "Fengshuo Liu",
        "jobTitle": "Graduate Researcher",
        "description": "Cancer biology and single-cell research",
        "worksFor": {{"name": "Example Medical Center"}},
        "alumniOf": {{"name": "Example College of Medicine"}}
      }}
      </script>
    </head><body><main><h1>Fengshuo Liu</h1>
      <h2>Experience</h2><p>Graduate Researcher at Example Medical Center</p>
      <h2>Education</h2><p>Example College of Medicine, 2021–2026</p>
    </main></body></html>
    """


def test_anonymous_linkedin_jsonld_can_form_public_baseline(tmp_path):
    target = "https://www.linkedin.com/in/fengshuo-liu-722169307"
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    source = tracker.add_person("Fengshuo Liu", urls=[target])["sources"][0]
    _, decision = observe(tracker, source, public_linkedin_html(target))
    assert decision.status == "baseline"
    stored = tracker.db.execute(
        "SELECT snapshot_json FROM sources WHERE source_id=?", (source["source_id"],)
    ).fetchone()["snapshot_json"]
    manifest = json.loads(stored)
    assert manifest["identity"]["name"] == "Fengshuo Liu"
    assert manifest["canonical_url"] == target
    tracker.close()


def test_search_index_profile_with_different_canonical_id_is_rejected(tmp_path):
    target = "https://www.linkedin.com/in/fengshuo-liu-722169307"
    other = "https://www.linkedin.com/in/fengshuo-liu-420855a5"
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    source = tracker.add_person("Fengshuo Liu", urls=[target])["sources"][0]
    indexed_projection = {
        "canonical_url": other,
        "profile": {"name": "Fengshuo Liu", "headline": "Graduate Researcher"},
        "sections": [
            {
                "name": "experience",
                "items": [{"text": "Graduate Researcher at Example Medical Center"}],
            },
            {
                "name": "education",
                "items": [{"text": "Example College of Medicine, 2021–2026"}],
            },
        ],
        "sentinels": ["main", "experience", "education"],
    }
    run_id = tracker.start_run("anonymous-linkedin-index")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=indexed_projection,
            retrieval_mode="search_index",
            final_url=other,
        ),
    )
    tracker.complete_run(run_id)
    # The body itself must also carry the indexed canonical URL.
    assert decision.status == "binding_conflict"
    tracker.close()


def test_linkedin_retrieval_mode_switch_rebaselines_without_false_change(tmp_path):
    target = "https://www.linkedin.com/in/fengshuo-liu-722169307"
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    source = tracker.add_person("Fengshuo Liu", urls=[target])["sources"][0]
    run1 = tracker.start_run("direct-public")
    first = tracker.observe(
        run1,
        source["source_id"],
        FetchObservation(
            body=public_linkedin_html(target),
            retrieval_mode="browser_public",
        ),
    )
    tracker.complete_run(run1)
    projection = {
        "canonical_url": target,
        "profile": {"name": "Fengshuo Liu", "headline": "Graduate Researcher"},
        "sections": [
            {
                "name": "experience",
                "items": [{"text": "Graduate Researcher at Example Medical Center"}],
            },
            {
                "name": "education",
                "items": [{"text": "Example College of Medicine, 2021–2026"}],
            },
        ],
        "sentinels": ["main", "experience", "education"],
    }
    run2 = tracker.start_run("search-index")
    second = tracker.observe(
        run2,
        source["source_id"],
        FetchObservation(body=projection, retrieval_mode="search_index"),
    )
    tracker.complete_run(run2)
    assert first.status == "baseline"
    assert second.status == "baseline"
    assert "已确认" in tracker.render_report(run2)
    tracker.close()


def weekly_linkedin_projection(
    *,
    second_job=False,
    edited_title=None,
    edited_description=None,
    include_experience=True,
):
    experience_items = [
        {
            "title": edited_title or "Research Intern",
            "company": "Example AI Lab",
            "employment_type": "Internship",
            "location": "Beijing, China",
            "start_date": "2025-06",
            "end_date": "2025-12",
            "description": edited_description or "Worked on reliable model evaluation.",
            "company_url": "https://www.linkedin.com/company/example-ai-lab",
            "detail_url": "https://www.linkedin.com/in/alice/details/experience/100",
            "text": "Research Intern · Example AI Lab · 2025-06 – 2025-12",
        }
    ]
    if second_job:
        experience_items.append(
            {
                "title": "Research Scientist",
                "company": "New Research Lab",
                "employment_type": "Full-time",
                "location": "Shanghai, China",
                "start_date": "2026-01",
                "end_date": "Present",
                "description": "Researching multimodal agents.",
                "company_url": "https://www.linkedin.com/company/new-research-lab",
                "detail_url": "https://www.linkedin.com/in/alice/details/experience/200",
                "text": "Research Scientist · New Research Lab · 2026-01 – Present",
            }
        )
    sections = []
    if include_experience:
        sections.append({"name": "experience", "items": experience_items})
    sections.append(
        {
            "name": "education",
            "items": [
                {
                    "stable_id": "https://www.linkedin.com/school/example-university",
                    "text": "Example University · PhD, Computer Science · 2021–2026",
                }
            ],
        }
    )
    return {
        "canonical_url": "https://www.linkedin.com/in/alice",
        "profile": {
            "name": "Alice Zhang",
            "headline": "AI Researcher",
            "location": "Shanghai, China",
            "summary": "Research on reliable AI systems.",
        },
        "sections": sections,
        "sentinels": ["main", "profile-name", *(["experience"] if include_experience else [])],
    }


def test_linkedin_weekly_state_keeps_fixed_fields_experience_and_due_date(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    run_id = tracker.start_run("weekly-linkedin")
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=weekly_linkedin_projection(),
            observed_at="2026-07-01T08:00:00+00:00",
            retrieval_mode="browser_public",
        ),
    )
    tracker.complete_run(run_id)
    state = tracker.linkedin_profile_state(source["source_id"])
    assert decision.status == "baseline"
    assert state["fixed_profile"]["headline"] == "AI Researcher"
    assert state["experiences"][0]["title"] == "Research Intern"
    assert state["next_check_at"] == "2026-07-08T08:00:00+00:00"
    assert tracker.list_due_linkedin("2026-07-08T07:59:59+00:00") == []
    assert tracker.list_due_linkedin("2026-07-08T08:00:00+00:00")[0]["source_id"] == source["source_id"]
    tracker.close()


def test_unchanged_homepage_still_schedules_the_next_weekly_check(tmp_path):
    tracker, source = make_tracker(tmp_path)
    first_run = tracker.start_run("homepage-schedule")
    baseline = tracker.observe(
        first_run,
        source["source_id"],
        FetchObservation(
            body=BASE,
            observed_at="2026-07-01T08:00:00+00:00",
        ),
    )
    tracker.complete_run(first_run)
    assert baseline.status == "baseline"
    assert tracker.list_due_sources(
        "2026-07-08T07:59:59+00:00",
        kinds=("homepage",),
    ) == []
    assert tracker.list_due_sources(
        "2026-07-08T08:00:00+00:00",
        kinds=("homepage",),
    )[0]["source_id"] == source["source_id"]

    second_run = tracker.start_run("homepage-schedule")
    unchanged = tracker.observe(
        second_run,
        source["source_id"],
        FetchObservation(
            body=BASE,
            observed_at="2026-07-08T08:00:00+00:00",
        ),
    )
    tracker.complete_run(second_run)
    state = tracker.db.execute(
        "SELECT next_check_at FROM sources WHERE source_id=?",
        (source["source_id"],),
    ).fetchone()
    assert unchanged.status == "unchanged"
    assert state["next_check_at"] == "2026-07-15T08:00:00+00:00"
    assert tracker.list_due_sources(
        "2026-07-15T07:59:59+00:00",
        kinds=("homepage",),
    ) == []
    assert tracker.list_due_sources(
        "2026-07-15T08:00:00+00:00",
        kinds=("homepage",),
    )[0]["source_id"] == source["source_id"]
    tracker.close()


def test_linkedin_weekly_new_experience_reports_short_summary_and_link(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    observe(tracker, source, weekly_linkedin_projection())
    run_id, decision = observe(
        tracker,
        source,
        weekly_linkedin_projection(second_job=True),
    )
    report = tracker.render_linkedin_weekly_report(run_id)
    assert decision.status == "changed"
    assert "新增工作经历：Research Scientist @ New Research Lab" in report
    assert "[LinkedIn](https://linkedin.com/in/alice)" in report
    tracker.close()


def test_linkedin_weekly_edit_same_experience_detects_fields_not_new_item(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    observe(tracker, source, weekly_linkedin_projection())
    run_id, decision = observe(
        tracker,
        source,
        weekly_linkedin_projection(edited_title="Research Engineer"),
    )
    report = tracker.render_linkedin_weekly_report(run_id)
    assert decision.status == "changed"
    assert not decision.additions
    assert decision.modifications
    assert "职位：Research Intern → Research Engineer" in report
    tracker.close()


def test_linkedin_weekly_description_only_edit_is_not_missed_by_stable_id(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    observe(tracker, source, weekly_linkedin_projection())
    run_id, decision = observe(
        tracker,
        source,
        weekly_linkedin_projection(
            edited_description="Led reliable model evaluation and red-team research."
        ),
    )
    assert decision.status == "changed"
    assert "经历描述有编辑" in tracker.render_linkedin_weekly_report(run_id)
    tracker.close()


def test_linkedin_weekly_reorder_is_unchanged(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    baseline = weekly_linkedin_projection(second_job=True)
    observe(tracker, source, baseline)
    reordered = weekly_linkedin_projection(second_job=True)
    reordered["sections"][0]["items"].reverse()
    _, decision = observe(tracker, source, reordered)
    assert decision.status == "unchanged"
    tracker.close()


def test_linkedin_weekly_missing_experience_section_preserves_old_state(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    observe(tracker, source, weekly_linkedin_projection())
    before = tracker.linkedin_profile_state(source["source_id"])
    run_id, decision = observe(
        tracker,
        source,
        weekly_linkedin_projection(include_experience=False),
    )
    after = tracker.linkedin_profile_state(source["source_id"])
    assert decision.status == "source_issue_pending"
    assert before["experiences"] == after["experiences"]
    assert "未能取得完整公开页面" in tracker.render_linkedin_weekly_report(run_id)
    tracker.close()


def test_linkedin_search_index_change_needs_two_weekly_observations(tmp_path):
    tracker, source = make_tracker(tmp_path, url="https://www.linkedin.com/in/alice")
    run1 = tracker.start_run("weekly-index")
    baseline = tracker.observe(
        run1,
        source["source_id"],
        FetchObservation(
            body=weekly_linkedin_projection(),
            retrieval_mode="search_index",
        ),
    )
    tracker.complete_run(run1)
    run2 = tracker.start_run("weekly-index")
    first = tracker.observe(
        run2,
        source["source_id"],
        FetchObservation(
            body=weekly_linkedin_projection(second_job=True),
            retrieval_mode="search_index",
        ),
    )
    tracker.complete_run(run2)
    run3 = tracker.start_run("weekly-index")
    second = tracker.observe(
        run3,
        source["source_id"],
        FetchObservation(
            body=weekly_linkedin_projection(second_job=True),
            retrieval_mode="search_index",
        ),
    )
    tracker.complete_run(run3)
    assert baseline.status == "baseline"
    assert first.status == "candidate"
    assert second.status == "changed"
    tracker.close()
