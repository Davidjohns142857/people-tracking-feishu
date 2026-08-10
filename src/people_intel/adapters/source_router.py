from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class FetchPlan:
    route: str
    command: tuple[str, ...]
    requires_auth: bool
    discovery_tool: str | None = None
    notes: str = ""


class SourceRouter:
    """Pure routing layer for qiaomu-markdown-proxy and agent-reach tools."""

    def __init__(
        self,
        qiaomu_skill_root: str | Path,
        xhs_skill_root: str | Path | None = None,
        xhs_api_root: str | Path | None = None,
    ):
        self.root = Path(qiaomu_skill_root)
        self.xhs_root = Path(
            xhs_skill_root or Path.home() / ".codex/skills/xiaohongshu-skills"
        )
        self.xhs_api_root = Path(
            xhs_api_root
            or Path(__file__).resolve().parents[3] / ".people_intel" / "evals" / "jackwener-xiaohongshu-cli"
        )

    def fetch_plan(self, url: str) -> FetchPlan:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "mp.weixin.qq.com":
            return FetchPlan(
                route="qiaomu_weixin",
                command=("python3", str(self.root / "scripts" / "fetch_weixin.py"), url),
                requires_auth=False,
                discovery_tool="exa site:mp.weixin.qq.com",
                notes="Discover with Exa; fetch known URLs through the dedicated WeChat route.",
            )
        if any(domain in host for domain in ("feishu.cn", "larksuite.com")) and (
            "/docx/" in path or "/wiki/" in path
        ):
            return FetchPlan(
                route="qiaomu_feishu",
                command=("python3", str(self.root / "scripts" / "fetch_feishu.py"), url),
                requires_auth=True,
                notes="Requires explicit Feishu credentials; never start OAuth automatically.",
            )
        if path.endswith(".pdf"):
            return FetchPlan(
                route="qiaomu_pdf",
                command=("bash", str(self.root / "scripts" / "extract_pdf.sh"), url),
                requires_auth=False,
            )
        if host in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
            return FetchPlan(
                route="twitter_cli",
                command=("twitter", "tweet", url, "--json"),
                requires_auth=True,
                discovery_tool="twitter search",
            )
        return FetchPlan(
            route="qiaomu_generic",
            command=("bash", str(self.root / "scripts" / "fetch.sh"), url),
            requires_auth=False,
            discovery_tool="exa.web_search_exa",
        )

    def discovery_plan(self, channel: str, query: str) -> FetchPlan:
        if channel == "github":
            return FetchPlan("github_cli", ("gh", "search", "repos", query, "--limit", "10"), False)
        if channel == "x":
            return FetchPlan(
                "twitter_user_posts_or_exa",
                ("twitter", "user-posts", query.lstrip("@"), "-n", "10", "--json"),
                True,
                discovery_tool="Exa site:x.com fallback for keyword discovery",
                notes="Known handles use authenticated timelines; keyword search falls back to Exa when the CLI route fails.",
            )
        if channel == "xhs":
            return FetchPlan(
                "xhs_api_cli",
                (
                    str(self.xhs_api_root / ".venv" / "bin" / "xhs"),
                    "search", query, "--sort", "latest", "--json",
                ),
                True,
                notes=(
                    "Preferred read-only signed API route in an isolated HOME. "
                    "A signature_error opens the route circuit; a later job may use the validated Extension Bridge fallback."
                ),
            )
        if channel == "wechat":
            return FetchPlan(
                "exa_wechat",
                ("mcporter", "call", f'exa.web_search_exa(query: "{query}", numResults: 5)'),
                False,
            )
        return FetchPlan(
            "itjuzi_api_or_exa_public" if channel == "news" else "exa_search",
            ("mcporter", "call", f'exa.web_search_exa(query: "{query}", numResults: 5)'),
            False,
            notes=(
                "Licensed IT Juzi API is the production route; this public Exa plan is discovery-only."
                if channel == "news" else ""
            ),
        )
