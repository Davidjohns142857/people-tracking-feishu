# 发布安装与测试报告

版本：`@@VERSION@@`

## 已完成验收

| 验收面 | 结果 |
| --- | --- |
| 全项目 pytest | 339/341 通过；2 项既有 Mono roster 输入 fixture 断言失败，与本版飞书/抓取修改无关 |
| 便携层与抓取内核定向测试 | 73/73 通过 |
| 包内离线 self-test | 8/8 通过；0 网络、0 billed token、0 真实人物 |
| Skill 结构 | `quick_validate.py` 通过 |
| release manifest | 87 个非 manifest 文件逐一 SHA-256 匹配 |
| release 安全扫描 | 0 secret、0 真实名单、0 数据库、0 本机绝对路径、0 symlink |
| 安装隔离 | 临时 HOME 完成 apply、重复 apply、同名 Skill/launcher 备份与 rollback |
| 无侵入性 | 预置 Claude settings hash 在 dry-run、拒绝 apply、成功 apply、重复 apply、rollback 后均不变 |
| 缺依赖处理 | apply 在写入前拒绝；不创建 config/state/release，不联网安装 |
| fake lark-cli | argv 无 shell、写前 dry-run、JSON `ok=true`、200 条分页、Base/Docs/消息回读与幂等通过 |
| Base 数据结构 | `People`/`Sources` 双表创建、记录映射和二次 upsert 链路通过 |
| 首次启用管线 | `bootstrap` 直接导入、来源 bridge、缺主页证据回填、总库 bridge、全量基线和幂等恢复通过 |
| macOS/Linux | namespaced launchd/systemd 生成物通过；真实 systemd/launchctl 注册留给目标机确认后执行 |

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

## 目标机最终门

目标 Agent 仍必须完成：release 验签、doctor/dry-run、一次性问卷、只读 source probe、“确认启用”、`bootstrap status=ready`、私有 `[TEST]` E2E 和真实回读。安装验证成功时可按交接 Prompt 直接 apply；首次入库之后不得停在任何 `waiting_for_*` bridge 状态。未得到对象 token、文档 URL、message ID 与 readback 前，不得把真实飞书 E2E 标为通过。

安全边界：包不含真实名单、历史快照、数据库、secret、本机路径；安装器不联网安装外部依赖，不改写现有飞书/OpenClaw/Claude 配置，不在首次确认前注册调度。
