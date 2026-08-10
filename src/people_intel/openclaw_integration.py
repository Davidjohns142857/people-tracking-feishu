from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from people_intel.schemas import StrictModel, utc_now


OPENCLAW_INTEGRATION_VERSION = "people-tracking-openclaw-v1"
MIN_OPENCLAW_VERSION = "2026.5.29"


class OpenClawOperation(StrictModel):
    operation_id: str
    title_zh: str
    user_phrases_zh: list[str]
    purpose_zh: str
    input_zh: list[str]
    output_zh: list[str]
    endpoints: list[str]
    side_effect: Literal["read_only", "sandbox_write", "ledger_write", "external_read", "config_write"]
    confirmation_rule_zh: str
    failure_rule_zh: str


class FeishuUsageRule(StrictModel):
    rule_id: str
    title_zh: str
    rule_zh: str
    examples_zh: list[str]


class IntegrationSecretRequirement(StrictModel):
    secret_id: str
    required_for: list[str]
    required: bool
    injection_method_zh: str
    validation_method_zh: str
    never_do_zh: str


class ChannelDeploymentRule(StrictModel):
    channel: Literal["homepage", "arxiv", "github", "news", "x", "wechat", "xhs"]
    preferred_route: str
    credential_kind: str
    deployment_location: Literal["api_host", "stateful_host_worker", "either"]
    fallback_zh: str
    stop_condition_zh: str


class OpenClawIntegrationManifest(StrictModel):
    integration_version: str
    minimum_openclaw_version: str
    architecture_zh: list[str]
    operations: list[OpenClawOperation]
    feishu_rules: list[FeishuUsageRule]
    secrets: list[IntegrationSecretRequirement]
    channels: list[ChannelDeploymentRule]
    api_security: dict[str, object]
    required_openclaw_components: list[str]
    generated_at: datetime = Field(default_factory=utc_now)


class OpenClawDocumentImportRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)
    source_uri: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1)
    normalized_markdown: str | None = None
    importer: Literal["auto", "icml_people", "apple_scholars", "generic"] = "auto"
    observed_at: datetime = Field(default_factory=utc_now)
    create_person_profiles: bool = True


class OpenClawDocumentImportResponse(StrictModel):
    command_id: str
    selected_importer: Literal["icml_people", "apple_scholars", "generic"]
    status: Literal["imported_for_review", "stored_pending_extraction", "unchanged"]
    source_version_id: str
    version_status: Literal["new_source", "new_version", "unchanged"]
    review_batch_ids: list[str] = Field(default_factory=list)
    imported_people: int = 0
    profile_import_run_id: str | None = None
    person_keys: list[str] = Field(default_factory=list)
    next_actions_zh: list[str] = Field(default_factory=list)


class OpenClawSchedulerTickRequest(StrictModel):
    command_id: str = Field(min_length=1, max_length=200)
    dry_run: bool = False
    evaluated_at: datetime = Field(default_factory=utc_now)


class OpenClawSchedulerTickResponse(StrictModel):
    command_id: str
    dry_run: bool
    subscription_count: int
    due_count: int
    enqueued_count: int
    existing_count: int
    disabled_count: int
    connector_job_ids: list[str] = Field(default_factory=list)


OPERATIONS = [
    OpenClawOperation(
        operation_id="import_feishu_document",
        title_zh="读取并导入飞书文档",
        user_phrases_zh=["导入这份飞书文档并建立跟踪", "把这个 wiki 中的人物拆分建档"],
        purpose_zh="由 OpenClaw 的 feishu_doc/feishu_wiki 工具读取内容，再把规范化 Markdown 发送到不可变原文库。",
        input_zh=["飞书 docx/wiki URL", "完整正文或规范化 Markdown", "调用者 open_id", "稳定 command_id"],
        output_zh=["SourceDocumentVersion", "导入器选择", "InitialReviewBatch", "PersonProfileImportRun"],
        endpoints=["POST /v1/openclaw/documents/import"],
        side_effect="ledger_write",
        confirmation_rule_zh="读取允许自动执行；创建档案前必须向用户展示识别人数和将生成的审核批次。",
        failure_rule_zh="无法识别结构时只保存 generic 原文并返回 stored_pending_extraction，不把相似人物强行合并。",
    ),
    OpenClawOperation(
        operation_id="review_initial_facts",
        title_zh="集中确认首次人物事实",
        user_phrases_zh=["打开待确认文档", "全部确认", "第 3 条改成……"],
        purpose_zh="把直接事实和逻辑派生分别展示，用户可按批次确认、驳回、纠正或标记歧义。",
        input_zh=["review_batch_id", "review_item_id 或自然语言范围", "actor_id", "command_id"],
        output_zh=["ReviewDeliveryPage", "InitialReviewDecision", "Annotation", "Working View 变化"],
        endpoints=[
            "GET /v1/initial-reviews/{id}/delivery",
            "POST /v1/initial-reviews/{id}/messages",
            "POST /v1/initial-reviews/{id}/bulk-decisions",
        ],
        side_effect="ledger_write",
        confirmation_rule_zh="批量确认必须先展示数量；correct 必须回显替换值；身份合并和关系派生不可静默确认。",
        failure_rule_zh="无法解析自然语言 correction 时不执行，返回可接受的示例句式。",
    ),
    OpenClawOperation(
        operation_id="maintain_search_plan",
        title_zh="维护人物搜索设计",
        user_phrases_zh=["查看这个人怎么跟踪", "把这个 X 账号加入一级账号", "暂停小红书"],
        purpose_zh="读取版本化 SearchPlan，维护已知 URL、平台稳定 ID、查询模板、频率和失败反馈。",
        input_zh=["person_key", "当前 SearchPlan revision", "拟修改目标"],
        output_zh=["新的 PersonSearchPlanRevision", "旧 revision 的 supersedes 关系"],
        endpoints=[
            "GET /v1/person-profiles/{person_key}/search-plan",
            "POST /v1/person-profiles/{person_key}/search-plan",
        ],
        side_effect="ledger_write",
        confirmation_rule_zh="新增查询可预览后写入；首次 account_owned_by、暂停全部渠道或提高频率必须明确确认。",
        failure_rule_zh="旧 revision 已变化时停止并重新读取，不覆盖并发修改。",
    ),
    OpenClawOperation(
        operation_id="scheduler_tick",
        title_zh="把到期订阅转换为幂等队列任务",
        user_phrases_zh=["运行一次跟踪调度", "检查现在有哪些人该更新"],
        purpose_zh="OpenClaw cron 每 15 分钟调用一次；后端按 tracking_key、时间桶和渠道策略生成 ConnectorJob。",
        input_zh=["command_id", "evaluated_at", "dry_run"],
        output_zh=["due/enqueued/existing 统计", "ConnectorJob IDs"],
        endpoints=["POST /v1/openclaw/scheduler/tick"],
        side_effect="ledger_write",
        confirmation_rule_zh="安装 cron 前必须展示 schedule、agent、command argv 和投递目标；普通 tick 无需逐次确认。",
        failure_rule_zh="数据库不可用或 live 模式未配置时失败，不退化到内存队列。",
    ),
    OpenClawOperation(
        operation_id="inspect_and_deliver",
        title_zh="查看运行状态并交付日报周报",
        user_phrases_zh=["查看今天新增", "查看失败渠道", "发送本周人物变化"],
        purpose_zh="读取任务、逐人增量包、待审核项和来源覆盖；按人聚合后交付飞书。",
        input_zh=["日期窗口", "Cohort/person_key", "飞书 chat/open_id"],
        output_zh=["PersonDigestBatch", "失败渠道覆盖", "证据链接和审核入口"],
        endpoints=[
            "GET /v1/connector-jobs",
            "GET /v1/person-digest-batches/latest",
            "GET /v1/person-profile-review-batches/{id}/export.md",
        ],
        side_effect="read_only",
        confirmation_rule_zh="读取和预览无需确认；发送到新的群或修改投递目标必须确认。",
        failure_rule_zh="无新信息时输出 NO_REPLY；部分渠道失败必须列出，不得伪装全覆盖。",
    ),
]


FEISHU_RULES = [
    FeishuUsageRule(
        rule_id="allowlist_first",
        title_zh="默认只允许本人私聊和指定群",
        rule_zh="OpenClaw Feishu 使用 pairing/allowlist；groupPolicy=allowlist 且 requireMention=true。未知 open_id/chat_id 不触发写操作。",
        examples_zh=["DM 首次使用先 pairing", "群聊必须 @机器人", "不使用 allowFrom='*'"],
    ),
    FeishuUsageRule(
        rule_id="read_then_preview",
        title_zh="先读取、再预览、后写入",
        rule_zh="读取飞书文档后先回复来源 URL、hash、识别人物数、导入器和预计审核项，再由用户确认建档。",
        examples_zh=["识别到 16 人、预计 73 条事实，回复“确认导入”后写入"],
    ),
    FeishuUsageRule(
        rule_id="no_secret_in_chat",
        title_zh="聊天消息不是 secret 管理器",
        rule_zh="收到 API key、Cookie 或密码时不回显、不写入文档/日志/命令参数；引导操作者在安装终端或 secret 文件中录入。",
        examples_zh=["不要把 App Secret 写进群消息", "不要把 xsec_token 放入飞书卡片"],
    ),
    FeishuUsageRule(
        rule_id="append_only_review",
        title_zh="确认与修正必须可审计",
        rule_zh="确认、驳回和 correction 均追加 Decision/Annotation；不直接修改 Assertion、档案旧版本或原文。",
        examples_zh=["第 3 条改成“2026 年 5 月加入”", "驳回所有低于 0.6 的推定"],
    ),
    FeishuUsageRule(
        rule_id="cron_preview",
        title_zh="cron 安装前展示精确行为",
        rule_zh="展示 cron 表达式、时区、exact argv、执行用户、超时、输出上限和飞书投递目标；禁止由模型拼接任意 shell。",
        examples_zh=["每 15 分钟 scheduler tick", "每日 18:00 日报", "周一 08:30 周报"],
    ),
]


SECRETS = [
    IntegrationSecretRequirement(
        secret_id="people_intel_api_token",
        required_for=["OpenClaw → People Intel API"],
        required=True,
        injection_method_zh="安装器生成 256-bit token，保存于 0600 token file；客户端只读取文件并发 Bearer header。",
        validation_method_zh="无 token/错误 token 返回 401；状态接口只返回 credential_source，不返回 token。",
        never_do_zh="不得写入 SKILL.md、openapi、压缩包、日志、URL query 或进程 argv。",
    ),
    IntegrationSecretRequirement(
        secret_id="feishu_app_secret",
        required_for=["@openclaw/feishu WebSocket"],
        required=True,
        injection_method_zh="使用 openclaw channels login --channel feishu 或目标实例的加密配置机制。",
        validation_method_zh="openclaw gateway status、feishu_app_scopes 与 allowlist/pairing 测试。",
        never_do_zh="不得复制到 People Intel 包；不得从聊天消息自动持久化。",
    ),
    IntegrationSecretRequirement(
        secret_id="gemini_api_key",
        required_for=["Cognee projection-only 抽取"],
        required=False,
        injection_method_zh="写入服务专用 0600 secrets.env 或外部 secret manager；OpenClaw 技能只看到 ready/unconfigured。",
        validation_method_zh="调用 /v1/cognee/status；不在健康响应中返回 key。",
        never_do_zh="不得把用户现有 .env 打包或复制到 OpenClaw workspace。",
    ),
    IntegrationSecretRequirement(
        secret_id="platform_credentials",
        required_for=["X、小红书、IT 桔子等登录态渠道"],
        required=False,
        injection_method_zh="优先使用各 CLI 自己的受控凭证存储；小红书使用隔离 HOME；IT 桔子只接受签约 API token。",
        validation_method_zh="每渠道执行一次低频只读 smoke，并记录 route/error_code/circuit。",
        never_do_zh="不得打包 Cookie、账号密码、Chrome Profile 或 xsec_token；不得自动绕过验证码和风控。",
    ),
]


CHANNELS = [
    ChannelDeploymentRule(channel="homepage", preferred_route="qiaomu_generic", credential_kind="none", deployment_location="either", fallback_zh="代理失败后保存 ConnectorAttempt 并下次重试。", stop_condition_zh="URL 非 http(s) 或内容验证失败。"),
    ChannelDeploymentRule(channel="arxiv", preferred_route="arxiv_api", credential_kind="none", deployment_location="either", fallback_zh="已知 arXiv ID 直读，作者 feed 空结果不删除历史。", stop_condition_zh="连续 API 错误进入渠道冷却。"),
    ChannelDeploymentRule(channel="github", preferred_route="gh_api", credential_kind="gh auth token", deployment_location="either", fallback_zh="无 token 时只使用公共限额；组织仓库继续核验 contributor/commit。", stop_condition_zh="rate limit 或 auth failure。"),
    ChannelDeploymentRule(channel="news", preferred_route="licensed_api_or_exa", credential_kind="Exa/授权聚合 API token", deployment_location="either", fallback_zh="公开搜索摘要只作 pending；取得全文后才抽事实。", stop_condition_zh="付费墙、robots、无全文或授权不足。"),
    ChannelDeploymentRule(channel="x", preferred_route="twitter_cli_known_feed", credential_kind="CLI session/cookie", deployment_location="stateful_host_worker", fallback_zh="关键词失败时 Exa 只补 URL，不能补造 author_id。", stop_condition_zh="登录失效、rate limit 或风控。"),
    ChannelDeploymentRule(channel="wechat", preferred_route="qiaomu_weixin_playwright", credential_kind="optional logged-in browser", deployment_location="stateful_host_worker", fallback_zh="搜索摘要保持 pending；已知 URL 可用 Playwright 读取。", stop_condition_zh="反爬、验证页或正文为空。"),
    ChannelDeploymentRule(channel="xhs", preferred_route="xiaohongshu_cli_readonly", credential_kind="isolated CLI cookie", deployment_location="stateful_host_worker", fallback_zh="普通 api_error 定向 search+user_id 过滤；signature_error 下一任务才允许 GUI fallback。", stop_condition_zh="verification_required/ip_blocked 立即停机等人工。"),
]


def integration_manifest(api_security: dict[str, object]) -> OpenClawIntegrationManifest:
    return OpenClawIntegrationManifest(
        integration_version=OPENCLAW_INTEGRATION_VERSION,
        minimum_openclaw_version=MIN_OPENCLAW_VERSION,
        architecture_zh=[
            "OpenClaw/Feishu 负责消息、文档读取、用户确认和 cron 触发；People Intel 负责不可变原文、人物拆分、双时态账本、队列和报告。",
            "OpenClaw 不直接写 PostgreSQL，也不让模型执行任意连接器命令；所有动作通过白名单客户端和 Bearer-authenticated API。",
            "API/PostgreSQL 可容器化；需要登录态或 GUI 的 X、微信、小红书连接器必须留在受控 host worker。",
            "OpenClaw cron 只负责精确调度 scheduler tick 与报告交付；真正渠道访问由 ConnectorJob worker 执行并服从配额、锁和熔断。",
        ],
        operations=OPERATIONS,
        feishu_rules=FEISHU_RULES,
        secrets=SECRETS,
        channels=CHANNELS,
        api_security=api_security,
        required_openclaw_components=[
            "OpenClaw >= 2026.5.29",
            "@openclaw/feishu official plugin",
            "Feishu WebSocket channel with pairing/allowlist",
            "feishu_doc + feishu_wiki + feishu_drive tools",
            "people-tracking skill",
            "cron operator.admin for installation only",
        ],
    )


__all__ = [
    "MIN_OPENCLAW_VERSION",
    "OPENCLAW_INTEGRATION_VERSION",
    "OpenClawDocumentImportRequest",
    "OpenClawDocumentImportResponse",
    "OpenClawIntegrationManifest",
    "OpenClawSchedulerTickRequest",
    "OpenClawSchedulerTickResponse",
    "integration_manifest",
]
