from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from people_intel.content_store import ContentAddressedStore
from people_intel.xhs_api import (
    FALLBACK_ERROR_CODES,
    RISK_ERROR_CODES,
    XhsApiCliTransport,
    XhsApiRouteCircuit,
    envelope_error,
)


@dataclass(frozen=True)
class ConnectorOutput:
    channel: str
    route: str
    status: str
    duration_ms: int
    request_summary: str
    raw_object_ref: str | None = None
    normalized_text: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ConnectorExecutor:
    """Allowlisted, read-only connectors used by Sandbox scan workers.

    X, WeChat, Xiaohongshu and funding discovery deliberately use different
    transports. The executor gives them one auditable output contract without
    pretending that a browser session, a search index and a licensed data API
    have the same authority.
    """

    def __init__(
        self,
        object_store: ContentAddressedStore,
        *,
        timeout_seconds: int = 45,
        max_bytes: int = 4_000_000,
        qiaomu_root: str | Path | None = None,
        xhs_root: str | Path | None = None,
        xhs_api_root: str | Path | None = None,
        xhs_api_home: str | Path | None = None,
        xhs_api_circuit_path: str | Path | None = None,
        twitter_bin: str | None = None,
        mcporter_bin: str | None = None,
        xhs_enabled: bool | None = None,
        xhs_api_enabled: bool | None = None,
        lock_root: str | Path | None = None,
    ):
        self.object_store = object_store
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.qiaomu_root = self._first_root(
            qiaomu_root or os.environ.get("PEOPLE_INTEL_QIAOMU_ROOT"),
            [
                Path.home() / ".codex/skills/qiaomu-markdown-proxy",
                Path.home() / ".claude/skills/qiaomu-markdown-proxy",
            ],
            required="scripts/fetch.sh",
        )
        self.xhs_root = self._first_root(
            xhs_root or os.environ.get("PEOPLE_INTEL_XHS_SKILL_ROOT"),
            [Path.home() / ".codex/skills/xiaohongshu-skills"],
            required="scripts/cli.py",
        )
        project_root = Path(__file__).resolve().parents[2]
        configured_xhs_api_root = xhs_api_root or os.environ.get("PEOPLE_INTEL_XHS_API_ROOT")
        default_xhs_api_root = project_root / ".people_intel" / "evals" / "jackwener-xiaohongshu-cli"
        self.xhs_api_root = self._first_root(
            configured_xhs_api_root,
            [default_xhs_api_root],
            required=".venv/bin/xhs",
        )
        self.twitter_bin = twitter_bin or shutil.which("twitter")
        self.mcporter_bin = mcporter_bin or shutil.which("mcporter")
        self.xhs_enabled = (
            xhs_enabled
            if xhs_enabled is not None
            else os.environ.get("PEOPLE_INTEL_XHS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        )
        self.xhs_api_enabled = (
            xhs_api_enabled
            if xhs_api_enabled is not None
            else os.environ.get("PEOPLE_INTEL_XHS_API_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        )
        self.xhs_account = os.environ.get("PEOPLE_INTEL_XHS_ACCOUNT", "").strip()
        self.xhs_detail_limit = max(0, min(3, int(os.environ.get("PEOPLE_INTEL_XHS_DETAIL_LIMIT", "1"))))
        self.xhs_timeout_seconds = max(60, min(180, int(os.environ.get("PEOPLE_INTEL_XHS_TIMEOUT_SECONDS", "120"))))
        project_root = Path(
            os.environ.get("PEOPLE_INTEL_PROJECT_ROOT", Path(__file__).resolve().parents[2])
        )
        self.wechat_node_script = project_root / "frontend" / "scripts" / "fetch-wechat.mjs"
        self.lock_root = Path(lock_root or os.environ.get(
            "PEOPLE_INTEL_CONNECTOR_LOCK_ROOT",
            self.object_store.root.parent / "connector-locks",
        ))
        self.lock_timeout_seconds = max(1, min(30, int(os.environ.get("PEOPLE_INTEL_CONNECTOR_LOCK_TIMEOUT_SECONDS", "5"))))
        api_home = Path(xhs_api_home or os.environ.get(
            "PEOPLE_INTEL_XHS_API_HOME",
            self.object_store.root.parent / "xhs-api-home",
        ))
        self.xhs_api = (
            XhsApiCliTransport(
                self.xhs_api_root,
                api_home,
                timeout_seconds=max(
                    30,
                    min(180, int(os.environ.get("PEOPLE_INTEL_XHS_API_TIMEOUT_SECONDS", "120"))),
                ),
                max_bytes=self.max_bytes,
            )
            if self.xhs_api_root
            else None
        )
        self.xhs_api_signature_cooldown_seconds = max(
            300,
            int(os.environ.get("PEOPLE_INTEL_XHS_API_SIGNATURE_COOLDOWN_SECONDS", "86400")),
        )
        self.xhs_api_risk_cooldown_seconds = max(
            3600,
            int(os.environ.get("PEOPLE_INTEL_XHS_API_RISK_COOLDOWN_SECONDS", "86400")),
        )
        self.xhs_api_circuit = XhsApiRouteCircuit(
            xhs_api_circuit_path
            or os.environ.get(
                "PEOPLE_INTEL_XHS_API_CIRCUIT_PATH",
                self.object_store.root.parent / "connector-state" / "xhs-api.json",
            )
        )

    def capabilities(self) -> list[dict[str, Any]]:
        qiaomu_wechat = bool(self.qiaomu_root and (self.qiaomu_root / "scripts" / "fetch_weixin.py").exists())
        xhs_cli = bool(self.xhs_root and (self.xhs_root / "scripts" / "cli.py").exists())
        xhs_bridge = bool(xhs_cli and self._xhs_bridge_ready())
        xhs_api_available = bool(self.xhs_api_enabled and self.xhs_api and self.xhs_api.available)
        xhs_api_credentials = bool(xhs_api_available and self.xhs_api and self.xhs_api.credentials_configured)
        xhs_api_circuit = self.xhs_api_circuit.read()
        itjuzi_api = bool(os.environ.get("PEOPLE_INTEL_ITJUZI_API_BASE") and os.environ.get("PEOPLE_INTEL_ITJUZI_API_TOKEN"))
        validation = self._validation_manifest()
        xhs_validation = validation.get("channels", {}).get("xhs", {})
        xhs_auth_validated = xhs_validation.get("authentication_status") == "passed"
        xhs_end_to_end_validated = xhs_validation.get("status") == "passed"
        xhs_gui_ready = bool(
            self.xhs_enabled
            and xhs_cli
            and xhs_bridge
            and xhs_auth_validated
            and xhs_end_to_end_validated
        )
        if self.xhs_api_enabled:
            xhs_selected_route = (
                "xhs_extension_bridge_fallback"
                if xhs_api_circuit.active and xhs_api_circuit.fallback_allowed
                else "xhs_api_cli"
            )
            xhs_ready = bool(
                (xhs_api_credentials and not xhs_api_circuit.active)
                or (xhs_api_circuit.active and xhs_api_circuit.fallback_allowed and xhs_gui_ready)
            )
            if xhs_api_credentials and not xhs_api_circuit.active:
                xhs_blocking_reason = None
            elif xhs_api_circuit.active and xhs_api_circuit.fallback_allowed and xhs_gui_ready:
                xhs_blocking_reason = None
            elif xhs_api_circuit.active and not xhs_api_circuit.fallback_allowed:
                xhs_blocking_reason = "签名 API 已因风控类错误暂停；不会自动转入 GUI，请先在浏览器完成人工验证。"
            elif xhs_api_available and not xhs_api_credentials:
                xhs_blocking_reason = "只读 API 尚未配置隔离 Cookie；不会自动从 Chrome 抽取登录态。"
            else:
                xhs_blocking_reason = "只读 API 与 Chrome Extension Bridge 均未就绪；不会执行搜索。"
        else:
            xhs_selected_route = "xhs_skill_cli"
            xhs_ready = xhs_gui_ready
            xhs_blocking_reason = (
                None
                if xhs_gui_ready
                else (
                    "Extension Bridge 已连接，但当前 Chrome Profile 尚未完成登录验收；不会执行自动搜索。"
                    if xhs_bridge
                    else "Chrome 尚未连接 XHS Bridge 扩展；不会执行搜索。"
                )
            )
        capabilities = [
            self._cap(
                "github",
                "github_cli" if shutil.which("gh") else "github_public_api",
                True,
                "已知公开账号可走 GitHub REST；配置 gh/token 后获得更高限额与搜索能力",
            ),
            self._cap("arxiv", "arxiv_api", True, "使用 arXiv 公共 API，不需要凭证"),
            self._cap(
                "homepage",
                "qiaomu_generic" if self.qiaomu_root else "builtin_public_http",
                True,
                "内置抓取器只允许公网 http(s)，校验 DNS/重定向/大小和媒体类型",
            ),
            self._cap(
                "news",
                "itjuzi_api" if itjuzi_api else "exa_itjuzi_public",
                bool(itjuzi_api or self.mcporter_bin),
                "优先 IT 桔子授权 API；未签约时使用 Exa 检索 IT 桔子公开页面并明确标记 public discovery",
                authority="licensed_aggregator" if itjuzi_api else "public_discovery",
                deployment_scope="cloud_api" if itjuzi_api else "cloud_public_discovery",
                credential_owner="secret_manager" if itjuzi_api else "none",
                blocking_reason_zh=None if itjuzi_api else "尚未配置 IT 桔子授权 API；当前只能稳定发现公开页面，不能读取会员字段。",
                setup_steps_zh=(
                    ["在部署 Secret Manager 设置 PEOPLE_INTEL_ITJUZI_API_BASE 与 PEOPLE_INTEL_ITJUZI_API_TOKEN", "重启 connector worker", "运行一次只读融资查询并核对 ConnectorAttempt"]
                    if not itjuzi_api else []
                ),
            ),
            self._cap(
                "x",
                "twitter_user_posts+exa_fallback",
                bool(self.twitter_bin and self.mcporter_bin),
                "已知账号走 twitter user-posts；关键词 search 失败时回退 Exa site:x.com",
                worker="credentialed_cli_worker",
                deployment_scope="persistent_local_worker",
                credential_owner="twitter_cli_profile",
                setup_steps_zh=["在常驻 worker 主机完成 twitter CLI 登录", "不要把 cookie 或密码传入飞书", "用 GET /v1/system/connectors 与一次只读 timeline 验收"],
            ),
            self._cap(
                "wechat",
                "exa_wechat+qiaomu_weixin",
                bool(self.mcporter_bin and qiaomu_wechat),
                "Exa 发现 mp.weixin.qq.com URL，qiaomu Playwright 读取已知文章全文",
                worker="headless_fetch_worker",
                deployment_scope="cloud_or_local_worker",
                credential_owner="none_for_public_articles",
                setup_steps_zh=["配置 Exa/agent-reach 发现路由", "保留 qiaomu 微信全文抓取脚本", "只有取得完整正文才创建 SourceDocumentVersion"],
            ),
            self._cap(
                "xhs",
                xhs_selected_route,
                xhs_ready,
                (
                    "优先使用隔离 HOME 的只读签名 API；仅签名错误熔断后由已验收的 Extension Bridge 在后续任务中兜底"
                    if self.xhs_api_enabled
                    else "使用已验收的 Chrome Extension Bridge 串行读取"
                ),
                worker="stateful_browser_worker",
                bridge_connected=xhs_bridge,
                authentication_status=(
                    "api_cookie_configured"
                    if xhs_api_credentials
                    else ("validated" if xhs_auth_validated else "required")
                ),
                deployment_scope="persistent_local_worker",
                credential_owner="isolated_xhs_api_home+chrome_profile_fallback",
                preferred_route="xhs_api_cli",
                fallback_route="xhs_extension_bridge",
                api_cli_available=xhs_api_available,
                api_credentials_configured=xhs_api_credentials,
                api_home=str(self.xhs_api.home) if self.xhs_api else None,
                circuit=xhs_api_circuit.public_dict(),
                extension_name="XHS Bridge",
                extension_path=str(self.xhs_root / "extension") if self.xhs_root else None,
                bridge_url="ws://localhost:9333",
                blocking_reason_zh=xhs_blocking_reason,
                setup_steps_zh=[
                    "设置 PEOPLE_INTEL_XHS_API_ENABLED=true、PEOPLE_INTEL_XHS_API_ROOT 与独立 PEOPLE_INTEL_XHS_API_HOME",
                    "显式运行 people-intel xhs-api-bootstrap --cookie-source chrome；运行期不会自动读取浏览器 Cookie",
                    "保留 Chrome 中的 XHS Bridge 扩展作为签名错误后的后续任务兜底",
                    "用 GET /v1/system/connectors 核对 preferred_route、circuit 和 fallback_route",
                ],
            ),
        ]
        for capability in capabilities:
            item = validation.get("channels", {}).get(capability["channel"], {})
            if item:
                capability["last_validation_status"] = item.get("status")
                capability["last_validated_at"] = validation.get("validated_at")
                capability["last_validation_route"] = item.get("route")
                capability["last_validation_detail_zh"] = item.get("detail_zh")
        return capabilities

    @staticmethod
    def _cap(
        channel: str,
        route: str,
        ready: bool,
        requirement_zh: str,
        **metadata: Any,
    ) -> dict[str, Any]:
        return {
            "channel": channel,
            "route": route,
            "status": "ready" if ready else "unconfigured",
            "requirement_zh": requirement_zh,
            "writes_to": "sandbox_only",
            **metadata,
        }

    def execute(
        self,
        channel: str,
        *,
        query: str,
        source_uri: str | None = None,
        max_results: int = 5,
    ) -> ConnectorOutput:
        started = time.monotonic()
        max_results = max(1, min(20, max_results))
        try:
            if channel in {"x", "xhs"}:
                with self._stateful_channel_lock(channel):
                    return self._execute_channel(channel, query, source_uri, started, max_results)
            return self._execute_channel(channel, query, source_uri, started, max_results)
        except (OSError, subprocess.SubprocessError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
            return ConnectorOutput(
                channel=channel,
                route=self._route(channel),
                status="failed",
                duration_ms=self._elapsed(started),
                request_summary=self._summary(query),
                error=self._redact(str(exc))[:500],
            )

    def _execute_channel(
        self,
        channel: str,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        if channel == "homepage":
            return self._homepage(source_uri or query, started)
        if channel == "github":
            return self._github(query, source_uri, started, max_results)
        if channel == "arxiv":
            return self._arxiv(query, started, max_results)
        if channel == "x":
            return self._x(query, source_uri, started, max_results)
        if channel == "wechat":
            return self._wechat(query, source_uri, started, max_results)
        if channel == "xhs":
            return self._xhs(query, source_uri, started, max_results)
        if channel == "news":
            return self._funding_news(query, source_uri, started, max_results)
        return self._unconfigured(channel, query, started)

    @contextmanager
    def _stateful_channel_lock(self, channel: str):
        account = self.xhs_account if channel == "xhs" and self.xhs_account else "default"
        safe_account = re.sub(r"[^a-zA-Z0-9_.-]+", "_", account)[:80]
        path = self.lock_root / f"{channel}-{safe_account}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + self.lock_timeout_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"stateful connector lock busy: {channel}/{safe_account}")
                    time.sleep(0.25)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _homepage(self, url: str, started: float) -> ConnectorOutput:
        self._validate_public_url(url)
        if self.qiaomu_root:
            output = self._run(
                ["bash", str(self.qiaomu_root / "scripts" / "fetch.sh"), url]
            )
            route = "qiaomu_generic"
        else:
            output = self._fetch_public_page(url)
            route = "builtin_public_http"
        stored = self.object_store.put_bytes(output)
        return ConnectorOutput(
            channel="homepage",
            route=route,
            status="completed",
            duration_ms=self._elapsed(started),
            request_summary=url,
            raw_object_ref=stored.object_ref,
            normalized_text=output.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("homepage connector accepts only credential-free http(s) URLs")
        if parsed.port not in {None, 80, 443}:
            raise ValueError("homepage connector permits only ports 80 and 443")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ValueError("homepage hostname did not resolve") from exc
        if not addresses:
            raise ValueError("homepage hostname did not resolve")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if not address.is_global:
                raise ValueError("homepage URL resolved to a non-public address")

    def _fetch_public_page(self, url: str) -> bytes:
        executor = self

        class SafeRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                executor._validate_public_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "people-intel-public-homepage-monitor/1",
                "Accept": "text/html,text/plain;q=0.8",
            },
        )
        opener = urllib.request.build_opener(SafeRedirect())
        with opener.open(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                raise ValueError(f"unsupported homepage media type: {content_type}")
            output = response.read(self.max_bytes + 1)
        if len(output) > self.max_bytes:
            raise ValueError("homepage response exceeded output limit")
        return output

    def _github(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        direct = self._github_target(source_uri)
        if direct:
            login, repository = direct
            gh = shutil.which("gh")
            if gh:
                profile_raw = self._run([gh, "api", f"users/{login}"])
            else:
                profile_raw = self._github_public_get(f"users/{login}")
            profile = json.loads(profile_raw)
            if repository:
                repo_raw = (
                    self._run([gh, "api", f"repos/{login}/{repository}"])
                    if gh
                    else self._github_public_get(f"repos/{login}/{repository}")
                )
                repositories = [json.loads(repo_raw)]
            else:
                endpoint = (
                    f"users/{login}/repos?per_page={max_results}"
                    "&sort=pushed&direction=desc"
                )
                repos_raw = (
                    self._run([gh, "api", endpoint])
                    if gh
                    else self._github_public_get(endpoint)
                )
                repositories = json.loads(repos_raw)
                repo_raw = repos_raw
            raw_payload = {"profile": profile, "repositories": repositories}
            stored = self.object_store.put_bytes(self._json_bytes(raw_payload))
            results = [self._github_profile_result(profile)]
            results.extend(self._github_repo_result(item, profile) for item in repositories[:max_results])
            return ConnectorOutput(
                channel="github",
                route=(
                    "github_account_api"
                    if gh and not repository
                    else "github_repository_api"
                    if gh
                    else "github_public_rest"
                ),
                status="completed",
                duration_ms=self._elapsed(started),
                request_summary=source_uri or login,
                raw_object_ref=stored.object_ref,
                normalized_text=json.dumps(results, ensure_ascii=False, indent=2),
                results=results,
                metadata={
                    "known_account": not repository,
                    "account_login": profile.get("login"),
                    "account_id": profile.get("id"),
                    "account_node_id": profile.get("node_id"),
                    "checkpoint_fields": ["repo_node_id", "pushed_at", "default_branch_sha"],
                },
            )
        if not shutil.which("gh"):
            return self._unconfigured("github", query, started)
        safe_query = self._summary(query, 300)
        output = self._run([
            "gh", "search", "repos", safe_query, "--limit", str(max_results),
            "--json", "fullName,url,description,updatedAt",
        ])
        stored = self.object_store.put_bytes(output)
        values = json.loads(output)
        results = [
            {
                "title": item.get("fullName", "GitHub result"),
                "uri": item.get("url", ""),
                "published_at": item.get("updatedAt"),
                "summary": item.get("description"),
                "content": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "owner_login": str(item.get("fullName") or "").split("/", 1)[0] or None,
                "result_kind": "repository_candidate",
            }
            for item in values
        ]
        return ConnectorOutput(
            channel="github", route="github_cli", status="completed",
            duration_ms=self._elapsed(started), request_summary=safe_query,
            raw_object_ref=stored.object_ref, normalized_text=output.decode(), results=results,
        )

    def _github_public_get(self, endpoint: str) -> bytes:
        request = urllib.request.Request(
            f"https://api.github.com/{endpoint}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "people-intel-public-github-monitor/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            output = response.read(self.max_bytes + 1)
        if len(output) > self.max_bytes:
            raise ValueError("GitHub response exceeded output limit")
        return output

    def _arxiv(self, query: str, started: float, max_results: int) -> ConnectorOutput:
        safe_query = self._summary(query, 300)
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": f"all:{safe_query}", "start": 0, "max_results": max_results}
        )
        request = urllib.request.Request(url, headers={"User-Agent": "people-intel-demo/0.3"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            output = response.read(self.max_bytes + 1)
        if len(output) > self.max_bytes:
            raise ValueError("arXiv response exceeded output limit")
        stored = self.object_store.put_bytes(output)
        root = ET.fromstring(output)
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        results = [
            {
                "title": (entry.findtext("atom:title", "", namespace) or "").strip(),
                "uri": entry.findtext("atom:id", "", namespace),
                "published_at": entry.findtext("atom:published", None, namespace),
            }
            for entry in root.findall("atom:entry", namespace)
        ]
        return ConnectorOutput(
            channel="arxiv", route="arxiv_api", status="completed",
            duration_ms=self._elapsed(started), request_summary=safe_query,
            raw_object_ref=stored.object_ref,
            normalized_text=output.decode("utf-8", errors="replace"), results=results,
        )

    def _x(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        if not self.twitter_bin:
            return self._unconfigured("x", query, started)
        target = source_uri or query.strip()
        handle: str | None = None
        profile_payload: dict[str, Any] | None = None
        parsed = urllib.parse.urlparse(target if "://" in target else "")
        if parsed.netloc.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and "/status/" in parsed.path:
            payload = self._run_json([self.twitter_bin, "tweet", target, "--json"])
            route = "twitter_tweet"
        else:
            handle = self._x_handle(target)
            if handle:
                if source_uri:
                    profile_payload = self._run_json([self.twitter_bin, "user", handle, "--json"])
                    if not profile_payload.get("ok"):
                        raise ValueError((profile_payload.get("error") or {}).get("message") or "twitter user profile failed")
                payload = self._run_json([self.twitter_bin, "user-posts", handle, "-n", str(max_results), "--json"])
                route = "twitter_user_posts"
            else:
                try:
                    payload = self._run_json([
                        self.twitter_bin, "search", self._summary(query, 300),
                        "--type", "latest", "-n", str(max_results), "--json",
                    ])
                    if not payload.get("ok"):
                        raise ValueError((payload.get("error") or {}).get("message") or "twitter search failed")
                    route = "twitter_search"
                except (ValueError, subprocess.SubprocessError):
                    return self._exa_output(
                        channel="x", route="exa_x_fallback",
                        query=f"site:x.com {self._summary(query, 260)}",
                        started=started, max_results=max_results,
                        include_domains=["x.com"],
                    )
        if not payload.get("ok"):
            raise ValueError((payload.get("error") or {}).get("message") or "twitter request failed")
        values = payload.get("tweets") or payload.get("data") or []
        if isinstance(values, dict):
            values = [values]
        raw_payload = {"profile": profile_payload, "timeline": payload} if profile_payload is not None else payload
        raw = self._json_bytes(raw_payload)
        stored = self.object_store.put_bytes(raw)
        results = [self._tweet_result(item) for item in values[:max_results]]
        account_identity = self._x_profile_identity(profile_payload)
        if account_identity:
            for result in results:
                result["tracked_account_handle"] = account_identity.get("username") or handle
                result["tracked_account_id"] = account_identity.get("platform_user_id")
                result["tracked_account_display_name"] = account_identity.get("display_name")
                result["observed_in_timeline_of"] = handle
                result["content_relationship"] = (
                    "retweeted"
                    if result.get("is_retweet")
                    else (
                        "authored"
                        if str(result.get("author_handle") or "").casefold()
                        == str(handle or "").casefold()
                        else "timeline_related"
                    )
                )
        return ConnectorOutput(
            channel="x", route=route, status="completed",
            duration_ms=self._elapsed(started), request_summary=self._summary(target),
            raw_object_ref=stored.object_ref,
            normalized_text=json.dumps(results, ensure_ascii=False, indent=2), results=results,
            metadata={
                "known_account": bool(source_uri and handle),
                "account_identity": account_identity,
                "checkpoint_fields": ["newest_post_id", "newest_created_at"],
            },
        )

    def _wechat(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        if source_uri:
            parsed = urllib.parse.urlparse(source_uri)
            if parsed.netloc.lower() != "mp.weixin.qq.com":
                raise ValueError("wechat source_uri must use mp.weixin.qq.com")
            if not self.qiaomu_root:
                return self._unconfigured("wechat", query, started)
            script = self.qiaomu_root / "scripts" / "fetch_weixin.py"
            payload = self._run_json([shutil.which("python3") or "python3", str(script), source_uri, "--json"])
            route = "qiaomu_weixin"
            if payload.get("error") and "playwright not installed" in str(payload["error"]).lower():
                node = shutil.which("node")
                if node and self.wechat_node_script.exists():
                    payload = self._run_json([node, str(self.wechat_node_script), source_uri])
                    route = "project_playwright_weixin_fallback"
            if payload.get("error"):
                raise ValueError(payload["error"])
            raw = self._json_bytes(payload)
            stored = self.object_store.put_bytes(raw)
            normalized = self._wechat_markdown(payload)
            return ConnectorOutput(
                channel="wechat", route=route, status="completed",
                duration_ms=self._elapsed(started), request_summary=source_uri,
                raw_object_ref=stored.object_ref, normalized_text=normalized,
                results=[{
                    "title": payload.get("title") or "微信公众号文章",
                    "uri": source_uri,
                    "published_at": payload.get("publish_time") or None,
                    "author": payload.get("author") or None,
                    "content": payload.get("content") or "",
                }],
            )
        return self._exa_output(
            channel="wechat", route="exa_wechat_discovery",
            query=self._summary(query, 300), started=started,
            max_results=max_results, include_domains=["mp.weixin.qq.com"],
        )

    def _xhs(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        if self.xhs_api_enabled and self.xhs_api:
            circuit = self.xhs_api_circuit.read()
            if circuit.active:
                if circuit.fallback_allowed:
                    return self._xhs_gui(
                        query,
                        started,
                        max_results,
                        fallback_reason=circuit.reason or "api_circuit_open",
                        circuit=circuit.public_dict(),
                    )
                return ConnectorOutput(
                    channel="xhs",
                    route="xhs_api_cli",
                    status="failed",
                    duration_ms=self._elapsed(started),
                    request_summary=self._summary(query),
                    error="XHS API route is paused after a risk-control response; human browser verification is required",
                    metadata={
                        "error_code": circuit.reason,
                        "circuit": circuit.public_dict(),
                        "fallback_allowed": False,
                    },
                )
            if not self.xhs_api.available:
                return ConnectorOutput(
                    channel="xhs",
                    route="xhs_api_cli",
                    status="unconfigured",
                    duration_ms=self._elapsed(started),
                    request_summary=self._summary(query),
                    error="xiaohongshu-cli executable is unavailable",
                    metadata={"preferred_route": "xhs_api_cli", "fallback_route": "xhs_extension_bridge"},
                )
            if not self.xhs_api.credentials_configured:
                return ConnectorOutput(
                    channel="xhs",
                    route="xhs_api_cli",
                    status="unconfigured",
                    duration_ms=self._elapsed(started),
                    request_summary=self._summary(query),
                    error="isolated xiaohongshu-cli credentials are not configured; run xhs-api-bootstrap explicitly",
                    metadata={"preferred_route": "xhs_api_cli", "fallback_route": "xhs_extension_bridge"},
                )
            return self._xhs_api(query, source_uri, started, max_results)
        return self._xhs_gui(query, started, max_results)

    def _xhs_api(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        assert self.xhs_api is not None
        user_id = self._xhs_user_id(source_uri)
        if user_id:
            profile = self.xhs_api.run("user", user_id)
            profile_error = envelope_error(profile)
            if profile_error:
                code, message = profile_error
                if code in FALLBACK_ERROR_CODES or code in RISK_ERROR_CODES:
                    return self._xhs_api_error(query, started, {"profile": profile}, code, message)
                return self._xhs_known_account_search_fallback(
                    query=query,
                    user_id=user_id,
                    started=started,
                    max_results=max_results,
                    failed_payloads={"profile": profile},
                    degraded_from=["user"],
                    original_error_codes=[code],
                )
            posts = self.xhs_api.run("user-posts", user_id)
            posts_error = envelope_error(posts)
            if posts_error:
                code, message = posts_error
                if code in FALLBACK_ERROR_CODES or code in RISK_ERROR_CODES:
                    return self._xhs_api_error(
                        query,
                        started,
                        {"profile": profile, "posts": posts},
                        code,
                        message,
                    )
                return self._xhs_known_account_search_fallback(
                    query=query,
                    user_id=user_id,
                    started=started,
                    max_results=max_results,
                    failed_payloads={"profile": profile, "posts": posts},
                    degraded_from=["user-posts"],
                    original_error_codes=[code],
                    profile=profile,
                )
            items = self._xhs_payload_items(posts)[:max_results]
            identity = self._xhs_profile_identity(profile, user_id)
            results = [self._xhs_api_result(item, None) for item in items]
            for result in results:
                result["author_id"] = result.get("author_id") or user_id
                result["author"] = result.get("author") or identity.get("display_name")
            raw_payload = {"profile": profile, "posts": posts}
            stored = self.object_store.put_bytes(self._json_bytes(raw_payload))
            self.xhs_api_circuit.close()
            return ConnectorOutput(
                channel="xhs",
                route="xhs_user_posts_cli",
                status="completed",
                duration_ms=self._elapsed(started),
                request_summary=user_id,
                raw_object_ref=stored.object_ref,
                normalized_text=json.dumps(results, ensure_ascii=False, indent=2),
                results=results,
                metadata={
                    "known_account": True,
                    "account_identity": identity,
                    "read_only": True,
                    "preferred_route": "xhs_api_cli",
                    "checkpoint_fields": ["newest_note_id", "newest_last_update_time", "pagination_cursor"],
                    "circuit": self.xhs_api_circuit.read().public_dict(),
                },
            )
        search = self.xhs_api.run("search", self._summary(query, 160), "--sort", "latest")
        search_error = envelope_error(search)
        if search_error:
            return self._xhs_api_error(query, started, search, *search_error)

        data = search.get("data") if isinstance(search.get("data"), dict) else {}
        items = [item for item in data.get("items", []) if isinstance(item, dict)][:max_results]
        detail_payloads: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        partial_error_codes: list[str] = []
        for index, item in enumerate(items):
            detail: dict[str, Any] | None = None
            if index < self.xhs_detail_limit:
                detail = self.xhs_api.run("read", str(index + 1))
                detail_payloads.append(detail)
                detail_error = envelope_error(detail)
                if detail_error:
                    code, message = detail_error
                    if code in FALLBACK_ERROR_CODES or code in RISK_ERROR_CODES:
                        raw_payload = {"search": search, "details": detail_payloads}
                        return self._xhs_api_error(
                            query,
                            started,
                            raw_payload,
                            code,
                            message,
                        )
                    partial_error_codes.append(code)
                    detail = None
            results.append(self._xhs_api_result(item, detail))

        raw_payload = {"search": search, "details": detail_payloads}
        stored = self.object_store.put_bytes(self._json_bytes(raw_payload))
        self.xhs_api_circuit.close()
        return ConnectorOutput(
            channel="xhs",
            route="xhs_api_cli",
            status="completed",
            duration_ms=self._elapsed(started),
            request_summary=self._summary(query),
            raw_object_ref=stored.object_ref,
            normalized_text=json.dumps(results, ensure_ascii=False, indent=2),
            results=results,
            metadata={
                "preferred_route": "xhs_api_cli",
                "fallback_route": "xhs_extension_bridge",
                "read_only": True,
                "detail_limit": self.xhs_detail_limit,
                "partial_error_codes": partial_error_codes,
                "circuit": self.xhs_api_circuit.read().public_dict(),
            },
        )

    def _xhs_known_account_search_fallback(
        self,
        *,
        query: str,
        user_id: str,
        started: float,
        max_results: int,
        failed_payloads: dict[str, Any],
        degraded_from: list[str],
        original_error_codes: list[str],
        profile: dict[str, Any] | None = None,
    ) -> ConnectorOutput:
        """Recover a known XHS account without weakening the identity boundary.

        The reverse-engineered ``user`` endpoints are less stable than search
        and note detail. A broad search is therefore acceptable only when every
        selected result is filtered by the already registered stable user_id.
        Search rank or nickname similarity can never substitute for that ID.
        """
        assert self.xhs_api is not None
        fallback_query = self._summary(query, 160)
        search = self.xhs_api.run("search", fallback_query, "--sort", "general")
        search_error = envelope_error(search)
        if search_error:
            raw_payload = {**failed_payloads, "fallback_search": search}
            return self._xhs_api_error(query, started, raw_payload, *search_error)

        items = self._xhs_payload_items(search)
        matched: list[tuple[int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            summary = self._xhs_api_result(item, None)
            if str(summary.get("author_id") or "") == user_id:
                matched.append((index, item))
                if len(matched) >= max_results:
                    break

        details: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        partial_error_codes: list[str] = []
        for index, item in matched:
            detail = self.xhs_api.run("read", str(index + 1))
            details.append(detail)
            detail_error = envelope_error(detail)
            if detail_error:
                code, message = detail_error
                if code in FALLBACK_ERROR_CODES or code in RISK_ERROR_CODES:
                    raw_payload = {
                        **failed_payloads,
                        "fallback_search": search,
                        "details": details,
                    }
                    return self._xhs_api_error(query, started, raw_payload, code, message)
                partial_error_codes.append(code)
                detail = None
            result = self._xhs_api_result(item, detail)
            # Recheck detail attribution: a malformed or redirected detail
            # response must not escape the stable-ID filter.
            if str(result.get("author_id") or "") != user_id:
                continue
            result["tracked_account_id"] = user_id
            result["content_relationship"] = "authored"
            result["identity_resolution"] = "known_user_id_filter"
            results.append(result)

        identity = self._xhs_profile_identity(profile or {}, user_id)
        if results and not identity.get("display_name"):
            identity["username"] = results[0].get("author")
            identity["display_name"] = results[0].get("author")
        raw_payload = {
            **failed_payloads,
            "fallback_search": search,
            "details": details,
        }
        stored = self.object_store.put_bytes(self._json_bytes(raw_payload))
        self.xhs_api_circuit.close()
        return ConnectorOutput(
            channel="xhs",
            route="xhs_known_account_search_fallback",
            status="completed",
            duration_ms=self._elapsed(started),
            request_summary=fallback_query,
            raw_object_ref=stored.object_ref,
            normalized_text=json.dumps(results, ensure_ascii=False, indent=2),
            results=results,
            metadata={
                "known_account": True,
                "account_identity": identity,
                "read_only": True,
                "preferred_route": "xhs_api_cli",
                "degraded_from": degraded_from,
                "identity_resolution": "known_user_id_filter",
                "target_user_id": user_id,
                "search_candidate_count": len(items),
                "matched_candidate_count": len(matched),
                "coverage_gap": not bool(results),
                "original_error_codes": original_error_codes,
                "partial_error_codes": partial_error_codes,
                "checkpoint_fields": ["newest_note_id", "newest_last_update_time"],
                "circuit": self.xhs_api_circuit.read().public_dict(),
            },
        )

    def _xhs_api_error(
        self,
        query: str,
        started: float,
        payload: dict[str, Any],
        code: str,
        message: str,
    ) -> ConnectorOutput:
        stored = self.object_store.put_bytes(self._json_bytes(payload))
        if code in FALLBACK_ERROR_CODES:
            circuit = self.xhs_api_circuit.open(
                reason=code,
                cooldown_seconds=self.xhs_api_signature_cooldown_seconds,
                fallback_allowed=True,
            )
            next_action = "API route circuit opened; the next scheduled job may use the Extension Bridge fallback"
        elif code in RISK_ERROR_CODES:
            circuit = self.xhs_api_circuit.open(
                reason=code,
                cooldown_seconds=self.xhs_api_risk_cooldown_seconds,
                fallback_allowed=False,
            )
            next_action = "risk-control circuit opened; automatic GUI fallback is blocked pending human verification"
        else:
            circuit = self.xhs_api_circuit.read()
            next_action = "the API attempt remains retryable under the normal connector policy"
        return ConnectorOutput(
            channel="xhs",
            route="xhs_api_cli",
            status="unconfigured" if code == "not_authenticated" else "failed",
            duration_ms=self._elapsed(started),
            request_summary=self._summary(query),
            raw_object_ref=stored.object_ref,
            error=f"{code}: {self._redact(message)[:400]}; {next_action}",
            metadata={
                "error_code": code,
                "preferred_route": "xhs_api_cli",
                "fallback_route": "xhs_extension_bridge",
                "fallback_next_run": code in FALLBACK_ERROR_CODES,
                "fallback_allowed": code in FALLBACK_ERROR_CODES,
                "circuit": circuit.public_dict(),
            },
        )

    @staticmethod
    def _xhs_api_result(item: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
        summary_card = item.get("note_card") if isinstance(item.get("note_card"), dict) else {}
        detail_data = detail.get("data") if detail and isinstance(detail.get("data"), dict) else {}
        detail_items = detail_data.get("items") if isinstance(detail_data.get("items"), list) else []
        detail_item = detail_items[0] if detail_items and isinstance(detail_items[0], dict) else {}
        detail_card = detail_item.get("note_card") if isinstance(detail_item.get("note_card"), dict) else {}
        card = detail_card or summary_card or item
        user = card.get("user") if isinstance(card.get("user"), dict) else {}
        interact = card.get("interact_info") if isinstance(card.get("interact_info"), dict) else {}
        note_id = str(item.get("id") or card.get("note_id") or "")
        return {
            "title": card.get("title") or card.get("display_title") or "小红书笔记",
            "uri": f"https://www.xiaohongshu.com/explore/{note_id}",
            "published_at": card.get("time") or card.get("last_update_time") or item.get("time"),
            "author": user.get("nickname"),
            "author_id": user.get("user_id"),
            "content": card.get("desc") if detail_card else None,
            "metrics": interact,
            "detail_fetched": bool(detail_card),
        }

    def _xhs_gui(
        self,
        query: str,
        started: float,
        max_results: int,
        *,
        fallback_reason: str | None = None,
        circuit: dict[str, Any] | None = None,
    ) -> ConnectorOutput:
        if not self.xhs_enabled or not self.xhs_root:
            return ConnectorOutput(
                channel="xhs",
                route="xhs_extension_bridge_fallback" if fallback_reason else "xhs_skill_cli",
                status="unconfigured",
                duration_ms=self._elapsed(started),
                request_summary=self._summary(query),
                error="XHS Extension Bridge fallback is not configured",
                metadata={
                    "fallback_reason": fallback_reason,
                    "circuit": circuit or self.xhs_api_circuit.read().public_dict(),
                },
            )
        if not self._xhs_bridge_ready():
            return ConnectorOutput(
                channel="xhs",
                route="xhs_extension_bridge_fallback" if fallback_reason else "xhs_extension_bridge",
                status="unconfigured",
                duration_ms=self._elapsed(started),
                request_summary=self._summary(query),
                error="XHS Bridge extension is not connected; install/enable the unpacked extension before retrying",
                metadata={
                    "fallback_reason": fallback_reason,
                    "circuit": circuit or self.xhs_api_circuit.read().public_dict(),
                },
            )
        # The production Extension Bridge is allowed to touch XHS only after a
        # human has explicitly completed the login check. This gate prevents a
        # cron/Feishu job from repeatedly opening an unauthenticated page and
        # triggering platform risk controls. Lightweight test fixtures without
        # an extension manifest exercise normalization independently.
        if (self.xhs_root / "extension" / "manifest.json").exists():
            xhs_validation = self._validation_manifest().get("channels", {}).get("xhs", {})
            if xhs_validation.get("authentication_status") != "passed":
                return ConnectorOutput(
                    channel="xhs",
                    route="xhs_extension_bridge_fallback" if fallback_reason else "xhs_extension_bridge",
                    status="unconfigured",
                    duration_ms=self._elapsed(started),
                    request_summary=self._summary(query),
                    error="XHS Chrome Profile login has not passed explicit human validation; automatic search is blocked",
                    metadata={
                        "fallback_reason": fallback_reason,
                        "circuit": circuit or self.xhs_api_circuit.read().public_dict(),
                    },
                )
        command = self._xhs_command("search-feeds", "--keyword", self._summary(query, 160), "--sort-by", "最新")
        payload = self._run_json(command, timeout=max(self.timeout_seconds, self.xhs_timeout_seconds))
        feeds = [item for item in payload.get("feeds", []) if item.get("modelType") == "note"][:max_results]
        detail_payloads: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for index, item in enumerate(feeds):
            detail: dict[str, Any] | None = None
            if index < self.xhs_detail_limit and item.get("id") and item.get("xsecToken"):
                detail = self._run_json(
                    self._xhs_command(
                        "get-feed-detail", "--feed-id", str(item["id"]),
                        "--xsec-token", str(item["xsecToken"]),
                    ),
                    timeout=max(self.timeout_seconds, self.xhs_timeout_seconds),
                )
                detail_payloads.append(detail)
            note = (detail or {}).get("note") or {}
            user = note.get("user") or item.get("user") or {}
            note_id = str(note.get("noteId") or item.get("id") or "")
            results.append({
                "title": note.get("title") or item.get("displayTitle") or "小红书笔记",
                "uri": f"https://www.xiaohongshu.com/explore/{note_id}",
                "published_at": note.get("time"),
                "author": user.get("nickname"),
                "author_id": user.get("userId"),
                "content": note.get("desc"),
                "metrics": note.get("interactInfo") or item.get("interactInfo") or {},
                "detail_fetched": detail is not None,
            })
        raw_payload = {"search": payload, "details": detail_payloads}
        stored = self.object_store.put_bytes(self._json_bytes(raw_payload))
        return ConnectorOutput(
            channel="xhs",
            route="xhs_extension_bridge_fallback" if fallback_reason else "xhs_skill_cli",
            status="completed",
            duration_ms=self._elapsed(started), request_summary=self._summary(query),
            raw_object_ref=stored.object_ref,
            normalized_text=json.dumps(results, ensure_ascii=False, indent=2), results=results,
            metadata={
                "preferred_route": "xhs_api_cli",
                "fallback_route": "xhs_extension_bridge",
                "fallback_reason": fallback_reason,
                "circuit": circuit or self.xhs_api_circuit.read().public_dict(),
            },
        )

    def _funding_news(
        self,
        query: str,
        source_uri: str | None,
        started: float,
        max_results: int,
    ) -> ConnectorOutput:
        if source_uri:
            fetched = self._homepage(source_uri, started)
            return ConnectorOutput(
                channel="news",
                route="qiaomu_news_source",
                status=fetched.status,
                duration_ms=fetched.duration_ms,
                request_summary=fetched.request_summary,
                raw_object_ref=fetched.raw_object_ref,
                normalized_text=fetched.normalized_text,
                results=[{
                    "title": self._summary(query) or "融资来源原文",
                    "uri": source_uri,
                    "content": fetched.normalized_text or "",
                    "provider": "selected_public_source",
                }] if fetched.status == "completed" else [],
                error=fetched.error,
            )
        if os.environ.get("PEOPLE_INTEL_ITJUZI_API_BASE") and os.environ.get("PEOPLE_INTEL_ITJUZI_API_TOKEN"):
            return self._itjuzi_api(query, started, max_results)
        return self._exa_output(
            channel="news", route="exa_itjuzi_public",
            query=self._summary(query, 300), started=started,
            max_results=max_results,
            include_domains=["itjuzi.com", "aboutus.itjuzi.com", "cdn.itjuzi.com"],
        )

    def _itjuzi_api(self, query: str, started: float, max_results: int) -> ConnectorOutput:
        """Licensed IT Juzi API boundary.

        The exact endpoint and response mapping are intentionally configurable:
        they must be filled from the user's signed data-service documentation,
        never reverse engineered from a consumer website.
        """
        base = os.environ["PEOPLE_INTEL_ITJUZI_API_BASE"].rstrip("/")
        endpoint = os.environ.get("PEOPLE_INTEL_ITJUZI_SEARCH_PATH", "/search")
        url = f"{base}{endpoint}?" + urllib.parse.urlencode({"q": query, "limit": max_results})
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {os.environ['PEOPLE_INTEL_ITJUZI_API_TOKEN']}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("IT Juzi response exceeded output limit")
        payload = json.loads(raw)
        values = payload.get("items") or payload.get("data") or []
        results = [
            {
                "title": item.get("title") or item.get("company_name") or "IT 桔子融资事件",
                "uri": item.get("url") or item.get("source_url") or "",
                "published_at": item.get("published_at") or item.get("date"),
                "provider_record_id": item.get("id"),
                "provider": "itjuzi_licensed_api",
                "content": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "payload": item,
            }
            for item in values[:max_results]
        ]
        stored = self.object_store.put_bytes(raw)
        return ConnectorOutput(
            channel="news", route="itjuzi_api", status="completed",
            duration_ms=self._elapsed(started), request_summary=self._summary(query),
            raw_object_ref=stored.object_ref,
            normalized_text=json.dumps(results, ensure_ascii=False, indent=2), results=results,
        )

    def _exa_output(
        self,
        *,
        channel: str,
        route: str,
        query: str,
        started: float,
        max_results: int,
        include_domains: list[str] | None = None,
    ) -> ConnectorOutput:
        if not self.mcporter_bin:
            return self._unconfigured(channel, query, started)
        args: dict[str, Any] = {"query": query, "numResults": max_results}
        if include_domains:
            args["includeDomains"] = include_domains
            args.update({
                "textMaxCharacters": 800,
                "enableHighlights": True,
                "highlightsMaxCharacters": 600,
            })
        tool_name = "exa.web_search_advanced_exa" if include_domains else "exa.web_search_exa"
        payload = self._run_json([
            self.mcporter_bin, "call", tool_name,
            "--args", json.dumps(args, ensure_ascii=False), "--output", "json",
        ])
        raw = self._json_bytes(payload)
        stored = self.object_store.put_bytes(raw)
        if isinstance(payload.get("results"), list):
            results = self._parse_exa_results(payload["results"])
            text = json.dumps(results, ensure_ascii=False, indent=2)
        else:
            text = "\n".join(
                item.get("text", "") for item in payload.get("content", []) if item.get("type") == "text"
            )
            results = self._parse_exa_text(text)
        if include_domains:
            allowed = {domain.lower() for domain in include_domains}
            results = [item for item in results if self._host_is_allowed(str(item.get("uri") or ""), allowed)]
        results = results[:max_results]
        return ConnectorOutput(
            channel=channel, route=route, status="completed",
            duration_ms=self._elapsed(started), request_summary=self._summary(query),
            raw_object_ref=stored.object_ref, normalized_text=text, results=results,
        )

    def _xhs_command(self, *args: str) -> list[str]:
        assert self.xhs_root is not None
        python = self.xhs_root / ".venv" / "bin" / "python"
        command = [str(python if python.exists() else (shutil.which("python3") or "python3")), str(self.xhs_root / "scripts" / "cli.py")]
        command.extend(args)
        return command

    def _xhs_bridge_ready(self) -> bool:
        if not self.xhs_root:
            return False
        # Test fixtures and legacy installations do not expose the Extension
        # Bridge marker; their transport behavior is exercised independently.
        if not (self.xhs_root / "extension" / "manifest.json").exists():
            return True
        try:
            payload = self._run_json(self._xhs_command("bridge-status"), timeout=8)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return bool(payload.get("server_running") and payload.get("extension_connected"))

    def _unconfigured(self, channel: str, query: str, started: float) -> ConnectorOutput:
        return ConnectorOutput(
            channel=channel, route=self._route(channel), status="unconfigured",
            duration_ms=self._elapsed(started), request_summary=self._summary(query),
            error="连接器未配置；没有模拟成功结果",
        )

    def _run(self, command: list[str], *, timeout: int | None = None) -> bytes:
        result = subprocess.run(
            command, shell=False, check=False, capture_output=True,
            timeout=timeout or self.timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
            # CalledProcessError.__str__ drops stderr, which made durable retry
            # records useless for diagnosing browser/login failures. Preserve a
            # bounded, redacted message while never logging the full command
            # response or credentials.
            raise ValueError(
                f"connector process exited {result.returncode}: {self._redact(detail).strip()[:1200]}"
            )
        if len(result.stdout) > self.max_bytes:
            raise ValueError("connector response exceeded output limit")
        return result.stdout

    def _run_json(self, command: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        raw = self._run(command, timeout=timeout)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise ValueError("connector returned a non-object JSON payload")
        return payload

    @staticmethod
    def _tweet_result(item: dict[str, Any]) -> dict[str, Any]:
        author = item.get("author") or {}
        handle = author.get("screenName") or "i"
        tweet_id = item.get("id") or ""
        text = item.get("text") or ""
        quoted = item.get("quotedTweet") if isinstance(item.get("quotedTweet"), dict) else None
        return {
            "title": text[:120] or f"X post by @{handle}",
            "uri": f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else "",
            "published_at": item.get("createdAtISO") or item.get("createdAt"),
            "author": author.get("name"),
            "author_handle": handle,
            "author_id": author.get("id") or author.get("restId") or author.get("rest_id"),
            "content": text,
            "metrics": item.get("metrics") or {},
            "post_id": str(tweet_id) if tweet_id else None,
            "is_retweet": bool(item.get("isRetweet")),
            "retweeted_by": item.get("retweetedBy"),
            "quoted_post": {
                "post_id": quoted.get("id"),
                "author_handle": (quoted.get("author") or {}).get("screenName"),
                "author": (quoted.get("author") or {}).get("name"),
                "text": quoted.get("text"),
            } if quoted else None,
            "content_origin": "original_author",
        }

    @staticmethod
    def _x_profile_identity(payload: dict[str, Any] | None) -> dict[str, Any]:
        if not payload:
            return {}
        value = payload.get("user") or payload.get("data") or payload
        if isinstance(value, list):
            value = value[0] if value else {}
        if not isinstance(value, dict):
            return {}
        legacy = value.get("legacy") if isinstance(value.get("legacy"), dict) else {}
        username = (
            value.get("screenName") or value.get("screen_name") or value.get("username")
            or legacy.get("screen_name")
        )
        return {
            "username": username,
            "platform_user_id": value.get("id") or value.get("restId") or value.get("rest_id"),
            "display_name": value.get("name") or legacy.get("name"),
            "bio": value.get("description") or legacy.get("description"),
        }

    @staticmethod
    def _xhs_user_id(source_uri: str | None) -> str | None:
        if not source_uri:
            return None
        parsed = urllib.parse.urlparse(source_uri)
        if not parsed.netloc.casefold().endswith("xiaohongshu.com"):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["user", "profile"]:
            return parts[2][:200]
        return None

    @staticmethod
    def _xhs_payload_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("items", "notes", "feeds"):
            values = data.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
        return []

    @staticmethod
    def _xhs_profile_identity(payload: dict[str, Any], user_id: str) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        return {
            "platform_user_id": str(user.get("user_id") or user.get("id") or user_id),
            "username": user.get("nickname") or user.get("name"),
            "display_name": user.get("nickname") or user.get("name"),
            "bio": user.get("desc") or user.get("description"),
        }

    @staticmethod
    def _github_target(source_uri: str | None) -> tuple[str, str | None] | None:
        if not source_uri:
            return None
        parsed = urllib.parse.urlparse(source_uri)
        if parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or len(parts) > 2:
            return None
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[0]):
            return None
        if len(parts) == 2 and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1]):
            return None
        return parts[0], parts[1] if len(parts) == 2 else None

    @staticmethod
    def _github_profile_result(profile: dict[str, Any]) -> dict[str, Any]:
        login = str(profile.get("login") or "")
        return {
            "title": profile.get("name") or login or "GitHub account",
            "uri": profile.get("html_url") or (f"https://github.com/{login}" if login else ""),
            "published_at": profile.get("updated_at"),
            "author": profile.get("name") or login,
            "account_login": login or None,
            "account_id": profile.get("id"),
            "account_node_id": profile.get("node_id"),
            "result_kind": "account_profile",
            "content": json.dumps({
                key: profile.get(key)
                for key in (
                    "login", "id", "node_id", "name", "company", "blog", "location",
                    "bio", "twitter_username", "public_repos", "created_at", "updated_at",
                )
            }, ensure_ascii=False, sort_keys=True),
        }

    @staticmethod
    def _github_repo_result(repository: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        owner = repository.get("owner") if isinstance(repository.get("owner"), dict) else {}
        login = owner.get("login") or profile.get("login")
        return {
            "title": repository.get("full_name") or repository.get("name") or "GitHub repository",
            "uri": repository.get("html_url") or "",
            "published_at": repository.get("pushed_at") or repository.get("updated_at"),
            "summary": repository.get("description"),
            "content": json.dumps({
                key: repository.get(key)
                for key in (
                    "id", "node_id", "name", "full_name", "description", "fork",
                    "html_url", "language", "topics", "default_branch", "pushed_at",
                    "updated_at", "archived",
                )
            }, ensure_ascii=False, sort_keys=True),
            "owner_login": login,
            "owner_id": owner.get("id") or profile.get("id"),
            "repo_id": repository.get("id"),
            "repo_node_id": repository.get("node_id"),
            "result_kind": "owned_repository" if login == profile.get("login") else "repository_candidate",
        }

    @staticmethod
    def _x_handle(value: str) -> str | None:
        value = value.strip()
        if re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", value):
            return value.lstrip("@")
        match = re.search(r"(?:^|\s)from:@?([A-Za-z0-9_]{1,15})(?:\s|$)", value)
        if match:
            return match.group(1)
        parsed = urllib.parse.urlparse(value if "://" in value else "")
        if parsed.netloc.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            first = parsed.path.strip("/").split("/", 1)[0]
            if first and first not in {"home", "search", "i"}:
                return first
        return None

    @staticmethod
    def _parse_exa_text(text: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for block in re.split(r"\n\s*---\s*\n", text.strip()):
            title = re.search(r"(?m)^Title:\s*(.+)$", block)
            url = re.search(r"(?m)^URL:\s*(https?://\S+)$", block)
            if not title or not url:
                continue
            published = re.search(r"(?m)^Published:\s*(.+)$", block)
            author = re.search(r"(?m)^Author:\s*(.+)$", block)
            highlights = block.split("Highlights:\n", 1)[1].strip() if "Highlights:\n" in block else ""
            results.append({
                "title": title.group(1).strip(),
                "uri": url.group(1).strip(),
                "published_at": None if not published or published.group(1).strip() == "N/A" else published.group(1).strip(),
                "author": None if not author or author.group(1).strip() == "N/A" else author.group(1).strip(),
                "summary": highlights[:1200],
            })
        return results

    @staticmethod
    def _parse_exa_results(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in values:
            uri = str(item.get("url") or item.get("id") or "").strip()
            if not uri:
                continue
            highlights = item.get("highlights") or []
            summary = "\n".join(str(value) for value in highlights if value)
            if not summary:
                summary = str(item.get("text") or "")[:1200]
            results.append({
                "title": item.get("title") or "Exa result",
                "uri": uri,
                "published_at": item.get("publishedDate") or item.get("published_at"),
                "author": item.get("author"),
                "summary": summary[:1200],
            })
        return results

    @staticmethod
    def _host_is_allowed(uri: str, allowed_domains: set[str]) -> bool:
        host = urllib.parse.urlparse(uri).netloc.lower().split(":", 1)[0]
        return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)

    @staticmethod
    def _wechat_markdown(payload: dict[str, Any]) -> str:
        return "\n".join([
            "---",
            f"title: {json.dumps(payload.get('title') or '', ensure_ascii=False)}",
            f"author: {json.dumps(payload.get('author') or '', ensure_ascii=False)}",
            f"date: {json.dumps(payload.get('publish_time') or '', ensure_ascii=False)}",
            f"url: {json.dumps(payload.get('url') or '', ensure_ascii=False)}",
            'source: "wechat"',
            "---",
            "",
            f"# {payload.get('title') or '微信公众号文章'}",
            "",
            payload.get("content") or "",
        ])

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _summary(value: str, limit: int = 240) -> str:
        return " ".join(value.split())[:limit]

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"(?i)(xsec[_-]?token[=:\s]+)[^&\s'\"]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", value)
        return value

    @staticmethod
    def _first_root(value: str | Path | None, candidates: list[Path], *, required: str) -> Path | None:
        paths = [Path(value).expanduser()] if value else []
        paths.extend(candidates)
        return next((path.resolve() for path in paths if (path / required).exists()), None)

    def _validation_manifest(self) -> dict[str, Any]:
        path = Path(__file__).resolve().parents[2] / "docs" / "connector-validation.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _elapsed(started: float) -> int:
        return max(0, round((time.monotonic() - started) * 1000))

    def _route(self, channel: str) -> str:
        return {
            "homepage": "qiaomu_generic" if self.qiaomu_root else "builtin_public_http",
            "github": "github_cli" if shutil.which("gh") else "github_public_api",
            "arxiv": "arxiv_api",
            "news": "itjuzi_api_or_exa_public",
            "x": "twitter_user_posts_or_exa",
            "wechat": "exa_wechat_or_qiaomu",
            "xhs": "xhs_api_cli",
        }.get(channel, "unconfigured")


__all__ = ["ConnectorExecutor", "ConnectorOutput"]
