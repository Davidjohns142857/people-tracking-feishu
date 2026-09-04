# 运行与安全

安装位置：

- release：`~/.local/share/people-tracking-feishu/releases/<version>`
- config：`~/.config/people-tracking-feishu/`
- state：`~/.local/state/people-tracking-feishu/`
- launcher：`~/.local/bin/people-tracking-feishu`

配置、数据库、报告、备份和 secret 引用目录使用 0700；文件使用 0600。安装器不得打开飞书凭证文件，不得执行全局 editable pip 安装。

任务名统一以 `people-tracking-feishu-` 开头。唯一受支持的无人值守方式是 OpenClaw 隔离宿主
Agent automation：每 15 分钟通过 `--session isolated --message ... --no-deliver` 启动一个调用
`$people-tracking` 的 Agent 回合。它先完整读取名单并增量对账，再扫描已到期来源，并持续消费
source/master/review/delivery action；不能把 bridge 排队当作同步完成。发现同名 OpenClaw
automation 时停止，不覆盖。`claude_lark_cli` 仅支持交互执行并要求 bootstrap 显式
`--skip-schedule`；不得安装 launchd/systemd 裸 tick，因为它无法消费 bridge 或使用宿主 token
完成最终审核。

用户和开发者投递各自使用 `audience + output_kind + time window` 幂等键及独立游标。若文档已创建而消息失败，记录 `document_created`，重试只发消息。一个 audience 的结果不得推进或冒充另一个 audience 完成。

开发者报告必须披露来源失败、候选和未确认变化；用户报告只含已批准的重要人员事实。以下措辞禁止：

- 把首次 baseline 写成“没有变化”。
- 把 403/429/authwall/验证码写成“主页无更新”。
- 把部分渠道成功写成“全网扫描完成”。
- 在用户报告中写 parser、错误码、覆盖率、候选次数或修复流水。

Homepage 抓取只允许匿名公开访问：不登录、不读取 Cookie、联系人、消息或浏览器 Profile，
不破解 CAPTCHA，不执行页面 JavaScript。公开 alternate route 必须预先登记且通过协议、host、
端口和凭证校验；禁止 localhost、私网 literal IP、URL credentials、token query 和非标准端口。

TLS 兼容只能在代码内 exact-host allowlist 上增加精确 cipher，同时保持 `CERT_REQUIRED` 与
hostname 验证。证书过期、hostname mismatch 或链错误不得用 `-k`、全局降级或关闭验证绕过；
应更换经身份闭环核验的 HTTPS 来源，或等待站方修复。

严格复扫必须同时报告 enabled、planned、attempted、full-fetch planned/attempted、retry、
健康分类和错误率。覆盖不足或错误率高于门限时不得发送“扫描成功”摘要。

回滚只恢复本包建立的 Skill/launcher/service 备份。保留 SQLite、配置和报告，不删除用户数据。回滚完成后 `install-state.json` 标记为 `rolled_back`；重复回滚返回幂等 replay，不再次移动文件。安装中途失败必须报告真实 `mutated` 和 mutation 路径，并自动尽可能恢复 Skill/launcher。清理沙箱或 release 必须另行确认。
