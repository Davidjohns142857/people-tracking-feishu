# 飞书 I/O 契约

## 双运行时

- OpenClaw 官方飞书插件可用时，读取/写入 action 交给插件，确定性 CLI 只处理配置、人物库、网页检测、报告和幂等状态。
- Claude 后台使用官方 `lark-cli 1.0.82`。读取使用 `--as user`，输出使用 `--as bot`。
- 不因缺少用户身份自动发起 OAuth。报告 `user_available=false` 和精确缺失 scope，等待用户决定。

## CLI 规则

始终使用 argv 数组；成功必须同时满足退出码 0 和 JSON `ok=true`。auth/doctor 是官方元数据例外，按其公开 JSON 状态读取。每个写动作先用相同 argv 加 `--dry-run`，再执行实际命令。

使用 Docs v2：

```text
lark-cli docs +fetch --api-version v2 --doc URL --as user --format json
lark-cli docs +create --api-version v2 --title TITLE --markdown @FILE --as bot --dry-run
```

Base 读取每页最多 200 条，按 offset/has_more 分页，并只投影字段映射需要的字段。Wiki 中的 Base 先用 `wiki +node-get` 解析 `obj_token`，不得猜 token。

消息使用：

```text
lark-cli im +messages-send --as bot --markdown TEXT \
  --idempotency-key KEY --chat-id oc_xxx --dry-run
```

`current_chat` 不传给 CLI；由 Agent 插件发送并回填结果。

## Bridge 回填

`source-probe --bridge-results` 文件至少包含：

```json
{"all_ok": true, "sources": [{"source_ref": "source-1", "record_count": 20, "fields": ["姓名"]}]}
```

只有实际完成读取和权限验证时才能写 `all_ok=true`。

`digest --bridge-results` 文件至少包含：

```json
{"delivery_key": "RUN:daily:2026-08-04", "doc_url": "https://...", "doc_token": "docx...", "message_id": "om_..."}
```

重试必须复用同一 delivery key；不得再次创建文档。
