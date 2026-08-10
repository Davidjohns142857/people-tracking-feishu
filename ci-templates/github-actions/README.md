# GitHub Actions templates

将 `ci.yml` 和 `release.yml` 复制到仓库 `.github/workflows/` 后即可启用。提交该路径要求当前
GitHub OAuth token 具有 `workflow` scope；未授权时保留模板，不触发登录或权限升级流程。

- `ci.yml`：源码校验、定向测试、双次确定性构建、release 验签、离线 self-test。
- `release.yml`：只接受 `v<VERSION>` tag，重复全部发布门后创建不可变 Release 资产。

没有 Actions 时，按 `CONTRIBUTING.md` 在可信构建机执行相同命令，再用 `gh release create`
上传 ZIP 与 `.sha256`。
