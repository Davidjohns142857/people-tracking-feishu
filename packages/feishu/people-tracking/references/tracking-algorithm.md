# 跟踪算法

每个来源只保存确认语义 manifest、至多一个候选 manifest、稳定条目 ID、hash、紧凑差分、ETag/Last-Modified、质量和健康状态；不保存完整历史 HTML。

处理顺序：

1. 平时用条件请求或平台稳定接口获取页面；每个来源按 `full_fetch_interval_days` 周期性读取正文。严格验收显式使用 `--force-full-fetch`，不得把 `--force-all` 或 304 当成正文重抓。
2. 先过 authwall/captcha/空壳页、canonical、覆盖率、关键分区和身份绑定门。
3. 抽取身份区、任职/教育、论文、项目、奖项、仓库等稳定条目。
4. 对稳定 ID 和归一化文本计算 manifest；忽略导航、cookie、顺序抖动和 Scholar 引用排序噪声。
5. 首次健康观察建立 baseline，只能表述为“建立基线”。普通变化先成为 candidate；第二次一致观察才成为 changed。可靠 LinkedIn 公开经历变化按既有质量门执行。
6. 304 必须用当前比较器重验已有候选；候选只剩 `www` ID 迁移、页脚或其他新噪声时清除，不得快速确认。来源失败、部分页面和 search-index 不得覆盖旧基线。
7. Homepage 的 status 0/408/425/500/502/503/504 最多重试一次并使用稳定退避；证书、WAF/CAPTCHA、authwall 和其他确定性错误不重试。

来源 `source_routes[]` 只登记匿名公开 URL。`configure` 只更新抓取路线；`soft_retire` 只关闭后续调度；`replace` 新增经身份核验的来源并软退役旧来源。三者均不得删除历史 observation、candidate 或 baseline。

人物以姓名 + 第二 ID 建档，用稳定主页 ID 交叉验证。中文、拼音、英文顺序和已确认 nickname 建立别名边；GitHub nickname 不要求与正式姓名逐字一致。冲突进入审核，不自动跨人合并。

OpenClaw 无人值守由隔离宿主 Agent 回合执行，不调用另配模型 API。所有通过解析、身份和完整性硬门且可能进入用户报告的紧凑差分都进入
`people-tracking-agent-review-v1`，由当前执行本 Skill 的 Agent 使用自身 token 最终裁定。请求必须
绑定事件、完整 `review_snapshot_id`、`request_id` 和 `evidence_hash`；回填只能选择
`publish`、`suppress`、`defer`，不能
修改原始 delta 或越过解析、身份、完整性硬门。
审核积压按 `agent_review.batch_size` 拆成持久的精确快照；当前快照全部原子提交后才生成下一批。
`defer` 表示当前证据不发布并保留审计，后续新证据会生成新事件重新裁定。

确定性重要性规则在生成审核包前抑制纯 UI、排版、same→same、不可解释年份和年级自然递增；
新增论文、明确录用、职位/单位变化和重要奖项只作为优先发布建议，仍须宿主 Agent 回填 `publish`
决定后才能进入用户报告。禁止在请求中包含完整页面、Cookie、headers、secret、内部路径或无关人物内容。
