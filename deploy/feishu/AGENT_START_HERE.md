# People Tracking Feishu @@VERSION@@

本包给已经运行在飞书上的 OpenClaw/Claude Agent 增加人员主页持续跟踪，不替换 Agent 或飞书配置。

严格顺序：

1. 在只含下载资产的临时目录确认同时存在同一 release、同一 basename 的 `people-tracking-feishu-@@VERSION@@.zip` 和 `people-tracking-feishu-@@VERSION@@.zip.sha256`；缺少任一文件就停止，不得导入或执行包内 Python。
2. Linux 运行 `sha256sum -c 'people-tracking-feishu-@@VERSION@@.zip.sha256'`；macOS 运行 `shasum -a 256 -c 'people-tracking-feishu-@@VERSION@@.zip.sha256'`。checksum 未明确成功就停止，也不得用包内脚本替代此外部校验。
3. checksum 成功后才解压到临时目录并进入唯一的 `people-tracking-feishu-@@VERSION@@` 目录。
4. 运行 `python3 verify_release.py`，只在 `ok=true` 时继续。
5. 如用户要求升级正在运行的系统，运行 `python3 install_bundle.py --upgrade --doctor` 与
   `--upgrade --dry-run`。一般安装可省略 `--upgrade`，安装器会识别本机已有配置。
6. 检查 `plan.ready_to_apply=true`。遇到实际依赖或权限问题，只报告具体错误，保持原配置与任务。
7. 当前会话已经授权安装或升级时，直接用同一模式运行 `python3 install_bundle.py --upgrade --apply`
   （首次安装省略 `--upgrade`）。已有配置先在隔离数据库副本验证兼容性。
8. 重启或重载 Agent Skills。
9. 若 `result.installation_phase=upgrade`，直接执行 `result.resume_argv`（已绑定原路径的
   `people-tracking-feishu upgrade-resume --json`）。
   消费所有 developer 投递动作并回填真实回执；如果回执只写本地，在当前升级私聊回复返回的
   `markdown`。检查 `scheduler.status` 和同一 job ID 的回读。旧任务已存在时沿用它的
   日期、时区、开关、Agent 与收件策略，只刷新动作处理说明。已有配置、SQLite 与游标原样接续，
   不运行 onboarding、bootstrap、重建名单、强制复扫或重新登录，无需问用户任何新设置。
   单个来源、桥接、搜索或 debug 投递等待时继续按原规则修复，不能据此回退版本。
   只有 `installation_phase=fresh` 才继续以下首次启用流程；
   `configured_inactive` 保持已有暂停状态。
10. **仅首次配置**，运行 `people-tracking-feishu onboarding --json` 并发送一次问卷。
11. **仅首次配置**，未收到“确认启用”前不导入、不建正式 Base、不注册调度。确认后按原 runtime 启动 bootstrap：
    OpenClaw 运行 `people-tracking-feishu bootstrap --confirmation '确认启用' --json`；
    `claude_lark_cli` 必须显式增加 `--skip-schedule`。处理全部 bridge action，恢复至 `status=ready`。
12. `bootstrap` 必须完成来源导入、缺主页分批补全、现有单表或 People/Sources 同步和预算内初始基线。只有 OpenClaw 注册隔离宿主 Agent automation；Claude + lark-cli 不得安装或宣称 launchd/systemd 裸调度可用。Scholar 延后项可返回 `baseline_degraded`，后续 Agent tick 或交互运行继续修复；不得为追求一次 ready 进行无界抓取。私有 `[TEST]` 写测仍只在用户单独确认后执行。
13. OpenClaw automation 必须以 `--session isolated --message ... --no-deliver` 启动调用 `$people-tracking` 的宿主 Agent 回合，并持续消费 source/master/review/delivery action。每轮先完整读取权威 Base，再以 record_id/version hash 增量对账并只扫描到期或受影响来源；Base 读取、分页或 bridge 校验失败时 fail closed，不得使用旧 SQLite 扫描或发用户报告。常规升级和名单编辑沿用到期扫描。
14. 报告前由当前 Agent 完整处理当前有上限的 `review-export` 快照并 `review-apply`，重复领取下一批直至没有请求。用户报告只写经批准、具体且重要的人员变化；所有错误、覆盖率、429 与待审状态只进入独立开发者报告。
15. 周度`execution_agent_career_search`不是普通diff审核：读取`payload_file`，依照Skill的`references/career-discovery.md`使用已有公开搜索工具核实本批活跃人物的创业与任职动态，再以`career-search-apply --input FILE --json`回填并恢复tick。区分事件、公告与观察时间；搜索摘要不替代原始来源；未完成搜索不阻断其他已批准事实的周报。升级后重载Skill并检查已保存automation message支持该action，按原job ID维护，不新增重复cron。
16. 需要生产证据时，已校验解压目录含`scripts/export_coverage_audit.py`和`scripts/export_discovery_audit.py`。二者仅用标准库只读既有数据库，显式传`--database`、带时区的`--since`/`--until`及新的`--output`文件；不导入runtime、不迁移、不联网、不扫描或投递。匿名覆盖与具体事实取证的内容权限不同，不要把旧本地库当目标机生产库。

信任边界：ZIP 与 `.sha256` 来自同一个 GitHub Release，只能校验传输/存储完整性，不能独立认证发布者。当前 release 不包含 Sigstore 或 GitHub Artifact Attestation；信任根仍是 GitHub HTTPS、仓库发布权限和维护者的 tag/Release 管理。若要求独立发布者认证，在通过另一可信渠道取得 digest 或签名证明前停止安装。

不要在聊天、文档、日志或命令参数中收集 secret 值。不要调用 DeepSeek 或另配模型 API。不要运行 `lark-cli config init --new` 或 `auth login`，除非用户明确批准。
