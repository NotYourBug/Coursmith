"""Shared request boundaries; callers still own authentication and authorization."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import unquote_to_bytes

from starlette.requests import Request

from .audit import AuditEvent, record_denial
from .database import to_db_time, transaction
from .domain import BusinessError, Clock, utc_now
from .settings import _origin_tuple


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise BusinessError("ambiguous_header", "Duplicate request header.", 400)
    return values[0] if values else None


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Bound the ASGI stream before any parsing, regardless of Content-Length.

    Only UTF-8 URL-encoded forms (64 KiB) and JSON (16 KiB) are supported.
    A smaller caller limit is respected. This returns bytes, not parsed JSON.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("Body limit must be a positive integer")
    content_type = _single_header(request, "content-type")
    parts = [part.strip().lower() for part in (content_type or "").split(";")]
    bounds = {"application/x-www-form-urlencoded": 65536, "application/json": 16384}
    if parts[0] not in bounds or len(parts) > 2 or (
        len(parts) == 2 and parts[1] not in ('charset=utf-8', 'charset="utf-8"')
    ):
        raise BusinessError("unsupported_media_type", "Unsupported request content type.", 415)
    limit = min(limit, bounds[parts[0]])
    length = _single_header(request, "content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]+", length) or len(length) > 20:
            raise BusinessError("invalid_content_length", "Invalid request length.", 400)
        if int(length) > limit:
            raise BusinessError("body_too_large", "Request body is too large.", 413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise BusinessError("body_too_large", "Request body is too large.", 413)
        body.extend(chunk)
    return bytes(body)


def parse_unique_form(body: bytes) -> dict[str, str]:
    """Strict UTF-8 URL-encoded parsing; reject all duplicate decoded keys."""
    result: dict[str, str] = {}
    try:
        text = body.decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9*_.~%+&=\-]*", text):
            raise ValueError
        if not text:
            return result
        for pair in text.split("&"):
            if "=" not in pair or re.search(r"%(?![0-9a-fA-F]{2})", pair):
                raise ValueError
            name, value = (
                unquote_to_bytes(part.replace("+", " ")).decode("utf-8")
                for part in pair.split("=", 1)
            )
            if not name or name in result or "\x00" in name or "\x00" in value:
                raise ValueError
            result[name] = value
    except (UnicodeError, ValueError):
        raise BusinessError("invalid_form", "Malformed or duplicate form fields.", 400) from None
    return result


def check_origin(request: Request, site_origin: str) -> None:
    """Require one same-site Origin; Host/Referer/proxy headers grant no trust."""
    try:
        origin = _single_header(request, "origin")
        if origin is None or _origin_tuple(origin) != _origin_tuple(site_origin):
            raise ValueError
    except (ValueError, BusinessError):
        raise BusinessError("invalid_origin", "Request origin is not allowed.", 403) from None


def source_key(request: Request, trusted_proxy_cidrs: tuple[str, ...]) -> str:
    """Hash the canonical IP reached through an explicit X-Forwarded-For chain.

    Forwarded and X-Real-IP are deliberately unsupported. The deploying proxy
    must append the incoming peer to X-Forwarded-For and preserve the real ASGI
    socket peer (disable server-side automatic forwarded-header rewriting).
    """
    networks = tuple(ip_network(cidr) for cidr in trusted_proxy_cidrs)
    try:
        if request.client is None:
            raise ValueError
        if "%" in request.client.host:
            raise ValueError
        source = ip_address(request.client.host)
        if any(source in network for network in networks):
            forwarded = _single_header(request, "x-forwarded-for")
            if forwarded is not None:
                for hop in reversed(forwarded.split(",")):
                    if not any(source in network for network in networks):
                        break
                    if "%" in hop:
                        raise ValueError
                    source = ip_address(hop.strip())
    except ValueError:
        raise BusinessError("invalid_source", "Request source is unavailable or invalid.", 400) from None
    return hashlib.sha256(str(source).encode("ascii")).hexdigest()


def _csrf_hash(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise BusinessError("invalid_csrf", "CSRF token is invalid or expired.", 403)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class CsrfService:
    """Persistent ten-minute prechallenges, plus session-bound token validation.

    A challenge proves possession of its cookie, never identity or entitlement.
    Auth/recovery domains own session nonce rotation inside their transactions.
    After consuming a prechallenge, an error response must issue a fresh one
    for both form and cookie and retain only its own non-sensitive fields.
    """

    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = db_path
        self.clock = clock

    def issue_challenge(self, scope: str) -> str:
        token = secrets.token_urlsafe(32)
        nonce_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with transaction(self.db_path, immediate=True) as connection:
            connection.execute(
                "INSERT INTO csrf_challenges (nonce_hash, scope, expires_at) VALUES (?, ?, ?)",
                (nonce_hash, scope, to_db_time(self.clock() + timedelta(minutes=10))),
            )
        return token

    @staticmethod
    def verify_bound_csrf(form_token: str, cookie_token: str, stored_hash: str) -> None:
        """Compare form, cookie and the caller's authenticated session hash."""
        if (not isinstance(form_token, str) or not isinstance(cookie_token, str)
                or not isinstance(stored_hash, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", form_token)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie_token)
                or not re.fullmatch(r"[0-9a-f]{64}", stored_hash)):
            raise BusinessError("invalid_csrf", "CSRF token is invalid or expired.", 403)
        nonce_hash = hashlib.sha256(form_token.encode("ascii")).hexdigest()
        equal_tokens = hmac.compare_digest(form_token, cookie_token)
        equal_hash = hmac.compare_digest(nonce_hash, stored_hash)
        if not (equal_tokens & equal_hash):
            raise BusinessError("invalid_csrf", "CSRF token is invalid or expired.", 403)

    def consume_challenge(self, scope: str, form_token: str, cookie_token: str, *,
                          audit_denial: bool = True) -> None:
        """Consume once; an HTTP boundary may own its correlated denial audit."""
        try:
            # The conditional update binds this digest to its persisted scope,
            # lifetime and unused state; the pair check cannot authorize alone.
            nonce_hash = _csrf_hash(form_token)
            self.verify_bound_csrf(form_token, cookie_token, nonce_hash)
            with transaction(self.db_path, immediate=True) as connection:
                now = to_db_time(self.clock())
                updated = connection.execute(
                    """UPDATE csrf_challenges SET consumed_at=?
                       WHERE nonce_hash=? AND scope=? AND consumed_at IS NULL AND expires_at>?""",
                    (now, nonce_hash, scope, now),
                )
                if updated.rowcount != 1:
                    raise BusinessError("invalid_csrf", "CSRF token is invalid or expired.", 403)
        except BusinessError as error:
            if error.code == "invalid_csrf" and audit_denial:
                record_denial(self.db_path, AuditEvent(
                    actor_admin_id=None, object_type="request", object_id="prechallenge",
                    action="security.csrf", reason="invalid_csrf", outcome="denied",
                    request_id=secrets.token_hex(16), changes={"error_code": "invalid_csrf"},
                ))
            raise
