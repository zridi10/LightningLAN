"""Access control for the HTTP server, as plain ASGI middleware (it must not buffer streams).

* Only GET/HEAD /api/files, /api/games, /api/download/{id} and the swarm routes (/api/pieces/{id},
  /api/swarm/{id}, /api/piece/{id}/{n}) are reachable from the LAN. When a
  passphrase is set they require a valid peer token, and responses carry a proof header.
  While this peer is stopped (offline) they answer 503, and transfers in progress are cut off.
* The phone page (/phone) and the page's own files (/static/) are open to the LAN; opening this
  computer's address on a phone leads to the phone page. The phone API (/phone/api/...) requires
  the phone cookie, which holds the key from the QR code in the web UI. Phone requests that
  change something must carry X-LightningLAN-Client and, if the browser sent an Origin, the
  page's own (blocking cross-site request forgery).
* Everything else (the web UI, uploads, starting downloads) is for the local user only. It
  requires a loopback client address and a localhost Host header (blocking DNS rebinding).
  State-changing requests must also carry X-LightningLAN-Client and, if the browser sent an
  Origin, a local one (blocking cross-site request forgery from other websites).
"""

from __future__ import annotations

import ipaddress
import json
from typing import Callable
from urllib.parse import urlsplit

from starlette.requests import cookie_parser

from .auth import PROOF_HEADER, TOKEN_HEADER, AuthError, Authenticator
from .phone import COOKIE as PHONE_COOKIE
from .serving import UploadsCut

CLIENT_HEADER = "x-lightninglan-client"
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


_LAN_PREFIXES = ("/api/download/", "/api/pieces/", "/api/swarm/", "/api/piece/")


def is_lan_route(path: str) -> bool:
    return path in ("/api/files", "/api/games") or path.startswith(_LAN_PREFIXES)


def is_phone_route(path: str) -> bool:
    return path == "/phone" or path.startswith("/phone/")


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_local_host_header(host_header: str | None) -> bool:
    if host_header is None:
        return True  # non-browser HTTP/1.0 clients; browsers always send Host
    name = _hostname(host_header)
    return name in _LOCAL_HOSTNAMES or is_loopback(name)


def is_local_origin(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return host in _LOCAL_HOSTNAMES or is_loopback(host)


def is_same_origin(origin: str, host_header: str | None) -> bool:
    """True if the Origin a browser sent is the site it is talking to (http://<Host header>)."""
    if not host_header:
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme == "http" and parts.netloc.lower() == host_header.strip().lower()


class AccessControlMiddleware:
    def __init__(self, app, auth: Authenticator, online: Callable[[], bool] = lambda: True,
                 phone_key_ok: Callable[[str | None], bool] = lambda key: False):
        self.app = app
        self.auth = auth
        self.online = online
        self.phone_key_ok = phone_key_ok

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        method: str = scope["method"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

        if is_lan_route(path):
            if method not in ("GET", "HEAD"):
                await _reject(send, 405, "method_not_allowed", "Only GET and HEAD are allowed here.")
                return
            if not self.online():
                await _reject(send, 503, "offline", "This peer has stopped sharing.")
                return
            if self.auth.enabled:
                token = headers.get(TOKEN_HEADER.lower())
                try:
                    self.auth.verify_request(method, path, token)
                except AuthError as exc:
                    await _reject(send, 401, "auth_failed", str(exc), reason=exc.reason)
                    return
                send = _with_proof(send, self.auth.proof_for(token or ""))
            try:
                await self.app(scope, receive, send)
            except UploadsCut:
                pass  # this peer went offline mid-transfer; ending early makes the server drop the connection
            return

        if path.startswith("/static/") and method in ("GET", "HEAD"):
            await self.app(scope, receive, send)  # the pages' own scripts, styles and pictures
            return
        if is_phone_route(path):
            await self._phone(scope, receive, send, path, method, headers)
            return

        client = scope.get("client")
        if not client or not is_loopback(client[0]):
            if path == "/" and method in ("GET", "HEAD"):
                await _redirect(send, "/phone")  # someone typed this computer's address on their phone
                return
            await _reject(send, 403, "local_only", "This page is only available on the computer running LightningLAN.")
            return
        if not is_local_host_header(headers.get("host")):
            await _reject(send, 403, "bad_host", "Unexpected Host header.")
            return
        if method not in _SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None and not is_local_origin(origin):
                await _reject(send, 403, "bad_origin", "Cross-site requests are not allowed.")
                return
            if CLIENT_HEADER not in headers:
                await _reject(send, 403, "missing_client_header", "Missing X-LightningLAN-Client header.")
                return
        await self.app(scope, receive, send)

    async def _phone(self, scope, receive, send, path: str, method: str, headers: dict[str, str]) -> None:
        if method not in ("GET", "HEAD", "POST"):
            await _reject(send, 405, "method_not_allowed", "Method not allowed.")
            return
        if path != "/phone":  # the page itself is open: it tells an unpaired phone how to connect
            key = cookie_parser(headers.get("cookie", "")).get(PHONE_COOKIE)
            if not self.phone_key_ok(key):
                await _reject(send, 401, "not_paired", "Scan the QR code in LightningLAN to connect this phone.")
                return
        if method == "POST":
            origin = headers.get("origin")
            if origin is not None and not is_same_origin(origin, headers.get("host")):
                await _reject(send, 403, "bad_origin", "Cross-site requests are not allowed.")
                return
            if CLIENT_HEADER not in headers:
                await _reject(send, 403, "missing_client_header", "Missing X-LightningLAN-Client header.")
                return
        try:
            await self.app(scope, receive, send)
        except UploadsCut:
            pass  # a download to the phone ended early: dropping the connection tells its browser


def _with_proof(send, proof: str):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            message = dict(message)
            message["headers"] = list(message.get("headers", [])) + [
                (PROOF_HEADER.lower().encode("latin-1"), proof.encode("latin-1"))
            ]
        await send(message)

    return wrapped


async def _redirect(send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _reject(send, status: int, error: str, detail: str, reason: str | None = None) -> None:
    payload = {"error": error, "detail": detail}
    if reason:
        payload["reason"] = reason
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("latin-1")),
]


class SecurityHeadersMiddleware:
    """Hardening headers on every response; the UI's own files are never cached stale."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        no_cache = path == "/" or path.startswith("/static/")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if not (no_cache and h[0].lower() == b"cache-control")]
                headers += _SECURITY_HEADERS
                if no_cache:
                    headers.append((b"cache-control", b"no-cache"))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, wrapped)
