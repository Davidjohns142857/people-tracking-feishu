# Changelog

## 0.8.0-portable.1 — 2026-08-10

- 同步 8 月 10 日抓取内核：识别 HTTP 200 CAPTCHA/WAF、JS 空壳和零条目页面，失败不覆盖旧基线。
- 保留有意义的 `www`，清除页脚与 URL ID 迁移造成的虚假候选，并在 304 时用当前比较器重验旧候选。
- 新增周期性正文抓取、独立 `--force-full-fetch`、Homepage 最多一次瞬时故障重试及覆盖/错误率指标。
- 新增 exact `source_routes[]`，支持安全公开 fallback、软退役和替换；历史来源、观察、候选与基线不删除。
- 新增 Hugging Face public API、raw HTML、Westlake 静态 inline projection 和 exact-host CUHK TLS 兼容；不登录、不读 Cookie、不执行 JavaScript、不关闭证书验证。
- 版本改由根目录 `VERSION` 单一来源控制，release verifier 同时核对版本、manifest、文件哈希、secret 与本机路径。

## 0.7.0-portable.2 — 2026-08-04

- 双运行时便携安装、一次性 onboarding、可恢复 bootstrap、飞书 Base/Docs/消息桥、调度和离线合成验收。
