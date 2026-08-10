from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from people_intel.schemas import GraphQuery, ReviewDeliveryPage
from people_intel.service import TemporalMemoryService
from people_intel.schemas import CommandActor, CommandEnvelope


BASE_TABLES: dict[str, list[str]] = {
    "Entities": ["entity_id", "entity_type", "canonical_name", "aliases", "working_summary"],
    "Timeline": ["assertion_id", "subject", "predicate", "object", "valid_time", "known_at", "status", "source"],
    "Working View": ["entity_id", "predicate", "selected_assertion_id", "alternatives", "selection_reasons"],
    "Assertions": ["assertion_id", "triple", "confidence", "review_required", "annotation_action"],
    "Initial Reviews": ["sequence", "review_item_id", "review_kind", "fact", "question", "status", "evidence", "inference_chain"],
    "Sources": ["source_version_id", "source_uri", "content_hash", "retrieved_at", "previous_version_id"],
    "Signals": ["signal_id", "signal_type", "score", "confidence", "summary", "assertion_ids"],
    "Candidates": ["candidate_id", "entity_id", "reason", "score", "status"],
}


class FeishuAccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class FeishuAllowlist:
    open_ids: frozenset[str] = frozenset()
    chat_ids: frozenset[str] = frozenset()

    @classmethod
    def from_iterables(cls, open_ids: Iterable[str], chat_ids: Iterable[str]) -> "FeishuAllowlist":
        return cls(frozenset(filter(None, open_ids)), frozenset(filter(None, chat_ids)))

    def authorize(self, open_id: str, chat_id: str | None) -> None:
        if open_id in self.open_ids:
            return
        if chat_id and chat_id in self.chat_ids:
            return
        raise FeishuAccessDenied(f"actor/chat is not allowlisted: {open_id} {chat_id or ''}")


class FeishuEventAdapter:
    EVENT_KEY = "im.message.receive_v1"

    def __init__(self, allowlist: FeishuAllowlist):
        self.allowlist = allowlist

    def to_command(self, event: dict[str, Any]) -> CommandEnvelope:
        header = event.get("header", {})
        event_body = event.get("event", {})
        sender = event_body.get("sender", {}).get("sender_id", {})
        message = event_body.get("message", {})
        open_id = sender.get("open_id", "")
        chat_id = message.get("chat_id")
        self.allowlist.authorize(open_id, chat_id)
        message_id = message.get("message_id") or header.get("event_id")
        content = message.get("content", "{}")
        try:
            content_payload = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            content_payload = {"text": str(content)}
        chat_type = message.get("chat_type")
        actor = CommandActor(
            channel="feishu_dm" if chat_type == "p2p" else "feishu_group",
            actor_id=open_id,
            chat_id=chat_id,
        )
        text = str(content_payload.get("text") or "").strip() if isinstance(content_payload, dict) else ""
        scan = self._parse_scan_request(text)
        if scan is not None:
            return CommandEnvelope(
                command_id=f"cmd_feishu_{message_id}",
                command_type="scan.request",
                occurred_at=_feishu_time(header.get("create_time") or event_body.get("event_time")),
                actor=actor,
                payload=scan,
            )
        return CommandEnvelope(
            command_id=f"cmd_feishu_{message_id}",
            command_type="source.ingest",
            occurred_at=_feishu_time(header.get("create_time") or event_body.get("event_time")),
            actor=actor,
            payload={
                "source_uri": f"feishu-message://{message_id}",
                "source_type": "feishu",
                "media_type": "application/json",
                "content": content_payload,
                "message_id": message_id,
            },
        )

    @staticmethod
    def _parse_scan_request(text: str) -> dict[str, Any] | None:
        """Map a small, explicit Chinese command grammar to connector intent."""
        patterns = [
            (r"^扫描\s*[Xx]\s+(.+)$", "x"),
            (r"^扫描(?:公众号|微信)\s+(.+)$", "wechat"),
            (r"^扫描(?:小红书|XHS)\s+(.+)$", "xhs"),
            (r"^扫描(?:融资|新闻|IT桔子)\s+(.+)$", "news"),
        ]
        for pattern, channel in patterns:
            match = re.match(pattern, text)
            if match:
                query = match.group(1).strip()
                return {
                    "channel": channel,
                    "query": query,
                    "subject_name": query,
                    "trigger": "feishu",
                    "max_results": 5,
                }
        match = re.match(r"^读取(?:公众号|微信)\s+(https://mp\.weixin\.qq\.com/\S+)$", text)
        if match:
            return {
                "channel": "wechat",
                "query": match.group(1),
                "source_uri": match.group(1),
                "subject_name": "飞书指定公众号文章",
                "trigger": "feishu",
                "max_results": 1,
            }
        return None


class LarkCliTransport:
    """Narrow bot transport. It never invokes auth/config/login commands."""

    def send_markdown(
        self,
        *,
        markdown: str,
        idempotency_key: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        dry_run: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if bool(chat_id) == bool(user_id):
            raise ValueError("exactly one of chat_id or user_id is required")
        command = ["lark-cli", "im", "+messages-send", "--as", "bot", "--markdown", markdown]
        command.extend(["--chat-id", chat_id] if chat_id else ["--user-id", user_id or ""])
        command.extend(["--idempotency-key", idempotency_key])
        if dry_run:
            command.append("--dry-run")
        return subprocess.run(command, text=True, capture_output=True, check=False)


class FeishuReviewCardRenderer:
    """Render review delivery data as Feishu interactive-card JSON.

    This renderer deliberately contains no knowledge logic. Button values carry
    the existing review-item decision endpoint and body, so a Feishu callback
    handler can relay the click to the same append-only API used by the web UI.
    """

    @staticmethod
    def render_page(page: ReviewDeliveryPage) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for item in page.cards:
            kind = "逻辑推定" if item.review_kind == "derived_inference" else "直接事实"
            evidence = "\n".join(f"- {quote}" for quote in item.evidence_quotes) or "- 暂无原文摘录"
            inference = ""
            if item.inference_chain_zh:
                inference = "\n**推理链（也需要确认）**\n" + "\n".join(
                    f"- {line}" for line in item.inference_chain_zh
                )
            elements: list[dict[str, Any]] = [
                {
                    "tag": "markdown",
                    "content": (
                        f"**第 {item.sequence} 条 · {kind}**\n"
                        f"{item.fact_zh}\n\n"
                        f"**需要你判断**：{item.question_zh}\n\n"
                        f"**原文证据**\n{evidence}{inference}\n\n"
                        f"置信度 `{item.confidence:.2f}` · 状态 `{item.status}`"
                    ),
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": action.label_zh},
                            "type": "primary" if action.action == "confirm" else "default",
                            "value": {
                                "review_batch_id": page.review_batch_id,
                                "review_item_id": item.review_item_id,
                                "action": action.action,
                                **action.postback,
                            },
                        }
                        for action in item.actions
                    ],
                },
                {
                    "tag": "note",
                    "elements": [{
                        "tag": "plain_text",
                        "content": "也可直接回复：" + "；".join(action.command_text for action in item.actions),
                    }],
                },
            ]
            cards.append({
                "config": {"wide_screen_mode": True, "enable_forward": False},
                "header": {
                    "template": "orange" if item.review_kind == "derived_inference" else "blue",
                    "title": {"tag": "plain_text", "content": item.title_zh},
                },
                "elements": elements,
            })
        return cards


class FeishuBaseClient(Protocol):
    def upsert_record(self, *, table_name: str, external_key: str, fields: dict[str, Any]) -> None: ...


class FeishuMirror:
    """Projects backend views to Base without making Base authoritative."""

    def __init__(self, service: TemporalMemoryService, client: FeishuBaseClient, api_base_url: str):
        self.service = service
        self.client = client
        self.api_base_url = api_base_url.rstrip("/")

    def mirror_entity(self, entity_id: str, query: GraphQuery) -> None:
        entity = self.service.ledger.get_entity(entity_id)
        timeline = self.service.timeline(entity_id, known_at=query.known_at)
        working = self.service.working_view(query)
        self.client.upsert_record(
            table_name="Entities",
            external_key=entity.entity_id,
            fields={
                "entity_id": entity.entity_id,
                "entity_type": str(entity.entity_type),
                "canonical_name": entity.canonical_name,
                "aliases": entity.aliases,
                "working_summary": [
                    {
                        "predicate": item.assertion.predicate_id,
                        "selected_assertion_id": item.assertion.assertion_id,
                    }
                    for item in working.selected
                ],
            },
        )
        for item in timeline:
            assertion = item["assertion"]
            source_ids = sorted(
                {
                    self.service.ledger.get_evidence_span(span_id).source_version_id
                    for span_id in assertion.evidence_span_ids
                }
            )
            self.client.upsert_record(
                table_name="Timeline",
                external_key=assertion.assertion_id,
                fields={
                    "assertion_id": assertion.assertion_id,
                    "subject": assertion.subject_entity_id,
                    "predicate": assertion.predicate_id,
                    "object": assertion.object.model_dump(mode="json"),
                    "valid_time": assertion.valid_time.model_dump(mode="json"),
                    "known_at": assertion.transaction_time.ingested_at.isoformat(),
                    "status": str(item["effective_status"]),
                    "source": [self._source_url(source_id) for source_id in source_ids],
                },
            )
        for item in working.selected:
            self.client.upsert_record(
                table_name="Working View",
                external_key=f"{entity_id}:{item.assertion.predicate_id}:{item.assertion.assertion_id}",
                fields={
                    "entity_id": entity_id,
                    "predicate": item.assertion.predicate_id,
                    "selected_assertion_id": item.assertion.assertion_id,
                    "alternatives": item.alternative_assertion_ids,
                    "selection_reasons": item.selection_reasons,
                },
            )

    def mirror_signals(self) -> None:
        for signal in self.service.ledger.list_signals():
            self.client.upsert_record(
                table_name="Signals",
                external_key=signal.signal_id,
                fields={
                    "signal_id": signal.signal_id,
                    "signal_type": str(signal.signal_type),
                    "score": signal.score,
                    "confidence": signal.confidence,
                    "summary": signal.summary,
                    "assertion_ids": signal.triggering_assertion_ids,
                },
            )

    def _source_url(self, source_version_id: str) -> str:
        return f"{self.api_base_url}/v1/source-documents/{source_version_id}/content"


def _feishu_time(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
