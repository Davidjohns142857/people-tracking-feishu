from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal

from people_intel.light_tracker import (
    _identity_name_forms,
    canonical_url,
    normalize_key,
    normalize_text,
)


DiscoveryKind = Literal["homepage", "scholar", "github", "linkedin"]
DiscoveryRound = Literal["general", "scholar_github", "people"]


EXCLUDED_HOME_DOMAINS = {
    "aclanthology.org",
    "arxiv.org",
    "arxiv.gg",
    "awesomepapers.io",
    "biorxiv.org",
    "cphof.org",
    "csauthors.net",
    "dblp.org",
    "doi.org",
    "huggingface.co",
    "lanfanshu.com",
    "mlanthology.org",
    "openreview.net",
    "orcid.org",
    "paperswithcode.com",
    "rankless.org",
    "researchgate.net",
    "semanticscholar.org",
    "thecvf.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "weibo.com",
    "zhihu.com",
}
GENERIC_PATH_PARTS = {
    "about",
    "academic",
    "author",
    "faculty",
    "home",
    "index",
    "member",
    "people",
    "person",
    "profile",
    "research",
    "staff",
    "team",
}
NON_PROFILE_PATH_PARTS = {
    "abs",
    "article",
    "conference",
    "event",
    "news",
    "paper",
    "papers",
    "poster",
    "publication",
    "publications",
}
PERSONAL_HOMEPAGE_HOSTS = {
    "about.me",
    "carrd.co",
    "sites.google.com",
}
CONTEXT_STOPWORDS = {
    "about",
    "and",
    "assistant",
    "author",
    "authors",
    "awards",
    "candidate",
    "com",
    "computer",
    "conference",
    "cvpr",
    "current",
    "doctor",
    "doctoral",
    "edu",
    "email",
    "engineer",
    "faculty",
    "first",
    "for",
    "from",
    "homepage",
    "iccv",
    "iclr",
    "icml",
    "into",
    "joined",
    "member",
    "paper",
    "papers",
    "phd",
    "present",
    "profile",
    "professor",
    "research",
    "researcher",
    "scientist",
    "senior",
    "student",
    "that",
    "the",
    "this",
    "university",
    "using",
    "via",
    "with",
    "years",
    "作者",
    "候选",
    "博士",
    "博士生",
    "大学",
    "教授",
    "研究",
    "研究员",
    "第一作者",
    "论文",
}
GATE_CONTEXT_NOISE = CONTEXT_STOPWORDS | {
    "acm",
    "academy",
    "algorithm",
    "api",
    "architect",
    "arxiv",
    "arxiv.org",
    "ath",
    "available",
    "box",
    "code",
    "data",
    "design",
    "designer",
    "deepseek",
    "download",
    "generation",
    "github",
    "github.com",
    "google",
    "group",
    "http",
    "https",
    "image",
    "info",
    "lab",
    "learning",
    "level",
    "llm",
    "manager",
    "model",
    "net",
    "off",
    "oral",
    "org",
    "out",
    "principal",
    "scholar",
    "serving",
    "source",
    "staff",
    "team",
    "tech",
    "technical",
    "thinking",
    "training",
    "vision",
    "www",
}
COMMON_COMPACT_NAMES = {
    "feiyang",
    "liwei",
    "wangyu",
    "yangfei",
    "yuwang",
    "weili",
    "weizhang",
    "zhangwei",
}


@dataclass
class DiscoverySubject:
    registry_person_id: str
    canonical_name: str
    name_zh: str | None = None
    name_en: str | None = None
    aliases: list[str] = field(default_factory=list)
    affiliations: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    research_focuses: list[str] = field(default_factory=list)
    cohorts: list[str] = field(default_factory=list)
    source_documents: list[str] = field(default_factory=list)
    supporting_urls: list[str] = field(default_factory=list)
    record_excerpt: str = ""

    @property
    def names(self) -> list[str]:
        return list(
            dict.fromkeys(
                normalize_text(value)
                for value in (
                    self.canonical_name,
                    self.name_zh or "",
                    self.name_en or "",
                    *self.aliases,
                )
                if normalize_text(value)
            )
        )

    @property
    def context_terms(self) -> list[str]:
        values = [
            *self.affiliations,
            *self.roles,
            *self.research_focuses,
            *self.cohorts,
        ]
        return list(
            dict.fromkeys(
                normalize_text(value)[:240]
                for value in values
                if normalize_text(value)
            )
        )


@dataclass
class ExaResult:
    title: str
    url: str
    text: str
    published_at: str | None = None
    author: str | None = None
    rank: int = 0


@dataclass
class ExaSearchResponse:
    query: str
    results: list[ExaResult]
    request_id: str | None = None
    cost_dollars: float = 0.0
    search_time_ms: float | None = None
    error: str | None = None


@dataclass
class DiscoveryCandidate:
    registry_person_id: str
    canonical_name: str
    kind: DiscoveryKind
    url: str
    title: str
    score: float
    decision: Literal["auto_accept", "review", "reject"]
    reasons: list[str]
    matched_name_forms: list[str]
    matched_context_terms: list[str]
    source_rank: int
    discovery_round: DiscoveryRound
    evidence_text: str
    supporting_pivot: bool = False
    github_account_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.casefold().split(":", 1)[0].removeprefix(
        "www."
    )


def _parts(url: str) -> list[str]:
    return [
        urllib.parse.unquote(value)
        for value in urllib.parse.urlparse(url).path.split("/")
        if value
    ]


def classify_profile_candidate(url: str) -> DiscoveryKind | None:
    try:
        normalized = canonical_url(url)
    except ValueError:
        return None
    parsed = urllib.parse.urlparse(normalized)
    host = _host(normalized)
    parts = _parts(normalized)
    if "." not in host:
        return None
    if host == "github.com":
        if len(parts) != 1 or parts[0].casefold() in {
            "features",
            "marketplace",
            "orgs",
            "organizations",
            "search",
            "topics",
        }:
            return None
        return "github"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin" if len(parts) == 2 and parts[0].casefold() == "in" else None
    if host.startswith("scholar.google.") or host == "scholar.google.com":
        query = urllib.parse.parse_qs(parsed.query)
        return "scholar" if parts[:1] == ["citations"] and query.get("user") else None
    if (
        not host
        or host in EXCLUDED_HOME_DOMAINS
        or any(host.endswith(f".{domain}") for domain in EXCLUDED_HOME_DOMAINS)
        or parsed.path.casefold().endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx"))
    ):
        return None
    if any(part.casefold() in NON_PROFILE_PATH_PARTS for part in parts):
        return None
    # user.github.io/project is normally a project site, not the user's stable
    # personal homepage.  Root GitHub Pages remains eligible.
    if host.endswith(".github.io") and parts:
        return None
    return "homepage"


def _canonical_profile_url(url: str) -> str:
    normalized = canonical_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    host = _host(normalized)
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return urllib.parse.urlunsplit(
            ("https", "linkedin.com", parsed.path, parsed.query, "")
        )
    if host.startswith("scholar.google."):
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if query.get("user"):
            return urllib.parse.urlunsplit(
                (
                    "https",
                    "scholar.google.com",
                    "/citations",
                    urllib.parse.urlencode({"user": query["user"]}),
                    "",
                )
            )
    return normalized


def _links(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>()\]\"']+", text)


def extract_roster_urls(text: str) -> list[str]:
    """Extract explicit web contacts from lightly escaped Markdown roster prose."""

    cleaned = (
        normalize_text(text)
        .replace("\\.", ".")
        .replace("\\-", "-")
        .replace("\\_", "_")
    )
    cleaned = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        " ",
        cleaned,
    )
    raw_values = [
        *_links(cleaned),
        *re.findall(
            r"(?<![@\w])(?:[A-Za-z0-9-]+\.)+"
            r"(?:com|org|net|edu|cn|io|ai|me)"
            r"(?:/[^\s<>()\]|，。；;]*)?",
            cleaned,
            flags=re.IGNORECASE,
        ),
    ]
    output: list[str] = []
    for raw in raw_values:
        raw = raw.rstrip(".,;:，。；")
        if not raw:
            continue
        if not re.match(r"https?://", raw, flags=re.IGNORECASE):
            raw = f"https://{raw}"
        try:
            normalized = _canonical_profile_url(raw)
        except ValueError:
            continue
        if "." not in _host(normalized):
            continue
        output.append(normalized)
    return list(dict.fromkeys(output))


def _candidate_urls(result: ExaResult) -> list[tuple[str, bool]]:
    values: list[tuple[str, bool]] = [(result.url, False)]
    for url in _links(result.text):
        if classify_profile_candidate(url):
            values.append((url.rstrip(".,;:"), False))
    output: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for url, pivot in values:
        try:
            normalized = _canonical_profile_url(url)
        except ValueError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append((normalized, pivot))
    return output


def _supporting_pivots(subject: DiscoverySubject) -> list[ExaResult]:
    results: list[ExaResult] = []
    for raw in subject.supporting_urls:
        try:
            normalized = _canonical_profile_url(raw)
        except ValueError:
            continue
        host = _host(normalized)
        parts = _parts(normalized)
        if host == "github.com" and len(parts) >= 2:
            account = canonical_url(f"https://github.com/{parts[0]}")
            results.append(
                ExaResult(
                    title=f"GitHub account derived from roster supporting URL {parts[0]}",
                    url=account,
                    text=f"Supporting repository: {normalized}\n{subject.record_excerpt}",
                    rank=50,
                )
            )
        elif classify_profile_candidate(normalized):
            results.append(
                ExaResult(
                    title="Roster supporting URL",
                    url=normalized,
                    text=subject.record_excerpt,
                    rank=50,
                )
            )
    return results


def _significant_tokens(values: Iterable[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        lowered = normalize_text(value).casefold()
        for token in re.findall(r"[a-z][a-z0-9+.-]{2,}|[\u4e00-\u9fff]{2,}", lowered):
            if token in CONTEXT_STOPWORDS:
                continue
            tokens.append(token)
    return list(dict.fromkeys(tokens))


def _homepage_trust(url: str) -> Literal[
    "official_institution", "personal_host", "custom_domain"
]:
    host = _host(url)
    if (
        host.endswith(".edu")
        or ".edu." in host
        or ".ac." in host
        or host.endswith(".ac")
        or host.endswith(".cas.cn")
        or host.endswith(".gov.cn")
        or host.endswith(".org.cn")
    ):
        return "official_institution"
    if (
        host in PERSONAL_HOMEPAGE_HOSTS
        or host.endswith(".github.io")
        or host.endswith(".notion.site")
    ):
        return "personal_host"
    return "custom_domain"


def _url_name_match(forms: set[str], url: str) -> bool:
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", urllib.parse.unquote(url).casefold())
    return any(
        len(form) >= 5
        and re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", form.casefold()) in compact
        for form in forms
    )


def _context_token_matches(token: str, blob: str) -> bool:
    normalized = normalize_text(token).casefold()
    if not normalized:
        return False
    if re.search(r"[\u4e00-\u9fff]", normalized):
        return normalized in blob
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
            blob,
        )
    )


def _subject_context_matches(
    subject: DiscoverySubject,
    blob: str,
) -> list[str]:
    forms = _identity_name_forms(
        subject.names[0] if subject.names else subject.canonical_name,
        subject.names[1:],
    )
    name_tokens = {
        token
        for form in forms
        for token in re.findall(
            r"[a-z][a-z0-9+.-]{2,}|[\u4e00-\u9fff]{2,}",
            form.casefold(),
        )
    }
    context_tokens = [
        token
        for token in _significant_tokens(
            [*subject.context_terms, subject.record_excerpt]
        )
        if token.casefold() not in name_tokens
    ]
    return [
        token
        for token in context_tokens
        if _context_token_matches(token, blob)
    ]


def _identity_zone_matches(
    subject: DiscoverySubject,
    candidate: DiscoveryCandidate,
) -> bool:
    forms = _identity_name_forms(
        subject.names[0] if subject.names else subject.canonical_name,
        subject.names[1:],
    )
    zone = normalize_text(f"{candidate.title}\n{candidate.url}").casefold()
    compact_zone = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", zone)
    return any(
        len(form) >= 3
        and (
            form.casefold() in zone
            or (
                len(re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", form.casefold()))
                >= 5
                and re.sub(
                    r"[^a-z0-9\u4e00-\u9fff]",
                    "",
                    form.casefold(),
                )
                in compact_zone
            )
        )
        for form in forms
    )


def _homepage_profile_route(url: str) -> bool:
    trust = _homepage_trust(url)
    if trust == "personal_host":
        return True
    if trust != "official_institution":
        return False
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path).casefold()
    parts = {part for part in re.split(r"[/_.-]+", path) if part}
    return (
        "~" in path
        or bool(
            parts
            & {
                "faculty",
                "member",
                "members",
                "ourteam",
                "people",
                "person",
                "profile",
                "profiles",
                "researchmembers",
                "staff",
                "team",
                "user",
                "users",
            }
        )
        or "/our-team/" in path
        or "/research-members/" in path
    )


def _informative_context_terms(candidate: DiscoveryCandidate) -> list[str]:
    identity_zone = normalize_text(f"{candidate.title}\n{candidate.url}").casefold()
    compact_zone = re.sub(
        r"[^a-z0-9\u4e00-\u9fff]",
        "",
        identity_zone,
    )
    return [
        term
        for term in candidate.matched_context_terms
        if normalize_text(term).casefold() not in GATE_CONTEXT_NOISE
        and len(normalize_text(term)) >= 3
        and normalize_text(term).casefold() not in identity_zone
        and re.sub(
            r"[^a-z0-9\u4e00-\u9fff]",
            "",
            normalize_text(term).casefold(),
        )
        not in compact_zone
    ]


def _direct_roster_edge(
    subject: DiscoverySubject,
    candidate: DiscoveryCandidate,
) -> bool:
    try:
        candidate_url = _canonical_profile_url(candidate.url)
    except ValueError:
        return False
    for raw in subject.supporting_urls:
        try:
            source_url = _canonical_profile_url(raw)
        except ValueError:
            continue
        if candidate_url == source_url:
            return True
        if (
            candidate.kind == "github"
            and _host(candidate_url) == "github.com"
            and source_url.startswith(candidate_url.rstrip("/") + "/")
        ):
            return True
    return False


def enforce_admission_gates(
    subject: DiscoverySubject,
    candidates: list[DiscoveryCandidate],
) -> list[DiscoveryCandidate]:
    """Apply identity-zone and profile-shape gates after relevance scoring.

    Search relevance can show that a page mentions the right person, but it does
    not prove the page is that person's profile.  This second gate deliberately
    lowers recall to prevent author lists, project pages and institutional news
    articles from becoming tracking anchors.
    """

    for candidate in candidates:
        if candidate.decision != "auto_accept":
            continue
        candidate.matched_context_terms = _subject_context_matches(
            subject,
            normalize_text(
                f"{candidate.title}\n{candidate.evidence_text}\n{candidate.url}"
            ).casefold(),
        )[:12]
        direct_roster_edge = _direct_roster_edge(subject, candidate)
        if direct_roster_edge and candidate.kind == "github":
            reason = "原表 GitHub 资源直接边保留为独立身份依据"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
            continue
        if not _identity_zone_matches(subject, candidate):
            candidate.decision = "review"
            reason = "姓名仅在正文命中，未出现在标题或固定 profile slug"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
            continue
        if direct_roster_edge and (
            candidate.kind != "homepage" or _homepage_profile_route(candidate.url)
        ):
            reason = "原表显式联系方式与候选固定 URL 一致"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
            continue
        informative = _informative_context_terms(candidate)
        if len(informative) < 2:
            candidate.decision = "review"
            reason = "独立的非通用表格上下文证据不足两项"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
            continue
        if candidate.kind == "homepage" and not _homepage_profile_route(candidate.url):
            candidate.decision = "review"
            reason = "机构页面不是人员目录/个人主页路径，可能只是新闻或项目页"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
    return candidates


def _score_one(
    subject: DiscoverySubject,
    result: ExaResult,
    url: str,
    *,
    discovery_round: DiscoveryRound,
    supporting_pivot: bool,
) -> DiscoveryCandidate | None:
    kind = classify_profile_candidate(url)
    if kind is None:
        return None
    forms = _identity_name_forms(
        subject.names[0] if subject.names else subject.canonical_name,
        subject.names[1:],
    )
    blob = normalize_text(f"{result.title}\n{result.text}\n{url}").casefold()
    compact_blob = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", blob)
    matched_forms = sorted(
        {
            form
            for form in forms
            if len(form) >= 3
            and (
                form.casefold() in blob
                or re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", form.casefold())
                in compact_blob
            )
        },
        key=lambda value: (-len(value), value),
    )
    # The normalized profile fields are preferred, but the original roster row is
    # also evidence: many source tables keep affiliation/project clues only in the
    # prose excerpt.  These tokens are used solely for cross-checking the returned
    # page; they never create a candidate URL by themselves.
    matched_context = _subject_context_matches(subject, blob)
    url_name = _url_name_match(forms, url)
    homepage_trust = _homepage_trust(url) if kind == "homepage" else None

    score = {
        "homepage": 0.20,
        "scholar": 0.28,
        "github": 0.25,
        "linkedin": 0.27,
    }[kind]
    reasons = [f"候选类型={kind}"]
    if matched_forms:
        score += 0.38
        reasons.append(f"姓名命中={matched_forms[0]}")
    if url_name:
        score += 0.12
        reasons.append("URL包含姓名形式")
    if matched_context:
        score += min(0.24, 0.08 * len(matched_context))
        reasons.append("上下文命中=" + ",".join(matched_context[:4]))
    if supporting_pivot:
        score += 0.07
        reasons.append("由原表 supporting URL 推导")
    if result.rank > 0:
        score += max(0.0, 0.08 - min(result.rank, 20) * 0.004)
    if homepage_trust == "official_institution":
        score += 0.05
        reasons.append("机构官方域名")
    elif homepage_trust == "personal_host":
        score += 0.04
        reasons.append("受认可的个人主页托管域名")
    elif homepage_trust == "custom_domain":
        reasons.append("自定义域名需更强身份交叉证据")

    # Search cards that never mention the person are discovery hints only.
    if not matched_forms:
        score -= 0.25
        reasons.append("未在候选证据中命中姓名")
    if kind == "homepage" and not matched_context and not url_name:
        score -= 0.10
    score = round(max(0.0, min(1.0, score)), 4)

    strong_context = len(matched_context) >= 1
    if kind == "github" and supporting_pivot:
        # A repository URL attached to this exact roster row is an independent
        # person→account edge.  GitHub display names are deliberately not required.
        auto = True
        reasons.append("原表已把该 GitHub 资源绑定到此人，允许昵称不同")
    elif kind in {"scholar", "github", "linkedin"}:
        # Name/slug is one signal, not two.  A platform profile needs at least one
        # independent roster-context match before automatic admission.
        auto = bool(matched_forms and strong_context and score >= 0.72)
    else:
        trusted = homepage_trust in {"official_institution", "personal_host"}
        if trusted:
            auto = bool(matched_forms and strong_context and score >= 0.72)
        else:
            # An arbitrary custom domain cannot prove that it is the person's own
            # homepage.  Keep it visible for review until another confirmed source
            # links back to it.
            auto = False
            reasons.append("自定义域名等待已确认来源回链，不自动入库")
    compact_name = re.sub(
        r"[^a-z0-9\u4e00-\u9fff]",
        "",
        normalize_key(subject.canonical_name),
    )
    if compact_name in COMMON_COMPACT_NAMES and len(matched_context) < 2:
        auto = False
        reasons.append("高重名姓名需要至少两个上下文证据")
    decision: Literal["auto_accept", "review", "reject"] = (
        "auto_accept" if auto else "review" if score >= 0.35 else "reject"
    )
    return DiscoveryCandidate(
        registry_person_id=subject.registry_person_id,
        canonical_name=subject.canonical_name,
        kind=kind,
        url=url,
        title=result.title,
        score=score,
        decision=decision,
        reasons=reasons,
        matched_name_forms=matched_forms[:12],
        matched_context_terms=matched_context[:12],
        source_rank=result.rank,
        discovery_round=discovery_round,
        evidence_text=normalize_text(result.text)[:1800],
        supporting_pivot=supporting_pivot,
    )


def rank_profile_candidates(
    subject: DiscoverySubject,
    results: list[ExaResult],
    *,
    discovery_round: DiscoveryRound,
) -> list[DiscoveryCandidate]:
    candidates: dict[tuple[str, str], DiscoveryCandidate] = {}
    all_results = [*results, *_supporting_pivots(subject)]
    pivot_urls = {item.url for item in _supporting_pivots(subject)}
    for rank, result in enumerate(all_results, 1):
        if result.rank <= 0:
            result.rank = rank
        for url, _ in _candidate_urls(result):
            candidate = _score_one(
                subject,
                result,
                url,
                discovery_round=discovery_round,
                supporting_pivot=url in pivot_urls,
            )
            if candidate is None:
                continue
            key = (candidate.kind, candidate.url)
            existing = candidates.get(key)
            if existing is None or candidate.score > existing.score:
                candidates[key] = candidate

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item.decision != "auto_accept",
            -item.score,
            item.kind,
            item.url,
        ),
    )
    # Only the strongest candidate of each type may auto-admit. Near ties are
    # deliberately held for review.
    by_kind: dict[str, list[DiscoveryCandidate]] = {}
    for item in ordered:
        by_kind.setdefault(item.kind, []).append(item)
    for values in by_kind.values():
        accepted = [item for item in values if item.decision == "auto_accept"]
        if len(accepted) > 1 and accepted[0].score - accepted[1].score < 0.08:
            for item in accepted:
                item.decision = "review"
                item.reasons.append("同类型高分候选接近，避免自动错绑")
        else:
            for item in accepted[1:]:
                item.decision = "review"
                item.reasons.append("同类型只自动接纳最高分候选")
    return enforce_admission_gates(subject, ordered)


def select_admission_candidate(
    candidates: list[DiscoveryCandidate],
) -> DiscoveryCandidate | None:
    accepted = [item for item in candidates if item.decision == "auto_accept"]
    if not accepted:
        return None
    priority = {"scholar": 0, "github": 1, "linkedin": 2, "homepage": 3}
    return max(accepted, key=lambda item: (item.score, -priority[item.kind]))


def github_account_type(
    url: str,
    *,
    fetcher: Any | None = None,
    timeout_seconds: int = 12,
) -> tuple[str | None, str | None]:
    parts = _parts(url)
    if _host(url) != "github.com" or len(parts) != 1:
        return None, "not a fixed GitHub account URL"
    login = parts[0]
    try:
        if fetcher is not None:
            payload = fetcher(login)
        else:
            request = urllib.request.Request(
                f"https://api.github.com/users/{urllib.parse.quote(login)}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "people-intel-profile-discovery/1",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:500]
    if not isinstance(payload, dict):
        return None, "GitHub account probe returned a non-object"
    account_type = str(payload.get("type") or "").strip()
    return (account_type or None), None


def validate_selected_github_candidate(
    candidates: list[DiscoveryCandidate],
    *,
    fetcher: Any | None = None,
) -> DiscoveryCandidate | None:
    """Fail closed if the selected GitHub anchor is not a personal User."""

    checked: set[str] = set()
    while True:
        selected = select_admission_candidate(candidates)
        if selected is None or selected.kind != "github":
            return selected
        if selected.url in checked:
            return None
        checked.add(selected.url)
        account_type, error = github_account_type(
            selected.url,
            fetcher=fetcher,
        )
        selected.github_account_type = account_type
        if account_type == "User":
            reason = "GitHub REST 确认为个人 User 账号"
            if reason not in selected.reasons:
                selected.reasons.append(reason)
            return selected
        selected.decision = "review"
        if account_type:
            selected.reasons.append(
                f"GitHub REST type={account_type}，不是个人 User 账号"
            )
        else:
            selected.reasons.append(
                f"GitHub 账号类型探测失败，暂不自动入库：{error}"
            )


class ExaProfileDiscoveryClient:
    def __init__(
        self,
        *,
        executable: str = "mcporter",
        timeout_seconds: int = 45,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def build_query(
        subject: DiscoverySubject,
        discovery_round: DiscoveryRound,
    ) -> str:
        names = " | ".join(subject.names[:4])
        structured_context = " ; ".join(subject.context_terms[:6])
        roster_context = normalize_text(subject.record_excerpt)[:900]
        context = " ; ".join(
            value for value in (structured_context, roster_context) if value
        )
        if discovery_round == "scholar_github":
            target = "Google Scholar profile or GitHub user profile"
        elif discovery_round == "people":
            target = "LinkedIn professional profile or official staff profile"
        else:
            target = "official personal homepage, GitHub, Google Scholar, or LinkedIn profile"
        return (
            f"Find the {target} belonging to {names}. "
            f"Use this table context to disambiguate: {context}. "
            "Return the person's own profile, not papers, repositories, news, or a namesake."
        )

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> tuple[list[ExaResult], dict[str, Any]]:
        if not isinstance(payload.get("results"), list):
            blocks = payload.get("content")
            text = "\n".join(
                str(block.get("text") or "")
                for block in blocks or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
            try:
                nested = json.loads(text)
            except json.JSONDecodeError:
                return [], payload
            if isinstance(nested, dict):
                return ExaProfileDiscoveryClient._parse_payload(nested)
            return [], payload
        results: list[ExaResult] = []
        for rank, item in enumerate(payload["results"], 1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("id") or "").strip()
            if not url:
                continue
            text = str(item.get("text") or "")
            if not text:
                highlights = item.get("highlights")
                if isinstance(highlights, list):
                    text = "\n...\n".join(str(value) for value in highlights)
            results.append(
                ExaResult(
                    title=str(item.get("title") or ""),
                    url=url,
                    text=text,
                    published_at=item.get("publishedDate"),
                    author=item.get("author"),
                    rank=rank,
                )
            )
        return results, payload

    def search(
        self,
        subject: DiscoverySubject,
        *,
        discovery_round: DiscoveryRound = "general",
        num_results: int = 12,
    ) -> ExaSearchResponse:
        query = self.build_query(subject, discovery_round)
        args: dict[str, Any] = {
            "query": query,
            "numResults": min(8, num_results),
            "type": "auto",
            "enableHighlights": True,
            "highlightsMaxCharacters": 1200,
            "textMaxCharacters": 2400,
        }
        if discovery_round == "people":
            args["category"] = "people"
        invocation = [
            self.executable,
            "call",
            "exa.web_search_advanced_exa",
            "--args",
            json.dumps(args, ensure_ascii=False),
            "--output",
            "json",
            "--timeout",
            "30000",
        ]
        try:
            completed = subprocess.run(
                invocation,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ExaSearchResponse(query=query, results=[], error=str(exc))
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"mcporter exit {completed.returncode}"
            return ExaSearchResponse(query=query, results=[], error=detail[:1200])
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            return ExaSearchResponse(
                query=query,
                results=[],
                error=f"invalid Exa response: {exc}",
            )
        if not isinstance(payload, dict):
            return ExaSearchResponse(
                query=query,
                results=[],
                error="invalid Exa response: expected object",
            )
        results, metadata = self._parse_payload(payload)
        cost = metadata.get("costDollars")
        if isinstance(cost, dict):
            cost = cost.get("total")
        return ExaSearchResponse(
            query=query,
            results=results,
            request_id=metadata.get("requestId"),
            cost_dollars=float(cost or 0),
            search_time_ms=(
                float(metadata["searchTime"])
                if metadata.get("searchTime") is not None
                else None
            ),
        )


__all__ = [
    "DiscoveryCandidate",
    "DiscoveryRound",
    "DiscoverySubject",
    "ExaProfileDiscoveryClient",
    "ExaResult",
    "ExaSearchResponse",
    "classify_profile_candidate",
    "enforce_admission_gates",
    "extract_roster_urls",
    "github_account_type",
    "rank_profile_candidates",
    "select_admission_candidate",
    "validate_selected_github_candidate",
]
