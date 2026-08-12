# Changelog

## 0.8.1-portable.1 — 2026-08-13

- 隔离 `production` 与 `acceptance`/`validation` run：验收不再推进正式 baseline 或候选确认，同一 run 也不能重复计数。
- run 失败与部分失败现在有独立状态和错误审计；CLI 在 partial 时返回非零退出码。
- 主 URL 与每次 redirect 都执行公网 URL/DNS/IP 安全门；拒绝私网、凭据、token query、异常端口和超大响应。
- HTTP 200 SPA 空壳可转入已登记 fallback，软退役 source 不再被扫描，Homepage 功能 query 被保留且跟踪 query 被移除。
- Bridge 必须带一次性 nonce、预期 source refs 和绑定 action/payload 的请求哈希；错误记录容器、缺项或裸 `all_ok=true` 不再能让 bootstrap 误报 ready。
- 人工维护字段冲突时保留原值；严格扫描 0/0 失败；schedule 真正消费 hourly/daily/weekly cadence 并同步可见总库。
- 安装中途失败会如实报告已变更路径并尝试回滚；rollback 可重复调用且不会再次移走已恢复目标。
- Skill 入口禁写包内 bytecode，验签器安全忽略安装器本就排除的派生缓存；按推荐顺序先 doctor 再验签/安装不再自我阻断。

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
