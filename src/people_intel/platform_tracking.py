from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from people_intel.schemas import StrictModel


ALGORITHM_VERSION = "platform-tracking-v2"


class IdentitySignalDefinition(StrictModel):
    signal_id: str
    weight: int
    evidence_class: str
    meaning_zh: str


class IdentityAssessment(StrictModel):
    score: int = Field(ge=0, le=100)
    status: Literal["high_confidence_review", "candidate", "insufficient", "conflicted"]
    evidence_classes: list[str] = Field(default_factory=list)
    applied_signals: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    requires_human_confirmation: bool = True
    explanation_zh: list[str] = Field(default_factory=list)


class PlatformAlgorithmDefinition(StrictModel):
    platform: Literal["x", "xhs", "github"]
    stable_account_id: str
    known_account_route: list[str]
    discovery_ladder: list[str]
    attribution_rule_zh: str
    checkpoint_fields: list[str]
    fallback_rule_zh: str
    fact_gate_zh: str


class PlatformTrackingDefinition(StrictModel):
    version: str
    objective_zh: str
    invariants_zh: list[str]
    identity_signals: list[IdentitySignalDefinition]
    thresholds: dict[str, str]
    platforms: list[PlatformAlgorithmDefinition]
    round_policies: dict[str, list[str]]
    release_gates: dict[str, str]


class AdversarialObservation(StrictModel):
    query_path: list[str]
    found_event_id: str | None = None
    observed_account_id: str | None = None
    tracked_account_id: str | None = None
    content_author_id: str | None = None
    content_relationship: str | None = None
    identity_signals: list[str] = Field(default_factory=list)
    identity_conflicts: list[str] = Field(default_factory=list)
    relation_evidence: list[str] = Field(default_factory=list)
    selected_source_urls: list[str] = Field(default_factory=list)
    notes_zh: list[str] = Field(default_factory=list)


class AdversarialCase(StrictModel):
    case_id: str
    difficulty: Literal["easy", "medium", "hard"]
    platform: Literal["x", "xhs", "github"]
    title_zh: str
    person_name: str
    target_account_id: str | None = None
    expected_event_id: str
    expected_content_author_id: str | None = None
    expected_relationship: str | None = None
    expected_relation_evidence: list[str] = Field(default_factory=list)
    truth_urls: list[str]
    observations: dict[str, AdversarialObservation]


class CaseRoundResult(StrictModel):
    case_id: str
    difficulty: str
    platform: str
    passed: bool
    event_found: bool
    account_correct: bool
    attribution_correct: bool
    relation_gate_passed: bool
    identity_assessment: IdentityAssessment
    query_path: list[str]
    failure_reasons_zh: list[str] = Field(default_factory=list)
    selected_source_urls: list[str] = Field(default_factory=list)
    notes_zh: list[str] = Field(default_factory=list)


class AdversarialRoundReport(StrictModel):
    round_id: Literal["round_1", "round_2", "round_3"]
    algorithm_version: str
    case_results: list[CaseRoundResult]
    passed_cases: int
    total_cases: int
    event_recall: float
    stable_id_precision: float
    attribution_accuracy: float
    identity_pollution_count: int
    evidence_coverage: float


class AdversarialEvaluationReport(StrictModel):
    fixture_version: str
    fixture_hash: str
    captured_at: str
    case_count: int
    cases: list[AdversarialCase]
    rounds: list[AdversarialRoundReport]
    final_release_passed: bool
    conclusions_zh: list[str]


IDENTITY_SIGNALS = [
    IdentitySignalDefinition(
        signal_id="trusted_source_direct_link",
        weight=45,
        evidence_class="direct_link",
        meaning_zh="输入文件、本人主页或已确认平台资料直接给出账号 URL。",
    ),
    IdentitySignalDefinition(
        signal_id="reciprocal_homepage",
        weight=35,
        evidence_class="reciprocal_link",
        meaning_zh="平台简介链接回已知个人主页，或主页链接回平台账号。",
    ),
    IdentitySignalDefinition(
        signal_id="stable_id_continuity",
        weight=20,
        evidence_class="stable_id",
        meaning_zh="本轮平台内部稳定 ID 与已登记账号一致。",
    ),
    IdentitySignalDefinition(
        signal_id="canonical_or_alias_match",
        weight=15,
        evidence_class="name",
        meaning_zh="display name、作者名或 commit author 与规范姓名/别名一致。",
    ),
    IdentitySignalDefinition(
        signal_id="affiliation_match",
        weight=15,
        evidence_class="affiliation",
        meaning_zh="平台简介、邮件域或仓库资料与已知学校/公司/团队一致。",
    ),
    IdentitySignalDefinition(
        signal_id="unique_project_or_coauthor",
        weight=10,
        evidence_class="work_graph",
        meaning_zh="唯一论文、项目或共同作者路径与已有档案一致。",
    ),
    IdentitySignalDefinition(
        signal_id="commit_email_match",
        weight=20,
        evidence_class="commit_identity",
        meaning_zh="Git commit 邮箱与来源文件中已知邮箱一致。",
    ),
]

HARD_CONFLICTS = {
    "stable_id_owned_by_other_person": "稳定账号 ID 已归属于另一个 person_key。",
    "explicit_not_same_as": "人工或来源明确标记为不同人物。",
    "affiliation_contradiction": "账号资料明确指向不相容的另一人物或机构。",
}


PLATFORM_TRACKING_DEFINITION = PlatformTrackingDefinition(
    version=ALGORITHM_VERSION,
    objective_zh="先以 person_key 限定人物，再发现并核验一级账号；更新时把账号身份、内容原作者、传播动作与可写入事实严格分开。",
    invariants_zh=[
        "平台稳定 ID 是连续性锚点，handle、login、nickname 只是可变展示字段。",
        "搜索结果只能创建账号或内容候选，首次 account_owned_by 必须人工确认。",
        "tracked_account_id 与 content_author_id 分开保存；转推、引用和回复不得改写一级账号。",
        "已知账号刷新先于开放搜索，开放搜索不得反向决定 person_key。",
        "没有原文、EvidenceSpan 或关系所需的最低证据时，不生成事实 Assertion。",
        "查询失败、空结果和错误候选都写入版本化 SearchPlan 反馈，不删除历史查询。",
    ],
    identity_signals=IDENTITY_SIGNALS,
    thresholds={
        "high_confidence_review": "score >= 80 且至少两个独立 evidence_class；仍需人工确认首次账号归属",
        "candidate": "55 <= score < 80；继续寻找主页回链、稳定 ID 或机构证据",
        "insufficient": "score < 55；只保留 DiscoveryCandidate",
        "conflicted": "出现硬冲突时忽略正向分数，进入冲突审核",
    },
    platforms=[
        PlatformAlgorithmDefinition(
            platform="x",
            stable_account_id="rest/numeric user id",
            known_account_route=["twitter user {handle}", "twitter user-posts {handle}"],
            discovery_ladder=[
                "来源或共同作者帖中的 @mention",
                "姓名/别名 + 机构",
                "姓名/别名 + 唯一论文/项目",
                "twitter search 失败后使用 Exa site:x.com，仅作候选发现",
            ],
            attribution_rule_zh="每条内容保存 tracked_account_id、content_author_id 和 authored/retweeted/quoted/replied；只有 authored 且 ID 相等才进入“本人发布”。",
            checkpoint_fields=["newest_post_id", "newest_created_at"],
            fallback_rule_zh="关键词端点失败不影响已知账号 feed；Exa 只补公开候选 URL，不能补造作者 ID。",
            fact_gate_zh="帖子正文只形成 reported Assertion；融资、任职等事实仍需读取原帖或交叉来源。",
        ),
        PlatformAlgorithmDefinition(
            platform="xhs",
            stable_account_id="user_id",
            known_account_route=["xhs user {user_id}", "xhs user-posts {user_id}"],
            discovery_ladder=[
                "已知 user_id 直读",
                "姓名/别名 + 唯一项目/论文（general 排序）",
                "只保留 author.user_id 等于已知 ID 的搜索结果",
                "读取匹配 note 详情",
                "signature_error 熔断后才允许已登录 Extension Bridge 兜底",
            ],
            attribution_rule_zh="nickname 和排名不能证明身份；已知账号退化搜索必须逐条以 author.user_id 过滤。",
            checkpoint_fields=["newest_note_id", "newest_last_update_time", "pagination_cursor"],
            fallback_rule_zh="user/user-posts 普通 api_error 退化为定向搜索；signature_error 熔断到 GUI；验证码/IP block 停机等待人工。",
            fact_gate_zh="搜索卡片只做候选；取得 note 详情且作者 ID 匹配后，正文才能进入人物增量包。",
        ),
        PlatformAlgorithmDefinition(
            platform="github",
            stable_account_id="database id + node id",
            known_account_route=["gh api users/{login}", "gh api users/{login}/repos?sort=pushed"],
            discovery_ladder=[
                "来源文件或主页中的 GitHub URL",
                "个人账号 profile 与 blog/twitter 回链",
                "论文项目页或组织仓库 URL",
                "仓库 contributors",
                "commit author login/name/email",
                "必要时 PR/issue/release 证据",
            ],
            attribution_rule_zh="owner、maintainer、contributor 分开；组织仓库命中不能自动成为个人拥有的仓库。",
            checkpoint_fields=["repo_node_id", "pushed_at", "default_branch_sha", "latest_release_id"],
            fallback_rule_zh="论文标题 repo search 为空时沿项目 URL、组织仓库、contributors、commits 逐层回溯，不做全库人物向量召回。",
            fact_gate_zh="contributed_to 至少需要 contributor/commit/PR 的账号证据；commit 邮箱与已知来源一致可提高身份评分。",
        ),
    ],
    round_policies={
        "round_1": ["精确姓名或已知 handle/login", "读取首屏结果", "不做跨来源身份回链"],
        "round_2": ["加入机构、论文、项目和共同作者锚点", "稳定 ID 过滤", "区分内容原作者与传播账号"],
        "round_3": ["按证据路径评分账号", "GitHub contributor/commit 深挖", "记录失败查询与 checkpoint", "硬冲突门和人工确认门"],
    },
    release_gates={
        "confirmed_account_cross_person_pollution": "0",
        "known_feed_stable_id_match": "100%",
        "content_attribution_accuracy": "100%",
        "written_fact_evidence_coverage": "100%",
        "unfound_information_auto_deletion": "0",
        "risk_control_high_frequency_retry": "0",
    },
)


def assess_identity(signal_ids: list[str], conflicts: list[str]) -> IdentityAssessment:
    if conflicts:
        return IdentityAssessment(
            score=0,
            status="conflicted",
            conflicts=conflicts,
            explanation_zh=[HARD_CONFLICTS.get(item, item) for item in conflicts],
        )
    definitions = {item.signal_id: item for item in IDENTITY_SIGNALS}
    applied = [item for item in dict.fromkeys(signal_ids) if item in definitions]
    classes = sorted({definitions[item].evidence_class for item in applied})
    score = min(100, sum(definitions[item].weight for item in applied))
    if score >= 80 and len(classes) >= 2:
        status = "high_confidence_review"
    elif score >= 55:
        status = "candidate"
    else:
        status = "insufficient"
    return IdentityAssessment(
        score=score,
        status=status,
        evidence_classes=classes,
        applied_signals=applied,
        explanation_zh=[
            f"{definitions[item].meaning_zh}（+{definitions[item].weight}）"
            for item in applied
        ],
    )


def load_adversarial_fixture(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate_adversarial_fixture(path: str | Path) -> AdversarialEvaluationReport:
    raw = load_adversarial_fixture(path)
    cases = [AdversarialCase.model_validate(item) for item in raw["cases"]]
    rounds: list[AdversarialRoundReport] = []
    for round_id in ("round_1", "round_2", "round_3"):
        results = [_evaluate_case(case, round_id) for case in cases]
        total = len(results)
        event_hits = sum(item.event_found for item in results)
        account_cases = [item for item, case in zip(results, cases) if case.target_account_id]
        attribution_cases = [
            item
            for item, case in zip(results, cases)
            if case.expected_content_author_id or case.expected_relationship
        ]
        evidence_hits = sum(bool(item.selected_source_urls) for item in results)
        rounds.append(AdversarialRoundReport(
            round_id=round_id,
            algorithm_version=f"{ALGORITHM_VERSION}/{round_id}",
            case_results=results,
            passed_cases=sum(item.passed for item in results),
            total_cases=total,
            event_recall=round(event_hits / total, 3) if total else 0,
            stable_id_precision=round(
                sum(item.account_correct for item in account_cases) / len(account_cases),
                3,
            ) if account_cases else 1.0,
            attribution_accuracy=round(
                sum(item.attribution_correct for item in attribution_cases) / len(attribution_cases),
                3,
            ) if attribution_cases else 1.0,
            identity_pollution_count=sum(
                bool(case.target_account_id)
                and bool(case.observations[round_id].observed_account_id)
                and not result.account_correct
                for case, result in zip(cases, results)
            ),
            evidence_coverage=round(evidence_hits / total, 3) if total else 0,
        ))
    fixture_bytes = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    final = rounds[-1]
    return AdversarialEvaluationReport(
        fixture_version=str(raw["fixture_version"]),
        fixture_hash=hashlib.sha256(fixture_bytes).hexdigest(),
        captured_at=str(raw["captured_at"]),
        case_count=len(cases),
        cases=cases,
        rounds=rounds,
        final_release_passed=(
            final.passed_cases == final.total_cases
            and final.identity_pollution_count == 0
            and final.attribution_accuracy == 1.0
            and final.evidence_coverage == 1.0
        ),
        conclusions_zh=[
            "精确姓名搜索不足以确认一级账号；必须保存平台稳定 ID 并积累跨来源证据。",
            "传播关系是内容信号，不是账号身份：retweet/quote/reply 必须保留原作者。",
            "小红书已知账号的可靠退化不是重新猜昵称，而是定向搜索后以 user_id 过滤。",
            "GitHub 组织仓库需要 contributors 与 commit 证据，才能把关联项目升级为 contributed_to。",
            "最终算法把失败查询、空结果和错误候选作为下一版 SearchPlan 的输入，而不是隐藏失败。",
        ],
    )


def _evaluate_case(case: AdversarialCase, round_id: str) -> CaseRoundResult:
    observation = case.observations[round_id]
    identity = assess_identity(observation.identity_signals, observation.identity_conflicts)
    event_found = observation.found_event_id == case.expected_event_id
    account_correct = (
        True
        if case.target_account_id is None
        else observation.observed_account_id == case.target_account_id
    )
    attribution_correct = (
        (
            case.expected_content_author_id is None
            or observation.content_author_id == case.expected_content_author_id
        )
        and (
            case.expected_relationship is None
            or observation.content_relationship == case.expected_relationship
        )
        and (
            case.target_account_id is None
            or observation.tracked_account_id in {None, case.target_account_id}
        )
    )
    relation_gate_passed = set(case.expected_relation_evidence).issubset(
        set(observation.relation_evidence)
    )
    failures: list[str] = []
    if not event_found:
        failures.append("没有找到金标事件或找到了错误事件。")
    if not account_correct:
        failures.append("候选账号稳定 ID 与金标不一致，存在身份污染。")
    if not attribution_correct:
        failures.append("内容原作者、被跟踪账号或传播关系归因错误。")
    if not relation_gate_passed:
        failures.append("关系所需的 contributor/commit/原文证据不足。")
    return CaseRoundResult(
        case_id=case.case_id,
        difficulty=case.difficulty,
        platform=case.platform,
        passed=event_found and account_correct and attribution_correct and relation_gate_passed,
        event_found=event_found,
        account_correct=account_correct,
        attribution_correct=attribution_correct,
        relation_gate_passed=relation_gate_passed,
        identity_assessment=identity,
        query_path=observation.query_path,
        failure_reasons_zh=failures,
        selected_source_urls=observation.selected_source_urls,
        notes_zh=observation.notes_zh,
    )


__all__ = [
    "ALGORITHM_VERSION",
    "AdversarialEvaluationReport",
    "PLATFORM_TRACKING_DEFINITION",
    "PlatformTrackingDefinition",
    "assess_identity",
    "evaluate_adversarial_fixture",
]
