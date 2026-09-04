# People Tracking Feishu

一个可版本化、可离线校验完整性的飞书/Lark 人员公开主页跟踪 Skill。它面向已经运行在飞书中的
OpenClaw 或 Claude Agent，以 SQLite 保存确认基线、候选变化、来源健康和投递幂等状态。

仓库只包含脱敏源码、测试、Skill、安装器和构建工具，不包含真实人员名单、网页快照、运行
数据库、报告、Cookie 或凭证。GitHub Release 中的 ZIP 是飞书 Agent 的固定升级入口。

## v0.9：增量名单、可靠解析与双报告

`0.9.0-portable.1` 直接围绕用户现有飞书总表工作：

- 以飞书 `record_id` 和规范化内容 hash 对账增删改恢复；只有完整分页成功才推断删除，删除只做
  tombstone。普通名单变化仅处理受影响记录，不触发全量主页重跑。
- 运行时读取完整字段清单，兼容中英文及 Mono 复数字段；缺机器列只做幂等新增，绝不改名或
  删除列。所有写入 read-before-write，权威 Base 的人工字段反向更新本地状态。
- 用户报告只列“谁的什么事实从什么变成什么”，错误、覆盖率、待审项和修复状态进入独立开发者
  Markdown/飞书文档；两类报告分别记账、重试和推进游标。
- 确定性规则先过滤 UI/字符噪声、年级自然递增、异常年份和 same→same；其余拟公开变化全部导出
  有上限、持久绑定的紧凑审核快照，由执行 Skill 的宿主 Agent 使用自身 token 最终裁定；不配置、
  不调用 DeepSeek。积压分批处理，不能省略快照 ID 或局部重放。
- Scholar 使用固定周内相位、小批预算和跨进程持久 host circuit；429 会停止当轮余下请求，等待
  `Retry-After` 后仅做 canary。
- charset 严格解码、U+FFFD/年份/arXiv 交叉验证和 parser anomaly 隔离保护可靠 baseline。

持续自动运行只支持 OpenClaw 的隔离宿主 Agent automation。它每 15 分钟启动一个新的 Agent
回合，由该 Agent 调用 `$people-tracking`、消费全部 source/master/review/delivery bridge，并使用自身
token 完成审核；automation 本身使用 `--no-deliver`，只有 Skill 已批准的投递动作可以发消息。
`claude_lark_cli` 仅支持交互运行：bootstrap 必须显式传 `--skip-schedule`，不会安装
launchd/systemd，也不会把缺少 Agent 裁决能力的裸 `schedule-tick` 宣称为可用后台任务。

既有 Homepage 安全门仍然保留：

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
下载同一 tag 的 ZIP 与 `.sha256`。必须在同一临时下载目录中先校验外部 checksum；校验失败或
缺少 `.sha256` 时停止，不得解压、导入或执行包内任何 Python：

```bash
release_version="0.9.0-portable.1"
archive="people-tracking-feishu-${release_version}.zip"
checksum_file="${archive}.sha256"

# Linux
sha256sum -c "${checksum_file}"

# macOS（与上一条二选一）
shasum -a 256 -c "${checksum_file}"
```

只有 checksum 返回成功后，才解压并执行包内校验器：

```bash
release_version="0.9.0-portable.1"
archive="people-tracking-feishu-${release_version}.zip"
unzip "${archive}"
cd "people-tracking-feishu-${release_version}"
python3 verify_release.py
python3 install_bundle.py --doctor
python3 install_bundle.py --dry-run
python3 install_bundle.py --apply
```

这里的 `.sha256` 与 ZIP 来自同一个 GitHub Release：它能发现下载/存储损坏并固定所执行的精确
字节，但不能独立证明发布者身份。当前模板没有生成 Sigstore 或 GitHub Artifact Attestation，
因此信任根是 GitHub HTTPS、该仓库的账号/发布权限，以及维护者不改写 tag/Release 的发布纪律。
若要求独立发布者认证，应在维护者通过另一个可信渠道发布 digest 或签名证明前停止安装；不要把
同源 `.sha256` 称作独立签名。

安装器不会联网安装外部依赖，不会改写现有飞书/OpenClaw/Claude 配置；同名 Skill 和 launcher
会先备份。依赖或权限不满足时，apply 在写入前停止。

升级后先预览名单与来源维护。日常运行只执行增量同步和到期扫描：

```bash
people-tracking-feishu sync --json
people-tracking-feishu sync --apply --json
people-tracking-feishu scan --json
people-tracking-feishu review-export --output /secure/review-requests.json --json
people-tracking-feishu review-apply --input /secure/review-decisions.json --json

people-tracking-feishu source-routes --json
people-tracking-feishu source-routes --apply --json
```

Claude + `lark-cli` 交互模式首次启用时使用：

```bash
people-tracking-feishu bootstrap --confirmation '确认启用' --skip-schedule --json
```

OpenClaw 模式不传 `--skip-schedule`，bootstrap 会注册隔离宿主 Agent automation。

仅在升级验收或定向修复 Homepage 时显式严格复扫：

```bash

people-tracking-feishu scan --force-all --force-full-fetch \
  --source-kind homepage --homepage-retries 1 \
  --homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json
```

首次健康读取只称为建立 baseline。覆盖不足或错误率超过门限时，运行结果进入开发者报告；它
不会污染用户报告，也不会阻断同一窗口内其他已确认的重要事实。

飞书中的旧附件不会自动更新。目标 Agent 必须从固定 tag 拉取新包，核对 SHA-256，执行
verify/doctor/dry-run/apply 并重载 Skill。

## 开发与发布

版本唯一来源是根目录 [`VERSION`](VERSION)，格式为 `MAJOR.MINOR.PATCH-portable.N`。每个
Git tag `v<VERSION>` 与 Release 资产按发布纪律视为不可改写；模板本身不启用或强制 GitHub
immutable releases，同一版本号不得生成不同内容。

```bash
python3 scripts/validate_portable_skill.py
python3 -m pytest -q tests
python3 scripts/build_feishu_release.py
```

仓库提供 [GitHub Actions 模板](ci-templates/github-actions/)，用于运行定向回归、两次确定性
构建、manifest/secret/local-path 校验和包内无网络合成测试。启用模板需要维护者的 GitHub
凭据具有 `workflow` scope；没有该权限时继续使用同一组本地发布门和手工 Release。
发布规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界见 [SECURITY.md](SECURITY.md)。
