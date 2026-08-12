from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = "people-tracking-config-v1"
VALID_STATES = {"draft", "validated", "enabled"}
SCAN_CADENCES = {"hourly", "daily", "weekly"}
MISSING_ANCHOR_POLICIES = {"agent_discovery", "manual_queue"}
SOURCE_KINDS = {
    "feishu_base",
    "feishu_doc",
    "feishu_wiki",
    "local_file",
    "people_intel_api",
}
MASTER_MODES = {"existing_base", "create_base", "local_or_api"}
OUTPUT_TARGET_KINDS = {"current_chat", "chat", "user"}
SOURCE_ROUTE_ACTIONS = {"configure", "soft_retire", "replace"}
PREFERRED_PUBLIC_ROUTES = {"direct", "westlake_faculty_inline", "wordpress_rest"}
ALTERNATE_PUBLIC_ROUTES = {
    "auto",
    "raw_html",
    "huggingface_user_overview",
    "hf_user_overview",
}
SOURCE_ROUTE_FIELDS = {
    "preferred_route",
    "alternate_routes",
    "alternate_urls",
    "follow_meta_refresh",
    "wordpress_endpoint",
    "site_name",
}
_CREDENTIAL_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "auth",
    "authorization",
    "id_token",
    "key",
    "password",
    "refresh_token",
    "secret",
    "sig",
    "signature",
    "token",
    "x-amz-security-token",
    "x-amz-credential",
    "x-amz-signature",
    "x-goog-credential",
    "x-goog-signature",
}
DEFAULT_FIELDS = {
    "name": "姓名",
    "secondary_id": "第二ID",
    "aliases": "别名",
    "school": "学校",
    "research_focus": "专业/研究方向",
    "stage": "阶段/年龄",
    "homepage": "个人主页",
    "scholar": "Google Scholar",
    "github": "GitHub",
    "linkedin": "LinkedIn",
}
QUESTIONNAIRE = """请一次性回复下面这份人员跟踪配置问卷。可以写“不启用”或“使用默认值”；请勿在聊天中发送任何 API key、App Secret、Cookie 或密码。

1. 人员来源
- 飞书多维表格 URL/视图：
- 飞书文档或 Wiki URL（可多条）：
- 受控本地文件路径或 People Intel API（如无请写无）：

2. 字段映射
- 姓名、第二 ID、别名、学校、专业/研究方向、阶段/年龄分别对应什么字段？
- 个人主页、Google Scholar、GitHub、LinkedIn 分别对应什么字段？
- 如果某人四类主页全缺失：自动检索补全（推荐）/仅进入人工队列？

3. 可见人才总库
- 选择：使用现有 Base / 创建新 Base / 仅本地或 API
- 如果使用现有 Base，请给 Base URL 和 People/Sources 表名或单表表名。

4. 输出
- 日期云文档：启用/不启用；目标文件夹 token 或 Wiki 空间/节点（不要提供凭证）
- 飞书消息：当前会话 / 固定群 chat_id / 固定用户 open_id / 不启用

5. 频率与时区
- 来源扫描频率（默认每周）：
- 日报时间（默认每日 18:00）：
- 周报时间（默认周一 08:30）：
- 时区（默认 Asia/Shanghai）：

6. API
- DeepSeek V4 Flash：启用/不启用
- 如启用，只提供 mode-0600 key 文件或 secret manager 引用，不要提供 key 值
- 每日最大调用数、每日最大 token、月度预算：
- 其他搜索 API：名称、secret 引用与预算（如无请写无）

收到后我会先生成草稿并只读探测来源、字段和权限；在你回复“确认启用”前，不导入、不创建正式 Base、不注册调度、不发送正式报告。"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    config_root: Path
    state_root: Path
    config_file: Path
    database: Path
    reports: Path
    install_state: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        config_root = Path(
            os.environ.get(
                "PEOPLE_TRACKING_FEISHU_CONFIG_ROOT",
                "~/.config/people-tracking-feishu",
            )
        ).expanduser()
        state_root = Path(
            os.environ.get(
                "PEOPLE_TRACKING_FEISHU_STATE_ROOT",
                "~/.local/state/people-tracking-feishu",
            )
        ).expanduser()
        return cls(
            config_root=config_root,
            state_root=state_root,
            config_file=config_root / "config.json",
            database=state_root / "people-tracking.sqlite3",
            reports=state_root / "reports",
            install_state=config_root / "install-state.json",
        )

    def ensure(self) -> None:
        for path in (self.config_root, self.state_root, self.reports):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def secure_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    path.chmod(0o600)


def _is_feishu_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and (
        host == "feishu.cn"
        or host.endswith(".feishu.cn")
        or host == "larksuite.com"
        or host.endswith(".larksuite.com")
    )


def _validate_secret_reference(value: Any, *, name: str) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a file or secret-manager reference")
    lowered = value.casefold()
    if lowered.startswith("sk-") or re.search(r"(?i)(api[_-]?key|secret)\s*=", value):
        raise ConfigError(f"{name} contains an inline secret; provide only a reference")
    if not (
        value.startswith(("file:", "secret:", "keychain:", "vault:"))
        or value.startswith(("/", "~/"))
    ):
        raise ConfigError(f"{name} must use file:/secret:/keychain:/vault: or an absolute path")


def _validate_public_route_url(value: Any, *, name: str) -> str:
    """Validate a registered credential-free public HTTP(S) route.

    This is deliberately configuration-only validation: the scanner still
    performs normal TLS and response-quality checks at fetch time.  No DNS
    lookup, login state, Cookie or Authorization value is accepted here.
    """

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty public HTTP(S) URL")
    url = value.strip()
    if re.search(r"[\x00-\x20\x7f]", url) or "\\" in url:
        raise ConfigError(f"{name} contains unsafe URL characters")
    try:
        parsed = urlparse(url)
        port = parsed.port
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid URL") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ConfigError(f"{name} must use public HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ConfigError(f"{name} must not contain URL credentials")
    if port not in {None, 80, 443}:
        raise ConfigError(f"{name} must use only port 80 or 443")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ConfigError(f"{name} must not target localhost or a local domain")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname or re.fullmatch(r"[0-9.]+", hostname):
            raise ConfigError(f"{name} must use a fully qualified public hostname")
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigError(f"{name} has an invalid hostname") from exc
        labels = ascii_hostname.split(".")
        if len(ascii_hostname) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ConfigError(f"{name} has an invalid hostname")
    else:
        if not address.is_global:
            raise ConfigError(f"{name} must not target a non-public IP address")
    query_names = {
        key.casefold().replace("-", "_")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    normalized_credentials = {name.replace("-", "_") for name in _CREDENTIAL_QUERY_NAMES}
    raw_query_names = {
        match.group(1).casefold().replace("-", "_")
        for match in re.finditer(r"(?:^|[&;])([^=&;]+)=", parsed.query)
    }
    if (query_names | raw_query_names) & normalized_credentials:
        raise ConfigError(f"{name} must not contain credential or token query parameters")
    if parsed.fragment:
        raise ConfigError(f"{name} must not contain a fragment")
    return url


def _validate_clock(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ConfigError(f"{name} must use 24-hour HH:MM")
    return value


def _validate_schedule(value: Any) -> None:
    if not isinstance(value, dict):
        raise ConfigError("schedule must be an object")
    unknown = sorted(set(value) - {"timezone", "scan", "daily_digest", "weekly_digest"})
    if unknown:
        raise ConfigError(f"schedule has unsupported fields: {', '.join(unknown)}")
    timezone_name = value.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ConfigError("schedule.timezone is required")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("schedule.timezone must be a valid IANA timezone") from exc
    cadence = value.get("scan", "weekly")
    if cadence not in SCAN_CADENCES:
        raise ConfigError("schedule.scan must be hourly, daily, or weekly")
    _validate_clock(value.get("daily_digest", "18:00"), name="schedule.daily_digest")
    weekly = value.get("weekly_digest", "MON 08:30")
    if not isinstance(weekly, str):
        raise ConfigError("schedule.weekly_digest must use DDD HH:MM")
    match = re.fullmatch(r"(MON|TUE|WED|THU|FRI|SAT|SUN) ((?:[01]\d|2[0-3]):[0-5]\d)", weekly)
    if not match:
        raise ConfigError("schedule.weekly_digest must use DDD HH:MM")


def _validate_source_routes(payload: Any) -> None:
    if payload is None:
        return
    if not isinstance(payload, list):
        raise ConfigError("source_routes must be a list")
    if len(payload) > 10_000:
        raise ConfigError("source_routes exceeds the 10000-entry safety limit")
    seen_targets: set[str] = set()
    allowed_keys = {"url", "action", "replacement_url", "curation_note", *SOURCE_ROUTE_FIELDS}
    for index, route in enumerate(payload):
        prefix = f"source_routes[{index}]"
        if not isinstance(route, dict):
            raise ConfigError(f"{prefix} must be an object")
        unknown = sorted(set(route) - allowed_keys)
        if unknown:
            raise ConfigError(f"{prefix} has unsupported fields: {', '.join(unknown)}")
        target = _validate_public_route_url(route.get("url"), name=f"{prefix}.url")
        duplicate_key = target.casefold()
        if duplicate_key in seen_targets:
            raise ConfigError(f"{prefix}.url duplicates another exact source route")
        seen_targets.add(duplicate_key)
        action = str(route.get("action") or "configure")
        if action not in SOURCE_ROUTE_ACTIONS:
            raise ConfigError(f"{prefix}.action must be configure, soft_retire, or replace")
        note = route.get("curation_note")
        if note is not None and (not isinstance(note, str) or len(note.strip()) > 2000):
            raise ConfigError(f"{prefix}.curation_note must be a string of at most 2000 characters")
        if action in {"soft_retire", "replace"} and not str(note or "").strip():
            raise ConfigError(f"{prefix}.curation_note is required for {action}")
        replacement = route.get("replacement_url")
        if action == "replace":
            replacement_url = _validate_public_route_url(
                replacement,
                name=f"{prefix}.replacement_url",
            )
            if replacement_url.casefold() == target.casefold():
                raise ConfigError(f"{prefix}.replacement_url must differ from url")
        elif replacement not in (None, ""):
            raise ConfigError(f"{prefix}.replacement_url is only valid for replace")

        configured_fields = SOURCE_ROUTE_FIELDS & set(route)
        if action == "configure" and not configured_fields:
            raise ConfigError(f"{prefix} configure requires at least one route field")
        if action == "soft_retire" and configured_fields:
            raise ConfigError(f"{prefix} soft_retire cannot also configure retrieval routes")
        preferred = route.get("preferred_route")
        if preferred is not None and preferred not in PREFERRED_PUBLIC_ROUTES:
            raise ConfigError(f"{prefix}.preferred_route is unsupported")
        follow_refresh = route.get("follow_meta_refresh")
        if follow_refresh is not None and not isinstance(follow_refresh, bool):
            raise ConfigError(f"{prefix}.follow_meta_refresh must be a JSON boolean")
        endpoint = route.get("wordpress_endpoint")
        if endpoint is not None:
            _validate_public_route_url(endpoint, name=f"{prefix}.wordpress_endpoint")
            if preferred != "wordpress_rest":
                raise ConfigError(
                    f"{prefix}.wordpress_endpoint requires preferred_route=wordpress_rest"
                )
        site_name = route.get("site_name")
        if site_name is not None and (
            not isinstance(site_name, str)
            or not site_name.strip()
            or len(site_name.strip()) > 200
        ):
            raise ConfigError(f"{prefix}.site_name must be a non-empty string of at most 200 characters")
        alternate_urls = route.get("alternate_urls")
        if alternate_urls is not None:
            if not isinstance(alternate_urls, list) or len(alternate_urls) > 20:
                raise ConfigError(f"{prefix}.alternate_urls must be a list of at most 20 URLs")
            for alternate_index, alternate_url in enumerate(alternate_urls):
                _validate_public_route_url(
                    alternate_url,
                    name=f"{prefix}.alternate_urls[{alternate_index}]",
                )
        alternate_routes = route.get("alternate_routes")
        if alternate_routes is not None:
            if not isinstance(alternate_routes, list) or len(alternate_routes) > 20:
                raise ConfigError(f"{prefix}.alternate_routes must be a list of at most 20 routes")
            for alternate_index, alternate in enumerate(alternate_routes):
                alternate_name = f"{prefix}.alternate_routes[{alternate_index}]"
                if isinstance(alternate, str):
                    _validate_public_route_url(alternate, name=alternate_name)
                    continue
                if not isinstance(alternate, dict) or set(alternate) - {"route", "url"}:
                    raise ConfigError(f"{alternate_name} must contain only route and url")
                route_name = str(alternate.get("route") or "auto").casefold()
                if route_name not in ALTERNATE_PUBLIC_ROUTES:
                    raise ConfigError(f"{alternate_name}.route is unsupported")
                _validate_public_route_url(alternate.get("url"), name=f"{alternate_name}.url")


def validate_config(payload: dict[str, Any], *, require_complete: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigError("configuration must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")
    state = str(payload.get("state") or "draft")
    if state not in VALID_STATES:
        raise ConfigError("state must be draft, validated, or enabled")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ConfigError("sources must be a list")
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
            raise ConfigError(f"sources[{index}] has an unsupported kind")
        location = source.get("url") or source.get("path")
        if not isinstance(location, str) or not location.strip():
            raise ConfigError(f"sources[{index}] needs url or path")
        if source["kind"].startswith("feishu_") and not _is_feishu_url(location):
            raise ConfigError(f"sources[{index}] must be an HTTPS Feishu/Lark URL")
        if source["kind"] == "people_intel_api":
            api_url = _validate_public_route_url(
                location,
                name=f"sources[{index}].url",
            )
            if urlparse(api_url).scheme.casefold() != "https":
                raise ConfigError("People Intel API must use public HTTPS")
    _validate_source_routes(payload.get("source_routes", []))
    mapping = payload.get("field_mapping")
    if not isinstance(mapping, dict):
        raise ConfigError("field_mapping must be an object")
    if require_complete and not mapping.get("name"):
        raise ConfigError("field_mapping.name is required")
    intake = payload.get("intake") or {}
    if intake.get("missing_anchor_policy", "agent_discovery") not in MISSING_ANCHOR_POLICIES:
        raise ConfigError("intake.missing_anchor_policy is invalid")
    if int(intake.get("discovery_batch_size", 50)) not in range(1, 201):
        raise ConfigError("intake.discovery_batch_size must be between 1 and 200")
    master = payload.get("master_database")
    if not isinstance(master, dict) or master.get("mode") not in MASTER_MODES:
        raise ConfigError("master_database.mode is invalid")
    if master.get("mode") == "existing_base":
        if not _is_feishu_url(str(master.get("url") or "")):
            raise ConfigError("master_database.url must be an HTTPS Feishu/Lark URL")
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        raise ConfigError("outputs must be an object")
    message = outputs.get("message") or {"enabled": False}
    if message.get("enabled"):
        if message.get("target_kind") not in OUTPUT_TARGET_KINDS:
            raise ConfigError("outputs.message.target_kind is invalid")
        if message.get("target_kind") in {"chat", "user"} and not message.get("target_id"):
            raise ConfigError("fixed message targets need target_id")
    _validate_schedule(payload.get("schedule"))
    apis = payload.get("apis") or {}
    deepseek = apis.get("deepseek") or {"enabled": False}
    if deepseek.get("enabled"):
        _validate_secret_reference(deepseek.get("key_reference"), name="DeepSeek key_reference")
        if not deepseek.get("key_reference"):
            raise ConfigError("enabled DeepSeek requires key_reference")
        if deepseek.get("model") != "deepseek-v4-flash":
            raise ConfigError("portable release only enables deepseek-v4-flash")
    for index, api in enumerate(apis.get("search", [])):
        _validate_secret_reference(api.get("secret_reference"), name=f"search API {index}")
    if require_complete and not sources:
        raise ConfigError("at least one source is required before validation")
    return payload


def load_config(paths: RuntimePaths, *, required: bool = True) -> dict[str, Any] | None:
    if not paths.config_file.exists():
        if required:
            raise ConfigError("onboarding has not created config.json")
        return None
    mode = paths.config_file.stat().st_mode
    if paths.config_file.is_symlink() or not stat.S_ISREG(mode):
        raise ConfigError("config.json must be a regular non-symlink file")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError("config.json must be mode 0600")
    try:
        payload = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config.json is not valid JSON") from exc
    return validate_config(payload)


def normalize_answers(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize an agent-produced questionnaire answer into the stable schema."""

    now = utc_now()
    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "state": "draft",
        "created_at": payload.get("created_at") or now,
        "updated_at": now,
        "runtime": {
            "mode": (payload.get("runtime") or {}).get("mode", "auto"),
            "lark_cli_identity_read": "user",
            "lark_cli_identity_write": "bot",
        },
        "sources": payload.get("sources") or [],
        "source_routes": (
            [] if payload.get("source_routes") is None else payload.get("source_routes")
        ),
        "field_mapping": {**DEFAULT_FIELDS, **(payload.get("field_mapping") or {})},
        "intake": {
            "missing_anchor_policy": "agent_discovery",
            "discovery_batch_size": 50,
            "minimum_evidence_links": 1,
            "require_identity_gate_on_baseline": True,
            **(payload.get("intake") or {}),
        },
        "master_database": {
            "mode": "local_or_api",
            **(payload.get("master_database") or {}),
        },
        "outputs": {
            "document": {"enabled": True, **((payload.get("outputs") or {}).get("document") or {})},
            "message": {
                "enabled": True,
                "target_kind": "current_chat",
                **((payload.get("outputs") or {}).get("message") or {}),
            },
        },
        "schedule": {
            "timezone": "Asia/Shanghai",
            "scan": "weekly",
            "daily_digest": "18:00",
            "weekly_digest": "MON 08:30",
            **(payload.get("schedule") or {}),
        },
        "apis": {
            "deepseek": {
                "enabled": False,
                "model": "deepseek-v4-flash",
                "max_calls_per_day": 100,
                "max_total_tokens_per_day": 100000,
                **((payload.get("apis") or {}).get("deepseek") or {}),
            },
            "search": (payload.get("apis") or {}).get("search") or [],
        },
        "validation": {},
    }
    return validate_config(normalized)


def mark_state(
    paths: RuntimePaths,
    config: dict[str, Any],
    state: str,
    *,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in VALID_STATES:
        raise ConfigError("invalid configuration state")
    updated = json.loads(json.dumps(config))
    updated["state"] = state
    updated["updated_at"] = utc_now()
    if validation is not None:
        updated["validation"] = validation
    validate_config(updated, require_complete=state in {"validated", "enabled"})
    secure_write_json(paths.config_file, updated)
    return updated
