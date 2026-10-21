"""Access control for the HTTP server, as plain ASGI middleware (it must not buffer streams).

* Only GET/HEAD /api/files and /api/download/{id} are reachable from the LAN. When a
  passphrase is set they require a valid peer token, and responses carry a proof header.
* Everything else (the web UI, uploads, starting downloads) is for the local user only. It
  requires a loopback client address and a localhost Host header (blocking DNS rebinding).
  State-changing requests must also carry X-LanShare-Client and, if the browser sent an
  Origin, a local one (blocking cross-site request forgery from other websites).
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

from .auth import PROOF_HEADER, TOKEN_HEADER, AuthError, Authenticator

CLIENT_HEADER = "x-lanshare-client"
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_lan_route(path: str) -> bool:
    return path == "/api/files" or path.startswith("/api/download/")


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


class AccessControlMiddleware:
    def __init__(self, app, auth: Authenticator):
        self.app = app
        self.auth = auth

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
            if self.auth.enabled:
                token = headers.get(TOKEN_HEADER.lower())
                try:
                    self.auth.verify_request(method, path, token)
                except AuthError as exc:
                    await _reject(send, 401, "auth_failed", str(exc), reason=exc.reason)
                    return
                send = _with_proof(send, self.auth.proof_for(token or ""))
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if not client or not is_loopback(client[0]):
            await _reject(send, 403, "local_only", "This page is only available on the computer running LanShare.")
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
                await _reject(send, 403, "missing_client_header", "Missing X-LanShare-Client header.")
                return
        await self.app(scope, receive, send)


def _with_proof(send, proof: str):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            message = dict(message)
            message["headers"] = list(message.get("headers", [])) + [
                (PROOF_HEADER.lower().encode("latin-1"), proof.encode("latin-1"))
            ]
        await send(message)

    return wrapped


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
