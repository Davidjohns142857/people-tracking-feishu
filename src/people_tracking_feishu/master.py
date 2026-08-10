from __future__ import annotations

import json
from typing import Any

from .lark import LarkCli, first_value
from .state import PortableState


PEOPLE_FIELDS = [
    {"field_name": "Name", "type": 1},
    {"field_name": "Person Key", "type": 1},
    {"field_name": "Aliases", "type": 1},
    {"field_name": "School", "type": 1},
    {"field_name": "Research Focus", "type": 1},
    {"field_name": "Stage", "type": 1},
    {"field_name": "Homepage", "type": 15},
    {"field_name": "Google Scholar", "type": 15},
    {"field_name": "GitHub", "type": 15},
    {"field_name": "LinkedIn", "type": 15},
]


def master_schema(people_table_id: str | None = None) -> dict[str, Any]:
    source_fields: list[dict[str, Any]] = [
        {"field_name": "Source Key", "type": 1},
        {"field_name": "Person Key", "type": 1},
    ]
    if people_table_id:
        source_fields.append(
            {
                "field_name": "Person",
                "type": 21,
                "property": {"table_id": people_table_id},
            }
        )
    source_fields.extend(
        [
            {"field_name": "Kind", "type": 1},
            {"field_name": "URL", "type": 15},
            {"field_name": "Health", "type": 1},
            {"field_name": "Last Checked", "type": 1},
            {"field_name": "Last Change Summary", "type": 1},
        ]
    )
    return {"People": PEOPLE_FIELDS, "Sources": source_fields}


def create_master_base(
    lark: LarkCli,
    *,
    timezone_name: str,
    folder_token: str | None,
    apply: bool,
) -> dict[str, Any]:
    args = [
        "base",
        "+base-create",
        "--name",
        "People Tracking Master",
        "--time-zone",
        timezone_name,
        "--as",
        "bot",
    ]
    if folder_token:
        args.extend(["--folder-token", folder_token])
    preview, actual = lark.write(args, apply=apply)
    result: dict[str, Any] = {
        "apply": apply,
        "preview": preview.public(),
        "schema": master_schema(),
    }
    if not actual:
        return result
    base_token = first_value(actual.payload, "base_token", "app_token", "token")
    if not base_token:
        raise RuntimeError("master Base create response did not include a token")
    table_ids: dict[str, str] = {}
    people_preview, people_actual = lark.write(
        [
            "base",
            "+table-create",
            "--base-token",
            str(base_token),
            "--name",
            "People",
            "--fields",
            json.dumps(PEOPLE_FIELDS, ensure_ascii=False, separators=(",", ":")),
            "--as",
            "bot",
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
            "base",
            "+table-create",
            "--base-token",
            str(base_token),
            "--name",
            "Sources",
            "--fields",
            json.dumps(sources_fields, ensure_ascii=False, separators=(",", ":")),
            "--as",
            "bot",
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


def visible_records(state: PortableState) -> dict[str, list[dict[str, Any]]]:
    people_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for person in state.tracker.list_people():
        urls: dict[str, str] = {}
        for source in person["sources"]:
            urls.setdefault(source["kind"], source["url"])
            source_rows.append(
                {
                    "entity_key": source["source_id"],
                    "fields": {
                        "Source Key": source["source_id"],
                        "Person Key": person["person_key"],
                        "Kind": source["kind"],
                        "URL": source["url"],
                        "Health": source["health_status"],
                        "Last Checked": source.get("last_checked_at") or "",
                        "Last Change Summary": source.get("health_detail") or "",
                    },
                    "person_key": person["person_key"],
                }
            )
        profile = person["profile"]
        people_rows.append(
            {
                "entity_key": person["person_key"],
                "fields": {
                    "Name": person["canonical_name"],
                    "Person Key": person["person_key"],
                    "Aliases": "; ".join(person["aliases"]),
                    "School": profile.get("school", ""),
                    "Research Focus": profile.get("research_focus", ""),
                    "Stage": profile.get("stage", ""),
                    "Homepage": urls.get("homepage", ""),
                    "Google Scholar": urls.get("scholar", ""),
                    "GitHub": urls.get("github", ""),
                    "LinkedIn": urls.get("linkedin", ""),
                },
            }
        )
    return {"People": people_rows, "Sources": source_rows}


def sync_visible_master(
    state: PortableState,
    lark: LarkCli,
    *,
    base_token: str,
    people_table_id: str,
    sources_table_id: str,
    apply: bool,
) -> dict[str, Any]:
    records = visible_records(state)
    if not apply:
        return {
            "apply": False,
            "base_token": base_token,
            "tables": {"People": people_table_id, "Sources": sources_table_id},
            "record_counts": {key: len(value) for key, value in records.items()},
        }
    outcomes: dict[str, list[dict[str, Any]]] = {"People": [], "Sources": []}
    people_ids: dict[str, str] = {}
    for table_name, table_id, entity_kind in (
        ("People", people_table_id, "person"),
        ("Sources", sources_table_id, "source"),
    ):
        for item in records[table_name]:
            entity_key = item["entity_key"]
            record_id = state.feishu_record_link(
                entity_kind=entity_kind,
                entity_key=entity_key,
                base_token=base_token,
                table_id=table_id,
            )
            fields = dict(item["fields"])
            if table_name == "Sources" and item["person_key"] in people_ids:
                fields["Person"] = [people_ids[item["person_key"]]]
            args = [
                "base",
                "+record-upsert",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--json",
                json.dumps(fields, ensure_ascii=False, separators=(",", ":")),
                "--as",
                "bot",
            ]
            if record_id:
                args.extend(["--record-id", record_id])
            preview, actual = lark.write(args, apply=True)
            returned_id = first_value(actual.payload if actual else {}, "record_id", "id") or record_id
            if not returned_id:
                raise RuntimeError(f"{table_name} upsert response did not include record_id")
            state.set_feishu_record_link(
                entity_kind=entity_kind,
                entity_key=entity_key,
                base_token=base_token,
                table_id=table_id,
                record_id=str(returned_id),
            )
            if table_name == "People":
                people_ids[entity_key] = str(returned_id)
            outcomes[table_name].append(
                {"entity_key": entity_key, "record_id": str(returned_id), "preview": preview.public()}
            )
    return {
        "apply": True,
        "base_token": base_token,
        "tables": {"People": people_table_id, "Sources": sources_table_id},
        "record_counts": {key: len(value) for key, value in outcomes.items()},
        "outcomes": outcomes,
    }
