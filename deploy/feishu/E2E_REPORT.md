# E2E 验收报告

## 发布前离线验收

- 全项目 270/270 与便携层 14/14 pytest 通过。
- 使用纯合成人物运行 Homepage `baseline → candidate → changed`。
- 使用 fake `lark-cli 1.0.82` 验证 Base、Docs、消息全部先 dry-run，再实际写入。
- 回读合成 Base/文档并验证消息幂等键。
- 使用临时 HOME 验证 OpenClaw/Claude 双安装、重复安装、备份和回滚不修改预先存在的无关配置 hash。
- DeepSeek 使用 fake transport 验证 `ambiguous_review` 与 `confirmed_summary`；不在发布构建中调用真实 key。

可恢复 `bootstrap` 已覆盖直接导入、缺主页证据回填、OpenClaw 来源 bridge、可见总库 bridge、全量首次基线和重复恢复。包内自测 6/6、既有轻量跟踪对抗 11/11 通过；详细证据由 `INSTALLATION_REPORT.md` 记录。

## 真实飞书私有沙箱

发布构建机未执行真实写测：当前只具备 bot 身份，且用户长期约束是不在未明确同意时触发飞书登录、授权或写操作。包内 `sandbox-e2e` 会在目标 Agent 完成配置并再次收到精确确认语后创建 `[TEST]` Base/文档/消息，回读后询问保留或清理。

这不是“已通过真实飞书”的声明。目标机最终验收结果必须由命令返回的对象 token、readback 和 message ID 填入安装报告；任一缺失都应标为失败或待验收。
