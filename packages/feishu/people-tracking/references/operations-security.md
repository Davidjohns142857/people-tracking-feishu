# 运行与安全

安装位置：

- release：`~/.local/share/people-tracking-feishu/releases/<version>`
- config：`~/.config/people-tracking-feishu/`
- state：`~/.local/state/people-tracking-feishu/`
- launcher：`~/.local/bin/people-tracking-feishu`

配置、数据库、报告、备份和 secret 引用目录使用 0700；文件使用 0600。安装器不得打开飞书凭证文件，不得执行全局 editable pip 安装。

任务名统一以 `people-tracking-feishu-` 开头。发现同名 OpenClaw cron、launchd plist 或 systemd unit 时停止，不覆盖。macOS/Linux 本地后台每 15 分钟运行一次内部 tick，由 SQLite 判断周扫、日报、周报是否到期。

投递幂等键为 `tracker_run_id + output_kind + period`。若文档已创建而消息失败，记录 `document_created`，重试只发消息。只有文档与消息都完成才标记 `completed`。

报告必须披露来源失败、候选和未确认变化。以下措辞禁止：

- 把首次 baseline 写成“没有变化”。
- 把 403/429/authwall/验证码写成“主页无更新”。
- 把部分渠道成功写成“全网扫描完成”。

Homepage 抓取只允许匿名公开访问：不登录、不读取 Cookie、联系人、消息或浏览器 Profile，
不破解 CAPTCHA，不执行页面 JavaScript。公开 alternate route 必须预先登记且通过协议、host、
端口和凭证校验；禁止 localhost、私网 literal IP、URL credentials、token query 和非标准端口。

TLS 兼容只能在代码内 exact-host allowlist 上增加精确 cipher，同时保持 `CERT_REQUIRED` 与
hostname 验证。证书过期、hostname mismatch 或链错误不得用 `-k`、全局降级或关闭验证绕过；
应更换经身份闭环核验的 HTTPS 来源，或等待站方修复。

严格复扫必须同时报告 enabled、planned、attempted、full-fetch planned/attempted、retry、
健康分类和错误率。覆盖不足或错误率高于门限时不得发送“扫描成功”摘要。

回滚只恢复本包建立的 Skill/launcher/service 备份。保留 SQLite、配置和报告，不删除用户数据。清理沙箱或 release 必须另行确认。
