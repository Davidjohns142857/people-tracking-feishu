# People Tracking Feishu

一个可版本化、可离线验签的飞书/Lark 人员公开主页跟踪 Skill。它面向已经运行在飞书中的
OpenClaw 或 Claude Agent，以 SQLite 保存确认基线、候选变化、来源健康和投递幂等状态。

仓库只包含脱敏源码、测试、Skill、安装器和构建工具，不包含真实人员名单、网页快照、运行
数据库、报告、Cookie 或凭证。GitHub Release 中的 ZIP 是飞书 Agent 的固定升级入口。

## Homepage 可靠性

`0.8.1-portable.1` 把严格复扫和状态安全所需能力带入便携运行时：

- 调度范围与正文下载分离；`--force-all` 不再冒充正文重抓，`--force-full-fetch` 才跳过 304。
- Homepage 只对瞬时传输错误和少数 5xx 最多重试一次；证书、WAF/CAPTCHA、403/404/410/429 不重试。
- HTTP 200 的挑战页、JS 空壳和零条目页面不得建立或覆盖基线。
- 304 使用当前比较器重验旧候选；`www` ID 迁移和页脚噪声不再被快速确认。
- `source_routes[]` 可登记匿名公开 fallback、软退役或替换来源；历史来源、观察、候选和基线不删除。
- 公开 route 拒绝 URL credentials、token query、私网/本机地址和非标准端口。
- RFC 2544 Fake-IP DNS 只在目标域名实际经 loopback HTTP(S) proxy 转发时放行；普通私网、混合解析和 proxy bypass 仍拒绝。
- LinkedIn HTTP 999、authwall/CAPTCHA 与 blocked-canary 统一记为匿名访问受限，保留旧基线且不推断任职变化。
- TLS 兼容只允许 exact-host 策略，并始终保留 CA 与 hostname 验证。
- `acceptance`/`validation` 扫描不推进正式 baseline 或候选，同一 run 不重复计数。
- Bridge 要求 nonce、预期 source refs 与哈希完整回填；裸 `all_ok=true` 不能将 bootstrap 标记为 ready。

完整原因与处理矩阵见
[`homepage-reliability.md`](packages/feishu/people-tracking/references/homepage-reliability.md)。

## 安装或升级

从 [GitHub Releases](https://github.com/Davidjohns142857/people-tracking-feishu/releases)
下载同一 tag 的 ZIP 与 `.sha256`，在临时目录解压后依次运行：

```bash
python3 verify_release.py
python3 install_bundle.py --doctor
python3 install_bundle.py --dry-run
python3 install_bundle.py --apply
```

安装器不会联网安装外部依赖，不会改写现有飞书/OpenClaw/Claude 配置；同名 Skill 和 launcher
会先备份。依赖或权限不满足时，apply 在写入前停止。

升级后先预览来源维护，再做严格 Homepage 复扫：

```bash
people-tracking-feishu source-routes --json
people-tracking-feishu source-routes --apply --json

people-tracking-feishu scan --force-all --force-full-fetch \
  --source-kind homepage --homepage-retries 1 \
  --homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json
```

首次健康读取只称为建立 baseline。覆盖不足或错误率超过门限时，运行结果保留用于审计，但不会
更新可投递的 `last_tracker_run_id`。

飞书中的旧附件不会自动更新。目标 Agent 必须从固定 tag 拉取新包，核对 SHA-256，执行
verify/doctor/dry-run/apply 并重载 Skill。

## 开发与发布

版本唯一来源是根目录 [`VERSION`](VERSION)，格式为 `MAJOR.MINOR.PATCH-portable.N`。每个
Git tag `v<VERSION>` 与 Release 资产不可变；同一版本号不得生成不同内容。

```bash
python3 scripts/validate_portable_skill.py
python3 -m pytest -q tests
python3 scripts/build_feishu_release.py
```

仓库提供 [GitHub Actions 模板](ci-templates/github-actions/)，用于运行定向回归、两次确定性
构建、manifest/secret/local-path 验签和包内无网络合成测试。启用模板需要维护者的 GitHub
凭据具有 `workflow` scope；没有该权限时继续使用同一组本地发布门和手工 Release。
发布规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界见 [SECURITY.md](SECURITY.md)。
