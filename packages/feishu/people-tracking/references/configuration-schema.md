# 配置契约

生成 `people-tracking-config-v1` JSON。不要把自然语言答案、secret 值或 Cookie 直接写进配置。

```json
{
  "schema_version": "people-tracking-config-v1",
  "runtime": {"mode": "auto"},
  "sources": [
    {
      "kind": "feishu_base",
      "url": "https://example.feishu.cn/base/BASE_TOKEN",
      "table_name": "People",
      "view_name": "Active"
    }
  ],
  "source_routes": [
    {
      "url": "https://example.org/people/example",
      "preferred_route": "direct",
      "follow_meta_refresh": true,
      "alternate_routes": [
        {
          "route": "raw_html",
          "url": "https://www.example.org/people/example"
        }
      ],
      "curation_note": "Versioned credential-free public fallback"
    }
  ],
  "field_mapping": {
    "name": "姓名",
    "secondary_id": "第二ID",
    "aliases": "别名",
    "school": "学校",
    "research_focus": "专业/研究方向",
    "stage": "阶段/年龄",
    "homepage": "个人主页",
    "scholar": "Google Scholar",
    "github": "GitHub",
    "linkedin": "LinkedIn"
  },
  "intake": {
    "missing_anchor_policy": "agent_discovery",
    "discovery_batch_size": 50,
    "minimum_evidence_links": 1,
    "require_identity_gate_on_baseline": true
  },
  "master_database": {"mode": "existing_base", "url": "https://example.feishu.cn/base/BASE_TOKEN"},
  "outputs": {
    "document": {"enabled": true, "folder_token": "FOLDER_TOKEN"},
    "message": {"enabled": true, "target_kind": "current_chat"}
  },
  "schedule": {
    "timezone": "Asia/Shanghai",
    "scan": "weekly",
    "daily_digest": "18:00",
    "weekly_digest": "MON 08:30"
  },
  "apis": {
    "deepseek": {
      "enabled": false,
      "model": "deepseek-v4-flash",
      "key_reference": "file:/secure/path/deepseek.key",
      "max_calls_per_day": 100,
      "max_total_tokens_per_day": 100000
    },
    "search": []
  }
}
```

来源 `kind` 只允许：`feishu_base`、`feishu_doc`、`feishu_wiki`、`local_file`、`people_intel_api`。

## 逐来源公开抓取路由

顶层 `source_routes[]` 只配置已导入 `sources.url` 的 exact URL，不支持 host/path
通配、自动搜索或猜测镜像。普通配置项省略 `action`（等同 `configure`），可写：

- `preferred_route`：`direct`、`wordpress_rest` 或仅限内置精确页面投影的
  `westlake_faculty_inline`。
- `follow_meta_refresh`：是否跟随页面明确声明的公开 meta refresh。
- `alternate_routes`：最多 20 个 `{route,url}`；`route` 只允许 `auto`、
  `raw_html`、`huggingface_user_overview`/`hf_user_overview`。Hugging Face
  路由只接受精确的公开 `/api/users/<user>/overview` 端点。
- `alternate_urls`：兼容旧配置的公开 URL 列表，等价于 `auto` route。
- `wordpress_endpoint`、`site_name`：WordPress REST 投影端点和显示名称。

所有 route URL 必须是 credential-free 的公开 HTTP(S) 地址：禁止用户名/密码、
token 或签名 query、Cookie、Authorization、本机/私网 IP、`.local` 域名和非
80/443 端口。配置不会读取浏览器登录态。exact URL 未命中、同时匹配多个人物或
同一 source 被重复配置时，整批 apply 拒绝，不做部分写入。

先预览，再应用：

```bash
people-tracking-feishu source-routes --json
people-tracking-feishu source-routes --apply --json
```

`sync --apply` 在人员导入后也会自动应用 `source_routes`，并返回逐项状态与审计计数。
路由更新只写 `sources.retrieval_config_json`；不会清除或覆盖 snapshot、candidate、
observation。首次通过新 route 成功读取只建立基线，不称为人员新进展。

需要停用错误旧页时可设置 `action: "soft_retire"` 并提供 `curation_note`；旧 source、
baseline、candidate 与 observations 全部保留，只关闭后续扫描并写
`source_curation_audit`。需要以公开新页替换时使用 `action: "replace"`、
`replacement_url` 和 `curation_note`；旧页软退役，新页作为独立 source 建立自己的
新基线。替换项中的 route 字段应用于新页。

可见总库 `mode` 只允许：`existing_base`、`create_base`、`local_or_api`。新建 Base 使用 `People` 和 `Sources` 两表；现有单表由字段映射兼容。

消息 `target_kind`：

- `current_chat`：由当前 Agent 飞书插件发送。
- `chat`：要求 `target_id=oc_xxx`，允许 bot CLI 直发。
- `user`：要求 `target_id=ou_xxx`，允许 bot CLI 直发。

DeepSeek `key_reference` 只能是 `file:`、`secret:`、`keychain:`、`vault:` 或绝对路径。无人值守 runtime 当前只解析 0600 普通文件；其他 secret manager 必须由宿主注入临时 0600 文件引用。

`intake.missing_anchor_policy`：

- `agent_discovery`：首次导入时分批要求 Agent 检索主页，并以姓名、第二 ID、别名、学校和方向交叉验证后回填；无法确认者转人工队列。
- `manual_queue`：不自动检索，缺四类主页的人直接保留在人工补全队列。
