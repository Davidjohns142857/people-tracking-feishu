# 控制主页验证契约

控制主页用于区分来源晚更新、轮询迟发现、候选迟确认、审核积压与报告迟交付。
所有模拟事实必须显示 `[TEST]`，不得合并进真实人物履历或作为真实人才动态发送。

## 上线后的首份回执与半周回执

以安装包入口执行一次。该命令从生产库读取真实覆盖，哪怕当前没有控制主页也会显示缺项。

```bash
python3 scripts/skill_entry.py debug-status --enable --json
```

有独立固定 developer 飞书收件人时，宿主执行返回的消息 action，当场投递首份回执。消息必须采用
返回的 markdown 和 idempotency_key，收到真实 message_id 后与 action 的 delivery_key、
message_hash 一起写入 JSON，再用 `digest --bridge-results FILE --json` 回填。
回执未回填会在后续 tick 重试同一份，不达到常规周期也可以完成。使用同一既有调度，每周一 08:00、
周四 20:00（部署时区）出一份，宿主会在到期后的下一次执行发出。业务抓取、名单或审核阻塞时独立投递。

如果仅配置了公开报告或本地 Markdown，`delivery.status=developer_target_missing`。先在发起升级的
私聊把返回的 markdown 回复给负责人，随后由他提供固定私聊 `ou_...` 或独立开发群 `oc_...`。
以以下命令绑定，自动投递仅作用于 debug，不能复用公开报告目标：

```bash
python3 scripts/skill_entry.py debug-status --enable --target-kind user --target-id ou_EXAMPLE --json
```

日常按需运行不带 `--enable` 的 `debug-status --json` 查看当前数据。84 小时内观察覆盖与当前启用数分别
计算；首次基线与解析失败不算已完成变化比较。控制页回执分别记录到期时间、抓取尝试、HTTP 状态、带
版本标记的最近 200 响应、首次发现、确认和事实审核。304 不写成重新读取正文。synthetic 测试从最终
公开报告排除，仍可看到正常事实审核结果。通过本地发布日志（含版本、公开回读时间）及 origin/CDN
访问记录补充对照。只有已经收到真正固定投递的回执才能确认投递；若连回执都没收到，从独立端检查。

## 证据与范围

- `injected_at`：本地 HTML 变更已准备好；不能冒充远端页面上线时间。
- `published_at`：宿主完成公开 origin 写入并回读后的发布时间凭据。
- `first_detected_at`：目标 runner 的候选/变化差分第一次包含该项受控事实的观察时间。
- `confirmed_at`：同一事实成为 `changed` 的实际观察时间。
- 名单登记回执、页面发布回执和观察回执均必须保留来源引用。该模块校验引用、身份、内容与时序，
  不将操作者提供的 JSON 回执称为独立的远程身份认证。
- 本模块不裁定真实周报措辞，也不修改最终周报编排。

## 本地 HTTP 回归

必须显式提供新的隔离目录和测试身份：

```bash
python3 scripts/skill_entry.py validation-profile smoke \
  --root /secure/validation-runs/unique-run \
  --subject-name '用户明确提供的姓名 / 已确认别名' --json
```

该命令实际启动自有 loopback HTTP server，按正常算法建立基线、观察当前日期模拟成果、补录历史
成果、未知日期成果及无变化对照，输出发现和确认延迟。原有公共 URL/SSRF 门不被修改。
本地毫秒级结果不代表生产扫描、飞书注册、审核或交付时效。

## 目标环境登记与持续更新

1. 在受控 origin 所在的持久目录初始化。只托管其 `public/`，不暴露 manifest、日志或 SQLite。

```bash
python3 scripts/skill_entry.py validation-profile init \
  --root /secure/controlled-profile \
  --subject-name '用户明确提供的姓名 / 已确认别名' \
  --public-url https://controlled-origin.example/ \
  --interval-seconds 86400 \
  --detection-sla-seconds 3600 --confirmation-sla-seconds 7200 --json
python3 scripts/skill_entry.py validation-profile registration \
  --root /secure/controlled-profile --json
```

`public_url` 必须是真实可匿名访问的 URL；示例域名不是部署结果。阈值是显式验收要求，须按目标
调度预算设定，不能为了使测试通过而自动放宽。origin 持续托管同一个 `public/` 目录，或每次注入
后执行真实发布与回读；上传一次静态副本不会自动随本地文件更新。

2. 本地保留 `registration.json` 作为 debug 名单。目标 Agent 将其中的测试行登记到实际生产 runner
   同步的飞书名单，按生产字段映射使用 Name、Homepage、ID（第二 ID）；姓名保持 `[TEST] ` 前缀，
   不改动或复用真实人物档案。完成正常
   source 同步与基线采集。保留 `record_id/source_id/source_url/readback_at/evidence_ref`，写入
   回执 JSON 后执行 `validation-profile record-receipt --root ROOT --input FILE --json`。
   回执前不得声称自动加名单已完成；不使用正式人物身份来承载模拟履历。

3. 使用**原生产 runner 与调度**采集该来源。从真实 observation 账本导出紧凑记录，包含
   `observation_id/run_id/source_id/source_url/record_id/observed_at/evidence_ref/decision`。
   `decision` 应包含正常算法的 `status/health_status/additions/modifications/removals`。
   执行 `validation-profile import-observations --root ROOT --input FILE --json`。
   不得将本地 smoke 记录改名后当作生产证据。
   已存在本地 profile 时，可用 `validation-profile bind-origin --root ROOT --public-url URL` 绑定
   正在服务该 public 目录的地址。线上使用 `debug-status --control-source-id SOURCE_ID --json`
   导出同一测试行的 `registration` 及 `observations`，分别保存这两个 JSON 值后导入本地。

4. 在已有、已授权的宿主维护 tick 中定期调用：

```bash
python3 scripts/skill_entry.py validation-profile tick \
  --root /secure/controlled-profile --evidence-source remote --json
```

remote tick 不代替生产 runner 抓取；它仅依据真实导入的基线与确认记录控制注入周转。
首次基线未完成或上一项尚未确认时等待；超期必须报告失败停滞，保留原证据，不不断追加新事实。
返回 `publication_required` 后由 origin 写入/发布并回读，保存
`event_id/published_at/source_url/evidence_ref`，执行
`validation-profile record-publication --root ROOT --input FILE --json`。
无变化对照也需要发布/读取同一页面的时间凭据，不能用发布前的旧观察来判通过。
默认远程轮换创业、任职、无变化三种场景；也可显式 `advance --scenario startup` 或
`--scenario job_change`。只在真实线上 baseline 完成后发布变化。

5. 持续导入目标观察，检查：

```bash
python3 scripts/skill_entry.py validation-profile check \
  --root /secure/controlled-profile --evidence-source remote --json
```

只有健康差分中的受控事实可作为命中，不能以摘要提到注入 ID 作为检测证据；未来观察不能提前
计入结果，失败抓取不能验证无变化。退出码 `3` 表示失败，`4` 表示检查证据尚不完整；仅
`status=passed` 表示该范围检查通过。`remote_registration_verified` 和
`production_scheduler_verified` 不应由回执导入自动改写为独立认证通过。

## 对已有生产周报取证

仓库 `scripts/export_discovery_audit.py` 仅使用标准库，以 SQLite `mode=ro` 读取生产账本。
先列出周报，再选择精确的 `report_key`，避免以时间重算出不同的49条：

```bash
python3 scripts/export_discovery_audit.py --database /secure/production.sqlite3 --list-reports
python3 scripts/export_discovery_audit.py --database /secure/production.sqlite3 \
  --report-key REPORT_KEY --output /secure/discovery-audit.json
```

导出不等于重跑抓取，不创建或迁移生产库，不推进基线/候选/游标。旧版本只保存紧凑差分和当前
语义快照时，不能恢复任意历史网页原文，也不能把最近一次健康检查直接当成最后一次明确未见。
发生候选重置、提取版本变化或证据缺失时，保留不确定性。
