from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from people_intel.light_tracker import (
    FetchObservation,
    LightTracker,
    assess_snapshot,
    dossier_records,
    extract_snapshot,
    utc_now,
)


HTTP_ERROR_PREFIX_LIMIT = 16_384
MAX_HTTP_BODY_BYTES = 3_000_000
DIAGNOSTIC_HEADERS = (
    "Server",
    "CF-Mitigated",
    "Retry-After",
    "X-Generator",
    "Content-Type",
)
ALTERNATE_ROUTE_NAMES = {
    "auto",
    "raw_html",
    "huggingface_user_overview",
    "hf_user_overview",
    "public_json_api",
}
REGISTERED_CREDENTIAL_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "authorization",
        "id_token",
        "key",
        "password",
        "refresh_token",
        "secret",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
CUHK_TLS12_STATIC_RSA_HOSTS = frozenset(
    {
        "myweb.cuhk.edu.cn",
        "sse.cuhk.edu.cn",
    }
)
CUHK_TLS12_STATIC_RSA_CIPHERS = "AES128-GCM-SHA256:@SECLEVEL=2"
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
WESTLAKE_FACULTY_INLINE_URL = (
    "https://en.westlake.edu.cn/faculty/weicheng-zang.html"
)
WESTLAKE_FACULTY_INLINE_MAX_BYTES = 1_000_000
_JS_DOUBLE_STRING = r'"(?:\\.|[^"\\])*"'


def _uses_cuhk_tls12_static_rsa(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and (parsed.hostname or "").casefold() in CUHK_TLS12_STATIC_RSA_HOSTS
    )


def _cuhk_tls12_static_rsa_context() -> ssl.SSLContext:
    """Verified TLS 1.2 context for two legacy CUHK public web hosts only."""

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(CUHK_TLS12_STATIC_RSA_CIPHERS)
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("verified CUHK TLS context could not be constructed")
    return context


class _ExactHostTLSHTTPSHandler(urllib.request.HTTPSHandler):
    """Select the compatibility context again after every HTTPS redirect."""

    def __init__(self) -> None:
        super().__init__()
        self._verified_default_context = ssl.create_default_context()
        self._cuhk_compatibility_context = _cuhk_tls12_static_rsa_context()

    def https_open(self, request):
        context = (
            self._cuhk_compatibility_context
            if _uses_cuhk_tls12_static_rsa(request.full_url)
            else self._verified_default_context
        )
        return self.do_open(
            http.client.HTTPSConnection,
            request,
            context=context,
        )


class _SafePublicRedirect(urllib.request.HTTPRedirectHandler):
    """Revalidate every redirect target before urllib follows it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_url(newurl, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _public_urlopen(request: urllib.request.Request, *, timeout: int):
    _validate_public_url(request.full_url, resolve=True)
    handlers: list[urllib.request.BaseHandler] = [
        _SafePublicRedirect(),
        _ExactHostTLSHTTPSHandler(),
    ]
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def _header_value(headers: object, name: str) -> str:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name)
    if value in (None, ""):
        items = getattr(headers, "items", None)
        if callable(items):
            value = next(
                (
                    item_value
                    for item_name, item_value in items()
                    if str(item_name).casefold() == name.casefold()
                ),
                None,
            )
    if value in (None, ""):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()[:160]


def _body_prefix(body: object, limit: int = HTTP_ERROR_PREFIX_LIMIT) -> bytes:
    if isinstance(body, bytes):
        return body[:limit]
    if isinstance(body, str):
        return body.encode("utf-8", errors="replace")[:limit]
    if isinstance(body, (dict, list)):
        return json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")[:limit]
    return b""


def _homepage_response_fingerprint(
    *,
    status_code: int,
    final_url: str | None,
    body: object,
    headers: object = None,
) -> str | None:
    """Classify a bounded public response without solving access challenges."""

    prefix = _body_prefix(body).decode("utf-8", errors="replace").casefold()
    server = _header_value(headers, "Server").casefold()
    cf_mitigated = _header_value(headers, "CF-Mitigated").casefold()
    generator = _header_value(headers, "X-Generator").casefold()
    final = (final_url or "").casefold()

    if cf_mitigated == "challenge" or (
        "challenges.cloudflare.com" in prefix
        and ("just a moment" in prefix or "_cf_chl" in prefix)
    ):
        return "cloudflare_challenge"
    if "cloudflare" in server and any(
        marker in prefix
        for marker in (
            "sorry, you have been blocked",
            "cf-error-details",
            "attention required! | cloudflare",
        )
    ):
        return "cloudflare_block"
    if "errors.edgesuite.net" in prefix or (
        "access denied" in prefix and "reference #18" in prefix
    ):
        return "akamai_access_denied"
    if (
        "captcha" in prefix
        and "challenge" in prefix
        and any(
            marker in prefix
            for marker in (
                "window.requestid",
                "verify you are human",
                "security verification",
                "captcha-delivery.com",
            )
        )
    ):
        return "captcha_challenge"
    if any(
        marker in prefix
        for marker in (
            "enable javascript and cookies to continue",
            "security verification",
            "checking if the site connection is secure",
        )
    ):
        return "browser_verification"
    if (
        status_code == 403
        and (
            "drupal" in generator
            or "/system/403" in final
            or ("access denied" in prefix and "drupal" in prefix)
        )
    ):
        return "origin_access_denied"
    if "lander_system" in prefix or "parking-lander" in prefix:
        return "parking_page"
    if len(prefix) < 2_000 and re.search(
        r"window\.location(?:\.href)?\s*=|location\.replace\s*\(",
        prefix,
    ):
        return "client_redirect_stub"
    if status_code in {401, 403}:
        return f"http_{status_code}_access_denied"
    return None


def _http_error_diagnostic(
    exc: urllib.error.HTTPError,
    body_prefix: bytes,
) -> str:
    fingerprint = _homepage_response_fingerprint(
        status_code=exc.code,
        final_url=exc.geturl(),
        body=body_prefix,
        headers=exc.headers,
    ) or "http_error"
    parts = [f"HTTP {exc.code}", f"access_fingerprint={fingerprint}"]
    for name in DIAGNOSTIC_HEADERS:
        value = _header_value(exc.headers, name)
        if value:
            key = name.casefold().replace("-", "_")
            parts.append(f"{key}={value}")
    if body_prefix:
        parts.append(
            "body_prefix_sha256="
            + hashlib.sha256(body_prefix).hexdigest()[:24]
        )
    return "; ".join(parts)[:800]


def _diagnostic_header_subset(headers: object) -> dict[str, str]:
    return {
        name: value
        for name in DIAGNOSTIC_HEADERS
        if (value := _header_value(headers, name))
    }


def _observation_fingerprint(observation: FetchObservation) -> str | None:
    return _homepage_response_fingerprint(
        status_code=observation.status_code,
        final_url=observation.final_url,
        body=observation.body,
        headers=getattr(observation, "_diagnostic_headers", None),
    )


def _sanitized_access_failure(
    observation: FetchObservation,
    fingerprint: str,
) -> FetchObservation:
    """Drop a challenge body while retaining bounded, non-secret diagnostics."""

    parts = [
        f"HTTP {observation.status_code}",
        f"access_fingerprint={fingerprint}",
    ]
    diagnostic_headers = getattr(observation, "_diagnostic_headers", {})
    for name in DIAGNOSTIC_HEADERS:
        value = _header_value(diagnostic_headers, name)
        if value:
            parts.append(f"{name.casefold().replace('-', '_')}={value}")
    prefix = _body_prefix(observation.body)
    if prefix:
        parts.append(
            "body_prefix_sha256=" + hashlib.sha256(prefix).hexdigest()[:24]
        )
    return FetchObservation(
        body=None,
        status_code=observation.status_code,
        final_url=observation.final_url,
        content_type=observation.content_type,
        error="; ".join(parts)[:800],
        etag=observation.etag,
        last_modified=observation.last_modified,
        observed_at=observation.observed_at,
        retrieval_mode=observation.retrieval_mode,
    )


def _retrieval_config(source: dict[str, object]) -> dict[str, object]:
    value = source.get("retrieval_config")
    if isinstance(value, dict):
        return value
    raw = source.get("retrieval_config_json")
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _http_fetch(
    url: str,
    *,
    headers: dict[str, str],
    timeout: int = 30,
) -> FetchObservation:
    try:
        with _public_urlopen(
            urllib.request.Request(url, headers=headers),
            timeout=timeout,
        ) as response:
            _validate_public_url(response.url, resolve=True)
            body = response.read(MAX_HTTP_BODY_BYTES + 1)
            if len(body) > MAX_HTTP_BODY_BYTES:
                return FetchObservation(
                    body=None,
                    status_code=response.status,
                    final_url=response.url,
                    content_type=(
                        _header_value(response.headers, "Content-Type")
                        or response.headers.get_content_type()
                    ),
                    error=(
                        f"public response exceeds {MAX_HTTP_BODY_BYTES}-byte safety limit; "
                        "confirmed baseline preserved"
                    ),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
            observation = FetchObservation(
                body=body,
                status_code=response.status,
                final_url=response.url,
                content_type=(
                    _header_value(response.headers, "Content-Type")
                    or response.headers.get_content_type()
                ),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
            # FetchObservation is intentionally unchanged. This private,
            # allow-listed subset exists only for in-process route selection and
            # is neither serialized nor written to SQLite.
            observation._diagnostic_headers = _diagnostic_header_subset(response.headers)
            return observation
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchObservation(
                body=None,
                status_code=304,
                final_url=url,
                etag=_header_value(exc.headers, "ETag") or None,
                last_modified=_header_value(exc.headers, "Last-Modified") or None,
            )
        try:
            diagnostic_bytes = exc.read(HTTP_ERROR_PREFIX_LIMIT + 1)
        except Exception:
            diagnostic_bytes = b""
        diagnostic_prefix = diagnostic_bytes[:HTTP_ERROR_PREFIX_LIMIT]
        diagnostic = _http_error_diagnostic(exc, diagnostic_prefix)
        if len(diagnostic_bytes) > HTTP_ERROR_PREFIX_LIMIT:
            diagnostic += "; body_prefix_truncated=true"
        content_type = _header_value(exc.headers, "Content-Type")
        return FetchObservation(
            body=None,
            status_code=exc.code,
            final_url=exc.geturl(),
            content_type=content_type or "text/html",
            error=diagnostic,
            etag=_header_value(exc.headers, "ETag") or None,
            last_modified=_header_value(exc.headers, "Last-Modified") or None,
        )
    except Exception as exc:
        return FetchObservation(
            body=None,
            status_code=getattr(exc, "code", 0),
            final_url=url,
            error=str(exc),
        )


def _scholar_tracking_url(url: str) -> str:
    """Return the deterministic recent-publications view used for monitoring.

    Scholar's default profile view is citation-sorted and normally exposes only
    twenty rows.  Papers can therefore enter or leave that window solely because
    their citation counts changed.  A larger publication-date-sorted window makes
    additions observable while keeping the stored source URL canonical.
    """

    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "hl": "en",
            "oe": "ASCII",
            "cstart": "0",
            "pagesize": "100",
            "sortby": "pubdate",
        }
    )
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            "",
        )
    )


def _wordpress_projection(
    endpoint: str,
    *,
    site_url: str,
    site_name: str,
    headers: dict[str, str],
) -> FetchObservation:
    observation = _http_fetch(endpoint, headers=headers)
    if observation.body is None or not 200 <= observation.status_code < 300:
        return observation
    try:
        payload = json.loads(observation.body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error=f"invalid WordPress REST response: {exc}",
            retrieval_mode="wordpress_rest",
        )
    if not isinstance(payload, list):
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error="invalid WordPress REST response: expected post list",
            retrieval_mode="wordpress_rest",
        )
    rows: list[str] = []
    for item in payload[:50]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        title = title.get("rendered") if isinstance(title, dict) else title
        excerpt = item.get("excerpt")
        excerpt = excerpt.get("rendered") if isinstance(excerpt, dict) else excerpt
        clean_excerpt = re.sub(r"<[^>]+>", " ", str(excerpt or ""))
        clean_excerpt = re.sub(r"\s+", " ", html.unescape(clean_excerpt)).strip()
        post_id = str(item.get("id") or item.get("slug") or item.get("link") or "")
        link = str(item.get("link") or site_url)
        rows.append(
            "<article data-post-id=\"{post_id}\">"
            "<h2><a href=\"{link}\">{title}</a></h2>"
            "<time>{date}</time><time data-kind=\"modified\">{modified}</time>"
            "<p>{excerpt}</p></article>".format(
                post_id=html.escape(post_id),
                link=html.escape(link),
                title=html.escape(str(title or post_id)),
                date=html.escape(str(item.get("date") or "")),
                modified=html.escape(str(item.get("modified") or "")),
                excerpt=html.escape(clean_excerpt),
            )
        )
    body = (
        "<!doctype html><html><head>"
        f"<title>{html.escape(site_name)}</title>"
        f"<link rel=\"canonical\" href=\"{html.escape(site_url)}\">"
        "</head><body><main>"
        f"<h1>{html.escape(site_name)}</h1>"
        "<section id=\"recent-posts\"><h2>Recent posts</h2>"
        + "".join(rows)
        + "</section></main></body></html>"
    )
    return FetchObservation(
        body=body,
        status_code=200,
        final_url=site_url,
        content_type="text/html",
        retrieval_mode="wordpress_rest",
    )


def _validate_public_url(value: object, *, resolve: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("public URL must be a string")
    url = value.strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid public URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("public URL must use http(s) with a hostname")
    if parsed.username or parsed.password:
        raise ValueError("credential-bearing public URL rejected")
    if port not in {None, 80, 443}:
        raise ValueError("non-standard public URL port rejected")
    if parsed.fragment:
        raise ValueError("public URL fragments are not fetched")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("local hostname rejected")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname or re.fullmatch(r"[0-9.]+", hostname):
            raise ValueError("invalid public hostname")
    else:
        if not address.is_global:
            raise ValueError("non-public IP address rejected")
    query_names = {
        key.casefold().replace("-", "_")
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    decoded_query = urllib.parse.unquote_plus(parsed.query)
    raw_query_names = {
        match.group(1).casefold().replace("-", "_")
        for match in re.finditer(r"(?:^|[&;])([^=&;]+)=", decoded_query)
    }
    if (query_names | raw_query_names) & REGISTERED_CREDENTIAL_QUERY_NAMES:
        raise ValueError("credential/token query parameter rejected")
    if resolve:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ValueError("public hostname did not resolve") from exc
        if not addresses:
            raise ValueError("public hostname did not resolve")
        parsed_addresses = [ipaddress.ip_address(value) for value in addresses]
        proxy_fake_ip_resolution = (
            all(address in PROXY_FAKE_IP_NETWORK for address in parsed_addresses)
            and _uses_loopback_proxy(parsed.scheme, hostname)
        )
        if (
            any(not address.is_global for address in parsed_addresses)
            and not proxy_fake_ip_resolution
        ):
            raise ValueError("public hostname resolved to a non-public address")
    return url


def _uses_loopback_proxy(scheme: str, hostname: str) -> bool:
    """Allow RFC 2544 Fake-IP DNS only when urllib will use a local proxy."""

    if urllib.request.proxy_bypass(hostname):
        return False
    proxy = urllib.request.getproxies().get(scheme)
    if not isinstance(proxy, str) or not proxy.strip():
        return False
    value = proxy.strip()
    if "://" not in value:
        value = f"http://{value}"
    try:
        parsed = urllib.parse.urlsplit(value)
        proxy_host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    if proxy_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(proxy_host).is_loopback
    except ValueError:
        return False


def _safe_registered_url(value: object) -> str | None:
    try:
        return _validate_public_url(value, resolve=False)
    except ValueError:
        return None


def _alternate_route_specs(config: dict[str, object]) -> list[tuple[str, str]]:
    """Return only explicitly registered, credential-free alternate routes."""

    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    configured = config.get("alternate_routes", [])
    if isinstance(configured, (str, dict)):
        configured = [configured]
    if isinstance(configured, list):
        for entry in configured:
            route = "auto"
            value: object = entry
            if isinstance(entry, dict):
                value = entry.get("url")
                route = str(
                    entry.get("route")
                    or entry.get("type")
                    or "auto"
                ).casefold()
            url = _safe_registered_url(value)
            if not url or route not in ALTERNATE_ROUTE_NAMES or url in seen:
                continue
            output.append((route, url))
            seen.add(url)

    legacy = config.get("alternate_urls", [])
    if isinstance(legacy, str):
        legacy = [legacy]
    if isinstance(legacy, list):
        for value in legacy:
            url = _safe_registered_url(value)
            if not url or url in seen:
                continue
            output.append(("auto", url))
            seen.add(url)
    return output


def _huggingface_overview_username(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "huggingface.co":
        return None
    match = re.fullmatch(r"/api/users/([^/]+)/overview/?", parsed.path)
    if not match:
        return None
    username = urllib.parse.unquote(match.group(1)).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return None
    return username


def _huggingface_profile_username(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "huggingface.co":
        return None
    match = re.fullmatch(r"/([^/]+)/?", parsed.path)
    if not match:
        return None
    username = urllib.parse.unquote(match.group(1)).strip()
    return username if re.fullmatch(r"[A-Za-z0-9_.-]+", username) else None


def _json_payload(body: object) -> dict[str, Any] | None:
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="strict")
    elif isinstance(body, str):
        text = body
    else:
        return None
    value = json.loads(text)
    return value if isinstance(value, dict) else None


def _huggingface_user_projection(
    endpoint: str,
    *,
    site_url: str,
    headers: dict[str, str],
) -> FetchObservation:
    """Project the anonymous HF user overview API into stable profile HTML."""

    expected_user = _huggingface_overview_username(endpoint)
    if not expected_user:
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error="unsupported public JSON endpoint",
            retrieval_mode="public_json_api",
        )
    profile_user = _huggingface_profile_username(site_url)
    if profile_user and profile_user.casefold() != expected_user.casefold():
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error="registered public JSON route does not match profile URL",
            retrieval_mode="public_json_api",
        )
    observation = _http_fetch(
        endpoint,
        headers={"User-Agent": headers["User-Agent"], "Accept": "application/json"},
    )
    observation.retrieval_mode = "public_json_api"
    if observation.body is None or not 200 <= observation.status_code < 300:
        return observation
    try:
        payload = _json_payload(observation.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error=f"invalid public JSON response: {type(exc).__name__}",
            retrieval_mode="public_json_api",
        )
    actual_user = str((payload or {}).get("user") or "").strip()
    if not payload or actual_user.casefold() != expected_user.casefold():
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=endpoint,
            error="public JSON identity mismatch",
            retrieval_mode="public_json_api",
        )

    fullname = str(payload.get("fullname") or actual_user).strip()[:240]
    details = str(payload.get("details") or "").strip()[:2_000]
    metrics = []
    for key in (
        "numModels",
        "numDatasets",
        "numSpaces",
        "numPapers",
        "numUpvotes",
        "numLikes",
        "numFollowers",
        "numFollowing",
    ):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics.append(
                f'<li data-field="{html.escape(key)}">'
                f"{html.escape(key)}: {html.escape(str(value))}</li>"
            )
    organizations = []
    raw_orgs = payload.get("orgs")
    if isinstance(raw_orgs, list):
        for item in raw_orgs[:100]:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or item.get("name") or "").strip()[:240]
            name = str(item.get("fullname") or item.get("name") or identifier).strip()[:240]
            if identifier and name:
                organizations.append((identifier, name))
    organizations.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
    organization_html = "".join(
        f'<li data-org-id="{html.escape(identifier, quote=True)}">'
        f"{html.escape(name)}</li>"
        for identifier, name in organizations
    )
    canonical = site_url if _safe_registered_url(site_url) else f"https://huggingface.co/{actual_user}"
    body = (
        "<!doctype html><html><head>"
        f"<title>{html.escape(fullname)}</title>"
        f'<link rel="canonical" href="{html.escape(canonical, quote=True)}">'
        "</head><body><main>"
        f"<h1>{html.escape(fullname)}</h1>"
        f'<p data-field="user">Hugging Face user: {html.escape(actual_user)}</p>'
        + (f'<p data-field="details">{html.escape(details)}</p>' if details else "")
        + "<section><h2>Public profile counts</h2><ul>"
        + "".join(metrics)
        + "</ul></section>"
        + "<section><h2>Organizations</h2><ul>"
        + organization_html
        + "</ul></section></main></body></html>"
    )
    return FetchObservation(
        body=body,
        status_code=200,
        final_url=canonical,
        content_type="text/html",
        etag=observation.etag,
        last_modified=observation.last_modified,
        observed_at=observation.observed_at,
        retrieval_mode="public_json_api",
    )


def _is_westlake_faculty_inline_url(url: str) -> bool:
    """Limit the inline projection to the one registered Westlake profile."""

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "en.westlake.edu.cn"
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and parsed.path == "/faculty/weicheng-zang.html"
        and not parsed.query
        and not parsed.fragment
    )


def _skip_javascript_trivia(source: str, index: int, end: int) -> int:
    """Skip whitespace and comments in a bounded, non-executing JS scanner."""

    while index < end:
        if source[index].isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2, end)
            if newline < 0:
                return end
            index = newline + 1
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2, end)
            if close < 0:
                raise ValueError("unterminated JavaScript comment")
            index = close + 2
            continue
        break
    return index


def _skip_javascript_string(source: str, index: int, end: int) -> int:
    quote = source[index]
    if quote not in {'"', "'"}:
        raise ValueError("unsupported JavaScript string")
    index += 1
    while index < end:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if char in "\r\n":
            raise ValueError("unterminated JavaScript string")
        index += 1
    raise ValueError("unterminated JavaScript string")


def _balanced_javascript_literal(
    source: str,
    start: int,
    opener: str,
    closer: str,
) -> str:
    """Return a balanced static literal without evaluating JavaScript."""

    if start >= len(source) or source[start] != opener:
        raise ValueError("JavaScript literal opener missing")
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [closer]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char in {'"', "'"}:
            index = _skip_javascript_string(source, index, len(source))
            continue
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_javascript_trivia(source, index, len(source))
            continue
        if char == "`":
            raise ValueError("template literals are not accepted")
        if char in pairs:
            stack.append(pairs[char])
        elif char in "}])":
            if not stack or char != stack[-1]:
                raise ValueError("unbalanced JavaScript literal")
            stack.pop()
            if not stack:
                return source[start : index + 1]
        index += 1
    raise ValueError("unterminated JavaScript literal")


def _javascript_value_end(source: str, start: int, end: int) -> int:
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    index = start
    while index < end:
        char = source[index]
        if char in {'"', "'"}:
            index = _skip_javascript_string(source, index, end)
            continue
        if source.startswith("//", index) or source.startswith("/*", index):
            index = _skip_javascript_trivia(source, index, end)
            continue
        if char == "`":
            raise ValueError("template literals are not accepted")
        if char in pairs:
            stack.append(pairs[char])
        elif char in "}])":
            if not stack or char != stack[-1]:
                raise ValueError("unbalanced JavaScript value")
            stack.pop()
        elif char == "," and not stack:
            return index
        index += 1
    if stack:
        raise ValueError("unterminated JavaScript value")
    return end


def _static_javascript_object(literal: str) -> dict[str, str]:
    """Split a narrow object literal into unevaluated top-level values."""

    source = literal.strip()
    if len(source) < 2 or source[0] != "{" or source[-1] != "}":
        raise ValueError("expected JavaScript object literal")
    properties: dict[str, str] = {}
    index = 1
    end = len(source) - 1
    while True:
        index = _skip_javascript_trivia(source, index, end)
        if index >= end:
            return properties
        key_match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", source[index:])
        if not key_match:
            raise ValueError("unsupported JavaScript object key")
        key = key_match.group(0)
        if key in properties:
            raise ValueError("duplicate JavaScript object key")
        index += len(key)
        index = _skip_javascript_trivia(source, index, end)
        if index >= end or source[index] != ":":
            raise ValueError("expected JavaScript property separator")
        index = _skip_javascript_trivia(source, index + 1, end)
        value_end = _javascript_value_end(source, index, end)
        value = source[index:value_end].strip()
        if not value:
            raise ValueError("empty JavaScript property value")
        properties[key] = value
        index = _skip_javascript_trivia(source, value_end, end)
        if index >= end:
            return properties
        if source[index] != ",":
            raise ValueError("expected JavaScript property delimiter")
        index += 1


def _static_javascript_array(literal: str) -> list[str]:
    source = literal.strip()
    if len(source) < 2 or source[0] != "[" or source[-1] != "]":
        raise ValueError("expected JavaScript array literal")
    values: list[str] = []
    index = 1
    end = len(source) - 1
    while True:
        index = _skip_javascript_trivia(source, index, end)
        if index >= end:
            return values
        value_end = _javascript_value_end(source, index, end)
        value = source[index:value_end].strip()
        if not value:
            raise ValueError("empty JavaScript array value")
        values.append(value)
        index = _skip_javascript_trivia(source, value_end, end)
        if index >= end:
            return values
        if source[index] != ",":
            raise ValueError("expected JavaScript array delimiter")
        index += 1


def _static_javascript_string(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(_JS_DOUBLE_STRING, value):
        raise ValueError("expected static double-quoted JavaScript string")
    decoded = json.loads(value)
    if not isinstance(decoded, str):
        raise ValueError("expected JavaScript string value")
    return decoded


class _InlineScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() == "script" and self._current is None:
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._current is not None:
            self.scripts.append("".join(self._current))
            self._current = None


class _SafeFragmentText(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {"address", "article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "td"}
    )
    _IGNORED_TAGS = frozenset({"script", "style", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._parts: list[str] = []
        self._ignored: list[str] = []

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        self._parts.clear()
        if text and len(self.blocks) < 500:
            self.blocks.append(text[:10_000])

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.casefold()
        if self._ignored:
            if tag in self._IGNORED_TAGS:
                self._ignored.append(tag)
            return
        if tag in self._IGNORED_TAGS:
            self._ignored.append(tag)
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag in self._BLOCK_TAGS:
            self._flush()

    def close(self) -> None:
        super().close()
        self._flush()


def _safe_fragment_blocks(fragment: str) -> list[str]:
    parser = _SafeFragmentText()
    parser.feed(fragment)
    parser.close()
    return parser.blocks


def _westlake_faculty_payload(document: str) -> dict[str, object]:
    scripts = _InlineScriptCollector()
    scripts.feed(document)
    scripts.close()
    candidates: list[str] = []
    call_pattern = re.compile(
        r"\bnew\s+facultyDetail\s*\(\s*['\"]#app['\"]\s*,\s*(?=\{)"
    )
    for script in scripts.scripts:
        for match in call_pattern.finditer(script):
            candidates.append(
                _balanced_javascript_literal(script, match.end(), "{", "}")
            )
    if len(candidates) != 1:
        raise ValueError("expected one facultyDetail payload")

    root = _static_javascript_object(candidates[0])
    if "personObj" not in root:
        raise ValueError("facultyDetail personObj missing")
    person_values = _static_javascript_object(root["personObj"])
    person: dict[str, str] = {}
    for field in ("name", "post", "lab", "email"):
        if field in person_values:
            person[field] = _static_javascript_string(person_values[field]).strip()
    name_key = re.sub(r"[^a-z]+", " ", person.get("name", "").casefold()).strip()
    if not name_key:
        raise ValueError("facultyDetail name missing")
    if not name_key.startswith("weicheng zang"):
        raise ValueError("facultyDetail identity mismatch")

    sections: dict[str, str] = {}
    for field in ("biographyStr", "historyStr", "researchStr"):
        if field in root:
            sections[field] = _static_javascript_string(root[field])

    representatives: list[tuple[str, str]] = []
    if "representativeList" in root:
        for item_literal in _static_javascript_array(root["representativeList"])[:50]:
            item = _static_javascript_object(item_literal)
            title = _static_javascript_string(item["title"]).strip() if "title" in item else ""
            content = _static_javascript_string(item["content"]) if "content" in item else ""
            if title or content:
                representatives.append((title, content))
    if not any(_safe_fragment_blocks(value) for value in sections.values()) and not any(
        _safe_fragment_blocks(content) for _, content in representatives
    ):
        raise ValueError("facultyDetail profile content missing")
    return {
        "person": person,
        "sections": sections,
        "representatives": representatives,
    }


def _westlake_faculty_html(payload: dict[str, object]) -> str:
    person = payload["person"]
    sections = payload["sections"]
    representatives = payload["representatives"]
    if not (
        isinstance(person, dict)
        and isinstance(sections, dict)
        and isinstance(representatives, list)
    ):
        raise ValueError("invalid facultyDetail projection payload")
    name = str(person["name"])
    profile_rows = "".join(
        f'<p data-field="{field}">{html.escape(str(person[field]))}</p>'
        for field in ("post", "lab", "email")
        if person.get(field)
    )
    section_labels = (
        ("biographyStr", "Biography"),
        ("historyStr", "Education and employment"),
        ("researchStr", "Research"),
    )
    body_sections: list[str] = []
    for field, label in section_labels:
        blocks = _safe_fragment_blocks(str(sections.get(field) or ""))
        if blocks:
            body_sections.append(
                f'<section data-field="{field}"><h2>{label}</h2>'
                + "".join(f"<p>{html.escape(block)}</p>" for block in blocks)
                + "</section>"
            )
    for index, item in enumerate(representatives):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        title, content = item
        blocks = _safe_fragment_blocks(str(content))
        if not blocks:
            continue
        heading = str(title).strip() or "Representative content"
        body_sections.append(
            f'<section data-field="representative-{index}">'
            f"<h2>{html.escape(heading)}</h2>"
            + "".join(f"<p>{html.escape(block)}</p>" for block in blocks)
            + "</section>"
        )
    return (
        "<!doctype html><html><head>"
        f"<title>{html.escape(name)}</title>"
        f'<link rel="canonical" href="{WESTLAKE_FACULTY_INLINE_URL}">'
        "</head><body><main>"
        f"<h1>{html.escape(name)}</h1>"
        f'<section data-field="profile"><h2>Profile</h2>{profile_rows}</section>'
        + "".join(body_sections)
        + "</main></body></html>"
    )


def _westlake_faculty_inline_projection(
    url: str,
    *,
    headers: dict[str, str],
) -> FetchObservation:
    """Project one public, static Westlake faculty payload into stable HTML."""

    if not _is_westlake_faculty_inline_url(url):
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=url,
            error="westlake_faculty_inline route rejected unsupported URL",
            retrieval_mode="westlake_faculty_inline",
        )
    observation = _http_fetch(url, headers=headers)
    observation.retrieval_mode = "westlake_faculty_inline"
    if observation.status_code == 304:
        return observation
    if observation.body is None or not 200 <= observation.status_code < 300:
        return observation
    fingerprint = _observation_fingerprint(observation)
    if fingerprint:
        return _sanitized_access_failure(observation, fingerprint)
    if not _is_westlake_faculty_inline_url(observation.final_url or ""):
        return FetchObservation(
            body=None,
            status_code=observation.status_code,
            final_url=observation.final_url,
            content_type=observation.content_type,
            error="westlake_faculty_inline final URL mismatch",
            etag=observation.etag,
            last_modified=observation.last_modified,
            observed_at=observation.observed_at,
            retrieval_mode="westlake_faculty_inline",
        )
    raw_body = observation.body
    if not isinstance(raw_body, (bytes, str)):
        parse_error = "unsupported response body"
    elif len(raw_body) > WESTLAKE_FACULTY_INLINE_MAX_BYTES:
        parse_error = "response exceeds projection size limit"
    else:
        try:
            document = (
                raw_body.decode("utf-8-sig", errors="strict")
                if isinstance(raw_body, bytes)
                else raw_body
            )
            projected_body = _westlake_faculty_html(
                _westlake_faculty_payload(document)
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            parse_error = str(exc)
        else:
            return FetchObservation(
                body=projected_body,
                status_code=observation.status_code,
                final_url=WESTLAKE_FACULTY_INLINE_URL,
                content_type="text/html",
                etag=observation.etag,
                last_modified=observation.last_modified,
                observed_at=observation.observed_at,
                retrieval_mode="westlake_faculty_inline",
            )
    return FetchObservation(
        body=None,
        status_code=observation.status_code,
        final_url=observation.final_url,
        content_type=observation.content_type,
        error=f"westlake_faculty_inline projection rejected: {parse_error}"[:800],
        etag=observation.etag,
        last_modified=observation.last_modified,
        observed_at=observation.observed_at,
        retrieval_mode="westlake_faculty_inline",
    )


def _looks_like_html(body: object) -> bool:
    prefix = _body_prefix(body, 8_192).decode("utf-8", errors="replace").casefold()
    return any(
        marker in prefix
        for marker in ("<!doctype html", "<html", "<head", "<body", "<main", "<h1")
    )


def _fetch_registered_alternate(
    route: str,
    url: str,
    *,
    site_url: str,
    headers: dict[str, str],
) -> FetchObservation | None:
    overview_user = _huggingface_overview_username(url)
    if route in {"huggingface_user_overview", "hf_user_overview", "public_json_api"}:
        if not overview_user:
            return None
        return _huggingface_user_projection(url, site_url=site_url, headers=headers)
    if route == "auto" and overview_user:
        return _huggingface_user_projection(url, site_url=site_url, headers=headers)

    observation = _http_fetch(
        url,
        headers={
            "User-Agent": headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        },
    )
    if observation.body is None or not 200 <= observation.status_code < 300:
        return observation
    if _observation_fingerprint(observation):
        return None
    content_type = (observation.content_type or "").casefold()
    raw_route = route == "raw_html" or (
        (urllib.parse.urlsplit(url).hostname or "").casefold()
        in {"raw.githubusercontent.com", "raw.github.com"}
    )
    if raw_route and not _looks_like_html(observation.body):
        return None
    if "json" in content_type and not raw_route:
        return None
    if "text/plain" in content_type and not _looks_like_html(observation.body):
        return None
    if not raw_route and not (
        "html" in content_type or _looks_like_html(observation.body)
    ):
        return None
    observation.retrieval_mode = (
        "alternate_raw_html" if raw_route else "alternate_public_url"
    )
    return observation


def _fetch_alternate_routes(
    config: dict[str, object],
    *,
    site_url: str,
    headers: dict[str, str],
) -> FetchObservation | None:
    for route, url in _alternate_route_specs(config):
        observation = _fetch_registered_alternate(
            route,
            url,
            site_url=site_url,
            headers=headers,
        )
        if (
            observation is not None
            and observation.body is not None
            and 200 <= observation.status_code < 300
        ):
            return observation
    return None


def _homepage_body_is_low_quality(observation: FetchObservation) -> bool:
    """Recognize a successful HTTP response that is only a SPA/title shell."""

    if observation.body is None or not 200 <= observation.status_code < 300:
        return False
    try:
        snapshot = extract_snapshot("homepage", observation.body)
        _, _, usable = assess_snapshot(snapshot)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return True
    return not usable


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="people-tracker-lite")
    root.add_argument("--db", default=".people_intel/light-tracker.sqlite3")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    imp = commands.add_parser("import-dossier")
    imp.add_argument("path")
    imp.add_argument("--format", choices=("icml", "apple"), required=True)
    scan = commands.add_parser("scan-live")
    scan.add_argument("--person-key")
    # Kept as a hidden no-op so old scheduler invocations fail safe while
    # upgrading.  There is intentionally no corresponding enable flag.
    scan.add_argument("--no-deepseek", action="store_true", help=argparse.SUPPRESS)
    report = commands.add_parser("report")
    report.add_argument("run_id")
    linkedin_due = commands.add_parser("linkedin-due")
    linkedin_due.add_argument("--as-of")
    linkedin_state = commands.add_parser("linkedin-state")
    linkedin_state.add_argument("source_id")
    linkedin_config = commands.add_parser("linkedin-config")
    linkedin_config.add_argument("source_id")
    linkedin_config.add_argument("--cadence-days", type=int, default=7)
    linkedin_ingest = commands.add_parser("linkedin-ingest")
    linkedin_ingest.add_argument("path")
    linkedin_report = commands.add_parser("linkedin-report")
    linkedin_report.add_argument("run_id")
    commands.add_parser("list")
    return root


def _fetch(source: dict[str, object]) -> FetchObservation:
    url = str(source["url"])
    if _safe_registered_url(url) is None:
        return FetchObservation(
            body=None,
            status_code=0,
            final_url=url,
            error="registered source URL failed the public URL safety policy",
        )
    if source.get("kind") == "github":
        login = url.rstrip("/").rsplit("/", 1)[-1]
        api_headers = {
            "User-Agent": "people-tracker-lite/2.0 (+research profile change monitor)",
            "Accept": "application/vnd.github+json",
        }
        github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if github_token:
            api_headers["Authorization"] = f"Bearer {github_token}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"https://api.github.com/users/{login}", headers=api_headers),
                timeout=30,
            ) as profile_response:
                profile = json.loads(profile_response.read())
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.github.com/users/{login}/repos?per_page=100&sort=updated",
                    headers=api_headers,
                ),
                timeout=30,
            ) as repositories_response:
                repositories = json.loads(repositories_response.read())
            return FetchObservation(
                body={"profile": profile, "repositories": repositories},
                status_code=200,
                final_url=url,
                content_type="application/json",
            )
        except urllib.error.HTTPError as exc:
            return FetchObservation(
                body=None,
                status_code=exc.code,
                final_url=exc.geturl(),
                error=str(exc),
            )
        except Exception as exc:
            return FetchObservation(body=None, status_code=0, final_url=url, error=str(exc))
    headers = {
        "User-Agent": "people-tracker-lite/2.0 (+research profile change monitor)",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    request_url = _scholar_tracking_url(url) if source.get("kind") == "scholar" else url
    force_full_fetch = bool(source.get("_force_full_fetch"))
    if source.get("etag") and not force_full_fetch:
        headers["If-None-Match"] = str(source["etag"])
    if source.get("last_modified") and not force_full_fetch:
        headers["If-Modified-Since"] = str(source["last_modified"])
    config = _retrieval_config(source)
    if config.get("preferred_route") == "westlake_faculty_inline":
        return _westlake_faculty_inline_projection(url, headers=headers)
    if config.get("preferred_route") == "wordpress_rest":
        endpoint = str(
            config.get("wordpress_endpoint")
            or f"{url.rstrip('/')}/wp-json/wp/v2/posts?"
            + urllib.parse.urlencode(
                {
                    "per_page": 50,
                    "_fields": "id,date,modified,link,slug,title,excerpt",
                }
            )
        )
        projected = _wordpress_projection(
            endpoint,
            site_url=url,
            site_name=str(config.get("site_name") or source.get("canonical_name") or url),
            headers={"User-Agent": headers["User-Agent"], "Accept": "application/json"},
        )
        if projected.body is not None:
            return projected

    direct = _http_fetch(request_url, headers=headers)
    direct_fingerprint = _observation_fingerprint(direct)
    if (
        direct.body is not None
        and direct_fingerprint is None
        and config.get("follow_meta_refresh")
        and isinstance(direct.body, bytes)
    ):
        text = direct.body.decode("utf-8", errors="replace")
        refresh = re.search(
            r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+content=[\"'][^\"']*url=([^\"'>]+)",
            text,
            re.I,
        )
        if refresh:
            target = urllib.parse.urljoin(url, html.unescape(refresh.group(1).strip()))
            registered_target = _safe_registered_url(target)
            if registered_target:
                followed = _http_fetch(registered_target, headers=headers)
                if followed.body is not None:
                    followed.retrieval_mode = "meta_refresh"
                    direct = followed
                    direct_fingerprint = _observation_fingerprint(direct)
            else:
                direct.retrieval_mode = "meta_refresh_rejected"
    alternate_specs = _alternate_route_specs(config)
    low_quality_shell = (
        source.get("kind") == "homepage"
        and bool(alternate_specs)
        and direct_fingerprint is None
        and _homepage_body_is_low_quality(direct)
    )
    if direct.status_code == 304 or (
        direct.body is not None
        and direct_fingerprint is None
        and not low_quality_shell
    ):
        return direct

    fallback = _fetch_alternate_routes(
        config,
        site_url=url,
        headers=headers,
    )
    if fallback is not None:
        return fallback
    if direct_fingerprint is not None and direct.body is not None:
        return _sanitized_access_failure(direct, direct_fingerprint)
    return direct


def main() -> None:
    args = parser().parse_args()
    tracker = LightTracker(args.db)
    active_run_id: str | None = None
    try:
        if args.command == "init":
            print(json.dumps({"database": str(Path(args.db).resolve()), "status": "ready"}, ensure_ascii=False))
        elif args.command == "import-dossier":
            counts = {"input": 0, "admitted": 0, "needs_discovery": 0}
            rejected = []
            for record in dossier_records(args.path, args.format):
                counts["input"] += 1
                try:
                    tracker.add_person(**record)
                    counts["admitted"] += 1
                except ValueError:
                    counts["needs_discovery"] += 1
                    rejected.append(record["canonical_name"])
            print(json.dumps({"counts": counts, "needs_discovery": rejected}, ensure_ascii=False, indent=2))
        elif args.command == "list":
            print(json.dumps(tracker.list_people(), ensure_ascii=False, indent=2))
        elif args.command == "scan-live":
            run_id = tracker.start_run("cli-live")
            active_run_id = run_id
            for person in tracker.list_people():
                if args.person_key and person["person_key"] != args.person_key:
                    continue
                for source in person["sources"]:
                    if int(source.get("tracking_enabled", 1)) != 1:
                        continue
                    tracker.observe(
                        run_id,
                        source["source_id"],
                        _fetch(source),
                    )
            tracker.complete_run(run_id)
            active_run_id = None
            print(json.dumps({
                "run_id": run_id,
                "agent_review": {
                    "mode": "execution_agent",
                    "decision_authority": "skill_host_agent",
                    "external_model_api_called": False,
                },
                "report": tracker.render_report(run_id),
            }, ensure_ascii=False, indent=2))
        elif args.command == "report":
            print(tracker.render_report(args.run_id))
        elif args.command == "linkedin-due":
            print(json.dumps(tracker.list_due_linkedin(args.as_of), ensure_ascii=False, indent=2))
        elif args.command == "linkedin-state":
            print(json.dumps(tracker.linkedin_profile_state(args.source_id), ensure_ascii=False, indent=2))
        elif args.command == "linkedin-config":
            print(json.dumps(
                tracker.configure_linkedin_weekly(
                    args.source_id,
                    cadence_days=args.cadence_days,
                ),
                ensure_ascii=False,
                indent=2,
            ))
        elif args.command == "linkedin-ingest":
            payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
            entries = payload.get("observations", payload) if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                raise ValueError("linkedin-ingest expects a list or {'observations': [...]} JSON")
            run_id = tracker.start_run("weekly-linkedin")
            active_run_id = run_id
            outcomes = []
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("source_id"):
                    raise ValueError("each LinkedIn observation must contain source_id")
                observation = FetchObservation(
                    body=entry.get("body"),
                    status_code=int(entry.get("status_code", 200)),
                    final_url=entry.get("final_url"),
                    content_type=entry.get("content_type", "application/json"),
                    error=entry.get("error"),
                    etag=entry.get("etag"),
                    last_modified=entry.get("last_modified"),
                    observed_at=entry.get("observed_at") or utc_now(),
                    retrieval_mode=entry.get("retrieval_mode", "browser_public"),
                )
                decision = tracker.observe(run_id, entry["source_id"], observation)
                outcomes.append({
                    "source_id": entry["source_id"],
                    "decision": decision.status,
                    "summary": decision.summary,
                })
            tracker.complete_run(run_id)
            active_run_id = None
            print(json.dumps({
                "run_id": run_id,
                "outcomes": outcomes,
                "report": tracker.render_linkedin_weekly_report(run_id),
            }, ensure_ascii=False, indent=2))
        elif args.command == "linkedin-report":
            print(tracker.render_linkedin_weekly_report(args.run_id))
    except BaseException as exc:
        if active_run_id is not None:
            tracker.fail_run(active_run_id, exc)
        raise
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
