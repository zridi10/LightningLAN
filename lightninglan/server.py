"""HTTP API and web UI for one peer."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from .desktop import FolderPickerError, open_in_file_manager, pick_folder
from .logs import log_dir
from .node import Node, SettingsError
from .resources import static_dir
from .security import AccessControlMiddleware, SecurityHeadersMiddleware
from .serving import serve_download
from .transfers import DownloadError
from .uploads import receive_upload

log = logging.getLogger(__name__)

STATIC_DIR = static_dir()


class StartDownload(BaseModel):
    file_id: str
    peer_id: str | None = None


class OpenTarget(BaseModel):
    target: Literal["share", "downloads", "download", "logs"]
    download_id: str | None = None


class SettingsUpdate(BaseModel):
    """Fields left out (or null) stay as they are. An empty passphrase turns it off."""

    name: str | None = None
    share_dir: str | None = None
    downloads_dir: str | None = None
    passphrase: str | None = None


class PickFolder(BaseModel):
    purpose: Literal["share", "downloads"]
    initial: str | None = None


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

    app = FastAPI(title="LightningLAN", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.node = node
    app.state.request_shutdown = None
    app.add_middleware(AccessControlMiddleware, auth=node.auth)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: applies to rejections too

    # ----- LAN-facing, read-only --------------------------------------------------------

    @app.get("/api/files")
    def list_files(request: Request) -> Response:
        body, etag = node.files_payload()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @app.api_route("/api/download/{file_id}", methods=["GET", "HEAD"])
    def download_file(file_id: str, request: Request) -> Response:
        return serve_download(node.indexer, file_id, request, node.settings.max_upload_rate)

    # ----- local web UI -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        log.info("web UI opened in a browser")
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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

    @app.post("/api/upload", status_code=201)
    async def upload(request: Request, name: str, mtime: float | None = None):
        try:
            result = await receive_upload(request, node.settings.share_dir, name, mtime)
        except ClientDisconnect:
            return Response(status_code=400)  # nobody is listening any more
        node.indexer.request_rescan()
        return result

    @app.get("/api/downloads")
    def list_downloads() -> dict:
        return {"downloads": node.downloads.list()}

    @app.post("/api/downloads")
    def start_download(body: StartDownload) -> dict:
        try:
            return node.downloads.start(body.file_id, body.peer_id).to_dict()
        except DownloadError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/downloads/clear")
    def clear_downloads() -> dict:
        node.downloads.clear_finished()
        return {"downloads": node.downloads.list()}

    @app.post("/api/downloads/{download_id}/{action}")
    def control_download(download_id: str, action: str) -> dict:
        actions = {"pause": node.downloads.pause, "resume": node.downloads.resume, "cancel": node.downloads.cancel}
        if action not in actions:
            raise HTTPException(404, "unknown action")
        try:
            return actions[action](download_id).to_dict()
        except DownloadError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.delete("/api/downloads/{download_id}")
    def remove_download(download_id: str) -> dict:
        try:
            node.downloads.remove(download_id)
        except DownloadError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings() -> dict:
        return node.settings_payload()

    @app.post("/api/settings")
    async def update_settings(body: SettingsUpdate) -> dict:
        try:
            changed = await node.update_settings(**body.model_dump())
        except SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"changed": changed, "settings": node.settings_payload()}

    @app.post("/api/pick-folder")
    def choose_folder(body: PickFolder) -> dict:
        # Blocks (in a worker thread) until the person picks a folder or cancels.
        title = "Choose the folder to share" if body.purpose == "share" else "Choose where downloads are saved"
        initial = Path(body.initial) if body.initial and Path(body.initial).is_absolute() else None
        try:
            chosen = pick_folder(title, initial)
        except FolderPickerError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"path": str(chosen) if chosen else None}

    @app.post("/api/open")
    def open_folder(body: OpenTarget) -> dict:
        if body.target == "share":
            path, reveal = node.settings.share_dir, False
        elif body.target == "downloads":
            path, reveal = node.settings.downloads_dir, False
        elif body.target == "logs":
            path, reveal = log_dir(node.settings.state_dir), False
            path.mkdir(parents=True, exist_ok=True)
        else:
            try:
                d = node.downloads.get(body.download_id or "")
            except DownloadError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
            if d.status != "completed" or not d.path or not Path(d.path).exists():
                raise HTTPException(404, "The downloaded file is no longer there.")
            path, reveal = Path(d.path), True
        try:
            open_in_file_manager(path, reveal)
        except OSError as exc:
            raise HTTPException(500, f"Could not open the folder: {exc}") from exc
        return {"ok": True}

    @app.post("/api/shutdown")
    async def shutdown(request: Request) -> dict:
        stop = request.app.state.request_shutdown
        if stop is None:
            return {"ok": False, "detail": "shutdown is not available in this mode"}
        asyncio.get_running_loop().call_later(0.2, stop)
        return {"ok": True}

    return app
