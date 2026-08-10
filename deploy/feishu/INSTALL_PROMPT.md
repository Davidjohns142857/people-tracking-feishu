请把我附带的 `people-tracking-feishu` 压缩包安装到你当前这个已经能正常运行飞书的 Claude/OpenClaw Agent 后台，并在安装后接管首次人员入库和持续跟踪初始化。

先在临时目录解压，依次运行 `python3 verify_release.py`、`python3 install_bundle.py --doctor` 和 `python3 install_bundle.py --dry-run`。如果三步均成功且没有依赖、权限、版本或路径冲突，不用停下来等我再次批准，直接运行 `python3 install_bundle.py --apply`。如果任一步失败，停止写入，只报告失败项和包内给出的官方固定版本修复命令；不要自行联网安装，不要改写现有飞书凭证、Claude/OpenClaw 配置、其他 Skills、cron 或服务。不要执行 `lark-cli config init --new`。不要让我在聊天中发送 API key、App Secret、Cookie 或密码。

安装成功后重载 Skill，运行 `people-tracking-feishu onboarding --json`，把返回的完整问卷作为一条飞书消息发给我。问卷只问一次，收集人员来源、字段映射、缺主页时自动检索或人工补全、可见总库、日期文档和消息输出、频率、时区及 API 使用方式。

若这是升级安装，先保留安装器创建的旧 Skill/launcher 备份和现有 SQLite。运行
`people-tracking-feishu source-routes --json` 预览已登记的 canonical、公开 fallback 与软退役动作；
只有预览无 exact-match/安全错误时才运行 `source-routes --apply --json`。不得删除旧来源、历史观察、
候选或确认基线。

收到我的回答后，把它规范化为 `people-tracking-config-v1`，运行只读 `source-probe`。OpenClaw 模式若返回 `agent_tool_bridge` action，立即使用当前已有的官方飞书工具读取相应 Base、文档或 Wiki，把真实 schema、记录数和读取结果回填；不要让我手工搬运数据。然后向我发送一条启用预览，明确列出来源数、读取到的人数、字段映射、缺主页人数、总库模式、输出目标、调度和 API 预算，并只询问我是否回复“确认启用”。

收到精确回复“确认启用”后，运行：

`people-tracking-feishu bootstrap --confirmation '确认启用' --json`

从这一步开始不要再把正常技术步骤交给我。持续处理命令返回的全部 action，并用 `--source-bridge-input`、`--anchor-bridge-input` 或 `--master-bridge-results` 恢复同一个 bootstrap，直到返回 `status=ready`：

1. 读取并导入全部配置来源，不只导入苹果或某一张表。
2. 用姓名和第二 ID 建档；中文名、拼音、英文名和有证据的 GitHub nickname 可以建立身份边，不要求主页显示名逐字相同。
3. 对缺 Homepage、Google Scholar、GitHub、LinkedIn 的人物，按配置决定分批自动检索或进入人工队列。自动检索必须结合别名、学校、方向和第二 ID，返回候选主页及证据链接；无法确定时保留人工审核，不得猜测。
4. 将有至少一个可靠主页的人写入内部 SQLite，并同步到已有或新建的 People/Sources 飞书总库；不得覆盖人工姓名、别名、学校、方向和阶段字段。
5. 对全部已入库来源运行首次全量扫描，建立可比较基线。异常、登录墙、限流和身份不一致不能被记成“无变化”，也不能覆盖可靠基线。
6. 注册 `people-tracking-feishu-` 命名空间调度，确认日报/周报文档与消息投递目标，最终报告 active 人数、pending 人数、来源数、基线成功/异常数、调度和回滚方法。

升级或来源修复完成后，用以下命令做一次严格 Homepage 复扫：

`people-tracking-feishu scan --force-all --force-full-fetch --source-kind homepage --homepage-retries 1 --homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json`

只有 enabled/planned/attempted/full-fetch 覆盖一致、错误率不超过门限且旧基线保护无违规，才发送成功摘要。
首次成功读取只称为建立 baseline。WAF/CAPTCHA、证书错误、HTTP 200 空壳和零条目页面都不能记为成功；
不登录、不读取 Cookie、不执行页面 JavaScript，也不关闭 TLS 证书或 hostname 验证。

如果用户启用了 DeepSeek，在 bootstrap 前说明会产生一次极小真实计费调用，并仅在我回复“确认运行DeepSeek烟测”后执行 smoke；失败时关闭 DeepSeek、保持确定性跟踪并明确报告。不要因为 DeepSeek 不可用而阻断确定性入库。

私有 `[TEST]` Base/文档/消息 E2E 可以先预览，但只有在我回复“确认运行私有沙箱E2E”后才能真正创建。任何时候都不得把 `waiting_for_*`、仅安装完成、仅生成问卷或仅创建数据库当作能力已经启用。
