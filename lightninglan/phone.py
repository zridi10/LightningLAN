"""Phones: a phone on the same Wi-Fi uses LightningLAN through this computer, in its web browser.

The web UI shows a QR code with a link that holds a secret key (Node.ensure_phone_key). Opening
the link keeps the key in a cookie, and with it the phone page can list ThunderCLOUD, download
any file in it and send files into the shared folder. Files this computer shares are sent
directly; files only other peers have are relayed from one of them as they arrive, with the
SHA-256 checked on the way, so a damaged file never finishes downloading on the phone.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import quote

import httpx
import segno
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .pathsafe import display_name
from .peers import describe_http_error
from .serving import UploadLimiter, UploadsCut

if TYPE_CHECKING:
    from .node import Node

log = logging.getLogger(__name__)

COOKIE = "lightninglan_phone"
COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers keep a cookie for at most 400 days
RELAY_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_PASSED_ON = ("content-length", "content-range", "accept-ranges", "etag")


class RelayEnded(UploadsCut):
    """A relayed download stopped early (the peer went away, or the data failed its SHA-256 check).

    Ending the response before all of it was sent makes the phone's browser report the download
    as failed instead of saving part of a file.
    """


def phone_links(addresses: list[str], port: int, key: str) -> list[dict]:
    """The phone link for each address, with its QR code as rows of "1" (dark) and "0" (light)."""
    links = []
    for address in addresses:
        url = f"http://{address}:{port}/phone?key={key}"
        qr = segno.make(url, error="m", micro=False)
        rows = ["".join("1" if dark else "0" for dark in row) for row in qr.matrix_iter(scale=1, border=0)]
        links.append({"address": address, "url": url, "qr": rows})
    return links


async def hold_until_verified(chunks: AsyncIterator[bytes], sha256: str) -> AsyncIterator[bytes]:
    """Pass the chunks on, but keep the last one back until the whole file matched its SHA-256."""
    digest = hashlib.sha256()
    held = b""
    async for chunk in chunks:
        if not chunk:
            continue
        digest.update(chunk)
        if held:
            yield held
        held = chunk
    if digest.hexdigest() != sha256:
        raise RelayEnded("the relayed file failed SHA-256 verification")
    if held:
        yield held


async def _relay_body(chunks: AsyncIterator[bytes], upstream: httpx.Response, client: httpx.AsyncClient,
                      limiter: UploadLimiter, source: str) -> AsyncIterator[bytes]:
    generation = limiter.generation
    try:
        async for chunk in chunks:
            if not limiter.allows(generation):
                raise UploadsCut()  # this peer stopped sharing
            yield chunk
    except httpx.HTTPError as exc:
        log.info("relaying a file from %s to a phone stopped: %s", source, describe_http_error(exc))
        raise RelayEnded(str(exc)) from exc
    except RelayEnded:
        log.warning("a file relayed from %s to a phone failed SHA-256 verification", source)
        raise
    finally:
        await upstream.aclose()
        await client.aclose()


async def relay_download(node: Node, file_id: str, request: Request) -> Response:
    """Stream a file that only other peers have to the phone, from the first peer that sends it."""
    sources = node.downloads.sources(file_id)
    if not sources:
        return JSONResponse({"error": "not_found", "detail": "No online peer has this file any more."},
                            status_code=404)
    path = f"/api/download/{file_id}"
    forwarded = {name: request.headers[name] for name in ("range", "if-range") if name in request.headers}
    problem = "No peer could send the file."
    for peer, remote in sources:
        headers = node.auth.sign_request(request.method, path)
        headers.update(forwarded)
        client = httpx.AsyncClient(timeout=RELAY_TIMEOUT, trust_env=False)
        try:
            upstream = await client.send(
                client.build_request(request.method, f"{peer.base_url()}{path}", headers=headers), stream=True
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            problem = f"Could not reach {peer.name}: {describe_http_error(exc)}."
            continue
        if not node.auth.verify_response(headers, upstream.headers) or upstream.status_code not in (200, 206, 416):
            problem = f"{peer.name} could not send the file (HTTP {upstream.status_code})."
            await upstream.aclose()
            await client.aclose()
            continue

        out = {name: upstream.headers[name] for name in _PASSED_ON if name in upstream.headers}
        if upstream.status_code == 416 or request.method == "HEAD":
            await upstream.aclose()
            await client.aclose()
            return Response(status_code=upstream.status_code, headers=out)
        filename = display_name(remote.name).rsplit("/", 1)[-1]
        out["content-disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        out["cache-control"] = "no-store"
        chunks: AsyncIterator[bytes] = upstream.aiter_raw()
        if upstream.status_code == 200:
            chunks = hold_until_verified(chunks, file_id)  # a whole file can be checked before it ends
        return StreamingResponse(
            _relay_body(chunks, upstream, client, node.uploads, peer.name),
            status_code=upstream.status_code,
            headers=out,
            media_type="application/octet-stream",
        )
    return JSONResponse({"error": "unavailable", "detail": problem}, status_code=502)


def hold_session(node: Node, response: Response) -> Response:
    """Keep this peer online while a phone downloads, even if the phone page itself goes to sleep."""
    if not isinstance(response, StreamingResponse):
        return response
    inner = response.body_iterator

    async def held() -> AsyncIterator:
        node.ui_opened()
        try:
            async for chunk in inner:
                yield chunk
        finally:
            node.ui_closed()

    response.body_iterator = held()
    return response
