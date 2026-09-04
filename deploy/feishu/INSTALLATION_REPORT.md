# 发布安装与测试报告

版本：`@@VERSION@@`

## 已完成验收

| 验收面 | 结果 |
| --- | --- |
| 全项目 pytest | 本 PR 在本地发布门重新运行；不在静态文档固化易失效的测试数 |
| 便携层与抓取内核定向测试 | 本地发布门执行；`ci-templates/github-actions/ci.yml` 仅为可选模板，仓库当前未安装 GitHub Actions workflow |
| 包内离线 self-test | 每次构建从 ZIP 解包重跑；0 网络、0 billed token、0 真实人物 |
| Skill 结构 | `quick_validate.py` 通过 |
| release manifest | 当次 ZIP 内所有非 manifest 文件逐一 SHA-256 匹配 |
| release 安全扫描 | 0 secret、0 真实名单、0 数据库、0 本机绝对路径、0 symlink |
| 安装隔离 | 临时 HOME 完成 apply、重复 apply、同名 Skill/launcher 备份与 rollback |
| 无侵入性 | 预置 Claude settings hash 在 dry-run、拒绝 apply、成功 apply、重复 apply、rollback 后均不变 |
| 缺依赖处理 | apply 在写入前拒绝；不创建 config/state/release，不联网安装 |
| fake lark-cli | argv 无 shell、写前 dry-run、JSON `ok=true`、200 条分页、Base/Docs/消息回读与幂等通过 |
| Base 数据结构 | 现有单表及 `People`/`Sources` 双表的 schema discovery、read-before-write、增量 upsert 和 tombstone 链路通过 |
| 首次启用管线 | `bootstrap` 直接导入、来源 bridge、缺主页证据回填、总库 bridge、预算内基线、`baseline_degraded` 后续修复和幂等恢复通过 |
| 自动调度 | OpenClaw 注册隔离宿主 Agent automation（`--session isolated --message ... --no-deliver`）；Claude + lark-cli 明确仅交互运行且不安装 launchd/systemd |

## 当前构建机环境结论

- Python 3.12 与 Node 24 可用。
- 本机 `lark-cli` 为 1.0.44，低于发布固定的 1.0.82；官方 `lark-*` Skills 未安装。
- 安装器在该环境正确报告不满足依赖并拒绝 apply，未升级或改动本机飞书配置。
- 当前只有 bot 身份且没有本轮真实飞书写测授权，因此没有创建 Base、文档或消息，也没有触发用户登录。

## 本版 Homepage 可靠性门

- 周期性正文重抓与显式 `--force-full-fetch` 和调度范围分离。
- 只对传输错误及少数 5xx 最多重试一次；WAF/CAPTCHA、403/404/410/429 和证书错误不重试。
- HTTP 200 空壳、挑战页和零条目页面不得建立或覆盖基线。
- 只允许登记的匿名公开 alternate route；不登录、不读 Cookie，不执行页面 JavaScript。
- 304 候选必须使用当前比较器重验；旧噪声候选不得因条件请求被快速确认。
- 失败报告区分访问受限、失效、限流、传输错误、内容退化和身份复核。

## 本版名单与报告门

- 飞书 record_id/version hash 驱动增量对账；不完整分页不得推断删除，removed 只做 tombstone。
- 已有 People 表是权威名单；每个 tick 完整读取后才扫描变化工作集，主表读取失败或 bridge 未闭环时 fail closed，不得沿用旧 SQLite 发报告。
- schema discovery 使用真实字段元数据；只安全增补机器管理列，稳定 key 类型不兼容时停止写入，人工列不改名、不删除、不覆盖。
- Scholar 固定周内相位、小批预算、429 持久 circuit 和恢复 canary 均可离线回放。
- parser anomaly 不推进候选或 baseline；旧可靠状态在异常后保持不变。
- 所有拟公开变化都由宿主 Agent 通过 `people-tracking-agent-review-v1` 最终裁定，不读取 DeepSeek key 或调用外部评审 API；整批决定在校验 request/evidence hash 后原子提交。
- 用户/开发者报告有独立窗口、幂等键和游标；用户报告不含错误、覆盖率或内部状态。

## 目标机最终门

目标 Agent 仍必须完成：在解压或执行包内 Python 前用包外同名 `.sha256` 校验 ZIP、解压后运行包内 manifest 校验、doctor/dry-run、一次性问卷、只读 source probe、“确认启用”、`bootstrap status=ready`、私有 `[TEST]` E2E 和真实回读。OpenClaw 还必须验证隔离宿主 Agent automation 能连续消费 source/master/review/delivery action；Claude + lark-cli 必须显式 `--skip-schedule` 并向用户说明仅交互运行。安装验证成功时可按交接 Prompt 直接 apply；首次入库之后不得停在任何 `waiting_for_*` bridge 状态。未得到对象 token、文档 URL、message ID 与 readback 前，不得把真实飞书 E2E 标为通过。

安全边界：包不含真实名单、历史快照、数据库、secret、本机路径；安装器不联网安装外部依赖，不改写现有飞书/OpenClaw/Claude 配置，不在首次确认前注册调度。同一 GitHub Release 中的 ZIP 与 `.sha256` 只能证明传输/存储完整性，不能独立认证发布者；当前模板不包含 Sigstore 或 GitHub Artifact Attestation。
