from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from people_intel.demo import build_memory_graph
from people_intel.connectors import ConnectorExecutor
from people_intel.events import EventBroker
from people_intel.schemas import (
    Assertion,
    AssertionObject,
    ChangeBlock,
    ChangeSet,
    CohortExampleDefinition,
    ConnectorAttempt,
    DemoStory,
    DiscoveryCandidate,
    Entity,
    EntityType,
    EpistemicType,
    EvidenceSpan,
    FetchMethod,
    GraphQuery,
    GraphView,
    KnowledgeJourney,
    KnowledgeJourneyStep,
    MemoryGraph,
    ObjectKind,
    PredicatePresentation,
    ProductFunctionDefinition,
    ReferenceFieldDefinition,
    ScanRun,
    ScheduleDefinition,
    Signal,
    SignalType,
    SourceIngestionRequest,
    SourceType,
    StorySession,
    StorySessionCreate,
    StoryStepResult,
    SourceChannelDefinition,
    TechnicalReferenceDefinition,
    TransactionTime,
    WorkflowArtifact,
    WorkflowDefinition,
    WorkflowStageDefinition,
    utc_now,
)
from people_intel.service import TemporalMemoryService


STAGES = [
    WorkflowStageDefinition(stage_id="trigger", position=1, title_zh="确定本轮要更新谁", purpose_zh="从关注名单和周期订阅中生成本轮扫描任务。", decision_zh="不是全网盲扫；根据稳定 subscription、上次入队时间和人工启停决定对象。", function_zh="把关注名单中的人物—来源配置转成可恢复的 ConnectorJob。", trigger_zh="scheduler 每 60 秒检查一次；各订阅按 1 天/3 天等独立周期到期，或用户手动立即更新。", input_zh=["ConnectorScheduleConfig", "人物与 tracking_key", "最近 ConnectorJob", "人工启停"], actions_zh=["按 tracking_key 查最近任务", "筛选本轮到期订阅", "生成时间桶 command_id", "幂等追加 ConnectorJob"], local_result_zh=["到期订阅清单", "ConnectorJob + queued 事件", "disabled/未到期统计"], aggregation_zh="所有任务进入 PostgreSQL 队列，供 worker、完成率和失败恢复统计。", exception_zh="订阅 disabled 时保留配置但不入队；同一时间桶重复 tick 返回已有任务。", sample_story_id="homepage-change", endpoint_paths=["/v1/system/connector-schedule", "/v1/connector-jobs"], schema_refs=["ConnectorScheduleConfig", "ConnectorJob"], test_refs=["test_connector_policy_scheduler.py"], event_types=["connector.job.queued"], technical_reference_ids=["ConnectorScheduleConfig", "ConnectorScheduleProducer", "ConnectorJob", "connector.job.queued", "GET /v1/system/connector-schedule", "POST /v1/connector-jobs", "test_connector_policy_scheduler.py"]),
    WorkflowStageDefinition(stage_id="plan", position=2, title_zh="为这个人制定搜索计划", purpose_zh="根据人物身份与关心的变化类型选择来源、已知 URL 和搜索词。", decision_zh="研究者优先 arXiv/GitHub/主页；创业者优先公司官网和新闻。", function_zh="将人物档案转换为本轮可执行的渠道与查询列表。", trigger_zh="ScanRun 创建后立即执行。", input_zh=["人物身份与别名", "已知主页/账号", "关注主题", "渠道能力状态"], actions_zh=["按人物类型选择渠道", "生成带别名与机构名的查询", "跳过未配置或低价值渠道"], local_result_zh=["查询词列表", "渠道选择理由", "预计抓取 URL"], aggregation_zh="同一人物各渠道查询挂在同一 ScanRun 下，后续可比较覆盖率。", exception_zh="渠道未配置时记录缺失原因，不伪造搜索结果。", sample_story_id="homepage-change", endpoint_paths=["/v1/workflow/definition"], schema_refs=["WorkflowArtifact"], test_refs=["test_product_workflow.py"], technical_reference_ids=["SourceSelectionPolicy", "WorkflowArtifact", "GET /v1/workflow/definition", "test_product_workflow.py"]),
    WorkflowStageDefinition(stage_id="discover", position=3, title_zh="执行搜索与抓取", purpose_zh="调用各信息源连接器，保留候选结果以及选择或跳过原因。", decision_zh="搜索结果只是候选来源，不在这里宣告人物事实。", function_zh="获得可进一步读取的公开页面、论文、仓库或新闻候选。", trigger_zh="搜索计划生成完成后先写 ConnectorJob；worker 通过 lease 领取，飞书回调不等待浏览器。", input_zh=["逐人 SearchPlan 与一级账号", "查询词与已知 URL", "渠道路由", "超时与输出限制", "稳定 command_id"], actions_zh=["飞书官方 WebSocket 接收中文命令并只做鉴权、幂等入队", "把 cron/飞书意图写成不可变 ConnectorJob", "GitHub 已知账号读取 profile + repos；组织仓库沿 contributors/commits 核验个人贡献", "X 已知账号读取 user + user-posts，并分开 tracked account、内容原作者和传播关系；关键词失败回退 Exa", "小红书已知 user ID 优先读取 user + user-posts；普通 API 错误时定向 search 并按已登记 user_id 过滤", "连接器补齐平台稳定 ID 时追加 SearchPlan revision，但不自动确认账号归属", "微信先发现 URL 再用 qiaomu 取全文", "小红书签名错误打开路由熔断，后续任务才走 Extension Bridge", "融资优先 IT 桔子授权 API，未签约只做公开页面发现", "失败追加 retry_scheduled，成功才写幂等回执"], local_result_zh=["CommandReceipt + connector_job_id", "ConnectorJob + ConnectorJobEvent", "ScanRun", "ConnectorAttempt（含 selected route/error_code/circuit）", "DiscoveryCandidate", "PersonPlatformAccount 候选或稳定 ID 补全", "PersonSearchPlanRevision（仅账号投影变化时）", "完整正文 SourceDocumentVersion（如已取得）", "原始响应对象引用"], aggregation_zh="所有候选按 ScanRun 与 person_key 聚合；已知账号 feed 与开放搜索分开。只有取得完整正文的 selected 候选进入版本化，摘要候选保持 pending。", exception_zh="账号搜索命中不自动升级为本人账号。转推/引用不改变被跟踪账号。小红书退化搜索只允许稳定 user_id 匹配结果进入本人增量包。签名错误只允许下一任务进入 GUI 兜底；验证码/IP 风控直接暂停等待人工处理。", sample_story_id="research-release", endpoint_paths=["/v1/person-profiles/{person_key}/platform-accounts", "/v1/person-profiles/{person_key}/search-plan", "/v1/platform-tracking/definition", "/v1/connector-jobs", "/v1/connectors/runs", "/v1/scan-runs/{scan_run_id}/attempts"], schema_refs=["PersonSearchPlanRevision", "PersonPlatformAccount", "PersonSearchTarget", "ConnectorJob", "ConnectorJobEvent", "ConnectorRunRequest", "ConnectorRunResponse", "ConnectorAttempt", "DiscoveryCandidate"], test_refs=["test_connector_jobs.py", "test_live_source_connectors.py", "test_person_profiles.py", "test_platform_adversarial.py", "test_feishu_runtime.py"], event_types=["connector.job.queued", "connector.started", "connector.completed", "source.versioned"], technical_reference_ids=["PlatformTrackingDefinition", "AdversarialEvaluationReport", "PersonSearchPlanRevision", "PersonPlatformAccount", "PersonSearchTarget", "FeishuIngress", "FeishuLongConnectionWorker", "ConnectorJob", "ConnectorJobEvent", "ConnectorRunRequest", "ConnectorRunResponse", "ConnectorAttempt", "DiscoveryCandidate", "ConnectorExecutor", "XhsApiCliTransport", "XhsApiRouteCircuit", "StatefulConnectorWorker", "connector.job.queued", "connector.started", "connector.completed", "GET /v1/platform-tracking/definition", "GET /v1/platform-tracking/adversarial-evaluation", "GET /v1/person-profiles/{person_key}/platform-accounts", "GET /v1/person-profiles/{person_key}/search-plan", "POST /v1/connector-jobs", "GET /v1/connector-jobs/{connector_job_id}/events", "POST /v1/connectors/runs", "GET /v1/scan-runs/{scan_run_id}/attempts", "test_connector_jobs.py", "test_live_source_connectors.py", "test_person_profiles.py", "test_platform_adversarial.py", "test_feishu_runtime.py"]),
    WorkflowStageDefinition(stage_id="version", position=4, title_zh="保存原文并判断是否变化", purpose_zh="规范化选中来源，并与同 URL 上一版本进行 hash 和结构化比较。", decision_zh="只有具体内容版本是证据；URL 本身不是事实。", function_zh="识别 unchanged 或产生不可变的新 SourceDocumentVersion 与 ChangeSet。", trigger_zh="候选来源被选中后执行。", input_zh=["原始响应", "规范化 Markdown", "同 URI 上一版本"], actions_zh=["内容寻址保存原文", "计算 SHA-256", "生成段落级 diff", "连接 previous_version_id"], local_result_zh=["SourceDocumentVersion", "ChangeSet", "新增/删除/修改片段"], aggregation_zh="unchanged 停止后续抽取；new_version 才进入证据抽取。", exception_zh="主页删除内容只表示页面变化，不自动关闭任职区间。", sample_story_id="homepage-change", endpoint_paths=["/v1/source-documents"], schema_refs=["SourceDocumentVersion", "ChangeSet"], test_refs=["test_content_and_sources.py"], event_types=["source.versioned", "change.detected"], technical_reference_ids=["SourceDocumentVersion", "ChangeSet", "SourceVersioningAlgorithm", "POST /v1/source-documents", "source.versioned", "change.detected", "test_content_and_sources.py"]),
    WorkflowStageDefinition(stage_id="evidence", position=5, title_zh="从变化中抽取证据与候选事实", purpose_zh="定位能够直接支持关系的原文片段，再提出实体和 Assertion。", decision_zh="没有直接证据就不建立事实关系。", function_zh="把原文变化转成可审计 EvidenceSpan 和候选实体—谓词—实体。", trigger_zh="仅对 new_source/new_version 运行。", input_zh=["变化片段", "领域本体", "人物 IdentityCluster"], actions_zh=["定位字符/段落 span", "识别实体", "只使用注册谓词", "保存抽取置信度"], local_result_zh=["EvidenceSpan", "候选 Assertion", "待确认关系或明确无事实结论"], aggregation_zh="每条 Assertion 必须引用 span；低置信或冲突关系进入待裁决队列。", exception_zh="信息缺失不是 negative Assertion；关联仓库不等于代码贡献。", sample_story_id="homepage-change", endpoint_paths=["/v1/source-documents/{source_version_id}/spans", "/v1/assertions"], schema_refs=["EvidenceSpan", "Assertion"], predicate_ids=["authored", "cofounded", "raised_funding"], test_refs=["test_temporal_memory.py"], event_types=["extraction.completed"], technical_reference_ids=["EvidenceSpan", "Assertion", "CogneeAdapter", "PredicateOntology", "POST /v1/source-documents/{source_version_id}/cognify", "extraction.completed", "test_temporal_memory.py"]),
    WorkflowStageDefinition(stage_id="resolve", position=6, title_zh="判断身份、时间与冲突", purpose_zh="确认信息属于哪个实体，并保留现实时间、获知时间和替代解释。", decision_zh="同名账号不破坏性合并；模糊月份不伪造成精确日期。", function_zh="为候选事实确定身份簇、双时态边界和冲突状态。", trigger_zh="候选 Assertion 产生后执行。", input_zh=["候选实体与 Assertion", "历史身份关系", "已有时间线"], actions_zh=["匹配或新建实体", "保留 valid_time 精度", "检测冲突", "决定人工复核"], local_result_zh=["身份匹配决定", "时间精度", "冲突与替代 Assertion 列表"], aggregation_zh="冲突长期共存；不会因新来源覆盖旧 Assertion。", exception_zh="页面内容消失不能推导 left；不确定身份使用 possibly_same_as。", sample_story_id="founder-transition", endpoint_paths=["/v1/graph/query"], schema_refs=["Assertion", "TemporalExtent"], predicate_ids=["possibly_same_as", "not_same_as"], test_refs=["test_identity_and_import.py"], technical_reference_ids=["IdentityResolutionAlgorithm", "TemporalExtent", "Assertion", "POST /v1/graph/query", "test_identity_and_import.py"]),
    WorkflowStageDefinition(stage_id="graph", position=7, title_zh="追加知识并计算当前视图", purpose_zh="把有证据的 Assertion 追加到账本，并计算指定时点的 Working View。", decision_zh="Working View 只是解释性选择，不能修改底层历史。", function_zh="更新人物完整时间线，并给日常使用提供当前状态。", trigger_zh="身份和冲突判断完成后执行。", input_zh=["已判断 Assertion", "Annotation", "valid_at/known_at", "排序策略版本"], actions_zh=["追加 Assertion", "投影局部图", "计算候选分数", "返回选中项与备选项"], local_result_zh=["局部图增量", "完整时间线新增项", "Working View 与选择理由"], aggregation_zh="人物级 Working View 供信号规则与飞书人物页读取。", exception_zh="冲突不隐藏；低置信内容只进入完整时间线。", sample_story_id="founder-transition", endpoint_paths=["/v1/entities/{entity_id}/working-view", "/v1/demo/story-sessions/{session_id}/graph-delta"], schema_refs=["MemoryGraph", "WorkingView"], predicate_ids=["authored", "cofounded", "raised_funding"], test_refs=["test_temporal_memory.py"], event_types=["assertion.appended"], technical_reference_ids=["MemoryArchitectureDefinition", "KnowledgeJourney", "WorkingView", "WorkingViewRanking", "MemoryGraph", "GET /v1/memory/architecture", "GET /v1/demo/story-sessions/{session_id}/knowledge-journey", "assertion.appended", "GET /v1/entities/{entity_id}/working-view", "test_memory_architecture.py", "test_temporal_memory.py"]),
    WorkflowStageDefinition(stage_id="deliver", position=8, title_zh="聚合信号并生成集中报告", purpose_zh="将本轮人物级变化汇入日报、周报、冲突队列和候选池。", decision_zh="只报告有证据的变化；信号不是投资结论。", function_zh="把分散的局部扫描结果整理成用户可消费的集中反馈。", trigger_zh="单个 ScanRun 完成时更新聚合；每日 18:00 和周一 08:30集中发送。", input_zh=["新增 Assertion", "ChangeSet", "冲突", "ConnectorAttempt", "图路径"], actions_zh=["运行图信号规则", "计算 0–100 重要性", "按人物和主题去重", "生成飞书摘要与可追溯链接"], local_result_zh=["Signal", "日报/周报条目", "待裁决项", "来源覆盖与失败统计"], aggregation_zh="日报按分数和新鲜度排序；周报汇总图谱增量、冲突、候选人与成本。", exception_zh="仅网页变化使用 homepage_change；无证据不升级为职业变化。", sample_story_id="funding-news", endpoint_paths=["/v1/signals"], schema_refs=["Signal", "WorkflowArtifact"], test_refs=["test_scheduler_signals_commands.py"], event_types=["signal.emitted", "delivery.previewed"], technical_reference_ids=["Signal", "GraphSignalEngine", "DailyDigestAggregation", "signal.emitted", "delivery.previewed", "GET /v1/signals", "test_scheduler_signals_commands.py"]),
]


PREDICATE_PRESENTATIONS = [
    PredicatePresentation(predicate_id="profile_claim", label_zh="待结构化履历陈述", description_zh="把人物履历中的每个原文句子先变成可独立审核的 literal Assertion，避免未映射内容只藏在段落里。", example_zh="Zanlin Ni profile_claim ‘现为清华大学 LEAP Lab 在读博士生。’"),
    PredicatePresentation(predicate_id="authored", label_zh="创作论文", description_zh="人物是论文作者。", example_zh="倪赞霖 authored The Flexibility Trap"),
    PredicatePresentation(predicate_id="coauthored_with", label_zh="共同作者", description_zh="两个人分别拥有指向同一论文的 authored 关系后，由规则推定；必须检查两条输入事实和推理规则。", example_zh="倪赞霖与王慎执均 authored 同一论文 → coauthored_with"),
    PredicatePresentation(predicate_id="contributed_to", label_zh="参与项目", description_zh="人物对项目、仓库或论文有直接贡献。", example_zh="只有项目页明确列出贡献者时才建立"),
    PredicatePresentation(predicate_id="cofounded", label_zh="共同创办", description_zh="人物共同创立公司或项目。", example_zh="Ilya Sutskever cofounded SSI"),
    PredicatePresentation(predicate_id="raised_funding", label_zh="完成融资", description_zh="公司或项目与一轮融资建立关系。", example_zh="SSI raised_funding 2024 FundingRound"),
    PredicatePresentation(predicate_id="possibly_same_as", label_zh="可能同一身份", description_zh="跨平台身份尚未完全确认。", example_zh="主页作者可能与 GitHub 账号相同"),
    PredicatePresentation(predicate_id="not_same_as", label_zh="确认不是同一身份", description_zh="撤回错误身份聚合而不删除历史。", example_zh="同名账号经人工核验后拆分"),
]


PRODUCT_FUNCTIONS = [
    ProductFunctionDefinition(function_id="mark", title_zh="导入、确认并维护关注名单", user_goal_zh="从文件或消息中提取人物，同时检查直接事实与基于这些事实产生的逻辑推定。", input_zh="名单、PDF、Markdown、表格、飞书消息或 API；以及主页/账号、关注原因和 Tier", action_zh="保存完整原文，把人物段落拆成 EvidenceSpan，生成直接 Assertion 与可解释 Derived Assertion，再通过网页/飞书卡片或消息指令完成人工确认", output_zh="一份每条事实和推定都有证据、依赖、裁决状态且可持续监控的人物知识库"),
    ProductFunctionDefinition(function_id="monitor", title_zh="定时发现人物动态", user_goal_zh="不逐个手工检查主页、论文、代码和新闻。", input_zh="到期人物、历史来源版本、连接器能力", action_zh="按 cron 创建 ScanRun，搜索、抓取、比较版本并抽取证据", output_zh="人物级变化、候选事实、冲突和来源覆盖记录"),
    ProductFunctionDefinition(function_id="report", title_zh="集中反馈值得看的变化", user_goal_zh="在固定时间只看新增、高价值和需要裁决的信息。", input_zh="所有 ScanRun 的 Assertion、ChangeSet、Signal 与失败", action_zh="跨人物去重、排序、聚类，并生成日报/周报", output_zh="飞书集中报告、人物时间线、待裁决队列和候选池"),
]


COHORT_EXAMPLES = [
    CohortExampleDefinition(person_name="Zanlin Ni", tier="A", reason_zh="持续跟踪强化学习、视觉推理的新论文与开源项目", channels=["homepage", "arxiv", "github"], scan_frequency_zh="每日增量 + 每周完整", next_action_zh="检查新论文、项目页与仓库贡献证据"),
    CohortExampleDefinition(person_name="Yang Yue", tier="B", reason_zh="跟踪个人主页中的研究方向与工作经历变化", channels=["homepage", "github"], scan_frequency_zh="每周完整", next_action_zh="比较主页仓库 commit 和线上版本"),
    CohortExampleDefinition(person_name="Ilya Sutskever", tier="A", reason_zh="跟踪研究高管的创业、角色和团队变化", channels=["homepage", "news"], scan_frequency_zh="每日增量 + 新闻触发", next_action_zh="交叉验证公司官网与权威媒体"),
    CohortExampleDefinition(person_name="Safe Superintelligence", tier="A", reason_zh="跟踪融资、投资方和团队扩张", channels=["homepage", "news"], scan_frequency_zh="每日增量", next_action_zh="识别 FundingRound 并保留来源差异"),
]


SCHEDULES = [
    ScheduleDefinition(schedule_id="tier-a-scan", title_zh="Tier A 增量扫描", cron="0 9 * * *", target_zh="最高关注人物与近期高分信号相关人物", actions_zh=["检查主页/arXiv/GitHub/新闻增量", "失败渠道独立重试", "不发送即时噪声"], output_zh="人物级 ScanRun 与局部变化"),
    ScheduleDefinition(schedule_id="weekly-full-scan", title_zh="全体完整扫描", cron="0 2 * * 0", target_zh="所有 active Cohort 人物", actions_zh=["补齐低频来源", "重新检查身份链接", "统计来源覆盖率"], output_zh="一周完整扫描批次"),
    ScheduleDefinition(schedule_id="daily-digest", title_zh="每日集中报告", cron="0 18 * * *", target_zh="过去 24 小时完成的 ScanRun", actions_zh=["按重要性与新鲜度排序", "同人同事件合并", "附证据、冲突和失败渠道"], output_zh="一条飞书日报"),
    ScheduleDefinition(schedule_id="weekly-digest", title_zh="每周图谱报告", cron="30 8 * * 1", target_zh="过去 7 天全体人物与候选图", actions_zh=["汇总实体/关系增量", "列出冲突与待裁决", "统计候选人物、覆盖率和成本"], output_zh="一条飞书周报"),
]


SOURCE_CHANNELS = [
    SourceChannelDefinition(channel="homepage", title_zh="个人主页/项目页", discovery_method_zh="从人物档案读取已知 URL，并定期重新抓取。", normalization_method_zh="qiaomu 转为 Markdown；保留原始响应与 SHA-256。", use_for_zh="任职表述、研究方向、项目和联系方式变化。", selection_rule_zh="页面删除只记 ChangeSet，不能自动推断离职。"),
    SourceChannelDefinition(channel="arxiv", title_zh="arXiv", discovery_method_zh="作者名、机构和主题组合查询 arXiv API。", normalization_method_zh="解析 Atom 元数据并保存 API 原始响应。", use_for_zh="新论文、版本更新和作者关系。", selection_rule_zh="作者身份必须与主页/机构等证据共同消歧。"),
    SourceChannelDefinition(channel="github", title_zh="GitHub", discovery_method_zh="已知账号直读 profile/repos；组织项目沿来源 URL、contributors、commits 与 PR 分层回溯。", normalization_method_zh="保存 JSON 响应、稳定 database/node ID、仓库元数据和 commit 引用。", use_for_zh="新仓库、release、维护与明确贡献。", selection_rule_zh="发现关联仓库不等于 contributed_to；至少需要 contributor/commit/PR 账号证据。"),
    SourceChannelDefinition(channel="news", title_zh="融资数据库 / 新闻", discovery_method_zh="生产优先调用已签约 IT 桔子数据 API；未配置时由 Exa 仅检索 itjuzi.com 公开页面，也可抓取人工选中的原文 URL。", normalization_method_zh="授权记录保存原始 JSON；公开网页经 qiaomu 规范化并独立版本化。", use_for_zh="创业、人事、融资和产品发布。", selection_rule_zh="公开搜索只是候选发现；金额、日期和投资方按来源分别保留，不把聚合商记录当作最终权威。"),
    SourceChannelDefinition(channel="x", title_zh="X", discovery_method_zh="已知账号先读 user profile 再读 user-posts；单帖读取 tweet；关键词检索失败时回退 Exa site:x.com。", normalization_method_zh="原始 JSON 进入对象库；每条内容分开保存 tracked account、原作者与 authored/retweeted/quoted/replied 关系。", use_for_zh="本人即时发布、传播动作、项目和角色线索。", selection_rule_zh="转推不能改写一级账号或事实主语；帖子陈述标注 reported，关键词摘要不直接抽取事实。"),
    SourceChannelDefinition(channel="wechat", title_zh="微信公众号", discovery_method_zh="Exa 发现 mp.weixin.qq.com 候选 URL，已知 URL 使用 qiaomu fetch_weixin.py 获取全文。", normalization_method_zh="标题、作者、发布时间与正文转 Markdown，同时保存抓取 JSON。", use_for_zh="中文访谈、产品与融资报道。", selection_rule_zh="搜索摘要保持 pending；只有全文取得后才能进入 EvidenceSpan 抽取。"),
    SourceChannelDefinition(channel="xhs", title_zh="小红书", discovery_method_zh="worker 优先调用隔离 HOME 的 xiaohongshu-cli；已知账号 user/user-posts 普通错误时定向 search 并按登记 user_id 过滤；签名错误熔断后下一任务才用 Extension Bridge。", normalization_method_zh="候选、正文、稳定 user_id、时间和互动指标形成脱敏投影；nickname 与 rank 不作为身份锚点，xsec_token 仅留在受控原始对象。", use_for_zh="本人动态和中文社区早期线索。", selection_rule_zh="已知账号 fallback 结果的 author.user_id 必须精确匹配；signature_error 可进入 GUI 兜底，verification_required/ip_blocked 必须停机等待人工。"),
]


def _field(name: str, meaning: str) -> ReferenceFieldDefinition:
    return ReferenceFieldDefinition(name=name, meaning_zh=meaning)


TECHNICAL_REFERENCES = [
    TechnicalReferenceDefinition(reference_id="ScanRun", kind="schema", title_zh="一次人物扫描的运行账本", summary_zh="记录为什么扫描某人、运行了哪些渠道、处于什么状态；它是运维审计对象，不是人物事实。", used_when_zh="某个人到达 cron 时间窗或被手动要求立即更新时创建。", fields=[_field("scan_run_id", "贯穿本轮所有连接器、候选和 artifact 的关联 ID"), _field("story_id", "演示样本标识；生产中对应扫描策略/批次"), _field("trigger", "cron、手动触发或上游事件"), _field("mode", "replay 或只写 Sandbox 的 live"), _field("channels", "本轮计划尝试的渠道快照"), _field("status", "running/completed/partial/failed")], related_reference_ids=["ConnectorAttempt", "scan.started", "StorySession"], source_ref="src/people_intel/schemas.py::ScanRun"),
    TechnicalReferenceDefinition(reference_id="StorySession", kind="schema", title_zh="演示隔离会话", summary_zh="控制某个公开样本的回放进度，并把产品页和技术页保持在同一上下文。它只用于演示，不进入正式人物知识。", used_when_zh="打开产品演示、切换故事或重播时创建。", fields=[_field("session_id", "前端深链上下文"), _field("scan_run_id", "对应的真实运行账本"), _field("current_stage", "已经幂等完成到第几步")], related_reference_ids=["ScanRun", "POST /v1/demo/story-sessions"], source_ref="src/people_intel/schemas.py::StorySession"),
    TechnicalReferenceDefinition(reference_id="ConnectorAttempt", kind="schema", title_zh="一次渠道调用记录", summary_zh="保存渠道路由、脱敏请求、耗时、状态、错误和原始响应对象引用；渠道失败不会被静默吞掉。", used_when_zh="每个 ScanRun 对每个计划渠道最多追加一次或多次重试记录。", fields=[_field("channel", "github/arxiv/homepage/news 等"), _field("route", "实际使用的执行器；小红书可区分 xhs_api_cli 与 xhs_extension_bridge_fallback"), _field("status", "completed/failed/unconfigured"), _field("raw_object_ref", "原始响应在内容寻址库中的位置"), _field("error", "可展示的降级原因"), _field("metadata.error_code/circuit", "结构化说明签名、风控或认证错误以及下一任务是否允许兜底")], related_reference_ids=["ConnectorExecutor", "XhsApiRouteCircuit", "DiscoveryCandidate"], source_ref="src/people_intel/schemas.py::ConnectorAttempt"),
    TechnicalReferenceDefinition(reference_id="ConnectorRunRequest", kind="schema", title_zh="统一来源扫描命令", summary_zh="把手动、cron 或飞书触发统一成一个白名单渠道请求；稳定 command_id 保证重复投递不重复访问外部来源。", used_when_zh="需要刷新已知主页、账号 feed、GitHub/arXiv，或执行 X、微信、小红书、新闻搜索时。", fields=[_field("command_id", "跨重试保持不变的幂等键"), _field("channel", "仅允许 homepage/github/arxiv/x/wechat/xhs/news"), _field("query/source_uri", "发现查询或已选中的原文 URL"), _field("person_key", "把这次扫描严格归属于哪个人物"), _field("query_kind", "known_account_feed/account_discovery/person_mentions 等动作语义"), _field("platform_account_key", "已知账号刷新时回链版本化账号记录"), _field("trigger", "manual/cron/feishu"), _field("max_results", "受限结果数量")], related_reference_ids=["PersonSearchTarget", "PersonPlatformAccount", "ScanRun", "ConnectorRunResponse", "POST /v1/connectors/runs"], source_ref="src/people_intel/schemas.py::ConnectorRunRequest"),
    TechnicalReferenceDefinition(reference_id="ConnectorRunResponse", kind="schema", title_zh="一次来源扫描的审计结果", summary_zh="同时返回运行、渠道尝试、发现候选和已经取得全文的原文版本；不会把仅有摘要的候选伪装成事实来源。", used_when_zh="ConnectorExecutor 完成或明确降级后。", fields=[_field("scan_run", "本轮运行与触发信息"), _field("attempt", "真实路由、状态、耗时与原始响应引用"), _field("candidates", "候选 URL、排名与 pending/selected 原因"), _field("source_version_ids", "仅完整正文对应的不可变版本"), _field("metadata.account_plan_revision_id", "平台账号 ID/候选回写后产生的新 SearchPlan revision；投影失败只在这里报错")], related_reference_ids=["ConnectorRunRequest", "ConnectorAttempt", "DiscoveryCandidate", "SourceDocumentVersion", "PersonSearchPlanRevision"], source_ref="src/people_intel/schemas.py::ConnectorRunResponse"),
    TechnicalReferenceDefinition(reference_id="PersonSearchPlanRevision", kind="schema", title_zh="逐人版本化搜索设计", summary_zh="把一个人物的已知网址、一级账号、开放查询、执行顺序、频率与身份门固化为可追溯版本。", used_when_zh="人物首次建档、账号 ID 被补齐、候选账号出现或人工修改查询策略时。", fields=[_field("platform_accounts", "X/XHS/GitHub 一级账号候选与确认记录"), _field("targets", "known_account_feed、account_discovery、person_mentions 等有序动作"), _field("platform_algorithm_version", "当前三平台算法版本"), _field("supersedes_revision_id", "上一版搜索计划")], related_reference_ids=["PersonPlatformAccount", "PersonSearchTarget", "ConnectorRunRequest"], source_ref="src/people_intel/person_profile_schemas.py::PersonSearchPlanRevision"),
    TechnicalReferenceDefinition(reference_id="PersonPlatformAccount", kind="schema", title_zh="人物一级账号记录", summary_zh="记录人物本人在 X、小红书或 GitHub 的账号候选/确认关系，优先保存平台稳定 ID，同时保留用户名历史所需的观测证据。", used_when_zh="从已知主页 URL 建档，或 connector profile/search 返回账号标识时。", fields=[_field("platform_user_id", "平台稳定 numeric/database ID；优先于可变用户名"), _field("username/profile_url", "当前可读标识和直达地址"), _field("identity_status", "candidate/pending_review/confirmed/rejected/conflicted"), _field("discovery_method", "来源 URL、profile connector、开放搜索或人工"), _field("source_version_ids/evidence_span_ids", "账号归属的可审计依据"), _field("first_seen_at/last_observed_at", "账号记录的观测边界")], related_reference_ids=["PersonSearchPlanRevision", "PersonSearchTarget", "IdentityResolutionAlgorithm"], source_ref="src/people_intel/person_profile_schemas.py::PersonPlatformAccount"),
    TechnicalReferenceDefinition(reference_id="PersonSearchTarget", kind="schema", title_zh="单个平台追踪动作", summary_zh="明确一次动作是刷新已知账号、寻找候选账号、找人物提及还是仓库/事件发现，并声明需要采集和比较的 checkpoint。", used_when_zh="SearchPlan 被 scheduler 展开成 ConnectorJob 时。", fields=[_field("query_kind", "动作语义，防止把开放搜索当账号 feed"), _field("platform_account_key", "直读账号时必须回链的账号记录"), _field("capture_fields", "平台响应要保留的实质字段"), _field("checkpoint_fields", "下一轮判断增量的稳定边界"), _field("identity_gate_zh", "候选升级前的身份验证要求")], related_reference_ids=["PersonSearchPlanRevision", "PersonPlatformAccount", "ConnectorRunRequest"], source_ref="src/people_intel/person_profile_schemas.py::PersonSearchTarget"),
    TechnicalReferenceDefinition(reference_id="ConnectorJob", kind="schema", title_zh="等待执行的持久来源任务", summary_zh="保存一次 cron、飞书或人工扫描意图；任务本身不可修改，当前状态从后续事件计算。", used_when_zh="耗时来源扫描需要跨进程执行、失败重试或 worker 重启恢复时。", fields=[_field("connector_job_id", "查询状态和事件史的稳定任务 ID"), _field("command_id", "跨飞书重投与 worker 重试保持不变的幂等键"), _field("request", "具体渠道、query/source_uri 与触发来源"), _field("max_attempts", "到达此次数后进入 dead letter"), _field("not_before", "最早可领取时间，用于 cron 与延迟执行")], related_reference_ids=["ConnectorJobEvent", "ConnectorRunRequest", "StatefulConnectorWorker"], source_ref="src/people_intel/schemas.py::ConnectorJob"),
    TechnicalReferenceDefinition(reference_id="ConnectorJobEvent", kind="schema", title_zh="任务状态的追加式事件", summary_zh="queued、claimed、deferred、retry_scheduled、succeeded、dead_lettered 每次都是新记录，不覆盖旧状态。", used_when_zh="任务入队、worker 领取、策略延迟、失败退避、完成或耗尽重试时。", fields=[_field("event_type", "本次状态转移"), _field("worker_id", "哪一个 worker 领取或完成"), _field("attempt", "第几次真实执行"), _field("lease_expires_at", "worker 崩溃后允许接管的时间"), _field("next_attempt_at", "策略延迟或指数退避后的下次可运行时间"), _field("error/result", "延迟/失败原因或成功产物 ID")], related_reference_ids=["ConnectorJob", "ConnectorAttempt"], source_ref="src/people_intel/schemas.py::ConnectorJobEvent"),
    TechnicalReferenceDefinition(reference_id="ConnectorScheduleConfig", kind="schema", title_zh="人物—来源周期订阅", summary_zh="把需要长期 mark 的人、渠道、查询词、周期和单次结果上限固化成版本化配置；scheduler tick 只为到期订阅创建任务。", used_when_zh="本机启动、cron tick 或修改关注名单时。", fields=[_field("subscription_id", "跨迭代稳定的跟踪键"), _field("subject_name/entity_id", "这条查询服务于哪个人物或主题"), _field("channel/query/source_uri", "执行渠道和实际查询"), _field("interval_minutes", "最短再次入队周期"), _field("max_attempts", "失败恢复上限"), _field("enabled", "暂停时保留配置但不创建新任务")], related_reference_ids=["ConnectorJob", "ConnectorScheduleProducer"], source_ref="config/connector-schedule.json"),
    TechnicalReferenceDefinition(reference_id="ConnectorPolicyDefinition", kind="schema", title_zh="渠道运行保护参数", summary_zh="按渠道声明最小间隔、每日上限、连续失败阈值、熔断时间和是否必须串行。", used_when_zh="worker claim 后、真实访问平台前。", fields=[_field("min_interval_seconds", "两次真实访问的最小间隔"), _field("daily_attempt_limit", "Asia/Shanghai 自然日内访问上限"), _field("circuit_failure_threshold", "连续失败多少次打开熔断"), _field("circuit_cooldown_seconds", "暂停多久再探测"), _field("serialized", "是否还需要跨进程账号锁")], related_reference_ids=["ConnectorExecutionPolicy", "StatefulConnectorWorker"], source_ref="src/people_intel/connector_policy.py"),
    TechnicalReferenceDefinition(reference_id="DiscoveryCandidate", kind="schema", title_zh="搜索发现候选", summary_zh="一条搜索结果及其排名、URL、selected/skipped 决定和理由；它不是 Assertion。", used_when_zh="连接器返回搜索结果后、内容版本化之前。", fields=[_field("rank", "原搜索排序"), _field("decision", "selected/skipped/pending"), _field("reason_zh", "选择或跳过的产品理由")], related_reference_ids=["ConnectorAttempt", "SourceDocumentVersion"], source_ref="src/people_intel/schemas.py::DiscoveryCandidate"),
    TechnicalReferenceDefinition(reference_id="SourceDocumentVersion", kind="schema", title_zh="不可变原文版本", summary_zh="同一 URL 每次内容变化都产生新版本；带 hash 的内容而非 URL 才是权威证据。", used_when_zh="抓取或导入任何公开来源后。", fields=[_field("content_hash", "原始内容 SHA-256"), _field("raw_object_ref", "内容寻址对象"), _field("previous_version_id", "同 URI 的上一版本"), _field("retrieved_at", "系统何时抓取")], related_reference_ids=["ChangeSet", "EvidenceSpan"], source_ref="src/people_intel/schemas.py::SourceDocumentVersion"),
    TechnicalReferenceDefinition(reference_id="ChangeSet", kind="schema", title_zh="来源版本差异", summary_zh="描述旧/新 SourceVersion 之间新增、删除和修改的片段；页面变化不等于职业事实。", used_when_zh="同一 URI 的 content_hash 变化时。", fields=[_field("version_status", "new_source/new_version/unchanged"), _field("blocks", "结构化变化块")], related_reference_ids=["SourceDocumentVersion", "SourceVersioningAlgorithm"], source_ref="src/people_intel/schemas.py::ChangeSet"),
    TechnicalReferenceDefinition(reference_id="EvidenceSpan", kind="schema", title_zh="可定位原文证据", summary_zh="把 Assertion 锚定到具体页码、段落、DOM、JSON Pointer 或字符范围。", used_when_zh="抽取器准备提出任何事实关系前。", fields=[_field("source_version_id", "证据所在的不可变原文版本"), _field("locator", "在规范化原文中的精确位置"), _field("quote_hash", "短摘录完整性校验")], related_reference_ids=["SourceDocumentVersion", "Assertion"], source_ref="src/people_intel/schemas.py::EvidenceSpan"),
    TechnicalReferenceDefinition(reference_id="Assertion", kind="schema", title_zh="最小可审计事实单元", summary_zh="一个带证据、双时态、认识论类型和置信度的实体—谓词—实体/字面量陈述，只追加不覆盖。", used_when_zh="原文 span 直接支持注册谓词关系时。", fields=[_field("subject_entity_id", "主语实体"), _field("predicate_id", "版本化本体谓词"), _field("object", "实体或类型化字面量"), _field("valid_time", "现实世界何时成立"), _field("transaction_time", "系统何时获知"), _field("evidence_span_ids", "直接来源证据")], related_reference_ids=["EvidenceSpan", "TemporalExtent", "WorkingView"], source_ref="src/people_intel/schemas.py::Assertion"),
    TechnicalReferenceDefinition(reference_id="TemporalExtent", kind="schema", title_zh="不确定有效时间区间", summary_zh="用 earliest/latest 和 precision 保存月、季度、年份等真实精度，不伪造日级时间。", used_when_zh="Assertion 描述任职、加入、融资等具有现实时间的关系。", fields=[_field("start/end", "区间边界"), _field("precision", "second 到 unknown"), _field("interval_type", "instant/open_end 等")], related_reference_ids=["Assertion", "WorkingView"], source_ref="src/people_intel/schemas.py::TemporalExtent"),
    TechnicalReferenceDefinition(reference_id="WorkingView", kind="schema", title_zh="指定双时态的当前工作视图", summary_zh="在 valid_at/known_at 下按版本化策略选择最可信 Assertion，同时返回备选项与选择理由。", used_when_zh="人物页、日报或查询需要“截至某时系统认为是什么”时。", fields=[_field("selected", "当前被选中的 Assertion"), _field("alternatives", "冲突或低排序的替代项"), _field("policy_version", "可重算的选择算法版本")], related_reference_ids=["Assertion", "WorkingViewRanking"], source_ref="src/people_intel/schemas.py::WorkingView"),
    TechnicalReferenceDefinition(reference_id="MemoryGraph", kind="schema", title_zh="局部记忆图投影", summary_zh="Entity → Assertion → Entity/Literal → EvidenceSpan → SourceVersion 的可重建展示，不是权威账本。", used_when_zh="解释本次扫描到底向人物记忆增加了什么。", fields=[_field("nodes", "实体、Assertion、证据、原文和字面量"), _field("edges", "角色、证据与来源连接")], related_reference_ids=["Assertion", "EvidenceSpan"], source_ref="src/people_intel/schemas.py::MemoryGraph"),
    TechnicalReferenceDefinition(reference_id="Signal", kind="schema", title_zh="可解释关注信号", summary_zh="由新增图结构触发的 0–100 重要性条目，必须保留触发 Assertion、证据与规则版本；不是投资结论。", used_when_zh="新论文、创业、融资、角色冲突或仅主页变化发生时。", fields=[_field("signal_type", "publication/funding/homepage_change 等"), _field("score", "重要性而非事实概率"), _field("triggering_assertion_ids", "触发关系"), _field("graph_path", "可解释图路径"), _field("rule_version", "可重算规则版本")], related_reference_ids=["GraphSignalEngine", "DailyDigestAggregation"], source_ref="src/people_intel/schemas.py::Signal"),
    TechnicalReferenceDefinition(reference_id="WorkflowArtifact", kind="schema", title_zh="演示中间产物投影", summary_zh="把查询、搜索结果、diff、证据和报告预览整理成前端可展示对象；可从运行账本重建，不是权威事实。", used_when_zh="产品演示需要展示某一步的实质局部结果时。", fields=[_field("stage_id", "对应流程步骤"), _field("kind", "renderer 类型"), _field("payload", "可重建展示数据"), _field("*_ids", "回链权威对象的 ID")], related_reference_ids=["ScanRun", "StorySession"], source_ref="src/people_intel/schemas.py::WorkflowArtifact"),
    TechnicalReferenceDefinition(reference_id="MemoryArchitectureDefinition", kind="schema", title_zh="知识库分层与写入契约", summary_zh="定义权威层、可重建投影、Cognee 边界、标准知识单元和七步写入路径；知识库页面只渲染这份后端契约。", used_when_zh="解释当前知识库是什么、各组件保存什么以及新信息怎样进入时。", fields=[_field("layers", "每层职责、权威性和重建来源"), _field("cognee", "实际 remember/recall 调用与权限边界"), _field("write_path", "从原文到 Signal 的标准路径")], related_reference_ids=["MemoryGraph", "CogneeAdapter", "KnowledgeJourney"], source_ref="src/people_intel/memory_architecture.py::MEMORY_ARCHITECTURE"),
    TechnicalReferenceDefinition(reference_id="KnowledgeJourney", kind="schema", title_zh="一次更新进入知识库的实况", summary_zh="把当前 StorySession 的 SourceVersion、EvidenceSpan、Assertion、图增量和 Signal 按写入路径对齐，并明确 skipped/unconfigured。", used_when_zh="知识库页面选择一个 sample 后。", fields=[_field("steps", "每一步的真实状态、结果和对象 ID"), _field("assertion_created", "这次变化是否真正形成事实"), _field("stop_reason_zh", "没有形成 Assertion 时停在哪个证据边界"), _field("graph_delta", "本次新增的可追溯局部图")], related_reference_ids=["StorySession", "MemoryArchitectureDefinition", "MemoryGraph"], source_ref="src/people_intel/schemas.py::KnowledgeJourney"),
    TechnicalReferenceDefinition(reference_id="InitialReviewBatch", kind="schema", title_zh="首次建档人工确认批次", summary_zh="把一次文件或消息导入产生的候选 Assertion、证据与处理进度组织成独立审核任务；批次状态由追加决策计算，不覆盖原始抽取。", used_when_zh="新人物或初始背景第一次进入系统时。", fields=[_field("source_kind/source_ref", "批次来自哪个导入或演示会话"), _field("source_version_ids", "原始输入版本"), _field("items", "逐条 Assertion、主体、对象和证据"), _field("counts/status", "由 ReviewDecision 重算的进度")], related_reference_ids=["Assertion", "InitialReviewDecision", "HumanInitialProfileGate"], source_ref="src/people_intel/schemas.py::InitialReviewBatch"),
    TechnicalReferenceDefinition(reference_id="ConsolidatedReviewDocument", kind="schema", title_zh="按人物聚合的集中审核文档", summary_zh="把底层细粒度 ReviewItem 按人物重新组织成一份连续文档；账号关系归回账号所属人物，避免数百张独立卡片。它只是可重建审核投影。", used_when_zh="一个名单或飞书资料文档拆出大量首次建档事实后。", fields=[_field("groups", "按人物组织的候选事实章节"), _field("sequences", "仍对应批次中的稳定全局序号"), _field("pending_count", "人物章节内尚未裁决数量"), _field("export_markdown_url", "完整外部审核文档")], related_reference_ids=["InitialReviewBatch", "ReviewBulkDecision", "ReviewMessageCommand"], source_ref="src/people_intel/service.py::consolidated_review_document"),
    TechnicalReferenceDefinition(reference_id="ReviewBulkDecision", kind="algorithm", title_zh="集中审批的幂等批量裁决", summary_zh="一次确认全部待审项、指定人物或指定条目；内部仍逐条追加 ReviewDecision 与 Annotation，不修改 Assertion。", used_when_zh="用户通读完整文档后点击批准全部或批准某个人。", behavior_zh=["command_id 幂等", "只处理当前 pending 项", "人物 scope 包含归属于该人的账号关系", "返回全部受影响 ID 供飞书反馈"], related_reference_ids=["ConsolidatedReviewDocument", "InitialReviewDecision"], source_ref="src/people_intel/service.py::bulk_decide_initial_review"),
    TechnicalReferenceDefinition(reference_id="DirectorySourceTracker", kind="algorithm", title_zh="资料目录与附件版本追踪器", summary_zh="对目录中正文、图片和其他附件逐文件计算 SHA-256，并把稳定目录清单作为 SourceDocumentVersion；删除只记录路径变化，不推导人物事实。", used_when_zh="本地知识包初次导入以及后续定时检查时。", behavior_zh=["每个文件进入内容寻址对象库", "清单相同返回 unchanged", "文件变化追加新 manifest 版本", "正文继续进入同一首次建档审核链"], related_reference_ids=["SourceDocumentVersion", "ConsolidatedReviewDocument"], source_ref="src/people_intel/source_tracking.py::DirectorySourceTracker"),
    TechnicalReferenceDefinition(reference_id="InitialReviewDecision", kind="schema", title_zh="首次事实的人类反馈", summary_zh="确认、驳回、修正或标记歧义均追加 ReviewDecision 与 Annotation；旧 Assertion 永不原地修改。", used_when_zh="用户在首次建档页处理一个候选事实时。", fields=[_field("action", "confirm/reject/correct/mark_ambiguous"), _field("annotation_id", "底层裁决账本引用"), _field("replacement_assertion_id", "correct 时新建的替代事实")], related_reference_ids=["InitialReviewBatch", "Assertion"], source_ref="src/people_intel/schemas.py::InitialReviewDecision"),
    TechnicalReferenceDefinition(reference_id="ReviewDeliveryPage", kind="schema", title_zh="跨媒介审核卡片页", summary_zh="把大批量 ReviewItem 分页转换成网页、飞书或 API 均可渲染的事实卡；每张卡都带问题、证据、推理链、按钮 postback 和可复制消息。", used_when_zh="一次文件导入产生几十条直接事实与逻辑推定时。", fields=[_field("cards", "本页事实卡与稳定序号"), _field("actions", "确认/驳回/歧义按钮及后端 postback"), _field("inference_chain_zh", "Derived Assertion 的输入事实和规则"), _field("message_examples_zh", "用户可直接输入的自然语言指令")], related_reference_ids=["InitialReviewBatch", "InitialReviewDecision", "DerivedInferenceGate"], source_ref="src/people_intel/schemas.py::ReviewDeliveryPage"),
    TechnicalReferenceDefinition(reference_id="ReviewMessageCommand", kind="algorithm", title_zh="审核消息确定性解析器", summary_zh="识别确认、批量确认、驳回、歧义和结构化纠错句式；无法识别时明确拒绝，不让模型猜测用户的修改意图。", used_when_zh="用户在飞书私聊、群聊或网页输入框中回复审核任务时。", behavior_zh=["command_id 幂等", "序号映射到稳定 ReviewItem", "纠错追加 replacement Assertion 与 supersedes", "未识别文本不改变账本"], related_reference_ids=["ReviewDeliveryPage", "InitialReviewDecision"], source_ref="src/people_intel/service.py::apply_initial_review_message"),
    TechnicalReferenceDefinition(reference_id="ConnectorExecutor", kind="algorithm", title_zh="账号优先的白名单连接器执行器", summary_zh="为 GitHub、X、小红书、微信与融资来源选择真实传输层；已知一级账号直读 profile/feed，开放搜索只产生候选。所有命令使用参数数组，并保留原始响应。", used_when_zh="Sandbox 或生产 connector worker 收到统一来源扫描命令时。", behavior_zh=["GitHub 已知账号读取 users/{login} 与按 pushed 排序的 repos，并保存 numeric/node ID", "X 已知账号先读取 user profile 再读 user-posts；关键词失败回退 Exa", "小红书已知 user ID 读取 user + user-posts；开放 search 只生成候选", "小红书签名错误仅允许后续任务使用 Extension Bridge；验证码/IP block 禁止自动 GUI 兜底", "连接器观测可补齐稳定账号 ID，但不会自动确认账号归属", "微信先 Exa 发现再 qiaomu 取全文", "融资优先 IT 桔子授权 API，公开 Exa 只做发现", "单渠道失败不阻断其他渠道", "未配置渠道返回 unconfigured，不模拟成功"], related_reference_ids=["PersonPlatformAccount", "PersonSearchTarget", "ConnectorRunRequest", "ConnectorAttempt", "XhsApiCliTransport", "XhsApiRouteCircuit", "StatefulConnectorWorker"], source_ref="src/people_intel/connectors.py::ConnectorExecutor"),
    TechnicalReferenceDefinition(reference_id="XhsApiCliTransport", kind="algorithm", title_zh="小红书只读 API 传输层", summary_zh="以专用 HOME 运行固定版本 xiaohongshu-cli，只允许 status/search/read/user/user-posts，正常扫描不需要持续控制图形化浏览器。", used_when_zh="小红书 ConnectorJob 通过渠道配额与账号锁后，且 API 路由未熔断时。", behavior_zh=["shell=False 参数数组执行", "单次最多读取 1 篇详情", "稳定 ok/data/error envelope", "Cookie 文件权限 0600 且不进入账本响应", "xsec_token 只保留在受控原始对象"], related_reference_ids=["ConnectorExecutor", "ConnectorAttempt", "XhsApiRouteCircuit"], source_ref="src/people_intel/xhs_api.py::XhsApiCliTransport"),
    TechnicalReferenceDefinition(reference_id="XhsApiRouteCircuit", kind="algorithm", title_zh="小红书传输层熔断与兜底选择", summary_zh="把签名失效与账号风控分开处理：signature_error 打开可兜底熔断，下一任务选择 Extension Bridge；验证码或 IP block 打开不可兜底熔断。", used_when_zh="API envelope 返回结构化错误码，或 worker 开始下一次小红书任务时。", behavior_zh=["首个签名失败任务只记录失败，不在同轮二次访问", "熔断状态跨 worker 重启保留", "冷却到期后 API canary 成功即关闭", "verification_required/ip_blocked 等待人工处理", "GET /v1/system/connectors 展示 reason/retry/fallback_allowed"], related_reference_ids=["ConnectorAttempt", "XhsApiCliTransport", "ConnectorExecutionPolicy"], source_ref="src/people_intel/xhs_api.py::XhsApiRouteCircuit"),
    TechnicalReferenceDefinition(reference_id="FeishuIngress", kind="algorithm", title_zh="飞书事件的安全入队边界", summary_zh="把飞书消息校验为统一 CommandEnvelope，并通过共享 handler 只追加 CommandReceipt 与 ConnectorJob；这里不调用任何来源连接器。", used_when_zh="收到 im.message.receive_v1 文本消息时。", behavior_zh=["open_id/chat_id allowlist", "message_id 生成稳定 command_id", "重复事件返回同一回执", "扫描命令只产生 queued 任务", "普通文本按用户输入原文入库", "不启动 Chrome、不读取平台 cookie"], related_reference_ids=["FeishuLongConnectionWorker", "ConnectorJob", "ConnectorRunRequest"], source_ref="src/people_intel/feishu_runtime.py::FeishuIngress"),
    TechnicalReferenceDefinition(reference_id="FeishuLongConnectionWorker", kind="algorithm", title_zh="飞书官方 WebSocket 长连接进程", summary_zh="使用 lark-oapi 从本机主动连接飞书事件流，不开放公网 webhook；SDK 事件回调交给 FeishuIngress。", used_when_zh="本机或常驻 worker 主机启动 people-intel feishu-worker 后。", behavior_zh=["只要求 App ID/secret 与 actor/chat allowlist", "verification/encrypt 仅为 webhook 兼容预留", "凭据不进入 manifest、日志或任务 payload", "事件处理快速结束", "来源访问由独立 ConnectorWorker 执行"], related_reference_ids=["FeishuIngress", "StatefulConnectorWorker", "connector.job.queued"], source_ref="src/people_intel/feishu_runtime.py::FeishuLongConnectionWorker"),
    TechnicalReferenceDefinition(reference_id="StatefulConnectorWorker", kind="algorithm", title_zh="可恢复的持久来源 worker", summary_zh="从 PostgreSQL 用 SKIP LOCKED + lease 独占领取 ConnectorJob，持有 X/小红书登录态并执行白名单命令；飞书回调只入队。", used_when_zh="渠道依赖 cookie、Chrome Profile、进程锁，或任何扫描需要跨进程重试时。", behavior_zh=["并发 worker 只允许一个 claim", "lease 到期后允许另一 worker 接管", "失败保留 ConnectorAttempt 并指数退避", "成功才写 CommandReceipt", "达到 max_attempts 后 dead_lettered", "登录态不进入飞书消息和 API 响应"], related_reference_ids=["ConnectorJob", "ConnectorJobEvent", "ConnectorRunRequest", "ConnectorExecutor"], source_ref="src/people_intel/connector_jobs.py::ConnectorWorker"),
    TechnicalReferenceDefinition(reference_id="ConnectorScheduleProducer", kind="algorithm", title_zh="幂等周期任务生产者", summary_zh="读取稳定订阅，比较同 tracking_key 最近任务时间，只为到期条目生成按时间桶确定的 command_id。", used_when_zh="每 60 秒 scheduler tick 或系统 cron 调用 --once 时。", behavior_zh=["相同时间桶重复运行不重复入队", "disabled 订阅保留但跳过", "只产生 ConnectorJob，不直接访问平台", "PostgreSQL 是跨重启状态来源"], related_reference_ids=["ConnectorScheduleConfig", "ConnectorJob"], source_ref="src/people_intel/connector_scheduler.py::ConnectorScheduleProducer"),
    TechnicalReferenceDefinition(reference_id="ConnectorExecutionPolicy", kind="algorithm", title_zh="配额、节流与熔断门", summary_zh="从追加式 ConnectorAttempt 历史计算渠道是否允许真实访问；被阻止时追加 deferred，而不是消耗一次外部重试。", used_when_zh="worker 已 claim 任务但尚未调用 X、微信、小红书或新闻连接器时。", behavior_zh=["自然日配额", "最小访问间隔", "连续失败熔断", "deferred 到期后复用同一执行 attempt", "X/小红书另有跨进程文件锁"], related_reference_ids=["ConnectorPolicyDefinition", "ConnectorJobEvent", "ConnectorAttempt"], source_ref="src/people_intel/connector_policy.py::ConnectorExecutionPolicy"),
    TechnicalReferenceDefinition(reference_id="CogneeAdapter", kind="algorithm", title_zh="Cognee 旁路时态检索适配器", summary_zh="在权威原文落盘后调用 remember/temporal cognify，并分别用 CHUNKS、TEMPORAL、HYBRID 或 GRAPH 找回上下文；返回值始终标记 projection_only。", used_when_zh="Cognee optional dependency 已安装且用户请求单源/全量投影或检索时。", behavior_zh=["self_improvement=False", "持久 asyncio Runner 避免跨请求 event-loop 锁错误", "失败不阻断 SourceVersion/Assertion 写入", "chunk 结果精确反查 SourceVersion/hash/字符区间", "recall 输出不得直接成为 human_confirmed Assertion", "可从原文版本全量重建"], related_reference_ids=["SourceDocumentVersion", "EvidenceSpan", "POST /v1/source-documents/{source_version_id}/cognify", "POST /v1/cognee/project-all", "POST /v1/cognee/recall"], source_ref="src/people_intel/adapters/cognee.py::CogneeAdapter"),
    TechnicalReferenceDefinition(reference_id="HumanInitialProfileGate", kind="algorithm", title_zh="首次人物事实人工确认门", summary_zh="任何初始名单或背景文件抽取出的 Assertion 都进入 ReviewBatch；未确认项即使置信度高也从 Working View 排除。", used_when_zh="新建人物档案，而不是后续定时动态扫描时。", behavior_zh=["先保存原文和机器候选", "每项必须带 EvidenceSpan", "pending/ambiguous/rejected 不进 Working View", "confirm 通过 effective status 生效", "correct 追加 supersedes，不改旧事实", "可导出 Markdown 交给外部审核"], related_reference_ids=["InitialReviewBatch", "InitialReviewDecision", "WorkingViewRanking"], source_ref="src/people_intel/service.py::create_initial_review_batch"),
    TechnicalReferenceDefinition(reference_id="DerivedInferenceGate", kind="algorithm", title_zh="逻辑推定依赖门", summary_zh="Derived Assertion 必须同时通过结论审核和全部输入 Assertion 审核；确认结论不能代替确认前提。", used_when_zh="共同作者、团队形成或其他图规则从多条事实推导新关系时。", behavior_zh=["记录全部 input_assertion_ids", "记录 rule_id/rule_version", "输入被驳回后派生关系自动失效", "输入未确认时派生关系不进入 Working View"], related_reference_ids=["Assertion", "InitialReviewBatch", "WorkingViewRanking"], source_ref="src/people_intel/service.py::working_view"),
    TechnicalReferenceDefinition(reference_id="SourceSelectionPolicy", kind="algorithm", title_zh="人物—来源选择策略", summary_zh="依据人物类型、已知账号、关注主题和渠道状态选择本轮来源与查询。", used_when_zh="ScanRun 创建后、连接器执行前。", behavior_zh=["researcher 优先主页/arXiv/GitHub", "builder 优先主页/GitHub/X", "创业与融资优先官网/新闻", "记录每个选择与跳过理由"], related_reference_ids=["ScanRun", "ConnectorExecutor"], source_ref="src/people_intel/workflow.py::STAGES[plan]"),
    TechnicalReferenceDefinition(reference_id="SourceVersioningAlgorithm", kind="algorithm", title_zh="原文版本化算法", summary_zh="对内容计算 SHA-256；同 URI 同 hash 返回 unchanged，不同 hash 追加版本并生成 ChangeSet。", used_when_zh="每个 selected 候选抓取完成后。", behavior_zh=["绝不覆盖旧版本", "unchanged 不重复抽取", "diff 本身不自动成为职业事实"], related_reference_ids=["SourceDocumentVersion", "ChangeSet"], source_ref="src/people_intel/service.py::ingest_source"),
    TechnicalReferenceDefinition(reference_id="PredicateOntology", kind="algorithm", title_zh="版本化谓词本体约束", summary_zh="限制模型只能使用注册谓词，并定义 domain/range、时间类型、负向规则与冲突策略。", used_when_zh="候选关系抽取与 Assertion 校验时。", behavior_zh=["未知自由文本谓词拒绝写入", "people-intel-v1 语义保持稳定"], related_reference_ids=["Assertion"], source_ref="ontology/people-intel-v1.json"),
    TechnicalReferenceDefinition(reference_id="IdentityResolutionAlgorithm", kind="algorithm", title_zh="可撤回身份消歧", summary_zh="通过 same_as/possibly_same_as/not_same_as/account_owned_by 构造可拆分 IdentityCluster。", used_when_zh="跨主页、论文和账号把信息归属到人物时。", behavior_zh=["不因同名自动合并", "拆分不回收实体 ID", "人工关系优先"], related_reference_ids=["Assertion"], source_ref="src/people_intel/service.py"),
    TechnicalReferenceDefinition(reference_id="WorkingViewRanking", kind="algorithm", title_zh="Working View 排序策略", summary_zh="综合人工状态、来源权威、时间精度、置信度和冲突，选择当前展示项并保留备选。", used_when_zh="任何当前状态查询时。", behavior_zh=["人工确认优先", "官方直接来源优先", "明确时间优先", "低于阈值只进时间线"], related_reference_ids=["WorkingView", "Assertion"], source_ref="src/people_intel/service.py::working_view"),
    TechnicalReferenceDefinition(reference_id="GraphSignalEngine", kind="algorithm", title_zh="图原生信号规则", summary_zh="把新增 authored/cofounded/raised_funding 等图结构映射为可解释 Signal。", used_when_zh="Assertion 成功追加或仅来源版本发生变化后。", behavior_zh=["附触发 Assertion 和 EvidenceSpan", "冲突提高数据质量信号优先级", "homepage_change 不等价于 role_change"], related_reference_ids=["Signal", "Assertion"], source_ref="src/people_intel/signals.py::GraphSignalEngine"),
    TechnicalReferenceDefinition(reference_id="DailyDigestAggregation", kind="algorithm", title_zh="日报/周报聚合策略", summary_zh="跨人物合并同一事件，按重要性、新鲜度和证据质量排序，同时报告冲突与渠道失败。", used_when_zh="每日 18:00 与周一 08:30。", behavior_zh=["同人同事件去重", "高分信号在前", "低置信进入待裁决", "附来源覆盖与失败统计"], related_reference_ids=["Signal", "ScanRun"], source_ref="src/people_intel/scheduler.py"),
    TechnicalReferenceDefinition(reference_id="PlatformTrackingDefinition", kind="algorithm", title_zh="X/XHS/GitHub 账号与检索算法契约", summary_zh="版本化定义一级账号证据分、三平台 query ladder、内容归因、checkpoint、退化路由和发布门；前端报告直接读取该契约。", used_when_zh="首次寻找平台账号、已知账号定时刷新、平台端点失败退化或判断内容是否属于本人时。", behavior_zh=["stable ID 优先于可变用户名", "首次账号归属始终需要人工确认", "X 分离 tracked account 与内容原作者", "XHS 普通 API 错误后按已知 user_id 过滤搜索结果", "GitHub 组织仓库沿 contributors/commits 核验贡献"], related_reference_ids=["PersonPlatformAccount", "PersonSearchPlanRevision", "ConnectorExecutor", "AdversarialEvaluationReport", "GET /v1/platform-tracking/definition"], source_ref="src/people_intel/platform_tracking.py::PLATFORM_TRACKING_DEFINITION"),
    TechnicalReferenceDefinition(reference_id="AdversarialEvaluationReport", kind="schema", title_zh="三轮真实平台对抗评估", summary_zh="把 6 个真实公开证据案例、每轮查询路径、账号/事件/归因/关系结果和整体指标固化成可重算报告。", used_when_zh="平台算法修改后判断是否提高召回且没有引入身份污染时。", fields=[_field("fixture_hash", "冻结公开证据 fixture 的完整性"), _field("rounds", "round_1/2/3 的逐案例结果"), _field("stable_id_precision", "账号稳定 ID 是否与金标一致"), _field("attribution_accuracy", "内容原作者与传播关系是否正确"), _field("identity_pollution_count", "错误账号写入人物投影的次数")], related_reference_ids=["PlatformTrackingDefinition", "test_platform_adversarial.py", "GET /v1/platform-tracking/adversarial-evaluation"], source_ref="src/people_intel/platform_tracking.py::AdversarialEvaluationReport"),
]


for _event_id, _title, _summary, _used in [
    ("connector.job.queued", "来源扫描任务已排队", "飞书或 cron 已持久化 ConnectorJob，可立即返回任务 ID；尚未访问外部平台。", "ConnectorJob 与 queued 事件追加后"),
    ("scan.started", "扫描已创建", "通知观测层一个 ScanRun 开始；事件本身不写人物事实。", "ScanRun 追加后"),
    ("connector.started", "渠道调用开始", "前端可显示正在访问哪个渠道。", "ConnectorExecutor 调用前"),
    ("connector.completed", "渠道调用结束", "携带 completed/failed/unconfigured 状态，触发覆盖率统计。", "ConnectorAttempt 追加后"),
    ("source.versioned", "原文已版本化", "通知后续读取具体 SourceVersion。", "原文写入后"),
    ("change.detected", "检测到内容变化", "只有 new_version 才触发抽取步骤。", "ChangeSet 追加后"),
    ("extraction.completed", "证据抽取完成", "携带候选 Assertion ID，允许为空。", "EvidenceSpan/Assertion 处理后"),
    ("assertion.appended", "事实关系已追加", "触发图投影、Working View 与信号规则。", "Assertion 写入后"),
    ("signal.emitted", "关注信号已生成", "把人物级变化送入日报聚合器。", "Signal 追加后"),
    ("delivery.previewed", "交付内容已生成", "演示或飞书适配器可读取最终摘要。", "日报条目构造后"),
    ("cognee.projection.completed", "Cognee 投影已完成", "原文已建立可重建语义图与向量投影；不会自动确认 Assertion。", "ExtractionRun 成功追加后"),
    ("cognee.projection.failed", "Cognee 投影失败", "记录可展示错误并保留 SourceVersion；权威知识写入不回滚。", "ExtractionRun 失败追加后"),
    ("cognee.batch.completed", "Cognee 全量投影批次完成", "返回来源总数、成功/失败和覆盖率；只有 coverage=100%、failed=0 才通过投影门。", "全量 SourceVersion 逐项处理后"),
    ("cognee.recall.completed", "Cognee 检索完成", "返回指定检索策略的候选上下文、耗时和证据交叉引用，结果仍需 Evidence gate。", "CHUNKS/TEMPORAL/HYBRID/GRAPH recall 完成后"),
    ("review.batch.created", "首次建档审核批次已生成", "提醒人类开始处理机器提出的首批人物事实。", "ReviewBatch 与 ReviewItem 追加后"),
    ("review.item.resolved", "一条首次事实已裁决", "触发批次进度重算；确认可进入 Working View，其他结果进入审计或补证据队列。", "ReviewDecision 与 Annotation 追加后"),
    ("knowledge.initial_import.completed", "初始知识拆分完成", "完整原文、段落证据、直接事实和派生推定已写入并形成审核批次。", "ICML/名单 importer 完成后"),
    ("review.message.processed", "审核消息已处理", "记录消息是否匹配、执行了什么动作以及影响多少 ReviewItem。", "网页或飞书文本指令解析后"),
    ("review.bulk_resolved", "集中审核已批量裁决", "通知前端或飞书本次批量处理数量，并触发 Working View 进度刷新。", "批量 ReviewDecision 与 Annotation 追加后"),
    ("source.watch.scanned", "资料目录已扫描", "报告目录 manifest 的 new/changed/unchanged 与附件变化；事件本身不推导人物事实。", "目录清单版本写入后"),
    ("feishu.document.versioned", "飞书资料文档已版本化", "通知同一飞书 URL 的具体内容版本已保存并可进入审核。", "飞书规范化正文写入后"),
]:
    TECHNICAL_REFERENCES.append(TechnicalReferenceDefinition(reference_id=_event_id, kind="event", title_zh=_title, summary_zh=_summary, used_when_zh=_used, behavior_zh=["SSE 仅作实时提示", "丢失后可从追加账本恢复"], source_ref="src/people_intel/events.py"))


for _endpoint, _title, _summary in [
    ("POST /v1/demo/story-sessions", "创建隔离故事运行", "输入 story_id/mode；输出 StorySession 与关联 ScanRun。"),
    ("POST /v1/connectors/runs", "执行一次可审计来源扫描", "输入稳定 command_id、x/wechat/xhs/news 渠道、查询或已知 URL；输出 ScanRun、真实 ConnectorAttempt、候选结果以及已经取得全文的 SourceVersion ID。"),
    ("POST /v1/connector-jobs", "持久化来源扫描任务", "输入 ConnectorRunRequest、max_attempts 与 not_before；立即返回任务当前状态，不在 HTTP 回调内等待浏览器。"),
    ("GET /v1/connector-jobs/{connector_job_id}/events", "读取任务完整状态史", "输出每次 queued、claimed、retry_scheduled、succeeded 或 dead_lettered 事件及 worker、lease、错误和产物 ID。"),
    ("GET /v1/system/connector-policy", "读取实时渠道保护参数", "输出最小间隔、每日上限、失败熔断与串行要求；环境变量覆盖后页面同步变化。"),
    ("GET /v1/system/connector-schedule", "读取当前周期订阅", "输出正在跟踪的人物/主题、渠道、查询、间隔、结果上限与启停状态。"),
    ("GET /v1/person-profiles/{person_key}/platform-accounts", "读取人物一级账号表", "输出 X、小红书与 GitHub 账号的稳定平台 ID、当前用户名、身份状态、证据和观测边界。"),
    ("GET /v1/person-profiles/{person_key}/search-plan", "读取人物搜索设计", "输出已知账号刷新、开放账号发现、人物提及、采集字段与增量 checkpoint 的当前版本。"),
    ("GET /v1/platform-tracking/definition", "读取平台追踪算法契约", "输出 stable ID 规则、身份证据分、X/XHS/GitHub 查询阶梯、内容归因、退化路由和发布门。"),
    ("GET /v1/platform-tracking/adversarial-evaluation", "读取三轮真实对抗评估", "从冻结公开证据重算 6 个案例的事件召回、stable ID precision、归因准确率与身份污染。"),
    ("GET /v1/workflow/definition", "读取唯一流程说明", "返回本页面使用的功能、调度、来源、步骤和技术术语定义。"),
    ("GET /v1/scan-runs/{scan_run_id}/attempts", "读取渠道运行明细", "输出某次扫描的全部 ConnectorAttempt。"),
    ("POST /v1/source-documents", "写入不可变原文", "输入 URI、内容、抓取方法；输出版本状态、hash 与 SourceVersion ID。"),
    ("POST /v1/graph/query", "执行时态图查询", "输入 subject、valid_at、known_at 和 view；输出事实时间线或 Working View。"),
    ("GET /v1/entities/{entity_id}/working-view", "读取人物当前工作视图", "输入双时态与谓词过滤；输出选中关系、备选关系和理由。"),
    ("GET /v1/signals", "分页读取关注信号", "输出可过滤、排序并回溯证据的 Signal。"),
    ("GET /v1/memory/architecture", "读取知识库架构契约", "输出权威分层、Cognee 实际集成方式、图例和七步写入路径。"),
    ("GET /v1/demo/story-sessions/{session_id}/knowledge-journey", "读取本次知识写入实况", "输出当前 sample 每一步的真实状态、对象 ID、停止原因和局部图。"),
    ("POST /v1/source-documents/{source_version_id}/cognify", "建立 Cognee 检索投影", "输入 dataset_name/temporal；输出 ExtractionRun 与 projection-only metadata。"),
    ("GET /v1/cognee/status", "读取 Cognee 真实运行配置", "输出 provider、model、dataset、本地存储与是否配置凭证，但绝不返回 secret。"),
    ("POST /v1/cognee/project-all", "投影全部 SourceVersion", "输入 dataset/extractor/stop_on_failure；逐项输出 ExtractionRun、覆盖率、复用、失败与存储变化。"),
    ("POST /v1/cognee/recall", "执行 Cognee 多策略检索", "输入查询、dataset、top_k 与 query_type；输出 projection-only 候选、耗时和 SourceVersion 证据候选。"),
    ("POST /v1/initial-reviews/from-story/{session_id}", "生成首次建档审核批次", "把 sample 中的候选 Assertion 与证据组织成可人工裁决的 ReviewBatch。"),
    ("POST /v1/initial-knowledge/imports/icml-markdown", "导入并拆分初始人物知识", "输入完整规范化 Markdown；输出人物、论文、证据、直接 Assertion、共同作者推定和审核批次计数。"),
    ("GET /v1/initial-reviews/{review_batch_id}/delivery", "生成飞书/网页审核卡片", "按稳定序号分页输出事实、证据、推理链、按钮 postback 和消息示例。"),
    ("POST /v1/initial-reviews/{review_batch_id}/messages", "处理审核文本指令", "支持确认、批量确认、驳回、标记歧义和结构化纠错；command_id 保证幂等。"),
    ("POST /v1/initial-review-items/{review_item_id}/decisions", "追加首次事实裁决", "输入 action/reason/actor；输出 ReviewDecision 并追加 Annotation。"),
    ("GET /v1/initial-reviews/{review_batch_id}/document", "读取集中审核文档", "按人物输出全部候选事实、稳定序号、证据与进度；无需逐张翻卡。"),
    ("POST /v1/initial-reviews/{review_batch_id}/bulk-decisions", "批量批准集中审核文档", "按全部待审、指定人物或指定条目追加幂等人工裁决。"),
    ("POST /v1/source-watches/directory/scan", "扫描资料文件夹", "输入服务端目录路径；输出附件 hash 状态、manifest 版本和首次审核批次。"),
    ("POST /v1/source-watches/feishu/scan", "追踪飞书文档版本", "输入稳定飞书 URL 与当前规范化 Markdown；输出版本状态和审核批次。"),
]:
    TECHNICAL_REFERENCES.append(TechnicalReferenceDefinition(reference_id=_endpoint, kind="api", title_zh=_title, summary_zh=_summary, used_when_zh="流程执行或技术页下钻时。", related_reference_ids=[], source_ref=f"OpenAPI::{_endpoint}"))


for _test_id, _title, _summary, _covers in [
    ("test_connector_jobs.py", "持久队列、worker 恢复与飞书入队测试", "验证任务幂等、独占 claim、lease 过期接管、失败可重试、成功封口和中文飞书命令的快速入队。", ["queued/claimed/succeeded 追加事件", "失败不写成功回执", "指数退避与 dead letter", "worker 重启接管", "飞书 callback 不执行浏览器"]),
    ("test_connector_policy_scheduler.py", "周期生产、节流熔断与账号锁测试", "验证订阅到期计算、确定性入队、自然日配额、最小间隔、连续失败熔断、deferred attempt 与跨进程文件锁。", ["重复 scheduler tick 不重复任务", "配额阻止真实访问", "熔断提供 next_attempt_at", "deferred 不消耗外部重试", "X/小红书登录态串行"]),
    ("test_live_source_connectors.py", "四类真实来源路由与统一账本测试", "验证 X 已知账号和关键词 fallback、微信发现/全文、小红书 API/GUI 分层路由、IT 桔子公开发现，以及统一 connector run 的幂等追加语义。", ["API 只读命令白名单与隔离 HOME", "签名错误下一任务 GUI 兜底", "验证码/IP 风控禁止自动兜底", "搜索摘要不冒充全文", "完整正文写入 SourceVersion", "xsecToken 不进入规范化投影", "重复 command_id 不重复扫描"]),
    ("test_feishu_runtime.py", "飞书长连接与快速入队测试", "验证官方 SDK 回调的事件封装、allowlist、message_id 幂等，以及回调结束前只产生 ConnectorJob。", ["WebSocket 模式不强制 webhook secret", "未授权事件不写账本", "重复消息只产生一个任务", "回调内 ScanRun/ConnectorAttempt 始终为空", "SDK client 使用运行时配置启动"]),
    ("test_product_workflow.py", "四故事产品闭环测试", "不是测试字段是否存在，而是验证四个故事都完成八步、重复步骤幂等、渠道尝试可审计。", ["主页删除不产生 left", "四故事均生成预期 artifact", "重复 discover 不追加重复 attempt"]),
    ("test_content_and_sources.py", "原文不可变与版本测试", "验证同 hash 幂等、不同 hash 追加版本和内容寻址完整性。", ["unchanged", "previous_version_id", "SHA-256 校验"]),
    ("test_temporal_memory.py", "双时态与裁决测试", "验证 valid_at/known_at、冲突共存、修正追加和 Derived 失效。", ["月级时间不伪造", "不同 known_at 返回不同认知", "人工 correction 不修改旧 Assertion"]),
    ("test_identity_and_import.py", "身份拆分与名单导入测试", "验证同名身份可合并、撤回、拆分且历史实体 ID 不变。", ["possibly_same_as", "not_same_as", "ICML 16 人导入"]),
    ("test_scheduler_signals_commands.py", "调度、信号与命令测试", "验证 cron 选择、图信号、冲突优先级和 command_id 幂等。", ["日报/周报时间", "Signal 证据链", "飞书命令不重复执行"]),
    ("test_memory_architecture.py", "知识库架构与写入实况测试", "验证分层权威边界、Cognee 非权威配置、写入步骤与实际图对象完全对齐。", ["Cognee unconfigured 不伪造成功", "有证据 sample 形成 Assertion", "主页变化在事实门停止", "所有架构 API 真实存在"]),
    ("test_cognee_projection.py", "Cognee 投影与边界测试", "验证完成/失败运行都进入 ExtractionRun、相同 hash 幂等、跨请求复用同一 event loop、批量覆盖率、chunk 证据回链，以及 recall 永远标记 projection_only。", ["运行配置不泄露 key", "失败不破坏 SourceVersion", "成功运行可复用", "全量批次 coverage 可计算", "exact chunk 回到 SourceVersion/hash"]),
    ("test_initial_review.py", "首次建档人工确认门测试", "验证批次生成、待确认不进 Working View、确认后生效、驳回留痕以及 Markdown 输出。", ["ReviewDecision 追加", "Annotation 可追溯", "Working View gate", "review 领域事件"]),
    ("test_initial_knowledge_import.py", "全量知识拆分与跨媒介审核测试", "验证 16 人知识包的段落拆分、直接事实、37 条共同作者推定、重复导入幂等和飞书消息式反馈。", ["EvidenceSpan 不丢段落", "coauthor 记录两条 authored 依赖", "推定与前提均确认后才进 Working View", "卡片分页", "消息纠错追加 supersedes", "未识别文本不改账本"]),
    ("test_consolidated_review_and_source_watch.py", "集中审核与资料源追踪测试", "验证人物分组、账号归组、批量批准幂等、附件对象完整性、目录变更和飞书同 URL 版本去重。", ["3 人文档形成 3 个章节", "人物批量审批不重复", "附件变化形成新 manifest", "相同飞书正文返回 unchanged"]),
    ("test_person_profiles.py", "逐人档案、账号计划与低噪声退化测试", "验证 person_key 隔离、逐人来源包、一级账号提取、平台稳定 ID 回写、计划版本化及 Cognee 关闭后的完整可用性。", ["跨人物 SourceSlice 污染为 0", "已知 GitHub URL 生成 known_account_feed", "connector 补齐 numeric ID 但不自动确认账号", "platform-accounts API 返回当前版本", "重复扫描不创建重复 bundle"]),
    ("test_platform_adversarial.py", "X/XHS/GitHub 三轮真实对抗测试", "验证 6 个公开证据案例从第一轮 2/6、第二轮 5/6 收敛到第三轮 6/6，并把账号污染、内容归因和关系证据作为独立发布门。", ["三档难度与三平台均覆盖", "硬身份冲突覆盖正向分数", "最终 stable ID precision 100%", "最终内容归因 100%", "最终身份污染为 0", "算法与评估 API 可读取"]),
]:
    TECHNICAL_REFERENCES.append(TechnicalReferenceDefinition(reference_id=_test_id, kind="test", title_zh=_title, summary_zh=_summary, used_when_zh="每次 MVP 迭代运行 people-intel verify-demo 时。", behavior_zh=_covers, source_ref=f"tests/{_test_id}"))


WORKFLOW_DEFINITION = WorkflowDefinition(
    workflow_version="people-intel-workflow-v1",
    title_zh="从关注名单到定时集中报告",
    product_purpose_zh="持续维护一组值得关注的人，按优先级自动检查公开动态，把变化写入可追溯知识库，并在固定时间集中反馈。",
    product_functions=PRODUCT_FUNCTIONS,
    cohort_examples=COHORT_EXAMPLES,
    schedules=SCHEDULES,
    source_channels=SOURCE_CHANNELS,
    stages=STAGES,
    predicate_presentations=PREDICATE_PRESENTATIONS,
    technical_references=TECHNICAL_REFERENCES,
)


class StoryFixtureStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        document = json.loads(self.raw)
        self.fixture_version = document["fixture_version"]
        self.captured_at = document["captured_at"]
        self.items = {item["story_id"]: item for item in document["stories"]}

    def fixture_hash(self, story_id: str) -> str:
        payload = json.dumps(self.items[story_id], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def stories(self) -> list[DemoStory]:
        return [
            DemoStory(
                story_id=item["story_id"], title_zh=item["title_zh"], subject_name=item["subject_name"],
                category_zh=item["category_zh"], promise_zh=item["promise_zh"], outcome_zh=item["outcome_zh"],
                channels=item["channels"], default=item["story_id"] == "homepage-change",
                fixture_hash=self.fixture_hash(item["story_id"]),
            )
            for item in self.items.values()
        ]


class StoryController:
    def __init__(self, service: TemporalMemoryService, broker: EventBroker, project_root: Path):
        self.service = service
        self.broker = broker
        self.project_root = project_root
        self.fixtures = StoryFixtureStore(project_root / "fixtures" / "demo_stories.json")
        self.connectors = ConnectorExecutor(service.object_store)
        self.sessions: dict[str, StorySession] = {}
        self.results: dict[str, dict[int, StoryStepResult]] = {}
        self.state: dict[str, dict[str, Any]] = {}

    def create_session(self, request: StorySessionCreate) -> StorySession:
        if request.story_id not in self.fixtures.items:
            raise KeyError(request.story_id)
        item = self.fixtures.items[request.story_id]
        run = ScanRun(story_id=request.story_id, subject_name=item["subject_name"], trigger="demo_opened", mode=request.mode, channels=item["channels"], metadata={"fixture_hash": self.fixtures.fixture_hash(request.story_id)})
        self.service.ledger.append_scan_run(run)
        session = StorySession(story_id=request.story_id, mode=request.mode, scan_run_id=run.scan_run_id)
        self.sessions[session.session_id] = session
        self.results[session.session_id] = {}
        self.state[session.session_id] = {"run": run, "fixture": item}
        return session

    def get_session(self, session_id: str) -> StorySession:
        return self.sessions[session_id]

    def reset(self, session_id: str) -> StorySession:
        current = self.get_session(session_id)
        return self.create_session(StorySessionCreate(story_id=current.story_id, mode=current.mode))

    def run_stage(self, session_id: str, stage_id: str) -> StoryStepResult:
        session = self.get_session(session_id)
        stage = next((item for item in STAGES if item.stage_id == stage_id), None)
        if stage is None:
            raise KeyError(stage_id)
        existing = self.results[session_id].get(stage.position)
        if existing:
            return existing.model_copy(update={"status": "already_completed"})
        if stage.position != session.current_stage + 1:
            raise ValueError(f"next stage is {STAGES[session.current_stage].stage_id}")
        handler = getattr(self, f"_stage_{stage.stage_id}")
        artifacts, events, changed = handler(session_id)
        result = StoryStepResult(
            session_id=session_id, story_id=session.story_id, stage_id=stage.stage_id, position=stage.position,
            status="completed", narration_zh=stage.purpose_zh, decision_zh=stage.decision_zh,
            artifact_ids=[item.artifact_id for item in artifacts], domain_event_ids=events, changed_ids=changed,
        )
        self.results[session_id][stage.position] = result
        self.sessions[session_id] = session.model_copy(update={"current_stage": stage.position, "status": "completed" if stage.position == 8 else "ready", "updated_at": utc_now()})
        return result

    def artifacts(self, session_id: str, stage_id: str | None = None) -> list[WorkflowArtifact]:
        run_id = self.get_session(session_id).scan_run_id
        values = self.service.ledger.list_workflow_artifacts(run_id)
        return [item for item in values if stage_id is None or item.stage_id == stage_id]

    def graph_delta(self, session_id: str) -> dict[str, Any]:
        state = self.state[session_id]
        ids = set(state.get("entity_ids", []) + state.get("assertion_ids", []) + state.get("span_ids", []) + state.get("source_ids", []))
        graph = build_memory_graph(self.service)
        nodes = [item for item in graph.nodes if item.id in ids]
        node_ids = {item.id for item in nodes}
        edges = [item for item in graph.edges if item.source in node_ids and item.target in node_ids]
        return {"nodes": nodes, "edges": edges}

    def knowledge_journey(self, session_id: str) -> KnowledgeJourney:
        session = self.get_session(session_id)
        state = self.state[session_id]
        graph_value = self.graph_delta(session_id)
        graph = MemoryGraph(nodes=graph_value["nodes"], edges=graph_value["edges"])
        artifacts = self.artifacts(session_id)
        evidence_artifact = next((item for item in artifacts if item.kind == "evidence"), None)
        # The signal card and delivery preview can reference the same Signal.
        # Journey counts describe domain objects, not the number of projections
        # that mention them, so keep first-seen order while removing duplicates.
        signal_ids = list(dict.fromkeys(value for item in artifacts for value in item.signal_ids))
        source_ids = state.get("source_ids", [])
        span_ids = state.get("span_ids", [])
        entity_ids = state.get("entity_ids", [])
        assertion_ids = state.get("assertion_ids", [])
        cognee_runs = [
            item for item in self.service.ledger.list_extraction_runs()
            if item.extractor == "cognee" and item.source_version_id in source_ids
        ]
        completed_cognee_runs = [item for item in cognee_runs if item.status == "completed"]
        failed_cognee_runs = [item for item in cognee_runs if item.status == "failed"]
        runtime = self.service.cognee_status()
        if completed_cognee_runs:
            cognee_status = "completed"
            cognee_result = (
                f"已把 {len(completed_cognee_runs)} 个原文版本真实投影到 {runtime.dataset_name}；"
                f"本地投影当前包含 {runtime.storage.get('file_count', 0)} 个文件。"
            )
        elif failed_cognee_runs:
            cognee_status = "failed"
            cognee_result = f"Cognee 投影失败但未影响权威账本：{failed_cognee_runs[-1].metadata.get('error', '未知错误')}"
        elif runtime.ready:
            cognee_status = "available_not_invoked"
            cognee_result = "Gemini + Cognee 已就绪；点击算法详情页的“投影当前原文”后才会产生真实运行记录。"
        else:
            cognee_status = "unconfigured"
            cognee_result = runtime.detail_zh
        stop_reason = None
        if session.current_stage >= 5 and not assertion_ids:
            stop_reason = str((evidence_artifact.payload if evidence_artifact else {}).get("reason_zh") or "没有足够直接证据创建 Assertion。")

        def status(stage_position: int, *, skipped: bool = False) -> str:
            if session.current_stage < stage_position:
                return "pending"
            return "skipped" if skipped else "completed"

        steps = [
            KnowledgeJourneyStep(step_id="capture", position=1, title_zh="保存新原文", status=status(4), actual_result_zh=(f"追加 {len(source_ids)} 个 SourceDocumentVersion，旧版本仍可访问。" if source_ids else "尚未执行原文版本化。"), output_ids=source_ids, graph_node_ids=source_ids),
            KnowledgeJourneyStep(step_id="cognify", position=2, title_zh="建立 Cognee 检索投影", status=(cognee_status if source_ids else "pending"), actual_result_zh=cognee_result, input_ids=source_ids, output_ids=[item.extraction_run_id for item in cognee_runs]),
            KnowledgeJourneyStep(step_id="evidence", position=3, title_zh="定位原文证据", status=status(5), actual_result_zh=(f"创建 {len(span_ids)} 个 EvidenceSpan，保留 quote 与 locator。" if span_ids else "尚未产生 EvidenceSpan。"), input_ids=source_ids, output_ids=span_ids, graph_node_ids=span_ids),
            KnowledgeJourneyStep(step_id="validate", position=4, title_zh="经过身份、本体和时间门", status=status(6), actual_result_zh=("候选关系通过身份、注册谓词和时间边界检查。" if assertion_ids else (stop_reason or "尚未完成事实门控。")), input_ids=entity_ids + span_ids, output_ids=assertion_ids),
            KnowledgeJourneyStep(step_id="assert", position=5, title_zh="追加 Assertion", status=status(6, skipped=session.current_stage >= 6 and not assertion_ids), actual_result_zh=(f"追加 {len(assertion_ids)} 条 machine_proposed Assertion；没有覆盖任何旧关系。" if assertion_ids else (stop_reason or "尚未执行 Assertion 写入。")), input_ids=entity_ids + span_ids, output_ids=assertion_ids, graph_node_ids=entity_ids + assertion_ids),
            KnowledgeJourneyStep(step_id="project", position=6, title_zh="更新图与当前视图", status=status(7), actual_result_zh=(f"本次局部图包含 {len(graph.nodes)} 个节点、{len(graph.edges)} 条边。" if session.current_stage >= 7 else "尚未投影局部图。"), input_ids=assertion_ids, output_ids=[item.id for item in graph.nodes], graph_node_ids=[item.id for item in graph.nodes]),
            KnowledgeJourneyStep(step_id="signal", position=7, title_zh="生成信号与集中报告", status=status(8), actual_result_zh=(f"生成 {len(signal_ids)} 个 Signal，并形成飞书集中报告预览。" if signal_ids else "尚未生成 Signal。"), input_ids=assertion_ids + span_ids, output_ids=signal_ids),
        ]
        return KnowledgeJourney(
            session_id=session_id,
            story_id=session.story_id,
            scan_run_id=session.scan_run_id,
            subject_name=state["fixture"]["subject_name"],
            assertion_created=bool(assertion_ids),
            stop_reason_zh=stop_reason,
            steps=steps,
            graph_delta=graph,
        )

    def _artifact(self, session_id: str, stage_id: str, kind: str, title: str, summary: str, payload: dict[str, Any], **refs) -> WorkflowArtifact:
        item = WorkflowArtifact(scan_run_id=self.get_session(session_id).scan_run_id, stage_id=stage_id, kind=kind, title_zh=title, summary_zh=summary, payload=payload, **refs)
        self.service.ledger.append_workflow_artifact(item)
        return item

    def _event(self, event_type: str, session_id: str, payload: dict[str, Any] | None = None) -> int:
        event = self.broker.publish(event_type, "scan_run", self.get_session(session_id).scan_run_id, trace_id=session_id, payload=payload or {})
        return event.event_id

    def _stage_trigger(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        artifact = self._artifact(session_id, "trigger", "trigger", "这次为什么更新", f"{fixture['subject_name']} 到达本轮监控时间窗。", {"trigger": "demo_opened", "subject": fixture["subject_name"], "mode": self.get_session(session_id).mode, "channels": fixture["channels"]})
        return [artifact], [self._event("scan.started", session_id)], [self.get_session(session_id).scan_run_id]

    def _stage_plan(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        artifact = self._artifact(session_id, "plan", "source_plan", "系统先决定去哪找", "按人物身份和目标变化选择渠道，而不是全网盲搜。", {"queries": fixture["queries"], "channels": [{"channel": channel, "reason_zh": self._channel_reason(channel)} for channel in fixture["channels"]]})
        return [artifact], [], []

    def _stage_discover(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        run_id = self.get_session(session_id).scan_run_id
        events: list[int] = []
        attempts = []
        for channel in fixture["channels"]:
            events.append(self._event("connector.started", session_id, {"channel": channel}))
            query = next((q for q in fixture["queries"] if channel in q.lower()), fixture["queries"][0])
            if self.get_session(session_id).mode == "live":
                output = self.connectors.execute(channel, query=query, source_uri=fixture["source_uri"] if channel == "homepage" else None)
                attempt = ConnectorAttempt(scan_run_id=run_id, channel=channel, route=output.route, request_summary=output.request_summary, status=output.status, duration_ms=output.duration_ms, raw_object_ref=output.raw_object_ref, error=output.error, metadata={"transport": "live", "result_count": len(output.results), **output.metadata})
                if output.normalized_text and channel == "homepage":
                    self.state[session_id]["live_content"] = output.normalized_text
            else:
                attempt = ConnectorAttempt(scan_run_id=run_id, channel=channel, route=self._route(channel), request_summary=query, status="completed", duration_ms=84, metadata={"transport": "replay"})
            self.service.ledger.append_connector_attempt(attempt)
            attempts.append(attempt)
            events.append(self._event("connector.completed", session_id, {"channel": channel, "status": attempt.status}))
        candidates = []
        for index, value in enumerate(fixture["candidates"], 1):
            candidate = DiscoveryCandidate(scan_run_id=run_id, channel=value["channel"], rank=index, title=value["title"], uri=value["uri"], decision=value["decision"], reason_zh=value["reason_zh"])
            self.service.ledger.append_discovery_candidate(candidate)
            candidates.append(candidate)
        artifact = self._artifact(session_id, "discover", "search_results", "搜索结果不是事实", "系统展示为什么选择或跳过每条结果。", {"attempts": [item.model_dump(mode="json") for item in attempts], "candidates": [item.model_dump(mode="json") for item in candidates]})
        return [artifact], events, [item.connector_attempt_id for item in attempts] + [item.discovery_candidate_id for item in candidates]

    def _stage_version(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        retrieved = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)
        before = self.service.ingest_source(SourceIngestionRequest(source_uri=fixture["source_uri"], source_type=SourceType(fixture["source_type"]), content=fixture["before"], normalized_markdown=fixture["before"], retrieved_at=retrieved, fetch_method=FetchMethod(fixture["fetch_method"]), source_identity=fixture["subject_name"]))
        after_content = self.state[session_id].get("live_content", fixture["after"])
        after = self.service.ingest_source(SourceIngestionRequest(source_uri=fixture["source_uri"], source_type=SourceType(fixture["source_type"]), content=after_content, normalized_markdown=after_content, retrieved_at=retrieved.replace(minute=1), fetch_method=FetchMethod(fixture["fetch_method"]), source_identity=fixture["subject_name"]))
        blocks = [ChangeBlock.model_validate(item) for item in fixture["change_blocks"]]
        change = ChangeSet(scan_run_id=self.get_session(session_id).scan_run_id, source_uri=fixture["source_uri"], previous_source_version_id=before.source_version_id, current_source_version_id=after.source_version_id, version_status=after.version_status, blocks=blocks)
        self.service.ledger.append_change_set(change)
        self.state[session_id].update(source_ids=[before.source_version_id, after.source_version_id], current_source_id=after.source_version_id, current_content=after_content, change_set_id=change.change_set_id)
        preview = self._artifact(session_id, "version", "source_preview", "原文先被保存", "URL 不是权威，带 hash 的具体内容版本才是。", {"uri": fixture["source_uri"], "fetch_method": fixture["fetch_method"], "retrieved_at": retrieved.isoformat(), "before_hash": before.content_hash, "after_hash": after.content_hash, "excerpt": fixture["after"]}, source_version_ids=[before.source_version_id, after.source_version_id])
        diff = self._artifact(session_id, "version", "version_diff", "系统只解释真正变化的部分", "旧版本仍然可访问，新版本通过 previous_version_id 相连。", {"version_status": after.version_status, "blocks": [item.model_dump(mode="json") for item in blocks]}, source_version_ids=[before.source_version_id, after.source_version_id])
        return [preview, diff], [self._event("source.versioned", session_id, {"status": after.version_status}), self._event("change.detected", session_id, {"change_set_id": change.change_set_id})], [before.source_version_id, after.source_version_id, change.change_set_id]

    def _stage_evidence(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        source_id = self.state[session_id]["current_source_id"]
        content = self.state[session_id].get("current_content", fixture["after"])
        quote = fixture["quote"]
        direct_fixture_evidence = quote in content
        if not direct_fixture_evidence:
            quote = next((line.strip()[:240] for line in content.splitlines() if len(line.strip()) > 20), content[:240])
        start = max(0, content.find(quote))
        span = self.service.create_span(source_id, locator_type="character", locator={"start": start, "end": start + len(quote)}, quote=quote)
        changed: list[str] = [span.evidence_span_id]
        entity_ids: list[str] = []
        assertion_ids: list[str] = []
        payload: dict[str, Any] = {"quote": quote, "locator": span.locator, "candidate_relation": fixture["predicate_id"], "boundary_zh": "仅使用高亮原文支持的最小关系"}
        if fixture["predicate_id"] and direct_fixture_evidence:
            subject = self.service.create_entity(Entity(entity_type=EntityType(fixture["subject_type"]), canonical_name=fixture["subject_name"]))
            obj = self.service.create_entity(Entity(entity_type=EntityType(fixture["object_type"]), canonical_name=fixture["object_name"]))
            entity_ids = [subject.entity_id, obj.entity_id]
            changed += entity_ids
            candidate_assertion = {
                "subject_entity_id": subject.entity_id,
                "predicate_id": fixture["predicate_id"],
                "object_entity_id": obj.entity_id,
                "evidence_span_ids": [span.evidence_span_id],
                "confidence": fixture["confidence"],
                "status": "candidate_before_identity_temporal_gate",
            }
            payload.update(subject=subject.model_dump(mode="json"), object=obj.model_dump(mode="json"), candidate_assertion=candidate_assertion)
            self.state[session_id].update(subject_id=subject.entity_id, object_id=obj.entity_id, pending_assertion=candidate_assertion)
        else:
            payload["candidate_relation"] = None
            payload["reason_zh"] = "原文没有直接支持目标关系，因此不创建 Assertion；页面删除也不会自动产生 negative Assertion 或 left 关系。"
        self.state[session_id].update(span_ids=[span.evidence_span_id], entity_ids=entity_ids, assertion_ids=assertion_ids)
        artifact = self._artifact(session_id, "evidence", "evidence", "高亮原文，再谈关系", "每条候选事实都必须能回到这一段原文。", payload, source_version_ids=[source_id], evidence_span_ids=[span.evidence_span_id], entity_ids=entity_ids, assertion_ids=assertion_ids)
        return [artifact], [self._event("extraction.completed", session_id, {"assertion_ids": assertion_ids})], changed

    def _stage_resolve(self, session_id: str):
        fixture = self.state[session_id]["fixture"]
        state = self.state[session_id]
        changed: list[str] = []
        assertion_ids: list[str] = []
        payload: dict[str, Any] = {"identity_result": "matched", "temporal_precision": "source-stated", "conflicts": [], "guardrail": "信息缺失不是否定事实"}
        candidate = state.get("pending_assertion")
        if candidate:
            observed_at = utc_now()
            assertion = self.service.create_assertion(Assertion(subject_entity_id=candidate["subject_entity_id"], predicate_id=candidate["predicate_id"], object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=candidate["object_entity_id"]), epistemic_type=EpistemicType.REPORTED, transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at), evidence_span_ids=candidate["evidence_span_ids"], confidence=candidate["confidence"], metadata={"story_id": fixture["story_id"]}))
            assertion_ids = [assertion.assertion_id]
            changed.append(assertion.assertion_id)
            state.update(assertion_id=assertion.assertion_id, assertion_ids=assertion_ids)
            payload["appended_assertion"] = assertion.model_dump(mode="json")
        artifact = self._artifact(session_id, "resolve", "identity_decision", "系统明确自己知道什么、不知道什么", fixture["identity_decision_zh"], payload, entity_ids=state.get("entity_ids", []), assertion_ids=assertion_ids)
        return [artifact], [], changed

    def _stage_graph(self, session_id: str):
        state = self.state[session_id]
        fixture = state["fixture"]
        graph = self.graph_delta(session_id)
        payload: dict[str, Any] = {"nodes": [item.model_dump(mode="json") for item in graph["nodes"]], "edges": [item.model_dump(mode="json") for item in graph["edges"]], "statement_zh": "本次只展示新增局部图；完整历史仍在账本中。"}
        if state.get("subject_id"):
            view = self.service.working_view(GraphQuery(subject_entity_id=state["subject_id"], valid_at=utc_now(), known_at=utc_now(), view=GraphView.WORKING))
            payload["working_view"] = view.model_dump(mode="json")
        else:
            payload["working_view"] = {"selected": [], "reason_zh": "只有页面变化，没有足够证据改变人物职业状态。"}
        artifact = self._artifact(session_id, "graph", "graph_delta", "只看这次新增了什么", fixture["outcome_zh"], payload, source_version_ids=state.get("source_ids", []), evidence_span_ids=state.get("span_ids", []), entity_ids=state.get("entity_ids", []), assertion_ids=state.get("assertion_ids", []))
        events = [self._event("assertion.appended", session_id, {"assertion_ids": state.get("assertion_ids", [])})] if state.get("assertion_ids") else []
        return [artifact], events, state.get("assertion_ids", [])

    def _stage_deliver(self, session_id: str):
        state = self.state[session_id]
        fixture = state["fixture"]
        signal = None
        if state.get("assertion_id"):
            signal = self.service.create_signal_for_assertion(state["assertion_id"])
        if signal is None:
            signal = Signal(signal_type=SignalType.HOMEPAGE_CHANGE, title=fixture["signal_title_zh"], summary=fixture["signal_summary_zh"], score=58.0, confidence=1.0, entity_ids=[], triggering_assertion_ids=[], evidence_span_ids=state.get("span_ids", []), graph_path=[fixture["subject_name"], "homepage_change", fixture["source_uri"]], rule_id="source-version-changed", rule_version="people-intel-signals-v1")
            self.service.ledger.append_signal(signal)
        signal_artifact = self._artifact(session_id, "deliver", "signal_card", fixture["signal_title_zh"], fixture["signal_summary_zh"], {"score": signal.score, "confidence": signal.confidence, "graph_path": signal.graph_path, "alternative_assertion_ids": signal.alternative_assertion_ids}, evidence_span_ids=state.get("span_ids", []), entity_ids=state.get("entity_ids", []), assertion_ids=state.get("assertion_ids", []), signal_ids=[signal.signal_id])
        delivery = self._artifact(session_id, "deliver", "delivery_preview", "飞书里最终只需要看到这一条", fixture["delivery_zh"], {"channel": "feishu_preview", "title": fixture["signal_title_zh"], "body": fixture["delivery_zh"], "actions": ["查看证据", "查看完整时间线", "加入待裁决"]}, evidence_span_ids=state.get("span_ids", []), signal_ids=[signal.signal_id])
        return [signal_artifact, delivery], [self._event("signal.emitted", session_id, {"signal_id": signal.signal_id}), self._event("delivery.previewed", session_id)], [signal.signal_id]

    @staticmethod
    def _channel_reason(channel: str) -> str:
        return {"homepage": "检查本人公开状态与页面版本", "github": "发现代码仓库和提交", "arxiv": "发现论文与版本", "news": "发现创业和融资事件"}.get(channel, "补充公开证据")

    @staticmethod
    def _route(channel: str) -> str:
        return {"homepage": "qiaomu_generic", "github": "github_cli", "arxiv": "arxiv_api", "news": "exa_search"}.get(channel, "unconfigured")


__all__ = ["STAGES", "WORKFLOW_DEFINITION", "StoryController", "StoryFixtureStore"]
