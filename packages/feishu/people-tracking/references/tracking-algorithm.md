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

DeepSeek V4 Flash 只处理：

- `ambiguous_review`：健康、身份匹配、质量 ≥0.70、已有基线、紧凑差分且来源为 Homepage/Scholar/LinkedIn；结果只作 advisory。
- `confirmed_summary`：确定性算法已经 confirmed；模型只生成中文摘要，不得改状态、score、delta 或确认计数。

禁止发送完整页面、Cookie、headers、key、内部路径或无关人物内容。预算按来源/人物/日调用与 token 原子扣减，失败关闭。
