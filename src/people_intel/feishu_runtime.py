from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from people_intel.adapters.feishu import FeishuAccessDenied, FeishuAllowlist, FeishuEventAdapter
from people_intel.commands import CommandProcessor
from people_intel.connector_jobs import build_scan_request_handler
from people_intel.schemas import CommandReceipt
from people_intel.service import TemporalMemoryService


logger = logging.getLogger(__name__)


def _split_ids(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class FeishuRuntimeConfig:
    app_id: str
    app_secret: str
    verification_token: str = ""
    encrypt_key: str = ""
    allowed_open_ids: frozenset[str] = frozenset()
    allowed_chat_ids: frozenset[str] = frozenset()
    transport: str = "websocket"

    @classmethod
    def from_environment(cls) -> "FeishuRuntimeConfig":
        return cls(
            app_id=os.environ.get("PEOPLE_INTEL_FEISHU_APP_ID", "").strip(),
            app_secret=os.environ.get("PEOPLE_INTEL_FEISHU_APP_SECRET", "").strip(),
            verification_token=os.environ.get("PEOPLE_INTEL_FEISHU_VERIFICATION_TOKEN", "").strip(),
            encrypt_key=os.environ.get("PEOPLE_INTEL_FEISHU_ENCRYPT_KEY", "").strip(),
            allowed_open_ids=_split_ids(os.environ.get("PEOPLE_INTEL_FEISHU_ALLOWED_OPEN_IDS")),
            allowed_chat_ids=_split_ids(os.environ.get("PEOPLE_INTEL_FEISHU_ALLOWED_CHAT_IDS")),
            transport=os.environ.get("PEOPLE_INTEL_FEISHU_TRANSPORT", "websocket").strip().lower(),
        )

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.app_id:
            missing.append("APP_ID")
        if not self.app_secret:
            missing.append("APP_SECRET")
        if not (self.allowed_open_ids or self.allowed_chat_ids):
            missing.append("OPEN_ID_OR_CHAT_ID_ALLOWLIST")
        if self.transport != "websocket":
            missing.append("SUPPORTED_TRANSPORT_WEBSOCKET")
        return missing

    @property
    def ready(self) -> bool:
        return not self.missing()

    def allowlist(self) -> FeishuAllowlist:
        return FeishuAllowlist(self.allowed_open_ids, self.allowed_chat_ids)


class FeishuIngress:
    """Transport-independent, idempotent Feishu event boundary.

    It validates the sender/chat allowlist, converts a message to the shared
    command envelope, and appends either a source or ConnectorJob. No external
    connector is called here.
    """

    def __init__(self, service: TemporalMemoryService, config: FeishuRuntimeConfig):
        self.service = service
        self.config = config
        self.adapter = FeishuEventAdapter(config.allowlist())
        self.processor = CommandProcessor(
            service,
            scan_request_handler=build_scan_request_handler(service),
        )

    def handle_message_event(self, payload: dict[str, Any]) -> CommandReceipt:
        command = self.adapter.to_command(payload)
        return self.processor.execute(command)


class FeishuLongConnectionWorker:
    """Official Feishu SDK WebSocket transport for the local persistent host."""

    def __init__(
        self,
        service: TemporalMemoryService,
        config: FeishuRuntimeConfig | None = None,
        *,
        lark_module: Any | None = None,
    ):
        self.service = service
        self.config = config or FeishuRuntimeConfig.from_environment()
        missing = self.config.missing()
        if missing:
            raise RuntimeError("Feishu worker is not configured: " + ", ".join(missing))
        if lark_module is None:
            try:
                import lark_oapi as lark_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise RuntimeError("Feishu worker requires the optional 'feishu' dependency") from exc
        self.lark = lark_module
        self.ingress = FeishuIngress(service, self.config)
        self.event_handler = self._build_event_handler()
        self.client = self.lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=self.event_handler,
            log_level=self.lark.LogLevel.INFO,
        )

    def _build_event_handler(self):
        def on_message(data: Any) -> None:
            payload = json.loads(self.lark.JSON.marshal(data))
            try:
                receipt = self.ingress.handle_message_event(payload)
            except FeishuAccessDenied:
                header = payload.get("header", {})
                logger.warning("Rejected non-allowlisted Feishu event event_id=%s", header.get("event_id"))
                return
            logger.info(
                "Accepted Feishu event command_id=%s status=%s",
                receipt.command_id,
                receipt.status,
            )

        return (
            self.lark.EventDispatcherHandler.builder(
                self.config.encrypt_key,
                self.config.verification_token,
            )
            .register_p2_im_message_receive_v1(on_message)
            .build()
        )

    def start(self) -> None:
        self.client.start()


__all__ = [
    "FeishuIngress",
    "FeishuLongConnectionWorker",
    "FeishuRuntimeConfig",
]
