"""Neon Managed Better Auth (NEON_AUTH_BASE_URL) same-origin proxy, session resolver, and EdDSA JWT verifier."""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient
import requests
from requests.adapters import HTTPAdapter

from models import load_env_files, resolve_neon_auth_urls

logger = logging.getLogger("neon_auth")

_AUTH_HTTP_SESSION = requests.Session()
_AUTH_HTTP_SESSION.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=16))

_JWKS_LOCK = threading.Lock()
_JWKS_CLIENT_CACHE: dict[str, PyJWKClient] = {}


def is_neon_auth_configured() -> bool:
    """Return True when NEON_AUTH_BASE_URL is configured."""
    base_url, _, _ = resolve_neon_auth_urls()
    return bool(base_url)


def neon_auth_status() -> dict[str, Any]:
    """Return non-secret Neon Auth metadata safe for /api/health."""
    base_url, jwks_url, issuer_origin = resolve_neon_auth_urls()
    return {
        "provider": "neon-auth",
        "configured": bool(base_url),
        "base_url": base_url,
        "jwks_url": jwks_url,
        "issuer": issuer_origin,
    }


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    with _JWKS_LOCK:
        client = _JWKS_CLIENT_CACHE.get(jwks_url)
        if client is None:
            client = PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
            _JWKS_CLIENT_CACHE[jwks_url] = client
        return client


def _normalize_cookies_for_upstream(cookie_header: str) -> str:
    """Restore __Secure- prefix when localhost HTTP stripped it from Better Auth session cookies."""
    raw = (cookie_header or "").strip()
    if not raw:
        return ""
    parts: list[str] = []
    seen_names: set[str] = set()
    raw_pairs = [segment.strip() for segment in raw.split(";") if "=" in segment]
    for pair in raw_pairs:
        name, val = pair.split("=", 1)
        name = name.strip()
        seen_names.add(name)
        parts.append(f"{name}={val.strip()}")
    for pair in raw_pairs:
        name, val = pair.split("=", 1)
        name = name.strip()
        if name.startswith("neon-auth.") and f"__Secure-{name}" not in seen_names:
            parts.append(f"__Secure-{name}={val.strip()}")
    return "; ".join(parts)


def _rewrite_upstream_set_cookie(raw_cookie: str, *, is_https: bool) -> str:
    """Rewrite upstream Better Auth Set-Cookie header so it acts cleanly as a first-party portal cookie."""
    segments = [seg.strip() for seg in raw_cookie.split(";") if seg.strip()]
    if not segments or "=" not in segments[0]:
        return raw_cookie
    name_val = segments[0]
    cname, cval = name_val.split("=", 1)
    cname = cname.strip()

    if not is_https and cname.startswith("__Secure-"):
        cname = cname[len("__Secure-") :]

    kept_attrs: list[str] = []
    has_path = False
    for attr in segments[1:]:
        low = attr.lower()
        if low.startswith("domain="):
            continue  # Bind host-only to portal domain (grants.zysgoldfields.pro / localhost)
        if low == "partitioned":
            if not is_https:
                continue
        if low == "secure" and not is_https:
            continue
        if low.startswith("samesite="):
            if not is_https:
                kept_attrs.append("SameSite=Lax")
                continue
        if low.startswith("path="):
            has_path = True
            kept_attrs.append("Path=/")
            continue
        kept_attrs.append(attr)

    if not has_path:
        kept_attrs.append("Path=/")

    return "; ".join([f"{cname}={cval}", *kept_attrs])


def proxy_auth_request(
    *,
    method: str,
    subpath: str,
    query: str,
    req_headers: Any,
    req_body: bytes,
    is_https: bool,
) -> tuple[int, str, list[str], bytes]:
    """Forward /api/auth/* requests cleanly to Neon Managed Better Auth (NEON_AUTH_BASE_URL)."""
    base_url, _, _ = resolve_neon_auth_urls()
    if not base_url:
        err = b'{"error":"Neon Auth is not configured (NEON_AUTH_BASE_URL missing)."}'
        return 503, "application/json; charset=utf-8", [], err

    clean_sub = "/" + subpath.lstrip("/")
    if ".." in clean_sub:
        return 400, "application/json; charset=utf-8", [], b'{"error":"Invalid auth path."}'

    target_url = f"{base_url}{clean_sub}"
    if query:
        target_url = f"{target_url}?{query}"

    forward_headers: dict[str, str] = {
        "Accept": req_headers.get("Accept") or "application/json",
        "User-Agent": req_headers.get("User-Agent") or "ZYS-GoldFields-AuthProxy/1.0",
    }
    content_type = req_headers.get("Content-Type")
    if content_type:
        forward_headers["Content-Type"] = content_type
    authz = req_headers.get("Authorization")
    if authz:
        forward_headers["Authorization"] = authz

    cookie_hdr = _normalize_cookies_for_upstream(req_headers.get("Cookie", ""))
    if cookie_hdr:
        forward_headers["Cookie"] = cookie_hdr

    origin_hdr = (req_headers.get("Origin") or "").strip()
    if origin_hdr:
        parsed_orig = urlparse(origin_hdr)
        if parsed_orig.hostname == "127.0.0.1":
            port_suffix = f":{parsed_orig.port}" if parsed_orig.port else ""
            origin_hdr = f"{parsed_orig.scheme}://localhost{port_suffix}"
        forward_headers["Origin"] = origin_hdr
    else:
        host_hdr = (req_headers.get("X-Forwarded-Host") or req_headers.get("Host") or "").split(",")[0].strip()
        if host_hdr:
            scheme = "https" if is_https else "http"
            if host_hdr == "127.0.0.1" or host_hdr.startswith("127.0.0.1:"):
                host_hdr = host_hdr.replace("127.0.0.1", "localhost", 1)
            forward_headers["Origin"] = f"{scheme}://{host_hdr}"

    try:
        upstream_resp = _AUTH_HTTP_SESSION.request(
            method=method.upper(),
            url=target_url,
            headers=forward_headers,
            data=req_body if req_body else None,
            allow_redirects=False,
            timeout=15,
            stream=True,
        )
        raw_content = upstream_resp.content
    except Exception as exc:
        logger.warning("Neon Auth upstream proxy error (%s %s): %s", method, clean_sub, exc)
        err = f'{{"error":"Upstream Neon Auth request failed: {exc}"}}'.encode("utf-8")
        return 502, "application/json; charset=utf-8", [], err

    raw_set_cookies: list[str] = []
    raw_hdrs = getattr(upstream_resp.raw, "headers", None)
    if raw_hdrs is not None and hasattr(raw_hdrs, "getlist"):
        raw_set_cookies = list(raw_hdrs.getlist("Set-Cookie"))
    elif "Set-Cookie" in upstream_resp.headers:
        raw_set_cookies = [upstream_resp.headers["Set-Cookie"]]

    rewritten_cookies = [
        _rewrite_upstream_set_cookie(c, is_https=is_https)
        for c in raw_set_cookies
        if c and c.strip()
    ]
    resp_ct = upstream_resp.headers.get("Content-Type", "application/json; charset=utf-8")
    return int(upstream_resp.status_code), resp_ct, rewritten_cookies, raw_content


def verify_neon_jwt(token: str) -> dict[str, Any] | None:
    """Verify an EdDSA/RS256 Neon Auth JWT against NEON_AUTH_JWKS_URL."""
    load_env_files()
    base_url, jwks_url, issuer_origin = resolve_neon_auth_urls()
    if not base_url or not jwks_url:
        return None
    tok = (token or "").strip()
    if tok.count(".") != 2:
        return None
    try:
        jwks_client = _get_jwks_client(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(tok)
        claims = jwt.decode(
            tok,
            signing_key.key,
            algorithms=["EdDSA", "RS256"],
            issuer=issuer_origin or None,
            options={"verify_aud": False},
        )
    except Exception as exc:
        logger.debug("Neon JWT verification failed: %s", exc)
        return None

    if claims.get("banned") is True:
        return None

    user = {
        "id": claims.get("sub") or claims.get("id") or "",
        "email": claims.get("email") or "",
        "name": claims.get("name") or "",
        "role": claims.get("role") or "authenticated",
        "emailVerified": bool(claims.get("emailVerified", False)),
    }
    return {
        "authenticated": True,
        "auth_method": "neon_jwt",
        "user": user,
        "claims": claims,
    }


def verify_neon_session_cookie(cookie_header: str) -> dict[str, Any] | None:
    """Validate Better Auth session cookie via GET {NEON_AUTH_BASE_URL}/get-session."""
    base_url, _, _ = resolve_neon_auth_urls()
    if not base_url:
        return None
    normalized_cookie = _normalize_cookies_for_upstream(cookie_header)
    if "neon-auth.session_token=" not in normalized_cookie:
        return None
    try:
        resp = _AUTH_HTTP_SESSION.get(
            f"{base_url}/get-session",
            headers={
                "Cookie": normalized_cookie,
                "Accept": "application/json",
                "User-Agent": "ZYS-GoldFields-SessionCheck/1.0",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        logger.debug("Neon session check failed: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    user = data.get("user")
    session = data.get("session")
    if not isinstance(user, dict) or not isinstance(session, dict):
        return None
    if user.get("banned") is True:
        return None
    return {
        "authenticated": True,
        "auth_method": "neon_session",
        "user": user,
        "session": session,
    }


def authenticate_request(headers: Any) -> dict[str, Any] | None:
    """Authenticate an incoming HTTP request using Neon Auth Bearer JWT or session cookie."""
    if not is_neon_auth_configured():
        return None
    auth_header = (headers.get("Authorization") or "").strip()
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        jwt_ctx = verify_neon_jwt(token)
        if jwt_ctx is not None:
            return jwt_ctx

    cookie_hdr = (headers.get("Cookie") or "").strip()
    if cookie_hdr:
        sess_ctx = verify_neon_session_cookie(cookie_hdr)
        if sess_ctx is not None:
            return sess_ctx
    return None
