"""The system tray icon (Windows, Linux) or menu bar icon (macOS), built with pystray."""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .desktop import open_in_file_manager
from .icon import draw_icon

if TYPE_CHECKING:
    from .node import Node

log = logging.getLogger(__name__)

TITLE_REFRESH_SECONDS = 5.0


def tray_supported() -> bool:
    """False where there is no desktop session to show an icon in (e.g. a Linux server)."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


class Tray:
    def __init__(self, node: Node, open_ui: Callable[[], None], log_dir: Path, quit_app: Callable[[], None]):
        import pystray  # imported here because it picks and loads a GUI backend on import

        self.node = node
        self._open_ui = open_ui  # shows the app window, or opens the web UI in the browser
        self.log_dir = log_dir
        self._quit_app = quit_app
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        item, separator = pystray.MenuItem, pystray.Menu.SEPARATOR
        menu = pystray.Menu(
            item("Open LightningLAN", self._action(self.open_ui), default=True),
            separator,
            item("Open Shared Folder", self._action(lambda: self._open_folder(self.node.settings.share_dir))),
            item("Open Downloads Folder", self._action(lambda: self._open_folder(self.node.settings.downloads_dir))),
            item("Open Log Folder", self._action(lambda: self._open_folder(self.log_dir))),
            separator,
            item("Quit LightningLAN", self._action(self.quit)),
        )
        self.icon = pystray.Icon("LightningLAN", draw_icon(64), self.title(), menu)

    # ----- menu actions -----------------------------------------------------------------

    @staticmethod
    def _action(fn: Callable[[], object]) -> Callable[[object, object], None]:
        def run(icon, item) -> None:
            try:
                fn()
            except Exception:
                log.exception("tray menu action failed")

        return run

    def open_ui(self) -> None:
        self._open_ui()

    def _open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(path)

    def quit(self) -> None:
        log.info("quit chosen from the tray menu")
        self._quit_app()
        self.stop()

    def title(self) -> str:
        if not self.node.online:
            return f"LightningLAN: {self.node.settings.name[:60]} (stopped)"
        n = len(self.node.peers.online())
        peers = "no peers online" if n == 0 else f"{n} peer{'s' if n != 1 else ''} online"
        return f"LightningLAN: {self.node.settings.name[:60]} ({peers})"

    # ----- running ----------------------------------------------------------------------

    def run(self) -> None:
        """Show the icon and run its event loop until stop(). On macOS this must be the main thread."""
        self.icon.run(setup=self._ready)

    def start(self) -> None:
        """Run the icon on a background thread (Windows and Linux)."""
        self._thread = threading.Thread(target=self._run_quietly, name="lightninglan-tray", daemon=True)
        self._thread.start()

    def _run_quietly(self) -> None:
        try:
            self.run()
        except Exception:
            log.warning("the tray icon is not available; use the web UI instead", exc_info=True)

    def stop(self) -> None:
        self._stopped.set()
        try:
            self.icon.stop()
        except Exception:
            log.debug("stopping the tray icon failed", exc_info=True)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _ready(self, icon) -> None:
        icon.visible = True
        log.info("tray icon ready")
        threading.Thread(target=self._refresh_title, name="lightninglan-tray-title", daemon=True).start()

    def _refresh_title(self) -> None:
        while not self._stopped.wait(TITLE_REFRESH_SECONDS):
            try:
                title = self.title()
                if self.icon.title != title:
                    self.icon.title = title
            except Exception:
                log.debug("updating the tray tooltip failed", exc_info=True)
