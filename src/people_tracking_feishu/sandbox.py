from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from people_intel.light_tracker import FetchObservation, LightTracker

from .lark import LarkCli, first_value


BASE = """<html><head><title>Synthetic Feishu Sandbox Researcher</title></head>
<body><main><h1>Synthetic Feishu Sandbox Researcher</h1>
<h2>Research</h2><p>Reliable evaluation systems.</p>
<h2>Publications</h2><p><a href="https://synthetic.invalid/paper/1">Synthetic Paper One, 2025</a></p>
</main></body></html>"""
CHANGED = BASE.replace(
    "</main>",
    '<p><a href="https://synthetic.invalid/paper/2">Synthetic Paper Two, 2026</a></p></main>',
)


def algorithm_e2e(database: Path) -> dict[str, Any]:
    tracker = LightTracker(database)
    try:
        person = tracker.add_person(
            "Synthetic Feishu Sandbox Researcher",
            aliases=["合成飞书沙箱研究者"],
            urls=["https://synthetic.invalid/researcher"],
            secondary_id=("sandbox", "person-001"),
            profile={"school": "Synthetic University", "research_focus": "Testing"},
        )
        source_id = person["sources"][0]["source_id"]
        decisions: list[str] = []
        for body in (BASE, CHANGED, CHANGED):
            run_id = tracker.start_run("sandbox-e2e")
            decision = tracker.observe(
                run_id,
                source_id,
                FetchObservation(
                    body=body,
                    status_code=200,
                    final_url="https://synthetic.invalid/researcher",
                    retrieval_mode="direct",
                ),
            )
            tracker.complete_run(run_id)
            decisions.append(decision.status)
        if decisions != ["baseline", "candidate", "changed"]:
            raise RuntimeError(f"algorithm E2E did not confirm the expected sequence: {decisions}")
        return {
            "synthetic_only": True,
            "person_key": person["person_key"],
            "source_id": source_id,
            "decisions": decisions,
            "passed": True,
            "last_report": tracker.render_report(run_id),
        }
    finally:
        tracker.close()


def feishu_e2e(
    *,
    lark: LarkCli | None,
    algorithm: dict[str, Any],
    apply: bool,
    message_target: dict[str, Any],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    if lark is None:
        actions.extend(
            [
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_test_base",
                    "name": "[TEST] People Tracking Feishu Sandbox",
                    "tables": ["People", "Sources"],
                    "synthetic_only": True,
                },
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_test_document",
                    "title": "[TEST] 人才跟踪报告",
                    "markdown": algorithm["last_report"],
                },
                {
                    "adapter": "agent_tool_bridge",
                    "operation": "send_test_message",
                    "target": message_target,
                    "idempotency_key": "people-tracking-feishu-sandbox-message-v1",
                },
                {"adapter": "agent_tool_bridge", "operation": "read_back_and_verify"},
            ]
        )
        return {"apply": False, "bridge_required": True, "actions": actions}

    base_args = [
        "base",
        "+base-create",
        "--name",
        "[TEST] People Tracking Feishu Sandbox",
        "--time-zone",
        "Asia/Shanghai",
        "--as",
        "bot",
    ]
    base_preview, base_actual = lark.write(base_args, apply=apply)
    actions.append({"operation": "base_create", "preview": base_preview.public()})
    if not base_actual:
        document_preview, _ = lark.create_document(
            title="[TEST] 人才跟踪报告",
            markdown=algorithm["last_report"],
            identity="bot",
            apply=False,
        )
        actions.append({"operation": "document_create", "preview": document_preview.public()})
        return {"apply": False, "bridge_required": False, "actions": actions}
    base_token = first_value(base_actual.payload, "base_token", "app_token", "token")
    if not base_token:
        raise RuntimeError("sandbox Base create response did not contain a token")
    table_ids: dict[str, str] = {}
    schemas = {
        "People": [
            {"field_name": "Name", "type": 1},
            {"field_name": "Person Key", "type": 1},
            {"field_name": "School", "type": 1},
        ],
        "Sources": [
            {"field_name": "Person Key", "type": 1},
            {"field_name": "Kind", "type": 1},
            {"field_name": "URL", "type": 15},
            {"field_name": "Decision", "type": 1},
        ],
    }
    for name, fields in schemas.items():
        preview, actual = lark.write(
            [
                "base",
                "+table-create",
                "--base-token",
                str(base_token),
                "--name",
                name,
                "--fields",
                json.dumps(fields, ensure_ascii=False, separators=(",", ":")),
                "--as",
                "bot",
            ],
            apply=True,
        )
        actions.append({"operation": f"table_create_{name}", "preview": preview.public()})
        token = first_value(actual.payload if actual else {}, "table_id", "id")
        if not token:
            raise RuntimeError(f"sandbox table {name} response did not contain table_id")
        table_ids[name] = str(token)
    people_record = {
        "Name": "Synthetic Feishu Sandbox Researcher",
        "Person Key": algorithm["person_key"],
        "School": "Synthetic University",
    }
    sources_record = {
        "Person Key": algorithm["person_key"],
        "Kind": "homepage",
        "URL": "https://synthetic.invalid/researcher",
        "Decision": "baseline → candidate → changed",
    }
    for name, record in (("People", people_record), ("Sources", sources_record)):
        preview, _ = lark.write(
            [
                "base",
                "+record-upsert",
                "--base-token",
                str(base_token),
                "--table-id",
                table_ids[name],
                "--json",
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                "--as",
                "bot",
            ],
            apply=True,
        )
        actions.append({"operation": f"record_upsert_{name}", "preview": preview.public()})
    doc_preview, doc_actual = lark.create_document(
        title="[TEST] 人才跟踪报告",
        markdown=algorithm["last_report"],
        identity="bot",
        apply=True,
    )
    actions.append({"operation": "document_create", "preview": doc_preview.public()})
    doc_url = first_value(doc_actual.payload if doc_actual else {}, "url", "document_url", "wiki_url")
    target_kind = str(message_target.get("target_kind") or "current_chat")
    if target_kind in {"chat", "user"} and message_target.get("target_id"):
        preview, message_actual = lark.send_message(
            markdown=f"[TEST] 人才跟踪沙箱通过。报告：{doc_url}",
            idempotency_key="people-tracking-feishu-sandbox-message-v1",
            target_kind=target_kind,
            target_id=str(message_target["target_id"]),
            apply=True,
        )
        actions.append({"operation": "message_send", "preview": preview.public()})
        message_id = first_value(message_actual.payload if message_actual else {}, "message_id", "id")
    else:
        actions.append(
            {
                "adapter": "agent_tool_bridge",
                "operation": "send_test_message",
                "markdown": f"[TEST] 人才跟踪沙箱通过。报告：{doc_url}",
                "idempotency_key": "people-tracking-feishu-sandbox-message-v1",
            }
        )
        message_id = None
    readback = {
        "people": lark.list_base_records(base_token=str(base_token), table_id=table_ids["People"], identity="bot"),
        "sources": lark.list_base_records(base_token=str(base_token), table_id=table_ids["Sources"], identity="bot"),
        "document": lark.fetch_document(str(doc_url), identity="bot") if doc_url else {},
    }
    passed = bool(readback["people"] and readback["sources"] and readback["document"])
    if not passed:
        raise RuntimeError("Feishu sandbox readback verification failed")
    return {
        "apply": True,
        "bridge_required": target_kind == "current_chat",
        "actions": actions,
        "objects": {
            "base_token": base_token,
            "table_ids": table_ids,
            "document_url": doc_url,
            "message_id": message_id,
        },
        "readback_passed": True,
        "idempotency_checked": bool(message_id) or target_kind == "current_chat",
        "cleanup": {
            "automatic": False,
            "question": "沙箱已通过。请回复“保留测试对象”或“确认清理测试对象”。",
            "reason": "删除属于破坏性操作，必须在对象 token 回显后另行确认。",
        },
    }
