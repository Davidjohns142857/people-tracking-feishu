# GitHub Actions templates

将 `ci.yml` 和 `release.yml` 复制到仓库 `.github/workflows/` 后即可启用。提交该路径要求当前
GitHub OAuth token 具有 `workflow` scope；未授权时保留模板，不触发登录或权限升级流程。

- `ci.yml`：源码校验、定向测试、双次确定性构建、release manifest/hash 完整性校验、离线 self-test。
- `release.yml`：只接受与 `VERSION` 一致的 `v<VERSION>` tag，重复全部发布门后创建 Release 资产；模板本身不启用或强制 GitHub immutable releases，维护者必须遵守不改写同版本 tag/资产的发布纪律。

没有 Actions 时，按 `CONTRIBUTING.md` 在可信构建机执行相同命令，再用 `gh release create`
上传 ZIP 与 `.sha256`。

模板生成的 `.sha256` 必须在解压或执行包内 Python 前从包外校验。它与 ZIP 同源，只能校验
传输/存储完整性，不能独立认证发布者；模板当前不生成 Sigstore 或 GitHub Artifact Attestation。
