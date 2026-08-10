from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable


URL_KEYS = ("homepage", "scholar", "github", "linkedin")


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
    field_mapping: dict[str, str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in records:
        fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else raw

        def field(key: str) -> Any:
            name = field_mapping.get(key, key)
            return fields.get(name) if isinstance(fields, dict) else None

        aliases = _values(field("aliases"))
        urls: list[str] = []
        for key in URL_KEYS:
            urls.extend(_values(field(key)))
        name = _plain(field("name"))
        secondary = _plain(field("secondary_id"))
        profile = {
            "school": _plain(field("school")),
            "research_focus": _plain(field("research_focus")),
            "stage": _plain(field("stage")),
        }
        profile = {key: value for key, value in profile.items() if value}
        output.append(
            {
                "canonical_name": name,
                "secondary_id": secondary,
                "aliases": aliases,
                "urls": list(dict.fromkeys(urls)),
                "profile": profile,
                "source_record_id": str(
                    raw.get("record_id") or raw.get("id") or raw.get("row_id") or ""
                ),
                "raw_field_names": sorted(str(key) for key in fields) if isinstance(fields, dict) else [],
            }
        )
    return output
