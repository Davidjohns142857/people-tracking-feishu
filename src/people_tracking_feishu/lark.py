from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


PINNED_LARK_CLI = "1.0.82"
PINNED_NPM_INTEGRITY = (
    "sha512-7jqwniqCtiunLPi2vypDu0aHSaPNeG93kRO9UZ9kywU/"
    "XSVSy/PH1L1GJfav1Goi95v3D8TjV5R+ttWnMEvjYQ=="
)
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
REDACT_VALUE_FLAGS = {"--markdown", "--json", "--content", "--text", "--data"}


class LarkCliError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        returncode: int | None = None,
        payload: Any = None,
    ):
        super().__init__(message)
        self.argv = argv or []
        self.returncode = returncode
        self.payload = payload


def _extract_version(value: str) -> str | None:
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", value)
    return match.group(1) if match else None


def _redacted_argv(argv: Iterable[str]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for value in argv:
        if redact_next:
            result.append("<redacted-content>")
            redact_next = False
            continue
        result.append(value)
        if value in REDACT_VALUE_FLAGS:
            redact_next = True
    return result


def _walk_values(value: Any, keys: set[str]) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in keys:
                yield child
            yield from _walk_values(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child, keys)


def first_value(payload: Any, *keys: str) -> Any:
    return next((value for value in _walk_values(payload, set(keys)) if value not in (None, "")), None)


def result_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    payload: Any
    stderr: str
    dry_run: bool

    def public(self) -> dict[str, Any]:
        return {
            "argv": _redacted_argv(self.argv),
            "ok": True,
            "dry_run": self.dry_run,
            "payload": self.payload,
            "stderr": self.stderr[-2000:] if self.stderr else "",
        }


class LarkCli:
    """Strict argv-only adapter for the official lark-cli JSON contract."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        timeout_seconds: int = 60,
        require_version: str = PINNED_LARK_CLI,
    ):
        resolved = str(executable) if executable else shutil.which("lark-cli")
        if not resolved:
            raise LarkCliError("lark-cli is not installed")
        self.executable = str(Path(resolved).expanduser())
        self.timeout_seconds = timeout_seconds
        self.require_version = require_version

    def version(self) -> str:
        completed = subprocess.run(
            [self.executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=self._environment(),
        )
        version = _extract_version(completed.stdout + "\n" + completed.stderr)
        if completed.returncode != 0 or not version:
            raise LarkCliError("unable to read lark-cli version", returncode=completed.returncode)
        return version

    def assert_pinned(self) -> None:
        version = self.version()
        if version != self.require_version:
            raise LarkCliError(
                f"lark-cli {self.require_version} is required; found {version}"
            )

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["NO_COLOR"] = "1"
        environment["CI"] = "1"
        return environment

    def run(
        self,
        args: list[str],
        *,
        require_ok: bool = True,
        dry_run: bool = False,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        if any(not isinstance(value, str) or "\x00" in value for value in args):
            raise LarkCliError("lark-cli argv contains an invalid value")
        argv = [self.executable, *args]
        if dry_run and "--dry-run" not in argv:
            argv.append("--dry-run")
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=False,
            timeout=timeout_seconds or self.timeout_seconds,
            check=False,
            env=self._environment(),
        )
        if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
            raise LarkCliError("lark-cli output exceeded the safety limit", argv=_redacted_argv(argv))
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise LarkCliError(
                "lark-cli did not return JSON",
                argv=_redacted_argv(argv),
                returncode=completed.returncode,
            ) from exc
        if completed.returncode != 0:
            raise LarkCliError(
                str(first_value(payload, "message", "msg", "error") or stderr or "lark-cli failed"),
                argv=_redacted_argv(argv),
                returncode=completed.returncode,
                payload=payload,
            )
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise LarkCliError(
                str(first_value(payload, "message", "msg", "error") or "lark-cli returned ok=false"),
                argv=_redacted_argv(argv),
                payload=payload,
            )
        if require_ok and (not isinstance(payload, dict) or payload.get("ok") is not True):
            raise LarkCliError(
                "lark-cli JSON contract missing ok=true",
                argv=_redacted_argv(argv),
                payload=payload,
            )
        return CommandResult(argv=argv, payload=payload, stderr=stderr, dry_run=dry_run)

    def metadata(self, args: list[str]) -> CommandResult:
        """Run auth/doctor commands whose documented envelope predates ok=true."""

        return self.run(args, require_ok=False)

    def write(self, args: list[str], *, apply: bool) -> tuple[CommandResult, CommandResult | None]:
        preview = self.run(args, dry_run=True)
        if not apply:
            return preview, None
        actual = self.run(args)
        return preview, actual

    def auth_status(self) -> dict[str, Any]:
        result = self.metadata(["auth", "status"])
        return result.payload if isinstance(result.payload, dict) else {"raw": result.payload}

    def doctor(self, *, offline: bool = True) -> dict[str, Any]:
        args = ["doctor"]
        if offline:
            args.append("--offline")
        result = self.metadata(args)
        return result.payload if isinstance(result.payload, dict) else {"raw": result.payload}

    def resolve_wiki(self, url: str, *, identity: str = "user") -> dict[str, Any]:
        result = self.run(
            [
                "wiki",
                "+node-get",
                "--node-token",
                url,
                "--as",
                identity,
                "--format",
                "json",
            ]
        )
        return result_data(result.payload)

    def fetch_document(self, url: str, *, identity: str = "user") -> dict[str, Any]:
        result = self.run(
            [
                "docs",
                "+fetch",
                "--api-version",
                "v2",
                "--doc",
                url,
                "--as",
                identity,
                "--format",
                "json",
            ],
            timeout_seconds=120,
        )
        data = result_data(result.payload)
        return data if isinstance(data, dict) else {"content": data}

    def base_token_from_source(self, url: str, *, identity: str = "user") -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        for key in ("base_token", "app_token"):
            if query.get(key):
                return str(query[key][0])
        match = re.search(r"/(?:base|bitable)/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
        if "/wiki/" in parsed.path:
            node = self.resolve_wiki(url, identity=identity)
            obj_type = str(first_value(node, "obj_type", "type") or "")
            token = first_value(node, "obj_token", "base_token", "app_token")
            if obj_type not in {"bitable", "base"} or not token:
                raise LarkCliError("the Wiki node is not a Base")
            return str(token)
        raise LarkCliError("unable to resolve Base token from source URL")

    def base_schema(
        self,
        url: str,
        *,
        identity: str = "user",
        configured_table: str | None = None,
    ) -> dict[str, Any]:
        token = self.base_token_from_source(url, identity=identity)
        base = self.run(
            ["base", "+base-get", "--base-token", token, "--as", identity]
        )
        tables = self.run(
            ["base", "+table-list", "--base-token", token, "--as", identity, "--limit", "100"]
        )
        table_items = list(_record_list(result_data(tables.payload), keys=("items", "tables")))
        chosen = None
        if configured_table:
            chosen = next(
                (
                    item
                    for item in table_items
                    if configured_table
                    in {str(item.get("table_id") or ""), str(item.get("name") or "")}
                ),
                None,
            )
        chosen = chosen or (table_items[0] if table_items else None)
        fields: list[dict[str, Any]] = []
        if chosen:
            table_id = str(chosen.get("table_id") or chosen.get("id") or chosen.get("name"))
            field_result = self.run(
                [
                    "base",
                    "+field-list",
                    "--base-token",
                    token,
                    "--table-id",
                    table_id,
                    "--as",
                    identity,
                    "--limit",
                    "200",
                ]
            )
            fields = list(_record_list(result_data(field_result.payload), keys=("items", "fields")))
        return {
            "base_token": token,
            "base": result_data(base.payload),
            "tables": table_items,
            "selected_table": chosen,
            "fields": fields,
        }

    def list_base_records(
        self,
        *,
        base_token: str,
        table_id: str,
        identity: str = "user",
        view_id: str | None = None,
        field_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            args = [
                "base",
                "+record-list",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--offset",
                str(offset),
                "--limit",
                "200",
                "--format",
                "json",
                "--as",
                identity,
            ]
            if view_id:
                args.extend(["--view-id", view_id])
            for name in field_names or []:
                args.extend(["--field-id", name])
            page = self.run(args, timeout_seconds=120)
            data = result_data(page.payload)
            items = list(_record_list(data, keys=("items", "records")))
            records.extend(items)
            has_more = bool(first_value(data, "has_more", "hasMore"))
            if not has_more or not items:
                break
            next_offset = first_value(data, "next_offset", "offset")
            offset = int(next_offset) if next_offset not in (None, "") else offset + len(items)
            if offset > 2_000_000:
                raise LarkCliError("Base pagination exceeded the safety limit")
        return records

    def create_document(
        self,
        *,
        title: str,
        markdown: str,
        identity: str = "bot",
        folder_token: str | None = None,
        wiki_space: str | None = None,
        wiki_node: str | None = None,
        apply: bool,
    ) -> tuple[CommandResult, CommandResult | None]:
        with tempfile.TemporaryDirectory(prefix="people-tracking-feishu-doc-") as temporary:
            content = Path(temporary) / "report.md"
            content.write_text(markdown, encoding="utf-8")
            content.chmod(0o600)
            args = [
                "docs",
                "+create",
                "--api-version",
                "v2",
                "--title",
                title,
                "--markdown",
                f"@{content}",
                "--as",
                identity,
            ]
            if folder_token:
                args.extend(["--folder-token", folder_token])
            if wiki_space:
                args.extend(["--wiki-space", wiki_space])
            if wiki_node:
                args.extend(["--wiki-node", wiki_node])
            return self.write(args, apply=apply)

    def send_message(
        self,
        *,
        markdown: str,
        idempotency_key: str,
        target_kind: str,
        target_id: str,
        identity: str = "bot",
        apply: bool,
    ) -> tuple[CommandResult, CommandResult | None]:
        if target_kind not in {"chat", "user"}:
            raise LarkCliError("direct CLI messaging requires a chat or user target")
        args = [
            "im",
            "+messages-send",
            "--as",
            identity,
            "--markdown",
            markdown,
            "--idempotency-key",
            idempotency_key,
            f"--{target_kind}-id",
            target_id,
        ]
        return self.write(args, apply=apply)


def _record_list(value: Any, *, keys: tuple[str, ...]) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(value, dict):
        for key in keys:
            child = value.get(key)
            if isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        yield item
                return
        for child in value.values():
            found = list(_record_list(child, keys=keys))
            if found:
                yield from found
                return
