"""The app window (Windows): the web UI in a window of its own instead of a browser tab.

It uses the Edge WebView2 runtime through pywebview. Closing the window works like closing the
browser tab did: the window hides, LightningLAN keeps running in the tray, and with no page open
this peer goes offline after a few seconds. Opening LightningLAN again (from the tray, or by
starting it again) shows the window and brings it back online.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

BLANK_PAGE = '<!doctype html><title>LightningLAN</title><body style="background:#0e1015"></body>'
SIZE = (1200, 820)
MIN_SIZE = (720, 520)


NO_WEBVIEW2 = "the WebView2 runtime is not installed"
_WEBVIEW2_RUNTIME = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _webview2_installed() -> bool:
    """WebView2 is part of Windows 11, and comes with Edge on Windows 10. Machine-wide or per user."""
    import winreg

    places = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_RUNTIME}"),
    ]
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if str(version).strip("0.") != "":
            return True
    return False


def window_problem() -> str | None:
    """Why there can't be an app window here, or None if there can.

    Without WebView2, pywebview would fall back to Internet Explorer, which can't run the page, so
    that is checked first.
    """
    if sys.platform != "win32":
        return "the app window is only made on Windows"
    if not _webview2_installed():
        return NO_WEBVIEW2
    try:
        from webview.platforms import winforms
    except Exception as exc:
        return f"pywebview could not be loaded: {exc!r}"
    if not winforms.is_chromium:
        return NO_WEBVIEW2
    return None


def window_supported() -> bool:
    problem = window_problem()
    if problem is not None:
        log.info("no app window (%s): using the browser", problem)
    return problem is None


class AppWindow:
    def __init__(self, url: str, storage_dir: Path):
        self.url = url
        self.storage_dir = storage_dir
        self._window = None
        self._quitting = False
        self._minimized = False
        self._page_open = True  # the web UI is loaded (not the blank page of a hidden window)
        self._lock = threading.Lock()

    def run(self) -> None:
        """Show the window and run its event loop until destroy(). Must be called on the main thread."""
        import webview

        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window(
            "LightningLAN", self.url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE, background_color="#0e1015",
        )
        window.events.closing += self._on_closing
        window.events.minimized += lambda: self._set_minimized(True)
        window.events.restored += lambda: self._set_minimized(False)
        window.events.maximized += lambda: self._set_minimized(False)
        self._window = window
        if self._quitting:  # LightningLAN quit while the window was being made
            return
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Not private: the page keeps its settings (the theme) in localStorage between runs.
            webview.start(gui="edgechromium", private_mode=False, storage_path=str(self.storage_dir))
        finally:
            self._window = None

    def show(self) -> bool:
        """Show the window (with the page loaded) and bring it to the front. False if there is none.

        It happens on a thread of its own: the caller may be the tray icon's thread, and bringing the
        window to the front sends that thread's windows messages, which it must be free to answer
        (waiting for the window there would freeze both).
        """
        if self._window is None:
            return False
        threading.Thread(target=self._show, name="lightninglan-window-show", daemon=True).start()
        return True

    def _show(self) -> None:
        window = self._window
        if window is None:
            return
        with self._lock:
            try:
                if not self._page_open:
                    window.load_url(self.url)
                    self._page_open = True
                window.show()  # shows and activates it, on the window's own thread
                if self._minimized:
                    window.restore()
            except Exception:
                log.warning("could not show the app window", exc_info=True)
                return
        log.info("app window shown")

    def destroy(self) -> None:
        """Close the window for good (LightningLAN is quitting); run() then returns."""
        self._quitting = True
        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception:
                log.debug("closing the app window failed", exc_info=True)

    def _set_minimized(self, value: bool) -> None:
        self._minimized = value

    def _on_closing(self) -> bool:
        if self._quitting:
            return True
        # Hide instead, and leave the page: like closing the browser tab, that ends the web UI's
        # session, so this peer goes offline unless the window is opened again.
        threading.Thread(target=self._hide, name="lightninglan-window-hide", daemon=True).start()
        log.info("app window closed (LightningLAN keeps running in the tray)")
        return False

    def _hide(self) -> None:
        window = self._window
        if window is None:
            return
        with self._lock:
            try:
                window.hide()
                window.load_html(BLANK_PAGE)
                self._page_open = False
            except Exception:
                log.debug("hiding the app window failed", exc_info=True)
