from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

URL_KEYS = ("homepage", "scholar", "github", "linkedin")


# Explicit mappings still win.  These aliases only make the portable importer
# understand both the original Feishu questionnaire fields and the canonical
# Mono/People Intel export without requiring users to rename either side.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("canonical_name", "name", "Name", "姓名", "name_zh", "name_en"),
    "secondary_id": (
        "secondary_id",
        "tracking_person_key",
        "registry_person_id",
        "Person Key",
        "人员编号",
        "第二ID",
        "ID",
    ),
    "aliases": ("aliases", "Aliases", "别名"),
    "school": ("school", "affiliation", "affiliations", "School", "学校", "机构"),
    "research_focus": (
        "research_focus",
        "research_focuses",
        "research",
        "Research Focus",
        "Research",
        "专业/研究方向",
        "研究方向",
    ),
    "stage": (
        "stage",
        "stage_or_role",
        "stages_or_roles",
        "Stage",
        "阶段/年龄",
        "当前角色",
    ),
    "cohorts": ("cohorts", "cohort", "cohort_years", "名单归属", "Cohorts"),
    "source_documents": (
        "source_documents",
        "source_document",
        "来源文档",
        "Source Documents",
    ),
    "employment_status": (
        "employment_status",
        "employment_status_claims",
        "任职状态",
        "Employment Status",
    ),
    "identity_confidence": (
        "identity_confidence",
        "身份置信度",
        "Identity Confidence",
    ),
    "admission_status": (
        "admission_status",
        "tracking_status",
        "lifecycle_status",
        "同步状态",
        "记录状态",
        "Sync Status",
        "Tracking Status",
    ),
    "record_type": ("record_type", "Record Type", "记录类型", "行类型"),
    "managed_by": ("managed_by", "Managed By", "管理方式", "维护者"),
    "review_status": ("review_status", "审核状态", "Review Status"),
    "review_reason": ("review_reason", "review_detail", "审核说明", "Review Detail"),
    "review_decision": ("review_decision", "Review Decision", "审核决定", "人工决定"),
    "homepage": ("homepage", "Homepage", "个人主页"),
    "scholar": ("scholar", "Google Scholar", "Scholar"),
    "github": ("github", "GitHub"),
    "linkedin": ("linkedin", "LinkedIn"),
}


_ACTIVE_STATES = {
    "",
    "active",
    "active_tracking",
    "enabled",
    "tracking",
    "restored",
    "跟踪中",
    "已恢复",
    "有效",
}
_REMOVED_STATES = {
    "deleted",
    "inactive",
    "removed",
    "retired",
    "replaced",
    "disabled",
    "已删除",
    "已移除",
    "已退役",
    "已停用",
}
_PENDING_STATES = {
    "pending",
    "pending_anchor",
    "pending_review",
    "review",
    "待审核",
    "待补主页",
}


def _plain(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(filter(None, (_plain(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "link", "url", "name", "value"):
            if key in value and value[key] not in (None, ""):
                return _plain(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_values(item))
        return list(dict.fromkeys(result))
    if isinstance(value, dict):
        for key in ("link", "url", "text", "value"):
            if value.get(key):
                return _values(value[key])
        return []
    text = _plain(value)
    matches = re.findall(r"https?://[^\s<>\])，,;；]+", text)
    if matches:
        return list(dict.fromkeys(matches))
    return [item.strip() for item in re.split(r"[\n,，;；]+", text) if item.strip()]


def _mapping_names(field_mapping: dict[str, Any], key: str) -> list[str]:
    mapped = field_mapping.get(key)
    names: list[str] = []
    if isinstance(mapped, str) and mapped.strip():
        names.append(mapped.strip())
    elif isinstance(mapped, (list, tuple)):
        names.extend(str(item).strip() for item in mapped if str(item).strip())
    names.extend(FIELD_ALIASES.get(key, (key,)))
    return list(dict.fromkeys(names))


def _lookup(containers: Iterable[dict[str, Any]], names: Iterable[str]) -> Any:
    wanted = [str(name) for name in names]
    for container in containers:
        for name in wanted:
            if name in container and container[name] not in (None, ""):
                return container[name]
    folded = {name.casefold(): name for name in wanted}
    for container in containers:
        for actual, value in container.items():
            if str(actual).casefold() in folded and value not in (None, ""):
                return value
    return None


def _field_present(containers: Iterable[dict[str, Any]], names: Iterable[str]) -> bool:
    wanted = {str(name).casefold() for name in names}
    return any(
        str(actual).casefold() in wanted
        for container in containers
        for actual in container
    )


def _normalized_record_type(value: Any, *, managed_by: str, secondary_id: str) -> str:
    normalized = _plain(value).casefold().replace("-", "_").replace(" ", "_")
    if normalized in {
        "review",
        "review_item",
        "candidate",
        "candidate_review",
        "pending_candidate",
        "pending_review",
        "审核",
        "审核项",
        "候选",
        "待确认",
        "待审核",
    }:
        return "review"
    # v0.9 review rows created before Record Type existed can still be
    # identified safely by the two machine-owned markers already written by
    # the mirror.  They must never become a new roster person on readback.
    if (
        managed_by.casefold() == "people-tracking-feishu"
        and secondary_id.casefold().startswith("review:")
    ):
        return "review"
    return normalized or "person"


def _urls_from_nested(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [item for item in _values(value) if item.startswith(("http://", "https://"))]
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_urls_from_nested(item))
        return list(dict.fromkeys(output))
    if isinstance(value, dict):
        direct = _lookup((value,), ("url", "link", "canonical_url", "original_url"))
        if direct:
            return _urls_from_nested(direct)
        output: list[str] = []
        for child in value.values():
            output.extend(_urls_from_nested(child))
        return list(dict.fromkeys(output))
    return []


def _normalized_lifecycle(raw: dict[str, Any], fields: dict[str, Any], value: Any) -> str:
    deleted = _lookup((fields, raw), ("is_deleted", "deleted", "deleted_at", "removed_at"))
    if deleted not in (None, "", False, 0, "0", "false", "False"):
        return "removed"
    normalized = _plain(value).casefold().replace("-", "_").replace(" ", "_")
    if normalized in _REMOVED_STATES:
        return "removed"
    if normalized in _PENDING_STATES:
        return "pending_review"
    if normalized in _ACTIVE_STATES:
        return "active"
    # Unknown business statuses are reviewable, never silently treated as a
    # deletion that could retire an otherwise valid person.
    return "pending_review" if normalized else "active"


def _record_hash(record: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"record_version_hash", "change_type", "raw_field_names"}
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_record_id(
    raw: dict[str, Any],
    *,
    name: str,
    secondary: str,
    cohorts: list[str],
    source_documents: list[str],
) -> str:
    explicit = _lookup(
        (raw,),
        (
            "record_id",
            "id",
            "row_id",
            "tracking_person_key",
            "registry_person_id",
            "source_record_id",
        ),
    )
    if explicit not in (None, ""):
        return str(explicit)
    stable_parts = [name.casefold().strip(), secondary.casefold().strip()]
    stable_parts.extend(sorted(value.casefold().strip() for value in cohorts))
    stable_parts.extend(sorted(value.casefold().strip() for value in source_documents))
    return "record_" + hashlib.sha256("|".join(stable_parts).encode("utf-8")).hexdigest()[:24]


def _markdown_tables(markdown: str) -> list[dict[str, str]]:
    lines = markdown.splitlines()
    records: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(lines):
        header = lines[index].strip()
        divider = lines[index + 1].strip()
        if not (header.startswith("|") and re.fullmatch(r"\|?[\s:|-]+\|?", divider)):
            index += 1
            continue
        columns = [cell.strip() for cell in header.strip("|").split("|")]
        index += 2
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
            if len(cells) < len(columns):
                cells.extend([""] * (len(columns) - len(cells)))
            records.append(dict(zip(columns, cells[: len(columns)], strict=True)))
            index += 1
    return records


def _recursive_markdown(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("markdown", "content", "text"):
            child = value.get(key)
            if isinstance(child, str) and ("|" in child or "\n" in child):
                return child
        for child in value.values():
            found = _recursive_markdown(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recursive_markdown(child)
            if found:
                return found
    return None


def records_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Accept lark-cli envelopes, raw record lists, JSON, or fetched Markdown."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("records", "items", "rows"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return [dict(item) for item in value]
        for key in ("data", "result", "document"):
            if key in payload:
                found = records_from_payload(payload[key])
                if found:
                    return found
        markdown = _recursive_markdown(payload)
        if markdown:
            return _markdown_tables(markdown)
    if isinstance(payload, str):
        return _markdown_tables(payload)
    return []


def records_from_file(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("source file must be a regular non-symlink file")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("source file exceeds the 20 MiB safety limit")
    suffix = path.suffix.casefold()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        return records_from_payload(json.loads(text))
    if suffix in {".csv", ".tsv"}:
        dialect = "excel-tab" if suffix == ".tsv" else "excel"
        return [dict(row) for row in csv.DictReader(io.StringIO(text), dialect=dialect)]
    if suffix in {".md", ".markdown", ".txt"}:
        return _markdown_tables(text)
    raise ValueError("supported local source formats are JSON, CSV, TSV, and Markdown")


def canonical_records(
    records: Iterable[dict[str, Any]],
    field_mapping: dict[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else raw
        assert isinstance(fields, dict)
        profile_source = _lookup((fields, raw), ("profile", "profile_json"))
        if isinstance(profile_source, str):
            try:
                profile_source = json.loads(profile_source)
            except json.JSONDecodeError:
                profile_source = {}
        profile = profile_source if isinstance(profile_source, dict) else {}

        def field(
            key: str,
            containers: tuple[dict[str, Any], ...] = (fields, raw, profile),
        ) -> Any:
            return _lookup(containers, _mapping_names(field_mapping, key))

        aliases = _values(field("aliases"))
        urls: list[str] = []
        for key in URL_KEYS:
            urls.extend(_urls_from_nested(field(key)))
        urls.extend(_urls_from_nested(_lookup((fields, raw), ("trackable_urls", "urls"))))

        normalized_sources: list[dict[str, Any]] = []
        source_values = _lookup((fields, raw), ("sources", "profiles", "anchors"))
        if isinstance(source_values, list):
            for source in source_values:
                if not isinstance(source, dict):
                    continue
                source_url = _plain(
                    _lookup((source,), ("url", "link", "canonical_url", "original_url"))
                )
                if not source_url.startswith(("http://", "https://")):
                    continue
                status = _normalized_lifecycle(source, source, source.get("status"))
                normalized_sources.append(
                    {
                        "source_id": _plain(source.get("source_id")),
                        "kind": _plain(source.get("kind")),
                        "url": source_url,
                        "lifecycle_status": status,
                        "is_primary": bool(source.get("is_primary", False)),
                    }
                )
                if status == "active":
                    urls.append(source_url)
        name = _plain(field("name"))
        # In an authoritative Base the configured person_key is the durable
        # identity anchor.  It must win even when the user also maps a separate
        # human-facing secondary ID column; custom key names are not aliases of
        # secondary_id and therefore have to be read explicitly.
        secondary = _plain(field("person_key")) or _plain(field("secondary_id"))
        affiliations = _values(field("school"))
        research_focuses = _values(field("research_focus"))
        stages_or_roles = _values(field("stage"))
        cohorts = _values(field("cohorts"))
        source_documents = _values(field("source_documents"))
        employment_status_claims = _values(field("employment_status"))
        identity_confidence = _plain(field("identity_confidence"))
        admission = field("admission_status")
        lifecycle_status = _normalized_lifecycle(raw, fields, admission)
        managed_by = _plain(field("managed_by"))
        record_type = _normalized_record_type(
            field("record_type"), managed_by=managed_by, secondary_id=secondary
        )
        human_fields_present = [
            key
            for key in (
                "name",
                "aliases",
                "school",
                "research_focus",
                "stage",
                "cohorts",
                "source_documents",
                "employment_status",
                "identity_confidence",
            )
            if _field_present((fields, raw, profile), _mapping_names(field_mapping, key))
        ]
        canonical_profile: dict[str, Any] = {
            # Keep the legacy scalar keys for existing configurations while
            # retaining the complete Mono arrays instead of throwing data away.
            "school": "; ".join(affiliations),
            "research_focus": "; ".join(research_focuses),
            "stage": "; ".join(stages_or_roles),
            "affiliations": affiliations,
            "research_focuses": research_focuses,
            "stages_or_roles": stages_or_roles,
            "cohorts": cohorts,
            "source_documents": source_documents,
            "employment_status_claims": employment_status_claims,
            "identity_confidence": identity_confidence,
        }
        canonical_profile = {
            key: value for key, value in canonical_profile.items() if value not in (None, "", [])
        }
        source_record_id = _stable_record_id(
            raw,
            name=name,
            secondary=secondary,
            cohorts=cohorts,
            source_documents=source_documents,
        )
        canonical = {
            "canonical_name": name,
            "secondary_id": secondary,
            "aliases": aliases,
            "urls": list(dict.fromkeys(urls)),
            "sources": normalized_sources,
            "profile": canonical_profile,
            "source_record_id": source_record_id,
            "lifecycle_status": lifecycle_status,
            "record_type": record_type,
            "managed_by": managed_by,
            "review_status": _plain(field("review_status"))
            or ("pending_anchor" if lifecycle_status == "pending_review" or not urls else ""),
            "review_reason": _plain(field("review_reason")),
            "review_decision": _plain(field("review_decision")),
            "human_fields_present": human_fields_present,
            "raw_field_names": sorted(str(key) for key in fields),
        }
        canonical["record_version_hash"] = _record_hash(canonical)
        output.append(canonical)
    return output


def reconcile_incremental_records(
    previous_records: Iterable[dict[str, Any]],
    current_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return a stable delta plus the next source snapshot.

    Missing records become tombstones rather than disappearing.  Feeding the
    returned ``records`` back as ``previous_records`` is idempotent; a later
    record with the same source ID is classified as ``restored``.  Persistence
    is intentionally left to ``PortableState`` so this pure function cannot
    mutate a database during preview.
    """

    def indexed(values: Iterable[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict):
                continue
            key = str(value.get("source_record_id") or "").strip()
            if not key:
                raise ValueError(f"{label} record is missing source_record_id")
            if key in result:
                raise ValueError(f"{label} contains duplicate source_record_id: {key}")
            item = json.loads(json.dumps(value, ensure_ascii=False))
            item["record_version_hash"] = _record_hash(item)
            result[key] = item
        return result

    previous = indexed(previous_records, label="previous")
    current = indexed(current_records, label="current")
    next_records: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    counts = {name: 0 for name in ("added", "updated", "removed", "restored", "unchanged")}

    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if after is None:
            assert before is not None
            if before.get("lifecycle_status") == "removed":
                next_item = before
                change_type = "unchanged"
            else:
                next_item = {**before, "lifecycle_status": "removed"}
                next_item["record_version_hash"] = _record_hash(next_item)
                change_type = "removed"
        elif before is None:
            next_item = after
            change_type = "removed" if after.get("lifecycle_status") == "removed" else "added"
        elif before.get("lifecycle_status") == "removed" and after.get("lifecycle_status") != "removed":
            next_item = after
            change_type = "restored"
        elif after.get("lifecycle_status") == "removed" and before.get("lifecycle_status") != "removed":
            next_item = after
            change_type = "removed"
        elif before.get("record_version_hash") != after.get("record_version_hash"):
            next_item = after
            change_type = "updated"
        else:
            next_item = after
            change_type = "unchanged"

        next_records.append(next_item)
        counts[change_type] += 1
        if change_type != "unchanged":
            changes.append(
                {
                    "source_record_id": key,
                    "change_type": change_type,
                    "before_hash": before.get("record_version_hash") if before else None,
                    "after_hash": next_item.get("record_version_hash"),
                }
            )

    return {
        "records": next_records,
        "changes": changes,
        "counts": counts,
        "changed_record_ids": [item["source_record_id"] for item in changes],
    }
