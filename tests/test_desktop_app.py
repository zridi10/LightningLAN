"""Pieces of the packaged desktop app: bundled resources, the icon and the tray menu."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lightninglan import resources
from lightninglan.icon import draw_icon, write_icon_files


def test_static_files_are_found_from_source():
    assert (resources.static_dir() / "index.html").is_file()
    assert (resources.static_dir() / "app.js").is_file()


def test_static_files_are_found_in_a_pyinstaller_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.static_dir() == tmp_path / "lightninglan" / "static"


def test_icon_drawing():
    image = draw_icon(32)
    assert image.size == (32, 32) and image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] < 255  # rounded corner is transparent
    assert image.getpixel((16, 16))[3] == 255


def test_icon_files_for_packaging(tmp_path):
    from PIL import Image

    paths = write_icon_files(tmp_path)
    for path in paths.values():
        assert path.stat().st_size > 1000
        Image.open(path).verify()


# ----- tray menu -------------------------------------------------------------------------------

try:
    import pystray
except Exception as exc:  # its X11 backend needs a display already at import time
    pystray = None
    _no_tray_reason = f"no tray backend: {exc!r}"

needs_tray = pytest.mark.skipif(pystray is None, reason=globals().get("_no_tray_reason", ""))


@pytest.fixture
def tray(tmp_path, monkeypatch):
    from lightninglan import tray as tray_module

    opened, shown, quits = [], [], []
    monkeypatch.setattr(tray_module, "open_in_file_manager", lambda path, reveal=False: opened.append(path))
    settings = SimpleNamespace(name="Den PC", share_dir=tmp_path / "share", downloads_dir=tmp_path / "dl")
    node = SimpleNamespace(settings=settings, peers=SimpleNamespace(online=lambda: ["peer"] * 2), online=True)
    try:
        t = tray_module.Tray(node, lambda: shown.append(True), tmp_path / "logs", lambda: quits.append(True))
    except Exception as exc:  # e.g. a Linux CI runner with no X display
        pytest.skip(f"no tray backend: {exc}")
    t.calls = SimpleNamespace(opened=opened, shown=shown, quits=quits)
    return t


def menu_items(t):
    return [item for item in t.icon.menu.items if item is not pystray.Menu.SEPARATOR]


@needs_tray
def test_tray_menu(tray):
    assert [item.text for item in menu_items(tray)] == [
        "Open LightningLAN", "Open Shared Folder", "Open Downloads Folder", "Open Log Folder", "Quit LightningLAN",
    ]
    default = [item for item in menu_items(tray) if item.default]
    assert [item.text for item in default] == ["Open LightningLAN"]  # what clicking the icon does


@needs_tray
def test_tray_actions(tray, tmp_path):
    items = {item.text: item for item in menu_items(tray)}
    items["Open LightningLAN"](tray.icon)
    assert tray.calls.shown == [True]  # the app window, or the browser
    items["Open Shared Folder"](tray.icon)
    items["Open Downloads Folder"](tray.icon)
    items["Open Log Folder"](tray.icon)
    assert tray.calls.opened == [tmp_path / "share", tmp_path / "dl", tmp_path / "logs"]
    assert all(Path(p).is_dir() for p in tray.calls.opened), "missing folders are created first"
    items["Quit LightningLAN"](tray.icon)
    assert tray.calls.quits == [True]


@needs_tray
def test_tray_action_errors_are_contained(tray, monkeypatch):
    from lightninglan import tray as tray_module

    def broken(path, reveal=False):
        raise OSError("no file manager")

    monkeypatch.setattr(tray_module, "open_in_file_manager", broken)
    items = {item.text: item for item in menu_items(tray)}
    items["Open Shared Folder"](tray.icon)  # logged, not raised into the tray's event loop


@needs_tray
def test_tray_tooltip(tray):
    assert tray.title() == "LightningLAN: Den PC (2 peers online)"
    tray.node.online = False  # Stop pressed, or the web UI was closed
    assert tray.title() == "LightningLAN: Den PC (stopped)"


# ----- the app window (Windows) ---------------------------------------------------------------

class FakeWebview:
    """Stands in for a pywebview window: records what the app window asks of it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


def app_window(tmp_path):
    from lightninglan.window import AppWindow

    w = AppWindow("http://localhost:8765", tmp_path / "window")
    w._window = FakeWebview()
    return w


def call_names(w, start=0):
    return [c[0] for c in w._window.calls[start:]]


def test_closing_the_window_hides_it_and_leaves_the_page(tmp_path):
    from .harness import wait_for

    w = app_window(tmp_path)
    assert w._on_closing() is False  # the window isn't closed, only hidden
    wait_for(lambda: len(w._window.calls) == 2, timeout=5, message="the window to hide")
    assert call_names(w) == ["hide", "load_html"]  # leaving the page ends the web UI's session
    assert w.show() is True  # opening LightningLAN again
    assert call_names(w, 2) == ["load_url", "show"]
    assert w._window.calls[2][1] == "http://localhost:8765"
    assert w.show() is True  # already showing the page: no reload
    assert call_names(w, 4) == ["show"]


def test_a_minimized_window_is_restored_when_shown(tmp_path):
    w = app_window(tmp_path)
    w._set_minimized(True)
    w.show()
    assert call_names(w) == ["show", "restore"]


def test_quitting_closes_the_window_for_good(tmp_path):
    w = app_window(tmp_path)
    fake = w._window
    w.destroy()
    assert fake.calls == [("destroy",)]
    assert w._on_closing() is True


def test_without_a_window_the_browser_opens(tmp_path):
    from lightninglan.cli import _make_window
    from lightninglan.window import AppWindow

    assert AppWindow("http://localhost:8765", tmp_path).show() is False  # not running: the caller opens the browser
    settings = SimpleNamespace(open_browser=True, state_dir=tmp_path)
    assert _make_window(settings, "http://localhost:8765", gui=False) is None  # from source or a terminal
    settings.open_browser = False
    assert _make_window(settings, "http://localhost:8765", gui=True) is None  # --no-browser


def test_starting_again_shows_the_app_window(peer):
    from .conftest import LAN_CLIENT

    ui = {"X-LightningLAN-Client": "ui"}
    assert peer.post("/api/show-window", headers=ui).json() == {"shown": False}  # browser mode
    shown = []
    peer.app.state.show_window = lambda: shown.append(True) or True
    assert peer.post("/api/show-window", headers=ui).json() == {"shown": True}
    assert shown == [True]
    assert peer.post("/api/show-window", headers=ui, client=LAN_CLIENT).status_code == 403
    assert peer.post("/api/show-window").status_code == 403  # needs the client header


@pytest.mark.integration
def test_a_second_launch_asks_the_running_copy(spawn):
    from lightninglan.cli import _show_running_window
    from .harness import free_port

    a = spawn("A")
    assert _show_running_window(a.url) is False  # it runs without a window: the browser opens instead
    assert _show_running_window(f"http://127.0.0.1:{free_port()}") is False  # nothing there
