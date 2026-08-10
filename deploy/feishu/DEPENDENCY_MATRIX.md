# 依赖矩阵

| 组件 | 固定要求 | 安装器行为 |
| --- | --- | --- |
| macOS | Intel/Apple Silicon | 支持；后台使用 launchd user agent |
| Linux | x64/arm64 | 支持；后台使用 systemd user timer |
| Python | 3.11 或 3.12 | 只检查；在隔离 state 目录创建 venv |
| Python wheel | `pypinyin==0.55.0` | 随包携带；`--no-index` 离线安装 |
| Node.js | `>=16` | Claude + lark-cli 模式只检查 |
| lark-cli | `1.0.82` | 只检查；版本漂移阻止 apply |
| npm integrity | `sha512-7jqwniqCtiunLPi2vypDu0aHSaPNeG93kRO9UZ9kywU/XSVSy/PH1L1GJfav1Goi95v3D8TjV5R+ttWnMEvjYQ==` | 写入 manifest 供外部核验 |
| 官方 Skills | `lark-shared`、`lark-doc`、`lark-base`、`lark-im` | Claude 模式只检查 |
| OpenClaw | 官方飞书插件 | OpenClaw 模式读取插件列表，不改配置 |

缺失外部依赖时安装器不会执行 npm、brew、apt、pip 网络安装。固定修复命令只作为报告输出：

```text
npx @larksuite/cli@1.0.82 install
npx skills add larksuite/cli -g -y
```

执行前必须取得用户同意；Skills 安装完成后重启 Agent。
