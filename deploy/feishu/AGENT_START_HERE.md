# People Tracking Feishu @@VERSION@@

本包给已经运行在飞书上的 OpenClaw/Claude Agent 增加人员主页持续跟踪，不替换 Agent 或飞书配置。

严格顺序：

1. 在只含下载资产的临时目录确认同时存在同一 release、同一 basename 的 `people-tracking-feishu-@@VERSION@@.zip` 和 `people-tracking-feishu-@@VERSION@@.zip.sha256`；缺少任一文件就停止，不得导入或执行包内 Python。
2. Linux 运行 `sha256sum -c 'people-tracking-feishu-@@VERSION@@.zip.sha256'`；macOS 运行 `shasum -a 256 -c 'people-tracking-feishu-@@VERSION@@.zip.sha256'`。checksum 未明确成功就停止，也不得用包内脚本替代此外部校验。
3. checksum 成功后才解压到临时目录并进入唯一的 `people-tracking-feishu-@@VERSION@@` 目录。
4. 运行 `python3 verify_release.py`，只在 `ok=true` 时继续。
5. 运行 `python3 install_bundle.py --doctor` 与 `--dry-run`。
6. 缺依赖时只报告 `official_remediation`，等待用户批准；不得自动联网安装。
7. 用户确认安装后运行 `python3 install_bundle.py --apply`。
8. 重启或重载 Agent Skills。
9. 运行 `people-tracking-feishu onboarding --json`，把一次性问卷发到当前会话。
10. 未收到“确认启用”前，不导入、不建正式 Base、不注册调度、不发正式报告。
11. 收到确认后按 runtime 启动 bootstrap：OpenClaw 运行 `people-tracking-feishu bootstrap --confirmation '确认启用' --json`；`claude_lark_cli` 仅交互运行，必须显式增加 `--skip-schedule`。执行并回填所有 bridge action，反复恢复，直至返回 `status=ready`。不得停在安装成功、等待 bridge、导入预览或仅建库状态。
12. `bootstrap` 必须完成来源导入、缺主页分批补全、现有单表或 People/Sources 同步和预算内初始基线。只有 OpenClaw 注册隔离宿主 Agent automation；Claude + lark-cli 不得安装或宣称 launchd/systemd 裸调度可用。Scholar 延后项可返回 `baseline_degraded`，后续 Agent tick 或交互运行继续修复；不得为追求一次 ready 进行无界抓取。私有 `[TEST]` 写测仍只在用户单独确认后执行。
13. OpenClaw automation 必须以 `--session isolated --message ... --no-deliver` 启动调用 `$people-tracking` 的宿主 Agent 回合，并持续消费 source/master/review/delivery action。每轮先完整读取权威 Base，再以 record_id/version hash 增量对账并只扫描到期或受影响来源；Base 读取、分页或 bridge 校验失败时 fail closed，不得使用旧 SQLite 扫描或发用户报告。普通名单编辑不得触发全量。升级验收才运行 `scan --force-all --force-full-fetch --source-kind homepage`。
14. 报告前由当前 Agent 完整处理当前有上限的 `review-export` 快照并 `review-apply`，重复领取下一批直至没有请求。用户报告只写经批准、具体且重要的人员变化；所有错误、覆盖率、429 与待审状态只进入独立开发者报告。

信任边界：ZIP 与 `.sha256` 来自同一个 GitHub Release，只能校验传输/存储完整性，不能独立认证发布者。当前 release 不包含 Sigstore 或 GitHub Artifact Attestation；信任根仍是 GitHub HTTPS、仓库发布权限和维护者的 tag/Release 管理。若要求独立发布者认证，在通过另一可信渠道取得 digest 或签名证明前停止安装。

不要在聊天、文档、日志或命令参数中收集 secret 值。不要调用 DeepSeek 或另配模型 API。不要运行 `lark-cli config init --new` 或 `auth login`，除非用户明确批准。
