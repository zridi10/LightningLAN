"""Command-line entry point: python -m lanshare --share <folder> ..."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .config import ConfigError, Settings, load_settings
from .node import Node
from .server import create_app

log = logging.getLogger("lanshare")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m lanshare",
        description="LanShare: share a folder with everyone on your local network.",
    )
    p.add_argument("--share", metavar="FOLDER", help="folder to share (remembered for next time)")
    p.add_argument("--downloads", metavar="FOLDER", help="where downloads are saved (remembered)")
    p.add_argument("--port", type=int, help="HTTP port (default 8765, remembered)")
    p.add_argument("--name", help="display name shown to other peers (default: hostname)")
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
        help="limit how fast each peer can download from you, in megabytes per second",
    )
    p.add_argument("--no-browser", action="store_true", help="don't open the web UI on start")
    p.add_argument("--verbose", "-v", action="store_true", help="log requests and debug output")
    p.add_argument("--version", action="version", version=f"LanShare {__version__}")
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
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


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
    lines = [
        "",
        f"  LanShare {__version__} is running as \"{settings.name}\"",
        f"  Web UI:     {url}",
        f"  LAN:        {', '.join(f'{ip}:{settings.port}' for ip in addresses) or 'discovery off'}",
        f"  Sharing:    {settings.share_dir}",
        f"  Downloads:  {settings.downloads_dir}",
        f"  Passphrase: {'on' if settings.passphrase else 'off'}",
        "",
        "  Press Ctrl+C to stop.",
        "",
    ]
    print("\n".join(lines), flush=True)


def _after_start(server: uvicorn.Server, node: Node) -> None:
    settings = node.settings
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.05)
    print_banner(settings, node.discovery.addresses if node.discovery else [])
    if settings.open_browser:
        webbrowser.open(f"http://localhost:{settings.port}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        for noisy in ("httpx", "httpcore", "zeroconf"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = settings_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        sock = bind_socket(settings.port)
    except OSError as exc:
        print(
            f"error: cannot listen on port {settings.port} ({exc.strerror or exc}).\n"
            f"Is LanShare already running? Use --port to pick another port.",
            file=sys.stderr,
        )
        return 1

    node = Node(settings)
    app = create_app(node)
    config = uvicorn.Config(
        app,
        log_level="debug" if args.verbose else "warning",
        access_log=args.verbose,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    threading.Thread(target=_after_start, args=(server, node), daemon=True).start()
    server.run(sockets=[sock])
    return 0
