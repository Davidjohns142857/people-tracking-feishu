# E2E 验收报告

## 发布前离线验收

- 全项目与便携层 pytest 由本地发布门重新执行；仓库只提供可选 CI 模板，静态报告不保留会过期的计数。
- 使用纯合成人物运行 Homepage `baseline → candidate → changed`。
- 使用 fake `lark-cli 1.0.82` 验证 Base、Docs、消息全部先 dry-run，再实际写入。
- 回读合成 Base/文档并验证消息幂等键。
- 使用临时 HOME 验证 OpenClaw/Claude 双安装、重复安装、备份和回滚不修改预先存在的无关配置 hash。
- 用纯本地请求/决定文件验证 `people-tracking-agent-review-v1` 的 evidence hash、全量覆盖、重放与过期拒绝；测试和运行时都不调用外部评审模型。
- 用模拟 429/Retry-After 验证 Scholar host circuit 跨进程保持、当轮停止、恢复 canary 和稳定周内错峰。
- 用增删改恢复的合成 Base 记录验证 record_id/version hash 对账、分页不完整不推断删除、tombstone 保留历史及同表 read-before-write。
- 分别验证用户报告不含内部错误词，开发者报告包含来源/解析诊断；任一投递失败不推进另一游标。

可恢复 `bootstrap` 已覆盖直接导入、缺主页证据回填、OpenClaw 来源 bridge、可见总库 bridge、受预算约束的首次基线和重复恢复；Scholar 未完成项可处于 `baseline_degraded`，并由后续 tick 继续修复。包内自测与轻量跟踪对抗测试的当次结果以 CI/发布日志为准；详细门禁由 `INSTALLATION_REPORT.md` 记录。

## 真实飞书私有沙箱

发布构建机未执行真实写测：当前只具备 bot 身份，且用户长期约束是不在未明确同意时触发飞书登录、授权或写操作。包内 `sandbox-e2e` 会在目标 Agent 完成配置并再次收到精确确认语后创建 `[TEST]` Base/文档/消息，回读后询问保留或清理。

这不是“已通过真实飞书”的声明。目标机最终验收结果必须由命令返回的对象 token、readback 和 message ID 填入安装报告；任一缺失都应标为失败或待验收。
