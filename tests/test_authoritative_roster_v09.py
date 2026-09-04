from __future__ import annotations

import json
from pathlib import Path

import pytest

from people_tracking_feishu.ingest import canonical_records
from people_tracking_feishu.state import PortableState

FIELD_MAPPING = {
    "name": "姓名",
    "secondary_id": "人员编号",
    "aliases": "别名",
    "school": "学校",
    "research_focus": "研究方向",
    "stage": "阶段",
    "homepage": "个人主页",
    "github": "GitHub",
    "sync_status": "同步状态",
    "record_type": "记录类型",
    "managed_by": "管理方式",
    "review_decision": "审核决定",
}


def _row(
    *,
    record_id: str = "rec-alice",
    name: str = "Alice Old",
    aliases: str = "A. Old",
    school: str = "Old University",
    homepage: str = "https://example.org/alice",
    github: str = "https://github.com/alice-example",
    status: str = "有效",
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "fields": {
            "姓名": name,
            "人员编号": "person-alice",
            "别名": aliases,
            "学校": school,
            "研究方向": "reliable systems",
            "阶段": "faculty",
            "个人主页": homepage,
            "GitHub": github,
            "同步状态": status,
        },
    }


def _canonical(row: dict[str, object]) -> dict[str, object]:
    return canonical_records([row], FIELD_MAPPING)[0]


def test_authoritative_reconcile_overrides_human_fields_and_resolves_conflicts(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        first = _canonical(_row())
        state.reconcile_source_records("base:people", [first])

        changed = _canonical(
            _row(
                name="Alice New",
                aliases="A. New",
                school="New University",
            )
        )
        non_authoritative = state.reconcile_source_records("base:people", [changed])
        assert non_authoritative["scan_source_ids"] == []
        assert state.tracker.list_people()[0]["canonical_name"] == "Alice Old"
        assert state.tracker.list_people()[0]["profile"]["school"] == "Old University"
        assert state.db.execute(
            "SELECT COUNT(*) FROM human_conflicts WHERE status='open'"
        ).fetchone()[0] >= 2

        # The roster hash is already current after the non-authoritative pass.
        # Switching the same source to authoritative must still apply Base truth.
        authoritative = state.reconcile_source_records(
            "base:people", [changed], authoritative=True
        )
        person = state.tracker.list_people()[0]
        assert authoritative["counts"] == {"updated": 1}
        assert authoritative["scan_source_ids"] == []
        assert person["canonical_name"] == "Alice New"
        assert person["aliases"] == ["A. New"]
        assert person["profile"]["school"] == "New University"
        assert state.db.execute(
            "SELECT COUNT(*) FROM human_conflicts WHERE status='open'"
        ).fetchone()[0] == 0
        audit = state.db.execute(
            """SELECT detail_json FROM source_sync_audit
               WHERE outcome='authoritative_fields_updated'
               ORDER BY observed_at DESC LIMIT 1"""
        ).fetchone()
        changed_fields = {item["field"] for item in json.loads(audit[0])["changes"]}
        assert {"canonical_name", "aliases", "school"} <= changed_fields

        cleared = _canonical(
            _row(name="Alice New", aliases="", school="")
        )
        state.reconcile_source_records("base:people", [cleared], authoritative=True)
        person = state.tracker.list_people()[0]
        assert person["aliases"] == []
        assert "school" not in person["profile"]
        assert "affiliations" not in person["profile"]
    finally:
        state.close()


def test_explicit_removed_row_soft_retires_and_restores_idempotently(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        active = _canonical(_row())
        added = state.reconcile_source_records(
            "base:people", [active], authoritative=True
        )
        source_ids = added["scan_source_ids"]
        assert len(source_ids) == 2

        removed_record = _canonical(_row(status="已移除"))
        removed = state.reconcile_source_records(
            "base:people", [removed_record], authoritative=True
        )
        assert removed["counts"] == {"removed": 1}
        assert removed["scan_source_ids"] == []
        roster = state.source_roster_records("base:people", include_removed=True)[0]
        assert roster["status"] == "removed"
        assert state.db.execute(
            "SELECT COUNT(*) FROM sources WHERE tracking_enabled=1"
        ).fetchone()[0] == 0

        replay = state.reconcile_source_records(
            "base:people", [removed_record], authoritative=True
        )
        assert replay["counts"] == {"unchanged": 1}
        assert replay["scan_source_ids"] == []

        restored = state.reconcile_source_records(
            "base:people", [active], authoritative=True
        )
        assert restored["counts"] == {"restored": 1}
        assert restored["scan_source_ids"] == source_ids
        assert state.db.execute(
            "SELECT COUNT(*) FROM sources WHERE tracking_enabled=1"
        ).fetchone()[0] == 2

        stable = state.reconcile_source_records(
            "base:people", [active], authoritative=True
        )
        assert stable["counts"] == {"unchanged": 1}
        assert stable["scan_source_ids"] == []
    finally:
        state.close()


def test_only_new_or_changed_urls_are_selected_for_rescan(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        initial = _canonical(_row())
        added = state.reconcile_source_records("base:people", [initial])
        initial_ids = set(added["scan_source_ids"])
        assert len(initial_ids) == 2

        metadata_only = _canonical(
            _row(
                name="Alice Renamed",
                aliases="Alice R.",
                school="Another University",
            )
        )
        metadata_result = state.reconcile_source_records(
            "base:people", [metadata_only]
        )
        assert metadata_result["counts"] == {"updated": 1}
        assert metadata_result["scan_source_ids"] == []

        replaced = _canonical(
            _row(
                name="Alice Renamed",
                aliases="Alice R.",
                school="Another University",
                homepage="https://example.org/alice-new",
            )
        )
        replaced_result = state.reconcile_source_records("base:people", [replaced])
        new_homepage_id = state.db.execute(
            "SELECT source_id FROM sources WHERE url='https://example.org/alice-new'"
        ).fetchone()[0]
        github_id = state.db.execute(
            "SELECT source_id FROM sources WHERE kind='github'"
        ).fetchone()[0]
        assert replaced_result["scan_source_ids"] == [new_homepage_id]
        assert github_id not in replaced_result["scan_source_ids"]
        assert state.db.execute(
            "SELECT tracking_enabled FROM sources WHERE url='https://example.org/alice'"
        ).fetchone()[0] == 0
    finally:
        state.close()


def test_machine_review_rows_are_preserved_but_never_admitted_as_roster(
    tmp_path: Path,
) -> None:
    row = {
        "record_id": "rec-review",
        "fields": {
            "姓名": "Pending Candidate",
            "人员编号": "review:iss_123",
            "管理方式": "people-tracking-feishu",
            "审核决定": "暂不跟踪",
            "同步状态": "待审核",
        },
    }
    record = _canonical(row)
    assert record["record_type"] == "review"
    assert record["managed_by"] == "people-tracking-feishu"
    assert record["review_decision"] == "暂不跟踪"
    explicit = _canonical(
        {
            "record_id": "rec-explicit-review",
            "fields": {"姓名": "Candidate", "记录类型": "候选"},
        }
    )
    assert explicit["record_type"] == "review"

    state = PortableState(tmp_path / "state.sqlite3")
    try:
        first = state.reconcile_source_records(
            "base:people", [record], authoritative=True
        )
        second = state.reconcile_source_records(
            "base:people", [record], authoritative=True
        )
        assert first["counts"] == {"skipped_review": 1}
        assert second["counts"] == {"skipped_review": 1}
        assert first["scan_source_ids"] == []
        assert state.tracker.list_people() == []
        assert state.source_roster_records("base:people", include_removed=True) == []
        assert state.db.execute(
            "SELECT COUNT(*) FROM intake_issues WHERE status!='resolved'"
        ).fetchone()[0] == 0
    finally:
        state.close()


def test_readding_same_person_under_new_base_record_reenables_owned_sources(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "readd.sqlite3")
    try:
        original = _canonical(_row(record_id="rec-old"))
        first = state.reconcile_source_records(
            "base:people", [original], authoritative=True
        )
        source_ids = first["scan_source_ids"]
        assert source_ids

        removed = state.reconcile_source_records(
            "base:people", [], authoritative=True
        )
        assert removed["counts"] == {"removed": 1}
        assert state.db.execute(
            "SELECT COUNT(*) FROM sources WHERE tracking_enabled=1"
        ).fetchone()[0] == 0

        replacement = _canonical(_row(record_id="rec-new"))
        readded = state.reconcile_source_records(
            "base:people", [replacement], authoritative=True
        )
        assert readded["counts"] == {"added": 1}
        assert readded["scan_source_ids"] == source_ids
        assert state.db.execute(
            "SELECT COUNT(*) FROM sources WHERE tracking_enabled=1"
        ).fetchone()[0] == len(source_ids)
        roster = {
            item["source_record_id"]: item
            for item in state.source_roster_records(
                "base:people", include_removed=True
            )
        }
        assert roster["rec-old"]["status"] == "removed"
        assert set(roster["rec-new"]["owned_source_ids"]) == set(source_ids)
    finally:
        state.close()


def test_authoritative_person_key_reuses_identity_after_readd_and_rename(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "identity-readd.sqlite3")
    try:
        first = state.reconcile_source_records(
            "base:people", [_canonical(_row(record_id="rec-old"))], authoritative=True
        )
        person_key = first["records"][0]["person_key"]
        state.reconcile_source_records("base:people", [], authoritative=True)

        replacement_row = _row(
            record_id="rec-new",
            name="Alice Renamed",
            homepage="https://example.org/alice-new",
        )
        replacement_row["fields"]["人员编号"] = person_key
        replacement = state.reconcile_source_records(
            "base:people", [_canonical(replacement_row)], authoritative=True
        )

        assert replacement["records"][0]["person_key"] == person_key
        people = state.tracker.list_people()
        assert len(people) == 1
        assert people[0]["canonical_name"] == "Alice Renamed"
    finally:
        state.close()


def test_authoritative_duplicate_person_keys_are_quarantined_per_row(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "duplicate-key.sqlite3")
    try:
        first = _row(record_id="rec-a", name="Alice A")
        second = _row(record_id="rec-b", name="Alice B")
        first["fields"]["人员编号"] = "duplicate-key"
        second["fields"]["人员编号"] = "duplicate-key"
        result = state.reconcile_source_records(
            "base:people",
            [_canonical(first), _canonical(second)],
            authoritative=True,
        )
        assert result["counts"] == {"pending_review": 2}
        assert result["scan_source_ids"] == []
        assert state.tracker.list_people() == []
        issues = state.db.execute(
            "SELECT issue_type,status FROM intake_issues ORDER BY source_record_id"
        ).fetchall()
        assert [(row["issue_type"], row["status"]) for row in issues] == [
            ("duplicate_person_key", "open"),
            ("duplicate_person_key", "open"),
        ]
    finally:
        state.close()


def test_custom_authoritative_person_key_is_primary_and_duplicate_safe(
    tmp_path: Path,
) -> None:
    mapping = {
        **FIELD_MAPPING,
        "person_key": "自定义人员键",
        "secondary_id": "第二ID",
    }
    rows = [
        {
            "record_id": "rec-custom-a",
            "fields": {
                "姓名": "Custom A",
                "自定义人员键": "person_same",
                "第二ID": "secondary-a",
                "个人主页": "https://example.org/custom-a",
            },
        },
        {
            "record_id": "rec-custom-b",
            "fields": {
                "姓名": "Custom B",
                "自定义人员键": "person_same",
                "第二ID": "secondary-b",
                "个人主页": "https://example.org/custom-b",
            },
        },
    ]
    canonical = canonical_records(rows, mapping)
    assert [row["secondary_id"] for row in canonical] == [
        "person_same",
        "person_same",
    ]

    state = PortableState(tmp_path / "custom-person-key.sqlite3")
    try:
        result = state.reconcile_source_records(
            "base:custom-people", canonical, authoritative=True,
        )
        assert result["counts"] == {"pending_review": 2}
        assert state.tracker.list_people() == []
    finally:
        state.close()


def test_duplicate_key_keeps_existing_row_bound_and_quarantines_only_new_row(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "duplicate-existing.sqlite3")
    try:
        initial = state.reconcile_source_records(
            "base:people", [_canonical(_row(record_id="rec-a"))], authoritative=True
        )
        person_key = initial["records"][0]["person_key"]
        existing = _row(record_id="rec-a", name="Alice Existing")
        duplicate = _row(record_id="rec-b", name="Alice Duplicate")
        existing["fields"]["人员编号"] = person_key
        duplicate["fields"]["人员编号"] = person_key

        result = state.reconcile_source_records(
            "base:people",
            [_canonical(existing), _canonical(duplicate)],
            authoritative=True,
        )
        assert result["counts"] == {
            "review_annotation": 1,
            "pending_review": 1,
        }
        outcomes = {
            item["source_record_id"]: item for item in result["records"]
        }
        assert outcomes["rec-a"]["person_key"] == person_key
        assert outcomes["rec-b"]["person_key"] is None
        assert len(state.tracker.list_people()) == 1
    finally:
        state.close()


def test_legacy_in_place_review_row_resolves_when_user_adds_url(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "legacy-review.sqlite3")
    try:
        original = _canonical(
            {
                "record_id": "rec-pending",
                "fields": {"姓名": "Pending Person", "人员编号": "pending-id"},
            }
        )
        first = state.reconcile_source_records(
            "base:people", [original], authoritative=True
        )
        issue_id = first["records"][0]["issues"][0]["issue_id"]

        completed = _canonical(
            {
                "record_id": "rec-pending",
                "fields": {
                    "姓名": "Pending Person",
                    "人员编号": f"review:{issue_id}",
                    "记录类型": "审核项",
                    "管理方式": "people-tracking-feishu",
                    "个人主页": "https://example.org/pending-person",
                },
            }
        )
        resolved = state.reconcile_source_records(
            "base:people", [completed], authoritative=True
        )
        assert resolved["counts"] == {"added": 1}
        assert len(state.tracker.list_people()) == 1
        assert state.tracker.list_people()[0]["canonical_name"] == "Pending Person"
        assert state.db.execute(
            "SELECT status FROM intake_issues WHERE issue_id=?", (issue_id,)
        ).fetchone()[0] == "resolved"
        assert resolved["scan_source_ids"]
    finally:
        state.close()


def test_review_row_for_external_issue_can_become_person_in_authoritative_base(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "external-review.sqlite3")
    try:
        imported = state.import_people(
            [
                {
                    "canonical_name": "External Candidate",
                    "secondary_id": "external-1",
                    "aliases": [],
                    "urls": [],
                    "profile": {"school": "Example University"},
                    "source_record_id": "doc-row-1",
                }
            ],
            source_ref="weekly-doc",
        )
        issue_id = imported["issues"][0]["issue_id"]
        review_record = _canonical(
            {
                "record_id": "rec-review",
                "fields": {
                    "姓名": "External Candidate",
                    "人员编号": f"review:{issue_id}",
                    "记录类型": "审核项",
                    "管理方式": "people-tracking-feishu",
                    "个人主页": "https://example.org/external-candidate",
                },
            }
        )
        result = state.reconcile_source_records(
            "base:people", [review_record], authoritative=True
        )
        assert result["counts"] == {"added": 1}
        assert result["records"][0]["person_key"]
        assert state.db.execute(
            "SELECT status FROM intake_issues WHERE issue_id=?", (issue_id,)
        ).fetchone()[0] == "resolved"
    finally:
        state.close()


def test_review_decision_not_to_track_closes_issue_without_admitting_person(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "review-decision.sqlite3")
    try:
        imported = state.import_people(
            [
                {
                    "canonical_name": "Deferred Candidate",
                    "secondary_id": "deferred-1",
                    "aliases": [],
                    "urls": [],
                    "profile": {},
                    "source_record_id": "doc-row-2",
                }
            ],
            source_ref="weekly-doc",
        )
        issue_id = imported["issues"][0]["issue_id"]
        review_record = _canonical(
            {
                "record_id": "rec-review-defer",
                "fields": {
                    "姓名": "Deferred Candidate",
                    "人员编号": f"review:{issue_id}",
                    "记录类型": "审核项",
                    "管理方式": "people-tracking-feishu",
                    "审核决定": "暂不跟踪",
                },
            }
        )
        result = state.reconcile_source_records(
            "base:people", [review_record], authoritative=True
        )
        assert result["counts"] == {"skipped_review": 1}
        assert state.tracker.list_people() == []
        assert state.db.execute(
            "SELECT status FROM intake_issues WHERE issue_id=?", (issue_id,)
        ).fetchone()[0] == "resolved"
    finally:
        state.close()


def test_original_pending_person_row_consumes_not_to_track_decision_once(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "person-review-decision.sqlite3")
    try:
        pending = _canonical(
            {
                "record_id": "rec-original-pending",
                "fields": {
                    "姓名": "Original Pending",
                    "人员编号": "original-pending-id",
                    "记录类型": "人员",
                },
            }
        )
        first = state.reconcile_source_records(
            "base:people", [pending], authoritative=True
        )
        issue_id = first["records"][0]["issues"][0]["issue_id"]

        decided_row = {
            "record_id": "rec-original-pending",
            "fields": {
                "姓名": "Original Pending",
                "人员编号": "original-pending-id",
                "记录类型": "人员",
                "审核决定": "暂不跟踪",
            },
        }
        decided = _canonical(decided_row)
        result = state.reconcile_source_records(
            "base:people", [decided], authoritative=True
        )
        replay = state.reconcile_source_records(
            "base:people", [decided], authoritative=True
        )
        assert result["counts"] == {"skipped_review": 1}
        assert replay["counts"] == {"skipped_review": 1}
        assert state.tracker.list_people() == []
        assert state.db.execute(
            "SELECT status FROM intake_issues WHERE issue_id=?", (issue_id,)
        ).fetchone()[0] == "resolved"
        # The resolved decision is audited once; replaying an unchanged row
        # must not create an unbounded audit stream.
        assert state.db.execute(
            """SELECT COUNT(*) FROM source_sync_audit
               WHERE outcome='review_decision_applied'"""
        ).fetchone()[0] == 1
    finally:
        state.close()


def test_nested_source_primary_preference_is_persisted_and_switchable(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "primary-source.sqlite3")
    first_url = "https://example.org/profile-a"
    second_url = "https://example.org/profile-b"

    def record(primary_url: str) -> dict[str, object]:
        return {
            "canonical_name": "Primary Person",
            "secondary_id": "primary-person",
            "aliases": [],
            "urls": [first_url, second_url],
            "sources": [
                {
                    "kind": "homepage",
                    "url": first_url,
                    "lifecycle_status": "active",
                    "is_primary": primary_url == first_url,
                },
                {
                    "kind": "homepage",
                    "url": second_url,
                    "lifecycle_status": "active",
                    "is_primary": primary_url == second_url,
                },
            ],
            "profile": {},
            "source_record_id": "rec-primary",
            "record_type": "person",
            "lifecycle_status": "active",
        }

    try:
        state.reconcile_source_records(
            "base:people", [record(first_url)], authoritative=True
        )
        selected = state.db.execute(
            "SELECT url FROM sources WHERE is_primary=1"
        ).fetchall()
        assert [row["url"] for row in selected] == [first_url]

        state.reconcile_source_records(
            "base:people", [record(second_url)], authoritative=True
        )
        selected = state.db.execute(
            "SELECT url FROM sources WHERE is_primary=1"
        ).fetchall()
        assert [row["url"] for row in selected] == [second_url]
    finally:
        state.close()


def test_authoritative_same_value_confirms_and_closes_external_conflict(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "confirm-conflict.sqlite3")
    try:
        authoritative_record = _canonical(
            _row(name="Alice Confirmed", school="MIT")
        )
        state.reconcile_source_records(
            "base:people", [authoritative_record], authoritative=True
        )
        state.import_people(
            [
                {
                    **authoritative_record,
                    "profile": {
                        **authoritative_record["profile"],
                        "school": "Stanford",
                        "affiliations": ["Stanford"],
                    },
                }
            ],
            source_ref="external-roster",
            authoritative=False,
        )
        assert state.db.execute(
            "SELECT COUNT(*) FROM human_conflicts WHERE status='open'"
        ).fetchone()[0] > 0

        result = state.reconcile_source_records(
            "base:people", [authoritative_record], authoritative=True
        )
        assert result["scan_source_ids"] == []
        assert state.db.execute(
            "SELECT COUNT(*) FROM human_conflicts WHERE status='open'"
        ).fetchone()[0] == 0
        assert state.db.execute(
            """SELECT COUNT(*) FROM source_sync_audit
               WHERE outcome='authoritative_conflicts_confirmed'"""
        ).fetchone()[0] == 1
    finally:
        state.close()


def test_reconcile_rolls_back_person_and_sources_on_unexpected_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = PortableState(tmp_path / "atomic-reconcile.sqlite3")

    def fail_after_person(*_args, **_kwargs):
        raise RuntimeError("injected authoritative field failure")

    monkeypatch.setattr(state, "_apply_human_fields", fail_after_person)
    try:
        with pytest.raises(RuntimeError, match="injected authoritative"):
            state.reconcile_source_records(
                "base:people",
                [_canonical(_row())],
                authoritative=True,
            )
        assert state.db.in_transaction is False
        assert state.db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        assert state.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert state.db.execute(
            "SELECT COUNT(*) FROM source_roster_records"
        ).fetchone()[0] == 0
    finally:
        state.close()
