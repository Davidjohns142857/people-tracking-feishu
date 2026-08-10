from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from people_intel.light_tracker import (
    SOURCE_KINDS,
    canonical_url,
    classify_url,
    normalize_key,
    normalize_text,
    source_external_id,
)


PARSER_VERSION = "mono-markdown-registry-v1"


@dataclass(frozen=True)
class DocumentSpec:
    filename: str
    cohort: str
    person_levels: tuple[int, ...]
    parser_kind: str = "heading"


DOCUMENT_SPECS = {
    spec.filename: spec
    for spec in (
        DocumentSpec("2021-2025 字节奖学金.md", "字节奖学金 2021-2025", (3,)),
        DocumentSpec("2026 WAIC 云帆奖得主揭晓.md", "2026 WAIC 云帆奖", (3,)),
        DocumentSpec("CVPR 2026 Best Paper华人researcher.md", "CVPR 2026 Awards", (2,)),
        DocumentSpec("DSpark 论文作者 Mapping.md", "DSpark Authors", (2,)),
        DocumentSpec("DataFlow-Harness 论文作者信息整理.md", "DataFlow-Harness Authors", (2,)),
        DocumentSpec("DeepSeek 论文及核心人员.md", "DeepSeek Core", (3, 4), "deepseek"),
        DocumentSpec("Deepmind华人researcher.md", "Google DeepMind 华人研究者", (3,)),
        DocumentSpec("Happy Horse-ATH 未来生活实验室成员.md", "Happy Horse-ATH", (3,)),
        DocumentSpec("Harness VLA清华团队人员信息整理.md", "Harness VLA 清华团队", (2,)),
        DocumentSpec("ICML 2026 获奖论文华人_华语背景贡献者.md", "ICML 2026", (3,)),
        DocumentSpec("JoyAI-VL-Interaction 核心贡献者信息整理.md", "JoyAI-VL-Interaction", (3,)),
        DocumentSpec("MIT·TR35 岁（2025 年度）中国区入选者名单.md", "MIT TR35 China 2025", (2,)),
        DocumentSpec("Seedance 相关人员.md", "Seedance", (4,)),
        DocumentSpec("可灵相关人员（持续mapping中）.md", "可灵", (3,)),
        DocumentSpec("清华大学 InspiringGroup 团队成员档案.md", "清华 InspiringGroup", (3,)),
        DocumentSpec("清华特奖名单 2020-2025 .md", "清华特奖 2020-2025", (4,), "tsinghua_award"),
        DocumentSpec(
            "红杉x麻省理工科技 中国 2025「AI25：25岁以下AI创新青年先锋」入选者信息.md",
            "中国 AI25 2025",
            (2,),
        ),
        DocumentSpec("腾讯青云奖 2025.md", "腾讯青云奖 2025", (3,)),
        DocumentSpec(
            "苹果 AI_ML 博士生学者（Apple Scholars in AI_ML PhD Fellowship）信息整理.md",
            "Apple Scholars 2020-2026",
            (2,),
        ),
        DocumentSpec("谢赛宁Saining Xie 团队成员.md", "Saining Xie Team", (4,)),
    )
}


SECTION_TITLES = {
    "人员",
    "论文",
    "华人",
    "非华人",
    "在职",
    "离职",
    "新增补充",
    "璀璨明星",
    "明日之星",
    "本科生特奖",
    "研究生特奖",
    "候选人未入围",
    "一、创始人",
    "二、博士生",
    "三、外部合作者",
}

NON_PERSON_TERMS = {
    "大学",
    "学院",
    "系",
    "书院",
    "研究院",
    "实验室",
    "研究所",
    "研究生",
    "博士",
    "硕士",
    "本科",
    "候选人",
    "未入围",
    "团队",
    "项目",
    "成员",
    "角色",
    "背景",
    "学术",
    "资金",
    "系列",
    "特奖",
    "论文",
    "作者",
    "贡献者",
    "得主",
    "获奖",
    "提名",
    "方向",
    "年度",
    "更新",
    "核心",
    "附录",
    "在职",
    "离职",
}

STATUS_TERMS = {
    "已离职": "left",
    "离职": "left",
    "在职": "active",
    "传闻去向": "uncertain",
    "待确认": "uncertain",
    "已联系": "contacted",
    "未入围": "not_selected",
}

SUPPORT_HOST_KINDS = {
    "orcid.org": "orcid",
    "dblp.org": "dblp",
    "dblp.uni-trier.de": "dblp",
    "openreview.net": "openreview",
    "arxiv.org": "paper",
    "semanticscholar.org": "paper",
    "aclanthology.org": "bibliography",
    "dl.acm.org": "bibliography",
    "aminer.org": "bibliography",
    "pubs.acs.org": "paper",
    "researchgate.net": "research_profile",
    "docs.google.com": "document",
    "competition.adesignaward.com": "award_profile",
    "conference.apnic.net": "nomination_profile",
    "twitter.com": "x",
    "x.com": "x",
}

BLOCKED_HOMEPAGE_HOSTS = {
    "mp.weixin.qq.com",
    "zhihu.com",
    "baidu.com",
    "baike.baidu.com",
    "wikipedia.org",
    "en.wikipedia.org",
    "internal-api-drive-stream.feishu.cn",
    "monolith.feishu.cn",
    "feishu.cn",
    "icml.cc",
    "cvpr.thecvf.com",
    "thecvf.com",
    "youtube.com",
    "bilibili.com",
    "xiaohongshu.com",
    "conf.researchr.org",
}

KNOWN_ORGANIZATION_GITHUBS = {
    "csuastt",
    "deepseek-ai",
    "eesast-software-design-competition",
    "leaplabthu",
    "opendcai",
    "roboparty",
}

KNOWN_PROJECT_OR_GROUP_HOMEPAGES = {
    "ai45lab.github.io",
    "benlogroup.wordpress.com",
    "dai.sjtu.edu.cn",
    "inspiringgroup.github.io",
    "leaplab.ai",
    "lumina-embodied.ai",
    "murgelab.cs.unc.edu",
    "next-gpt.github.io",
    "nicsefc.ee.tsinghua.edu.cn",
    "odesign1.github.io",
    "poloclub.github.io",
    "recursive.com",
    "scalelab-sjtu.github.io",
    "teai.fudan.edu.cn",
    "xiaomimimo.com",
    "zheng-yan-group.github.io",
    "zju3dv.github.io",
}

KNOWN_ORGANIZATION_HUGGINGFACE_PROFILES = {
    "languagebind",
}

MANUAL_SAME_PERSON_NAME_PAIRS = {
    frozenset(("吴鹏皓", "吴鹏浩")),
    frozenset(("游凯超", "kaichao you")),
    frozenset(("xindi cindy wu", "xindi wu")),
    frozenset(("wenhao chai", "柴文浩")),
    frozenset(("朱琪豪", "朱其豪")),
    frozenset(("袁境阳", "jingyang yuan")),
}

MANUAL_RECORD_DECISIONS = {
    frozenset(
        (
            ("2026 WAIC 云帆奖得主揭晓.md", "黄一"),
            (
                "红杉x麻省理工科技 中国 2025「AI25：25岁以下AI创新青年先锋」入选者信息.md",
                "黄一",
            ),
        )
    ): "auto_merge",
    frozenset(
        (
            ("MIT·TR35 岁（2025 年度）中国区入选者名单.md", "张迪"),
            ("可灵相关人员（持续mapping中）.md", "张迪"),
        )
    ): "distinct",
    frozenset(
        (
            (
                "苹果 AI_ML 博士生学者（Apple Scholars in AI_ML PhD Fellowship）信息整理.md",
                "Wentao Zhang",
            ),
            ("DSpark 论文作者 Mapping.md", "Wentao Zhang"),
        )
    ): "distinct",
    frozenset(
        (
            ("DataFlow-Harness 论文作者信息整理.md", "Wentao Zhang"),
            ("DSpark 论文作者 Mapping.md", "Wentao Zhang"),
        )
    ): "distinct",
}

EXCLUDED_IDENTITY_EMAILS = {
    ("JoyAI-VL-Interaction 核心贡献者信息整理.md", "Haoyang Huang"): {
        "haowenhou@outlook.com"
    }
}

PERSONAL_CONTEXT_RE = re.compile(
    r"个人主页|个人网站|个人页|主页链接|教师页|学校个人页|官方主页|"
    r"实验室个人页|学术主页|profile|home\s?page|faculty\s?page",
    re.I,
)
NON_PERSONAL_CONTEXT_RE = re.compile(
    r"项目主页|项目页面|项目页|论文|代码|开源|仓库|repo|报道|新闻|来源|参考|"
    r"团队主页|团队页面|实验室|课题组|研究组|公司主页|模型权重|社区",
    re.I,
)

ENGLISH_NAME_RE = re.compile(
    r"(?<![A-Za-z])"
    r"((?:Dr\.\s+)?(?:[A-Z][A-Za-z'’.-]*|[A-Z]\.)"
    r"(?:\s+(?:\([A-Z][A-Za-z'’.-]*\)|[\"“]?[A-Z][A-Za-z'’.-]*[\"”]?|[A-Z]\.)){1,4})"
)
URL_RE = re.compile(r"""https?://[^\s<>\]()（）\]），,、；;。"'*]+""")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")


@dataclass
class Heading:
    level: int
    title: str
    start: int
    content_start: int
    end: int = 0
    line: int = 0
    parent_titles: list[str] = field(default_factory=list)


@dataclass
class UrlEvidence:
    url: str
    canonical_url: str
    detected_kind: str | None
    support_kind: str
    tracking_eligible: bool
    context: str
    reason: str


@dataclass
class RawPersonRecord:
    record_id: str
    source_document: str
    source_sha256: str
    cohort: str
    heading: str
    heading_level: int
    heading_line: int
    section_path: list[str]
    canonical_name: str
    name_zh: str | None
    name_en: str | None
    aliases: list[str]
    content: str
    content_hash: str
    emails: list[str]
    urls: list[UrlEvidence]
    profile: dict[str, Any]
    extraction_confidence: float
    parser_version: str = PARSER_VERSION

    @property
    def trackable_urls(self) -> list[str]:
        return list(
            dict.fromkeys(item.canonical_url for item in self.urls if item.tracking_eligible)
        )

    @property
    def identity_keys(self) -> set[str]:
        excluded = {
            str(value).casefold()
            for value in self.profile.get("excluded_identity_emails", [])
        }
        keys = {
            f"email:{email.casefold()}"
            for email in self.emails
            if email.casefold() not in excluded
        }
        for item in self.urls:
            if item.tracking_eligible and item.detected_kind:
                keys.add(source_external_id(item.detected_kind, item.canonical_url))
            elif item.support_kind in {"orcid", "dblp", "openreview"}:
                keys.add(f"{item.support_kind}:{item.canonical_url.casefold()}")
        return keys

    def as_json(self) -> dict[str, Any]:
        result = asdict(self)
        result["trackable_urls"] = self.trackable_urls
        result["identity_keys"] = sorted(self.identity_keys)
        return result


@dataclass
class IdentityEdge:
    left_record_id: str
    right_record_id: str
    decision: str
    method: str
    score: float
    evidence: list[str]


@dataclass
class RegistryPerson:
    registry_person_id: str
    canonical_name: str
    name_zh: str | None
    name_en: str | None
    aliases: list[str]
    record_ids: list[str]
    cohorts: list[str]
    source_documents: list[str]
    trackable_urls: list[str]
    supporting_urls: list[str]
    profile: dict[str, Any]
    admission_status: str
    identity_confidence: str
    merge_methods: list[str]
    tracking_person_key: str | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def markdown_unescape(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value))
    value = re.sub(r"\\([\\`*_{}\[\]()#+.!~>|&-])", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_`]+", "", value)
    return normalize_text(value)


def parse_headings(text: str) -> list[Heading]:
    matches = list(re.finditer(r"^(#{1,6})[ \t]+(.+?)\s*$", text, re.M))
    headings: list[Heading] = []
    stack: list[Heading] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = markdown_unescape(match.group(2))
        while stack and stack[-1].level >= level:
            stack.pop()
        end = len(text)
        for candidate in matches[index + 1 :]:
            if len(candidate.group(1)) <= level:
                end = candidate.start()
                break
        heading = Heading(
            level=level,
            title=title,
            start=match.start(),
            content_start=match.end(),
            end=end,
            line=text.count("\n", 0, match.start()) + 1,
            parent_titles=[item.title for item in stack],
        )
        headings.append(heading)
        stack.append(heading)
    return headings


def _strip_heading_noise(value: str) -> str:
    value = markdown_unescape(value)
    value = re.sub(r"^\s*\d+\s*[.)、．]\s*", "", value)
    value = re.sub(r"\s*(?:--+|—)\s*(?:已离职|已联系|目前均在读|.+?大学)\s*$", "", value)
    value = re.sub(r"\s*-\s*已联系\s*$", "", value)
    value = re.sub(r"（传闻去向[^）]*）", "", value)
    value = re.sub(r"\([^)]*待确认[^)]*\)", "", value)
    return normalize_text(value)


def _chinese_name_candidates(value: str) -> list[str]:
    candidates: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fff]{2,12}", value):
        if not 2 <= len(token) <= 4:
            continue
        if any(term in token for term in NON_PERSON_TERMS):
            continue
        candidates.append(token)
    return candidates


def _english_name_candidates(value: str) -> list[str]:
    value = re.sub(r"[\u4e00-\u9fff]+", " ", value)
    value = value.split("—", 1)[0]
    candidates = []
    for match in ENGLISH_NAME_RE.finditer(value):
        candidate = normalize_text(match.group(1)).removeprefix("Dr. ")
        if candidate.casefold() in {
            "best paper",
            "research scientist",
            "principal investigator",
            "deepseek core",
        }:
            continue
        candidates.append(candidate)
    return candidates


def _english_name_from_content(name_zh: str | None, content: str) -> str | None:
    if not name_zh:
        return None
    sample = markdown_unescape(content[:600])
    pattern = re.compile(
        re.escape(name_zh)
        + r"\s*[（(]\s*"
        + ENGLISH_NAME_RE.pattern.replace("(?<![A-Za-z])", "")
        + r"\s*[)）]"
    )
    match = pattern.search(sample)
    if match:
        values = _english_name_candidates(match.group(0))
        if values:
            return values[0]
    return None


def _chinese_name_from_content(name_en: str | None, content: str) -> str | None:
    if not name_en:
        return None
    sample = markdown_unescape(content[:700])
    pattern = re.compile(
        re.escape(name_en)
        + r"\s*[（(]\s*([\u4e00-\u9fff]{2,4})(?:[^）)]*)[)）]",
        re.I,
    )
    match = pattern.search(sample)
    if match:
        candidates = _chinese_name_candidates(match.group(1))
        if candidates:
            return candidates[0]
    return None


def parse_person_name(title: str, content: str) -> tuple[str, str | None, str | None, list[str]]:
    cleaned = _strip_heading_noise(title)
    zh_candidates = _chinese_name_candidates(cleaned)
    en_candidates = _english_name_candidates(cleaned)
    name_zh = zh_candidates[0] if zh_candidates else None
    name_en = en_candidates[0] if en_candidates else None
    if name_zh is None:
        name_zh = _chinese_name_from_content(name_en, content)
    if name_en is None:
        name_en = _english_name_from_content(name_zh, content)
    begins_english = bool(re.match(r"^(?:Dr\.\s+)?[A-Za-z]", cleaned))
    canonical = name_en if begins_english and name_en else name_zh or name_en
    if not canonical:
        raise ValueError(f"heading does not contain a person name: {title}")
    aliases = list(dict.fromkeys(value for value in (name_zh, name_en) if value and value != canonical))
    for alternative in re.split(r"\s*/\s*", cleaned):
        for candidate in _english_name_candidates(alternative):
            if candidate not in {canonical, *aliases}:
                aliases.append(candidate)
    return canonical, name_zh, name_en, aliases


def _host_matches(host: str, target: str) -> bool:
    return host == target or host.endswith(f".{target}")


def _support_kind(url: str) -> str:
    parsed = urllib.parse.urlsplit(canonical_url(url))
    host = parsed.netloc
    path = parsed.path.casefold()
    for target, kind in SUPPORT_HOST_KINDS.items():
        if _host_matches(host, target):
            return kind
    if "/news/" in path or host.startswith("news.") or "hznews." in host:
        return "news"
    if host == "github.com":
        return "github"
    if _host_matches(host, "linkedin.com"):
        return "linkedin"
    if re.fullmatch(r"scholar\.google\.[a-z.]+", host):
        return "scholar"
    return "web"


def _looks_personal_homepage(
    url: str,
    context: str,
    aliases: Iterable[str],
    *,
    label: str = "",
) -> tuple[bool, str]:
    parsed = urllib.parse.urlsplit(canonical_url(url))
    host = parsed.netloc
    path = parsed.path.casefold()
    if path.endswith((".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png")):
        return False, "download or media file, not a maintained HTML profile"
    if any(_host_matches(host, blocked) for blocked in BLOCKED_HOMEPAGE_HOSTS):
        return False, "blocked non-personal host"
    if host in KNOWN_PROJECT_OR_GROUP_HOMEPAGES:
        return False, "known project, company, laboratory, or group homepage"
    if host.endswith(".github.io") and host.split(".", 1)[0] in KNOWN_ORGANIZATION_GITHUBS:
        return False, "known organization GitHub Pages site"
    clean_label = markdown_unescape(label)
    if NON_PERSONAL_CONTEXT_RE.search(clean_label):
        return False, "link label identifies a project, organization, paper, or report"
    if PERSONAL_CONTEXT_RE.search(clean_label):
        return True, "explicit personal-page link label"
    personal_matches = list(PERSONAL_CONTEXT_RE.finditer(context))
    non_personal_matches = list(NON_PERSONAL_CONTEXT_RE.finditer(context))
    if non_personal_matches and (
        not personal_matches
        or non_personal_matches[-1].start() > personal_matches[-1].start()
    ):
        return False, "context identifies a project, organization, paper, or report"
    if personal_matches:
        return True, "explicit personal-page context"
    if host.endswith(".github.io") or host.endswith(".wordpress.com"):
        return True, "personal publishing host"
    if host == "sites.google.com" and "/view/" in path:
        return True, "Google Sites personal-page pattern"
    if host == "huggingface.co" and len([part for part in path.split("/") if part]) == 1:
        return True, "public Hugging Face person profile"
    if any(marker in path for marker in ("/~", "/people/", "/faculty/", "/profile/", "/members/")):
        return True, "institutional person-page path"
    name_tokens = {
        token
        for alias in aliases
        for token in re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]{2,}", normalize_key(alias))
    }
    haystack = normalize_key(f"{host} {path}")
    if any(token in haystack for token in name_tokens):
        return True, "URL contains a person-name token"
    return False, "generic URL lacks personal-page evidence"


def classify_url_evidence(
    url: str,
    *,
    context: str,
    aliases: Iterable[str],
    label: str = "",
) -> UrlEvidence:
    canonical = canonical_url(markdown_unescape(url))
    detected = classify_url(canonical)
    support = _support_kind(canonical)
    if support not in {"web", "github", "linkedin", "scholar"}:
        return UrlEvidence(
            url=url,
            canonical_url=canonical,
            detected_kind=detected,
            support_kind=support,
            tracking_eligible=False,
            context=context,
            reason=f"supporting {support} identity/evidence page, not an active four-kind tracking page",
        )
    if detected in {"scholar", "linkedin"}:
        return UrlEvidence(
            url=url,
            canonical_url=canonical,
            detected_kind=detected,
            support_kind=support,
            tracking_eligible=True,
            context=context,
            reason="stable profile URL",
        )
    if detected == "github":
        owner = urllib.parse.urlsplit(canonical).path.strip("/").casefold()
        if owner in KNOWN_ORGANIZATION_GITHUBS:
            eligible, reason = False, "known organization GitHub account"
        elif re.search(r"实验室|课题组|团队|组织|项目|代码|仓库|repo", context, re.I):
            eligible, reason = False, "GitHub context points to an organization or project"
        else:
            eligible, reason = True, "single-segment GitHub profile"
        return UrlEvidence(
            url=url,
            canonical_url=canonical,
            detected_kind=detected,
            support_kind=support,
            tracking_eligible=eligible,
            context=context,
            reason=reason,
        )
    if detected == "homepage":
        if urllib.parse.urlsplit(canonical).netloc == "huggingface.co":
            owner = urllib.parse.urlsplit(canonical).path.strip("/").casefold()
            if owner in KNOWN_ORGANIZATION_HUGGINGFACE_PROFILES:
                eligible, reason = False, "known organization Hugging Face profile"
            else:
                eligible, reason = _looks_personal_homepage(
                    canonical,
                    context,
                    aliases,
                    label=label,
                )
        else:
            eligible, reason = _looks_personal_homepage(
                canonical,
                context,
                aliases,
                label=label,
            )
        return UrlEvidence(
            url=url,
            canonical_url=canonical,
            detected_kind=detected,
            support_kind=support,
            tracking_eligible=eligible,
            context=context,
            reason=reason,
        )
    return UrlEvidence(
        url=url,
        canonical_url=canonical,
        detected_kind=None,
        support_kind=support,
        tracking_eligible=False,
        context=context,
        reason=f"supporting {support} URL, not one of the four tracked page kinds",
    )


def extract_url_evidence(content: str, aliases: Iterable[str]) -> list[UrlEvidence]:
    link_matches = list(MARKDOWN_LINK_RE.finditer(content))
    seen: set[str] = set()
    output: list[UrlEvidence] = []

    def append(url: str, label: str, start: int, end: int) -> None:
        url = markdown_unescape(url)
        url = re.sub(r"(?i)%E2%80%9[CD]", "", url)
        url = re.split(r"[、（）()，,；;。]", url, maxsplit=1)[0].rstrip(".\\")
        for separator in ("%EF%BC%88", "%28"):
            position = url.upper().find(separator)
            if position >= 0:
                url = url[:position].rstrip("/")
        for suffix in ("%EF%BC%89%E3%80%82", "%EF%BC%89", "%E3%80%82"):
            if url.upper().endswith(suffix):
                url = url[: -len(suffix)].rstrip("/")
        try:
            canonical = canonical_url(url)
        except ValueError:
            return
        if canonical in seen:
            return
        seen.add(canonical)
        separators = "\n；;。|"
        field_start = max(content.rfind(marker, 0, start) for marker in separators) + 1
        following = [
            position
            for marker in separators
            if (position := content.find(marker, end)) >= 0
        ]
        field_end = min(following) if following else min(len(content), end + 120)
        field_context = markdown_unescape(content[max(field_start, start - 120) : field_end])
        context = normalize_text(
            " ".join(value for value in (label, field_context) if value)
        )[:320]
        output.append(
            classify_url_evidence(
                url,
                context=context,
                aliases=aliases,
                label=label,
            )
        )

    for match in link_matches:
        append(match.group(2), markdown_unescape(match.group(1)), match.start(), match.end())
    link_spans = [(match.start(), match.end()) for match in link_matches]
    for match in URL_RE.finditer(content):
        if any(start <= match.start() < end for start, end in link_spans):
            continue
        append(match.group(0), "", match.start(), match.end())
    return output


def extract_profile(
    content: str,
    *,
    heading: str,
    section_path: list[str],
    cohort: str,
) -> dict[str, Any]:
    plain = markdown_unescape(content)
    status = None
    status_evidence = normalize_text(" ".join([*section_path, heading, plain[:260]]))
    for marker, value in STATUS_TERMS.items():
        if marker in status_evidence:
            status = value
            break
    stage_match = re.search(
        r"(本科生|硕士生|博士生|博士后|研究员|研究科学家|助理教授|副教授|教授|"
        r"创始人|联合创始人|首席科学家|工程师)",
        plain,
    )
    affiliation_match = re.search(
        r"(?:现任|现为|目前(?:是|就职于)?|隶属于|来自|任职于|是)"
        r"([^。；;\n]{2,70}?(?:大学|学院|研究院|实验室|研究所|公司|集团|团队|"
        r"DeepMind|DeepSeek|OpenAI|ByteDance|Google|Meta|Apple|Microsoft|NVIDIA))",
        plain,
        re.I,
    )
    focus_match = re.search(
        r"(?:研究方向|研究聚焦|主要研究|专注于|长期从事)[：:\s]*([^。；;\n]{4,160})",
        plain,
        re.I,
    )
    year_match = next(
        (
            re.search(r"\b(20\d{2})\b", value)
            for value in reversed(section_path)
            if re.search(r"\b20\d{2}\b", value)
        ),
        None,
    )
    return {
        "cohort": cohort,
        "section_path": section_path,
        "cohort_year": year_match.group(1) if year_match else None,
        "affiliation": affiliation_match.group(1).strip() if affiliation_match else None,
        "stage_or_role": stage_match.group(1) if stage_match else None,
        "research_focus": focus_match.group(1).strip() if focus_match else None,
        "employment_status": status,
    }


def _is_person_heading(spec: DocumentSpec, heading: Heading) -> bool:
    if heading.level not in spec.person_levels:
        return False
    title = _strip_heading_noise(heading.title)
    if not title or title in SECTION_TITLES:
        return False
    if spec.parser_kind == "deepseek":
        if heading.level == 3:
            return "人员" in heading.parent_titles and "论文" not in heading.parent_titles
        return heading.level == 4 and title.startswith("梁文锋")
    if spec.parser_kind == "tsinghua_award" and "候选人未入围" in title:
        return False
    if re.fullmatch(r"20\d{2}.*", title):
        return False
    return True


def _deepseek_extra_rows(text: str) -> list[tuple[str, str, int, list[str]]]:
    output: list[tuple[str, str, int, list[str]]] = []
    appendix_start = text.find("## 附录：团队人物全览")
    appendix = text[appendix_start:] if appendix_start >= 0 else ""
    appendix_line = text.count("\n", 0, appendix_start) if appendix_start >= 0 else 0
    for relative_line, line in enumerate(appendix.splitlines(), 1):
        line_number = appendix_line + relative_line
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [markdown_unescape(cell) for cell in line.strip().strip("|").split("|")]
        title = ""
        if len(cells) >= 6 and re.fullmatch(r"\d+", cells[0]):
            title = " ".join(value for value in (cells[2], cells[1]) if value and value != "—")
        elif len(cells) >= 3 and cells[0] not in {"姓名", "频次"}:
            title = cells[0]
        if title and not any(term in title for term in NON_PERSON_TERMS):
            output.append((title, line, line_number, ["附录：团队人物全览"]))
    pitch_start = appendix.find("### 六、Pitch 优先级")
    pitch_end = appendix.find("### 七、延伸资源", pitch_start)
    pitch_text = appendix[pitch_start:pitch_end] if pitch_start >= 0 else ""
    pitch = re.compile(r"^-\s+\*\*([^*]+)\*\*[：:](.+)$", re.M)
    for match in pitch.finditer(pitch_text):
        title = markdown_unescape(match.group(1))
        if any(term in title for term in NON_PERSON_TERMS):
            continue
        absolute_start = appendix_start + pitch_start + match.start()
        line_number = text.count("\n", 0, absolute_start) + 1
        output.append(
            (
                title,
                match.group(0),
                line_number,
                ["附录：团队人物全览", "Pitch 优先级"],
            )
        )
    return output


def parse_document(path: str | Path) -> list[RawPersonRecord]:
    path = Path(path)
    spec = DOCUMENT_SPECS.get(path.name)
    if spec is None:
        raise ValueError(f"unsupported Mono mapping document: {path.name}")
    text = path.read_text(encoding="utf-8")
    source_sha = sha256_text(text)
    records: list[RawPersonRecord] = []
    seen_local_names: set[str] = set()
    candidates: list[tuple[str, str, int, int, list[str], int]] = []
    for heading in parse_headings(text):
        if not _is_person_heading(spec, heading):
            continue
        content = text[heading.start : heading.end].strip()
        if path.name == "Seedance 相关人员.md" and heading.line == 13:
            content = content.split("**Weilin Huang**", 1)[0].strip()
        candidates.append(
            (
                heading.title,
                content,
                heading.line,
                heading.level,
                heading.parent_titles,
                heading.start,
            )
        )
    if spec.parser_kind == "deepseek":
        for title, content, line, parents in _deepseek_extra_rows(text):
            candidates.append((title, content, line, 0, parents, text.find(content)))
    for title, content, line, level, parents, start in candidates:
        name_title = title
        if spec.parser_kind == "tsinghua_award":
            trailing_name = re.search(
                r"[)）]\s*([\u4e00-\u9fff]{2,4})\s*$",
                markdown_unescape(title),
            )
            if trailing_name:
                name_title = trailing_name.group(1)
        try:
            canonical_name, name_zh, name_en, aliases = parse_person_name(
                name_title,
                content,
            )
        except ValueError:
            continue
        local_key = min(
            [normalize_key(canonical_name), *(normalize_key(alias) for alias in aliases if alias)]
        )
        if spec.parser_kind == "deepseek" and level == 0 and local_key in seen_local_names:
            continue
        seen_local_names.add(local_key)
        all_aliases = list(dict.fromkeys([canonical_name, *aliases]))
        url_evidence = extract_url_evidence(content, all_aliases)
        emails = sorted(set(value.casefold() for value in EMAIL_RE.findall(markdown_unescape(content))))
        record_id = "rec_" + sha256_text(
            f"{path.name}|{line}|{canonical_name}|{sha256_text(content)}"
        )[:20]
        profile = extract_profile(
            content,
            heading=title,
            section_path=parents,
            cohort=spec.cohort,
        )
        excluded_emails = EXCLUDED_IDENTITY_EMAILS.get((path.name, canonical_name), set())
        if excluded_emails:
            profile["excluded_identity_emails"] = sorted(excluded_emails)
            profile["data_quality_flags"] = [
                "source lists another person's email; retained as raw evidence but excluded from identity matching"
            ]
        records.append(
            RawPersonRecord(
                record_id=record_id,
                source_document=path.name,
                source_sha256=source_sha,
                cohort=spec.cohort,
                heading=title,
                heading_level=level,
                heading_line=line,
                section_path=parents,
                canonical_name=canonical_name,
                name_zh=name_zh,
                name_en=name_en,
                aliases=aliases,
                content=content,
                content_hash=sha256_text(content),
                emails=emails,
                urls=url_evidence,
                profile=profile,
                extraction_confidence=0.98 if level else 0.90,
            )
        )
    return records


def parse_mono_directory(root: str | Path) -> tuple[list[dict[str, Any]], list[RawPersonRecord]]:
    root = Path(root)
    paths = sorted(root.glob("*.md"))
    unknown = [path.name for path in paths if path.name not in DOCUMENT_SPECS]
    missing = sorted(set(DOCUMENT_SPECS) - {path.name for path in paths})
    if unknown or missing:
        raise ValueError(f"document set mismatch: unknown={unknown} missing={missing}")
    documents: list[dict[str, Any]] = []
    records: list[RawPersonRecord] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        parsed = parse_document(path)
        documents.append(
            {
                "filename": path.name,
                "path": str(path.resolve()),
                "sha256": sha256_text(text),
                "size_bytes": len(text.encode("utf-8")),
                "record_count": len(parsed),
                "cohort": DOCUMENT_SPECS[path.name].cohort,
            }
        )
        records.extend(parsed)
    return documents, records


def name_forms(record: RawPersonRecord) -> set[str]:
    forms: set[str] = set()
    for value in (record.canonical_name, *record.aliases):
        normalized = normalize_key(value)
        if normalized:
            forms.add(normalized)
        no_nickname = re.sub(r"\([^)]*\)|（[^）]*）|[\"“][^\"”]+[\"”]", " ", value)
        normalized = normalize_key(no_nickname)
        if normalized:
            forms.add(normalized)
        if re.fullmatch(r"[A-Za-z .'\-]+", value):
            parts = normalized.split()
            if len(parts) == 2:
                forms.add(" ".join(reversed(parts)))
    return forms


def _affiliation_tokens(record: RawPersonRecord) -> set[str]:
    text = markdown_unescape(
        " ".join(
            value
            for value in (
                str(record.profile.get("affiliation") or ""),
                record.cohort,
                record.content[:1200],
            )
            if value
        )
    ).casefold()
    aliases = {
        "清华大学": "tsinghua",
        "清华": "tsinghua",
        "北京大学": "peking",
        "北大": "peking",
        "字节跳动": "bytedance",
        "bytedance": "bytedance",
        "deepseek": "deepseek",
        "google deepmind": "deepmind",
        "deepmind": "deepmind",
        "麻省理工": "mit",
        "mit": "mit",
        "斯坦福": "stanford",
        "stanford": "stanford",
        "普林斯顿": "princeton",
        "princeton": "princeton",
        "腾讯": "tencent",
        "tencent": "tencent",
        "快手": "kuaishou",
        "kuaishou": "kuaishou",
        "apple": "apple",
        "openai": "openai",
        "meta": "meta",
        "nvidia": "nvidia",
        "阿里巴巴": "alibaba",
        "阿里": "alibaba",
        "alibaba": "alibaba",
    }
    output = {canonical for marker, canonical in aliases.items() if marker in text}
    for match in re.finditer(
        r"([\u4e00-\u9fffA-Za-z .&-]{2,40}(?:大学|学院|研究院|实验室|研究所|公司|集团))",
        text,
        re.I,
    ):
        output.add(normalize_key(match.group(1)))
    for email in record.emails:
        output.add(f"domain:{email.rsplit('@', 1)[-1]}")
    return output


def _content_ngrams(record: RawPersonRecord) -> set[str]:
    value = normalize_key(markdown_unescape(record.content[:1800]))
    value = re.sub(r"\s+", "", value)
    return {value[index : index + 4] for index in range(max(0, len(value) - 3))}


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _names_compatible(left: RawPersonRecord, right: RawPersonRecord) -> bool:
    if not name_forms(left) & name_forms(right):
        return False
    if left.name_zh and right.name_zh and left.name_zh != right.name_zh:
        return False
    if left.name_en and right.name_en:
        left_en = normalize_key(left.name_en)
        right_en = normalize_key(right.name_en)
        left_parts, right_parts = left_en.split(), right_en.split()
        initials_compatible = (
            len(left_parts) == len(right_parts) == 2
            and left_parts[-1] == right_parts[-1]
            and left_parts[0][0] == right_parts[0][0]
        )
        if (
            left_en != right_en
            and " ".join(reversed(left_parts)) != right_en
            and not initials_compatible
        ):
            return False
    return True


def _manual_same_person(left: RawPersonRecord, right: RawPersonRecord) -> bool:
    forms = name_forms(left) | name_forms(right)
    return any(pair <= forms for pair in MANUAL_SAME_PERSON_NAME_PAIRS)


def _manual_record_decision(left: RawPersonRecord, right: RawPersonRecord) -> str | None:
    key = frozenset(
        (
            (left.source_document, left.canonical_name),
            (right.source_document, right.canonical_name),
        )
    )
    return MANUAL_RECORD_DECISIONS.get(key)


class _UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            next_value = self.parent[value]
            self.parent[value] = root
            value = next_value
        return root

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def cluster_records(
    records: list[RawPersonRecord],
) -> tuple[list[RegistryPerson], list[IdentityEdge]]:
    union = _UnionFind(record.record_id for record in records)
    edges: list[IdentityEdge] = []
    identity_index: dict[str, list[RawPersonRecord]] = {}
    name_index: dict[str, list[RawPersonRecord]] = {}
    for record in records:
        for key in record.identity_keys:
            identity_index.setdefault(key, []).append(record)
        for key in name_forms(record):
            name_index.setdefault(key, []).append(record)
    pair_decisions: set[tuple[str, str]] = set()

    def record_edge(
        left: RawPersonRecord,
        right: RawPersonRecord,
        decision: str,
        method: str,
        score: float,
        evidence: list[str],
    ) -> None:
        pair = tuple(sorted((left.record_id, right.record_id)))
        if pair in pair_decisions:
            return
        pair_decisions.add(pair)
        edges.append(
            IdentityEdge(
                left_record_id=pair[0],
                right_record_id=pair[1],
                decision=decision,
                method=method,
                score=score,
                evidence=evidence,
            )
        )
        if decision == "auto_merge":
            union.union(left.record_id, right.record_id)

    for key, matches in identity_index.items():
        for index, left in enumerate(matches):
            for right in matches[index + 1 :]:
                if _names_compatible(left, right) or _manual_same_person(left, right):
                    method = (
                        "manual_name_variant_shared_identity"
                        if _manual_same_person(left, right)
                        else "shared_stable_identity"
                    )
                    record_edge(left, right, "auto_merge", method, 1.0, [key])
                else:
                    record_edge(
                        left,
                        right,
                        "conflict",
                        "shared_identity_incompatible_name",
                        1.0,
                        [key],
                    )

    for key, matches in name_index.items():
        if len(matches) < 2:
            continue
        for index, left in enumerate(matches):
            for right in matches[index + 1 :]:
                pair = tuple(sorted((left.record_id, right.record_id)))
                if pair in pair_decisions or not _names_compatible(left, right):
                    continue
                manual_decision = _manual_record_decision(left, right)
                if manual_decision == "auto_merge":
                    record_edge(
                        left,
                        right,
                        "auto_merge",
                        "manual_biography_corroboration",
                        0.99,
                        [f"name:{key}", "same RoboParty founder biography"],
                    )
                    continue
                if manual_decision == "distinct":
                    distinction = (
                        "DSpark paper assigns Wentao Zhang to DeepSeek-AI only; "
                        "the other record is the Peking University professor"
                        if left.canonical_name == right.canonical_name == "Wentao Zhang"
                        else "Yale spatial-omics researcher versus Kuaishou/Alibaba engineering executive"
                    )
                    record_edge(
                        left,
                        right,
                        "distinct",
                        "manual_biography_conflict",
                        1.0,
                        [
                            f"name:{key}",
                            distinction,
                        ],
                    )
                    continue
                affiliations = _affiliation_tokens(left) & _affiliation_tokens(right)
                similarity = _jaccard(_content_ngrams(left), _content_ngrams(right))
                same_document = left.source_document == right.source_document
                chinese_key = bool(re.fullmatch(r"[\u4e00-\u9fff]+", key))
                common_english_surname = bool(
                    re.search(
                        r"\b(wang|zhang|li|liu|chen|yang|zhao|wu|zhou|xu|sun|ma|gao|huang|he)$",
                        key,
                    )
                )
                if same_document:
                    record_edge(
                        left,
                        right,
                        "auto_merge",
                        "same_document_duplicate_name",
                        0.98,
                        [f"name:{key}"],
                    )
                elif affiliations:
                    record_edge(
                        left,
                        right,
                        "auto_merge",
                        "name_and_affiliation",
                        0.96,
                        [f"name:{key}", *sorted(affiliations)[:4]],
                    )
                elif similarity >= 0.12:
                    record_edge(
                        left,
                        right,
                        "auto_merge",
                        "name_and_biography_similarity",
                        round(0.90 + min(similarity, 0.09), 3),
                        [f"name:{key}", f"content_jaccard:{similarity:.3f}"],
                    )
                elif chinese_key and len(key) >= 3 and len(matches) == 2:
                    record_edge(
                        left,
                        right,
                        "review",
                        "same_three_character_name_only",
                        0.72,
                        [f"name:{key}"],
                    )
                elif not chinese_key and not common_english_surname and len(matches) == 2:
                    record_edge(
                        left,
                        right,
                        "review",
                        "same_rare_english_name_only",
                        0.70,
                        [f"name:{key}"],
                    )
                else:
                    record_edge(
                        left,
                        right,
                        "review",
                        "same_name_insufficient_context",
                        0.55,
                        [f"name:{key}"],
                    )

    clusters: dict[str, list[RawPersonRecord]] = {}
    for record in records:
        clusters.setdefault(union.find(record.record_id), []).append(record)
    auto_methods: dict[str, list[str]] = {}
    for edge in edges:
        if edge.decision == "auto_merge":
            root = union.find(edge.left_record_id)
            auto_methods.setdefault(root, []).append(edge.method)

    people: list[RegistryPerson] = []
    for root, cluster in clusters.items():
        cluster.sort(key=lambda record: (record.source_document, record.heading_line))
        primary = max(
            cluster,
            key=lambda record: (
                bool(record.name_en and record.name_zh),
                len(record.trackable_urls),
                record.extraction_confidence,
                -record.heading_line,
            ),
        )
        aliases = list(
            dict.fromkeys(
                value
                for record in cluster
                for value in (record.canonical_name, *record.aliases)
                if value != primary.canonical_name
            )
        )
        trackable = list(
            dict.fromkeys(url for record in cluster for url in record.trackable_urls)
        )
        supporting = list(
            dict.fromkeys(
                item.canonical_url
                for record in cluster
                for item in record.urls
                if not item.tracking_eligible
            )
        )
        profiles = [record.profile for record in cluster]
        profile = {
            "affiliations": list(
                dict.fromkeys(
                    str(item["affiliation"])
                    for item in profiles
                    if item.get("affiliation")
                )
            ),
            "stages_or_roles": list(
                dict.fromkeys(
                    str(item["stage_or_role"])
                    for item in profiles
                    if item.get("stage_or_role")
                )
            ),
            "research_focuses": list(
                dict.fromkeys(
                    str(item["research_focus"])
                    for item in profiles
                    if item.get("research_focus")
                )
            ),
            "employment_status_claims": list(
                dict.fromkeys(
                    str(item["employment_status"])
                    for item in profiles
                    if item.get("employment_status")
                )
            ),
            "cohort_years": list(
                dict.fromkeys(
                    str(item["cohort_year"])
                    for item in profiles
                    if item.get("cohort_year")
                )
            ),
        }
        methods = sorted(set(auto_methods.get(root, [])))
        admission = "active_tracking" if trackable else "pending_anchor"
        confidence = (
            "stable_identity"
            if any(record.identity_keys for record in cluster)
            else "corroborated_name"
            if len(cluster) > 1
            else "single_source"
        )
        registry_id = "mono_" + sha256_text(
            "|".join(sorted(record.record_id for record in cluster))
        )[:20]
        people.append(
            RegistryPerson(
                registry_person_id=registry_id,
                canonical_name=primary.canonical_name,
                name_zh=primary.name_zh,
                name_en=primary.name_en,
                aliases=aliases,
                record_ids=[record.record_id for record in cluster],
                cohorts=list(dict.fromkeys(record.cohort for record in cluster)),
                source_documents=list(
                    dict.fromkeys(record.source_document for record in cluster)
                ),
                trackable_urls=trackable,
                supporting_urls=supporting,
                profile=profile,
                admission_status=admission,
                identity_confidence=confidence,
                merge_methods=methods,
            )
        )
    people.sort(key=lambda person: normalize_key(person.canonical_name))
    edges.sort(key=lambda edge: (edge.decision, -edge.score, edge.left_record_id))
    return people, edges


def records_json(records: Iterable[RawPersonRecord]) -> str:
    return json.dumps([record.as_json() for record in records], ensure_ascii=False, indent=2)


__all__ = [
    "DOCUMENT_SPECS",
    "PARSER_VERSION",
    "DocumentSpec",
    "IdentityEdge",
    "RawPersonRecord",
    "RegistryPerson",
    "UrlEvidence",
    "classify_url_evidence",
    "cluster_records",
    "extract_url_evidence",
    "name_forms",
    "parse_document",
    "parse_headings",
    "parse_mono_directory",
    "parse_person_name",
]
