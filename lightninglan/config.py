"""Peer configuration: a JSON file holding the peer ID and remembered settings."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .names import is_funny_name, random_name

DEFAULT_PORT = 8765
DEFAULT_REFRESH_INTERVAL = 3.0
MAX_NAME_LENGTH = 64

_PEER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PHONE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class ConfigError(Exception):
    """The configuration is invalid; the message is meant for the user."""


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "LightningLAN" / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LightningLAN" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "lightninglan" / "config.json"


def default_share_dir() -> Path:
    return Path.home() / "LightningLAN" / "Shared"


def default_downloads_dir() -> Path:
    return Path.home() / "LightningLAN" / "Downloads"


def clean_display_name(name: str) -> str:
    """Strip control characters and limit length; fall back to the hostname."""
    name = "".join(c for c in name if c.isprintable()).strip()[:MAX_NAME_LENGTH].strip()
    return name or socket.gethostname()


@dataclass
class Settings:
    peer_id: str
    name: str
    share_dir: Path
    downloads_dir: Path
    port: int = DEFAULT_PORT
    passphrase: str | None = None
    phone_key: str | None = None  # the secret in the phone link (QR code); None until a phone is first set up
    config_path: Path | None = None
    # The name was made up by LightningLAN, not picked by the person: it changes by itself if another
    # peer turns out to have the same one (Node._check_name).
    name_generated: bool = False
    # The web UI plays its tutorial by itself once: on the first run (and until it has been shown).
    tutorial_due: bool = False
    # Runtime-only options (never written to the config file).
    interfaces: list[str] | None = None
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL
    max_upload_rate: float | None = None  # bytes per second, all uploads together
    discovery: bool = True
    open_browser: bool = True
    first_run: bool = False  # no config file existed when this run started

    @property
    def state_dir(self) -> Path:
        """Directory for files LightningLAN maintains itself (hash cache)."""
        if self.config_path is not None:
            return self.config_path.parent
        return default_config_path().parent


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Readable by this user only: the file can hold the passphrase.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def update_config_file(path: Path, values: dict[str, Any]) -> None:
    """Change some keys of the config file, keeping the rest. A value of None removes the key."""
    data = read_config_file(path)
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    try:
        write_config_file(path, data)
    except OSError as exc:
        raise ConfigError(f"cannot write config file {path}: {exc}") from exc


def prepare_folder(folder: str | os.PathLike[str], label: str) -> Path:
    """Make `folder` absolute and create it if needed. Raises ConfigError with a user-facing message."""
    path = Path(folder).expanduser().absolute()
    if path.exists() and not path.is_dir():
        raise ConfigError(f"{label} folder {path} is not a directory")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create {label} folder {path}: {exc.strerror or exc}") from exc
    if not path.is_dir():
        raise ConfigError(f"{label} folder {path} is not a directory")
    return path


def load_settings(
    config_path: Path | None = None,
    *,
    share: str | os.PathLike[str] | None = None,
    downloads: str | os.PathLike[str] | None = None,
    port: int | None = None,
    name: str | None = None,
    passphrase: str | None = None,
    save: bool = True,
) -> Settings:
    """Build Settings from the config file, overridden by explicit arguments.

    A peer ID is generated on first run. Explicit share/downloads/port/name values are
    remembered in the config file for next time; a passphrase given here is used for this run
    only and is never written to disk (put it in the config file to make it permanent).
    """
    path = Path(config_path).expanduser().absolute() if config_path else default_config_path()
    first_run = not path.exists()
    data = read_config_file(path)

    peer_id = data.get("peer_id")
    if not isinstance(peer_id, str) or not _PEER_ID_RE.match(peer_id):
        peer_id = uuid.uuid4().hex

    stored_name = data.get("name") if isinstance(data.get("name"), str) else ""
    if name is not None:
        resolved_name = clean_display_name(name)
        generated = False
    elif stored_name.strip():
        resolved_name = clean_display_name(stored_name)
        stored_generated = data.get("name_generated")
        # Configs from before 1.3 don't say: a funny name in one of them was most likely made up.
        generated = stored_generated if isinstance(stored_generated, bool) else is_funny_name(resolved_name)
    else:
        resolved_name = random_name()  # never picked a name: a funny one, remembered from now on
        generated = True
    share_dir = Path(share if share is not None else data.get("share_dir") or default_share_dir())
    downloads_dir = Path(
        downloads if downloads is not None else data.get("downloads_dir") or default_downloads_dir()
    )
    resolved_port = port if port is not None else data.get("port", DEFAULT_PORT)
    if not isinstance(resolved_port, int) or not 1 <= resolved_port <= 65535:
        raise ConfigError(f"port must be between 1 and 65535, got {resolved_port!r}")

    if passphrase is None:
        stored = data.get("passphrase")
        passphrase = stored if isinstance(stored, str) else None
    passphrase = passphrase or None
    phone_key = data.get("phone_key")
    if not isinstance(phone_key, str) or not PHONE_KEY_RE.match(phone_key):
        phone_key = None
    # Configs from before the tutorial don't say: someone who has used LightningLAN already doesn't need it.
    tutorial_due = first_run or data.get("tutorial_due") is True

    share_dir = prepare_folder(share_dir, "shared")
    downloads_dir = prepare_folder(downloads_dir, "downloads")

    settings = Settings(
        peer_id=peer_id,
        name=resolved_name,
        share_dir=share_dir,
        downloads_dir=downloads_dir,
        port=resolved_port,
        passphrase=passphrase,
        phone_key=phone_key,
        config_path=path,
        first_run=first_run,
        name_generated=generated,
        tutorial_due=tutorial_due,
    )

    if save:
        updated = dict(data)
        updated.update(
            peer_id=peer_id,
            share_dir=str(share_dir),
            downloads_dir=str(downloads_dir),
            port=resolved_port,
        )
        if name is not None or not stored_name.strip():
            updated["name"] = resolved_name
        updated["name_generated"] = generated
        if tutorial_due:
            updated["tutorial_due"] = True
        if updated != data:
            try:
                write_config_file(path, updated)
            except OSError as exc:
                raise ConfigError(f"cannot write config file {path}: {exc}") from exc
    return settings
