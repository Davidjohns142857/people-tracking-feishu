# 回滚说明

安装器只写版本化 release、隔离 venv、`people-tracking` Skill、launcher 和安装状态。发现已有同名 Skill/launcher 时先移动到带 UTC 时间戳的 backup，不覆盖其他文件。

执行：

```bash
python3 install_bundle.py --rollback
```

回滚会：

- 把本包安装的 Skill 和 launcher 移到 `*.rolledback-<UTC>`，不删除。
- 恢复安装前同名 backup。
- 保留 release、venv、配置、SQLite、报告和审计，便于恢复和取证。
- 不读取或修改飞书/OpenClaw/Claude 凭证与其他配置。

调度只在首次问卷验证完成、用户回复“确认启用”，且 `bootstrap` 已完成导入、主页补全、可见总库同步和初始基线后创建。调度停用属于独立状态变更，回滚器不会在未确认时自动删除 OpenClaw cron、launchd 或 systemd 项。
