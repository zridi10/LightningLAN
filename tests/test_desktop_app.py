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

    opened, urls, quits = [], [], []
    monkeypatch.setattr(tray_module, "open_in_file_manager", lambda path, reveal=False: opened.append(path))
    monkeypatch.setattr(tray_module, "open_url", lambda url: urls.append(url) or True)
    settings = SimpleNamespace(name="Den PC", share_dir=tmp_path / "share", downloads_dir=tmp_path / "dl")
    node = SimpleNamespace(settings=settings, peers=SimpleNamespace(online=lambda: ["peer"] * 2), online=True)
    try:
        t = tray_module.Tray(node, "http://localhost:8765", tmp_path / "logs", lambda: quits.append(True))
    except Exception as exc:  # e.g. a Linux CI runner with no X display
        pytest.skip(f"no tray backend: {exc}")
    t.calls = SimpleNamespace(opened=opened, urls=urls, quits=quits)
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
    assert tray.calls.urls == ["http://localhost:8765"]
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
