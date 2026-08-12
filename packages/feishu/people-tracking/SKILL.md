---
name: people-tracking
description: Configure, import, monitor, and report a portable Feishu/Lark people-tracking database for OpenClaw or Claude agents. Use when a user asks to onboard researcher/talent lists from Base, Docs, Wiki, controlled files, or People Intel; map people to homepage, Google Scholar, GitHub, or LinkedIn; detect profile changes; run DeepSeek V4 Flash only for eligible ambiguity or confirmed-summary wording; schedule scans; or publish dated Feishu reports and short messages.
---

# Feishu 人员跟踪

使用包内确定性 CLI 管理配置、状态、网页检测和投递幂等。使用官方 OpenClaw 飞书插件或 `lark-cli` 完成飞书 I/O；不得自行拼接 Open API、SQL 或 shell。

## 入口与硬边界

1. 先运行 `python3 scripts/skill_entry.py doctor --json`。
2. 只调用 `scripts/skill_entry.py` 或已安装的 `people-tracking-feishu`；所有参数使用 argv 数组，不使用 `shell=True`。
3. 不读取、回显或要求用户在聊天中发送 API key、App Secret、Cookie、密码。只收集 0600 key 文件或 secret-manager 引用。
4. 不执行 `lark-cli config init --new`、`lark-cli auth login`、权限审批或浏览器授权，除非用户明确同意。缺权限时只报告 schema/错误返回的最小 scope。
5. 写飞书前必须先执行对应 dry-run。不得覆盖现有 Base 人工字段、Agent 配置、其他 Skill、cron 或服务。
6. 只把配置从 `draft → validated → enabled` 单向推进。收到精确的“确认启用”前，不导入、不创建正式 Base、不注册调度、不发送正式报告。
7. 当前会话消息必须走 Agent 的飞书工具桥；固定 `chat_id`/`open_id` 才允许 CLI 直发。把实际 `doc_url` 和 `message_id` 回填给 CLI，保证重试不重复投递。

## 首次配置

运行：

```bash
python3 scripts/skill_entry.py onboarding --json
```

把返回的完整问卷作为一条消息发送，不拆成多轮。收到答案后读取 [配置契约](references/configuration-schema.md)，生成不含 secret 值的 JSON 文件，再运行：

```bash
python3 scripts/skill_entry.py onboarding --answers /path/to/answers.json --json
python3 scripts/skill_entry.py source-probe --json
```

若 `source-probe` 返回 `agent_tool_bridge` actions，严格按 action 使用官方飞书插件做只读读取，将实际 schema/权限/记录数写成 bridge result，再运行 `source-probe --bridge-results <file>`。原样回传 action 中的 nonce、完整 expected refs 和 hash；缺项、重复或未知 ref 必须停止。不得在未执行时伪造 `all_ok=true`。

向用户汇报来源、人物记录数、字段映射、输出目标、时区、调度、API 预算和失败项。

若启用 DeepSeek，先说明将产生一次极小真实计费调用并取得同意，再运行：

```bash
python3 scripts/skill_entry.py doctor --deepseek-smoke \
  --confirmation '确认运行DeepSeek烟测' --json
```

Smoke 未通过时将配置改为确定性模式并重新验证；不得声称 DeepSeek 已启用。

用户回复“确认启用”后，不要把后续步骤拆回给用户；运行可恢复的首次初始化编排：

```bash
python3 scripts/skill_entry.py bootstrap --confirmation '确认启用' --json
```

持续执行 `bootstrap` 直至 `status=ready`。它依次导入全部来源、处理缺主页队列、同步
People/Sources 可见总库、为全部已入库来源建立初始基线，最后注册命名空间调度。若返回
`agent_tool_bridge` action，立即用宿主已有飞书或网页工具执行，将结果写入 mode-0600
JSON，并用对应的 `--source-bridge-input`、`--anchor-bridge-input` 或
`--master-bridge-results` 恢复同一个命令。不要要求用户重复配置，也不要把
`waiting_for_*` 当成初始化完成。

缺四类主页且配置为 `agent_discovery` 时，按 action 中的有界批次检索。使用姓名、第二
ID、别名、学校和方向交叉验证；拼音/英文名及有证据的 GitHub nickname 可以建立身份边，
不要求显示名逐字相同。返回候选主页与证据链接；无法确定的条目进入人工队列，不得猜测。
首次基线的身份门会再次复核候选，异常页面不能覆盖可靠状态。

## 同步、扫描与投递

后续新增或变更名单时，先预览同步，再写入：

```bash
python3 scripts/skill_entry.py sync --json
python3 scripts/skill_entry.py sync --apply --json
```

若某个 Homepage 需要新 canonical URL、公开静态 fallback 或软退役，先预览再应用
`source_routes[]`。历史来源、观察、候选和确认基线只保留，不删除：

```bash
python3 scripts/skill_entry.py source-routes --json
python3 scripts/skill_entry.py source-routes --apply --json
```

无四类必要主页的人进入 intake issue，不进入主动跟踪。人工 Base 字段为权威；incoming 值与非空人工字段冲突时只写 conflict，不覆盖人工值。机器仅维护 SQLite 中的指纹、候选、来源健康、审计和投递状态。中文名、拼音、英文名和已确认 nickname 作为身份边，不要求 GitHub 显示名逐字相同。

执行到期扫描：

```bash
python3 scripts/skill_entry.py scan --json
```

升级、修复来源或做严格验收时，调度范围与正文下载必须分别显式控制：

```bash
python3 scripts/skill_entry.py scan --force-all --force-full-fetch \
  --source-kind homepage --homepage-retries 1 \
  --homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json
```

`--force-all` 只扩大到全部已启用来源；`--force-full-fetch` 才禁用条件请求并读取正文。
Homepage 只对瞬时传输错误和少数 5xx 最多重试一次。403/404/410/429、WAF/CAPTCHA、
authwall 与证书错误不重试，也不覆盖旧基线。HTTP 200 但零条目、JS 空壳或挑战页不算成功。
首次健康读取只称为建立 baseline。
严格 `--force-all` 扫描若 enabled/observed 为 0 必须失败。后台扫描成功后还要检查
`sync_visible_master`：若返回 bridge action，完成全部 expected refs 后回填；不能把排队
写成飞书总库已经同步。

必须区分 `unchanged`、`candidate`、`changed`、`source_issue_pending` 和 `source_issue`。来源失败不能覆盖旧基线，也不能表述为“没有变化”。DeepSeek 仅接收紧凑差分：`ambiguous_review` 只给 advisory，`confirmed_summary` 只改写措辞。

先预览日报或周报，再投递：

```bash
python3 scripts/skill_entry.py digest --kind daily --json
python3 scripts/skill_entry.py digest --kind daily --apply --json
```

若返回 bridge actions，用官方工具创建日期文档和当前会话消息，并以同一个 `delivery_key` 回填 `doc_url`、`doc_token`、`message_id`。投递结果只展示变化、新内容、必要上下文和原链接，不展示内部算法细节。

## 私有沙箱验收

先运行预览；只使用 `[TEST]` 对象和合成人物：

```bash
python3 scripts/skill_entry.py sandbox-e2e --json
```

用户确认后运行：

```bash
python3 scripts/skill_entry.py sandbox-e2e --apply \
  --confirmation '确认运行私有沙箱E2E' --json
```

验证 `baseline → candidate → changed`、Base/文档回读和消息幂等。结束后列出对象 token，询问“保留测试对象”或“确认清理测试对象”；未获清理确认不得删除。

## 按需读取

- 配置字段与一次性问卷答案：读取 [配置契约](references/configuration-schema.md)。
- `lark-cli`、OpenClaw bridge、Base/Docs/消息命令：读取 [飞书 I/O 契约](references/feishu-io-contract.md)。
- 指纹、候选确认、LinkedIn 与 DeepSeek 门：读取 [跟踪算法](references/tracking-algorithm.md)。
- Homepage 失败归因、公开 fallback、严格复扫和验收：读取 [Homepage 可靠性](references/homepage-reliability.md)。
- 安全、调度、幂等、回滚和故障披露：读取 [运行与安全](references/operations-security.md)。
