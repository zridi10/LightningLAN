"""Steam games: the Steam libraries on this computer, and sharing the games installed in them.

Steam keeps each library's games in <library>/steamapps: an appmanifest_<appid>.acf per game (a
KeyValues text file saying what is installed, at which build, in which folder) and the game itself
in common/<installdir>. The libraries are listed in steamapps/libraryfolders.vdf in the Steam
folder. A game copied into common/ with its manifest next to the others is in that library the
next time Steam starts, as if Steam had installed it; playing it still needs a Steam account that
owns it. steamget.py does that copying.

With sharing on, the fully installed games are listed in GET /api/files (under "steam"). A peer
that wants one reads its file list, GET /api/steam/<appid>: the files are hashed (SHA-256) the
first time someone asks, in the background, and the list grows as they are, so the copy can start
at once. The hashes are kept in steam_hash_cache.json, so a game is only hashed again when it
changes. The files themselves come from GET /api/steam/<appid>/<path>.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .fsutil import long_path
from .indexer import is_junction
from .pathsafe import UnsafePathError, display_name, safe_join, validate_component

log = logging.getLogger(__name__)

if sys.platform == "win32":
    STEAM_OS = "windows"
elif sys.platform == "darwin":
    STEAM_OS = "mac"
else:
    STEAM_OS = "linux"

SCAN_INTERVAL = 30.0  # how often the libraries are read again (a few small text files)
PAGE_SIZE = 5000  # files per GET /api/steam/<appid> answer; the rest comes with ?since=
PART_SUFFIX = ".lightninglan-part"  # a file being copied into a game's folder (steamget.py)
MAX_MANIFEST_BYTES = 256 * 1024
MAX_JOBS = 16  # games whose file lists are kept in memory
HASH_CHUNK = 1024 * 1024

FULLY_INSTALLED = 4
# Besides "fully installed": an update is due or paused, or the game is running. Its files are still
# one whole build then. Any other state (updating, validating, files missing...) is not shared.
_SHAREABLE_EXTRA = 2 | 64 | 512

# Steam's own tools, which it installs by itself when a game needs them.
_TOOL_APPIDS = frozenset({228980, 1070560, 1391110, 1628350})
_TOOL_PREFIXES = ("steamworks common redistributables", "steam linux runtime", "proton ")


# ----- Valve's KeyValues text format (.vdf, .acf) -----------------------------------------------

class VdfError(ValueError):
    pass


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}
_MAX_DEPTH = 16


def parse_vdf(text: str) -> dict:
    """"key" "value" pairs and "key" { ... } blocks, as nested dicts. Raises VdfError."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for kind, value in _tokens(text):
        if kind == "str":
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
        elif kind == "{":
            if key is None or len(stack) > _MAX_DEPTH:
                raise VdfError("unexpected {")
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        else:
            if key is not None or len(stack) == 1:
                raise VdfError("unexpected }")
            stack.pop()
    if key is not None or len(stack) != 1:
        raise VdfError("unexpected end of file")
    return root


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n\ufeff":
            i += 1
        elif c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
        elif c in "{}":
            yield c, c
            i += 1
        elif c == '"':
            out = []
            i += 1
            while True:
                if i >= n:
                    raise VdfError("unterminated string")
                c = text[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n:
                    out.append(_ESCAPES.get(text[i + 1], c + text[i + 1]))
                    i += 2
                    continue
                out.append(c)
                i += 1
            yield "str", "".join(out)
        elif c == "[":  # a platform condition after a value, such as [$WIN32]
            end = text.find("]", i)
            if end == -1:
                raise VdfError("unterminated condition")
            i = end + 1
        else:
            start = i
            while i < n and text[i] not in ' \t\r\n{}"':
                i += 1
            yield "str", text[start:i]


def dump_vdf(data: dict) -> str:
    """The inverse of parse_vdf, laid out the way Steam writes its files."""
    lines: list[str] = []
    _dump(data, 0, lines)
    return "\n".join(lines) + "\n"


def _dump(block: dict, depth: int, lines: list[str]) -> None:
    pad = "\t" * depth
    for key, value in block.items():
        if isinstance(value, dict):
            lines += [pad + _quote(key), pad + "{"]
            _dump(value, depth + 1, lines)
            lines.append(pad + "}")
        else:
            lines.append(f"{pad}{_quote(key)}\t\t{_quote(value)}")


def _quote(text: object) -> str:
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def vdf_get(block: dict, key: str, default=None):
    """block[key], with Steam's case-insensitive keys ("appid" is sometimes "AppID")."""
    if key in block:
        return block[key]
    lower = key.lower()
    return next((v for k, v in block.items() if k.lower() == lower), default)


def _vdf_set(block: dict, key: str, value: object) -> None:
    existing = next((k for k in block if k.lower() == key.lower()), key)
    block[existing] = value


def _int(value: object) -> int | None:
    if isinstance(value, str) and value.isdigit() and len(value) <= 19:
        return int(value)
    return None


# ----- finding Steam and its libraries ----------------------------------------------------------

def find_steam_dir() -> Path | None:
    """Where Steam is installed, or None."""
    for candidate in _steam_dir_candidates():
        try:
            if (candidate / "steamapps").is_dir():
                return candidate.absolute()
        except OSError:
            continue
    return None


def _steam_dir_candidates() -> Iterator[Path]:
    if sys.platform == "win32":
        import winreg

        keys = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, subkey, name in keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if isinstance(value, str) and value:
                yield Path(value)
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(variable)
            if base:
                yield Path(base) / "Steam"
    elif sys.platform == "darwin":
        yield Path.home() / "Library" / "Application Support" / "Steam"
    else:
        home = Path.home()
        yield Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "Steam"
        yield home / ".steam" / "steam"
        yield home / ".steam" / "root"
        yield home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"  # Flatpak
        yield home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"


def same_folder(a: Path, b: Path) -> bool:
    return _folder_key(a) == _folder_key(b)


def _folder_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def library_folders(steam_dir: Path) -> list[Path]:
    """Every Steam library: the Steam folder's own and the ones in libraryfolders.vdf, each once."""
    found: list[Path] = []
    for vdf in (steam_dir / "steamapps" / "libraryfolders.vdf", steam_dir / "config" / "libraryfolders.vdf"):
        try:
            data = parse_vdf(_read_small(vdf))
        except (OSError, VdfError, UnicodeError):
            continue
        block = vdf_get(data, "libraryfolders")
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if not key.isdigit():
                continue  # "contentstatsid" and the like
            # Steam has written both "1" "D:\\SteamLibrary" and "1" { "path" "D:\\SteamLibrary" ... }.
            path = vdf_get(value, "path") if isinstance(value, dict) else value
            if isinstance(path, str) and path:
                found.append(Path(path))
        break
    found.append(steam_dir)  # after the list, which spells it better than the registry's lower case
    libraries: list[Path] = []
    seen: set[str] = set()
    for path in found:
        try:
            if not path.is_absolute() or not (path / "steamapps").is_dir():
                continue
            key = _folder_key(path)
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            libraries.append(path)
    return libraries


def _read_small(path: Path) -> str:
    with open(path, "rb") as f:
        data = f.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise VdfError(f"{path.name} is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:  # older manifests can have a name in the Windows code page (a 0x99 for ™)
        return data.decode("cp1252", errors="replace")


# ----- the games in a library -------------------------------------------------------------------

@dataclass(frozen=True)
class SteamGame:
    appid: int
    name: str
    installdir: str
    size: int  # SizeOnDisk
    buildid: str
    updated: int  # LastUpdated: when Steam last installed or updated it (Unix time)
    flags: int  # StateFlags
    library: Path
    manifest: Path  # its appmanifest_<appid>.acf

    @property
    def folder(self) -> Path:
        return self.library / "steamapps" / "common" / self.installdir

    @property
    def shareable(self) -> bool:
        return bool(self.flags & FULLY_INSTALLED) and not self.flags & ~(FULLY_INSTALLED | _SHAREABLE_EXTRA)

    def to_dict(self) -> dict:
        return {"appid": self.appid, "name": self.name, "size": self.size, "buildid": self.buildid,
                "updated": self.updated}


def is_tool(appid: int, name: str) -> bool:
    lower = name.lower()
    return appid in _TOOL_APPIDS or lower == "proton" or lower.startswith(_TOOL_PREFIXES)


def read_game(acf: Path, library: Path) -> SteamGame | None:
    """The game an appmanifest_<appid>.acf describes, or None if it can't be used."""
    try:
        state = vdf_get(parse_vdf(_read_small(acf)), "AppState")
    except (OSError, VdfError):
        return None
    if not isinstance(state, dict):
        return None
    appid = _int(vdf_get(state, "appid"))
    installdir = vdf_get(state, "installdir")
    if appid is None or acf.name != f"appmanifest_{appid}.acf" or not isinstance(installdir, str):
        return None
    try:
        validate_component(installdir)
    except UnsafePathError:
        return None
    raw_name = vdf_get(state, "name")
    name = display_name(raw_name).strip()[:200] if isinstance(raw_name, str) else ""
    buildid = vdf_get(state, "buildid")
    return SteamGame(
        appid=appid,
        name=name or f"Steam app {appid}",
        installdir=installdir,
        size=_int(vdf_get(state, "SizeOnDisk")) or 0,
        buildid=buildid if isinstance(buildid, str) and buildid.isdigit() else "0",
        updated=_int(vdf_get(state, "LastUpdated")) or 0,
        flags=_int(vdf_get(state, "StateFlags")) or 0,
        library=library,
        manifest=acf,
    )


def scan_games(libraries: list[Path]) -> dict[int, SteamGame]:
    """Every game Steam has a manifest for, in any state, tools left out. A game in two libraries
    counts once: the fully installed copy, else the most recently updated one."""
    games: dict[int, SteamGame] = {}
    for library in libraries:
        try:
            names = sorted(os.listdir(library / "steamapps"))
        except OSError:
            continue
        for name in names:
            if not (name.startswith("appmanifest_") and name.endswith(".acf")):
                continue
            game = read_game(library / "steamapps" / name, library)
            if game is None or is_tool(game.appid, game.name):
                continue
            other = games.get(game.appid)
            if other is None or (game.shareable, game.updated) > (other.shareable, other.updated):
                games[game.appid] = game
    return games


# What a game's manifest keeps when it is copied to another computer: only what says which game is
# installed, at which build and with which depots. LastOwner (the sharer's Steam account),
# LauncherPath, LastPlayed and the like are left out; Steam fills them in.
_PORTABLE_KEYS = frozenset(k.lower() for k in (
    "appid", "Universe", "name", "StateFlags", "installdir", "LastUpdated", "SizeOnDisk", "StagingSize", "buildid",
    "DownloadType", "UpdateResult", "BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged",
    "TargetBuildID", "AutoUpdateBehavior", "AllowOtherDownloadsWhileRunning", "ScheduledAutoUpdate",
    "InstalledDepots", "SharedDepots", "InstallScripts", "UserConfig", "MountedConfig",
))


def portable_app_state(state: dict, appid: int, installdir: str) -> dict:
    """A game's AppState as it is written on the computer it is copied to: fully installed, at the build
    its files are, with the update Steam may have been waiting for still to come."""
    out = {k: v for k, v in state.items() if k.lower() in _PORTABLE_KEYS}
    _vdf_set(out, "appid", str(appid))
    _vdf_set(out, "installdir", installdir)
    _vdf_set(out, "StateFlags", str(FULLY_INSTALLED))
    buildid = vdf_get(out, "buildid")
    if isinstance(buildid, str):
        _vdf_set(out, "TargetBuildID", buildid)
    for key in ("UpdateResult", "ScheduledAutoUpdate", "StagingSize"):
        if vdf_get(out, key) is not None:
            _vdf_set(out, key, "0")
    for total, done in (("BytesToDownload", "BytesDownloaded"), ("BytesToStage", "BytesStaged")):
        value = vdf_get(out, total)
        if isinstance(value, str):
            _vdf_set(out, done, value)
    return out


def portable_manifest(text: str, appid: int, installdir: str) -> str:
    """Check a game's manifest (from this computer or another peer) and make it portable. Raises ValueError."""
    if len(text) > MAX_MANIFEST_BYTES:
        raise ValueError("the game's Steam manifest is too large")
    state = vdf_get(parse_vdf(text), "AppState")
    if not isinstance(state, dict):
        raise ValueError("the game's Steam manifest has no AppState")
    if _int(vdf_get(state, "appid")) != appid or vdf_get(state, "installdir") != installdir:
        raise ValueError("the game's Steam manifest is for another game")
    return dump_vdf({"AppState": portable_app_state(state, appid, installdir)})


# ----- hashing a game's files ---------------------------------------------------------------------

def walk_game(folder: Path, abort: threading.Event) -> tuple[list[tuple[str, str, int, int, bool]], list[str], int]:
    """Everything in a game's folder: (relpath, path to open, size, mtime_ns, executable) for each file,
    sorted, the empty folders, and how many files were left out because their names would not work on
    every computer. Linked folders are not entered."""
    root = long_path(folder)
    real_root = os.path.normcase(os.path.realpath(root))
    files: list[tuple[str, str, int, int, bool]] = []
    has_children: dict[str, bool] = {}
    skipped = 0
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        if abort.is_set():
            raise InterruptedError("stopped")
        directory, prefix = stack.pop()
        try:
            scanner = os.scandir(directory)
        except OSError as exc:
            log.debug("cannot list %s: %s", directory, exc)
            continue
        with scanner:
            for entry in scanner:
                if entry.name.endswith(PART_SUFFIX):
                    continue
                try:
                    validate_component(entry.name)
                except UnsafePathError:
                    skipped += 1
                    continue
                rel = prefix + entry.name
                try:
                    if entry.is_symlink():
                        target = os.path.realpath(entry.path)
                        if not os.path.normcase(target).startswith(real_root + os.sep) or not os.path.isfile(target):
                            continue
                        st = os.stat(target)
                    elif entry.is_dir(follow_symlinks=False):
                        if not is_junction(entry):
                            stack.append((entry.path, rel + "/"))
                            has_children.setdefault(rel, False)
                            if prefix:
                                has_children[prefix[:-1]] = True
                        continue
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                    else:
                        continue
                except OSError:
                    continue
                if prefix:
                    has_children[prefix[:-1]] = True
                files.append((rel, entry.path, st.st_size, st.st_mtime_ns, os.name != "nt" and bool(st.st_mode & 0o111)))
    files.sort(key=lambda f: f[0])
    empty = sorted(rel for rel, children in has_children.items() if not children)
    return files, empty, skipped


def sha256_file(path: str, abort: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    buf = bytearray(HASH_CHUNK)
    view = memoryview(buf)
    with open(path, "rb") as f:
        while n := f.readinto(buf):
            if abort is not None and abort.is_set():
                raise InterruptedError("stopped")
            digest.update(view[:n])
    return digest.hexdigest()


class HashCache:
    """SHA-256 of game files by path, kept while their size and modification time stay the same."""

    def __init__(self, path: Path | None):
        self.path = path
        self._files: dict[str, tuple[int, int, str]] = {}
        self._dirty = False
        self._lock = threading.Lock()
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for key, v in data.get("files", {}).items():
                    if isinstance(v, list) and len(v) == 3:
                        self._files[key] = (int(v[0]), int(v[1]), str(v[2]))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                log.warning("ignoring unreadable Steam hash cache %s: %s", path, exc)

    def get(self, path: str, size: int, mtime_ns: int) -> str | None:
        found = self._files.get(path)
        return found[2] if found is not None and found[:2] == (size, mtime_ns) else None

    def put(self, path: str, size: int, mtime_ns: int, sha256: str) -> None:
        with self._lock:
            self._files[path] = (size, mtime_ns, sha256)
            self._dirty = True

    def save(self, keep: list[Path]) -> None:
        """Write the cache, keeping only files inside the `keep` folders (the games still shared)."""
        if self.path is None:
            return
        prefixes = tuple(long_path(folder) + os.sep for folder in keep)
        with self._lock:
            kept = {k: v for k, v in self._files.items() if k.startswith(prefixes)}
            if not self._dirty and len(kept) == len(self._files):
                return
            self._files, self._dirty = kept, False
            payload = {"version": 1, "files": {k: list(v) for k, v in kept.items()}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save the Steam hash cache: %s", exc)


class FileList:
    """One shared game's files with their SHA-256, worked out in a background thread. The list grows
    while the files are hashed, so a peer can start copying the first ones before it is done."""

    def __init__(self, game: SteamGame, cache: HashCache, slot: threading.Semaphore, on_hashed):
        self.game = game
        self.key = (game.folder, game.buildid, game.updated)
        self.touched = time.monotonic()
        self.stale = False  # a file changed after it was hashed: the next list from the start is new
        self.records: dict[str, tuple[int, int]] = {}  # relpath -> (size, mtime_ns) when it was hashed
        self._cache = cache
        self._slot = slot  # one game is hashed at a time, so the disk isn't read in two places at once
        self._on_done = on_hashed  # saves the hash cache
        self._files: list[dict] = []
        self._dirs: list[str] = []
        self._total_files: int | None = None
        self._total_bytes = 0
        self._hashed_bytes = 0
        self._skipped = 0
        self._complete = False
        self._error: str | None = None
        self._acf: str | None = None
        self._abort = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        game = self.game
        try:  # right away: every page of the list carries it
            self._acf = portable_manifest(_read_small(game.manifest), game.appid, game.installdir)
        except (OSError, ValueError) as exc:
            log.warning("could not read the Steam manifest of %s: %s", game.name, exc)
            self._error = f"Could not read {game.name} on this computer: {exc}"
            return
        threading.Thread(target=self._run, name=f"steam-files-{game.appid}", daemon=True).start()

    def abort(self) -> None:
        self._abort.set()

    def payload(self, since: int) -> dict:
        game = self.game
        with self._lock:
            since = max(0, min(since, len(self._files)))
            files = self._files[since:since + PAGE_SIZE]
            complete = self._complete and since + len(files) == len(self._files)
            return {
                "appid": game.appid,
                "name": game.name,
                "installdir": game.installdir,
                "buildid": game.buildid,
                "os": STEAM_OS,
                "acf": self._acf,
                "since": since,
                "files": files,
                "listed": len(self._files),  # hashed so far
                "complete": complete,
                "dirs": self._dirs if complete else [],
                "total_files": self._total_files,
                "total_bytes": self._total_bytes,
                "hashed_bytes": self._hashed_bytes,
                "skipped": self._skipped,
                "error": self._error,
            }

    def _run(self) -> None:
        game = self.game
        try:
            found, empty, skipped = walk_game(game.folder, self._abort)
            with self._lock:
                self._total_files = len(found)
                self._total_bytes = sum(f[2] for f in found)
                self._skipped = skipped
            if skipped:
                log.warning("%s: %d files have names that don't work on every computer; they are not shared",
                            game.name, skipped)
            while not self._slot.acquire(timeout=0.5):
                if self._abort.is_set():
                    return
            try:
                started = time.monotonic()
                for rel, path, listed_size, mtime_ns, executable in found:
                    hashed = self._hash(path, listed_size, mtime_ns)
                    if hashed is None:
                        with self._lock:
                            self._skipped += 1
                            self._total_files -= 1
                            self._total_bytes -= listed_size
                        continue
                    sha, size, mtime_ns = hashed
                    entry = {"p": rel, "s": size, "h": sha}
                    if executable:
                        entry["x"] = 1
                    with self._lock:
                        self._files.append(entry)
                        self.records[rel] = (size, mtime_ns)
                        self._hashed_bytes += size
                        self._total_bytes += size - listed_size
            finally:
                self._slot.release()
            self._on_done()
            with self._lock:
                self._dirs = empty
                self._complete = True
            log.info("file list of %s ready: %d files in %.1f s", game.name, len(found), time.monotonic() - started)
        except InterruptedError:
            return
        except (OSError, ValueError) as exc:
            log.warning("could not list the files of %s: %s", game.name, exc)
            with self._lock:
                self._error = f"Could not read {game.name} on this computer: {exc}"

    def _hash(self, path: str, size: int, mtime_ns: int) -> tuple[str, int, int] | None:
        """The file's (SHA-256, size, mtime_ns), or None if it keeps changing (a log the running game
        writes to, say)."""
        cached = self._cache.get(path, size, mtime_ns)
        if cached is not None:
            return cached, size, mtime_ns
        for _ in range(3):
            try:
                sha = sha256_file(path, self._abort)
                st = os.stat(path)
            except OSError as exc:
                log.debug("skipping %s: %s", path, exc)
                return None
            if (st.st_size, st.st_mtime_ns) == (size, mtime_ns):
                self._cache.put(path, size, mtime_ns, sha)
                return sha, size, mtime_ns
            size, mtime_ns = st.st_size, st.st_mtime_ns
        return None


# ----- this computer's Steam ----------------------------------------------------------------------

class SteamLibrary:
    """The Steam games on this computer (read again every SCAN_INTERVAL) and, while sharing is on, the
    file lists of the ones other peers copy."""

    def __init__(self, steam_dir: Path | None = None, cache_path: Path | None = None, sharing: bool = False,
                 interval: float = SCAN_INTERVAL):
        self.steam_dir_override = steam_dir
        self.interval = interval
        self.steam_dir: Path | None = None
        self.libraries: list[Path] = []
        self.installed: dict[int, SteamGame] = {}  # every game with a manifest, in any state
        self.sharing = sharing
        self.version = 0  # bumps when the games or sharing change
        self._cache = HashCache(cache_path)
        self._jobs: dict[int, FileList] = {}
        self._jobs_lock = threading.Lock()
        self._hash_slot = threading.Semaphore(1)
        self._scan_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def found(self) -> bool:
        return self.steam_dir is not None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lightninglan-steam", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._jobs_lock:
            for job in self._jobs.values():
                job.abort()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def request_rescan(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                log.exception("reading the Steam libraries failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def scan(self) -> bool:
        """Read the libraries again. Returns True if anything changed."""
        with self._scan_lock:
            steam_dir = self.steam_dir_override or find_steam_dir()
            libraries = library_folders(steam_dir) if steam_dir is not None else []
            installed = scan_games(libraries)
            if (steam_dir, libraries, installed) == (self.steam_dir, self.libraries, self.installed):
                return False
            if steam_dir != self.steam_dir:
                log.info("Steam found in %s" if steam_dir else "Steam is no longer found", steam_dir)
            self.steam_dir, self.libraries, self.installed = steam_dir, libraries, installed
            self.version += 1
            return True

    # ----- sharing ----------------------------------------------------------------------------

    def set_sharing(self, on: bool) -> None:
        self.sharing = on
        self.version += 1
        if not on:
            with self._jobs_lock:
                for job in self._jobs.values():
                    job.abort()
                self._jobs.clear()
        self.request_rescan()

    def shared(self) -> list[SteamGame]:
        if not self.sharing:
            return []
        return sorted((g for g in self.installed.values() if g.shareable), key=lambda g: g.name.lower())

    def shared_game(self, appid: int) -> SteamGame | None:
        game = self.installed.get(appid) if self.sharing else None
        return game if game is not None and game.shareable else None

    def listing(self) -> dict | None:
        """What GET /api/files says about the games shared: None when there are none."""
        games = self.shared()
        if not games:
            return None
        return {"v": 1, "os": STEAM_OS, "games": [g.to_dict() for g in games]}

    def file_list(self, appid: int, since: int = 0) -> dict | None:
        """The shared game's file list from file number `since` on (started the first time), or None."""
        game = self.shared_game(appid)
        if game is None:
            return None
        with self._jobs_lock:
            job = self._jobs.get(appid)
            if job is None or job.key != (game.folder, game.buildid, game.updated) or (job.stale and since == 0):
                if job is not None:
                    job.abort()
                job = self._jobs[appid] = FileList(game, self._cache, self._hash_slot, self._save_cache)
                job.start()
                self._trim_jobs()
            job.touched = time.monotonic()
        return job.payload(since)

    def _trim_jobs(self) -> None:
        while len(self._jobs) > MAX_JOBS:
            oldest = min(self._jobs, key=lambda appid: self._jobs[appid].touched)
            self._jobs.pop(oldest).abort()

    def _save_cache(self) -> None:
        self._cache.save([g.folder for g in self.shared()])

    def open_file(self, appid: int, relpath: str) -> tuple[Path, int, int] | str:
        """A shared game's file to send: (path, size, mtime_ns), or why not: "not_found" or "changed"
        (it is not the file that was hashed any more)."""
        game = self.shared_game(appid)
        if game is None:
            return "not_found"
        try:
            path = safe_join(game.folder, relpath)
            st = os.stat(long_path(path))
        except (UnsafePathError, OSError):
            return "not_found"
        if not stat.S_ISREG(st.st_mode):
            return "not_found"
        job = self._jobs.get(appid)
        record = job.records.get(relpath) if job is not None else None
        if record is not None and record != (st.st_size, st.st_mtime_ns):
            job.stale = True
            return "changed"
        return path, st.st_size, st.st_mtime_ns
