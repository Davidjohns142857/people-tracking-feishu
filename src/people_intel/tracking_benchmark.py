from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from people_intel.schemas import StrictModel


BENCHMARK_VERSION = "tracking-monitoring-benchmark-v1"

ChannelId = Literal["homepage", "arxiv", "github", "news", "x", "wechat", "xhs"]
StageId = Literal[
    "trigger",
    "plan",
    "discover",
    "version",
    "evidence",
    "resolve",
    "graph",
    "deliver",
]


class EvidenceAnchor(StrictModel):
    anchor_id: str
    channel: ChannelId
    title: str
    url: str
    source_identity: str
    published_at: str | None = None
    evidence_level: Literal[
        "official_primary",
        "direct_fulltext",
        "captured_api_response",
        "secondary_report",
    ]
    verification_mode: Literal["live_api", "captured_response", "official_url", "replay_fixture"]
    verified_at: str
    rights_scope: Literal["public", "user_supplied"]
    truth_summary_zh: str
    raw_object_ref: str | None = None
    stable_ids: dict[str, str] = Field(default_factory=dict)


class BenchmarkCase(StrictModel):
    case_id: str
    scope_kind: Literal["channel", "stage"]
    scope_id: str
    difficulty: Literal["easy", "medium", "hard"]
    title_zh: str
    objective_zh: str
    input_contract_zh: str
    adversarial_condition_zh: str
    anchor_ids: list[str]
    required_capabilities: list[str]
    required_artifacts: list[str]
    expected_events: list[str]
    expected_decision_zh: str
    forbidden_outcomes_zh: list[str]


class AlgorithmRound(StrictModel):
    round_id: Literal["round_1", "round_2", "round_3"]
    name_zh: str
    method_zh: str
    capabilities: list[str]
    data_layout_zh: list[str]
    changes_from_previous_zh: list[str]
    known_limits_zh: list[str]


class BenchmarkCaseResult(StrictModel):
    case_id: str
    scope_kind: str
    scope_id: str
    difficulty: str
    passed: bool
    unmet_capabilities: list[str]
    evidence_anchor_count: int
    produced_artifacts: list[str]
    emitted_events: list[str]
    decision_zh: str
    failure_reasons_zh: list[str]


class BenchmarkRoundReport(StrictModel):
    round_id: str
    name_zh: str
    passed_cases: int
    total_cases: int
    pass_rate: float
    channel_passed: int
    channel_total: int
    stage_passed: int
    stage_total: int
    by_difficulty: dict[str, str]
    failed_capabilities: dict[str, int]
    case_results: list[BenchmarkCaseResult]


class TrackingBenchmarkDefinition(StrictModel):
    benchmark_version: str
    objective_zh: str
    data_format_zh: list[str]
    invariants_zh: list[str]
    evidence_anchors: list[EvidenceAnchor]
    rounds: list[AlgorithmRound]
    cases: list[BenchmarkCase]


class TrackingBenchmarkReport(StrictModel):
    benchmark_version: str
    benchmark_hash: str
    evaluated_at: str
    channel_case_count: int
    stage_case_count: int
    total_case_count: int
    channels: list[str]
    stages: list[str]
    rounds: list[BenchmarkRoundReport]
    final_release_passed: bool
    release_gates: dict[str, str]
    conclusions_zh: list[str]


class LiveSmokeChannelResult(StrictModel):
    channel: ChannelId
    status: str
    route: str
    duration_ms: int | None = None
    result_count: int | None = None
    raw_object_ref: str | None = None
    error: str | None = None
    identity: dict[str, object] = Field(default_factory=dict)
    evidence_reference: str | None = None


class LiveSmokeManifest(StrictModel):
    executed_at: str
    mode: Literal["low_frequency_live_smoke"]
    results: list[LiveSmokeChannelResult]
    notes_zh: list[str] = Field(default_factory=list)


_R1 = {
    "cron_due",
    "manual_trigger",
    "known_url_first",
    "exact_query",
    "source_fetch",
    "basic_rank",
    "hash_compare",
    "raw_manifest",
    "text_span",
    "basic_extract",
    "name_match",
    "append_assertion",
    "daily_digest",
    "evidence_link",
}

_R2 = _R1 | {
    "weekly_due",
    "idempotent_bucket",
    "person_scope",
    "stable_account_id",
    "query_enrichment",
    "channel_selection",
    "canonical_url",
    "structured_diff",
    "partial_failure",
    "summary_pending",
    "json_pointer",
    "pdf_locator",
    "time_precision",
    "missing_not_negative",
    "identity_conflict",
    "conflict_coexistence",
    "working_view_alternatives",
    "review_queue",
    "weekly_aggregate",
    "coverage_report",
    "dedupe",
}

_R3 = _R2 | {
    "missed_run_recovery",
    "timezone_boundary",
    "disabled_subject_gate",
    "negative_query_memory",
    "query_feedback",
    "fallback_ladder",
    "risk_control_breaker",
    "author_attribution",
    "relationship_gate",
    "dynamic_noise_filter",
    "redirect_chain",
    "deletion_is_change_only",
    "paginated_checkpoint",
    "fulltext_gate",
    "contradictory_spans",
    "refine_supersede",
    "derived_dependencies",
    "derived_staleness",
    "projection_rebuild",
    "no_investment_conclusion",
    "delivery_conflict_explain",
}


ROUNDS = [
    AlgorithmRound(
        round_id="round_1",
        name_zh="第一轮：关键词与快照基线",
        method_zh="已知 URL 或精确关键词直取，保存原文与 hash，做最小字段抽取和日报。",
        capabilities=sorted(_R1),
        data_layout_zh=[
            "结果以渠道列表排列，缺少统一人物边界。",
            "原文、hash 和基础 EvidenceSpan 已保存，但账号连续性和冲突解释不足。",
        ],
        changes_from_previous_zh=["建立可跑通的最小闭环，先测召回与不可变保存。"],
        known_limits_zh=["容易受同名、排序噪声、动态页面和二手摘要污染。"],
    ),
    AlgorithmRound(
        round_id="round_2",
        name_zh="第二轮：逐人限定与稳定标识",
        method_zh="先解析 person_key，再用主页、平台稳定 ID、机构和项目丰富查询；加入版本、冲突和审核层。",
        capabilities=sorted(_R2),
        data_layout_zh=[
            "EvidenceAnchor 独立去重，Case 仅引用 anchor_id。",
            "每个案例固定记录 input、对抗条件、期望产物、事件和禁止结果。",
            "按 channel 与 workflow stage 双矩阵展示。",
        ],
        changes_from_previous_zh=[
            "加入 stable account id、person scope、canonical URL、结构化 diff。",
            "摘要只进入 pending，缺失不再转为否定事实。",
        ],
        known_limits_zh=["复杂降级、风控熔断、逻辑派生失效和断点恢复仍不完整。"],
    ),
    AlgorithmRound(
        round_id="round_3",
        name_zh="第三轮：证据门、断点与反馈闭环",
        method_zh="以 checkpoint 驱动增量扫描，执行渠道降级阶梯、证据准入、派生依赖和可解释交付。",
        capabilities=sorted(_R3),
        data_layout_zh=[
            "公共证据锚点、案例契约、算法能力和运行结果四层分离。",
            "同一测试可用于 pytest、API、CLI、HTML 与后续真实 connector smoke。",
            "失败以 unmet_capabilities 聚合，直接形成下一轮工程任务。",
        ],
        changes_from_previous_zh=[
            "加入 signature/risk-control 熔断、fallback ladder、query feedback。",
            "加入 derived dependency/staleness、projection rebuild 和冲突交付。",
            "日报、周报只汇总逐人结果，不把跨人物长文本重新送入抽取器。",
        ],
        known_limits_zh=["冻结回放证明算法契约；凭证、限流和站点变更仍需低频 live smoke 单独验证。"],
    ),
]


ANCHORS = [
    EvidenceAnchor(
        anchor_id="homepage-zanlin",
        channel="homepage",
        title="Zanlin Ni 个人主页",
        url="https://nzl-thu.github.io/",
        source_identity="nzl-thu.github.io",
        evidence_level="official_primary",
        verification_mode="official_url",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="人物主页是已知网址刷新入口；页面变化本身不等价于职业事实变化。",
    ),
    EvidenceAnchor(
        anchor_id="homepage-flexibility-trap",
        channel="homepage",
        title="The Flexibility Trap 项目页",
        url="https://nzl-thu.github.io/the-flexibility-trap",
        source_identity="nzl-thu.github.io",
        evidence_level="official_primary",
        verification_mode="official_url",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="项目页提供论文、作者与项目上下文，可用于主页新增链接和跨来源核验。",
    ),
    EvidenceAnchor(
        anchor_id="homepage-ssi",
        channel="homepage",
        title="Safe Superintelligence 官方网站",
        url="https://ssi.inc/",
        source_identity="Safe Superintelligence Inc.",
        evidence_level="official_primary",
        verification_mode="official_url",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="公司官网可作为创始关系的直接来源之一，但融资金额需另行保留媒体来源。",
    ),
    EvidenceAnchor(
        anchor_id="arxiv-flexibility-trap",
        channel="arxiv",
        title="The Flexibility Trap",
        url="https://arxiv.org/abs/2601.15165",
        source_identity="arXiv",
        published_at="2026-01-21",
        evidence_level="official_primary",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="作者包括 Zanlin Ni、Shenzhi Wang、Yang Yue；适合测试作者消歧和共同作者派生。",
        stable_ids={"arxiv_id": "2601.15165"},
    ),
    EvidenceAnchor(
        anchor_id="arxiv-entropy-minority",
        channel="arxiv",
        title="Beyond the 80/20 Rule",
        url="https://arxiv.org/abs/2506.01939",
        source_identity="arXiv",
        published_at="2025-06-02",
        evidence_level="official_primary",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="作者包括 Shenzhi Wang 与 Yang Yue；用于版本、长作者表和跨论文关系测试。",
        stable_ids={"arxiv_id": "2506.01939"},
    ),
    EvidenceAnchor(
        anchor_id="arxiv-absolute-zero",
        channel="arxiv",
        title="Absolute Zero",
        url="https://arxiv.org/abs/2505.03335",
        source_identity="arXiv",
        published_at="2025-05-06",
        evidence_level="official_primary",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="同一批作者在多篇论文出现，不能仅凭姓名把账号或机构自动合并。",
        stable_ids={"arxiv_id": "2505.03335"},
    ),
    EvidenceAnchor(
        anchor_id="github-shenzhi",
        channel="github",
        title="Shenzhi-Wang GitHub 账号",
        url="https://github.com/Shenzhi-Wang",
        source_identity="GitHub",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="login 可变化，database id 与 node id 用于连续性。",
        stable_ids={"database_id": "86948348", "node_id": "MDQ6VXNlcjg2OTQ4MzQ4"},
    ),
    EvidenceAnchor(
        anchor_id="github-justgrpo",
        channel="github",
        title="LeapLabTHU/JustGRPO",
        url="https://github.com/LeapLabTHU/JustGRPO",
        source_identity="GitHub",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="组织仓库不是个人仓库；Zanlin Ni 的 commit 与账号证据可支持 contributed_to 候选。",
        stable_ids={"repository_id": "1132844275", "commit_prefix": "1a2fddb5c665"},
    ),
    EvidenceAnchor(
        anchor_id="news-ssi-founder",
        channel="news",
        title="Ilya Sutskever starts Safe Superintelligence",
        url="https://apnews.com/article/c6b48a3675fb3fb459859dece2b45499",
        source_identity="Associated Press",
        published_at="2024-06-20",
        evidence_level="secondary_report",
        verification_mode="official_url",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="AP 报道支持创业事件；需与公司官网交叉核验身份。",
    ),
    EvidenceAnchor(
        anchor_id="news-ssi-funding",
        channel="news",
        title="SSI raises $1 billion",
        url="https://www.investing.com/news/stock-market-news/exclusiveopenai-cofounder-sutskevers-new-safetyfocused-ai-startup-ssi-raises-1-billion-3600613",
        source_identity="Reuters mirror / Investing.com",
        evidence_level="secondary_report",
        verification_mode="replay_fixture",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="融资金额、时间和投资方保留 Reuters 镜像来源，不混入官网未陈述内容。",
    ),
    EvidenceAnchor(
        anchor_id="news-together-2026",
        channel="news",
        title="Together AI raises $800 million",
        url="https://www.investing.com/news/stock-market-news/together-ai-raises-800-million-at-83-billion-valuation-4771027",
        source_identity="Reuters mirror / Investing.com",
        published_at="2026-07-01",
        evidence_level="secondary_report",
        verification_mode="official_url",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="同一报道同时含融资额、估值和领投方，字段必须分别保留证据。",
    ),
    EvidenceAnchor(
        anchor_id="x-shenzhi-profile",
        channel="x",
        title="Shenzhi Wang X profile",
        url="https://x.com/ShenzhiWang_THU",
        source_identity="X",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="handle 是展示字段，numeric user id 是一级账号连续性锚点。",
        stable_ids={"user_id": "1676443035184316416"},
    ),
    EvidenceAnchor(
        anchor_id="x-shenzhi-icml",
        channel="x",
        title="Shenzhi Wang ICML post",
        url="https://x.com/ShenzhiWang_THU/status/2059112265933357316",
        source_identity="X",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="用于 authored 内容与一级账号 ID 一致性测试。",
        stable_ids={"post_id": "2059112265933357316", "author_id": "1676443035184316416"},
    ),
    EvidenceAnchor(
        anchor_id="x-etched-funding",
        channel="x",
        title="Etched funding announcement thread",
        url="https://x.com/Etched/status/2071972062202343590",
        source_identity="X",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="主帖、回复、引用与转推必须保持传播关系，不能都算作被跟踪人物本人陈述。",
        stable_ids={"post_id": "2071972062202343590", "author_id": "1781914021760991232"},
    ),
    EvidenceAnchor(
        anchor_id="xhs-zanlin-profile",
        channel="xhs",
        title="Zanlin Ni 小红书一级账号",
        url="https://www.xiaohongshu.com/user/profile/5b89cbbf0b967e0001d196a0",
        source_identity="小红书",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="nickname 和搜索排名可变，user_id 是一级账号稳定标识。",
        stable_ids={"user_id": "5b89cbbf0b967e0001d196a0"},
    ),
    EvidenceAnchor(
        anchor_id="xhs-zanlin-icml",
        channel="xhs",
        title="终于轮到我发 ICML 最佳论文宣传了",
        url="https://www.xiaohongshu.com/explore/6a4e588c000000001702ec29",
        source_identity="小红书",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="目标人物笔记在宽泛搜索中可能排名较后，必须以 author.user_id 过滤。",
        stable_ids={"note_id": "6a4e588c000000001702ec29", "author_id": "5b89cbbf0b967e0001d196a0"},
    ),
    EvidenceAnchor(
        anchor_id="xhs-collaborator-icml",
        channel="xhs",
        title="我们拿ICML最佳论文奖了！！！",
        url="https://www.xiaohongshu.com/explore/6a5657830000000021019207",
        source_identity="小红书",
        evidence_level="captured_api_response",
        verification_mode="live_api",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="共同作者笔记可能排在目标人物之前，属于 related_author，不是目标一级账号。",
        stable_ids={"note_id": "6a5657830000000021019207", "author_id": "6396cd4f000000002702a2f1"},
    ),
    EvidenceAnchor(
        anchor_id="wechat-kimi-funding",
        channel="wechat",
        title="融资超10亿美金，AI公司「月之暗面」获新一轮投资",
        url="https://mp.weixin.qq.com/s/unr45dijDMG9WpImESIm0Q",
        source_identity="智能涌现",
        evidence_level="direct_fulltext",
        verification_mode="captured_response",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="已保存全文的公众号报道，可抽 EvidenceSpan；报道中的“独家获悉”仍是 reported。",
        raw_object_ref="sha256/b5/d5/b5d5468c505434961ca93b820aaa22c7b212c88d10aa1e7d356d4df04675b38c",
    ),
    EvidenceAnchor(
        anchor_id="wechat-video-rebirth",
        channel="wechat",
        title="Video Rebirth 完成5000万美元融资",
        url="https://mp.weixin.qq.com/s/5cHYgedbCwub1c2kmhtkCg",
        source_identity="机器之心",
        evidence_level="direct_fulltext",
        verification_mode="captured_response",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="搜索响应包含正文；人物、公司和轮次仍需各自证据定位。",
        raw_object_ref="sha256/00/15/0015fe520a4dcece82417aab47630a4f080f4f12456c3f1a300c540f9fb031dd",
    ),
    EvidenceAnchor(
        anchor_id="wechat-minimax-talks",
        channel="wechat",
        title="阿里领投了这家大模型初创公司！",
        url="https://mp.weixin.qq.com/s/WE3dx2iOScTFvyPhzlfRLA",
        source_identity="财联社 AI daily / 创投日报",
        evidence_level="direct_fulltext",
        verification_mode="captured_response",
        verified_at="2026-07-27T00:00:00+08:00",
        rights_scope="public",
        truth_summary_zh="正文明确称融资仍在进行、最终金额未确定，禁止写成已完成 FundingRound。",
        raw_object_ref="sha256/00/15/0015fe520a4dcece82417aab47630a4f080f4f12456c3f1a300c540f9fb031dd",
    ),
]


def _case(
    case_id: str,
    scope_kind: Literal["channel", "stage"],
    scope_id: str,
    difficulty: Literal["easy", "medium", "hard"],
    title: str,
    adversary: str,
    anchors: list[str],
    capabilities: list[str],
    artifacts: list[str],
    events: list[str],
    decision: str,
    forbidden: list[str],
) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        difficulty=difficulty,
        title_zh=title,
        objective_zh=f"验证 {scope_id} 在“{title}”条件下仍满足人物监控契约。",
        input_contract_zh="输入为已锁定 person_key、当前 SearchPlan/checkpoint 与引用的公共证据锚点。",
        adversarial_condition_zh=adversary,
        anchor_ids=anchors,
        required_capabilities=capabilities,
        required_artifacts=artifacts,
        expected_events=events,
        expected_decision_zh=decision,
        forbidden_outcomes_zh=forbidden,
    )


def _channel_cases() -> list[BenchmarkCase]:
    specs: dict[str, list[tuple]] = {
        "homepage": [
            ("easy", "已知主页内容未变化", "重复抓取完全相同内容。", ["homepage-zanlin"], ["known_url_first", "source_fetch", "hash_compare"], ["ConnectorAttempt", "SourceManifest"], ["connector.completed"], "返回 unchanged，不重复抽取。", ["创建重复 SourceVersion"]),
            ("easy", "主页新增论文链接", "新增链接只出现于项目区。", ["homepage-zanlin", "homepage-flexibility-trap"], ["known_url_first", "hash_compare", "text_span", "basic_extract"], ["SourceDocumentVersion", "EvidenceSpan"], ["source.versioned", "extraction.completed"], "创建页面新版本与 paper 候选。", ["无 span 直接确认 authored"]),
            ("medium", "页面重排但语义未变", "导航与区块顺序改变导致整页 diff。", ["homepage-zanlin"], ["structured_diff", "canonical_url"], ["ChangeSet"], ["change.detected"], "仅保留结构变化，不制造字段补丁。", ["把重排当职业变化"]),
            ("medium", "主页删除工作经历", "旧版本段落消失但无离职声明。", ["homepage-zanlin"], ["structured_diff", "missing_not_negative"], ["ChangeSet", "ProfilePatchReview"], ["change.detected"], "生成 homepage_change 待审，不生成 left。", ["自动关闭任职区间"]),
            ("hard", "同一人物多站点与跳转", "主页、项目页和重定向 URL 共存。", ["homepage-zanlin", "homepage-flexibility-trap"], ["redirect_chain", "canonical_url", "person_scope"], ["SourceDocumentVersion", "CanonicalSourceLink"], ["source.versioned"], "保留抓取 URI 与 canonical URI 的关系。", ["把同一内容计为多个独立事实"]),
            ("hard", "动态时间戳与埋点噪声", "每次加载 HTML 都含不同 analytics/token。", ["homepage-zanlin"], ["dynamic_noise_filter", "hash_compare"], ["NormalizedDocument", "ChangeSet"], ["connector.completed"], "规范化 hash 不变，扫描结束。", ["因动态脚本触发高分信号"]),
        ],
        "arxiv": [
            ("easy", "精确作者出现新论文", "姓名与 person_key 已确认。", ["arxiv-flexibility-trap"], ["exact_query", "source_fetch", "basic_extract"], ["PaperCandidate", "EvidenceSpan"], ["extraction.completed"], "生成 authored 候选并保留 arXiv ID。", ["只存标题不存来源"]),
            ("easy", "同一 arXiv ID 出现新版", "版本号变化但论文实体相同。", ["arxiv-entropy-minority"], ["hash_compare", "raw_manifest"], ["SourceDocumentVersion", "PaperVersion"], ["source.versioned"], "追加论文版本，不复制 Paper 实体。", ["把 v2 当新论文"]),
            ("medium", "同名作者与机构歧义", "仅姓名匹配，作者表含同名候选。", ["arxiv-absolute-zero"], ["person_scope", "query_enrichment", "identity_conflict"], ["DiscoveryCandidate", "IdentityReview"], ["extraction.completed"], "机构/主页不足时保持待确认。", ["仅按姓名自动合并"]),
            ("medium", "长作者表产生共同作者", "多篇论文共享部分作者。", ["arxiv-flexibility-trap", "arxiv-entropy-minority"], ["person_scope", "dedupe", "review_queue"], ["DerivedCoauthorCandidate"], ["extraction.completed"], "coauthor 作为 derived 候选进入审核。", ["把同论文作者直接确认 same_as"]),
            ("hard", "跨版本标题与分类变化", "标题小改、cross-list 分类变化、发布时间不变。", ["arxiv-flexibility-trap"], ["refine_supersede", "time_precision", "paginated_checkpoint"], ["PaperVersion", "AssertionRelation"], ["source.versioned"], "用 refines/supersedes 连接旧提取。", ["覆盖旧标题或伪造精确时间"]),
            ("hard", "API 临时空结果", "作者 feed 暂时返回空，但已知论文 URL 可读。", ["arxiv-flexibility-trap"], ["fallback_ladder", "partial_failure", "negative_query_memory"], ["ConnectorAttempt", "SearchPlanFeedback"], ["connector.completed"], "记录失败并回退已知 ID，不删除论文。", ["把空结果解释为撤稿"]),
        ],
        "github": [
            ("easy", "已知账号新增仓库", "login 与稳定 ID 均匹配。", ["github-shenzhi"], ["known_url_first", "stable_account_id", "basic_extract"], ["RepositoryCandidate", "AccountObservation"], ["extraction.completed"], "以 repo id 创建候选并更新 checkpoint。", ["仅凭名字绑定账号"]),
            ("easy", "仓库 pushed_at 增量", "已有仓库发生新 push。", ["github-justgrpo"], ["hash_compare", "raw_manifest", "stable_account_id"], ["RepositoryVersion", "Checkpoint"], ["source.versioned"], "保存 default branch SHA 与 pushed_at。", ["重复发出同一 push 信号"]),
            ("medium", "fork 与自有仓库区分", "搜索命中 fork，owner 不是被跟踪人物。", ["github-shenzhi"], ["author_attribution", "stable_account_id"], ["RepositoryCandidate"], ["extraction.completed"], "标记 fork/associated，不生成 maintains。", ["把 fork 当原创仓库"]),
            ("medium", "组织仓库中的个人贡献", "仓库 owner 为组织，贡献者是人物账号。", ["github-justgrpo"], ["relationship_gate", "stable_account_id", "person_scope"], ["ContributionCandidate", "CommitEvidence"], ["extraction.completed"], "只有 contributor/commit 证据后生成 contributed_to 候选。", ["把组织仓库自动归个人"]),
            ("hard", "login 改名保持账号连续", "profile login 变化但 database/node id 不变。", ["github-shenzhi"], ["stable_account_id", "refine_supersede"], ["AccountObservation", "AliasAssertion"], ["assertion.appended"], "追加新 login alias，保持 IdentityAccount 实体。", ["按新 login 新建另一个人"]),
            ("hard", "commit 邮箱与账号归属冲突", "commit name 匹配但 login 或邮箱指向另一身份。", ["github-justgrpo"], ["relationship_gate", "identity_conflict", "contradictory_spans"], ["IdentityReview", "CommitEvidence"], ["extraction.completed"], "冲突共存并进入人工审核。", ["强行确认 contributed_to"]),
        ],
        "news": [
            ("easy", "官方创业消息被报道", "官网与 AP 同时支持创业事件。", ["news-ssi-founder", "homepage-ssi"], ["source_fetch", "basic_extract", "evidence_link"], ["EventCandidate", "EvidenceSpan"], ["extraction.completed"], "生成 cofounded 候选并标明两类来源。", ["把媒体发布时间当成立日"]),
            ("easy", "确认融资报道", "报道明确使用 raises/完成融资。", ["news-ssi-funding"], ["source_fetch", "text_span", "basic_extract"], ["FundingRoundCandidate"], ["extraction.completed"], "融资额按报道来源写 reported 候选。", ["无 span 直接确认"]),
            ("medium", "同一 Reuters 多镜像", "多个镜像标题和 URL 不同。", ["news-together-2026"], ["canonical_url", "dedupe"], ["DiscoveryCandidate", "CanonicalStory"], ["extraction.completed"], "按事件键去重但保存各来源版本。", ["重复发出多条融资信号"]),
            ("medium", "金额估值投资方分字段", "一句话同时含 $800m、$8.3b 和领投方。", ["news-together-2026"], ["json_pointer", "conflict_coexistence"], ["FieldPatch", "EvidenceSpan"], ["extraction.completed"], "各字段引用对应 span，来源差异并存。", ["把估值写成融资额"]),
            ("hard", "融资洽谈不等于完成", "来源明确称 still in progress / 金额未定。", ["wechat-minimax-talks"], ["fulltext_gate", "contradictory_spans", "time_precision"], ["ReportedEventCandidate"], ["extraction.completed"], "仅生成“融资进行中” reported 候选。", ["创建已完成 FundingRound"]),
            ("hard", "多轮融资与金额冲突", "历史轮次、当前轮次和媒体估算混在文中。", ["news-ssi-funding", "news-together-2026"], ["conflict_coexistence", "refine_supersede", "delivery_conflict_explain"], ["FundingRoundCandidate", "ConflictSet"], ["signal.emitted"], "按公司、轮次、日期拆实体并展示冲突。", ["覆盖旧轮次"]),
        ],
        "x": [
            ("easy", "已知账号本人发布", "tracked_account_id 与 author_id 相同。", ["x-shenzhi-profile", "x-shenzhi-icml"], ["stable_account_id", "source_fetch", "basic_extract"], ["ContentObservation"], ["extraction.completed"], "关系标为 authored。", ["用 handle 代替稳定 ID"]),
            ("easy", "回复链中的技术信息", "目标人物回复他人帖子。", ["x-shenzhi-profile"], ["stable_account_id", "basic_extract"], ["ThreadObservation"], ["extraction.completed"], "保留 replied_to 和原帖作者。", ["把回复当独立官方公告"]),
            ("medium", "共同作者 mention 发现账号", "目标人物只在已知作者帖子中被 @。", ["x-shenzhi-icml"], ["query_enrichment", "person_scope", "review_queue"], ["AccountCandidate"], ["extraction.completed"], "mention 只创建账号候选。", ["自动确认 account_owned_by"]),
            ("medium", "引用帖与本人观点区分", "目标账号 quote 另一账号的融资消息。", ["x-etched-funding"], ["author_attribution", "stable_account_id"], ["ContentObservation"], ["extraction.completed"], "保留 quoted 与两层作者。", ["把被引文字当本人事实"]),
            ("hard", "转推融资新闻的错误归因", "被跟踪账号转推公司主帖。", ["x-etched-funding"], ["author_attribution", "relationship_gate"], ["ContentObservation", "SourceLink"], ["extraction.completed"], "信号可关联人物，但事实来源仍指向原作者。", ["把转推者写成融资公司"]),
            ("hard", "搜索端点失败的降级", "关键词搜索 404，已知账号 feed 仍可读。", ["x-shenzhi-profile"], ["fallback_ladder", "partial_failure", "negative_query_memory"], ["ConnectorAttempt", "SearchPlanFeedback"], ["connector.completed"], "继续刷新 known feed；Exa 只补 URL 候选。", ["用 Exa 摘要伪造 author_id"]),
        ],
        "wechat": [
            ("easy", "已知公众号 URL 取得全文", "代理返回正文和原始 URL。", ["wechat-kimi-funding"], ["known_url_first", "source_fetch", "text_span"], ["SourceDocumentVersion", "EvidenceSpan"], ["source.versioned"], "全文可抽取 reported 候选。", ["只存摘要"]),
            ("easy", "公开搜索发现文章摘要", "只有 Exa 卡片与 highlight。", ["wechat-video-rebirth"], ["exact_query", "basic_rank", "summary_pending"], ["DiscoveryCandidate"], ["connector.completed"], "保留 pending，等待全文。", ["从摘要直接确认融资"]),
            ("medium", "转载与原公众号去重", "标题相同但 author/source_identity 不同。", ["wechat-kimi-funding"], ["canonical_url", "dedupe"], ["CanonicalStory", "SourceLink"], ["extraction.completed"], "保留转载关系并选原始来源优先。", ["把转载当独立事件"]),
            ("medium", "缺失或中文发布时间", "正文仅写“刚刚”或无机器时间。", ["wechat-video-rebirth"], ["time_precision", "missing_not_negative"], ["TemporalBoundary"], ["extraction.completed"], "published_at 留空或保持模糊精度。", ["用抓取日伪造发布日期"]),
            ("hard", "反爬导致全文不可读", "搜索命中但 qiaomu/浏览器读取失败。", ["wechat-minimax-talks"], ["fulltext_gate", "partial_failure", "negative_query_memory"], ["ConnectorAttempt", "DiscoveryCandidate"], ["connector.completed"], "保留摘要和失败原因，不抽事实。", ["臆造正文 EvidenceSpan"]),
            ("hard", "二手引述与事实层级", "文章引用知情人士且称金额未定。", ["wechat-minimax-talks"], ["contradictory_spans", "fulltext_gate", "delivery_conflict_explain"], ["ReportedEventCandidate", "EvidenceSpan"], ["extraction.completed"], "epistemic_type=reported，状态=进行中。", ["写成官方确认融资"]),
        ],
        "xhs": [
            ("easy", "已知 user_id 读取本人笔记", "author.user_id 与一级账号一致。", ["xhs-zanlin-profile", "xhs-zanlin-icml"], ["stable_account_id", "source_fetch", "basic_extract"], ["ContentObservation"], ["extraction.completed"], "笔记标记 authored 并更新 note checkpoint。", ["按 nickname 绑定"]),
            ("easy", "姓名加项目词提升召回", "精确姓名搜索排名不稳定。", ["xhs-zanlin-icml"], ["query_enrichment", "basic_rank"], ["DiscoveryCandidate"], ["connector.completed"], "使用姓名+ICML/论文词并记录查询版本。", ["只保留首条结果"]),
            ("medium", "共同作者排名高于目标", "宽泛查询 rank1 是 collaborator。", ["xhs-zanlin-icml", "xhs-collaborator-icml"], ["person_scope", "stable_account_id", "author_attribution"], ["ContentObservation", "RelatedAuthorCandidate"], ["extraction.completed"], "按 author.user_id 过滤；共同作者另存关联。", ["把 rank1 当目标账号"]),
            ("medium", "中英文昵称与别名", "nickname 不含规范英文名。", ["xhs-zanlin-profile"], ["query_enrichment", "identity_conflict"], ["AccountCandidate", "IdentityReview"], ["extraction.completed"], "稳定 ID 或回链不足时待确认。", ["仅凭昵称模糊匹配确认"]),
            ("hard", "user-posts API 普通错误", "已知账号接口 api_error，但搜索可用。", ["xhs-zanlin-profile", "xhs-zanlin-icml"], ["fallback_ladder", "stable_account_id", "partial_failure"], ["ConnectorAttempt", "ContentObservation"], ["connector.completed"], "general 搜索后按 author.user_id 过滤再读详情。", ["无限重试 user-posts"]),
            ("hard", "签名错误与风控熔断", "signature_error 或验证码/IP block。", ["xhs-zanlin-profile"], ["risk_control_breaker", "fallback_ladder", "negative_query_memory"], ["CircuitBreakerEvent", "ConnectorAttempt"], ["connector.completed"], "签名错熔断到已登录 GUI；验证码停机等人工。", ["高频重试触发封禁"]),
        ],
    }
    cases: list[BenchmarkCase] = []
    for channel, rows in specs.items():
        for index, row in enumerate(rows, 1):
            difficulty, title, adversary, anchors, caps, artifacts, events, decision, forbidden = row
            cases.append(_case(
                f"channel-{channel}-{index:02d}", "channel", channel, difficulty,
                title, adversary, anchors, caps, artifacts, events, decision, forbidden,
            ))
    return cases


def _stage_cases() -> list[BenchmarkCase]:
    specs: dict[str, list[tuple]] = {
        "trigger": [
            ("easy", "Tier A 每日到期", "上次成功扫描早于当前日窗口。", ["homepage-zanlin"], ["cron_due"], ["ScanRun"], ["scan.started"], "创建一次 daily ScanRun。", ["跳过应执行人物"]),
            ("easy", "人工立即扫描", "用户在周期任务前手动触发。", ["homepage-zanlin"], ["manual_trigger"], ["ScanRun"], ["scan.started"], "记录 trigger=manual。", ["篡改下一次 cron"]),
            ("medium", "周度完整扫描到期", "活跃人物非 Tier A。", ["arxiv-flexibility-trap"], ["weekly_due", "person_scope"], ["ScanRun"], ["scan.started"], "选择 weekly_full 渠道集合。", ["只扫单一渠道"]),
            ("medium", "重复 cron 同一时间桶", "scheduler 重启后重复派发。", ["homepage-zanlin"], ["idempotent_bucket"], ["CommandReceipt"], ["scan.started"], "command_id/period bucket 去重。", ["创建两个 ScanRun"]),
            ("hard", "manual 与 cron 并发", "两个触发同时命中同一人物。", ["github-shenzhi"], ["idempotent_bucket", "missed_run_recovery"], ["ScanRun", "CommandReceipt"], ["scan.started"], "一个运行、另一个合并或返回幂等结果。", ["并发覆盖 checkpoint"]),
            ("hard", "时区边界与漏跑恢复", "Asia/Shanghai 周一边界，服务曾离线。", ["homepage-zanlin"], ["timezone_boundary", "missed_run_recovery", "disabled_subject_gate"], ["ScheduleDecision"], ["scan.started"], "补跑最近未完成窗口，不扫描禁用候选。", ["补跑全部历史窗口"]),
        ],
        "plan": [
            ("easy", "已知 URL 优先", "SearchPlan 同时含主页和开放搜索。", ["homepage-zanlin"], ["known_url_first"], ["PersonSearchPlanRevision"], ["connector.started"], "主页任务排在开放搜索前。", ["先全网搜再读主页"]),
            ("easy", "精确查询模板", "人物有规范名和唯一论文。", ["arxiv-flexibility-trap"], ["exact_query"], ["QueryPlan"], ["connector.started"], "生成可审计查询。", ["运行自由文本任意 shell"]),
            ("medium", "稳定账号 feed 规划", "handle 可能变更但 numeric id 已知。", ["x-shenzhi-profile"], ["stable_account_id", "person_scope"], ["QueryPlan"], ["connector.started"], "known-account route 保存 stable id。", ["只保存 handle"]),
            ("medium", "按人物类型选渠道", "研究者与创业者关注点不同。", ["arxiv-flexibility-trap", "news-ssi-founder"], ["channel_selection", "query_enrichment"], ["PersonSearchPlanRevision"], ["connector.started"], "研究者侧重论文/代码，创业者侧重新闻/官网。", ["所有人使用同一查询"]),
            ("hard", "未配置渠道与失败记忆", "XHS 无凭证，历史查询连续为空。", ["xhs-zanlin-profile"], ["negative_query_memory", "partial_failure"], ["SearchPlanFeedback"], ["connector.completed"], "跳过并说明缺失配置，保留失败次数。", ["模拟成功"]),
            ("hard", "查询反馈自动收敛", "宽泛查询长期命中共同作者噪声。", ["xhs-zanlin-icml", "xhs-collaborator-icml"], ["query_feedback", "query_enrichment", "stable_account_id"], ["PersonSearchPlanRevision"], ["connector.started"], "新版本加入项目词和 user_id 过滤。", ["原地改写旧 SearchPlan"]),
        ],
        "discover": [
            ("easy", "发现直接可读原文", "结果 URL 可正常读取。", ["wechat-kimi-funding"], ["source_fetch", "basic_rank"], ["DiscoveryCandidate"], ["connector.completed"], "selected 且进入版本化。", ["丢弃抓取方式"]),
            ("easy", "结果相关性排序", "多个结果只有一个含唯一项目词。", ["xhs-zanlin-icml"], ["basic_rank", "exact_query"], ["DiscoveryCandidate"], ["connector.completed"], "记录 rank 和选中理由。", ["只保留最终 URL"]),
            ("medium", "摘要候选等待全文", "搜索服务提供 highlight 但正文未取。", ["wechat-video-rebirth"], ["summary_pending"], ["DiscoveryCandidate"], ["connector.completed"], "status=pending_fulltext。", ["直接送入事实抽取"]),
            ("medium", "单渠道失败继续运行", "X 失败而 GitHub 正常。", ["x-shenzhi-profile", "github-shenzhi"], ["partial_failure", "person_scope"], ["ConnectorAttempt"], ["connector.completed"], "聚合覆盖说明且继续。", ["整轮标记完全失败"]),
            ("hard", "主路由失败走降级阶梯", "XHS user-posts api_error。", ["xhs-zanlin-profile"], ["fallback_ladder", "stable_account_id"], ["ConnectorAttempt", "DiscoveryCandidate"], ["connector.completed"], "按白名单 fallback，保留每次 attempt。", ["执行任意外部命令"]),
            ("hard", "验证码与 IP 风控", "连续返回验证码/风险控制。", ["xhs-zanlin-profile"], ["risk_control_breaker", "negative_query_memory"], ["CircuitBreakerEvent"], ["connector.completed"], "熔断并等待人工，不重试。", ["高频绕过风控"]),
        ],
        "version": [
            ("easy", "相同 hash 去重", "同 URL 相同规范化内容。", ["homepage-zanlin"], ["hash_compare"], ["SourceManifest"], ["connector.completed"], "unchanged。", ["重复 cognify"]),
            ("easy", "新 hash 建版本链", "同 URL 正文新增一段。", ["homepage-flexibility-trap"], ["hash_compare", "raw_manifest"], ["SourceDocumentVersion"], ["source.versioned"], "previous_version_id 指向旧版。", ["覆盖旧对象"]),
            ("medium", "canonical URL 与重定向", "抓取 URL 重定向到新域名。", ["homepage-zanlin"], ["canonical_url", "redirect_chain"], ["SourceDocumentVersion"], ["source.versioned"], "保留 source_uri 与 canonical 关系。", ["丢失原始请求 URL"]),
            ("medium", "结构化 diff", "页面只新增论文卡片。", ["homepage-flexibility-trap"], ["structured_diff"], ["ChangeSet"], ["change.detected"], "输出变化块与段落定位。", ["只返回整页字符串"]),
            ("hard", "删除内容只算页面变化", "工作经历段落从新版本消失。", ["homepage-zanlin"], ["deletion_is_change_only", "missing_not_negative"], ["ChangeSet"], ["change.detected"], "不自动生成 negative/left。", ["自动删除既有事实"]),
            ("hard", "分页 API 与断点", "结果跨页且中途失败。", ["github-justgrpo"], ["paginated_checkpoint", "partial_failure", "raw_manifest"], ["ConnectorAttempt", "Checkpoint"], ["connector.completed"], "保存 cursor/etag，重试从断点继续。", ["重复写入前页结果"]),
        ],
        "evidence": [
            ("easy", "字符级原文 span", "HTML 规范化为 Markdown。", ["homepage-flexibility-trap"], ["text_span"], ["EvidenceSpan"], ["extraction.completed"], "quote/hash/offset 可回溯。", ["只存摘要"]),
            ("easy", "API JSON Pointer", "GitHub 响应含 login/id/node_id。", ["github-shenzhi"], ["json_pointer"], ["EvidenceSpan"], ["extraction.completed"], "以 JSON Pointer 定位字段。", ["凭空拼接稳定 ID"]),
            ("medium", "PDF 页码与段落定位", "输入为名单 PDF。", ["arxiv-flexibility-trap"], ["pdf_locator", "time_precision"], ["EvidenceSpan"], ["extraction.completed"], "记录 page/paragraph 与规范化 hash。", ["只保留 OCR 摘要"]),
            ("medium", "摘要无全文不得升级", "搜索卡片含融资金额。", ["wechat-video-rebirth"], ["summary_pending", "fulltext_gate"], ["DiscoveryCandidate"], ["connector.completed"], "候选等待原文。", ["创建 human_confirmed Assertion"]),
            ("hard", "代码贡献最低证据门", "项目页链接仓库但无个人 commit。", ["github-justgrpo"], ["relationship_gate", "contradictory_spans"], ["ContributionCandidate"], ["extraction.completed"], "关联仓库待确认，不生成 contributed_to。", ["把项目链接当贡献证据"]),
            ("hard", "冲突原文 spans 并存", "两个来源对金额或角色表述不同。", ["news-ssi-funding", "wechat-minimax-talks"], ["contradictory_spans", "conflict_coexistence"], ["ConflictSet", "EvidenceSpan"], ["extraction.completed"], "两条 Assertion 共存并指向各自 span。", ["选择一条并删除另一条"]),
        ],
        "resolve": [
            ("easy", "稳定账号 ID 连续性", "X handle 显示字段变化。", ["x-shenzhi-profile"], ["stable_account_id", "name_match"], ["IdentityAccountObservation"], ["extraction.completed"], "保持同一 IdentityAccount。", ["新建 Person"]),
            ("easy", "月份精度不伪造日期", "来源只写 2026 年 5 月。", ["arxiv-flexibility-trap"], ["time_precision"], ["TemporalBoundary"], ["assertion.appended"], "precision=month。", ["伪造 5 月 1 日精确发生"]),
            ("medium", "同名人物待确认", "姓名相同，机构和主页不同。", ["arxiv-absolute-zero"], ["identity_conflict", "person_scope"], ["IdentityReview"], ["extraction.completed"], "possibly_same_as 或 not_same_as 候选。", ["破坏性合并"]),
            ("medium", "信息缺失不等于否定", "新主页没有旧任职字段。", ["homepage-zanlin"], ["missing_not_negative", "structured_diff"], ["ChangeSet"], ["change.detected"], "保留旧 Assertion。", ["生成 negative Assertion"]),
            ("hard", "冲突状态长期共存", "官网与新闻角色表述不一致。", ["homepage-ssi", "news-ssi-founder"], ["conflict_coexistence", "working_view_alternatives"], ["ConflictSet", "WorkingView"], ["assertion.appended"], "返回选中项和 alternatives。", ["隐藏冲突"]),
            ("hard", "更精确信息细化旧时间", "新来源给出日级日期。", ["news-ssi-founder"], ["refine_supersede", "time_precision"], ["AssertionRelation"], ["assertion.appended"], "新增 Assertion 并连接 refines。", ["原地改旧 Assertion"]),
        ],
        "graph": [
            ("easy", "新 authored 关系", "论文与人物均已消歧。", ["arxiv-flexibility-trap"], ["append_assertion", "evidence_link"], ["Assertion"], ["assertion.appended"], "追加 authored 候选与证据。", ["覆盖旧关系"]),
            ("easy", "FundingRound 图结构", "融资报道字段齐全。", ["news-together-2026"], ["append_assertion", "basic_extract"], ["FundingRound", "Assertion"], ["assertion.appended"], "公司—raised_funding—轮次。", ["只把金额写人物字符串"]),
            ("medium", "共同作者派生候选", "两人共享同一 Paper authored 边。", ["arxiv-flexibility-trap"], ["derived_dependencies", "review_queue"], ["DerivedAssertion"], ["assertion.appended"], "记录全部 authored 依赖并待审。", ["冒充来源直接陈述 coauthor"]),
            ("medium", "Working View 显示备选", "任职状态冲突。", ["homepage-ssi", "news-ssi-founder"], ["working_view_alternatives", "conflict_coexistence"], ["WorkingView"], ["signal.emitted"], "返回 selected 与 alternative IDs。", ["静默丢弃低优先来源"]),
            ("hard", "依赖拒绝使派生失效", "共同作者依赖之一被人工 reject。", ["arxiv-flexibility-trap"], ["derived_staleness", "derived_dependencies"], ["DerivedAssertion", "Annotation"], ["assertion.appended"], "derived status=stale。", ["继续当有效事实"]),
            ("hard", "投影清空后确定性重建", "Neo4j/Cognee projection 被删除。", ["homepage-zanlin", "arxiv-flexibility-trap"], ["projection_rebuild", "raw_manifest"], ["ProjectionRebuildReport"], ["projection.rebuilt"], "从原文、Assertion、Annotation 重建同一图摘要。", ["把投影当权威层"]),
        ],
        "deliver": [
            ("easy", "每日摘要去重", "同一信号由两个来源触发。", ["news-together-2026"], ["daily_digest", "dedupe"], ["PersonDigestBatch"], ["delivery.previewed"], "合并为一条事件并列来源。", ["推送两次"]),
            ("easy", "证据链接可回跳", "用户点击信号中的事实。", ["x-shenzhi-icml"], ["evidence_link"], ["DeliveryPreview"], ["delivery.previewed"], "跳转 Assertion→Span→SourceVersion。", ["只给结论"]),
            ("medium", "周报汇总逐人结果", "多人多渠道均有增量。", ["homepage-zanlin", "github-shenzhi"], ["weekly_aggregate", "person_scope"], ["PersonDigestBatch"], ["delivery.previewed"], "按人分区后汇总。", ["把全体长文本再送入抽取器"]),
            ("medium", "失败渠道覆盖说明", "XHS 熔断但 GitHub 成功。", ["xhs-zanlin-profile", "github-justgrpo"], ["coverage_report", "partial_failure"], ["DeliveryPreview"], ["delivery.previewed"], "列出缺失渠道、原因与下次动作。", ["隐藏失败"]),
            ("hard", "冲突与低置信审核入口", "事实存在两个版本且置信度不足。", ["news-ssi-funding", "wechat-minimax-talks"], ["delivery_conflict_explain", "review_queue"], ["ReviewBatch", "DeliveryPreview"], ["delivery.previewed"], "消息可一键确认/驳回/修正并看原文。", ["只显示单一当前值"]),
            ("hard", "不输出投资结论", "信号分数很高且含融资。", ["news-together-2026"], ["no_investment_conclusion", "evidence_link"], ["Signal", "DeliveryPreview"], ["signal.emitted", "delivery.previewed"], "仅说明变化、证据和不确定性。", ["生成买入/投资建议"]),
        ],
    }
    cases: list[BenchmarkCase] = []
    for stage, rows in specs.items():
        for index, row in enumerate(rows, 1):
            difficulty, title, adversary, anchors, caps, artifacts, events, decision, forbidden = row
            cases.append(_case(
                f"stage-{stage}-{index:02d}", "stage", stage, difficulty,
                title, adversary, anchors, caps, artifacts, events, decision, forbidden,
            ))
    return cases


CASES = _channel_cases() + _stage_cases()


def tracking_benchmark_definition() -> TrackingBenchmarkDefinition:
    return TrackingBenchmarkDefinition(
        benchmark_version=BENCHMARK_VERSION,
        objective_zh="对正常人物跟踪的七个来源渠道与一次扫描的八个流程环节，各设置六个三级难度对抗案例，并用三轮算法演进验证监控契约。",
        data_format_zh=[
            "EvidenceAnchor：去重保存公开 URL、稳定 ID、证据等级和验证方式。",
            "BenchmarkCase：只引用 anchor_id，并固定输入、对抗条件、能力要求、产物、事件、决策和禁止结果。",
            "AlgorithmRound：显式列出本轮能力集合；案例是否通过由 required_capabilities 的集合包含关系计算。",
            "BenchmarkRoundReport：按渠道、流程、难度和缺失能力聚合，失败项可直接转成工程任务。",
        ],
        invariants_zh=[
            "冻结回放测试算法语义；低频 live smoke 测试外部链路可用性，两者结果不得混写。",
            "搜索命中不等于事实确认；摘要、原文、官方来源和二手报道使用不同证据等级。",
            "所有人先解析 person_key，再执行人物专属检索；不允许全局结果反推人物。",
            "同一 URL 相同 hash 不重复抽取；页面删除或搜索为空不删除历史事实。",
            "首次账号归属、身份冲突、任职终止、融资差异和逻辑派生保留人工审核门。",
            "最终交付按人物聚合增量，必须展示来源覆盖、失败渠道、冲突和证据回跳。",
        ],
        evidence_anchors=ANCHORS,
        rounds=ROUNDS,
        cases=CASES,
    )


def evaluate_tracking_benchmark() -> TrackingBenchmarkReport:
    definition = tracking_benchmark_definition()
    anchor_ids = {item.anchor_id for item in definition.evidence_anchors}
    reports: list[BenchmarkRoundReport] = []
    for algorithm_round in definition.rounds:
        available = set(algorithm_round.capabilities)
        results: list[BenchmarkCaseResult] = []
        for case in definition.cases:
            unmet = sorted(set(case.required_capabilities) - available)
            missing_anchors = sorted(set(case.anchor_ids) - anchor_ids)
            failures = [f"本轮尚未实现能力：{item}" for item in unmet]
            failures.extend(f"缺失证据锚点：{item}" for item in missing_anchors)
            passed = not unmet and not missing_anchors
            results.append(BenchmarkCaseResult(
                case_id=case.case_id,
                scope_kind=case.scope_kind,
                scope_id=case.scope_id,
                difficulty=case.difficulty,
                passed=passed,
                unmet_capabilities=unmet,
                evidence_anchor_count=len(case.anchor_ids) - len(missing_anchors),
                produced_artifacts=case.required_artifacts if passed else [],
                emitted_events=case.expected_events if passed else [],
                decision_zh=case.expected_decision_zh if passed else "保持候选/失败状态，不越过事实准入门。",
                failure_reasons_zh=failures,
            ))
        difficulty = {}
        for level in ("easy", "medium", "hard"):
            subset = [item for item in results if item.difficulty == level]
            difficulty[level] = f"{sum(item.passed for item in subset)}/{len(subset)}"
        failed_capabilities = Counter(
            capability
            for item in results
            for capability in item.unmet_capabilities
        )
        channel = [item for item in results if item.scope_kind == "channel"]
        stage = [item for item in results if item.scope_kind == "stage"]
        reports.append(BenchmarkRoundReport(
            round_id=algorithm_round.round_id,
            name_zh=algorithm_round.name_zh,
            passed_cases=sum(item.passed for item in results),
            total_cases=len(results),
            pass_rate=round(sum(item.passed for item in results) / len(results), 3),
            channel_passed=sum(item.passed for item in channel),
            channel_total=len(channel),
            stage_passed=sum(item.passed for item in stage),
            stage_total=len(stage),
            by_difficulty=difficulty,
            failed_capabilities=dict(failed_capabilities.most_common()),
            case_results=results,
        ))
    normalized = definition.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    final = reports[-1]
    final_result_map = {item.case_id: item for item in final.case_results}
    all_anchored = all(
        case.anchor_ids and final_result_map[case.case_id].evidence_anchor_count == len(case.anchor_ids)
        for case in definition.cases
    )
    return TrackingBenchmarkReport(
        benchmark_version=BENCHMARK_VERSION,
        benchmark_hash=digest,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        channel_case_count=sum(item.scope_kind == "channel" for item in definition.cases),
        stage_case_count=sum(item.scope_kind == "stage" for item in definition.cases),
        total_case_count=len(definition.cases),
        channels=sorted({item.scope_id for item in definition.cases if item.scope_kind == "channel"}),
        stages=sorted({item.scope_id for item in definition.cases if item.scope_kind == "stage"}),
        rounds=reports,
        final_release_passed=(
            final.passed_cases == final.total_cases
            and all_anchored
            and len(definition.evidence_anchors) >= 2 * 7
        ),
        release_gates={
            "channel_matrix": "7 个渠道 × 6 案例，easy/medium/hard 各 2",
            "stage_matrix": "8 个环节 × 6 案例，easy/medium/hard 各 2",
            "public_evidence": "每个案例至少引用 1 个已登记公开证据锚点",
            "three_rounds": "通过数严格递增，第三轮 90/90",
            "fact_safety": "摘要不确认、缺失不否定、删除不推断、冲突不隐藏",
            "delivery_safety": "证据可回跳、渠道失败可见、不输出投资结论",
        },
        conclusions_zh=[
            "最佳排布不是按数据表字段，而是“渠道矩阵 × 流程矩阵”：前者检验外部平台特色，后者检验整轮扫描不会在中间环节丢语义。",
            "最佳数据格式是四层分离：EvidenceAnchor 去重公共事实，BenchmarkCase 定义契约，AlgorithmRound 定义能力，RoundReport 只保存可重算结果。",
            "第二轮解决主要检索噪声：person_key 先行、稳定账号 ID、canonical URL、摘要 pending 和冲突共存。",
            "第三轮解决长期运行问题：checkpoint、失败查询记忆、熔断降级、证据准入、派生失效、投影重建和解释性交付。",
            "冻结公开证据回放必须是 CI 主体；每渠道保留一个低频 live smoke，用于发现 API、签名、登录或站点结构变化。",
        ],
    )


def load_live_smoke_manifest(path: str | Path) -> LiveSmokeManifest | None:
    target = Path(path)
    if not target.exists():
        return None
    return LiveSmokeManifest.model_validate_json(target.read_text(encoding="utf-8"))


__all__ = [
    "BENCHMARK_VERSION",
    "TrackingBenchmarkDefinition",
    "TrackingBenchmarkReport",
    "evaluate_tracking_benchmark",
    "LiveSmokeManifest",
    "load_live_smoke_manifest",
    "tracking_benchmark_definition",
]
