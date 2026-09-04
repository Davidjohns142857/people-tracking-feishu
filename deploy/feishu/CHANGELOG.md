# Changelog

## 0.9.0-portable.1 — 2026-09-04

- 现有飞书 Base 成为名单事实源与审核工作台：保留 record_id/version hash，完整分页后才判定
  removed，删除只做 tombstone，恢复复用历史，普通编辑只处理受影响记录。
- 修复 Mono 复数字段映射，并为 People/Sources 写入加入 schema discovery、read-before-write、
  人工/机器字段所有权和单表兼容；缺少的机器管理列安全增补，人工列不改名、不删除、不覆盖。
- 增加 charset 严格解码、U+FFFD/异常年份/arXiv 年份交叉验证、same→same 抑制、稳定 ID
  可解释 diff 和 parser anomaly 隔离；解析失败不推进候选或 baseline。
- Scholar 采用固定周内相位、小批请求预算和持久 host circuit；429/Retry-After 会停止当轮余下
  请求，跨重启等待，恢复后先做 canary。
- 用户报告与开发者报告使用独立事件账本、窗口、幂等键和游标。用户报告只列具体的重要人员
  变化；错误、覆盖率、429、待审核和修复状态只进入单独开发者报告。
- 删除 DeepSeek 运行路径和发布包依赖。所有拟进入用户报告的变化都以
  `people-tracking-agent-review-v1` 交给执行 Skill 的宿主 Agent，用自身 token 最终裁定；决定绑定
  review_snapshot_id/request_id/evidence_hash，拒绝空批、缺项、局部重放和混合快照，以配置上限
  拆分持久顺序批次并整批事务写入；defer 终结当前证据而不留下悬空 pending 事件。
- OpenClaw 无人值守调度改为隔离宿主 Agent automation，由 Agent 循环消费 source/master/review/
  delivery bridge；名单 checkpoint 绑定来源 bridge 身份且租期短于 15 分钟 tick，只允许同一工作流
  立即续跑，未完成总表回写时用户报告 fail closed。
- 总表写入先完成整批 schema/记录预检，再只更新机器字段并逐条回读；短时 live pre-read 有明确 TTL，
  避免进程间沿用陈旧快照。离线发布自测覆盖 Agent 裁定、双报告和权威 Base 的人工字段保护。
- 新增职位、任职单位、论文录用和重要奖项等优先建议发布；纯 UI/字符排版、same→same、异常年份、
  页面移除及年级自然递增等在生成审核包前即抑制。

## 0.8.1-portable.1 — 2026-08-19

- 修复本机 loopback proxy 使用 RFC 2544 Fake-IP DNS 时被公网 URL 安全门误拦截的问题；只允许 `198.18.0.0/15` 全量解析且目标域名未 bypass、匹配代理确为 loopback 的组合，普通私网与非 loopback 代理仍拒绝。
- LinkedIn HTTP 999、authwall/CAPTCHA 和 blocked-canary fallback 现在归类为匿名访问受限而非传输未知，旧基线继续受保护且不得据此推断离职。
- 隔离 `production` 与 `acceptance`/`validation` run：验收不再推进正式 baseline 或候选确认，同一 run 也不能重复计数。
- run 失败与部分失败现在有独立状态和错误审计；CLI 在 partial 时返回非零退出码。
- 主 URL 与每次 redirect 都执行公网 URL/DNS/IP 安全门；拒绝私网、凭据、token query、异常端口和超大响应。
- HTTP 200 SPA 空壳可转入已登记 fallback，软退役 source 不再被扫描，Homepage 功能 query 被保留且跟踪 query 被移除。
- Bridge 必须带一次性 nonce、预期 source refs 和绑定 action/payload 的请求哈希；错误记录容器、缺项或裸 `all_ok=true` 不再能让 bootstrap 误报 ready。
- 人工维护字段冲突时保留原值；严格扫描 0/0 失败；schedule 真正消费 hourly/daily/weekly cadence 并同步可见总库。
- 安装中途失败会如实报告已变更路径并尝试回滚；rollback 可重复调用且不会再次移走已恢复目标。
- Skill 入口禁写包内 bytecode；发布校验拒绝派生缓存和超出完整内容扫描上限的文件。安装顺序为包外 checksum 成功后才解压，再做包内 manifest 校验、doctor 与安装。

## 0.8.0-portable.1 — 2026-08-10

- 同步 8 月 10 日抓取内核：识别 HTTP 200 CAPTCHA/WAF、JS 空壳和零条目页面，失败不覆盖旧基线。
- 保留有意义的 `www`，清除页脚与 URL ID 迁移造成的虚假候选，并在 304 时用当前比较器重验旧候选。
- 新增周期性正文抓取、独立 `--force-full-fetch`、Homepage 最多一次瞬时故障重试及覆盖/错误率指标。
- 新增 exact `source_routes[]`，支持安全公开 fallback、软退役和替换；历史来源、观察、候选与基线不删除。
- 新增 Hugging Face public API、raw HTML、Westlake 静态 inline projection 和 exact-host CUHK TLS 兼容；不登录、不读 Cookie、不执行 JavaScript、不关闭证书验证。
- 严格扫描同时验收 enabled/planned/attempted/full-fetch 覆盖，失败返回非零退出码；meta refresh 重新执行公开 URL 安全校验，致命 TLS 协议错误不做无效重试。
- 版本改由根目录 `VERSION` 单一来源控制，release verifier 同时核对版本、manifest、文件哈希、secret 与本机路径。

## 0.7.0-portable.2 — 2026-08-04

- 双运行时便携安装、一次性 onboarding、可恢复 bootstrap、飞书 Base/Docs/消息桥、调度和离线合成验收。
