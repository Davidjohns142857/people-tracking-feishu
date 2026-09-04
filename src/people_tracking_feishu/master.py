from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable

from .lark import LarkCli, first_value
from .state import PortableState


MANAGED_BY = "people-tracking-feishu"


PEOPLE_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "name": {"field_name": "Name", "type": 1, "owner": "human", "aliases": ("姓名",)},
    "person_key": {
        "field_name": "Person Key",
        "type": 1,
        "owner": "machine",
        "aliases": ("人员编号", "人员 Key"),
    },
    "record_type": {
        "field_name": "Record Type",
        "type": 1,
        "owner": "machine",
        "aliases": ("记录类型",),
    },
    "aliases": {"field_name": "Aliases", "type": 1, "owner": "human", "aliases": ("别名",)},
    "school": {
        "field_name": "School",
        "type": 1,
        "owner": "human",
        "aliases": ("学校", "机构", "学校/机构"),
    },
    "research_focus": {
        "field_name": "Research Focus",
        "type": 1,
        "owner": "human",
        "aliases": ("研究方向", "专业/研究方向"),
    },
    "stage": {
        "field_name": "Stage",
        "type": 1,
        "owner": "human",
        "aliases": ("阶段", "阶段/年龄", "当前角色"),
    },
    "employment_status": {
        "field_name": "Employment Status",
        "type": 1,
        "owner": "human",
        "aliases": ("任职状态",),
    },
    "cohorts": {
        "field_name": "Cohorts",
        "type": 1,
        "owner": "human",
        "aliases": ("名单归属", "批次"),
    },
    "source_documents": {
        "field_name": "Source Documents",
        "type": 1,
        "owner": "machine",
        "aliases": ("来源文档",),
    },
    "identity_confidence": {
        "field_name": "Identity Confidence",
        "type": 1,
        "owner": "machine",
        "aliases": ("身份置信度",),
    },
    "tracking_status": {
        "field_name": "Tracking Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("跟踪状态",),
    },
    "homepage": {"field_name": "Homepage", "type": 15, "owner": "human", "aliases": ("个人主页",)},
    "scholar": {
        "field_name": "Google Scholar",
        "type": 15,
        "owner": "human",
        "aliases": ("Scholar",),
    },
    "github": {"field_name": "GitHub", "type": 15, "owner": "human", "aliases": ()},
    "linkedin": {"field_name": "LinkedIn", "type": 15, "owner": "human", "aliases": ()},
    "sync_status": {
        "field_name": "Sync Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("同步状态", "记录状态"),
    },
    "review_status": {
        "field_name": "Review Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("审核状态",),
    },
    "review_detail": {
        "field_name": "Review Detail",
        "type": 1,
        "owner": "machine",
        "aliases": ("审核说明", "待确认内容"),
    },
    "review_decision": {
        "field_name": "Review Decision",
        "type": 1,
        "owner": "human",
        "aliases": ("审核决定", "人工决定"),
    },
    "managed_by": {
        "field_name": "Managed By",
        "type": 1,
        "owner": "machine",
        "aliases": ("维护者",),
    },
}


SOURCE_FIELD_SPECS: dict[str, dict[str, Any]] = {
    "source_key": {
        "field_name": "Source Key",
        "type": 1,
        "owner": "machine",
        "aliases": ("来源编号", "来源 Key"),
    },
    "person_key": {
        "field_name": "Person Key",
        "type": 1,
        "owner": "machine",
        "aliases": ("人员编号", "人员 Key"),
    },
    "person_link": {
        "field_name": "Person",
        "type": 21,
        "owner": "machine",
        "aliases": ("人员",),
    },
    "kind": {"field_name": "Kind", "type": 1, "owner": "machine", "aliases": ("来源类型",)},
    # URL is deliberately human-owned in the visible Base. A canonical URL
    # replacement creates/restores a source row; it never silently rewrites a
    # URL that a user may be reviewing in place.
    "url": {"field_name": "URL", "type": 15, "owner": "human", "aliases": ("链接", "来源链接")},
    "tracking_enabled": {
        "field_name": "Tracking Enabled",
        "type": 1,
        "owner": "machine",
        "aliases": ("启用跟踪",),
    },
    "primary_source": {
        "field_name": "Primary Source",
        "type": 1,
        "owner": "machine",
        "aliases": ("主来源",),
    },
    "source_status": {
        "field_name": "Source Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("来源状态",),
    },
    "health": {"field_name": "Health", "type": 1, "owner": "machine", "aliases": ("健康状态",)},
    "last_checked": {
        "field_name": "Last Checked",
        "type": 1,
        "owner": "machine",
        "aliases": ("最近检查",),
    },
    "last_success": {
        "field_name": "Last Success",
        "type": 1,
        "owner": "machine",
        "aliases": ("最近成功",),
    },
    "next_check": {
        "field_name": "Next Check",
        "type": 1,
        "owner": "machine",
        "aliases": ("下次检查",),
    },
    "last_change_summary": {
        "field_name": "Last Change Summary",
        "type": 1,
        "owner": "machine",
        "aliases": ("最近变化", "最近重要变化"),
    },
    "binding_status": {
        "field_name": "Binding Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("身份绑定",),
    },
    "sync_status": {
        "field_name": "Sync Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("同步状态", "记录状态"),
    },
    "review_status": {
        "field_name": "Review Status",
        "type": 1,
        "owner": "machine",
        "aliases": ("审核状态",),
    },
    "review_detail": {
        "field_name": "Review Detail",
        "type": 1,
        "owner": "machine",
        "aliases": ("审核说明", "待确认内容"),
    },
    "review_decision": {
        "field_name": "Review Decision",
        "type": 1,
        "owner": "human",
        "aliases": ("审核决定", "人工决定"),
    },
    "managed_by": {
        "field_name": "Managed By",
        "type": 1,
        "owner": "machine",
        "aliases": ("维护者",),
    },
}


# Names used only when this package creates a new visible Base.  Internal
# semantic keys remain stable and existing tables continue to use their live
# names/aliases, while a newly created table is immediately readable by a
# non-technical Chinese-speaking operator.
PEOPLE_STANDARD_FIELD_NAMES = {
    "name": "姓名",
    "person_key": "人员编号",
    "record_type": "记录类型",
    "aliases": "别名",
    "school": "学校/机构",
    "research_focus": "专业/研究方向",
    "stage": "阶段/年龄",
    "employment_status": "任职状态",
    "cohorts": "名单归属",
    "source_documents": "来源文档",
    "identity_confidence": "身份置信度",
    "tracking_status": "跟踪状态",
    "homepage": "个人主页",
    "scholar": "Google Scholar",
    "github": "GitHub",
    "linkedin": "LinkedIn",
    "sync_status": "同步状态",
    "review_status": "审核状态",
    "review_detail": "待确认信息",
    "review_decision": "审核决定",
    "managed_by": "管理方式",
}

SOURCE_STANDARD_FIELD_NAMES = {
    "source_key": "来源编号",
    "person_key": "人员编号",
    "person_link": "人员",
    "kind": "来源类型",
    "url": "来源链接",
    "tracking_enabled": "启用跟踪",
    "primary_source": "主来源",
    "source_status": "来源状态",
    "health": "健康状态",
    "last_checked": "最近检查",
    "last_success": "最近成功",
    "next_check": "下次检查",
    "last_change_summary": "最近重要变化",
    "binding_status": "身份绑定",
    "sync_status": "同步状态",
    "review_status": "审核状态",
    "review_detail": "待确认信息",
    "review_decision": "审核决定",
    "managed_by": "管理方式",
}


def _standard_field_name(table: str, semantic: str, spec: dict[str, Any]) -> str:
    names = (
        PEOPLE_STANDARD_FIELD_NAMES
        if table == "People"
        else SOURCE_STANDARD_FIELD_NAMES
    )
    return names.get(semantic, str(spec["field_name"]))


PEOPLE_FIELDS = [
    {
        "field_name": _standard_field_name("People", semantic, spec),
        "type": spec["type"],
    }
    for semantic, spec in PEOPLE_FIELD_SPECS.items()
]


def master_field_ownership() -> dict[str, dict[str, list[str]]]:
    return {
        table: {
            owner: [
                _standard_field_name(table, semantic, spec)
                for semantic, spec in specs.items()
                if spec["owner"] == owner
            ]
            for owner in ("human", "machine")
        }
        for table, specs in (("People", PEOPLE_FIELD_SPECS), ("Sources", SOURCE_FIELD_SPECS))
    }


def master_schema(people_table_id: str | None = None) -> dict[str, Any]:
    source_fields: list[dict[str, Any]] = []
    for key, spec in SOURCE_FIELD_SPECS.items():
        if key == "person_link" and not people_table_id:
            continue
        field = {
            "field_name": _standard_field_name("Sources", key, spec),
            "type": spec["type"],
        }
        if key == "person_link":
            field["property"] = {"table_id": people_table_id}
        source_fields.append(field)
    return {"People": PEOPLE_FIELDS, "Sources": source_fields}


def create_master_base(
    lark: LarkCli,
    *,
    timezone_name: str,
    folder_token: str | None,
    apply: bool,
) -> dict[str, Any]:
    args = [
        "base", "+base-create", "--name", "People Tracking Master",
        "--time-zone", timezone_name, "--as", "bot",
    ]
    if folder_token:
        args.extend(["--folder-token", folder_token])
    preview, actual = lark.write(args, apply=apply)
    result: dict[str, Any] = {
        "apply": apply,
        "preview": preview.public(),
        "schema": master_schema(),
        "field_ownership": master_field_ownership(),
    }
    if not actual:
        return result
    base_token = first_value(actual.payload, "base_token", "app_token", "token")
    if not base_token:
        raise RuntimeError("master Base create response did not include a token")
    table_ids: dict[str, str] = {}
    people_preview, people_actual = lark.write(
        [
            "base", "+table-create", "--base-token", str(base_token), "--name", "People",
            "--fields", json.dumps(PEOPLE_FIELDS, ensure_ascii=False, separators=(",", ":")),
            "--as", "bot",
        ],
        apply=True,
    )
    people_id = first_value(people_actual.payload if people_actual else {}, "table_id", "id")
    if not people_id:
        raise RuntimeError("People table create response did not include table_id")
    table_ids["People"] = str(people_id)
    sources_fields = master_schema(str(people_id))["Sources"]
    sources_preview, sources_actual = lark.write(
        [
            "base", "+table-create", "--base-token", str(base_token), "--name", "Sources",
            "--fields", json.dumps(sources_fields, ensure_ascii=False, separators=(",", ":")),
            "--as", "bot",
        ],
        apply=True,
    )
    sources_id = first_value(sources_actual.payload if sources_actual else {}, "table_id", "id")
    if not sources_id:
        raise RuntimeError("Sources table create response did not include table_id")
    table_ids["Sources"] = str(sources_id)
    result.update(
        {
            "base_token": str(base_token),
            "base_url": first_value(actual.payload, "url", "base_url"),
            "table_ids": table_ids,
            "table_previews": [people_preview.public(), sources_preview.public()],
        }
    )
    return result


def _as_values(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_as_values(item))
        return list(dict.fromkeys(output))
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link", "url"):
            if value.get(key) not in (None, ""):
                return _as_values(value[key])
        return []
    return [item.strip() for item in str(value).replace("；", ";").split(";") if item.strip()]


def _profile_text(profile: dict[str, Any], scalar: str, plural: str) -> str:
    values = _as_values(profile.get(scalar)) or _as_values(profile.get(plural))
    return "; ".join(values)


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _display(value: Any, *, limit: int = 240) -> str:
    decoded = _json_value(value)
    values = _as_values(decoded)
    text = "; ".join(values) if values else str(decoded or "未填写")
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_change_summaries(state: PortableState) -> dict[str, str]:
    summaries: dict[str, str] = {}
    try:
        rows = state.db.execute(
            """SELECT source_id,summary FROM observations
               WHERE decision_status='changed'
               ORDER BY observed_at DESC,observation_id DESC"""
        )
        for row in rows:
            summaries.setdefault(str(row["source_id"]), str(row["summary"] or ""))
    except Exception:
        return {}
    return summaries


def _open_conflicts(state: PortableState) -> dict[str, list[str]]:
    conflicts: dict[str, list[str]] = defaultdict(list)
    try:
        rows = state.db.execute(
            """SELECT person_key,field_name,current_value_json,incoming_value_json
               FROM human_conflicts WHERE status='open'
               ORDER BY person_key,field_name,created_at"""
        )
        for row in rows:
            message = (
                f"{row['field_name']}：当前“{_display(row['current_value_json'])}”，"
                f"待选“{_display(row['incoming_value_json'])}”"
            )
            if message not in conflicts[str(row["person_key"] or "")]:
                conflicts[str(row["person_key"] or "")].append(message)
    except Exception:
        return {}
    return conflicts


def _roster_presence(state: PortableState) -> dict[str, tuple[int, int]]:
    """Return active/removed roster memberships without making legacy DBs mandatory."""

    try:
        rows = state.db.execute(
            """SELECT person_key,
                      SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_count,
                      SUM(CASE WHEN status='removed' THEN 1 ELSE 0 END) AS removed_count
               FROM source_roster_records GROUP BY person_key"""
        )
    except Exception:
        return {}
    return {
        str(row["person_key"]): (int(row["active_count"] or 0), int(row["removed_count"] or 0))
        for row in rows
    }


def _source_priority(source: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(bool(source.get("is_primary"))),
        int(source.get("binding_status") == "verified"),
        int(source.get("health_status") == "healthy"),
        str(source.get("last_success_at") or ""),
        str(source.get("last_checked_at") or ""),
        str(source.get("source_id") or ""),
    )


def _health_label(value: Any) -> str:
    return {
        "healthy": "正常",
        "never_checked": "尚未检查",
        "blocked": "公开访问受限",
        "gone": "页面失效",
        "transport_error": "访问失败",
        "temporary_error": "临时访问失败（等待自动重试）",
        "rate_limited": "访问频率受限（等待自动重试）",
        "degraded": "页面解析待修复",
    }.get(str(value or ""), str(value or "未知"))


def _kind_label(value: Any) -> str:
    return {
        "homepage": "个人主页",
        "scholar": "Google Scholar",
        "github": "GitHub",
        "linkedin": "LinkedIn",
    }.get(str(value or ""), str(value or "未知来源"))


def _binding_label(value: Any) -> str:
    return {
        "verified": "已确认",
        "unverified": "尚未确认",
        "review": "待人工确认",
        "conflict": "身份冲突",
    }.get(str(value or ""), str(value or "尚未确认"))


def _identity_confidence_label(value: Any) -> str:
    return {
        "stable_identity": "身份稳定",
        "verified": "已确认",
        "high": "高",
        "medium": "中",
        "low": "低",
        "pending": "待确认",
    }.get(str(value or ""), str(value or ""))


def _source_status(source: dict[str, Any]) -> str:
    if int(source.get("tracking_enabled", 1)) == 1:
        return "跟踪中"
    if source.get("curation_status") == "replaced":
        return "已替换"
    return "已退役"


def _record_item(
    table: str,
    entity_key: str,
    values: dict[str, Any],
    *,
    person_key: str | None = None,
) -> dict[str, Any]:
    specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
    fields = {
        specs[key]["field_name"]: value
        for key, value in values.items()
        if key in specs and key != "person_link"
    }
    return {
        "entity_key": entity_key,
        "fields": fields,
        "field_values": values,
        "field_ownership": {
            "human": [key for key in values if key in specs and specs[key]["owner"] == "human"],
            "machine": [key for key in values if key in specs and specs[key]["owner"] == "machine"],
        },
        **({"person_key": person_key} if person_key else {}),
    }


def _issue_presentation(issue_type: str) -> tuple[str, str]:
    label = {
        "missing_required_profile": "待补主页",
        "missing_name": "待补姓名",
        "identity_or_source_conflict": "身份冲突待确认",
        "duplicate_person_key": "人员编号重复待确认",
        "multiple_primary_sources": "主来源重复待确认",
    }.get(issue_type, "待人工确认")
    explanation = {
        "missing_required_profile": (
            "缺少可跟踪主页；请在个人主页、Google Scholar、GitHub 或 LinkedIn 列"
            "补充可靠链接；若无需跟踪，请在“审核决定”填写“暂不跟踪”。"
        ),
        "missing_name": "缺少姓名；请补全后再纳入跟踪。",
        "identity_or_source_conflict": (
            "身份或来源与现有记录冲突；请核对并直接修正人员编号或主页字段。"
        ),
        "duplicate_person_key": (
            "同一个人员编号出现在多行；请保留正确编号或为不同人员填写不同编号。"
        ),
        "multiple_primary_sources": (
            "同一来源类型标记了多个主来源；请只保留一个主来源。"
        ),
    }.get(issue_type, "请核对该记录并填写“审核决定”。")
    return label, explanation


def _intake_review_notes(state: PortableState) -> dict[str, list[str]]:
    notes: dict[str, list[str]] = defaultdict(list)
    try:
        rows = state.db.execute(
            """SELECT i.issue_type,r.person_key
               FROM intake_issues i
               JOIN source_roster_records r
                 ON r.source_ref=i.source_ref
                AND r.source_record_id=i.source_record_id
               WHERE i.status!='resolved'
               ORDER BY i.created_at,i.issue_id"""
        ).fetchall()
    except Exception:
        return notes
    for row in rows:
        label, explanation = _issue_presentation(str(row["issue_type"]))
        note = f"{label}：{explanation}"
        if note not in notes[str(row["person_key"])]:
            notes[str(row["person_key"])].append(note)
    return notes


def _pending_people(state: PortableState) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    try:
        rows = state.db.execute(
            """SELECT i.issue_id,i.source_ref,i.source_record_id,i.display_name,
                      i.issue_type,i.detail_json,i.status
               FROM intake_issues i
               WHERE i.status!='resolved'
                 AND NOT EXISTS (
                   SELECT 1 FROM source_roster_records r
                   WHERE r.source_ref=i.source_ref
                     AND r.source_record_id=i.source_record_id
                 )
               ORDER BY i.created_at,i.issue_id"""
        )
    except Exception:
        return output
    for row in rows:
        detail = _json_value(row["detail_json"])
        detail = detail if isinstance(detail, dict) else {}
        candidate = (
            detail.get("record")
            if isinstance(detail.get("record"), dict)
            else detail
        )
        profile = (
            candidate.get("profile")
            if isinstance(candidate.get("profile"), dict)
            else {}
        )
        review_key = f"review:{row['issue_id']}"
        issue_label, explanation = _issue_presentation(str(row["issue_type"]))
        diagnostic_parts = [explanation]
        candidate_urls = _as_values(candidate.get("urls"))
        if candidate_urls:
            diagnostic_parts.append("候选链接：" + "；".join(candidate_urls[:8]))
        if detail.get("error"):
            diagnostic_parts.append("冲突原因：" + _display(detail["error"], limit=500))
        if detail.get("duplicate_identity"):
            diagnostic_parts.append(
                "重复人员编号：" + _display(detail["duplicate_identity"], limit=200)
            )
        explanation = "；".join(diagnostic_parts)[:1800]
        values = {
            "name": str(
                row["display_name"]
                or candidate.get("canonical_name")
                or "待确认人物"
            ),
            "person_key": review_key,
            "record_type": "审核项",
            "aliases": "; ".join(_as_values(candidate.get("aliases"))),
            "school": _profile_text(profile, "school", "affiliations"),
            "research_focus": _profile_text(profile, "research_focus", "research_focuses"),
            "stage": _profile_text(profile, "stage", "stages_or_roles"),
            "employment_status": _profile_text(
                profile, "employment_status", "employment_status_claims"
            ),
            "cohorts": _profile_text(profile, "cohort", "cohorts"),
            "source_documents": _profile_text(profile, "source_document", "source_documents"),
            "identity_confidence": str(profile.get("identity_confidence") or "待确认"),
            "tracking_status": "未跟踪",
            "homepage": "",
            "scholar": "",
            "github": "",
            "linkedin": "",
            "sync_status": "待审核",
            "review_status": issue_label,
            "review_detail": explanation,
            "managed_by": MANAGED_BY,
        }
        item = _record_item("People", review_key, values)
        # Preserve the origin so an authoritative Base row that merely lacks an
        # anchor can be annotated in place without being converted into a
        # machine-created review row.  Review rows created for other sources do
        # not have a matching Feishu record_id in the destination Base.
        item["origin_source_ref"] = str(row["source_ref"] or "")
        item["origin_record_id"] = str(row["source_record_id"] or "")
        output.append(item)
    return output


def visible_records(state: PortableState) -> dict[str, list[dict[str, Any]]]:
    people_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    conflicts = _open_conflicts(state)
    intake_review_notes = _intake_review_notes(state)
    roster_presence = _roster_presence(state)
    change_summaries = _latest_change_summaries(state)
    for person in state.tracker.list_people():
        active_rosters, removed_rosters = roster_presence.get(str(person["person_key"]), (0, 0))
        roster_removed = removed_rosters > 0 and active_rosters == 0
        sources = list(person["sources"])
        active = [source for source in sources if int(source.get("tracking_enabled", 1)) == 1]
        selected: dict[str, dict[str, Any]] = {}
        for source in active:
            kind = str(source["kind"])
            if kind not in selected or _source_priority(source) > _source_priority(selected[kind]):
                selected[kind] = source
        primary_ids = {str(source["source_id"]) for source in selected.values()}
        source_review_notes: list[str] = []
        for source in sorted(sources, key=lambda item: (str(item["kind"]), str(item["source_id"]))):
            binding_status = str(source.get("binding_status") or "unverified")
            review_status = "无需处理"
            review_detail = ""
            if int(source.get("tracking_enabled", 1)) == 1 and binding_status != "verified":
                review_status = "来源身份待确认"
                review_detail = str(source.get("binding_reason") or "请核对该主页是否属于此人。")
                source_review_notes.append(f"{source['kind']}：{review_detail}")
            elif str(source.get("curation_status") or "") == "pending":
                review_status = "来源待确认"
                review_detail = str(source.get("curation_note") or "请确认是否继续跟踪该来源。")
                source_review_notes.append(f"{source['kind']}：{review_detail}")
            values = {
                "source_key": source["source_id"],
                "person_key": person["person_key"],
                "kind": _kind_label(source["kind"]),
                "url": source["url"],
                "tracking_enabled": "是" if int(source.get("tracking_enabled", 1)) == 1 else "否",
                "primary_source": "是" if str(source["source_id"]) in primary_ids else "否",
                "source_status": _source_status(source),
                "health": _health_label(source.get("health_status")),
                "last_checked": source.get("last_checked_at") or "",
                "last_success": source.get("last_success_at") or "",
                "next_check": source.get("next_check_at") or "",
                "last_change_summary": change_summaries.get(str(source["source_id"]), ""),
                "binding_status": _binding_label(binding_status),
                "sync_status": "已移除" if roster_removed else "有效",
                "review_status": review_status,
                "review_detail": review_detail,
                "managed_by": MANAGED_BY,
            }
            source_rows.append(
                _record_item("Sources", str(source["source_id"]), values, person_key=str(person["person_key"]))
            )
        profile = person["profile"]
        field_review_notes = list(conflicts.get(str(person["person_key"]), []))
        roster_review_notes = list(
            intake_review_notes.get(str(person["person_key"]), [])
        )
        review_notes = field_review_notes + roster_review_notes + source_review_notes
        values = {
            "name": person["canonical_name"],
            "person_key": person["person_key"],
            "record_type": "人员",
            "aliases": "; ".join(person["aliases"]),
            "school": _profile_text(profile, "school", "affiliations"),
            "research_focus": _profile_text(profile, "research_focus", "research_focuses"),
            "stage": _profile_text(profile, "stage", "stages_or_roles"),
            "employment_status": _profile_text(
                profile, "employment_status", "employment_status_claims"
            ),
            "cohorts": _profile_text(profile, "cohort", "cohorts"),
            "source_documents": _profile_text(profile, "source_document", "source_documents"),
            "identity_confidence": _identity_confidence_label(
                profile.get("identity_confidence")
            ),
            "tracking_status": "已移除" if roster_removed else "跟踪中" if active else "已暂停",
            "homepage": selected.get("homepage", {}).get("url", ""),
            "scholar": selected.get("scholar", {}).get("url", ""),
            "github": selected.get("github", {}).get("url", ""),
            "linkedin": selected.get("linkedin", {}).get("url", ""),
            "sync_status": "已移除" if roster_removed else "有效",
            "review_status": (
                "字段冲突待确认"
                if field_review_notes
                else "名单信息待确认"
                if roster_review_notes
                else "来源待确认"
                if source_review_notes
                else "历史记录"
                if roster_removed
                else "无需处理"
            ),
            "review_detail": "；".join(dict.fromkeys(review_notes))[:1800],
            "managed_by": MANAGED_BY,
        }
        people_rows.append(_record_item("People", str(person["person_key"]), values))
    people_rows.extend(_pending_people(state))
    people_rows.sort(key=lambda item: item["entity_key"])
    source_rows.sort(key=lambda item: item["entity_key"])
    return {"People": people_rows, "Sources": source_rows}


def _record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else record


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("record_id") or record.get("id") or record.get("row_id") or "")


def _field_names(values: Iterable[Any]) -> set[str]:
    output: set[str] = set()
    for value in values:
        if isinstance(value, str):
            output.add(value)
        elif isinstance(value, dict):
            name = value.get("field_name") or value.get("name")
            if name:
                output.add(str(name))
    return output


def _field_descriptors(values: Iterable[Any]) -> dict[str, dict[str, Any]]:
    descriptors: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        name = value.get("field_name") or value.get("name")
        if name:
            descriptors[_physical_field_identity(str(name))] = dict(value)
    return descriptors


def _field_type(value: dict[str, Any] | None) -> int | None:
    if not value:
        return None
    raw = value.get("type")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _compatible_field_type(semantic: str, expected: int, actual: int) -> bool:
    if actual == expected:
        return True
    if expected == 15 and actual == 1:
        # Existing rosters frequently keep URLs in ordinary text cells.
        return True
    if expected == 1 and actual == 3 and semantic not in {
        "name",
        "person_key",
        "source_key",
        "aliases",
        "school",
        "research_focus",
        "stage",
        "cohorts",
        "source_documents",
        "review_detail",
        "last_change_summary",
    }:
        # Status/decision columns may be a single-select in a hand-maintained
        # Base while still accepting the same human-readable string values.
        return True
    return False


def _table_mapping(mapping: dict[str, Any] | None, table: str) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    nested = mapping.get(table) or mapping.get(table.casefold())
    source = nested if isinstance(nested, dict) else mapping
    return {
        str(key): str(value)
        for key, value in source.items()
        if isinstance(value, str) and value.strip()
    }


def _physical_field_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def _validate_mapping_ownership(table: str, mapping: dict[str, str]) -> None:
    """Fail closed if any two semantics share one physical column."""

    specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
    bindings: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for semantic, field_name in mapping.items():
        spec = specs.get(semantic)
        if not spec:
            continue
        identity = _physical_field_identity(field_name)
        if identity:
            bindings[identity].append((str(spec["owner"]), semantic, field_name))
    for values in bindings.values():
        semantics = sorted({semantic for _, semantic, _ in values})
        if len(semantics) < 2:
            continue
        owners = {owner for owner, _, _ in values}
        names = sorted({name for _, _, name in values}, key=str.casefold)
        if owners == {"human", "machine"}:
            human = sorted(
                {semantic for owner, semantic, _ in values if owner == "human"}
            )
            machine = sorted(
                {semantic for owner, semantic, _ in values if owner == "machine"}
            )
            raise ValueError(
                f"{table} field ownership collision: human-owned {', '.join(human)} "
                f"and machine-owned {', '.join(machine)} resolve to the same "
                f"physical field ({' / '.join(names)})"
            )
        raise ValueError(
            f"{table} field semantic collision: {', '.join(semantics)} resolve "
            f"to the same physical field ({' / '.join(names)})"
        )


def _resolve_field_mapping(
    table: str,
    available: set[str],
    explicit_mapping: dict[str, Any] | None,
) -> tuple[dict[str, str], bool]:
    specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
    explicit = _table_mapping(explicit_mapping, table)
    # Validate the declared intent even if the current schema does not yet
    # contain the target.  Otherwise an additive migration could create a
    # machine field under the alias reserved for an absent human field.
    _validate_mapping_ownership(table, explicit)
    by_identity: dict[str, list[str]] = defaultdict(list)
    for name in sorted(available, key=str.casefold):
        identity = _physical_field_identity(name)
        if identity:
            by_identity[identity].append(name)
    # A complete field-list is authoritative even when the required stable-key
    # column is missing.  The previous implementation treated that situation as
    # an unknown schema and then attempted writes to phantom configured fields.
    schema_is_known = bool(available)
    resolved: dict[str, str] = {}
    for semantic, spec in specs.items():
        target = explicit.get(semantic)
        aliases = tuple(
            dict.fromkeys(
                value
                for value in (
                    target,
                    _standard_field_name(table, semantic, spec),
                    spec["field_name"],
                    *spec["aliases"],
                )
                if value
            )
        )
        present = None
        for name in aliases:
            matches = by_identity.get(_physical_field_identity(name), [])
            if len(matches) > 1:
                raise ValueError(
                    f"{table} Base field lookup is ambiguous after physical-name "
                    f"normalization: {' / '.join(matches)}"
                )
            if matches:
                present = matches[0]
                break
        if present:
            resolved[semantic] = present
        elif not schema_is_known:
            resolved[semantic] = target or spec["field_name"]
    # Resolution includes standard names and aliases.  Re-check the concrete
    # result so case-only or alias-driven collisions cannot route a machine
    # patch into a human-maintained column.
    _validate_mapping_ownership(table, resolved)
    return resolved, schema_is_known


def _comparable(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("record_id", "id", "link", "url", "text", "value", "name"):
            if value.get(key) not in (None, ""):
                return _comparable(value[key])
        return {str(key): _comparable(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        return sorted((_comparable(item) for item in value), key=lambda item: str(item))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _is_empty(value: Any) -> bool:
    return value in (None, "", [])


def _is_removed(value: Any) -> bool:
    return str(_comparable(value)).casefold() in {
        "removed", "deleted", "retired", "已移除", "已删除", "已退役"
    }


def _state_links(
    state: PortableState,
    *,
    entity_kind: str,
    base_token: str,
    table_id: str,
) -> dict[str, str]:
    try:
        rows = state.db.execute(
            """SELECT entity_key,record_id FROM feishu_record_links
               WHERE entity_kind=? AND base_token=? AND table_id=?""",
            (entity_kind, base_token, table_id),
        )
    except Exception:
        return {}
    return {str(row["entity_key"]): str(row["record_id"]) for row in rows}


def _item_patch(
    table: str,
    item: dict[str, Any],
    mapping: dict[str, str],
    existing_fields: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
    values = dict(item.get("field_values") or {})
    creating = existing_fields is None
    patch: dict[str, Any] = {}
    human_differences: list[str] = []
    for semantic, desired in values.items():
        spec = specs.get(semantic)
        target = mapping.get(semantic)
        if not spec or not target:
            continue
        if creating:
            if not _is_empty(desired) or semantic in {"name", "person_key", "source_key"}:
                patch[target] = desired
            continue
        current = existing_fields.get(target, "")
        if spec["owner"] == "human":
            if not _is_empty(desired) and _comparable(current) != _comparable(desired):
                human_differences.append(
                    f"{target}：Base“{_display(current)}”，系统建议“{_display(desired)}”"
                )
            continue
        if _comparable(current) != _comparable(desired):
            patch[target] = desired
    if human_differences and not creating:
        status_field = mapping.get("review_status")
        detail_field = mapping.get("review_detail")
        if status_field:
            if _comparable(existing_fields.get(status_field)) != "字段待确认":
                patch[status_field] = "字段待确认"
            else:
                patch.pop(status_field, None)
        if detail_field:
            desired_detail = "；".join(human_differences)[:1800]
            if _comparable(existing_fields.get(detail_field)) != desired_detail:
                patch[detail_field] = desired_detail
            else:
                patch.pop(detail_field, None)
    return patch, human_differences


def _upsert(
    lark: LarkCli,
    *,
    base_token: str,
    table_id: str,
    fields: dict[str, Any],
    record_id: str | None,
    apply: bool,
) -> tuple[dict[str, Any], str | None]:
    args = [
        "base", "+record-upsert", "--base-token", base_token, "--table-id", table_id,
        "--json", json.dumps(fields, ensure_ascii=False, separators=(",", ":")), "--as", "bot",
    ]
    if record_id:
        args.extend(["--record-id", record_id])
    preview, actual = lark.write(args, apply=apply)
    returned = first_value(actual.payload if actual else {}, "record_id", "id") or record_id
    if apply and not returned:
        raise RuntimeError("Base upsert response did not include record_id")
    return preview.public(), str(returned) if returned else None


def _create_missing_field(
    lark: LarkCli,
    *,
    base_token: str,
    table_id: str,
    field_name: str,
    spec: dict[str, Any],
    people_table_id: str,
    apply: bool,
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "field_name": field_name,
        "type": int(spec["type"]),
    }
    if int(spec["type"]) == 21:
        definition["property"] = {"table_id": people_table_id}
    preview, actual = lark.write(
        [
            "base",
            "+field-create",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--json",
            json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
            "--as",
            "bot",
        ],
        apply=apply,
    )
    return {
        "field_name": field_name,
        "type": int(spec["type"]),
        "action": "created" if actual else "planned",
        "preview": preview.public(),
    }


def sync_visible_master(
    state: PortableState,
    lark: LarkCli,
    *,
    base_token: str,
    people_table_id: str,
    sources_table_id: str | None = None,
    apply: bool,
    field_mapping: dict[str, Any] | None = None,
    existing_records: dict[str, list[dict[str, Any]]] | None = None,
    available_fields: dict[str, Iterable[Any]] | None = None,
    authoritative_roster: bool = False,
) -> dict[str, Any]:
    """Incrementally mirror state without overwriting existing human fields.

    Missing managed rows are soft-tombstoned; returning entities restore the
    same Base record. ``sources_table_id=None`` keeps people and pending review
    rows in one existing table without forcing a Sources table.
    """

    records = visible_records(state)
    if authoritative_roster:
        # When this is the same table the user edits as the roster, a physical
        # deletion is an intentional removal.  Keep the SQLite tombstone but do
        # not recreate the deleted row from the projection on the next mirror.
        records["People"] = [
            item
            for item in records["People"]
            if (item.get("field_values") or {}).get("sync_status") != "已移除"
        ]
    if sources_table_id == people_table_id:
        sources_table_id = None
    table_ids: dict[str, str] = {"People": people_table_id}
    if sources_table_id:
        table_ids["Sources"] = sources_table_id
    remote: dict[str, list[dict[str, Any]]] = {}
    mappings: dict[str, dict[str, str]] = {}
    known_schema: dict[str, bool] = {}
    schema_changes: dict[str, list[dict[str, Any]]] = {
        "People": [],
        "Sources": [],
    }
    unsupported: list[dict[str, Any]] = []
    schema_contexts: dict[str, dict[str, Any]] = {}

    # Phase 1 is strictly read-only.  Validate every table before adding even
    # one field so a later incompatible Sources table cannot leave the People
    # table half-migrated.
    for table, table_id in table_ids.items():
        supplied = (existing_records or {}).get(table)
        remote[table] = (
            [dict(item) for item in supplied]
            if supplied is not None
            else lark.list_base_records(base_token=base_token, table_id=table_id, identity="user")
        )
        supplied_fields = (available_fields or {}).get(table)
        if supplied_fields is None:
            loader = getattr(lark, "list_base_fields", None)
            supplied_fields = (
                loader(base_token=base_token, table_id=table_id, identity="user")
                if callable(loader)
                else []
            )
        supplied_fields = list(supplied_fields or [])
        names = _field_names(supplied_fields)
        descriptors = _field_descriptors(supplied_fields)
        if not names:
            names = {str(name) for row in remote[table] for name in _record_fields(row)}
        mapping, schema_known = _resolve_field_mapping(table, names, field_mapping)
        specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
        explicit = _table_mapping(field_mapping, table)
        incompatible_semantics: set[str] = set()
        if schema_known:
            required_human = "name" if table == "People" else "url"
            if not mapping.get(required_human):
                raise ValueError(
                    f"{table} Base is missing its mapped human-owned "
                    f"{specs[required_human]['field_name']} field"
                )
            for semantic, spec in specs.items():
                resolved_name = mapping.get(semantic)
                descriptor = (
                    descriptors.get(_physical_field_identity(resolved_name))
                    if resolved_name
                    else None
                )
                actual_type = _field_type(descriptor)
                expected_type = int(spec["type"])
                if (
                    resolved_name
                    and actual_type is not None
                    and not _compatible_field_type(semantic, expected_type, actual_type)
                ):
                    stable_semantic = "person_key" if table == "People" else "source_key"
                    if semantic in {stable_semantic, required_human}:
                        role = "stable" if semantic == stable_semantic else "required human-owned"
                        raise ValueError(
                            f"{table} Base {role} {spec['field_name']} field has "
                            f"incompatible type {actual_type}; expected {expected_type}"
                        )
                    incompatible_semantics.add(semantic)
            stable_semantic = "person_key" if table == "People" else "source_key"
            stable_field = mapping.get(stable_semantic)
            if stable_field:
                remote_keys: dict[str, list[str]] = defaultdict(list)
                for row in remote[table]:
                    stable_value = str(
                        _comparable(_record_fields(row).get(stable_field, ""))
                    )
                    record_id = _record_id(row)
                    if stable_value and record_id:
                        remote_keys[stable_value].append(record_id)
                duplicate_remote_keys = {
                    key: sorted(set(record_ids))
                    for key, record_ids in remote_keys.items()
                    if len(set(record_ids)) > 1
                }
                if duplicate_remote_keys:
                    detail = "; ".join(
                        f"{key}={','.join(record_ids)}"
                        for key, record_ids in sorted(duplicate_remote_keys.items())
                    )
                    raise ValueError(
                        f"{table} Base contains duplicate stable keys: {detail}"
                    )
            entity_kind = "person" if table == "People" else "source"
            links = _state_links(
                state,
                entity_kind=entity_kind,
                base_token=base_token,
                table_id=table_id,
            )
            linked_entities: dict[str, list[str]] = defaultdict(list)
            for entity_key, record_id in links.items():
                linked_entities[record_id].append(entity_key)
            duplicate_links = {
                record_id: sorted(entity_keys)
                for record_id, entity_keys in linked_entities.items()
                if len(entity_keys) > 1
            }
            if duplicate_links:
                detail = "; ".join(
                    f"{record_id}={','.join(entity_keys)}"
                    for record_id, entity_keys in sorted(duplicate_links.items())
                )
                raise ValueError(
                    f"{table} Base has multiple entity links for one record: {detail}"
                )
        schema_contexts[table] = {
            "table_id": table_id,
            "names": names,
            "descriptors": descriptors,
            "mapping": mapping,
            "schema_known": schema_known,
            "specs": specs,
            "explicit": explicit,
            "incompatible_semantics": incompatible_semantics,
        }

    # Phase 2 performs only the safe additive migration planned above.  Curated
    # human fields remain untouched; optional incompatible fields are reported.
    for table, context in schema_contexts.items():
        table_id = str(context["table_id"])
        names = context["names"]
        descriptors = context["descriptors"]
        mapping = context["mapping"]
        schema_known = bool(context["schema_known"])
        specs = context["specs"]
        explicit = context["explicit"]
        incompatible_semantics = context["incompatible_semantics"]
        if schema_known:
            for semantic, spec in specs.items():
                resolved_name = mapping.get(semantic)
                expected_type = int(spec["type"])
                if semantic in incompatible_semantics:
                    unsupported.append(
                        {
                            "table": table,
                            "field": resolved_name,
                            "semantic": semantic,
                            "reason": (
                                f"field type {_field_type(descriptors.get(_physical_field_identity(str(resolved_name))))} "
                                f"is incompatible with required type {expected_type}; "
                                "field was preserved"
                            ),
                        }
                    )
                    mapping.pop(semantic, None)
                    continue
                if resolved_name:
                    continue
                if spec["owner"] != "machine" and semantic != "review_decision":
                    unsupported.append(
                        {
                            "table": table,
                            "semantic": semantic,
                            "reason": "mapped human-owned field is absent; schema was preserved",
                        }
                    )
                    continue
                target = explicit.get(semantic) or _standard_field_name(
                    table, semantic, spec
                )
                change = _create_missing_field(
                    lark,
                    base_token=base_token,
                    table_id=table_id,
                    field_name=target,
                    spec=spec,
                    people_table_id=people_table_id,
                    apply=apply,
                )
                schema_changes[table].append(change)
                names.add(target)
                descriptors[_physical_field_identity(target)] = {
                    "field_name": target,
                    "type": expected_type,
                }
            mapping, _ = _resolve_field_mapping(table, names, field_mapping)
            for semantic in incompatible_semantics:
                mapping.pop(semantic, None)
        mappings[table] = mapping
        known_schema[table] = schema_known

    outcomes: dict[str, list[dict[str, Any]]] = {"People": [], "Sources": []}
    write_counts: dict[str, dict[str, int]] = {
        table: {name: 0 for name in ("created", "updated", "restored", "tombstoned", "unchanged")}
        for table in ("People", "Sources")
    }
    people_ids: dict[str, str] = {}

    for table, table_id in table_ids.items():
        specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
        entity_kind = "person" if table == "People" else "source"
        key_semantic = "person_key" if table == "People" else "source_key"
        mapping = mappings[table]
        key_field = mapping.get(key_semantic)
        if not key_field:
            raise ValueError(f"{table} Base needs a stable {specs[key_semantic]['field_name']} field")
        required_human = "name" if table == "People" else "url"
        if not mapping.get(required_human):
            raise ValueError(
                f"{table} Base is missing its mapped human-owned "
                f"{specs[required_human]['field_name']} field"
            )
        links = _state_links(
            state, entity_kind=entity_kind, base_token=base_token, table_id=table_id
        )
        remote_by_id = {_record_id(row): row for row in remote[table] if _record_id(row)}
        remote_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in remote[table]:
            key = str(_comparable(_record_fields(row).get(key_field, "")))
            if key:
                remote_by_key[key].append(row)
        desired_by_key = {str(item["entity_key"]): item for item in records[table]}
        used_record_ids: set[str] = set()

        for entity_key in sorted(desired_by_key):
            item = desired_by_key[entity_key]
            linked_id = links.get(entity_key)
            existing = remote_by_id.get(linked_id or "")
            candidates = remote_by_key.get(entity_key, [])
            if existing is None and candidates:
                existing = sorted(candidates, key=_record_id)[0]
            existing_id = _record_id(existing) if existing else None
            item_for_patch = dict(item)
            values = dict(item.get("field_values") or {})
            if (
                table == "People"
                and authoritative_roster
                and entity_key.startswith("review:")
                and existing_id
                and str(item.get("origin_record_id") or "") == existing_id
            ):
                # This is the user's original roster row, not a separate review
                # artifact.  Keep it ingestible as a person so adding a Homepage,
                # Scholar, GitHub or LinkedIn URL on the next tick resolves the
                # issue.  In particular, never replace its stable key with
                # ``review:*`` or mark it as a review row.
                values.pop("person_key", None)
                values["record_type"] = "人员"
            if table == "Sources":
                person_id = people_ids.get(str(item.get("person_key") or ""))
                if person_id:
                    values["person_link"] = [person_id]
            item_for_patch["field_values"] = values
            existing_fields = _record_fields(existing) if existing else None
            was_removed = bool(
                existing_fields
                and mapping.get("sync_status")
                and _is_removed(existing_fields.get(mapping["sync_status"]))
            )
            patch, human_differences = _item_patch(table, item_for_patch, mapping, existing_fields)
            action = "created" if existing is None else "restored" if was_removed else "updated"
            preview: dict[str, Any] | None = None
            returned_id = existing_id
            if patch:
                preview, returned_id = _upsert(
                    lark,
                    base_token=base_token,
                    table_id=table_id,
                    fields=patch,
                    record_id=existing_id,
                    apply=apply,
                )
                write_counts[table][action] += 1
            else:
                action = "unchanged"
                write_counts[table]["unchanged"] += 1
            if apply and returned_id and links.get(entity_key) != returned_id:
                state.set_feishu_record_link(
                    entity_kind=entity_kind,
                    entity_key=entity_key,
                    base_token=base_token,
                    table_id=table_id,
                    record_id=returned_id,
                )
                links[entity_key] = returned_id
            if table == "People" and returned_id:
                people_ids[entity_key] = returned_id
            if returned_id:
                used_record_ids.add(returned_id)
            outcomes[table].append(
                {
                    "entity_key": entity_key,
                    "record_id": returned_id,
                    "action": action,
                    "changed_fields": sorted(patch),
                    "human_fields_preserved": human_differences,
                    **({"preview": preview} if preview else {}),
                }
            )

        linked_ids = set(links.values())
        managed_field = mapping.get("managed_by")
        status_field = mapping.get("sync_status")
        for row in sorted(remote[table], key=_record_id):
            row_id = _record_id(row)
            if row_id in used_record_ids:
                # The field values in ``remote`` are the pre-write readback.  A
                # row reused under a repaired entity key must not be tombstoned
                # later in the same pass based on that stale key.
                continue
            row_fields = _record_fields(row)
            entity_key = str(_comparable(row_fields.get(key_field, "")))
            if not entity_key or entity_key in desired_by_key:
                continue
            owned = row_id in linked_ids or (
                managed_field and _comparable(row_fields.get(managed_field)) == MANAGED_BY
            )
            if not owned:
                continue
            if not status_field:
                unsupported.append(
                    {
                        "table": table,
                        "entity_key": entity_key,
                        "reason": "existing Base has no Sync Status field; row was preserved",
                    }
                )
                continue
            patch: dict[str, Any] = {}
            if not _is_removed(row_fields.get(status_field)):
                patch[status_field] = "已移除"
            if table == "People" and mapping.get("tracking_status"):
                field = mapping["tracking_status"]
                if _comparable(row_fields.get(field)) != "已移除":
                    patch[field] = "已移除"
            if table == "Sources":
                for semantic, value in (
                    ("tracking_enabled", "否"), ("primary_source", "否"), ("source_status", "已移除")
                ):
                    field = mapping.get(semantic)
                    if field and _comparable(row_fields.get(field)) != value:
                        patch[field] = value
            if patch:
                preview, _ = _upsert(
                    lark,
                    base_token=base_token,
                    table_id=table_id,
                    fields=patch,
                    record_id=row_id,
                    apply=apply,
                )
                write_counts[table]["tombstoned"] += 1
                outcomes[table].append(
                    {
                        "entity_key": entity_key,
                        "record_id": row_id,
                        "action": "tombstoned",
                        "changed_fields": sorted(patch),
                        "human_fields_preserved": [],
                        "preview": preview,
                    }
                )
            else:
                write_counts[table]["unchanged"] += 1

    return {
        "apply": apply,
        "base_token": base_token,
        "tables": {"People": people_table_id, "Sources": sources_table_id},
        "single_table_mode": sources_table_id is None,
        "record_counts": {
            "People": len(records["People"]),
            "Sources": len(records["Sources"]) if sources_table_id else 0,
        },
        "write_counts": write_counts,
        "outcomes": outcomes,
        "field_mapping": mappings,
        "field_ownership": master_field_ownership(),
        "known_schema": known_schema,
        "schema_changes": schema_changes,
        "unsupported": unsupported,
    }
