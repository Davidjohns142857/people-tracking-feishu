from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from people_intel.light_tracker import (
    FetchObservation,
    LightTracker,
    SnapshotItem,
    assess_snapshot,
    classify_url,
    compare_snapshots,
    dossier_records,
    extract_snapshot,
    health_status,
    canonical_url,
    _scheduled_next_check_at,
)


def generic(body: str):
    return extract_snapshot("homepage", body)


def test_homepage_identity_only_shell_is_not_a_usable_baseline():
    snapshot = generic(
        "<html><head><title>Alice Zhang</title></head>"
        "<body><h1>Alice Zhang</h1></body></html>"
    )
    score, reasons, usable = assess_snapshot(
        snapshot,
        expected_names=["Alice Zhang"],
        source_scoped_names=["Alice Zhang"],
        expected_url="https://profiles.example/alice",
        curated_binding=True,
    )

    assert snapshot.token_count < 5
    assert snapshot.item_count <= 1
    assert score == 0.55
    assert "提取到的稳定内容过少" in reasons
    assert usable is False


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            "<html><head><title>Alice</title></head><body><h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p></body></html>",
            "<html><head><title>Alice</title><script nonce='random'>x</script></head><body><nav>Menu changed</nav><h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p><footer>Copyright 2026</footer></body></html>",
            "unchanged",
        ),
        (
            "<h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p>",
            "<h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p><p>Paper B, NeurIPS 2026</p>",
            "changed",
        ),
        (
            "<h1>Alice</h1><p>PhD student at MIT working on ML.</p>",
            "<h1>Alice</h1><p>Assistant professor at Stanford working on ML.</p>",
            "changed",
        ),
    ],
)
def test_homepage_semantic_adversarial_cases(before, after, expected):
    assert compare_snapshots(generic(before), generic(after)).status == expected


def scholar_html(
    *,
    citation: int,
    include_b: bool = False,
    locale: str | None = None,
    paper_a_venue: str = "ICML 2025",
    paper_a_title: str = "Paper A",
) -> str:
    locale_query = f"&hl={locale}&oe=ASCII" if locale else ""
    row_b = """
    <tr class="gsc_a_tr" data-href="/citations?view_op=view_citation&citation_for_view=u:B">
      <td><a class="gsc_a_at">Paper B</a><div class="gs_gray">Alice</div><div class="gs_gray">NeurIPS 2026</div></td>
      <td class="gsc_a_c"><a class="gsc_a_ac">0</a></td><td class="gsc_a_y">2026</td>
    </tr>""" if include_b else ""
    return f"""
    <div id="gsc_prf_in">Alice</div><div id="gsc_prf_i">Example University</div>
    <tr class="gsc_a_tr" data-href="/citations?view_op=view_citation{locale_query}&citation_for_view=u:A">
      <td><a class="gsc_a_at">{paper_a_title}</a><div class="gs_gray">Alice</div><div class="gs_gray">{paper_a_venue}</div></td>
      <td class="gsc_a_c"><a class="gsc_a_ac">{citation}</a></td><td class="gsc_a_y">2025</td>
    </tr>{row_b}"""


def test_scholar_ignores_citation_churn_but_detects_new_publication():
    baseline = extract_snapshot("scholar", scholar_html(citation=10))
    assert compare_snapshots(baseline, extract_snapshot("scholar", scholar_html(citation=11))).status == "unchanged"
    assert compare_snapshots(baseline, extract_snapshot("scholar", scholar_html(citation=11, include_b=True))).status == "changed"


def test_scholar_locale_query_does_not_change_publication_identity():
    english = extract_snapshot("scholar", scholar_html(citation=10, locale="en"))
    french = extract_snapshot("scholar", scholar_html(citation=11, locale="fr"))
    publication = next(item for item in english.items if item.category == "publication")
    assert publication.stable_id == "scholar-citation:u:A"
    assert english.semantic_hash == french.semantic_hash
    assert compare_snapshots(english, french).status == "unchanged"


def test_legacy_scholar_locale_id_migrates_without_false_change():
    legacy = extract_snapshot("scholar", scholar_html(citation=10, locale="en"))
    current = extract_snapshot("scholar", scholar_html(citation=10, locale="fr"))
    publication = next(item for item in legacy.items if item.category == "publication")
    publication.stable_id = (
        "/citations?view_op=view_citation&hl=en&oe=ASCII&citation_for_view=u:A"
    )
    legacy.semantic_hash = "legacy-locale-sensitive-hash"
    decision = compare_snapshots(legacy, current)
    assert decision.status == "unchanged"
    assert "locale" in decision.summary


def test_legacy_www_item_id_migrates_without_false_person_change():
    body = (
        "<title>Alice Zhang</title><h1>Alice Zhang</h1>"
        "<h2>Publications</h2>"
        "<p><a href='https://www.example.edu/paper'>"
        "A Stable Machine Learning Publication</a></p>"
    )
    legacy = generic(body)
    current = generic(body)
    linked = next(item for item in legacy.items if item.stable_id)
    linked.stable_id = linked.stable_id.replace("://www.", "://", 1)
    legacy.semantic_hash = "legacy-www-stripping-hash"

    decision = compare_snapshots(legacy, current)

    assert decision.status == "noise"
    assert decision.additions == []
    assert decision.removals == []
    assert decision.modifications == []


@pytest.mark.parametrize(
    ("before_url", "after_url", "before_text", "after_text"),
    [
        (
            "https://example.edu/paper",
            "https://www.example.edu/paper",
            "A Stable Machine Learning Publication",
            "A Revised Machine Learning Publication",
        ),
        (
            "https://example.edu/paper-v1",
            "https://example.edu/paper-v2",
            "A Stable Machine Learning Publication",
            "A Stable Machine Learning Publication",
        ),
        (
            "https://example.edu/paper",
            "https://different.example/paper",
            "A Stable Machine Learning Publication",
            "A Stable Machine Learning Publication",
        ),
    ],
)
def test_www_migration_equivalence_does_not_hide_real_item_change(
    before_url,
    after_url,
    before_text,
    after_text,
):
    def linked(url, text):
        return (
            "<title>Alice Zhang</title><h1>Alice Zhang</h1>"
            "<h2>Publications</h2>"
            f"<p><a href='{url}'>{text}</a></p>"
        )

    decision = compare_snapshots(
        generic(linked(before_url, before_text)),
        generic(linked(after_url, after_text)),
    )

    assert decision.status == "changed"


def test_legacy_privacy_footer_item_is_not_a_person_change():
    body = (
        "<title>Alice Zhang</title><h1>Alice Zhang</h1>"
        "<p>Reliable machine learning research and publications.</p>"
    )
    legacy = generic(body)
    current = generic(body)
    legacy.items.append(
        SnapshotItem(
            key="legacy-privacy-footer",
            category="content",
            text="Privacy",
            stable_id="https://example.edu/privacy",
        )
    )
    legacy.semantic_hash = "legacy-privacy-footer-hash"

    decision = compare_snapshots(legacy, current)

    assert decision.status == "noise"
    assert decision.removals == []


def test_scholar_ignores_window_removal_and_venue_enrichment():
    baseline = extract_snapshot("scholar", scholar_html(citation=10, include_b=True))
    venue_enriched = extract_snapshot(
        "scholar",
        scholar_html(citation=11, paper_a_venue="ICML 2025, pages 1-12"),
    )
    decision = compare_snapshots(baseline, venue_enriched)
    assert decision.status == "unchanged"
    assert decision.removals == []


def test_scholar_detects_title_edit_for_same_publication_id():
    baseline = extract_snapshot("scholar", scholar_html(citation=10))
    corrected = extract_snapshot(
        "scholar",
        scholar_html(citation=10, paper_a_title="Paper A: Corrected Title"),
    )
    decision = compare_snapshots(baseline, corrected)
    assert decision.status == "changed"
    assert decision.modifications[0]["changed_fields"] == ["title"]


def test_scholar_uses_dedicated_extractor_version():
    snapshot = extract_snapshot("scholar", scholar_html(citation=10))
    assert snapshot.extractor_version == "semantic-manifest-v5-scholar-recent100"


def test_github_ignores_followers_but_detects_new_repository():
    before = {"profile": {"login": "alice", "id": 1, "followers": 10}, "repositories": [{"id": 2, "name": "old", "description": "baseline"}]}
    counters = {"profile": {"login": "alice", "id": 1, "followers": 11}, "repositories": [{"id": 2, "name": "old", "description": "baseline"}]}
    added = {"profile": {"login": "alice", "id": 1, "followers": 11}, "repositories": [*counters["repositories"], {"id": 3, "name": "new-model", "description": "released"}]}
    baseline = extract_snapshot("github", before)
    assert compare_snapshots(baseline, extract_snapshot("github", counters)).status == "unchanged"
    assert compare_snapshots(baseline, extract_snapshot("github", added)).status == "changed"


def test_linkedin_authwall_is_health_issue_not_profile_change():
    observation = FetchObservation(
        body="<title>LinkedIn Login</title><div class='authwall'>Sign in</div>",
        status_code=200,
        final_url="https://www.linkedin.com/authwall",
    )
    assert health_status(observation, "linkedin")[0] == "blocked"


def test_http_status_takes_precedence_over_transport_error_text():
    observation = FetchObservation(
        body=None,
        status_code=403,
        final_url="https://api.github.com/users/alice",
        error="HTTP Error 403: rate limit exceeded",
    )
    assert health_status(observation, "github") == ("blocked", "HTTP 403")


def test_http_200_access_fingerprint_is_blocked_before_extraction():
    observation = FetchObservation(
        body=None,
        status_code=200,
        final_url="https://profiles.example/alice",
        error="access_fingerprint=cloudflare_challenge",
    )
    assert health_status(observation, "homepage") == (
        "blocked",
        "access_fingerprint=cloudflare_challenge",
    )


def test_rate_limit_schedules_hours_scale_recovery_instead_of_weekly_delay():
    next_check = _scheduled_next_check_at(
        "2026-08-03T00:00:00+00:00",
        cadence_days=7,
        health_status="rate_limited",
        consecutive_failures=1,
        source_id="source-a",
    )
    delay = datetime.fromisoformat(next_check) - datetime.fromisoformat(
        "2026-08-03T00:00:00+00:00"
    )
    assert timedelta(hours=6) <= delay <= timedelta(hours=6, minutes=30)


def test_store_requires_anchor_tracks_delta_and_renders_readable_report(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    with pytest.raises(ValueError):
        tracker.add_person("No Anchor")
    person = tracker.add_person(
        "Alice Zhang",
        urls=["https://alice.example/", "https://github.com/alice"],
        profile={"school": "Example University", "research_focus": "reliable ML", "stage": "PhD"},
    )
    assert person["person_key"].startswith("alice-zhang--github-")
    homepage = next(item for item in person["sources"] if item["kind"] == "homepage")
    run1 = tracker.start_run()
    assert tracker.observe(run1, homepage["source_id"], FetchObservation(body="<h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p>")).status == "baseline"
    tracker.complete_run(run1)
    run2 = tracker.start_run()
    decision = tracker.observe(run2, homepage["source_id"], FetchObservation(body="<h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p><p>Paper B, NeurIPS 2026</p>"))
    tracker.complete_run(run2)
    assert decision.status == "candidate"
    assert "Paper B" not in tracker.render_report(run2)
    run3 = tracker.start_run()
    decision = tracker.observe(run3, homepage["source_id"], FetchObservation(body="<h1>Alice</h1><h2>Publications</h2><p>Paper A, ICML 2025</p><p>Paper B, NeurIPS 2026</p>"))
    tracker.complete_run(run3)
    assert decision.status == "changed"
    report = tracker.render_report(run3)
    assert "Alice Zhang" in report and "Paper B" in report and "reliable ML" in report
    assert tracker.match_person("Alice Zhang", urls=["https://github.com/alice"])[0]["decision"] == "auto_merge"
    tracker.close()


def test_preserves_multiple_homepages_and_recognizes_regional_scholar(tmp_path):
    regional_scholar = "https://scholar.google.com.tw/citations?user=G14TzOEAAAAJ"
    assert classify_url(regional_scholar) == "scholar"
    assert classify_url("https://huggingface.co/hyg22") == "homepage"
    assert classify_url("https://huggingface.co/papers/2601.15165") is None

    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "Alice Zhang",
        urls=[
            "https://alice.example/",
            "https://example.edu/people/alice",
            regional_scholar,
            "https://alice.example/",
        ],
    )
    assert [source["kind"] for source in person["sources"]].count("homepage") == 2
    assert [source["kind"] for source in person["sources"]].count("scholar") == 1
    assert person["secondary_id_type"] == "scholar"
    tracker.close()


def test_homepage_canonical_url_preserves_meaningful_www_authority():
    assert canonical_url(
        "https://www.cs.toronto.edu/~gdzhang/"
    ) == "https://www.cs.toronto.edu/~gdzhang"
    assert canonical_url(
        "https://www.linkedin.com/in/alice/?trk=public"
    ) == "https://linkedin.com/in/alice"
    assert canonical_url(
        "https://www.github.com/alice/"
    ) == "https://github.com/alice"


def test_homepage_canonical_url_preserves_functional_query_and_drops_tracking():
    assert canonical_url(
        "https://profiles.example/person?lang=zh&view=research&utm_source=newsletter&fbclid=x"
    ) == "https://profiles.example/person?lang=zh&view=research"
    assert canonical_url(
        "https://pkuzqh.github.io/?utmsource=chatgpt.com"
    ) == "https://pkuzqh.github.io/"


def test_merges_name_variant_when_stable_anchor_matches(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    first = tracker.add_person(
        "Xindi (Cindy) Wu",
        urls=["https://scholar.google.com/citations?user=hvnUnrUAAAAJ"],
        profile={"school": "Princeton University", "cohort": "Apple Scholars 2026"},
    )
    second = tracker.add_person(
        "Xindi Wu",
        aliases=["Cindy Wu", "吴昕迪"],
        urls=[
            "https://scholar.google.com/citations?user=hvnUnrUAAAAJ&hl=en",
            "https://www.linkedin.com/in/xindi-cindy-wu-3ba243111",
        ],
        profile={"research_focus": "multimodal learning", "cohort": "ICML 2026"},
    )
    assert first["person_key"] == second["person_key"]
    assert len(tracker.list_people()) == 1
    assert second["canonical_name"] == "Xindi (Cindy) Wu"
    assert {"Xindi Wu", "Cindy Wu", "吴昕迪"} <= set(second["aliases"])
    assert second["profile"]["cohorts"] == ["Apple Scholars 2026", "ICML 2026"]
    assert second["profile"]["research_focus"] == "multimodal learning"
    assert {source["kind"] for source in second["sources"]} == {"scholar", "linkedin"}
    tracker.close()


def test_shared_organization_homepage_does_not_merge_incompatible_names(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    tracker.add_person("Alice Zhang", urls=["https://example.edu/"])
    tracker.add_person("Bob Li", urls=["https://example.edu/"])
    assert len(tracker.list_people()) == 2
    tracker.close()


def test_source_scoped_identity_name_allows_verified_romanized_profile(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "张懿元",
        urls=["https://github.com/invictus717"],
        profile={
            "source_identity_names": {
                "github:invictus717": ["Yiyuan Zhang"],
            }
        },
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body={
                "profile": {
                    "login": "invictus717",
                    "name": "Yiyuan Zhang",
                    "type": "User",
                    "id": 123,
                },
                "repositories": [
                    {
                        "id": 456,
                        "name": "research-code",
                        "description": "public research repository",
                    }
                ],
            }
        ),
    )
    assert decision.status == "baseline"
    assert tracker.person(person["person_key"])["aliases"] == []
    tracker.close()


def test_chinese_name_is_verified_from_romanized_name_anywhere_on_page(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "张懿元",
        urls=["https://example.edu/researcher/717"],
        profile={"registry_person_id": "mono_person_717"},
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=(
                "<html><head><title>Research Group</title></head><body>"
                "<h1>People</h1><div>Yiyuan Zhang is a PhD researcher.</div>"
                "<p>Reliable machine learning and evaluation.</p></body></html>"
            ),
            final_url=source["url"],
        ),
    )
    state = tracker.person(person["person_key"])["sources"][0]
    assert decision.status == "baseline"
    assert state["binding_status"] == "verified"
    assert state["binding_evidence"]["whole_page_name_match"] is True
    tracker.close()


def test_curated_github_handle_does_not_require_display_name_match(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "仲殷旻",
        urls=["https://github.com/PKUFlyingPig"],
        profile={"registry_person_id": "mono_person_pkuflyingpig"},
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body={
                "profile": {
                    "login": "PKUFlyingPig",
                    "name": "Flying Pig",
                    "type": "User",
                    "id": 123,
                },
                "repositories": [
                    {"id": 456, "name": "llm-course", "description": "course"}
                ],
            },
            final_url=source["url"],
        ),
    )
    state = tracker.person(person["person_key"])["sources"][0]
    assert decision.status == "baseline"
    assert state["binding_status"] == "verified"
    assert state["binding_evidence"]["stable_platform_id_match"] is True
    tracker.close()


def test_curated_homepage_without_identity_corrobation_stays_binding_review(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "张懿元",
        urls=["https://example.edu/people/other"],
        profile={"registry_person_id": "mono_person_wrong"},
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body="<title>Other Person</title><h1>Other Person</h1><p>Biology professor at Example University.</p>",
            final_url=source["url"],
        ),
    )
    assert decision.status == "binding_review"
    assert tracker.person(person["person_key"])["sources"][0]["binding_status"] == "review"
    tracker.close()


def test_curated_personal_domain_can_verify_full_name_with_middle_initial(tmp_path):
    tracker = LightTracker(tmp_path / "tracker.sqlite3")
    person = tracker.add_person(
        "Anjali Gupta",
        urls=["https://anjaliwgupta.com/"],
        profile={"registry_person_id": "mono_person_anjali"},
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body="<title>Portfolio</title><h1>Research</h1><p>Machine learning and systems research.</p>",
            final_url=source["url"],
        ),
    )
    state = tracker.person(person["person_key"])["sources"][0]
    assert decision.status == "baseline"
    assert state["binding_status"] == "verified"
    assert state["binding_evidence"]["url_name_match"] is True
    tracker.close()


@pytest.mark.parametrize(
    ("registered", "observed"),
    [
        ("Tian (Sunny) Qin", "Sunny Qin"),
        ("Yecheng (Jason) Ma", "Jason Ma"),
        ('Chia-Chun "Alden" Hung', "Alden Hung"),
        ('谢若宇', "Roy Xie"),
    ],
)
def test_parenthetical_or_source_scoped_english_name_can_verify_profile(
    tmp_path,
    registered,
    observed,
):
    profile = {"registry_person_id": f"mono_{observed}"}
    aliases = []
    if registered == "谢若宇":
        aliases = ['Ruoyu "Roy" Xie']
    tracker = LightTracker(tmp_path / f"{observed}.sqlite3")
    person = tracker.add_person(
        registered,
        aliases=aliases,
        urls=[f"https://example.edu/{observed.replace(' ', '-')}"],
        profile=profile,
    )
    source = person["sources"][0]
    run_id = tracker.start_run()
    decision = tracker.observe(
        run_id,
        source["source_id"],
        FetchObservation(
            body=f"<title>{observed}</title><h1>{observed}</h1><p>Machine learning research.</p>",
            final_url=source["url"],
        ),
    )
    assert decision.status == "baseline"
    assert tracker.person(person["person_key"])["sources"][0]["binding_status"] == "verified"
    tracker.close()


def test_full_visible_fingerprint_prevents_false_unchanged_for_unclassified_text():
    before = generic(
        "<title>Alice</title><h1>Alice</h1><p>Research scientist at Example Lab.</p><p>2024</p>"
    )
    after = generic(
        "<title>Alice</title><h1>Alice</h1><p>Research scientist at Example Lab.</p><p>2025</p>"
    )
    decision = compare_snapshots(before, after)
    assert before.semantic_hash == after.semantic_hash
    assert before.visible_content_hash != after.visible_content_hash
    assert decision.status == "ambiguous"


def test_real_dossier_formats_preserve_people_and_enforce_anchor_gate():
    icml_value = os.environ.get("PEOPLE_TRACKING_TEST_ICML_DOSSIER")
    apple_value = os.environ.get("PEOPLE_TRACKING_TEST_APPLE_DOSSIER")
    if not icml_value or not apple_value:
        pytest.skip("optional dossier test paths are not configured")
    icml = Path(icml_value)
    apple = Path(apple_value)
    if not icml.exists() or not apple.exists():
        pytest.skip("user source dossiers are not present")
    icml_records, apple_records = dossier_records(icml, "icml"), dossier_records(apple, "apple")
    assert len(icml_records) == 16
    assert len(apple_records) == 56
    assert sum(bool(row["urls"]) for row in apple_records) >= 50
    assert any(any("scholar.google.com" in url for url in row["urls"]) for row in apple_records)
    assert any(any("linkedin.com" in url for url in row["urls"]) for row in apple_records)
