from __future__ import annotations

import io
import json
import ssl
import sys
import urllib.error
import urllib.request
from email.message import Message

import pytest

from people_intel import light_cli
from people_intel.light_tracker import FetchObservation, extract_snapshot, health_status


CAPTCHA_PAGE = b"""
<html><head><title>Captcha</title></head><body><script>
captcha
window.requestId = "public-request";
challenge
</script></body></html>
"""


def test_http_error_keeps_only_bounded_challenge_diagnostics(monkeypatch) -> None:
    headers = Message()
    headers["Server"] = "cloudflare"
    headers["CF-Mitigated"] = "challenge"
    headers["Content-Type"] = "text/html; charset=UTF-8"
    headers["Set-Cookie"] = "private-session=must-not-be-retained"
    body = (
        b"<html><title>Just a moment...</title>"
        b"<script src='https://challenges.cloudflare.com/cdn-cgi/challenge'></script>"
        + b"sensitive-error-body" * 2_000
    )

    def blocked(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://profiles.example/alice",
            403,
            "Forbidden",
            headers,
            io.BytesIO(body),
        )

    monkeypatch.setattr(light_cli, "_public_urlopen", blocked)
    observation = light_cli._http_fetch(
        "https://profiles.example/alice",
        headers={"User-Agent": "public-monitor"},
    )

    assert observation.status_code == 403
    assert observation.body is None
    assert observation.content_type == "text/html; charset=UTF-8"
    assert "access_fingerprint=cloudflare_challenge" in observation.error
    assert "server=cloudflare" in observation.error
    assert "cf_mitigated=challenge" in observation.error
    assert "body_prefix_sha256=" in observation.error
    assert "body_prefix_truncated=true" in observation.error
    assert "private-session" not in observation.error
    assert "sensitive-error-body" not in observation.error
    assert len(observation.error) < 900


def test_http_200_captcha_uses_registered_raw_html_fallback(monkeypatch) -> None:
    profile_url = "https://nieshenx.github.io/"
    raw_url = (
        "https://raw.githubusercontent.com/"
        "nieshenx/nieshenx.github.io/main/index.html"
    )
    raw_html = b"""
    <!doctype html><html><head><title>Shen Nie</title></head><body><main>
    <h1>Shen Nie</h1><p>Research on multimodal diffusion models.</p>
    </main></body></html>
    """
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append((url, headers))
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        if url == profile_url:
            return FetchObservation(
                body=CAPTCHA_PAGE,
                status_code=200,
                final_url=url,
            )
        assert url == raw_url
        return FetchObservation(
            body=raw_html,
            status_code=200,
            final_url=url,
            content_type="text/plain",
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"alternate_urls": [raw_url]},
        }
    )

    assert [url for url, _ in calls] == [profile_url, raw_url]
    assert observation.body == raw_html
    assert observation.retrieval_mode == "alternate_raw_html"
    snapshot = extract_snapshot("homepage", observation.body)
    assert snapshot.identity["name_or_title"] == "Shen Nie"
    assert snapshot.token_count > 5


def test_http_200_waf_header_uses_registered_alternate(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    alternate_url = "https://alice.example/"
    alternate_html = b"<html><head><title>Alice Zhang</title></head><body><h1>Alice Zhang</h1></body></html>"

    def fake_http_fetch(url, *, headers, timeout=30):
        if url == profile_url:
            observation = FetchObservation(
                body=b"<html><body><h1>Please wait</h1></body></html>",
                final_url=url,
            )
            observation._diagnostic_headers = {"CF-Mitigated": "challenge"}
            return observation
        assert url == alternate_url
        return FetchObservation(body=alternate_html, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"alternate_urls": [alternate_url]},
        }
    )

    assert observation.body == alternate_html
    assert observation.retrieval_mode == "alternate_public_url"


def test_http_200_spa_shell_uses_registered_alternate(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    alternate_url = "https://backup.example/alice"
    shell = b"<html><head><title>Alice Zhang</title></head><body><div id='root'></div></body></html>"
    full_page = (
        b"<html><head><title>Alice Zhang</title></head><body><main>"
        b"<h1>Alice Zhang</h1><p>Research scientist working on reliable machine learning.</p>"
        b"</main></body></html>"
    )
    calls: list[str] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append(url)
        if url == profile_url:
            return FetchObservation(body=shell, final_url=url)
        return FetchObservation(body=full_page, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"alternate_urls": [alternate_url]},
        }
    )

    assert calls == [profile_url, alternate_url]
    assert observation.body == full_page
    assert observation.retrieval_mode == "alternate_public_url"


def test_success_body_over_limit_is_rejected_without_truncated_baseline(monkeypatch) -> None:
    headers = Message()
    headers["Content-Type"] = "text/html"

    class Response:
        status = 200
        url = "https://example.com/profile"

        def __init__(self):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == light_cli.MAX_HTTP_BODY_BYTES + 1
            return b"x" * limit

    monkeypatch.setattr(light_cli, "_public_urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        light_cli,
        "_validate_public_url",
        lambda value, *, resolve: str(value),
    )
    observation = light_cli._http_fetch(
        "https://example.com/profile",
        headers={"User-Agent": "public-monitor"},
    )

    assert observation.status_code == 200
    assert observation.body is None
    assert "exceeds 3000000-byte safety limit" in observation.error
    assert health_status(observation, "homepage")[0] == "transport_error"


def test_http_fetch_preserves_header_charset_for_legacy_profile(monkeypatch) -> None:
    headers = Message()
    headers["Content-Type"] = "text/html; charset=gb18030"
    person_text = "张三的机器学习研究"
    body = (
        f"<html><head><title>{person_text}</title></head><body><main>"
        f"<h1>{person_text}</h1><p>{person_text}，关注可靠评测系统。</p>"
        "</main></body></html>"
    ).encode("gb18030")

    class Response:
        status = 200
        url = "https://example.com/legacy-profile"

        def __init__(self):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == light_cli.MAX_HTTP_BODY_BYTES + 1
            return body

    monkeypatch.setattr(light_cli, "_public_urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        light_cli,
        "_validate_public_url",
        lambda value, *, resolve: str(value),
    )

    observation = light_cli._http_fetch(
        "https://example.com/legacy-profile",
        headers={"User-Agent": "public-monitor"},
    )
    snapshot = extract_snapshot(
        "homepage", observation.body, content_type=observation.content_type,
    )

    assert observation.content_type == "text/html; charset=gb18030"
    assert snapshot.extractor_version != "parser-anomaly-v1"
    assert person_text in " ".join(
        [*snapshot.identity.values(), *(item.text for item in snapshot.items)]
    )


def test_public_redirect_rejects_private_and_token_targets() -> None:
    handler = light_cli._SafePublicRedirect()
    request = urllib.request.Request("https://example.com/profile")
    for target in (
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com/profile?access_token=secret",
    ):
        try:
            handler.redirect_request(request, None, 302, "Found", {}, target)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe redirect accepted: {target}")


def test_public_url_allows_proxy_fake_ip_only_through_loopback_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        light_cli.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("198.18.5.161", 443))],
    )
    monkeypatch.setattr(
        light_cli.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        light_cli.urllib.request,
        "proxy_bypass",
        lambda hostname: False,
    )

    assert light_cli._validate_public_url(
        "https://scholar.google.com/citations?user=example",
        resolve=True,
    ) == "https://scholar.google.com/citations?user=example"


def test_public_url_rejects_proxy_fake_ip_without_usable_local_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        light_cli.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("198.18.5.161", 443))],
    )
    monkeypatch.setattr(light_cli.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(
        light_cli.urllib.request,
        "proxy_bypass",
        lambda hostname: False,
    )

    with pytest.raises(ValueError, match="resolved to a non-public address"):
        light_cli._validate_public_url(
            "https://scholar.google.com/citations?user=example",
            resolve=True,
        )


def test_public_url_rejects_private_dns_even_with_loopback_proxy(monkeypatch) -> None:
    monkeypatch.setattr(
        light_cli.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    monkeypatch.setattr(
        light_cli.urllib.request,
        "getproxies",
        lambda: {"https": "http://127.0.0.1:7890"},
    )
    monkeypatch.setattr(
        light_cli.urllib.request,
        "proxy_bypass",
        lambda hostname: False,
    )

    with pytest.raises(ValueError, match="resolved to a non-public address"):
        light_cli._validate_public_url("https://example.com/profile", resolve=True)


def test_primary_source_rejects_private_url_before_fetch(monkeypatch) -> None:
    monkeypatch.setattr(
        light_cli,
        "_http_fetch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("private primary URL must not be fetched")
        ),
    )
    observation = light_cli._fetch(
        {"kind": "homepage", "url": "http://127.0.0.1/private"}
    )
    assert observation.body is None
    assert "public URL safety policy" in observation.error


def test_scan_live_skips_disabled_sources(monkeypatch, tmp_path, capsys) -> None:
    database = tmp_path / "tracker.sqlite3"
    tracker = light_cli.LightTracker(database)
    person = tracker.add_person(
        "Alice Zhang",
        urls=["https://example.com/alice", "https://backup.example.com/alice"],
    )
    disabled_id = person["sources"][0]["source_id"]
    tracker.db.execute(
        "UPDATE sources SET tracking_enabled=0 WHERE source_id=?", (disabled_id,)
    )
    tracker.db.commit()
    tracker.close()
    fetched: list[str] = []

    def fake_fetch(source):
        fetched.append(source["source_id"])
        return FetchObservation(
            body=(
                "<html><title>Alice Zhang</title><h1>Alice Zhang</h1>"
                "<p>Machine learning research profile.</p></html>"
            ),
            final_url=source["url"],
        )

    monkeypatch.setattr(light_cli, "_fetch", fake_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["people-tracker-lite", "--db", str(database), "scan-live", "--no-deepseek"],
    )
    light_cli.main()
    capsys.readouterr()
    assert disabled_id not in fetched
    assert len(fetched) == 1


def test_huggingface_overview_api_is_projected_after_captcha(monkeypatch) -> None:
    profile_url = "https://huggingface.co/Yiyuan"
    api_url = "https://huggingface.co/api/users/Yiyuan/overview"
    payload = {
        "_id": "public-id",
        "user": "Yiyuan",
        "fullname": "Yiyuan Zhang",
        "details": "Vision research <script>not markup</script>",
        "numModels": 4,
        "numDatasets": 1,
        "numSpaces": 2,
        "numPapers": 9,
        "numFollowers": 7,
        "orgs": [{"id": "org-1", "name": "example", "fullname": "Example Lab"}],
    }
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append((url, headers))
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        if url == profile_url:
            return FetchObservation(
                body=CAPTCHA_PAGE,
                status_code=200,
                final_url=url,
            )
        assert url == api_url
        assert headers["Accept"] == "application/json"
        return FetchObservation(
            body=json.dumps(payload).encode(),
            status_code=200,
            final_url=url,
            content_type="application/json",
            etag='"overview-v1"',
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config_json": json.dumps({"alternate_urls": [api_url]}),
        }
    )

    assert [url for url, _ in calls] == [profile_url, api_url]
    assert observation.status_code == 200
    assert observation.final_url == profile_url
    assert observation.retrieval_mode == "public_json_api"
    assert observation.etag == '"overview-v1"'
    assert "<h1>Yiyuan Zhang</h1>" in observation.body
    assert "Vision research &lt;script&gt;not markup&lt;/script&gt;" in observation.body
    assert '<link rel="canonical" href="https://huggingface.co/Yiyuan">' in observation.body
    snapshot = extract_snapshot("homepage", observation.body)
    assert snapshot.identity["name_or_title"] == "Yiyuan Zhang"
    assert snapshot.token_count > 5


def test_public_api_identity_mismatch_continues_to_next_registered_route(monkeypatch) -> None:
    profile_url = "https://huggingface.co/Yiyuan"
    api_url = "https://huggingface.co/api/users/Yiyuan/overview"
    raw_url = "https://profiles.example/yiyuan/index.html"
    raw_html = b"<html><head><title>Yiyuan Zhang</title></head><body><h1>Yiyuan Zhang</h1></body></html>"

    def fake_http_fetch(url, *, headers, timeout=30):
        if url == profile_url:
            return FetchObservation(body=CAPTCHA_PAGE, final_url=url)
        if url == api_url:
            return FetchObservation(
                body=json.dumps({"user": "DifferentUser", "fullname": "Other"}).encode(),
                final_url=url,
                content_type="application/json",
            )
        assert url == raw_url
        return FetchObservation(
            body=raw_html,
            final_url=url,
            content_type="text/plain",
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {
                "alternate_routes": [
                    {"route": "huggingface_user_overview", "url": api_url},
                    {"route": "raw_html", "url": raw_url},
                ]
            },
        }
    )

    assert observation.body == raw_html
    assert observation.retrieval_mode == "alternate_raw_html"


def test_normal_page_that_mentions_captcha_does_not_trigger_fallback(monkeypatch) -> None:
    profile_url = "https://research.example/alice"
    alternate_url = "https://backup.example/alice"
    normal_page = b"""
    <html><head><title>Alice Zhang</title></head><body><main>
    <h1>Alice Zhang</h1>
    <p>Our paper studies CAPTCHA challenge detection for web security.</p>
    </main></body></html>
    """
    calls: list[str] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append(url)
        if url != profile_url:
            raise AssertionError("healthy direct pages must not fetch alternates")
        return FetchObservation(body=normal_page, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"alternate_urls": [alternate_url]},
        }
    )

    assert calls == [profile_url]
    assert observation.body == normal_page
    assert observation.retrieval_mode == "direct"


def test_meta_refresh_refuses_non_public_target(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    shell = b"""
    <html><head>
    <meta http-equiv="refresh" content="0; url=http://169.254.169.254/latest/meta-data/">
    <title>Alice Zhang</title></head><body><h1>Alice Zhang</h1></body></html>
    """
    calls: list[str] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append(url)
        if url != profile_url:
            raise AssertionError("unsafe meta-refresh target must not be fetched")
        return FetchObservation(body=shell, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"follow_meta_refresh": True},
        }
    )

    assert calls == [profile_url]
    assert observation.body == shell
    assert observation.retrieval_mode == "meta_refresh_rejected"


def test_meta_refresh_refuses_semicolon_hidden_token(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    shell = b"""
    <html><head>
    <meta http-equiv="refresh" content="0; url=https://example.org/profile?view=1;access_token=secret">
    <title>Alice Zhang</title></head><body><h1>Alice Zhang</h1></body></html>
    """
    calls: list[str] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append(url)
        if url != profile_url:
            raise AssertionError("credential-bearing meta-refresh target must not be fetched")
        return FetchObservation(body=shell, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": profile_url,
            "retrieval_config": {"follow_meta_refresh": True},
        }
    )

    assert calls == [profile_url]
    assert observation.retrieval_mode == "meta_refresh_rejected"


def test_registered_routes_reject_token_and_signature_queries() -> None:
    routes = light_cli._alternate_route_specs(
        {
            "alternate_routes": [
                "https://example.com/profile?refresh_token=secret",
                "https://example.com/profile?X-Goog-Signature=secret",
                "https://example.com/profile?view=1;access_token=secret",
                "https://example.com/profile?view=1%3BX-Goog-Signature=secret",
                "https://example.com/profile?view=public",
            ]
        }
    )

    assert routes == [("auto", "https://example.com/profile?view=public")]


def test_captcha_without_registered_alternate_is_not_bypassed(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    calls: list[str] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append(url)
        return FetchObservation(body=CAPTCHA_PAGE, final_url=url)

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch({"kind": "homepage", "url": profile_url})

    assert calls == [profile_url]
    assert observation.body is None
    assert observation.status_code == 200
    assert "access_fingerprint=captcha_challenge" in observation.error
    assert "window.requestId" not in observation.error
    assert observation.retrieval_mode == "direct"
    health, detail = health_status(observation, "homepage")
    assert health == "blocked"
    assert "access_fingerprint=captcha_challenge" in detail


def test_cloudflare_200_without_fallback_is_sanitized(monkeypatch) -> None:
    profile_url = "https://profiles.example/alice"
    challenge_body = b"<html><body><h1>Challenge response body</h1></body></html>"

    def fake_http_fetch(url, *, headers, timeout=30):
        observation = FetchObservation(body=challenge_body, final_url=url)
        observation._diagnostic_headers = {
            "Server": "cloudflare",
            "CF-Mitigated": "challenge",
            "Set-Cookie": "must-not-be-retained",
        }
        return observation

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch({"kind": "homepage", "url": profile_url})

    assert observation.body is None
    assert observation.status_code == 200
    assert "access_fingerprint=cloudflare_challenge" in observation.error
    assert "body_prefix_sha256=" in observation.error
    assert "Challenge response body" not in observation.error
    assert "must-not-be-retained" not in observation.error
    assert health_status(observation, "homepage")[0] == "blocked"


def test_cuhk_tls_compatibility_is_exact_host_only() -> None:
    assert light_cli._uses_cuhk_tls12_static_rsa(
        "https://myweb.cuhk.edu.cn/guanjun"
    )
    assert light_cli._uses_cuhk_tls12_static_rsa(
        "https://sse.cuhk.edu.cn/en/faculty/guanjun"
    )
    assert not light_cli._uses_cuhk_tls12_static_rsa(
        "https://www.myweb.cuhk.edu.cn/guanjun"
    )
    assert not light_cli._uses_cuhk_tls12_static_rsa(
        "https://myweb.cuhk.edu.cn.evil.example/guanjun"
    )
    assert not light_cli._uses_cuhk_tls12_static_rsa(
        "https://myweb.cuhk.edu.cn:8443/guanjun"
    )
    assert not light_cli._uses_cuhk_tls12_static_rsa(
        "http://myweb.cuhk.edu.cn/guanjun"
    )


def test_cuhk_tls_context_keeps_verification_and_security_level_two() -> None:
    context = light_cli._cuhk_tls12_static_rsa_context()

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.security_level >= 2
    assert light_cli.CUHK_TLS12_STATIC_RSA_CIPHERS == (
        "AES128-GCM-SHA256:@SECLEVEL=2"
    )
    assert "@SECLEVEL=1" not in light_cli.CUHK_TLS12_STATIC_RSA_CIPHERS
    tls12_ciphers = {
        item["name"]
        for item in context.get_ciphers()
        if item["protocol"] != "TLSv1.3"
    }
    assert tls12_ciphers == {"AES128-GCM-SHA256"}


def test_exact_host_handler_reselects_context_after_redirect(monkeypatch) -> None:
    handler = light_cli._ExactHostTLSHTTPSHandler()

    def selected_context(http_class, request, **kwargs):
        return kwargs["context"]

    monkeypatch.setattr(handler, "do_open", selected_context)
    compatibility = handler.https_open(
        urllib.request.Request("https://myweb.cuhk.edu.cn/guanjun")
    )
    default = handler.https_open(
        urllib.request.Request("https://example.com/profile")
    )

    assert compatibility is handler._cuhk_compatibility_context
    assert default is handler._verified_default_context
    assert default.verify_mode == ssl.CERT_REQUIRED
    assert default.check_hostname is True


def test_alternate_routes_reject_credentials_and_nonstandard_ports() -> None:
    routes = light_cli._alternate_route_specs(
        {
            "alternate_routes": [
                "https://user:secret@example.com/profile",
                "https://example.com:8443/profile",
                "http://127.0.0.1/profile",
                "http://169.254.169.254/latest/meta-data/",
                {"route": "browser_login", "url": "https://example.com/login"},
                {"route": "raw_html", "url": "https://example.com/profile.html"},
            ]
        }
    )

    assert routes == [("raw_html", "https://example.com/profile.html")]


WESTLAKE_INLINE_PAGE = b"""
<!doctype html><html><head><title>Westlake faculty</title></head><body>
<div id="app"></div><script>
new facultyDetail("#app", {
    personObj: {
        name: "Weicheng Zang, Ph.D.",
        post: "School of Science",
        lab: "Exoplanet Survey Laboratory",
        email: "zangweicheng@westlake.edu.cn",
    },
    biographyStr: "<div><p>Weicheng Zang joined Westlake in January 2026.<\\/p><\\/div>",
    historyStr: "<div><p>Assistant Professor, Westlake University<\\/p><\\/div>",
    researchStr: "<div><p>Exoplanets and gravitational microlensing.<\\/p><\\/div>",
    representativeList: [
        {
            title: "Representative Publications",
            content: "<div><p>Microlensing planet discovery (2025).<\\/p><\\/div>",
        },
    ],
    mounted: function () {},
});
</script></body></html>
"""


def test_westlake_faculty_inline_projects_static_public_payload(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_fetch(url, *, headers, timeout=30):
        calls.append((url, headers))
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        return FetchObservation(
            body=WESTLAKE_INLINE_PAGE,
            status_code=200,
            final_url=light_cli.WESTLAKE_FACULTY_INLINE_URL,
            content_type="text/html",
            etag='"westlake-v1"',
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": light_cli.WESTLAKE_FACULTY_INLINE_URL,
            "retrieval_config": {"preferred_route": "westlake_faculty_inline"},
        }
    )

    assert [url for url, _ in calls] == [light_cli.WESTLAKE_FACULTY_INLINE_URL]
    assert observation.status_code == 200
    assert observation.retrieval_mode == "westlake_faculty_inline"
    assert observation.etag == '"westlake-v1"'
    assert "Weicheng Zang, Ph.D." in observation.body
    assert "School of Science" in observation.body
    assert "Exoplanet Survey Laboratory" in observation.body
    assert "zangweicheng@westlake.edu.cn" in observation.body
    assert "joined Westlake in January 2026" in observation.body
    assert "Microlensing planet discovery (2025)" in observation.body
    assert "facultyDetail" not in observation.body
    assert "<\\/p>" not in observation.body
    snapshot = extract_snapshot("homepage", observation.body)
    assert snapshot.identity["name_or_title"] == "Weicheng Zang, Ph.D."
    assert snapshot.token_count > 15


def test_westlake_faculty_inline_rejects_wrong_host_without_fetch(monkeypatch) -> None:
    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("unsupported URL must be rejected before fetching")

    monkeypatch.setattr(light_cli, "_http_fetch", unexpected_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": "https://en.westlake.edu.cn.evil.example/faculty/weicheng-zang.html",
            "retrieval_config": {"preferred_route": "westlake_faculty_inline"},
        }
    )

    assert observation.body is None
    assert observation.status_code == 0
    assert "unsupported URL" in observation.error
    assert observation.retrieval_mode == "westlake_faculty_inline"


def test_westlake_faculty_inline_rejects_payload_without_name(monkeypatch) -> None:
    missing_name = WESTLAKE_INLINE_PAGE.replace(
        b'name: "Weicheng Zang, Ph.D.",', b'office: "E10",'
    )

    def fake_http_fetch(url, *, headers, timeout=30):
        return FetchObservation(
            body=missing_name,
            status_code=200,
            final_url=url,
            content_type="text/html",
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {
            "kind": "homepage",
            "url": light_cli.WESTLAKE_FACULTY_INLINE_URL,
            "retrieval_config": {"preferred_route": "westlake_faculty_inline"},
        }
    )

    assert observation.body is None
    assert observation.status_code == 200
    assert "name missing" in observation.error
    assert "joined Westlake" not in observation.error
    assert observation.retrieval_mode == "westlake_faculty_inline"


def test_westlake_faculty_inline_requires_explicit_preferred_route(monkeypatch) -> None:
    def fake_http_fetch(url, *, headers, timeout=30):
        return FetchObservation(
            body=WESTLAKE_INLINE_PAGE,
            status_code=200,
            final_url=url,
            content_type="text/html",
        )

    monkeypatch.setattr(light_cli, "_http_fetch", fake_http_fetch)
    observation = light_cli._fetch(
        {"kind": "homepage", "url": light_cli.WESTLAKE_FACULTY_INLINE_URL}
    )

    assert observation.body == WESTLAKE_INLINE_PAGE
    assert observation.retrieval_mode == "direct"
