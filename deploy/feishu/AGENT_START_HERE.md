# People Tracking Feishu @@VERSION@@

本包给已经运行在飞书上的 OpenClaw/Claude Agent 增加人员主页持续跟踪，不替换 Agent 或飞书配置。

严格顺序：

1. 在临时目录解压。
2. 运行 `python3 verify_release.py`，只在 `ok=true` 时继续。
3. 运行 `python3 install_bundle.py --doctor` 与 `--dry-run`。
4. 缺依赖时只报告 `official_remediation`，等待用户批准；不得自动联网安装。
5. 用户确认安装后运行 `python3 install_bundle.py --apply`。
6. 重启或重载 Agent Skills。
7. 运行 `people-tracking-feishu onboarding --json`，把一次性问卷发到当前会话。
8. 未收到“确认启用”前，不导入、不建正式 Base、不注册调度、不发正式报告。
9. 收到确认后运行 `people-tracking-feishu bootstrap --confirmation '确认启用' --json`；执行并回填所有 bridge action，反复恢复，直至返回 `status=ready`。不得停在安装成功、等待 bridge、导入预览或仅建库状态。
10. `bootstrap` 必须完成来源导入、缺主页分批补全、People/Sources 同步、强制正文初始基线与调度注册。私有 `[TEST]` 写测仍只在用户单独确认后执行。
11. 升级后先运行 `people-tracking-feishu scan --force-all --force-full-fetch --source-kind homepage --json`。只有已启用 Homepage 全部形成 observation、错误率在配置门限内且旧基线保护无违规，才可称为严格复扫完成。

不要在聊天、文档、日志或命令参数中收集 secret 值。不要运行 `lark-cli config init --new` 或 `auth login`，除非用户明确批准。
