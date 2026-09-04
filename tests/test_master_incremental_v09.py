from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from people_tracking_feishu.ingest import canonical_records, reconcile_incremental_records
from people_tracking_feishu.master import (
    MANAGED_BY,
    PEOPLE_FIELD_SPECS,
    SOURCE_FIELD_SPECS,
    sync_visible_master,
    visible_records,
)
from people_tracking_feishu.state import PortableState


@dataclass
class _Result:
    payload: dict[str, Any]
    dry_run: bool

    def public(self) -> dict[str, Any]:
        return {"ok": True, "dry_run": self.dry_run, "payload": self.payload}


class _MemoryLark:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {"people": [], "sources": []}
        self.writes: list[dict[str, Any]] = []
        self._next = 1

    def list_base_records(self, *, table_id: str, **_: Any) -> list[dict[str, Any]]:
        return deepcopy(self.tables[table_id])

    def write(self, args: list[str], *, apply: bool):
        assert args[:2] == ["base", "+record-upsert"]
        table_id = args[args.index("--table-id") + 1]
        fields = json.loads(args[args.index("--json") + 1])
        record_id = (
            args[args.index("--record-id") + 1]
            if "--record-id" in args
            else f"rec_{self._next}"
        )
        preview = _Result({"record_id": record_id}, True)
        if not apply:
            return preview, None
        if "--record-id" not in args:
            self._next += 1
            self.tables[table_id].append({"record_id": record_id, "fields": {}})
        row = next(item for item in self.tables[table_id] if item["record_id"] == record_id)
        row["fields"].update(deepcopy(fields))
        self.writes.append({"table_id": table_id, "record_id": record_id, "fields": fields})
        return preview, _Result({"record_id": record_id}, False)


class _SchemaLark(_MemoryLark):
    def __init__(self, fields: list[dict[str, Any]]) -> None:
        super().__init__()
        self.fields = deepcopy(fields)
        self.field_creates: list[dict[str, Any]] = []

    def list_base_fields(self, **_: Any) -> list[dict[str, Any]]:
        return deepcopy(self.fields)

    def write(self, args: list[str], *, apply: bool):
        if args[:2] != ["base", "+field-create"]:
            return super().write(args, apply=apply)
        definition = json.loads(args[args.index("--json") + 1])
        preview = _Result({"field": definition}, True)
        if not apply:
            return preview, None
        self.fields.append(deepcopy(definition))
        self.field_creates.append(deepcopy(definition))
        return preview, _Result({"field_id": f"fld_{len(self.fields)}"}, False)


class _PerTableSchemaLark(_MemoryLark):
    def __init__(self, fields: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.fields = deepcopy(fields)
        self.field_creates: list[dict[str, Any]] = []

    def list_base_fields(self, *, table_id: str, **_: Any) -> list[dict[str, Any]]:
        return deepcopy(self.fields[table_id])

    def write(self, args: list[str], *, apply: bool):
        if args[:2] != ["base", "+field-create"]:
            return super().write(args, apply=apply)
        table_id = args[args.index("--table-id") + 1]
        definition = json.loads(args[args.index("--json") + 1])
        preview = _Result({"field": definition}, True)
        if not apply:
            return preview, None
        self.fields[table_id].append(deepcopy(definition))
        self.field_creates.append({"table_id": table_id, **deepcopy(definition)})
        return preview, _Result({"field_id": f"fld_{len(self.field_creates)}"}, False)


def _mono_record() -> dict[str, Any]:
    return {
        "registry_person_id": "mono_123",
        "tracking_person_key": "person_123",
        "canonical_name": "Mono Researcher",
        "aliases": ["研究员甲"],
        "cohorts": ["Mono 2026H1", "AI Lab"],
        "source_documents": ["roster-a.md"],
        "trackable_urls": [
            "https://example.org/mono-researcher",
            "https://scholar.google.com/citations?user=mono123",
        ],
        "profile": {
            "affiliations": ["Example University", "Example Lab"],
            "research_focuses": ["multimodal learning", "agents"],
            "stages_or_roles": ["Assistant Professor"],
            "employment_status_claims": ["active"],
            "identity_confidence": "stable_identity",
        },
        "admission_status": "active_tracking",
    }


def test_mono_plural_profile_is_preserved_with_legacy_scalars() -> None:
    record = canonical_records([_mono_record()], {"name": "姓名"})[0]

    assert record["canonical_name"] == "Mono Researcher"
    assert record["secondary_id"] == "person_123"
    assert record["source_record_id"] == "person_123"
    assert record["lifecycle_status"] == "active"
    assert record["profile"]["affiliations"] == ["Example University", "Example Lab"]
    assert record["profile"]["school"] == "Example University; Example Lab"
    assert record["profile"]["research_focuses"] == ["multimodal learning", "agents"]
    assert record["profile"]["stage"] == "Assistant Professor"
    assert record["profile"]["cohorts"] == ["Mono 2026H1", "AI Lab"]
    assert len(record["urls"]) == 2
    assert record["record_version_hash"] == canonical_records([_mono_record()], {})[0][
        "record_version_hash"
    ]


def test_incremental_reconcile_tombstones_once_and_restores_same_key() -> None:
    first = canonical_records([_mono_record()], {})[0]
    second_raw = {
        **_mono_record(),
        "tracking_person_key": "person_456",
        "registry_person_id": "mono_456",
        "canonical_name": "Second Researcher",
    }
    second = canonical_records([second_raw], {})[0]

    removed = reconcile_incremental_records([first, second], [first])
    assert removed["counts"] == {
        "added": 0,
        "updated": 0,
        "removed": 1,
        "restored": 0,
        "unchanged": 1,
    }
    assert removed["changes"][0]["source_record_id"] == "person_456"

    replay = reconcile_incremental_records(removed["records"], [first])
    assert replay["counts"]["removed"] == 0
    assert replay["changes"] == []

    restored = reconcile_incremental_records(replay["records"], [first, second])
    assert restored["counts"]["restored"] == 1
    assert restored["changes"][0]["source_record_id"] == "person_456"


def test_visible_master_uses_plural_fields_active_primary_and_same_table_review(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        person = state.tracker.add_person(
            "Mono Researcher",
            secondary_id=("source_table_id", "person_123"),
            aliases=["研究员甲"],
            urls=[
                "https://example.org/retired",
                "https://example.org/primary",
                "https://example.org/fallback",
            ],
            profile={
                "school": "Example University",
                "affiliations": ["Example University", "Example Lab"],
                "research_focuses": ["agents", "multimodal learning"],
                "stages_or_roles": ["Assistant Professor"],
                "employment_status_claims": ["在职"],
                "cohorts": ["Mono 2026H1"],
                "source_documents": ["roster.md"],
            },
        )
        by_url = {source["url"]: source["source_id"] for source in person["sources"]}
        state.db.execute(
            """UPDATE sources SET tracking_enabled=0,is_primary=1,binding_status='verified'
               WHERE source_id=?""",
            (by_url["https://example.org/retired"],),
        )
        state.db.execute(
            """UPDATE sources SET tracking_enabled=1,is_primary=1,binding_status='verified'
               WHERE source_id=?""",
            (by_url["https://example.org/primary"],),
        )
        state.db.execute(
            """UPDATE sources SET tracking_enabled=1,is_primary=0,binding_status='verified'
               WHERE source_id=?""",
            (by_url["https://example.org/fallback"],),
        )
        state.db.commit()
        state.import_people(
            [
                {
                    "canonical_name": "Mono Researcher",
                    "secondary_id": "person_123",
                    "aliases": [],
                    "urls": ["https://example.org/primary"],
                    "profile": {"school": "Different University"},
                    "source_record_id": "conflict-1",
                },
                {
                    "canonical_name": "Needs Homepage",
                    "secondary_id": "pending-1",
                    "aliases": [],
                    "urls": [],
                    "profile": {"affiliations": ["Pending University"]},
                    "source_record_id": "pending-1",
                },
            ],
            source_ref="weekly-roster",
        )

        records = visible_records(state)
        active = next(item for item in records["People"] if item["entity_key"] == person["person_key"])
        assert active["fields"]["School"] == "Example University"
        assert active["fields"]["Research Focus"] == "agents; multimodal learning"
        assert active["fields"]["Stage"] == "Assistant Professor"
        assert active["fields"]["Employment Status"] == "在职"
        assert active["fields"]["Homepage"] == "https://example.org/primary"
        assert active["fields"]["Review Status"] == "字段冲突待确认"

        retired = next(
            item for item in records["Sources"] if item["entity_key"] == by_url["https://example.org/retired"]
        )
        primary = next(
            item for item in records["Sources"] if item["entity_key"] == by_url["https://example.org/primary"]
        )
        assert retired["fields"]["Source Status"] == "已退役"
        assert retired["fields"]["Primary Source"] == "否"
        assert primary["fields"]["Primary Source"] == "是"

        review = next(item for item in records["People"] if item["entity_key"].startswith("review:"))
        assert review["fields"]["Name"] == "Needs Homepage"
        assert review["fields"]["Review Status"] == "待补主页"
        assert "审核决定" in review["fields"]["Review Detail"]
    finally:
        state.close()


def test_master_sync_is_incremental_preserves_human_fields_and_restores_record(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    lark = _MemoryLark()
    person = state.tracker.add_person(
        "System Name",
        secondary_id=("source_table_id", "stable-1"),
        urls=["https://example.org/stable"],
        profile={"school": "System University"},
    )
    state.db.execute(
        "UPDATE sources SET binding_status='verified',is_primary=1 WHERE person_key=?",
        (person["person_key"],),
    )
    state.db.commit()
    mapping = {
        "People": {
            "name": "姓名",
            "person_key": "人员编号",
            "record_type": "记录类型",
            "school": "机构",
            "tracking_status": "跟踪状态",
            "sync_status": "同步状态",
            "review_status": "审核状态",
            "review_detail": "审核说明",
            "review_decision": "审核决定",
            "managed_by": "维护者",
        },
        "Sources": {
            "source_key": "来源编号",
            "person_key": "人员编号",
            "person_link": "人员",
            "kind": "来源类型",
            "url": "链接",
            "tracking_enabled": "启用跟踪",
            "primary_source": "主来源",
            "source_status": "来源状态",
            "sync_status": "同步状态",
            "review_status": "审核状态",
            "review_detail": "审核说明",
            "review_decision": "审核决定",
            "managed_by": "维护者",
        },
    }
    available = {
        "People": [
            *mapping["People"].values(),
            *(spec["field_name"] for spec in PEOPLE_FIELD_SPECS.values()),
        ],
        "Sources": [
            *mapping["Sources"].values(),
            *(spec["field_name"] for spec in SOURCE_FIELD_SPECS.values()),
        ],
    }
    try:
        created = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id="sources",
            apply=True,
            field_mapping=mapping,
            available_fields=available,
        )
        assert created["write_counts"]["People"]["created"] == 1
        assert created["write_counts"]["Sources"]["created"] == 1
        people_row = lark.tables["people"][0]
        source_row = lark.tables["sources"][0]
        people_record_id = people_row["record_id"]
        source_record_id = source_row["record_id"]

        people_row["fields"].update(
            {"姓名": "Human Name", "机构": "Human University", "审核决定": "保留人工值"}
        )
        source_row["fields"].update({"链接": "https://human.example/source", "审核决定": "保留"})
        lark.writes.clear()
        reviewed = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id="sources",
            apply=True,
            field_mapping=mapping,
            available_fields=available,
        )
        assert people_row["fields"]["姓名"] == "Human Name"
        assert people_row["fields"]["机构"] == "Human University"
        assert people_row["fields"]["审核决定"] == "保留人工值"
        assert source_row["fields"]["链接"] == "https://human.example/source"
        assert source_row["fields"]["审核决定"] == "保留"
        assert any(item["human_fields_preserved"] for item in reviewed["outcomes"]["People"])

        lark.writes.clear()
        replay = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id="sources",
            apply=True,
            field_mapping=mapping,
            available_fields=available,
        )
        assert lark.writes == []
        assert replay["write_counts"]["People"]["unchanged"] == 1

        state.db.execute("DELETE FROM sources")
        state.db.execute("DELETE FROM people")
        state.db.commit()
        lark.writes.clear()
        removed = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id="sources",
            apply=True,
            field_mapping=mapping,
            available_fields=available,
        )
        assert removed["write_counts"]["People"]["tombstoned"] == 1
        assert removed["write_counts"]["Sources"]["tombstoned"] == 1
        assert people_row["fields"]["同步状态"] == "已移除"
        assert source_row["fields"]["来源状态"] == "已移除"

        restored_person = state.tracker.add_person(
            "System Name",
            secondary_id=("source_table_id", "stable-1"),
            urls=["https://example.org/stable"],
            profile={"school": "System University"},
        )
        state.db.execute(
            "UPDATE sources SET binding_status='verified',is_primary=1 WHERE person_key=?",
            (restored_person["person_key"],),
        )
        state.db.commit()
        lark.writes.clear()
        restored = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id="sources",
            apply=True,
            field_mapping=mapping,
            available_fields=available,
        )
        assert restored["write_counts"]["People"]["restored"] == 1
        assert restored["write_counts"]["Sources"]["restored"] == 1
        assert people_row["record_id"] == people_record_id
        assert source_row["record_id"] == source_record_id
        assert people_row["fields"]["同步状态"] == "有效"
        assert people_row["fields"]["姓名"] == "Human Name"
    finally:
        state.close()


def test_single_table_mode_keeps_pending_people_without_forcing_sources_table(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    lark = _MemoryLark()
    try:
        state.import_people(
            [
                {
                    "canonical_name": "Pending Person",
                    "secondary_id": "pending",
                    "aliases": [],
                    "urls": [],
                    "profile": {},
                    "source_record_id": "pending",
                }
            ],
            source_ref="weekly",
        )
        result = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            sources_table_id=None,
            apply=True,
            available_fields={
                "People": [spec["field_name"] for spec in PEOPLE_FIELD_SPECS.values()]
            },
        )
        assert result["single_table_mode"] is True
        assert result["record_counts"] == {"People": 1, "Sources": 0}
        assert lark.tables["sources"] == []
        assert lark.tables["people"][0]["fields"]["Review Status"] == "待补主页"
        assert lark.tables["people"][0]["fields"]["Managed By"] == MANAGED_BY
    finally:
        state.close()


def test_identity_conflict_review_keeps_candidate_context_visible(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "conflict-review.sqlite3")
    shared_url = "https://example.org/shared-profile"
    try:
        state.import_people(
            [
                {
                    "canonical_name": "Existing Person",
                    "secondary_id": "existing-1",
                    "aliases": [],
                    "urls": [shared_url],
                    "profile": {},
                    "source_record_id": "existing-row",
                }
            ],
            source_ref="source-a",
        )
        state.import_people(
            [
                {
                    "canonical_name": "Other Person",
                    "secondary_id": "other-1",
                    "aliases": [],
                    "urls": [shared_url],
                    "profile": {},
                    "source_record_id": "other-row",
                }
            ],
            source_ref="source-other",
        )
        state.import_people(
            [
                {
                    "canonical_name": "Conflicting Candidate",
                    "secondary_id": "candidate-1",
                    "aliases": ["Candidate Alias", "Existing Person", "Other Person"],
                    "urls": [shared_url],
                    "profile": {"school": "Candidate University"},
                    "source_record_id": "candidate-row",
                }
            ],
            source_ref="source-b",
        )

        review = next(
            item
            for item in visible_records(state)["People"]
            if item["entity_key"].startswith("review:")
        )
        assert "Candidate Alias" in review["fields"]["Aliases"]
        assert review["fields"]["School"] == "Candidate University"
        assert shared_url in review["fields"]["Review Detail"]
        assert "冲突原因" in review["fields"]["Review Detail"]
    finally:
        state.close()


def test_authoritative_pending_row_stays_ingestible_and_is_reused_after_url_added(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "pending-roundtrip.sqlite3")
    lark = _MemoryLark()
    lark.tables["people"] = [
        {
            "record_id": "rec_pending",
            "fields": {"Name": "Pending Person", "ID": "pending-1"},
        }
    ]
    field_mapping = {
        "People": {
            "name": "Name",
            "secondary_id": "ID",
            "person_key": "Person Key",
            "record_type": "Record Type",
            "homepage": "Homepage",
            "sync_status": "Sync Status",
            "tracking_status": "Tracking Status",
            "review_status": "Review Status",
            "review_detail": "Review Detail",
            "review_decision": "Review Decision",
            "managed_by": "Managed By",
        }
    }
    available = {
        "People": [
            "ID",
            *(spec["field_name"] for spec in PEOPLE_FIELD_SPECS.values()),
        ]
    }
    try:
        first_record = canonical_records(
            lark.tables["people"], field_mapping["People"]
        )[0]
        pending = state.reconcile_source_records(
            "base:people", [first_record], authoritative=True
        )
        assert pending["counts"] == {"pending_review": 1}
        issue = state.db.execute(
            "SELECT issue_id,status FROM intake_issues WHERE source_record_id='rec_pending'"
        ).fetchone()
        assert issue["status"] == "open"
        review_key = f"review:{issue['issue_id']}"
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key=review_key,
            base_token="base",
            table_id="people",
            record_id="rec_pending",
        )

        sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            apply=True,
            field_mapping=field_mapping,
            available_fields=available,
            authoritative_roster=True,
        )
        row = lark.tables["people"][0]
        assert row["fields"]["Record Type"] == "人员"
        assert "Person Key" not in row["fields"]
        assert state.db.execute(
            "SELECT status FROM intake_issues WHERE issue_id=?", (issue["issue_id"],)
        ).fetchone()[0] == "open"

        row["fields"]["Homepage"] = "https://example.org/pending-person"
        resolved_record = canonical_records(
            lark.tables["people"], field_mapping["People"]
        )[0]
        resolved = state.reconcile_source_records(
            "base:people", [resolved_record], authoritative=True
        )
        assert resolved["counts"] == {"added": 1}
        person_key = resolved["records"][0]["person_key"]
        state.set_feishu_record_link(
            entity_kind="person",
            entity_key=person_key,
            base_token="base",
            table_id="people",
            record_id="rec_pending",
        )
        assert state.feishu_record_link(
            entity_kind="person",
            entity_key=review_key,
            base_token="base",
            table_id="people",
        ) is None

        result = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            apply=True,
            field_mapping=field_mapping,
            available_fields=available,
            authoritative_roster=True,
        )
        assert result["write_counts"]["People"]["tombstoned"] == 0
        assert row["fields"]["Person Key"] == person_key
        assert row["fields"]["Record Type"] == "人员"
        assert row["fields"]["Sync Status"] != "已移除"
    finally:
        state.close()


def test_existing_base_schema_migration_is_additive_and_idempotent(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "schema.sqlite3")
    lark = _SchemaLark([{"field_name": "Name", "type": 1}])
    person = state.tracker.add_person(
        "Schema Person",
        urls=["https://example.org/schema-person"],
    )
    state.db.execute(
        "UPDATE sources SET binding_status='verified',is_primary=1 WHERE person_key=?",
        (person["person_key"],),
    )
    state.db.commit()
    mapping = {
        "People": {
            "name": "姓名",
            "person_key": "人员编号",
            "record_type": "记录类型",
            "aliases": "别名",
            "school": "学校",
            "research_focus": "专业/研究方向",
            "stage": "阶段/年龄",
            "homepage": "个人主页",
            "sync_status": "同步状态",
            "tracking_status": "跟踪状态",
            "review_status": "审核状态",
            "review_detail": "待确认信息",
            "review_decision": "审核决定",
            "managed_by": "管理方式",
        }
    }
    try:
        first = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            apply=True,
            field_mapping=mapping,
        )
        created_names = {item["field_name"] for item in lark.field_creates}
        assert {"人员编号", "记录类型", "同步状态", "审核决定", "管理方式"} <= created_names
        assert not ({"姓名", "别名", "学校", "专业/研究方向", "阶段/年龄"} & created_names)
        assert first["schema_changes"]["People"]
        assert lark.tables["people"][0]["fields"]["Name"] == "Schema Person"
        assert lark.tables["people"][0]["fields"]["人员编号"] == person["person_key"]

        lark.writes.clear()
        lark.field_creates.clear()
        replay = sync_visible_master(
            state,
            lark,
            base_token="base",
            people_table_id="people",
            apply=True,
            field_mapping=mapping,
        )
        assert lark.field_creates == []
        assert lark.writes == []
        assert replay["schema_changes"]["People"] == []
    finally:
        state.close()


def test_existing_base_rejects_incompatible_stable_key_type(tmp_path: Path) -> None:
    state = PortableState(tmp_path / "wrong-type.sqlite3")
    lark = _SchemaLark(
        [
            {"field_name": "Name", "type": 1},
            {"field_name": "人员编号", "type": 15},
        ]
    )
    state.tracker.add_person(
        "Wrong Type",
        urls=["https://example.org/wrong-type"],
    )
    try:
        try:
            sync_visible_master(
                state,
                lark,
                base_token="base",
                people_table_id="people",
                apply=True,
                field_mapping={"People": {"name": "Name", "person_key": "人员编号"}},
            )
        except ValueError as exc:
            assert "stable Person Key" in str(exc)
        else:
            raise AssertionError("incompatible stable key type must fail closed")
        assert lark.tables["people"] == []
    finally:
        state.close()


def test_existing_base_rejects_duplicate_stable_keys_before_any_write(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "duplicate-stable-key.sqlite3")
    lark = _SchemaLark(
        [
            {"field_name": "Name", "type": 1},
            {"field_name": "Person Key", "type": 1},
        ]
    )
    lark.tables["people"] = [
        {
            "record_id": "rec_duplicate_a",
            "fields": {"Name": "First Copy", "Person Key": "person_duplicate"},
        },
        {
            "record_id": "rec_duplicate_b",
            "fields": {"Name": "Second Copy", "Person Key": "person_duplicate"},
        },
    ]
    state.tracker.add_person(
        "Duplicate Key Person", urls=["https://example.org/duplicate-key"]
    )
    try:
        try:
            sync_visible_master(
                state,
                lark,
                base_token="base",
                people_table_id="people",
                apply=True,
            )
        except ValueError as exc:
            assert "duplicate stable keys" in str(exc)
            assert "person_duplicate" in str(exc)
        else:
            raise AssertionError("duplicate stable keys must fail closed")
        assert lark.field_creates == []
        assert lark.writes == []
    finally:
        state.close()


def test_all_table_schemas_are_preflighted_before_any_field_is_created(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "schema-preflight.sqlite3")
    lark = _PerTableSchemaLark(
        {
            "people": [{"field_name": "Name", "type": 1}],
            # A Sources table without its human-owned URL column is unsafe.
            "sources": [{"field_name": "Source Key", "type": 1}],
        }
    )
    state.tracker.add_person(
        "Preflight Person", urls=["https://example.org/preflight"]
    )
    try:
        try:
            sync_visible_master(
                state,
                lark,
                base_token="base",
                people_table_id="people",
                sources_table_id="sources",
                apply=True,
            )
        except ValueError as exc:
            assert "human-owned URL" in str(exc)
        else:
            raise AssertionError("missing Sources URL must fail before migration")
        assert lark.field_creates == []
        assert lark.writes == []
    finally:
        state.close()
