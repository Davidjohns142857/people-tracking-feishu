from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Literal

from pypinyin import Style, lazy_pinyin

from people_intel.deepseek_fallback import DeepSeekDiffReviewer


SOURCE_KINDS = ("homepage", "scholar", "github", "linkedin")
MEANINGFUL_CATEGORIES = {
    "identity", "affiliation", "research", "publication", "project",
    "position", "education", "award", "repository",
}
NOISE_TEXT = {
    "search this site", "skip to main content", "skip to navigation",
    "sign in", "show more", "view all", "loading", "cookie settings",
    "accept cookies", "embedded files", "privacy",
}
COMPOUND_CHINESE_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫",
    "万俟", "闻人", "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台",
    "皇甫", "宗政", "濮阳", "公冶", "太叔", "申屠", "公孙", "慕容",
    "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于", "司空", "闾丘",
    "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐",
    "段干", "百里", "呼延", "东郭", "南门", "羊舌", "微生", "梁丘",
    "左丘", "东门", "西门", "第五",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: str, length: int | None = None) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length] if length else digest


def normalize_text(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value))
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_key(value: str) -> str:
    value = normalize_text(value).casefold()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _identity_key(value: str) -> str:
    """Normalize display names, handles and camel-case account IDs for matching."""
    value = normalize_text(value)
    value = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", value)
    value = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", value)
    return normalize_key(value)


def _compact_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _identity_key(value))


def _chinese_name_forms(value: str) -> set[str]:
    forms: set[str] = set()
    for match in re.finditer(r"[\u4e00-\u9fff]{2,5}", value):
        name = match.group(0)
        syllables = lazy_pinyin(name, style=Style.NORMAL, errors="ignore")
        if len(syllables) != len(name):
            continue
        surname_length = 2 if name[:2] in COMPOUND_CHINESE_SURNAMES else 1
        surname = syllables[:surname_length]
        given = syllables[surname_length:]
        if not given:
            continue
        chinese_order = " ".join([*surname, *given])
        western_order = " ".join([*given, *surname])
        joined_surname = "".join(surname)
        joined_given = "".join(given)
        forms.update(
            {
                normalize_key(name),
                normalize_key(chinese_order),
                normalize_key(western_order),
                normalize_key(f"{joined_surname} {joined_given}"),
                normalize_key(f"{joined_given} {joined_surname}"),
                _compact_identity(chinese_order),
                _compact_identity(western_order),
            }
        )
    return {item for item in forms if item}


def _identity_name_forms(canonical_name: str, aliases: Iterable[str] = ()) -> set[str]:
    forms: set[str] = set()
    for value in (canonical_name, *aliases):
        normalized = _identity_key(value)
        if normalized:
            forms.add(normalized)
            compact = _compact_identity(normalized)
            if len(compact) >= 5:
                forms.add(compact)
        nickname_values = [
            normalize_text(item)
            for pattern in (
                r"\(([^)]*)\)",
                r"（([^）]*)）",
                r"“([^”]*)”",
                r'"([^"]*)"',
            )
            for item in re.findall(pattern, normalize_text(value))
            if normalize_text(item)
        ]
        without_nickname = re.sub(
            r"\([^)]*\)|（[^）]*）|“[^”]*”|\"[^\"]*\"",
            " ",
            normalize_text(value),
        )
        normalized_without_nickname = _identity_key(without_nickname)
        if normalized_without_nickname:
            forms.add(normalized_without_nickname)
            latin_tokens = [
                token
                for token in normalized_without_nickname.split()
                if re.fullmatch(r"[a-z][a-z'-]*", token)
            ]
            if (
                len(latin_tokens) == len(normalized_without_nickname.split())
                and 2 <= len(latin_tokens) <= 4
            ):
                forms.add(" ".join([latin_tokens[-1], *latin_tokens[:-1]]))
            last_token = latin_tokens[-1] if latin_tokens else ""
            for nickname in nickname_values:
                nickname_key = _identity_key(nickname)
                if nickname_key:
                    forms.add(nickname_key)
                    if last_token:
                        forms.add(f"{nickname_key} {last_token}")
        forms.update(_chinese_name_forms(value))
    return {item for item in forms if item}


def _identity_match(
    expected_forms: Iterable[str],
    observed_values: Iterable[str],
) -> tuple[bool, str | None, str | None]:
    """Match names without letting short surnames such as Li match arbitrary text."""
    raw_observed = [str(value) for value in observed_values if normalize_text(str(value))]
    observed_keys = [_identity_key(value) for value in raw_observed]
    observed_compact = [_compact_identity(value) for value in raw_observed]
    for form in sorted(set(expected_forms), key=len, reverse=True):
        normalized = _identity_key(form)
        compact = _compact_identity(form)
        for index, candidate in enumerate(observed_keys):
            if normalized and (
                normalized == candidate
                or re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", candidate)
            ):
                return True, form, raw_observed[index]
            form_tokens = normalized.split()
            candidate_tokens = candidate.split()
            if (
                len(candidate_tokens) == 1
                and len(candidate_tokens[0]) >= 4
                and len(form_tokens) >= 2
                and candidate_tokens[0] == form_tokens[0]
            ):
                return True, form, raw_observed[index]
            if (
                2 <= len(form_tokens) <= 4
                and 2 <= len(candidate_tokens) <= 5
                and form_tokens[0] == candidate_tokens[0]
                and form_tokens[-1] == candidate_tokens[-1]
            ):
                return True, form, raw_observed[index]
            if len(compact) >= 5 and compact in observed_compact[index]:
                return True, form, raw_observed[index]
            if (
                len(compact) >= 8
                and abs(len(compact) - len(observed_compact[index])) <= 2
                and SequenceMatcher(None, compact, observed_compact[index]).ratio() >= 0.92
            ):
                return True, form, raw_observed[index]
    return False, None, None


def _identity_url_match(
    expected_forms: Iterable[str],
    url: str | None,
) -> tuple[bool, str | None]:
    if not url:
        return False, None
    parsed = urllib.parse.urlsplit(url)
    visible_url = urllib.parse.unquote(
        f"{parsed.netloc.removeprefix('www.')} {parsed.path}"
    )
    compact_url = _compact_identity(visible_url)
    for form in sorted(set(expected_forms), key=len, reverse=True):
        compact = _compact_identity(form)
        if len(compact) >= 6 and compact in compact_url:
            return True, form
        tokens = [
            token for token in _identity_key(form).split()
            if token and token not in {"dr", "prof", "phd"}
        ]
        if len(tokens) >= 2 and len(tokens[0]) >= 3 and len(tokens[-1]) >= 2:
            # Personal domains commonly include a middle initial while the roster
            # does not (anjaliwgupta, serenalwang).
            pattern = re.escape(tokens[0]) + r"[a-z0-9]{0,2}" + re.escape(tokens[-1])
            if re.search(pattern, compact_url):
                return True, form
    return False, None


def _merge_person_profile(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value not in (None, "", []):
            if merged.get(key) in (None, "", []):
                merged[key] = value
            elif isinstance(value, list) and isinstance(merged.get(key), list):
                merged[key] = list(dict.fromkeys([*merged[key], *value]))
    cohorts = []
    for profile in (existing, incoming):
        values = profile.get("cohorts") or []
        if isinstance(values, str):
            values = [values]
        if profile.get("cohort"):
            values = [*values, profile["cohort"]]
        cohorts.extend(str(value) for value in values if value)
    if cohorts:
        merged["cohorts"] = list(dict.fromkeys(cohorts))
        merged["cohort"] = merged.get("cohort") or merged["cohorts"][0]
    return merged


def canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    host_without_www = host.removeprefix("www.")
    # `www` is cosmetic for the fixed platform namespaces below, but it is not
    # cosmetic for arbitrary institutional and personal sites.  Several real
    # university hosts publish only the `www` name while the bare host is 404 or
    # has no DNS record at all.  Preserve the fetch authority for Homepages.
    if (
        host_without_www in {"github.com", "huggingface.co"}
        or host_without_www.endswith("linkedin.com")
        or re.fullmatch(r"scholar\.google\.[a-z.]+", host_without_www)
    ):
        host = host_without_www
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    keep = [(k, v) for k, v in query if k in {"user", "id"}]
    return urllib.parse.urlunsplit((scheme, host, path, urllib.parse.urlencode(keep), ""))


def classify_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(canonical_url(value))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.netloc
    platform_host = host.removeprefix("www.")
    if re.fullmatch(r"scholar\.google\.[a-z.]+", platform_host):
        query = dict(urllib.parse.parse_qsl(parsed.query))
        return "scholar" if parsed.path == "/citations" and query.get("user") else None
    if platform_host == "github.com":
        return "github" if len([p for p in parsed.path.split("/") if p]) == 1 else None
    if platform_host.endswith("linkedin.com") and "/in/" in parsed.path:
        return "linkedin"
    if platform_host == "huggingface.co":
        parts = [part for part in parsed.path.split("/") if part]
        reserved = {
            "api", "blog", "datasets", "docs", "enterprise", "models",
            "organizations", "papers", "pricing", "spaces",
        }
        return "homepage" if len(parts) == 1 and parts[0].casefold() not in reserved else None
    non_personal = (
        "arxiv.org", "huggingface.co", "icml.cc", "blog.icml.cc",
        "researchgate.net", "dblp.uni-trier.de", "openreview.net",
        "twitter.com", "x.com", "youtube.com",
    )
    if any(
        platform_host == item or platform_host.endswith(f".{item}")
        for item in non_personal
    ):
        return None
    return "homepage"


def source_external_id(kind: str, url: str) -> str:
    parsed = urllib.parse.urlsplit(canonical_url(url))
    if kind == "scholar":
        user = dict(urllib.parse.parse_qsl(parsed.query)).get("user")
        return f"scholar:{user}" if user else f"scholar-search:{_sha(canonical_url(url), 16)}"
    if kind == "github":
        return f"github:{parsed.path.strip('/').casefold()}"
    if kind == "linkedin":
        match = re.search(r"/in/([^/]+)", parsed.path)
        return f"linkedin:{match.group(1).casefold()}" if match else f"linkedin:{_sha(url, 16)}"
    return f"homepage:{_sha(canonical_url(url), 16)}"


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", ascii_value or value).strip("-").lower()
    return value or "person"


def _category(text: str, context: str = "") -> str:
    probe = f"{context} {text}".casefold()
    rules = (
        ("publication", r"\b(publications?|papers?|preprints?|arxiv|neurips|icml|iclr|cvpr|acl|emnlp)\b|论文"),
        ("award", r"\b(awards?|honou?rs?|fellowship|recipient)\b|获奖|奖学金|荣誉"),
        ("position", r"\b(experience|employment|intern|professor|scientist|ph\.?d\.? student|joined)\b|任职|实习|博士生"),
        ("education", r"\b(education|b\.?sc|m\.?sc|b\.?eng|university degree)\b|教育|毕业"),
        ("research", r"\b(research|interests?|focus|working on|machine learning|artificial intelligence)\b|研究方向|研究聚焦"),
        ("project", r"\b(projects?|code|demo|dataset)\b|项目|代码"),
        ("affiliation", r"\b(university|institute|laboratory| lab\b|school of|department|openai|google|apple|meta|nvidia)\b|大学|学院|实验室|研究院"),
    )
    for name, pattern in rules:
        if re.search(pattern, probe, re.I):
            return name
    return "content"


def _simhash(items: Iterable[str]) -> str:
    vector = [0] * 64
    tokens: list[str] = []
    for item in items:
        tokens.extend(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", normalize_key(item)))
    for token, weight in Counter(tokens).items():
        value = int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16)
        for bit in range(64):
            vector[bit] += weight if value & (1 << bit) else -weight
    result = sum((1 << bit) for bit, score in enumerate(vector) if score >= 0)
    return f"{result:016x}"


def _hamming(left: str, right: str) -> int:
    return (int(left or "0", 16) ^ int(right or "0", 16)).bit_count()


class _SemanticHTMLParser(HTMLParser):
    BLOCK_TAGS = {"title", "h1", "h2", "h3", "h4", "p", "li", "td"}
    EXCLUDED_TAGS = {"style", "noscript", "svg", "canvas", "template", "form", "button"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.current_tag: str | None = None
        self.current_text: list[str] = []
        self.current_attrs: dict[str, str] = {}
        self.current_links: list[str] = []
        self.blocks: list[tuple[str, str, dict[str, str]]] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self.json_ld: list[str] = []
        self._json_buffer: list[str] | None = None
        self.seen_main = False
        self.section_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        amap = {k.casefold(): v or "" for k, v in attrs}
        descriptor = " ".join((amap.get("id", ""), amap.get("class", ""), amap.get("role", ""))).casefold()
        parent_excluded = self.stack[-1][1] if self.stack else False
        excluded = parent_excluded or tag in self.EXCLUDED_TAGS or bool(
            re.search(r"\b(nav|footer|cookie|consent|modal|menu|toolbar|advert|social-share)\b", descriptor)
        )
        if tag == "script" and amap.get("type", "").casefold() == "application/ld+json":
            excluded = False
            self._json_buffer = []
        elif tag == "script":
            excluded = True
        if tag == "meta":
            key = (amap.get("property") or amap.get("name") or "").casefold()
            if key and amap.get("content"):
                self.meta[key] = normalize_text(amap["content"])
        if tag == "link" and amap.get("rel", "").casefold() == "canonical":
            self.canonical = amap.get("href") or None
        if tag == "main" or amap.get("role", "").casefold() == "main":
            self.seen_main = True
        if tag == "section":
            self.section_count += 1
        if tag == "a" and self.current_tag and amap.get("href"):
            self.current_links.append(amap["href"])
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, excluded))
        if tag in self.BLOCK_TAGS and not excluded:
            self.current_tag, self.current_text, self.current_attrs, self.current_links = tag, [], amap, []

    def handle_endtag(self, tag: str) -> None:
        if self._json_buffer is not None and tag == "script":
            self.json_ld.append("".join(self._json_buffer))
            self._json_buffer = None
        if self.current_tag == tag:
            value = normalize_text(" ".join(self.current_text))
            if value:
                attrs = dict(self.current_attrs)
                if self.current_links:
                    attrs["_links"] = "\x1f".join(self.current_links[:8])
                self.blocks.append((tag, value, attrs))
            self.current_tag, self.current_text, self.current_attrs, self.current_links = None, [], {}, []
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._json_buffer is not None:
            self._json_buffer.append(data)
        if self.current_tag and not (self.stack and self.stack[-1][1]):
            self.current_text.append(data)


@dataclass
class SnapshotItem:
    key: str
    category: str
    text: str
    stable_id: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class FeatureSnapshot:
    kind: str
    extractor_version: str
    semantic_hash: str
    simhash64: str
    identity: dict[str, str]
    items: list[SnapshotItem]
    metrics: dict[str, int | float | str] = field(default_factory=dict)
    canonical_url: str | None = None
    token_count: int = 0
    item_count: int = 0
    section_count: int = 0
    sentinels: list[str] = field(default_factory=list)
    truncated: bool = False
    retrieval_mode: str = "direct"
    visible_content_hash: str = ""
    ordered_content_hash: str = ""
    structure_hash: str = ""
    critical_hash: str = ""
    section_hashes: dict[str, str] = field(default_factory=dict)
    comparison_hash: str = ""
    # Transient normalized visible text is used only by the identity gate. It is
    # intentionally omitted from SQLite manifests to retain the low-storage and
    # no-raw-page contract.
    identity_search_text: str = field(default="", repr=False)

    def to_json(self) -> str:
        payload = asdict(self)
        payload.pop("identity_search_text", None)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "FeatureSnapshot":
        data = json.loads(value)
        data["items"] = [SnapshotItem(**item) for item in data.get("items", [])]
        return cls(**data)


@dataclass
class FetchObservation:
    body: str | bytes | dict[str, Any] | list[Any] | None
    status_code: int = 200
    final_url: str | None = None
    content_type: str = "text/html"
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    observed_at: str = field(default_factory=utc_now)
    retrieval_mode: Literal[
        "direct",
        "browser_public",
        "search_index",
        "search_index_discovery_cache",
    ] = "direct"


@dataclass
class ChangeDecision:
    status: Literal[
        "baseline", "unchanged", "noise", "candidate", "changed", "ambiguous",
        "source_issue_pending", "source_issue", "binding_review", "binding_conflict",
    ]
    score: float
    summary: str
    additions: list[dict[str, Any]] = field(default_factory=list)
    removals: list[dict[str, Any]] = field(default_factory=list)
    modifications: list[dict[str, Any]] = field(default_factory=list)
    health_status: str = "healthy"
    reviewer: str = "deterministic"
    confirmation_count: int = 0
    confirmations_required: int = 0
    quality_score: float = 1.0
    quality_reasons: list[str] = field(default_factory=list)


def _item(
    category: str,
    text: str,
    stable_id: str | None = None,
    *,
    attributes: dict[str, Any] | None = None,
) -> SnapshotItem:
    text = normalize_text(text)[:240]
    basis = stable_id or normalize_key(text)
    normalized_attributes = {
        str(key): normalize_text(str(value))[:500]
        for key, value in (attributes or {}).items()
        if value not in (None, "")
    }
    return SnapshotItem(
        key=_sha(f"{category}\x1f{basis}", 24),
        category=category,
        text=text,
        stable_id=stable_id,
        attributes=normalized_attributes,
    )


def _finalize_snapshot(
    kind: str,
    identity: dict[str, str],
    items: list[SnapshotItem],
    *,
    metrics: dict[str, Any] | None = None,
    canonical: str | None = None,
    section_count: int = 0,
    sentinels: list[str] | None = None,
    visible_blocks: Iterable[str] | None = None,
    structure_tokens: Iterable[str] | None = None,
    identity_text: str | None = None,
    max_items: int = 1200,
    extractor_version: str = "semantic-manifest-v4",
) -> FeatureSnapshot:
    unique: dict[tuple[str, str], SnapshotItem] = {}
    for item in items:
        unique[(item.category, item.stable_id or normalize_key(item.text))] = item
    all_items = list(unique.values())
    bounded = all_items[:max_items]
    stable = sorted(
        json.dumps(
            {
                "category": item.category,
                "stable_id": item.stable_id or normalize_key(item.text),
                "text": normalize_key(item.text),
                "attributes": item.attributes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in bounded
    )
    identity_norm = {k: normalize_text(v)[:240] for k, v in identity.items() if normalize_text(v)}
    payload = json.dumps({"identity": identity_norm, "items": stable}, ensure_ascii=False, sort_keys=True)
    words = sum(len(re.findall(r"\w+|[\u4e00-\u9fff]", item.text)) for item in bounded)
    normalized_visible = [
        normalize_key(value)
        for value in (
            visible_blocks
            or [*identity.values(), *(item.text for item in all_items)]
        )
        if normalize_key(value)
    ]
    visible_multiset = sorted(Counter(normalized_visible).items())
    visible_content_hash = _sha(
        json.dumps(visible_multiset, ensure_ascii=False, separators=(",", ":"))
    )
    ordered_content_hash = _sha(
        json.dumps(normalized_visible, ensure_ascii=False, separators=(",", ":"))
    )
    normalized_structure = [
        normalize_key(value) for value in (structure_tokens or []) if normalize_key(value)
    ]
    structure_hash = _sha(
        json.dumps(normalized_structure, ensure_ascii=False, separators=(",", ":"))
    )
    section_values: dict[str, list[str]] = {}
    for item in all_items:
        section_values.setdefault(item.category, []).append(
            json.dumps(
                {
                    "stable_id": item.stable_id or normalize_key(item.text),
                    "text": normalize_key(item.text),
                    "attributes": item.attributes,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    section_hashes = {
        category: _sha(json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":")))
        for category, values in sorted(section_values.items())
    }
    critical_payload = {
        "identity": identity_norm,
        "sections": {
            category: section_hashes[category]
            for category in sorted(section_hashes)
            if category in MEANINGFUL_CATEGORIES
        },
    }
    semantic_hash = _sha(payload)
    critical_hash = _sha(
        json.dumps(critical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return FeatureSnapshot(
        kind=kind,
        extractor_version=extractor_version,
        semantic_hash=semantic_hash,
        simhash64=_simhash([*identity_norm.values(), *(item.text for item in bounded)]),
        identity=identity_norm,
        items=bounded,
        metrics=metrics or {},
        canonical_url=canonical,
        token_count=words,
        item_count=len(bounded),
        section_count=section_count,
        sentinels=sorted(set(sentinels or [])),
        truncated=len(all_items) > len(bounded),
        visible_content_hash=visible_content_hash,
        ordered_content_hash=ordered_content_hash,
        structure_hash=structure_hash,
        critical_hash=critical_hash,
        section_hashes=section_hashes,
        comparison_hash=_sha(
            "\x1f".join(
                (
                    semantic_hash,
                    visible_content_hash,
                    critical_hash,
                )
            )
        ),
        identity_search_text=normalize_text(
            identity_text or " \n ".join(normalized_visible)
        )[:250_000],
    )


def _extract_generic(body: str, kind: str = "homepage") -> FeatureSnapshot:
    parser = _SemanticHTMLParser()
    parser.feed(body)
    if len(parser.blocks) <= 1:
        cleaned = re.sub(r"(?is)<(script|style|noscript|nav|footer|form)\b.*?</\1>", " ", body)
        for match in re.finditer(r"(?is)<(h[1-4]|p|li|td)\b[^>]*>(.*?)</\1>", cleaned):
            text = normalize_text(re.sub(r"(?is)<[^>]+>", " ", match.group(2)))
            if text:
                parser.blocks.append((match.group(1).lower(), text, {}))
    titles = [text for tag, text, _ in parser.blocks if tag == "title"]
    headings = [text for tag, text, _ in parser.blocks if tag in {"h1", "h2", "h3", "h4"}]
    identity: dict[str, str] = {}
    if parser.meta.get("og:title") or titles:
        identity["name_or_title"] = parser.meta.get("og:title") or titles[0]
    if parser.meta.get("og:description") or parser.meta.get("description"):
        identity["headline_or_summary"] = parser.meta.get("og:description") or parser.meta["description"]
    if headings:
        identity["primary_heading"] = headings[0]
    items: list[SnapshotItem] = []
    context = ""
    for tag, text, attrs in parser.blocks:
        low = normalize_key(text)
        if tag in {"title"} or not low or low in NOISE_TEXT or len(low) < 5:
            continue
        if tag in {"h1", "h2", "h3", "h4"}:
            context = text
            if tag != "h1":
                continue
        category = "identity" if tag == "h1" else _category(text, context)
        if re.fullmatch(r"(©|copyright)?\s*20\d{2}.*", low, re.I):
            continue
        links = [
            canonical_url(value)
            for value in attrs.get("_links", "").split("\x1f")
            if value.startswith(("http://", "https://"))
        ]
        stable_id = links[0] if links else None
        items.append(_item(category, text, stable_id=stable_id))
    if kind == "linkedin":
        for raw in parser.json_ld:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            values = data if isinstance(data, list) else [data]
            for entry in values:
                if not isinstance(entry, dict) or str(entry.get("@type", "")).casefold() != "person":
                    continue
                for key in ("name", "jobTitle", "description"):
                    if entry.get(key):
                        identity[key] = str(entry[key])
                for key, category in (("worksFor", "position"), ("alumniOf", "education")):
                    value = entry.get(key)
                    if isinstance(value, dict):
                        value = value.get("name")
                    if value:
                        items.append(_item(category, str(value), stable_id=normalize_key(str(value))))
    fingerprint_blocks = [
        text
        for tag, text, _ in parser.blocks
        if tag != "title"
        and normalize_key(text) not in NOISE_TEXT
        and not re.fullmatch(r"(?:©|copyright)\s*20\d{2}.*", normalize_key(text), re.I)
    ]
    identity_html = re.sub(
        r"(?is)<(script|style|noscript|svg|canvas|template|form|button|nav|footer)\b.*?</\1>",
        " ",
        body,
    )
    identity_text = normalize_text(re.sub(r"(?is)<[^>]+>", " ", identity_html))
    sentinels = []
    if parser.seen_main:
        sentinels.append("main")
    if headings:
        sentinels.append("heading")
    if parser.canonical:
        sentinels.append("canonical")
    if parser.meta.get("og:title"):
        sentinels.append("og:title")
    if kind == "linkedin" and identity.get("name"):
        sentinels.append("profile-name")
    return _finalize_snapshot(
        kind,
        identity,
        items,
        canonical=parser.canonical,
        section_count=parser.section_count,
        sentinels=sentinels,
        visible_blocks=[*identity.values(), *fingerprint_blocks],
        structure_tokens=[
            f"{tag}:{_category(text)}"
            for tag, text, _ in parser.blocks
            if tag != "title" and normalize_key(text) not in NOISE_TEXT
        ],
        identity_text=" ".join([*identity.values(), identity_text]),
    )


def _linkedin_experience_stable_id(entry: dict[str, Any]) -> str | None:
    explicit = normalize_text(str(entry.get("stable_id") or ""))
    if explicit.startswith("urn:li:"):
        return explicit
    detail_url = normalize_text(str(entry.get("detail_url") or ""))
    if "/details/experience" in detail_url:
        return f"linkedin-exp-id:{_sha(canonical_url(detail_url), 20)}"
    company_url = normalize_text(str(entry.get("company_url") or entry.get("href") or ""))
    start_date = normalize_key(str(entry.get("start_date") or ""))
    if company_url.startswith(("http://", "https://")) and start_date:
        return f"linkedin-exp-url:{_sha(f'{canonical_url(company_url)}|{start_date}', 20)}"
    company = normalize_key(str(entry.get("company") or ""))
    if company and start_date:
        return f"linkedin-exp-derived:{_sha(f'{company}|{start_date}', 20)}"
    return explicit or None


def _linkedin_experience_text(entry: dict[str, Any]) -> str:
    date_range = " – ".join(
        value
        for value in (
            normalize_text(str(entry.get("start_date") or "")),
            normalize_text(str(entry.get("end_date") or "")),
        )
        if value
    )
    values = [
        entry.get("title"),
        entry.get("company"),
        entry.get("employment_type"),
        date_range,
        entry.get("location"),
    ]
    structured = " · ".join(normalize_text(str(value)) for value in values if value)
    return structured or normalize_text(str(entry.get("text") or ""))


def _extract_linkedin_structured(body: dict[str, Any]) -> FeatureSnapshot:
    """Extract the compact weekly profile/experience state from a public projection."""
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    identity = {
        key: str(profile[key])
        for key in ("name", "headline", "location", "current_role", "current_company", "summary")
        if profile.get(key)
    }
    items: list[SnapshotItem] = []
    sections = body.get("sections") if isinstance(body.get("sections"), list) else []
    allowed = {
        "about": "research",
        "关于": "research",
        "experience": "position",
        "工作经历": "position",
        "education": "education",
        "教育经历": "education",
        "publications": "publication",
        "出版物": "publication",
        "projects": "project",
        "项目经历": "project",
        "honors": "award",
        "honors & awards": "award",
        "honors awards": "award",
        "荣誉与奖项": "award",
        "skills": "research",
        "技能": "research",
    }
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = normalize_key(str(section.get("name") or ""))
        category = allowed.get(name)
        if category is None:
            continue
        entries = section.get("items") if isinstance(section.get("items"), list) else []
        for entry in entries:
            if isinstance(entry, str):
                entry = {"text": entry}
            if not isinstance(entry, dict):
                continue
            if category == "position":
                text = _linkedin_experience_text(entry)
                if not text:
                    continue
                attributes = {
                    key: entry.get(key)
                    for key in (
                        "title", "company", "employment_type", "location",
                        "start_date", "end_date", "description", "company_url", "detail_url",
                    )
                    if entry.get(key) not in (None, "")
                }
                stable = _linkedin_experience_stable_id(entry)
                items.append(_item("position", text, stable_id=stable, attributes=attributes))
                continue
            if not entry.get("text"):
                continue
            stable = entry.get("stable_id") or entry.get("href")
            items.append(
                _item(
                    category,
                    str(entry["text"]),
                    stable_id=str(stable) if stable else None,
                    attributes={
                        key: entry.get(key)
                        for key in ("title", "organization", "date", "description", "href")
                        if entry.get(key) not in (None, "")
                    },
                )
            )
    positions = [item for item in items if item.category == "position"]
    if positions and not identity.get("current_role"):
        current_positions = [
            item for item in positions
            if normalize_key(item.attributes.get("end_date", "")) in {"", "present", "至今", "现在"}
        ]
        current = max(
            current_positions or positions,
            key=lambda item: normalize_key(item.attributes.get("start_date", "")),
        )
        if current.attributes.get("title"):
            identity["current_role"] = current.attributes["title"]
        if current.attributes.get("company"):
            identity["current_company"] = current.attributes["company"]
    sentinels = [str(value) for value in body.get("sentinels", []) if value]
    if identity.get("name"):
        sentinels.append("profile-name")
    if positions:
        sentinels.append("experience")
    return _finalize_snapshot(
        "linkedin",
        identity,
        items,
        metrics={
            "visible_experience_count": len(positions),
            "experience_section_present": "experience" in {
                normalize_key(str(section.get("name") or ""))
                for section in sections if isinstance(section, dict)
            },
        },
        canonical=str(body.get("canonical_url")) if body.get("canonical_url") else None,
        section_count=len(sections),
        sentinels=sentinels,
    )


class _ScholarParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capture: str | None = None
        self.buffer: list[str] = []
        self.profile: dict[str, str] = {}
        self.rows: list[dict[str, str]] = []
        self.row: dict[str, str] | None = None
        self.row_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        amap = {k: v or "" for k, v in attrs}
        ident, classes = amap.get("id", ""), set(amap.get("class", "").split())
        mapping = {
            "gsc_prf_in": "name", "gsc_prf_i": "affiliation",
            "gsc_prf_int": "interests", "gsc_prf_ivh": "verified",
        }
        if ident in mapping:
            self.capture, self.buffer = mapping[ident], []
        if tag == "tr" and "gsc_a_tr" in classes:
            self.row, self.row_href = {}, amap.get("data-href")
        if self.row is not None:
            if "gsc_a_at" in classes:
                self.capture, self.buffer = "title", []
                self.row_href = amap.get("href") or self.row_href
            elif "gsc_a_at" not in classes and "gsc_a_t" in classes:
                pass
            elif "gsc_a_y" in classes:
                self.capture, self.buffer = "year", []
            elif "gsc_a_c" in classes:
                self.capture, self.buffer = "citations", []
            elif tag == "div" and ("gs_gray" in classes or "gsc_a_at" in classes):
                self.capture, self.buffer = "detail", []

    def handle_endtag(self, tag: str) -> None:
        if self.capture:
            value = normalize_text(" ".join(self.buffer))
            if self.row is not None and self.capture in {"title", "year", "citations", "detail"}:
                if self.capture == "detail":
                    key = "authors" if "authors" not in self.row else "venue"
                    self.row[key] = value
                else:
                    self.row[self.capture] = value
            else:
                self.profile[self.capture] = value
            self.capture, self.buffer = None, []
        if tag == "tr" and self.row is not None:
            self.row["href"] = self.row_href or ""
            if self.row.get("title"):
                self.rows.append(self.row)
            self.row, self.row_href = None, None

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)


def _canonical_scholar_publication_id(value: str) -> str:
    """Keep only Scholar's publication identity, never display locale params."""

    if not value:
        return value
    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.parse_qs(parsed.query)
    citation = query.get("citation_for_view")
    if citation and citation[0]:
        return f"scholar-citation:{citation[0]}"
    return value


def _extract_scholar(body: str) -> FeatureSnapshot:
    parser = _ScholarParser()
    parser.feed(body)
    identity = {k: v for k, v in parser.profile.items() if k != "verified"}
    items: list[SnapshotItem] = []
    for row in parser.rows:
        stable = _canonical_scholar_publication_id(
            row.get("href") or normalize_key(row.get("title", ""))
        )
        text = " · ".join(filter(None, (row.get("title"), row.get("authors"), row.get("venue"), row.get("year"))))
        items.append(
            _item(
                "publication",
                text,
                stable_id=stable,
                attributes={
                    key: row.get(key, "")
                    for key in ("title", "authors", "venue", "year")
                },
            )
        )
    for key, category in (("affiliation", "affiliation"), ("interests", "research")):
        if identity.get(key):
            items.append(_item(category, identity[key], stable_id=key))
    citations = sum(int(value) for value in re.findall(r'class="gsc_a_ac[^"]*"[^>]*>(\d+)<', body))
    sentinels = []
    if identity.get("name"):
        sentinels.append("profile-name")
    if parser.rows:
        sentinels.append("publication-table")
    return _finalize_snapshot(
        "scholar",
        identity,
        items,
        metrics={"visible_citations": citations},
        section_count=1 if parser.rows else 0,
        sentinels=sentinels,
        extractor_version="semantic-manifest-v5-scholar-recent100",
    )


def _extract_github(body: str | dict[str, Any] | list[Any]) -> FeatureSnapshot:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return _extract_generic(body, "github")
    if isinstance(body, list):
        profile, repositories = {}, body
    else:
        profile = body.get("profile", body) if isinstance(body, dict) else {}
        repositories = body.get("repositories", []) if isinstance(body, dict) else []
    identity = {
        key: str(profile.get(key))
        for key in ("login", "name", "company", "location", "bio", "blog")
        if profile.get(key) not in (None, "")
    }
    if profile.get("type"):
        identity["account_type"] = str(profile["type"])
    items: list[SnapshotItem] = []
    if identity:
        items.append(_item("identity", " · ".join(identity.values()), stable_id=str(profile.get("node_id") or profile.get("id") or identity.get("login"))))
    for repo in repositories[:100]:
        if not isinstance(repo, dict) or repo.get("fork"):
            continue
        stable = str(repo.get("node_id") or repo.get("id") or repo.get("full_name") or repo.get("name"))
        text = " · ".join(str(repo[key]) for key in ("name", "description", "language") if repo.get(key))
        items.append(_item("repository", text, stable_id=stable))
    metrics = {
        key: profile[key] for key in ("followers", "following", "public_repos") if isinstance(profile.get(key), (int, float))
    }
    sentinels = []
    if identity.get("login"):
        sentinels.append("profile-login")
    if repositories:
        sentinels.append("repository-list")
    return _finalize_snapshot(
        "github",
        identity,
        items,
        metrics=metrics,
        section_count=1 if repositories else 0,
        sentinels=sentinels,
    )


def extract_snapshot(kind: str, body: str | bytes | dict[str, Any] | list[Any]) -> FeatureSnapshot:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if kind == "linkedin" and isinstance(body, dict):
        return _extract_linkedin_structured(body)
    if kind == "github":
        return _extract_github(body)
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    if kind == "scholar":
        return _extract_scholar(body)
    return _extract_generic(body, kind)


def health_status(observation: FetchObservation, expected_kind: str) -> tuple[str, str]:
    code = observation.status_code
    if code in {404, 410}:
        return "gone", f"HTTP {code}"
    if code == 429:
        return "rate_limited", "HTTP 429"
    if code in {401, 403}:
        return "blocked", f"HTTP {code}"
    if code >= 500:
        return "temporary_error", f"HTTP {code}"
    if observation.error:
        # A challenge page may arrive with HTTP 200.  The fetch layer removes
        # that untrusted body before semantic extraction and records only a
        # bounded, cookie-free access fingerprint.  Treat it as an anonymous
        # access restriction rather than a network failure so the old baseline
        # is preserved and the report explains the real cause.
        if "access_fingerprint=" in observation.error.casefold():
            return "blocked", observation.error
        return "transport_error", observation.error
    final = (observation.final_url or "").casefold()
    text = observation.body.decode(errors="ignore") if isinstance(observation.body, bytes) else str(observation.body or "")
    probe = f"{final} {text[:10000]}".casefold()
    blockers = {
        "captcha": "captcha",
        "unusual traffic": "Google unusual traffic",
        "authwall": "authentication wall",
        "checkpoint/challenge": "challenge",
        "security verification": "security verification",
    }
    for marker, reason in blockers.items():
        if marker in probe:
            return "blocked", reason
    if expected_kind == "linkedin" and any(marker in final for marker in ("/login", "/authwall", "/checkpoint")):
        return "blocked", "LinkedIn redirected to login/authwall"
    if not (200 <= code < 400):
        return "degraded", f"HTTP {code}"
    return "healthy", "ok"


def _source_health_summary(kind: str, health: str, detail: str) -> str:
    """Render a health failure without implying that every source is dead."""

    labels = {
        "gone": "历史链接失效",
        "blocked": "匿名公开访问受限（不代表链接失效）",
        "rate_limited": "公开访问限流",
        "temporary_error": "暂时不可达",
        "transport_error": "传输状态未知（旧基线保留）",
        "degraded": "响应异常",
    }
    label = labels.get(health, "当前无法可靠检查")
    return f"{kind} {label}：{detail}"


@dataclass
class IdentityAssessment:
    status: Literal["verified", "review", "conflict", "unverified"]
    confidence: float
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def assess_identity_binding(
    snapshot: FeatureSnapshot,
    *,
    expected_names: Iterable[str] = (),
    source_scoped_names: Iterable[str] = (),
    expected_url: str | None = None,
    curated_binding: bool = False,
) -> IdentityAssessment:
    """Verify a person→source edge separately from transport/page completeness.

    The roster edge is provenance, not a claim that a public display name must be
    identical. A curated GitHub handle is therefore accepted when the API proves
    the exact handle is a User account. For other pages the roster edge must be
    corroborated by a Chinese/pinyin/English name found anywhere in stable visible
    text, unless a stronger platform-stable ID is available.
    """
    names = [normalize_text(str(value)) for value in expected_names if normalize_text(str(value))]
    scoped = [
        normalize_text(str(value))
        for value in source_scoped_names
        if normalize_text(str(value))
    ]
    expected_forms = _identity_name_forms(
        names[0] if names else "",
        [*names[1:], *scoped],
    )
    explicit_values = [
        snapshot.identity[key]
        for key in ("name", "name_or_title", "primary_heading", "login")
        if snapshot.identity.get(key)
    ]
    explicit_match, matched_form, matched_explicit = _identity_match(
        expected_forms,
        explicit_values,
    )
    body_values = [snapshot.identity_search_text] if snapshot.identity_search_text else []
    body_match, body_form, _ = _identity_match(expected_forms, body_values)
    scoped_forms = _identity_name_forms(scoped[0], scoped[1:]) if scoped else set()
    scoped_match, scoped_form, _ = _identity_match(
        scoped_forms,
        [*explicit_values, *body_values],
    )
    url_name_match, url_form = _identity_url_match(expected_forms, expected_url)

    stable_id_match = False
    expected_external_id = None
    observed_external_id = None
    if expected_url:
        expected_external_id = source_external_id(snapshot.kind, expected_url)
        if snapshot.kind == "github" and snapshot.identity.get("login"):
            observed_external_id = f"github:{snapshot.identity['login'].casefold()}"
            stable_id_match = expected_external_id == observed_external_id
        elif snapshot.kind == "linkedin" and snapshot.canonical_url:
            observed_external_id = source_external_id("linkedin", snapshot.canonical_url)
            stable_id_match = expected_external_id == observed_external_id
        elif snapshot.kind == "scholar" and snapshot.canonical_url:
            observed_external_id = source_external_id("scholar", snapshot.canonical_url)
            stable_id_match = expected_external_id == observed_external_id
        elif snapshot.kind == "homepage" and snapshot.canonical_url:
            stable_id_match = canonical_url(expected_url) == canonical_url(snapshot.canonical_url)
            observed_external_id = (
                source_external_id("homepage", snapshot.canonical_url)
                if stable_id_match
                else None
            )

    reasons: list[str] = []
    evidence = {
        "curated_roster_edge": bool(curated_binding),
        "explicit_name_match": explicit_match,
        "whole_page_name_match": body_match,
        "source_scoped_name_match": scoped_match,
        "url_name_match": url_name_match,
        "stable_platform_id_match": stable_id_match,
        "matched_expected_form": scoped_form or matched_form or body_form or url_form,
        "matched_explicit_value": matched_explicit,
        "observed_explicit_values": explicit_values[:8],
        "expected_external_id": expected_external_id,
        "observed_external_id": observed_external_id,
    }
    if snapshot.kind == "github" and snapshot.identity.get("account_type", "").casefold() not in {"", "user"}:
        return IdentityAssessment(
            "conflict",
            0.99,
            ["GitHub 地址不是个人 User profile"],
            evidence,
        )
    if (
        snapshot.kind == "linkedin"
        and expected_url
        and snapshot.canonical_url
        and not stable_id_match
    ):
        return IdentityAssessment(
            "conflict",
            0.99,
            ["LinkedIn canonical profile ID 与目标 URL 不一致"],
            evidence,
        )
    if scoped_match:
        reasons.append("来源专属英文名/显示名与页面一致")
        return IdentityAssessment("verified", 0.99, reasons, evidence)
    if explicit_match:
        reasons.append("页面显式姓名与中文名/拼音/英文名一致")
        return IdentityAssessment("verified", 0.98, reasons, evidence)
    if body_match:
        reasons.append("整页稳定文本中找到中文名对应的拼音/英文名")
        return IdentityAssessment("verified", 0.94, reasons, evidence)
    if snapshot.kind == "homepage" and curated_binding and url_name_match:
        reasons.append("原名单明确绑定，且个人域名/路径包含对应拼音或英文名")
        return IdentityAssessment("verified", 0.93, reasons, evidence)
    if (
        snapshot.kind == "github"
        and curated_binding
        and stable_id_match
        and snapshot.identity.get("account_type", "").casefold() == "user"
    ):
        reasons.append("原名单明确绑定 GitHub handle，API 确认为同一 User 稳定账号")
        return IdentityAssessment("verified", 0.96, reasons, evidence)
    if curated_binding:
        reasons.append("原名单有人物—URL关系，但当前页面未找到可交叉验证姓名")
        return IdentityAssessment("review", 0.68, reasons, evidence)
    if not names:
        return IdentityAssessment("unverified", 0.0, ["没有待核验人物姓名"], evidence)
    reasons.append("页面未找到跟踪对象的中文名、拼音或来源专属英文名")
    return IdentityAssessment("review", 0.35, reasons, evidence)


def assess_snapshot(
    snapshot: FeatureSnapshot,
    previous: FeatureSnapshot | None = None,
    *,
    expected_names: Iterable[str] = (),
    source_scoped_names: Iterable[str] = (),
    expected_url: str | None = None,
    curated_binding: bool = False,
    identity_assessment: IdentityAssessment | None = None,
) -> tuple[float, list[str], bool]:
    """Score whether a 200 response is complete enough to compare safely."""
    expected_names = list(expected_names)
    source_scoped_names = list(source_scoped_names)
    score = 1.0
    reasons: list[str] = []
    usable = True
    minimum_tokens = {"homepage": 5, "scholar": 4, "github": 1, "linkedin": 6}[snapshot.kind]
    if snapshot.token_count < minimum_tokens or snapshot.item_count == 0:
        score -= 0.45
        reasons.append("提取到的稳定内容过少")
        # Identity metadata alone is useful for binding, but it is not a
        # semantic page baseline.  Previously the 0.45 penalty left an exact
        # score of 0.55, which passed the generic score threshold and allowed a
        # title-only/SPA shell with zero stable items to be counted as parsed.
        usable = False
    if not snapshot.identity:
        score -= 0.25
        reasons.append("缺少人物身份哨兵")
    if snapshot.truncated:
        score -= 0.15
        reasons.append("页面内容超过语义清单上限")
    required_sentinel = {
        "scholar": "profile-name",
        "github": "profile-login",
        "linkedin": "profile-name",
    }.get(snapshot.kind)
    if required_sentinel and required_sentinel not in snapshot.sentinels:
        score -= 0.4
        reasons.append(f"缺少 {required_sentinel} 哨兵")

    identity_result = identity_assessment or assess_identity_binding(
        snapshot,
        expected_names=expected_names,
        source_scoped_names=source_scoped_names,
        expected_url=expected_url,
        curated_binding=curated_binding,
    )
    if identity_result.status == "conflict":
        score -= 0.8
        reasons.extend(identity_result.reasons)
        usable = False
    elif identity_result.status == "review" and expected_names:
        score -= 0.55
        reasons.extend(identity_result.reasons)
        usable = False
    elif identity_result.status == "unverified" and expected_names:
        score -= 0.45
        reasons.extend(identity_result.reasons)
        usable = False
    if snapshot.kind == "linkedin" and previous:
        previous_experience_count = int(previous.metrics.get("visible_experience_count") or 0)
        current_experience_count = int(snapshot.metrics.get("visible_experience_count") or 0)
        experience_present = bool(snapshot.metrics.get("experience_section_present"))
        if previous_experience_count and not experience_present:
            score -= 0.7
            reasons.append("LinkedIn Experience 分区消失，疑似折叠或公开内容不完整")
            usable = False
        elif previous_experience_count and current_experience_count == 0:
            score -= 0.7
            reasons.append("LinkedIn Experience 条目为空，拒绝覆盖历史经历基线")
            usable = False

    if previous and previous.item_count >= 4:
        ratio = snapshot.item_count / max(1, previous.item_count)
        previous_sentinels = set(previous.sentinels)
        sentinel_overlap = (
            len(previous_sentinels & set(snapshot.sentinels)) / len(previous_sentinels)
            if previous_sentinels
            else 1.0
        )
        if ratio < 0.25 and sentinel_overlap < 0.5:
            score -= 0.65
            reasons.append("内容骤减且页面哨兵同时消失，疑似半渲染/登录墙")
            usable = False
        elif ratio < 0.5:
            score -= 0.2
            reasons.append("内容显著减少；仅作为破坏性变更候选")
    if score < 0.55:
        usable = False
    return max(0.0, round(score, 3)), reasons, usable


def compare_snapshots(old: FeatureSnapshot | None, new: FeatureSnapshot) -> ChangeDecision:
    if old is None:
        return ChangeDecision("baseline", 0.0, "建立首次语义基线")
    if old.semantic_hash == new.semantic_hash:
        if not old.visible_content_hash or not old.critical_hash:
            return ChangeDecision(
                "unchanged",
                0.0,
                "旧版基线语义清单一致；补建整页与关键字段指纹",
            )
        if (
            old.visible_content_hash == new.visible_content_hash
            and old.critical_hash == new.critical_hash
        ):
            return ChangeDecision(
                "unchanged",
                0.0,
                (
                    "稳定文本集合和关键字段均未变化；仅顺序或页面结构变化"
                    if (
                        old.ordered_content_hash != new.ordered_content_hash
                        or old.structure_hash != new.structure_hash
                    )
                    else "语义清单、整页稳定文本和关键字段指纹均未变化"
                ),
            )
        return ChangeDecision(
            "ambiguous",
            0.55,
            "整页稳定文本指纹变化，但现有语义分类未定位到具体字段",
            modifications=[
                {
                    "category": "content",
                    "field": "visible_content_fingerprint",
                    "before": old.visible_content_hash,
                    "after": new.visible_content_hash,
                }
            ],
        )
    def comparison_key(item: SnapshotItem) -> tuple[str, str]:
        stable_id = item.stable_id or item.key
        if item.category == "publication":
            stable_id = _canonical_scholar_publication_id(stable_id)
        if stable_id.startswith(("http://", "https://")):
            # Homepage fetch authority preserves a meaningful leading ``www``.
            # Item links, however, used to drop it globally.  Treat that one
            # legacy ID migration as equivalent during snapshot comparison so
            # a parser/canonicalization upgrade cannot publish ``text → text``
            # as a person change.  The stored/fetched URL itself is untouched.
            parsed = urllib.parse.urlsplit(stable_id)
            if parsed.netloc.casefold().startswith("www."):
                stable_id = urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc[4:],
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        return item.category, stable_id

    # Apply the current chrome/noise vocabulary to both historical and current
    # items.  Otherwise removing a legacy footer link such as ``Privacy`` can
    # be published once as a person change even though new extraction already
    # knows not to ingest it.
    old_by = {
        comparison_key(item): item
        for item in old.items
        if normalize_key(item.text) not in NOISE_TEXT
    }
    new_by = {
        comparison_key(item): item
        for item in new.items
        if normalize_key(item.text) not in NOISE_TEXT
    }
    additions = [item for key, item in new_by.items() if key not in old_by]
    removals = [item for key, item in old_by.items() if key not in new_by]
    modifications: list[dict[str, Any]] = []
    for key in set(old_by) & set(new_by):
        before, after = old_by[key], new_by[key]
        scholar_publication = (
            old.kind == "scholar"
            and new.kind == "scholar"
            and after.category == "publication"
        )
        compared_attribute_names = (
            {"title", "year"}
            if scholar_publication
            else set(before.attributes) | set(after.attributes)
        )
        changed_fields = sorted(
            field_name
            for field_name in compared_attribute_names
            if normalize_key(before.attributes.get(field_name, ""))
            != normalize_key(after.attributes.get(field_name, ""))
        )
        text_changed = normalize_key(before.text) != normalize_key(after.text)
        if scholar_publication:
            # Authors and venue strings are frequently enriched by Scholar after
            # indexing. They are retained for display, but only title/year edits
            # are treated as a substantive change for an existing publication ID.
            text_changed = False
        if text_changed or changed_fields:
            modifications.append({
                "category": after.category,
                "stable_id": after.stable_id,
                "before": before.text,
                "after": after.text,
                "before_attributes": before.attributes,
                "after_attributes": after.attributes,
                "changed_fields": changed_fields,
            })
    for key in sorted(set(old.identity) | set(new.identity)):
        before = old.identity.get(key, "")
        after = new.identity.get(key, "")
        if normalize_key(before) != normalize_key(after):
            modifications.append({
                "category": "identity",
                "field": key,
                "before": before,
                "after": after,
            })
    if old.kind == "scholar" and new.kind == "scholar":
        # The monitored view is a bounded recent-publications window. A paper
        # falling out of that window is not evidence that the author deleted it.
        removals = [item for item in removals if item.category != "publication"]

    # Pair likely edits that lack a stable source ID.
    remaining_add, remaining_remove = [], list(removals)
    for added in additions:
        best = max(
            (removed for removed in remaining_remove if removed.category == added.category),
            key=lambda item: SequenceMatcher(None, normalize_key(item.text), normalize_key(added.text)).ratio(),
            default=None,
        )
        ratio = SequenceMatcher(None, normalize_key(best.text), normalize_key(added.text)).ratio() if best else 0
        if best and ratio >= 0.72:
            modifications.append({
                "category": added.category,
                "stable_id": added.stable_id,
                "before": best.text,
                "after": added.text,
                "before_attributes": best.attributes,
                "after_attributes": added.attributes,
                "changed_fields": sorted(
                    field_name
                    for field_name in set(best.attributes) | set(added.attributes)
                    if normalize_key(best.attributes.get(field_name, ""))
                    != normalize_key(added.attributes.get(field_name, ""))
                ),
            })
            remaining_remove.remove(best)
        else:
            remaining_add.append(added)
    additions, removals = remaining_add, remaining_remove
    identity_changed = any(item["category"] == "identity" for item in modifications)
    meaningful = [
        item for item in [*additions, *removals]
        if item.category in MEANINGFUL_CATEGORIES
    ]
    meaningful_mods = [item for item in modifications if item["category"] in MEANINGFUL_CATEGORIES]
    stable_item_deltas = [
        item for item in [*additions, *removals]
        if item.stable_id
    ]
    unclassified_item_deltas = [
        item for item in [*additions, *removals]
        if item.category not in MEANINGFUL_CATEGORIES and not item.stable_id
    ]
    sim_distance = _hamming(old.simhash64, new.simhash64)
    if (
        not additions
        and not removals
        and not modifications
        and not identity_changed
        and old.kind == "scholar"
        and new.kind == "scholar"
    ):
        return ChangeDecision(
            "unchanged",
            0.0,
            "Scholar 论文身份及标题/年份未变化；已忽略 locale、排序、窗口淘汰或元数据补全",
        )
    if not additions and not removals and not modifications and not identity_changed:
        return ChangeDecision("noise", 0.0, "只有计数、排序或页面运行噪声变化")
    score = min(
        1.0,
        0.28 * len(meaningful)
        + 0.28 * len(stable_item_deltas)
        + 0.18 * len(unclassified_item_deltas)
        + 0.38 * len(meaningful_mods)
        + (0.55 if identity_changed else 0),
    )
    payload = {
        "additions": [asdict(item) for item in additions[:12]],
        "removals": [asdict(item) for item in removals[:12]],
        "modifications": modifications[:12],
    }
    if (
        identity_changed
        or meaningful
        or meaningful_mods
        or stable_item_deltas
        or unclassified_item_deltas
    ):
        return ChangeDecision(
            "changed", max(score, 0.65), "检测到与人物进展相关的稳定内容变化",
            payload["additions"], payload["removals"], payload["modifications"],
        )
    if sim_distance <= 2:
        return ChangeDecision("noise", 0.1, "版式或极小文本噪声变化")
    return ChangeDecision(
        "ambiguous", max(0.25, min(0.6, sim_distance / 32)), "存在未分类文本变化，建议低成本模型复核",
        payload["additions"], payload["removals"], payload["modifications"],
    )


def confirmations_required(
    decision: ChangeDecision,
    kind: str,
    snapshot: FeatureSnapshot | None = None,
) -> int:
    """Return the number of identical healthy observations needed to publish."""
    if decision.status != "changed":
        return 0
    if kind == "linkedin":
        if decision.removals:
            return 2
        changed_items = [*decision.additions, *decision.modifications]
        position_changes = [
            item for item in changed_items if item.get("category") == "position"
        ]
        stable_ids = [str(item.get("stable_id") or "") for item in position_changes]
        strong = bool(stable_ids) and all(
            value.startswith(("urn:li:", "linkedin-exp-id:", "linkedin-exp-url:"))
            for value in stable_ids
        )
        position_or_identity_only = bool(changed_items) and all(
            item.get("category") in {"position", "identity"} for item in changed_items
        )
        identity_only = bool(changed_items) and all(
            item.get("category") == "identity" for item in changed_items
        )
        direct_public = snapshot is not None and snapshot.retrieval_mode != "search_index"
        if identity_only and direct_public:
            return 1
        if (
            strong
            and position_or_identity_only
            and direct_public
        ):
            return 1
        return 2
    if decision.removals or any(item.get("category") == "identity" for item in decision.modifications):
        return 3
    stable_additions = bool(decision.additions) and all(item.get("stable_id") for item in decision.additions)
    if not decision.modifications and stable_additions and kind in {"scholar", "github"}:
        return 1
    return 2


def _scheduled_next_check_at(
    observed_at: str,
    *,
    cadence_days: int,
    health_status: str,
    consecutive_failures: int,
    source_id: str,
) -> str:
    """Schedule healthy cadence or an earlier, jittered recovery probe."""

    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
    except ValueError:
        observed = datetime.now(timezone.utc)
    failures = max(1, int(consecutive_failures or 1))
    jitter = timedelta(minutes=int(_sha(source_id, 4), 16) % 31)
    if health_status == "rate_limited":
        delay = timedelta(hours=min(24, 6 * (2 ** (failures - 1)))) + jitter
    elif health_status in {"temporary_error", "transport_error"}:
        delay = timedelta(hours=min(24, 2 ** (failures - 1))) + jitter
    elif health_status == "degraded":
        delay = timedelta(hours=24) + jitter
    else:
        delay = timedelta(days=max(1, int(cadence_days or 7)))
    return (observed + delay).isoformat()


class LightTracker:
    """SQLite-backed low-storage researcher tracker.

    It retains a confirmed semantic manifest plus at most one candidate manifest
    per source. Run history keeps hashes, health, quality and compact deltas,
    never complete page bodies.
    """

    def __init__(self, database: str | Path):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS people (
          person_key TEXT PRIMARY KEY, canonical_name TEXT NOT NULL,
          normalized_name TEXT NOT NULL, secondary_id_type TEXT NOT NULL,
          secondary_id_value TEXT NOT NULL, aliases_json TEXT NOT NULL,
          profile_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        DROP INDEX IF EXISTS people_secondary;
        CREATE UNIQUE INDEX IF NOT EXISTS people_name_secondary
          ON people(normalized_name, secondary_id_type, secondary_id_value);
        CREATE TABLE IF NOT EXISTS sources (
          source_id TEXT PRIMARY KEY, person_key TEXT NOT NULL, kind TEXT NOT NULL,
          url TEXT NOT NULL, external_id TEXT NOT NULL, snapshot_json TEXT,
          semantic_hash TEXT, health_status TEXT NOT NULL DEFAULT 'never_checked',
          health_detail TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
          last_checked_at TEXT, last_changed_at TEXT,
          UNIQUE(person_key, kind, url), FOREIGN KEY(person_key) REFERENCES people(person_key)
        );
        CREATE INDEX IF NOT EXISTS sources_external ON sources(kind, external_id);
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
          trigger TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations (
          observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
          observed_at TEXT NOT NULL, health_status TEXT NOT NULL, semantic_hash TEXT,
          decision_status TEXT NOT NULL, score REAL NOT NULL, delta_json TEXT NOT NULL,
          summary TEXT NOT NULL, reviewer TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationships (
          relationship_id TEXT PRIMARY KEY, left_person_key TEXT NOT NULL,
          relation_type TEXT NOT NULL, right_person_key TEXT NOT NULL,
          evidence_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_curation_audit (
          audit_id TEXT PRIMARY KEY,
          source_id TEXT,
          person_key TEXT NOT NULL,
          registry_person_id TEXT,
          action TEXT NOT NULL,
          url TEXT NOT NULL,
          replacement_url TEXT,
          target_identity TEXT,
          note TEXT NOT NULL,
          source_state_json TEXT NOT NULL,
          decided_by TEXT NOT NULL,
          decided_at TEXT NOT NULL
        );
        """)
        self._ensure_column("sources", "candidate_snapshot_json", "TEXT")
        self._ensure_column("sources", "candidate_hash", "TEXT")
        self._ensure_column("sources", "candidate_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("sources", "candidate_first_seen_at", "TEXT")
        self._ensure_column("sources", "candidate_decision_json", "TEXT")
        self._ensure_column("sources", "quality_json", "TEXT")
        self._ensure_column("sources", "etag", "TEXT")
        self._ensure_column("sources", "last_modified", "TEXT")
        self._ensure_column("sources", "cadence_days", "INTEGER NOT NULL DEFAULT 7")
        self._ensure_column("sources", "next_check_at", "TEXT")
        self._ensure_column("sources", "last_success_at", "TEXT")
        self._ensure_column("sources", "binding_status", "TEXT NOT NULL DEFAULT 'unverified'")
        self._ensure_column("sources", "binding_confidence", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("sources", "binding_reason", "TEXT")
        self._ensure_column("sources", "binding_evidence_json", "TEXT")
        self._ensure_column("sources", "binding_verified_at", "TEXT")
        self._ensure_column("sources", "last_full_fetch_at", "TEXT")
        self._ensure_column("sources", "full_fetch_interval_days", "INTEGER NOT NULL DEFAULT 7")
        self._ensure_column("sources", "tracking_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("sources", "is_primary", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("sources", "attention_level", "TEXT NOT NULL DEFAULT 'normal'")
        self._ensure_column("sources", "curation_status", "TEXT NOT NULL DEFAULT 'auto'")
        self._ensure_column("sources", "curation_note", "TEXT")
        self._ensure_column("sources", "retrieval_config_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("observations", "quality_json", "TEXT")
        self._ensure_column("observations", "confirmation_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("observations", "confirmations_required", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("observations", "extractor_version", "TEXT")
        self.db.commit()

    def _ensure_column(self, table: str, name: str, definition: str) -> None:
        columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def add_person(
        self,
        canonical_name: str,
        *,
        aliases: list[str] | None = None,
        urls: list[str] | None = None,
        profile: dict[str, Any] | None = None,
        secondary_id: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        sources: dict[str, list[str]] = {}
        for value in urls or []:
            kind = classify_url(value)
            if kind in SOURCE_KINDS:
                canonical = canonical_url(value)
                if canonical not in sources.setdefault(kind, []):
                    sources[kind].append(canonical)
        if not sources:
            raise ValueError("入库失败：必须至少找到个人主页、Google Scholar、GitHub、LinkedIn 之一")
        if secondary_id is None:
            priority = next((kind for kind in ("scholar", "github", "linkedin", "homepage") if kind in sources), None)
            assert priority
            ext = source_external_id(priority, sources[priority][0])
            secondary_id = priority, ext.split(":", 1)[1]
        id_type, id_value = secondary_id[0].strip(), secondary_id[1].strip().casefold()
        incoming_name = normalize_text(canonical_name)
        incoming_aliases = [normalize_text(value) for value in aliases or [] if normalize_text(value)]
        incoming_profile = profile or {}
        normalized_name = normalize_key(incoming_name)
        existing = self.db.execute(
            """SELECT * FROM people
               WHERE normalized_name=? AND secondary_id_type=? AND secondary_id_value=?""",
            (normalized_name, id_type, id_value),
        ).fetchone()
        if not existing:
            shared: dict[str, sqlite3.Row] = {}
            for kind, urls_for_kind in sources.items():
                for url in urls_for_kind:
                    external_id = source_external_id(kind, url)
                    for row in self.db.execute(
                        """SELECT p.* FROM people p
                           JOIN sources s ON s.person_key=p.person_key
                           WHERE s.kind=? AND s.external_id=?""",
                        (kind, external_id),
                    ):
                        known_aliases = json.loads(row["aliases_json"])
                        if _identity_name_forms(incoming_name, incoming_aliases) & _identity_name_forms(
                            row["canonical_name"], known_aliases
                        ):
                            shared[row["person_key"]] = row
            if len(shared) > 1:
                raise ValueError("入库失败：同一必要主页匹配到多个兼容人物，需要人工消歧")
            existing = next(iter(shared.values()), None)
        now = utc_now()
        if existing:
            person_key = existing["person_key"]
            stored_name = existing["canonical_name"]
            normalized_name = existing["normalized_name"]
            id_type = existing["secondary_id_type"]
            id_value = existing["secondary_id_value"]
            stored_aliases = json.loads(existing["aliases_json"])
            merged_aliases = list(dict.fromkeys([
                *stored_aliases,
                *incoming_aliases,
                *([] if normalize_key(incoming_name) == normalize_key(stored_name) else [incoming_name]),
            ]))
            stored_profile = json.loads(existing["profile_json"])
            merged_profile = _merge_person_profile(stored_profile, incoming_profile)
        else:
            person_key = f"{_slug(incoming_name)}--{id_type}-{_sha(id_value, 10)}"
            stored_name = incoming_name
            merged_aliases = list(dict.fromkeys(incoming_aliases))
            merged_profile = _merge_person_profile({}, incoming_profile)
        self.db.execute(
            """INSERT INTO people VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(person_key) DO UPDATE SET
                 canonical_name=excluded.canonical_name, aliases_json=excluded.aliases_json,
                 profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
            (
                person_key, stored_name, normalized_name,
                id_type, id_value, json.dumps(merged_aliases, ensure_ascii=False),
                json.dumps(merged_profile, ensure_ascii=False), now, now,
            ),
        )
        for kind, urls_for_kind in sources.items():
            for url in urls_for_kind:
                external_id = source_external_id(kind, url)
                source_id = f"src_{_sha(f'{person_key}|{kind}|{url}', 20)}"
                self.db.execute(
                    """INSERT INTO sources(source_id,person_key,kind,url,external_id)
                       VALUES(?,?,?,?,?) ON CONFLICT(person_key,kind,url) DO NOTHING""",
                    (source_id, person_key, kind, url, external_id),
                )
        self.db.commit()
        return self.person(person_key)

    def person(self, person_key: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM people WHERE person_key=?", (person_key,)).fetchone()
        if not row:
            raise KeyError(person_key)
        result = dict(row)
        result["aliases"] = json.loads(result.pop("aliases_json"))
        result["profile"] = json.loads(result.pop("profile_json"))
        result["sources"] = [dict(item) for item in self.db.execute(
            """SELECT source_id,kind,url,external_id,health_status,health_detail,
                      consecutive_failures,last_checked_at,last_changed_at,etag,last_modified,
                      candidate_count,candidate_first_seen_at,cadence_days,next_check_at,last_success_at,
                      binding_status,binding_confidence,binding_reason,binding_evidence_json,
                      binding_verified_at,last_full_fetch_at,full_fetch_interval_days,
                      tracking_enabled,is_primary,attention_level,curation_status,
                      curation_note,retrieval_config_json
               FROM sources WHERE person_key=? ORDER BY kind""",
            (person_key,),
        )]
        for source in result["sources"]:
            source["binding_evidence"] = (
                json.loads(source.pop("binding_evidence_json"))
                if source.get("binding_evidence_json")
                else {}
            )
            source["retrieval_config"] = json.loads(
                source.pop("retrieval_config_json") or "{}"
            )
        return result

    def list_people(self) -> list[dict[str, Any]]:
        return [self.person(row["person_key"]) for row in self.db.execute("SELECT person_key FROM people ORDER BY canonical_name")]

    def configure_linkedin_weekly(self, source_id: str, *, cadence_days: int = 7) -> dict[str, Any]:
        if cadence_days < 1:
            raise ValueError("cadence_days must be at least 1")
        source = self.db.execute(
            "SELECT kind FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if not source:
            raise KeyError(source_id)
        if int(source["tracking_enabled"] or 0) != 1:
            raise ValueError(f"source is disabled from tracking: {source_id}")
        if source["kind"] != "linkedin":
            raise ValueError("weekly LinkedIn cadence can only be set on a LinkedIn source")
        self.db.execute(
            "UPDATE sources SET cadence_days=?,next_check_at=NULL WHERE source_id=?",
            (cadence_days, source_id),
        )
        self.db.commit()
        return self.linkedin_profile_state(source_id)

    def list_due_linkedin(self, as_of: str | None = None) -> list[dict[str, Any]]:
        return self.list_due_sources(as_of=as_of, kinds=("linkedin",))

    def list_due_sources(
        self,
        as_of: str | None = None,
        *,
        kinds: Iterable[str] = SOURCE_KINDS,
    ) -> list[dict[str, Any]]:
        boundary = as_of or utc_now()
        selected = tuple(dict.fromkeys(str(kind) for kind in kinds))
        if not selected or any(kind not in SOURCE_KINDS for kind in selected):
            raise ValueError("kinds must contain one or more supported source kinds")
        placeholders = ",".join("?" for _ in selected)
        rows = self.db.execute(
            f"""SELECT s.source_id,s.kind,s.url,s.external_id,s.health_status,s.health_detail,
                      s.last_checked_at,s.last_success_at,s.next_check_at,s.cadence_days,
                      s.etag,s.last_modified,s.last_full_fetch_at,s.full_fetch_interval_days,
                      s.binding_status,s.binding_confidence,s.binding_reason,
                      s.tracking_enabled,s.is_primary,s.attention_level,s.curation_status,
                      s.curation_note,s.retrieval_config_json,
                      p.person_key,p.canonical_name,p.aliases_json,p.profile_json
               FROM sources s JOIN people p ON p.person_key=s.person_key
               WHERE s.kind IN ({placeholders})
                 AND s.tracking_enabled=1
                 AND (s.next_check_at IS NULL OR s.next_check_at<=?)
               ORDER BY COALESCE(s.next_check_at,''),p.canonical_name""",
            (*selected, boundary),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
            item["profile"] = json.loads(item.pop("profile_json") or "{}")
            item["retrieval_config"] = json.loads(
                item.pop("retrieval_config_json") or "{}"
            )
            output.append(item)
        return output

    def linkedin_profile_state(self, source_id: str) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT s.*,p.canonical_name FROM sources s
               JOIN people p ON p.person_key=s.person_key
               WHERE s.source_id=? AND s.kind='linkedin'""",
            (source_id,),
        ).fetchone()
        if not row:
            raise KeyError(source_id)
        snapshot = FeatureSnapshot.from_json(row["snapshot_json"]) if row["snapshot_json"] else None
        experiences = []
        if snapshot:
            experiences = [
                {
                    "stable_id": item.stable_id,
                    "text": item.text,
                    **item.attributes,
                }
                for item in snapshot.items
                if item.category == "position"
            ]
            experiences.sort(
                key=lambda item: normalize_key(str(item.get("start_date") or "")),
                reverse=True,
            )
        return {
            "source_id": row["source_id"],
            "person_key": row["person_key"],
            "canonical_name": row["canonical_name"],
            "url": row["url"],
            "fixed_profile": snapshot.identity if snapshot else {},
            "experiences": experiences,
            "retrieval_mode": snapshot.retrieval_mode if snapshot else None,
            "health_status": row["health_status"],
            "health_detail": row["health_detail"],
            "last_checked_at": row["last_checked_at"],
            "last_success_at": row["last_success_at"],
            "next_check_at": row["next_check_at"],
            "cadence_days": row["cadence_days"],
        }

    def patch_profile(self, person_key: str, **fields: Any) -> dict[str, Any]:
        person = self.person(person_key)
        profile = person["profile"]
        for key, value in fields.items():
            if value not in (None, "", []):
                profile[key] = value
        self.db.execute(
            "UPDATE people SET profile_json=?,updated_at=? WHERE person_key=?",
            (json.dumps(profile, ensure_ascii=False), utc_now(), person_key),
        )
        self.db.commit()
        return self.person(person_key)

    def start_run(self, trigger: str = "manual") -> str:
        run_id = f"run_{_sha(f'{utc_now()}|{trigger}', 20)}"
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (run_id, utc_now(), None, trigger, "running"))
        self.db.commit()
        return run_id

    def observe(
        self,
        run_id: str,
        source_id: str,
        observation: FetchObservation,
        *,
        reviewer: Callable[[ChangeDecision], ChangeDecision] | None = None,
        timing_sink: dict[str, float] | None = None,
        ai_usage_sink: dict[str, int] | None = None,
    ) -> ChangeDecision:
        observe_started = perf_counter()

        def record_timing(name: str, started: float) -> None:
            if timing_sink is not None:
                timing_sink[name] = timing_sink.get(name, 0.0) + (
                    perf_counter() - started
                )

        def record_ai(name: str) -> None:
            if ai_usage_sink is not None:
                ai_usage_sink[name] = ai_usage_sink.get(name, 0) + 1

        def summarize_confirmed_change(
            decision: ChangeDecision,
            *,
            snapshot: FeatureSnapshot,
            quality_score: float,
            quality_reasons: list[str],
            identity_gate_passed: bool,
        ) -> ChangeDecision:
            context = {
                "person_key": source["person_key"],
                "source_id": source_id,
                "source_kind": source["kind"],
                "retrieval_mode": snapshot.retrieval_mode,
                "health_status": "healthy",
                "identity_gate_passed": identity_gate_passed,
                "quality_score": quality_score,
                "quality_reasons": quality_reasons,
                "has_confirmed_baseline": old is not None,
                "change_confirmed": True,
            }
            eligible, _ = DeepSeekDiffReviewer.summary_eligibility(
                decision,
                context,
            )
            if not eligible:
                record_ai("confirmed_summary_ineligible")
                return decision
            record_ai("confirmed_summary_eligible")
            summarize_method = getattr(reviewer, "summarize_confirmed", None)
            if not callable(summarize_method):
                record_ai("confirmed_summary_skipped_unconfigured")
                return decision
            record_ai("confirmed_summary_attempted")
            try:
                summarized = summarize_method(decision, context=context)
            except Exception as exc:
                record_ai("confirmed_summary_failed")
                decision.summary += (
                    f"；确认结果不受影响，模型摘要未生成"
                    f"（{type(exc).__name__}）"
                )
                return decision
            record_ai("confirmed_summary_completed")
            return summarized

        state_started = perf_counter()
        source = self.db.execute(
            """SELECT s.*,p.canonical_name,p.aliases_json,p.profile_json
               FROM sources s JOIN people p ON p.person_key=s.person_key
               WHERE s.source_id=?""",
            (source_id,),
        ).fetchone()
        if not source:
            raise KeyError(source_id)
        old = FeatureSnapshot.from_json(source["snapshot_json"]) if source["snapshot_json"] else None
        record_timing("source_state_load", state_started)
        health_started = perf_counter()
        health, detail = health_status(observation, source["kind"])
        record_timing("transport_health_gate", health_started)
        quality_payload: dict[str, Any] = {}
        extractor_version = old.extractor_version if old else None

        if observation.status_code == 304 and old is not None:
            transition_started = perf_counter()
            semantic_hash = source["semantic_hash"]
            if source["candidate_snapshot_json"] and source["candidate_decision_json"]:
                candidate = FeatureSnapshot.from_json(source["candidate_snapshot_json"])
                stored_decision = ChangeDecision(
                    **json.loads(source["candidate_decision_json"])
                )
                stored_quality = {}
                if source["quality_json"]:
                    try:
                        stored_quality = json.loads(source["quality_json"])
                    except (TypeError, json.JSONDecodeError):
                        stored_quality = {}

                # A 304 proves that the server representation associated with
                # the candidate ETag is unchanged; it does not prove that an
                # older comparator still classifies that representation as a
                # person change.  Re-run the current deterministic comparator
                # before incrementing or confirming a persisted candidate.
                # This is especially important after canonicalization or
                # extractor migrations, where a stale candidate can now be
                # recognized as noise without downloading the same body again.
                decision = compare_snapshots(old, candidate)
                decision.quality_score = float(
                    stored_quality.get("score")
                    or stored_decision.quality_score
                    or 1.0
                )
                decision.quality_reasons = [
                    str(value)
                    for value in (
                        stored_quality.get("reasons")
                        or stored_decision.quality_reasons
                        or []
                    )
                ]

                if decision.status in {"unchanged", "noise"}:
                    self.db.execute(
                        """UPDATE sources SET candidate_snapshot_json=NULL,
                           candidate_hash=NULL,candidate_count=0,
                           candidate_first_seen_at=NULL,candidate_decision_json=NULL,
                           health_status='healthy',health_detail='not modified',
                           consecutive_failures=0,last_checked_at=?,
                           etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified)
                           WHERE source_id=?""",
                        (
                            observation.observed_at,
                            observation.etag,
                            observation.last_modified,
                            source_id,
                        ),
                    )
                elif decision.status == "changed":
                    count = source["candidate_count"] + 1
                    required = confirmations_required(
                        decision,
                        source["kind"],
                        candidate,
                    )
                    decision.confirmation_count = count
                    decision.confirmations_required = required
                    if required and count >= required:
                        decision.status = "changed"
                        decision.summary += (
                            f"；候选变化经条件请求达到 {count}/{required} 次确认"
                        )
                        decision = summarize_confirmed_change(
                            decision,
                            snapshot=candidate,
                            quality_score=decision.quality_score,
                            quality_reasons=decision.quality_reasons,
                            identity_gate_passed=bool(
                                stored_quality.get("usable", True)
                            ),
                        )
                        semantic_hash = candidate.semantic_hash
                        self.db.execute(
                            """UPDATE sources SET snapshot_json=?,semantic_hash=?,candidate_snapshot_json=NULL,
                               candidate_hash=NULL,candidate_count=0,candidate_first_seen_at=NULL,
                               candidate_decision_json=NULL,health_status='healthy',health_detail='not modified',
                               consecutive_failures=0,last_checked_at=?,last_changed_at=?,
                               etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified)
                               WHERE source_id=?""",
                            (
                                candidate.to_json(), candidate.semantic_hash,
                                observation.observed_at, observation.observed_at,
                                observation.etag, observation.last_modified, source_id,
                            ),
                        )
                    else:
                        decision.status = "candidate"
                        decision.summary += (
                            f"；候选变化已重复 {count}/{required} 次，仍等待确认"
                        )
                        self.db.execute(
                            """UPDATE sources SET candidate_count=?,candidate_decision_json=?,
                               health_status='healthy',health_detail='not modified',consecutive_failures=0,
                               last_checked_at=?,etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified)
                               WHERE source_id=?""",
                            (
                                count, json.dumps(asdict(decision), ensure_ascii=False),
                                observation.observed_at, observation.etag,
                                observation.last_modified, source_id,
                            ),
                        )
                else:
                    count = source["candidate_count"] + 1
                    decision.status = "candidate"
                    decision.confirmation_count = count
                    decision.confirmations_required = 0
                    decision.summary += (
                        "；按当前规则仍需低成本模型或人工复核，304 不自动确认"
                    )
                    self.db.execute(
                        """UPDATE sources SET candidate_count=?,candidate_decision_json=?,
                           health_status='healthy',health_detail='not modified',consecutive_failures=0,
                           last_checked_at=?,etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified)
                           WHERE source_id=?""",
                        (
                            count, json.dumps(asdict(decision), ensure_ascii=False),
                            observation.observed_at, observation.etag,
                            observation.last_modified, source_id,
                        ),
                    )
            else:
                decision = ChangeDecision("unchanged", 0.0, "条件请求返回 304，页面表示未变化")
                self.db.execute(
                    """UPDATE sources SET health_status='healthy',health_detail='not modified',
                       consecutive_failures=0,last_checked_at=?,etag=COALESCE(?,etag),
                       last_modified=COALESCE(?,last_modified) WHERE source_id=?""",
                    (observation.observed_at, observation.etag, observation.last_modified, source_id),
                )
            record_timing(
                "conditional_confirmation_and_state_transition",
                transition_started,
            )
        elif health != "healthy" or observation.body is None:
            transition_started = perf_counter()
            failures = source["consecutive_failures"] + 1
            status = "source_issue" if failures >= 2 else "source_issue_pending"
            decision = ChangeDecision(
                status, min(1.0, 0.35 + failures * 0.2),
                _source_health_summary(source["kind"], health, detail),
                health_status=health,
                confirmation_count=failures,
                confirmations_required=2,
            )
            self.db.execute(
                "UPDATE sources SET health_status=?,health_detail=?,consecutive_failures=?,last_checked_at=? WHERE source_id=?",
                (health, detail, failures, observation.observed_at, source_id),
            )
            semantic_hash = source["semantic_hash"]
            record_timing("health_failure_state_transition", transition_started)
        else:
            extraction_started = perf_counter()
            snapshot = extract_snapshot(source["kind"], observation.body)
            snapshot.retrieval_mode = observation.retrieval_mode
            if not snapshot.canonical_url and observation.final_url:
                snapshot.canonical_url = canonical_url(observation.final_url)
            extractor_version = snapshot.extractor_version
            record_timing("semantic_extraction", extraction_started)
            quality_started = perf_counter()
            person_profile = json.loads(source["profile_json"] or "{}")
            curated_binding = bool(person_profile.get("registry_person_id"))
            source_identity_names = person_profile.get("source_identity_names") or {}
            scoped_names = (
                source_identity_names.get(source["external_id"], [])
                if isinstance(source_identity_names, dict)
                else []
            )
            if isinstance(scoped_names, str):
                scoped_names = [scoped_names]
            expected_names = [
                source["canonical_name"],
                *json.loads(source["aliases_json"] or "[]"),
            ]
            identity_result = assess_identity_binding(
                snapshot,
                expected_names=expected_names,
                source_scoped_names=scoped_names,
                expected_url=source["url"],
                curated_binding=curated_binding,
            )
            quality_score, quality_reasons, usable = assess_snapshot(
                snapshot,
                old,
                expected_names=expected_names,
                source_scoped_names=scoped_names,
                expected_url=source["url"],
                curated_binding=curated_binding,
                identity_assessment=identity_result,
            )
            record_timing("identity_and_completeness_gate", quality_started)
            quality_payload = {
                "score": quality_score,
                "reasons": quality_reasons,
                "usable": usable,
                "item_count": snapshot.item_count,
                "token_count": snapshot.token_count,
                "section_count": snapshot.section_count,
                "sentinels": snapshot.sentinels,
                "truncated": snapshot.truncated,
                "binding": asdict(identity_result),
            }
            if not usable:
                transition_started = perf_counter()
                if identity_result.status in {"review", "conflict"}:
                    status = (
                        "binding_conflict"
                        if identity_result.status == "conflict"
                        else "binding_review"
                    )
                    decision = ChangeDecision(
                        status,
                        identity_result.confidence,
                        f"{source['kind']} 页面可访问，但人物绑定需要核验："
                        f"{'；'.join(identity_result.reasons)}",
                        health_status="healthy",
                        quality_score=quality_score,
                        quality_reasons=quality_reasons,
                    )
                    self.db.execute(
                        """UPDATE sources SET health_status='healthy',health_detail='ok',
                           consecutive_failures=0,last_checked_at=?,last_full_fetch_at=?,
                           quality_json=?,binding_status=?,binding_confidence=?,
                           binding_reason=?,binding_evidence_json=?,binding_verified_at=NULL
                           WHERE source_id=?""",
                        (
                            observation.observed_at,
                            observation.observed_at,
                            json.dumps(quality_payload, ensure_ascii=False),
                            identity_result.status,
                            identity_result.confidence,
                            "；".join(identity_result.reasons),
                            json.dumps(identity_result.evidence, ensure_ascii=False),
                            source_id,
                        ),
                    )
                else:
                    failures = source["consecutive_failures"] + 1
                    status = "source_issue" if failures >= 2 else "source_issue_pending"
                    decision = ChangeDecision(
                        status,
                        min(1.0, 0.35 + failures * 0.2),
                        f"{source['kind']} 提取不完整：{'；'.join(quality_reasons)}",
                        health_status="degraded",
                        confirmation_count=failures,
                        confirmations_required=2,
                        quality_score=quality_score,
                        quality_reasons=quality_reasons,
                    )
                    self.db.execute(
                        """UPDATE sources SET health_status='degraded',health_detail=?,
                           consecutive_failures=?,last_checked_at=?,last_full_fetch_at=?,
                           quality_json=?,binding_status=?,binding_confidence=?,
                           binding_reason=?,binding_evidence_json=?,
                           binding_verified_at=? WHERE source_id=?""",
                        (
                            "；".join(quality_reasons),
                            failures,
                            observation.observed_at,
                            observation.observed_at,
                            json.dumps(quality_payload, ensure_ascii=False),
                            identity_result.status,
                            identity_result.confidence,
                            "；".join(identity_result.reasons),
                            json.dumps(identity_result.evidence, ensure_ascii=False),
                            (
                                observation.observed_at
                                if identity_result.status == "verified"
                                else None
                            ),
                            source_id,
                        ),
                    )
                semantic_hash = source["semantic_hash"]
                record_timing(
                    "quality_failure_state_transition",
                    transition_started,
                )
            else:
                diff_started = perf_counter()
                legacy_incompatible = (
                    old
                    and old.extractor_version != snapshot.extractor_version
                    and not (
                        old.extractor_version == "semantic-manifest-v3"
                        and snapshot.extractor_version == "semantic-manifest-v4"
                    )
                )
                if old and (
                    legacy_incompatible
                    or old.retrieval_mode != snapshot.retrieval_mode
                ):
                    decision = ChangeDecision(
                        "baseline",
                        0.0,
                        "提取器或检索模式发生变化，静默重建基线",
                    )
                else:
                    decision = compare_snapshots(old, snapshot)
                decision.quality_score = quality_score
                decision.quality_reasons = quality_reasons
                if decision.status == "ambiguous":
                    review_context = {
                        "person_key": source["person_key"],
                        "source_id": source_id,
                        "source_kind": source["kind"],
                        "retrieval_mode": snapshot.retrieval_mode,
                        "health_status": "healthy",
                        "identity_gate_passed": usable,
                        "quality_score": quality_score,
                        "quality_reasons": quality_reasons,
                        "has_confirmed_baseline": old is not None,
                    }
                    eligible, _ = DeepSeekDiffReviewer.eligibility(
                        decision,
                        review_context,
                    )
                    if eligible:
                        record_ai("ambiguous_review_eligible")
                    review_method = getattr(reviewer, "review", None)
                    if not callable(review_method):
                        if eligible:
                            record_ai("ambiguous_review_skipped_unconfigured")
                    else:
                        record_ai("ambiguous_review_attempted")
                        try:
                            decision = review_method(
                                decision,
                                context=review_context,
                            )
                        except Exception as exc:  # keep deterministic result and make fallback visible
                            record_ai("ambiguous_review_failed")
                            decision.summary += f"；模型复核失败，保留待审（{type(exc).__name__}）"
                        else:
                            record_ai("ambiguous_review_completed")
                record_timing("snapshot_diff_and_optional_review", diff_started)

                transition_started = perf_counter()
                accepted_snapshot = old
                changed_at = source["last_changed_at"]
                clear_candidate = False
                candidate_snapshot_json: str | None = source["candidate_snapshot_json"]
                candidate_hash: str | None = source["candidate_hash"]
                candidate_count = source["candidate_count"]
                candidate_first_seen = source["candidate_first_seen_at"]
                candidate_decision_json: str | None = source["candidate_decision_json"]

                if decision.status == "baseline":
                    accepted_snapshot = snapshot
                    clear_candidate = True
                elif decision.status == "unchanged":
                    accepted_snapshot = snapshot
                    clear_candidate = True
                elif decision.status == "noise":
                    # Noise is deliberately not allowed to advance the confirmed
                    # baseline. Otherwise a low-distance false negative can erase
                    # the reference needed to detect the same change next round.
                    accepted_snapshot = old
                    clear_candidate = True
                elif decision.status == "changed":
                    required = confirmations_required(decision, source["kind"], snapshot)
                    candidate_representation = snapshot.comparison_hash or snapshot.semantic_hash
                    count = candidate_count + 1 if candidate_hash == candidate_representation else 1
                    decision.confirmation_count = count
                    decision.confirmations_required = required
                    if count >= required:
                        accepted_snapshot = snapshot
                        changed_at = observation.observed_at
                        clear_candidate = True
                        decision.summary += f"；经 {count}/{required} 次一致观测确认"
                        decision = summarize_confirmed_change(
                            decision,
                            snapshot=snapshot,
                            quality_score=quality_score,
                            quality_reasons=quality_reasons,
                            identity_gate_passed=usable,
                        )
                    else:
                        decision.status = "candidate"
                        decision.summary += f"；候选 {count}/{required}，未写入确认基线"
                        candidate_snapshot_json = snapshot.to_json()
                        candidate_hash = candidate_representation
                        candidate_count = count
                        candidate_first_seen = (
                            source["candidate_first_seen_at"]
                            if source["candidate_hash"] == candidate_representation
                            else observation.observed_at
                        )
                        candidate_decision_json = json.dumps(asdict(decision), ensure_ascii=False)
                else:
                    candidate_representation = snapshot.comparison_hash or snapshot.semantic_hash
                    count = candidate_count + 1 if candidate_hash == candidate_representation else 1
                    decision.status = "candidate"
                    decision.confirmation_count = count
                    decision.confirmations_required = 0
                    decision.summary += "；稳定重现后仍需低成本模型或人工复核"
                    candidate_snapshot_json = snapshot.to_json()
                    candidate_hash = candidate_representation
                    candidate_count = count
                    candidate_first_seen = (
                        source["candidate_first_seen_at"]
                        if source["candidate_hash"] == candidate_representation
                        else observation.observed_at
                    )
                    candidate_decision_json = json.dumps(asdict(decision), ensure_ascii=False)

                if clear_candidate:
                    candidate_snapshot_json = candidate_hash = candidate_first_seen = candidate_decision_json = None
                    candidate_count = 0
                assert accepted_snapshot is not None
                self.db.execute(
                    """UPDATE sources SET snapshot_json=?,semantic_hash=?,
                       candidate_snapshot_json=?,candidate_hash=?,candidate_count=?,
                       candidate_first_seen_at=?,candidate_decision_json=?,
                       health_status='healthy',health_detail='ok',consecutive_failures=0,
                       last_checked_at=?,last_changed_at=?,quality_json=?,
                       etag=COALESCE(?,etag),last_modified=COALESCE(?,last_modified),
                       binding_status=?,binding_confidence=?,binding_reason=?,
                       binding_evidence_json=?,binding_verified_at=?,
                       last_full_fetch_at=?
                       WHERE source_id=?""",
                    (
                        accepted_snapshot.to_json(), accepted_snapshot.semantic_hash,
                        candidate_snapshot_json, candidate_hash, candidate_count,
                        candidate_first_seen, candidate_decision_json,
                        observation.observed_at, changed_at,
                        json.dumps(quality_payload, ensure_ascii=False),
                        observation.etag,
                        observation.last_modified,
                        identity_result.status,
                        identity_result.confidence,
                        "；".join(identity_result.reasons),
                        json.dumps(identity_result.evidence, ensure_ascii=False),
                        (
                            observation.observed_at
                            if identity_result.status == "verified"
                            else None
                        ),
                        observation.observed_at,
                        source_id,
                    ),
                )
                semantic_hash = snapshot.semantic_hash
                record_timing(
                    "confirmation_and_baseline_transition",
                    transition_started,
                )
        persist_started = perf_counter()
        failed_health = decision.health_status != "healthy"
        failure_count = (
            int(source["consecutive_failures"] or 0) + 1
            if failed_health
            else 0
        )
        next_check_at = _scheduled_next_check_at(
            observation.observed_at,
            cadence_days=int(source["cadence_days"] or 7),
            health_status=decision.health_status,
            consecutive_failures=failure_count,
            source_id=source_id,
        )
        successful_at = (
            observation.observed_at
            if decision.health_status == "healthy"
            and decision.status not in {"source_issue", "source_issue_pending"}
            else None
        )
        self.db.execute(
            """UPDATE sources SET next_check_at=?,
               last_success_at=COALESCE(?,last_success_at) WHERE source_id=?""",
            (next_check_at, successful_at, source_id),
        )

        observation_id = f"obs_{_sha(f'{run_id}|{source_id}|{observation.observed_at}', 20)}"
        delta = {
            "additions": decision.additions,
            "removals": decision.removals,
            "modifications": decision.modifications,
        }
        self.db.execute(
            """INSERT OR REPLACE INTO observations(
                 observation_id,run_id,source_id,observed_at,health_status,semantic_hash,
                 decision_status,score,delta_json,summary,reviewer,quality_json,
                 confirmation_count,confirmations_required,extractor_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id, run_id, source_id, observation.observed_at, decision.health_status,
                semantic_hash, decision.status, decision.score, json.dumps(delta, ensure_ascii=False),
                decision.summary, decision.reviewer, json.dumps(quality_payload, ensure_ascii=False),
                decision.confirmation_count, decision.confirmations_required, extractor_version,
            ),
        )
        self.db.commit()
        record_timing("observation_audit_persist", persist_started)
        record_timing("observe_total", observe_started)
        return decision

    def complete_run(self, run_id: str) -> None:
        self.db.execute("UPDATE runs SET completed_at=?,status='completed' WHERE run_id=?", (utc_now(), run_id))
        self.db.commit()

    def render_report(self, run_id: str) -> str:
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise KeyError(run_id)
        rows = self.db.execute(
            """SELECT o.*,s.kind,s.url,s.person_key,p.canonical_name,p.profile_json
               FROM observations o JOIN sources s ON s.source_id=o.source_id
               JOIN people p ON p.person_key=s.person_key
               WHERE o.run_id=? AND o.decision_status IN ('changed','source_issue')
               ORDER BY p.canonical_name,s.kind""",
            (run_id,),
        ).fetchall()
        pending = {
            row["decision_status"]: row["count"]
            for row in self.db.execute(
                """SELECT decision_status,COUNT(*) AS count FROM observations
                   WHERE run_id=? AND decision_status IN ('candidate','source_issue_pending')
                   GROUP BY decision_status""",
                (run_id,),
            )
        }
        lines = [
            "# 人才跟踪更新",
            "",
            f"- 扫描时间：{run['completed_at'] or run['started_at']}",
            f"- 有进展或来源异常的人数：{len(set(row['person_key'] for row in rows))}",
            f"- 待确认变化：{pending.get('candidate', 0)}；首次来源异常：{pending.get('source_issue_pending', 0)}",
            "",
        ]
        if not rows:
            lines.extend(["本轮没有发现已确认、需要汇报的新进展或持续来源异常。", ""])
            return "\n".join(lines)
        current: str | None = None
        for row in rows:
            if row["person_key"] != current:
                current = row["person_key"]
                profile = json.loads(row["profile_json"])
                context = "；".join(str(profile[key]) for key in ("school", "research_focus", "stage") if profile.get(key))
                lines.extend([f"## {row['canonical_name']}", "", f"{context}" if context else ""])
            delta = json.loads(row["delta_json"])
            if row["decision_status"] == "source_issue":
                lines.extend([f"- ⚠️ {row['summary']}（[{row['kind']}]({row['url']}））"])
                continue
            lines.append(f"### {row['kind']} 更新")
            lines.append("")
            lines.append(row["summary"])
            for item in delta["additions"][:8]:
                lines.append(f"- 新增：{item['text']}")
            for item in delta["modifications"][:8]:
                lines.append(f"- 更新：{item['before']} → {item['after']}")
            for item in delta["removals"][:5]:
                lines.append(f"- 页面不再显示：{item['text']}（仅记录页面变化，不自动推断离职或撤回）")
            lines.extend([f"- 来源：[{row['url']}]({row['url']})", ""])
        return "\n".join(line for line in lines if line is not None)

    def render_linkedin_weekly_report(self, run_id: str) -> str:
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise KeyError(run_id)
        rows = self.db.execute(
            """SELECT o.*,s.url,p.canonical_name
               FROM observations o JOIN sources s ON s.source_id=o.source_id
               JOIN people p ON p.person_key=s.person_key
               WHERE o.run_id=? AND s.kind='linkedin'
               ORDER BY p.canonical_name""",
            (run_id,),
        ).fetchall()
        lines = [
            "# LinkedIn 每周更新",
            "",
            f"- 扫描时间：{run['completed_at'] or run['started_at']}",
            "",
        ]
        updates: list[str] = []
        failed = 0
        for row in rows:
            if row["decision_status"] in {"source_issue", "source_issue_pending"}:
                failed += 1
                continue
            if row["decision_status"] != "changed":
                continue
            delta = json.loads(row["delta_json"])
            summaries: list[str] = []
            has_position_change = any(
                item.get("category") == "position"
                for item in [
                    *delta.get("additions", []),
                    *delta.get("modifications", []),
                ]
            )
            for item in delta.get("additions", []):
                if item.get("category") != "position":
                    continue
                attrs = item.get("attributes") or {}
                role = attrs.get("title") or item.get("text") or "新经历"
                company = attrs.get("company")
                dates = "–".join(
                    value for value in (attrs.get("start_date"), attrs.get("end_date")) if value
                )
                detail = f"新增工作经历：{role}"
                if company:
                    detail += f" @ {company}"
                if dates:
                    detail += f"（{dates}）"
                summaries.append(detail)
            for item in delta.get("modifications", []):
                category = item.get("category")
                if category == "position":
                    before = item.get("before_attributes") or {}
                    after = item.get("after_attributes") or {}
                    label = after.get("title") or before.get("title") or "工作经历"
                    company = after.get("company") or before.get("company")
                    changes = []
                    labels = {
                        "title": "职位",
                        "company": "公司",
                        "employment_type": "类型",
                        "location": "地点",
                        "start_date": "开始时间",
                        "end_date": "结束时间",
                    }
                    for field_name in item.get("changed_fields", []):
                        if field_name == "description":
                            changes.append("经历描述有编辑")
                            continue
                        if field_name not in labels:
                            continue
                        before_value = before.get(field_name) or "未填写"
                        after_value = after.get(field_name) or "未填写"
                        changes.append(f"{labels[field_name]}：{before_value} → {after_value}")
                    if not changes and item.get("before") != item.get("after"):
                        changes.append("展示文本有编辑")
                    prefix = f"更新工作经历：{label}"
                    if company:
                        prefix += f" @ {company}"
                    summaries.append(f"{prefix}；{'；'.join(changes)}")
                elif category == "identity":
                    field_name = item.get("field") or "profile"
                    if has_position_change and field_name in {"current_role", "current_company"}:
                        continue
                    labels = {
                        "headline": "个人标题",
                        "location": "所在地",
                        "current_role": "当前职位",
                        "current_company": "当前公司",
                        "summary": "简介",
                        "name": "姓名",
                    }
                    if field_name not in labels:
                        continue
                    if field_name == "summary":
                        summaries.append("个人简介有编辑")
                    else:
                        summaries.append(
                            f"{labels[field_name]}：{item.get('before') or '未填写'}"
                            f" → {item.get('after') or '未填写'}"
                        )
            if summaries:
                concise = "；".join(summaries[:3])
                updates.append(
                    f"- **{row['canonical_name']}**：{concise}。[LinkedIn]({row['url']})"
                )
        if updates:
            lines.extend(updates)
        else:
            lines.append("本周没有发现已确认的工作经历或固定资料编辑。")
        if failed:
            lines.extend(["", f"- 有 {failed} 个 LinkedIn profile 本周未能取得完整公开页面，旧基线保持不变。"])
        lines.append("")
        return "\n".join(lines)

    def match_person(
        self,
        canonical_name: str,
        *,
        aliases: list[str] | None = None,
        urls: list[str] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        input_names = {normalize_key(canonical_name), *(normalize_key(v) for v in aliases or [])}
        input_ids = {
            (kind, source_external_id(kind, value))
            for value in urls or [] if (kind := classify_url(value))
        }
        profile = profile or {}
        output = []
        for person in self.list_people():
            known_ids = {(item["kind"], item["external_id"]) for item in person["sources"]}
            known_names = {person["normalized_name"], *(normalize_key(v) for v in person["aliases"])}
            anchor_matches = len(input_ids & known_ids)
            name_match = bool(input_names & known_names)
            score, reasons = 0.0, []
            if anchor_matches:
                score += 70
                reasons.append("必要主页稳定 ID 一致")
            if name_match:
                score += 25
                reasons.append("姓名或别名一致")
            for field_name, label in (("school", "学校"), ("stage", "阶段")):
                if profile.get(field_name) and normalize_key(str(profile[field_name])) == normalize_key(str(person["profile"].get(field_name, ""))):
                    score += 8
                    reasons.append(f"{label}一致")
            left = set(re.findall(r"\w+", normalize_key(str(profile.get("research_focus", "")))))
            right = set(re.findall(r"\w+", normalize_key(str(person["profile"].get("research_focus", "")))))
            if left and right:
                score += 12 * len(left & right) / len(left | right)
            decision = "auto_merge" if anchor_matches and name_match else ("review" if score >= 55 else "new")
            output.append({"person_key": person["person_key"], "score": round(score, 1), "decision": decision, "reasons": reasons})
        return sorted(output, key=lambda item: item["score"], reverse=True)


def dossier_records_from_text(
    text: str,
    format_name: Literal["icml", "apple"],
) -> list[dict[str, Any]]:
    """Normalize the two real dossier formats without importing the heavy graph."""
    from people_intel.importers import AppleScholarsMarkdownImporter, IcmlPeopleMarkdownImporter

    records = (
        IcmlPeopleMarkdownImporter._parse(text)
        if format_name == "icml"
        else AppleScholarsMarkdownImporter._parse_apple(text)
    )
    output = []
    for record in records:
        urls = [url for url in record["urls"] if classify_url(url) in SOURCE_KINDS]
        content = normalize_text(record["content"])
        school_match = re.search(
            r"((?:[\w .'-]+(?:University|Institute|College|Lab))|(?:[\u4e00-\u9fff]{2,20}(?:大学|学院|实验室|研究院)))",
            content,
            re.I,
        )
        stage_match = re.search(
            r"(Ph\.?D\.? student|doctoral student|research scientist|research fellow|assistant professor|在读博士生|博士生|研究员|助理教授)",
            content,
            re.I,
        )
        focus_match = re.search(r"(?:研究方向|研究聚焦|research (?:focus(?:es)?|interests?)(?: on| include)?)[：:\s]*([^。.;]{5,140})", content, re.I)
        output.append({
            "canonical_name": record["canonical_name"],
            "aliases": record.get("aliases", []),
            "urls": urls,
            "profile": {
                "school": school_match.group(1).strip() if school_match else None,
                "stage": stage_match.group(1).strip() if stage_match else None,
                "research_focus": focus_match.group(1).strip() if focus_match else None,
                "cohort": "ICML 2026" if format_name == "icml" else f"Apple Scholars {record.get('year', '')}".strip(),
            },
        })
    return output


def dossier_records(path: str | Path, format_name: Literal["icml", "apple"]) -> list[dict[str, Any]]:
    """Read and normalize one of the two real dossier formats."""
    return dossier_records_from_text(Path(path).read_text(encoding="utf-8"), format_name)


__all__ = [
    "ChangeDecision", "DeepSeekDiffReviewer", "FeatureSnapshot", "FetchObservation",
    "IdentityAssessment", "LightTracker", "assess_identity_binding",
    "assess_snapshot", "canonical_url", "classify_url", "compare_snapshots",
    "dossier_records", "dossier_records_from_text", "extract_snapshot",
    "health_status", "source_external_id",
]
