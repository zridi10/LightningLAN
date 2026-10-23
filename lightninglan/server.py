"""HTTP API and web UI for one peer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import anyio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from . import __version__
from .desktop import FolderPickerError, open_in_file_manager, pick_folder
from .logs import log_dir
from .names import random_name
from .node import STOPPED, Node, SettingsError
from .phone import COOKIE as PHONE_COOKIE, COOKIE_MAX_AGE, hold_session, phone_links, relay_download
from .play import PlayError
from .resources import static_dir
from .security import AccessControlMiddleware, SecurityHeadersMiddleware
from .serving import serve_download, serve_piece
from .transfers import DownloadError
from .uploads import receive_upload

log = logging.getLogger(__name__)

STATIC_DIR = static_dir()
SESSION_TICK = 0.5  # how often the web UI session stream checks for news
SESSION_PING = 15.0  # seconds between keep-alive comments on it
STOPPED_DETAIL = "LightningLAN is stopped. Press Start LightningLAN first."


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


class PlayInvite(BaseModel):
    peer_id: str
    kind: str
    best_of: int = 3
    first: bool | None = None  # this side moves first; by default the other one from the last game, or random


class PlayMove(BaseModel):
    """One move; which field depends on the game (see play.py)."""

    cell: int | None = None  # tic-tac-toe, ultimate, four in a row (column), dots and boxes (line), memory (card)
    path: list[int] | None = None  # checkers
    choice: str | None = None  # rock-paper-scissors
    shot: int | None = None  # battleship
    shuffle: bool = False  # battleship: place the ships somewhere else
    ready: bool = False  # battleship: lock the ships in
    time_ms: int | None = None  # typing race, math sprint


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
        # Streamed files and pieces are read in the worker thread pool; swarms keep many in flight.
        anyio.to_thread.current_default_thread_limiter().total_tokens = 100
        await node.start()
        try:
            yield
        finally:
            await node.stop()

    app = FastAPI(title="LightningLAN", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.node = node
    app.state.request_shutdown = None
    app.state.exiting = lambda: False  # the server sets this up to report that it is shutting down
    app.state.show_window = None  # shows the app window, when the web UI runs in one (Windows app)
    app.add_middleware(AccessControlMiddleware, auth=node.auth, online=lambda: node.online,
                       phone_key_ok=node.phone_key_matches)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: applies to rejections too

    def session_stream(request: Request, opened, closed, valid=lambda: True) -> StreamingResponse:
        """Server-sent events that say whether this peer is online (and how many phones are open),
        held open by a page for as long as it is showing. opened/closed count it in and out; it
        ends when valid() turns false."""
        exiting = request.app.state.exiting

        async def events():
            opened()
            try:
                seen, quiet = None, 0.0
                while not exiting() and valid():
                    now = (node.state_version, node.phones)
                    if now != seen:
                        seen, quiet = now, 0.0
                        yield f"data: {json.dumps({'online': node.online})}\n\n"
                    elif quiet >= SESSION_PING:
                        quiet = 0.0
                        yield ": ping\n\n"
                    await asyncio.sleep(SESSION_TICK)
                    quiet += SESSION_TICK
            finally:
                closed()

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    def common_response(request: Request) -> Response:
        body, etag = node.common_payload()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

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
        return serve_download(node.indexer, file_id, request, node.uploads)

    @app.get("/api/games")
    def published_games(request: Request) -> Response:
        """This peer's half of its head-to-head games, read by its opponents (see play.py)."""
        body, etag = node.arcade.published()
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    @app.get("/api/pieces/{file_id}")
    def piece_list(file_id: str) -> Response:
        info = node.piece_info(file_id)
        if info is None:
            return JSONResponse({"error": "not_found", "detail": "No piece list for that file."}, status_code=404)
        return JSONResponse(info.to_json(), headers={"Cache-Control": "no-store"})

    @app.get("/api/swarm/{file_id}")
    def swarm_status(file_id: str) -> Response:
        status = node.downloads.swarm_status(file_id)
        if status is None:
            return JSONResponse({"error": "not_found", "detail": "Not downloading that file."}, status_code=404)
        return JSONResponse(status, headers={"Cache-Control": "no-store"})

    @app.get("/api/piece/{file_id}/{index}")
    def swarm_piece(file_id: str, index: int) -> Response:
        return serve_piece(node.downloads, file_id, index, node.uploads)

    # ----- local web UI -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        log.info("web UI opened")
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ----- local UI API -----------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        return node.status()

    @app.post("/api/stop")
    async def stop_sharing() -> dict:
        await node.go_offline(STOPPED)
        return node.status()

    @app.post("/api/start")
    async def start_sharing() -> dict:
        await node.go_online()
        return node.status()

    @app.get("/api/ui-session")
    async def ui_session(request: Request) -> StreamingResponse:
        """Held open by the web UI (one tab at a time). When no tab (or phone) has it open for a
        while, this peer goes offline, and opening the UI again brings it back. It also reports
        Stop and Start, and phones connecting."""
        return session_stream(request, node.ui_opened, node.ui_closed)

    @app.get("/api/common")
    def common_space(request: Request) -> Response:
        return common_response(request)

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

    def require_online() -> None:
        if not node.online:
            raise HTTPException(409, STOPPED_DETAIL)

    @app.post("/api/downloads")
    def start_download(body: StartDownload) -> dict:
        require_online()
        try:
            return node.downloads.start(body.file_id, body.peer_id).to_dict()
        except DownloadError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/downloads/clear")
    def clear_downloads() -> dict:
        node.downloads.clear_finished()
        return {"downloads": node.downloads.list()}

    @app.post("/api/downloads/pause-all")
    def pause_all_downloads() -> dict:
        node.downloads.pause_all()
        return {"downloads": node.downloads.list()}

    @app.post("/api/downloads/resume-all")
    def resume_all_downloads() -> dict:
        require_online()
        node.downloads.resume_all()
        return {"downloads": node.downloads.list()}

    @app.post("/api/downloads/{download_id}/{action}")
    def control_download(download_id: str, action: str) -> dict:
        actions = {"pause": node.downloads.pause, "resume": node.downloads.resume, "cancel": node.downloads.cancel}
        if action not in actions:
            raise HTTPException(404, "unknown action")
        if action == "resume":
            require_online()
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

    @app.get("/api/random-name")
    def suggest_name() -> dict:
        """A funny name for the dice button: not ours, and not one an online peer uses."""
        return {"name": random_name([node.name] + [p.name for p in node.peers.known()])}

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

    @app.post("/api/show-window")
    def show_window(request: Request) -> dict:
        """LightningLAN was started again while running: show the app window, if there is one."""
        show = request.app.state.show_window
        return {"shown": bool(show is not None and show())}

    @app.post("/api/shutdown")
    async def shutdown(request: Request) -> dict:
        stop = request.app.state.request_shutdown
        if stop is None:
            return {"ok": False, "detail": "shutdown is not available in this mode"}
        asyncio.get_running_loop().call_later(0.2, stop)
        return {"ok": True}

    async def phone_setup(new_key: bool = False) -> dict:
        try:
            key = await node.ensure_phone_key(new=new_key)
        except SettingsError as exc:
            raise HTTPException(500, str(exc)) from exc
        links = await asyncio.to_thread(phone_links, await node.phone_addresses(), node.settings.port, key)
        return {"links": links, "phones": node.phones}

    @app.get("/api/phone")
    async def phone_link() -> dict:
        """The phone link and its QR code, for the web UI's phone dialog."""
        return await phone_setup()

    @app.post("/api/phone/new-key")
    async def new_phone_key() -> dict:
        """Disconnect every phone: the old link and QR code stop working."""
        return await phone_setup(new_key=True)

    # ----- While you wait: head-to-head games (the web UI and the phone page play as this peer) ----

    def play_move(arcade, game_id: str, body: PlayMove) -> dict:
        if body.choice is not None:
            return arcade.choose(game_id, body.choice)
        if body.path is not None:
            return arcade.move(game_id, body.path)
        if body.shuffle:
            return arcade.shuffle_fleet(game_id)
        if body.ready:
            return arcade.fleet_ready(game_id)
        if body.shot is not None:
            return arcade.fire(game_id, body.shot)
        if body.time_ms is not None:
            return arcade.finish(game_id, body.time_ms)
        return arcade.move(game_id, body.cell if body.cell is not None else -1)

    def play_view(request: Request) -> Response:
        body = json.dumps(node.arcade.view(), separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    def play_invite(body: PlayInvite) -> dict:
        require_online()
        try:
            return node.arcade.invite(body.peer_id, body.kind, body.best_of, body.first)
        except PlayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    def play_action(game_id: str, action: str, body: PlayMove | None = None) -> dict:
        arcade = node.arcade
        actions = {
            "accept": lambda: arcade.answer(game_id, True),
            "decline": lambda: arcade.answer(game_id, False),
            "withdraw": lambda: arcade.withdraw(game_id),
            "resign": lambda: arcade.resign(game_id),
            "close": lambda: arcade.close(game_id),
            "rematch": lambda: arcade.rematch(game_id),
            "move": lambda: play_move(arcade, game_id, body or PlayMove()),
        }
        if action not in actions:
            raise HTTPException(404, "unknown action")
        if action in ("accept", "rematch", "move"):
            require_online()
        try:
            return {"game": actions[action]()}
        except PlayError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    for prefix in ("/api/play", "/phone/api/play"):
        app.add_api_route(prefix, play_view, methods=["GET"])
        app.add_api_route(prefix + "/invite", play_invite, methods=["POST"])
        app.add_api_route(prefix + "/{game_id}/{action}", play_action, methods=["POST"])

    # ----- the phone page (on the LAN, for phones paired with the QR code) ------------------

    @app.get("/phone", include_in_schema=False)
    def phone_page(request: Request, key: str | None = None) -> Response:
        if key is not None:
            # Opened from the QR code: keep the key in a cookie, and take it out of the address bar.
            if not node.phone_key_matches(key):
                return RedirectResponse("/phone#expired", status_code=303)
            response = RedirectResponse("/phone", status_code=303)
            response.set_cookie(PHONE_COOKIE, key, max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="lax")
            log.info("a phone was paired from %s", request.client.host if request.client else "?")
            return response
        return FileResponse(STATIC_DIR / "phone.html", media_type="text/html")

    @app.get("/phone/api/status")
    def phone_status() -> dict:
        return {
            "name": node.name,
            "version": __version__,
            "online": node.online,
            "offline_reason": node.offline_reason,
            "peers_online": len(node.peers.online()),
        }

    @app.get("/phone/api/session")
    async def phone_session(request: Request) -> StreamingResponse:
        key = request.cookies.get(PHONE_COOKIE)  # "Disconnect phones" replaces the key: then this ends
        return session_stream(request, node.phone_opened, node.phone_closed, lambda: node.phone_key_matches(key))

    @app.get("/phone/api/files")
    def phone_files(request: Request) -> Response:
        return common_response(request)

    @app.api_route("/phone/api/file/{file_id}", methods=["GET", "HEAD"])
    async def phone_download(file_id: str, request: Request) -> Response:
        if not node.online:
            raise HTTPException(409, f"LightningLAN is stopped on {node.name}. Start it there first.")
        if node.indexer.lookup(file_id) is not None:
            response = await run_in_threadpool(serve_download, node.indexer, file_id, request, node.uploads)
        else:
            response = await relay_download(node, file_id, request)
        return hold_session(node, response)

    @app.post("/phone/api/upload", status_code=201)
    async def phone_upload(request: Request, name: str, mtime: float | None = None):
        try:
            result = await receive_upload(request, node.settings.share_dir, name, mtime)
        except ClientDisconnect:
            return Response(status_code=400)
        node.indexer.request_rescan()
        log.info("a phone added %s to the shared folder", result["name"])
        return result

    return app
