# Security

请通过 GitHub 私密漏洞报告或仓库所有者的私有渠道报告安全问题；不要在公开 issue 中粘贴
Feishu App Secret、API key、Cookie、人员名单或运行日志。

本项目的硬边界：

- 只抓取登记 URL 的匿名公开信息，不登录，不读取 Cookie、联系人、消息或浏览器 Profile。
- 不破解 CAPTCHA/WAF，不执行远端 JavaScript，不跟随未登记的凭证化 fallback。
- 不关闭 TLS 证书或 hostname 验证；证书真实失效时更换经身份核验的来源。
- 失败、部分页面、search-index 和低质量页面不能覆盖确认基线。
- release 构建拒绝 secret、本机绝对路径、数据库、真实数据文件和 symlink。
- 安装前必须在包外用同名 `.sha256` 校验 ZIP，成功后才解压或执行包内 Python；随后再运行包内 manifest 校验与 dry-run。
- ZIP 与 `.sha256` 来自同一个 GitHub Release，只能校验传输/存储完整性，不能独立认证发布者；当前模板不提供 Sigstore 或 GitHub Artifact Attestation。
- 安装器不联网安装依赖，不覆盖现有 Agent/飞书配置。
