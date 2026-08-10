# 维护与发布

## 变更原则

1. 从 `main` 新建短分支，通过 PR 合并；不要直接覆盖已有 tag 或 Release 资产。
2. 版本只改根目录 `VERSION`，并在 `deploy/feishu/CHANGELOG.md` 增加条目。
3. 不提交真实名单、运行数据库、报告、网页快照、配置实例、Cookie、token 或本机绝对路径。
4. 来源适配优先使用经身份核验的公开静态 URL。WAF/CAPTCHA 不破解，证书错误不通过关闭验证绕过。
5. TLS 特例必须 exact-host、exact-cipher，保留 `CERT_REQUIRED` 和 hostname 验证，并有负向边界测试。
6. 新 projection 不执行 JavaScript，只解析有界、精确 host/path 的静态 payload。

## 必过门

```bash
python3 scripts/validate_portable_skill.py
python3 -m pytest -q tests
python3 scripts/build_feishu_release.py --output-dir /tmp/build-a
python3 scripts/build_feishu_release.py --output-dir /tmp/build-b
```

两次 ZIP SHA-256 必须一致。解包后还要运行 `verify_release.py` 和
`offline_self_test.py`。source route apply 必须保留 baseline/candidate/observation，并在修改前用
SQLite backup API 生成 integrity_check=ok 的 0600 备份。

`ci-templates/github-actions/` 保存与上述门一致的 Actions。只有 GitHub 凭据已获 `workflow`
scope 时，才把它们复制到 `.github/workflows/` 并提交；不得为绕过权限拒绝而弱化测试。

## 版本约定

- `0.x.y` 表示兼容性仍在演进。
- `portable.N` 是同一功能版本的便携发行修订号。
- tag 固定为 `v<VERSION>`。
- GitHub Release 只上传 ZIP 与对应 `.sha256`；`dist/` 不入库。
