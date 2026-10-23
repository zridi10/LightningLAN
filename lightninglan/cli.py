"""Entry point for `python -m lightninglan` and for the packaged desktop app.

With no arguments it runs zero-config: default folders, the computer's name, port 8765 (or the
next free one), a tray icon, and the web UI opened in the browser (in the packaged Windows app,
in a window of its own). Launching it again while it runs just shows the web UI of the running copy.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from . import __version__
from .config import ConfigError, Settings, default_config_path, load_settings
from .desktop import open_url, restore_library_path, show_message
from .instance import InstanceLock, running_instance_url
from .logs import add_file_logging, log_dir, log_uncaught_exceptions, setup_console_logging
from .node import Node
from .resources import is_frozen
from .server import create_app

log = logging.getLogger("lightninglan")

PORT_SEARCH_RANGE = 20  # without --port, try this many ports from the remembered one
FILE_ONLY = {"file_only": True}


class Fatal(Exception):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lightninglan",
        description="LightningLAN: share a folder with everyone on your local network.",
    )
    p.add_argument("--share", metavar="FOLDER", help="folder to share (remembered for next time)")
    p.add_argument("--downloads", metavar="FOLDER", help="where downloads are saved (remembered)")
    p.add_argument("--port", type=int, help="HTTP port (default 8765 or the next free one; remembered)")
    p.add_argument("--name", help="display name shown to other peers (default: computer name, remembered)")
    p.add_argument("--passphrase", help="shared secret; only peers with the same passphrase are seen")
    p.add_argument("--config", metavar="FILE", type=Path, help="config file location")
    p.add_argument(
        "--interface",
        action="append",
        metavar="IP",
        help="advertise only on this local IPv4 address (repeatable; default: auto-detect LAN)",
    )
    p.add_argument(
        "--refresh-interval",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="how often peers are polled for changes (default 3)",
    )
    p.add_argument(
        "--max-upload-rate",
        type=float,
        metavar="MB_PER_S",
        help="limit your total upload to other peers, in megabytes per second",
    )
    p.add_argument("--no-browser", action="store_true", help="don't open the web UI on start")
    p.add_argument("--no-tray", action="store_true", help="no tray icon and no pop-up messages (servers, scripts)")
    p.add_argument("--verbose", "-v", action="store_true", help="log requests and debug output")
    # For build.py's smoke test: can the app window be made here? Exit code 0 yes, 3 no WebView2, 4 broken.
    p.add_argument("--check-window", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"LightningLAN {__version__}")
    return p


def bind_socket(port: int) -> socket.socket:
    """Bind the HTTP port on all interfaces, refusing to share it with another process."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
        # Listen right away: on Linux two SO_REUSEADDR sockets can both bind a port that nobody
        # listens on yet, and the loser would only find out later, when the server starts.
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def bind_first_free(preferred: int, explicit: bool) -> socket.socket:
    """Bind `preferred`; unless that port was asked for explicitly, fall back to the next free ones."""
    last_error: OSError | None = None
    candidates = [preferred] if explicit else range(preferred, min(preferred + PORT_SEARCH_RANGE, 65536))
    for port in candidates:
        try:
            return bind_socket(port)
        except OSError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings(
        args.config,
        share=args.share,
        downloads=args.downloads,
        port=args.port,
        name=args.name,
        passphrase=args.passphrase,
    )
    settings.open_browser = not args.no_browser
    settings.interfaces = args.interface
    if not 0.2 <= args.refresh_interval <= 60:
        raise ConfigError("--refresh-interval must be between 0.2 and 60 seconds")
    settings.refresh_interval = args.refresh_interval
    if args.max_upload_rate is not None:
        if args.max_upload_rate <= 0:
            raise ConfigError("--max-upload-rate must be positive")
        settings.max_upload_rate = args.max_upload_rate * 1024 * 1024
    return settings


def print_banner(settings: Settings, addresses: list[str]) -> None:
    url = f"http://localhost:{settings.port}"
    lan = ", ".join(f"{ip}:{settings.port}" for ip in addresses) or "discovery off"
    lines = [
        "",
        f"  LightningLAN {__version__} is running as \"{settings.name}\"",
        f"  Web UI:     {url}",
        f"  LAN:        {lan}",
        f"  Sharing:    {settings.share_dir}",
        f"  Downloads:  {settings.downloads_dir}",
        f"  Passphrase: {'on' if settings.passphrase else 'off'}",
        "",
        "  Press Ctrl+C to stop.",
        "",
    ]
    print("\n".join(lines), flush=True)
    log.info(
        "LightningLAN %s running as %r: web UI %s, LAN %s, sharing %s, downloads %s, passphrase %s",
        __version__, settings.name, url, lan, settings.share_dir, settings.downloads_dir,
        "on" if settings.passphrase else "off", extra=FILE_ONLY,
    )


def _after_start(server: uvicorn.Server, node: Node, url: str, open_browser: bool) -> None:
    settings = node.settings
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.05)
    print_banner(settings, node.discovery.addresses if node.discovery else [])
    if open_browser:
        if open_url(url):
            log.info("opened %s in the default browser", url, extra=FILE_ONLY)
        else:
            log.warning("could not open a browser; open %s yourself", url)


# ----- running -------------------------------------------------------------------------------

def _gui_mode(args: argparse.Namespace) -> bool:
    """A packaged app started from the desktop: report problems in pop-ups, not on a console."""
    stderr = sys.stderr
    has_terminal = stderr is not None and hasattr(stderr, "isatty") and stderr.isatty()
    return is_frozen() and not args.no_tray and not has_terminal


def _prepare_streams() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:  # a windowed app on Windows has no console at all
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, "reconfigure"):
            # Redirected output on Windows uses the ANSI code page; never crash on a peer's name.
            stream.reconfigure(errors="backslashreplace")


def _report_error(message: str, gui: bool) -> None:
    print(f"error: {message}", file=sys.stderr, flush=True)
    log.error("%s", message, extra=FILE_ONLY)
    if gui:
        show_message("LightningLAN", message)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gui = _gui_mode(args)
    setup_console_logging(args.verbose)
    _prepare_streams()
    if args.check_window:
        from .window import NO_WEBVIEW2, window_problem

        problem = window_problem()
        print(f"app window: {problem or 'available'}", flush=True)
        return 0 if problem is None else 3 if problem == NO_WEBVIEW2 else 4
    restore_library_path()
    log_uncaught_exceptions()
    try:
        return _run(args, gui)
    except Fatal as exc:
        _report_error(str(exc), gui)
        return exc.exit_code


def _run(args: argparse.Namespace, gui: bool) -> int:
    config_path = args.config.expanduser().absolute() if args.config else default_config_path()
    state_dir = config_path.parent
    lock = InstanceLock(state_dir)
    try:
        acquired = lock.acquire()
    except OSError as exc:
        raise Fatal(f"cannot use the settings folder {state_dir}: {exc.strerror or exc}", 2) from exc
    if not acquired:
        return _hand_over(state_dir, args)
    try:
        add_file_logging(state_dir)
        try:
            settings = settings_from_args(args)
        except ConfigError as exc:
            raise Fatal(str(exc), 2) from exc
        try:
            sock = bind_first_free(settings.port, explicit=args.port is not None)
        except OSError as exc:
            raise Fatal(
                f"cannot listen on port {settings.port} ({exc.strerror or exc}).\n"
                f"Is LightningLAN already running? Use --port to pick another port.",
                1,
            ) from exc
        settings.port = sock.getsockname()[1]
        lock.publish(settings.port, settings.peer_id)
        _serve(settings, sock, args, gui)
        log.info("LightningLAN stopped", extra=FILE_ONLY)
        return 0
    finally:
        lock.release()


def _hand_over(state_dir: Path, args: argparse.Namespace) -> int:
    """Another copy is running with this config: show its web UI instead of starting a second one."""
    url = running_instance_url(state_dir)
    if url is None:
        raise Fatal(
            "LightningLAN is already running but is not responding. Wait a moment and try again, "
            "or quit it from its tray icon.",
            1,
        )
    print(f"LightningLAN is already running: {url}", flush=True)
    if not args.no_browser and not _show_running_window(url):
        open_url(url)
    return 0


def _show_running_window(url: str) -> bool:
    """Ask the running copy to show its app window. False if it has none (it uses the browser)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: it may come to the front
    request = urllib.request.Request(
        f"{url}/api/show-window", data=b"", method="POST", headers={"X-LightningLAN-Client": "launcher"},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as resp:
            return json.loads(resp.read()).get("shown") is True
    except (OSError, ValueError):  # an older copy without this, or it is shutting down
        return False


def _serve(settings: Settings, sock: socket.socket, args: argparse.Namespace, gui: bool) -> None:
    node = Node(settings)
    app = create_app(node)
    config = uvicorn.Config(
        app,
        log_config=None,  # use our logging setup (console and file)
        log_level="debug" if args.verbose else "warning",
        access_log=args.verbose,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        server.should_exit = True

    app.state.request_shutdown = request_shutdown
    app.state.exiting = lambda: server.should_exit  # lets the web UI's open session end promptly
    url = f"http://localhost:{settings.port}"
    window = _make_window(settings, url, gui)
    app.state.show_window = window.show if window is not None else None

    def open_ui() -> None:
        if window is None or not window.show():
            open_url(url)

    tray = None if args.no_tray else _make_tray(node, open_ui, request_shutdown)
    open_browser = settings.open_browser and window is None
    threading.Thread(target=_after_start, args=(server, node, url, open_browser), daemon=True).start()
    if window is not None:
        _serve_with_window(server, sock, tray, window, url)
    elif tray is None:
        server.run(sockets=[sock])
    elif sys.platform == "darwin":
        _serve_with_tray_on_main_thread(server, sock, tray)
    else:
        tray.start()
        try:
            server.run(sockets=[sock])
        finally:
            tray.stop()


def _make_window(settings: Settings, url: str, gui: bool):
    """The app window, for the packaged Windows app started from the desktop; None means the browser."""
    if not (gui and settings.open_browser):
        return None
    from .window import AppWindow, window_supported

    if not window_supported():
        return None
    return AppWindow(url, settings.state_dir / "window")


def _make_tray(node: Node, open_ui, request_shutdown):
    from .tray import Tray, tray_supported

    if not tray_supported():
        log.info("no desktop session: running without a tray icon")
        return None
    try:
        return Tray(node, open_ui, log_dir(node.settings.state_dir), request_shutdown)
    except Exception:
        log.warning("the tray icon is not available; use the web UI instead", exc_info=True)
        return None


def _serve_with_window(server: uvicorn.Server, sock: socket.socket, tray, window, url: str) -> None:
    """Windows app window: it must own the main thread, so the web server runs on another one."""
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="lightninglan-server")
    server_thread.start()

    def close_window_when_server_ends() -> None:
        server_thread.join()
        window.destroy()

    threading.Thread(target=close_window_when_server_ends, name="lightninglan-window-stop", daemon=True).start()
    if tray is not None:
        tray.start()
    try:
        while not server.started and server_thread.is_alive():
            time.sleep(0.05)
        if server_thread.is_alive():
            log.info("opening the app window")
            try:
                window.run()  # until LightningLAN quits
            except Exception:
                log.warning("the app window is not available; using the browser instead", exc_info=True)
                open_url(url)
        server_thread.join()
    finally:
        if tray is not None:
            tray.stop()


def _serve_with_tray_on_main_thread(server: uvicorn.Server, sock: socket.socket, tray) -> None:
    """macOS: the menu bar icon must own the main thread, so the web server runs on another one."""
    signals = {signal.SIGINT, signal.SIGTERM}
    signal.pthread_sigmask(signal.SIG_BLOCK, signals)  # threads started below inherit this mask
    tray_done = threading.Event()

    def stop_on_signal() -> None:
        signal.sigwait(signals)
        server.should_exit = True

    def stop_tray_when_server_ends() -> None:
        server_thread.join()
        while not tray_done.is_set():
            tray.stop()
            tray_done.wait(1.0)

    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="lightninglan-server")
    threading.Thread(target=stop_on_signal, name="lightninglan-signals", daemon=True).start()
    server_thread.start()
    threading.Thread(target=stop_tray_when_server_ends, name="lightninglan-tray-stop", daemon=True).start()
    try:
        tray.run()
    except Exception:
        log.warning("the menu bar icon is not available; use the web UI instead", exc_info=True)
    finally:
        tray_done.set()
    server_thread.join()
