# Homepage 可靠性

## 问题来源

Homepage 异常分成六类，不能统一写成“来源不可用”：

| 类别 | 常见现象 | 处理 |
| --- | --- | --- |
| URL 失效或迁移 | 404、非 `www` 无 DNS、旧路径、停放域名 | 核验 canonical；`replace` 新来源并软退役旧来源 |
| 访问受限 | 403、WAF、Cloudflare、HTTP 200 CAPTCHA/authwall | 保留旧基线；不重试、不登录；登记匿名公开 fallback |
| 传输抖动 | timeout、EOF、reset、DNS 暂时失败、502/503/504 | 最多重试一次，稳定退避；仍失败则记录暂时不可达 |
| TLS 配置 | 过期、hostname mismatch、漏中间证书、旧 cipher | 不关闭验证；换安全来源，只有 exact-host cipher 兼容可进代码 |
| 可访问但无正文 | React/Notion 壳、meta refresh、正文藏在 inline data | 使用已登记静态页、raw HTML 或严格静态 projection；不执行 JS |
| 解析或比较噪声 | HTTP 200 零条目、`www` ID 迁移、Privacy 页脚、旧 304 候选 | 严格质量门；当前比较器重验；不推进假候选 |

## 来源路由

在配置顶层维护 `source_routes[]`。每项以 exact `url` 定位现有来源：

```json
{
  "source_routes": [
    {
      "action": "configure",
      "url": "https://example.org/profile",
      "alternate_routes": [
        {"route": "raw_html", "url": "https://raw.githubusercontent.com/example/site/main/index.html"}
      ],
      "curation_note": "原入口偶发 503；使用本人仓库静态镜像"
    },
    {
      "action": "replace",
      "url": "https://old.example.org/person",
      "replacement_url": "https://www.example.edu/people/person/",
      "curation_note": "学校 canonical 已迁移并有身份回链"
    }
  ]
}
```

允许的路由字段是 `preferred_route`、`alternate_routes`、`alternate_urls`、
`follow_meta_refresh`、`wordpress_endpoint` 和 `site_name`。URL 必须是匿名公开
HTTP(S)，不得含 credentials、token query、私网/本机 host 或非 80/443 端口。

先运行 `source-routes --json` 检查 exact match、动作与安全校验，再运行
`source-routes --apply --json`。`replace` 与 `soft_retire` 必须有 `curation_note`；所有动作均保留
旧来源、观察、候选和基线。

## 严格复扫

```bash
python3 scripts/skill_entry.py scan --force-all --force-full-fetch \
  --source-kind homepage --homepage-retries 1 \
  --homepage-backoff-seconds 1.0 --max-error-rate 0.10 --json
```

验收同时检查：全部已启用 Homepage 已计划且已观察；正文抓取 planned 与 attempted 一致；
失败按 blocked、gone、rate_limited、transport_error、degraded、binding review/conflict 分类；错误率
不超过门限；失败来源的旧 baseline 未改变；首次成功只标 baseline。任一项不满足时停止成功投递，
先修正来源或保留为公开访问受限。
