"""Finding the files that ship with LanShare, whether it runs from source or as a packaged app."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running as an app built with PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def package_dir() -> Path:
    """The lanshare package folder. A PyInstaller build unpacks its data files under sys._MEIPASS."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "lanshare"
    return Path(__file__).resolve().parent


def static_dir() -> Path:
    """The web UI files (index.html, app.js, style.css)."""
    return package_dir() / "static"
