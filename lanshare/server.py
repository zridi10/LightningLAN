"""HTTP API and web UI for one peer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .node import Node
from .security import AccessControlMiddleware


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    candidates = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return etag in candidates or "*" in candidates


def create_app(node: Node) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await node.start()
        try:
            yield
        finally:
            await node.stop()

    app = FastAPI(title="LanShare", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.node = node
    app.state.request_shutdown = None
    app.add_middleware(AccessControlMiddleware, auth=node.auth)

    # ----- LAN-facing, read-only --------------------------------------------------------

    @app.get("/api/files")
    def list_files(request: Request) -> Response:
        body, etag = node.files_payload()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    # ----- local UI API -----------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        return node.status()

    @app.get("/api/common")
    def common_space(request: Request) -> Response:
        body, etag = node.common_payload()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/api/peers")
    def peers() -> dict:
        return node.peers_payload()

    @app.post("/api/shutdown")
    async def shutdown(request: Request) -> dict:
        stop = request.app.state.request_shutdown
        if stop is None:
            return {"ok": False, "detail": "shutdown is not available in this mode"}
        asyncio.get_running_loop().call_later(0.2, stop)
        return {"ok": True}

    return app
