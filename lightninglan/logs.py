"""Logging to the console (when there is one) and to a file in the app-data folder."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = "lightninglan.log"
MAX_LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger("lightninglan")


def log_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "logs"


class _NotFileOnly(logging.Filter):
    """Keeps records logged with extra={"file_only": True} off the console."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


def setup_console_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr is not None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_NotFileOnly())
        root.addHandler(handler)
    if not verbose:
        for noisy in ("httpx", "httpcore", "zeroconf", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def add_file_logging(state_dir: Path) -> Path | None:
    """Also log to <state_dir>/logs/lightninglan.log (rotated at 1 MB). Returns the file, or None."""
    directory = log_dir(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("cannot write a log file in %s: %s", directory, exc)
        return None
    handler.setFormatter(logging.Formatter(_FORMAT))
    logging.getLogger().addHandler(handler)
    return directory / LOG_FILE


def log_uncaught_exceptions() -> None:
    """Send crashes to the log too; a packaged app has no console to print them on."""

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("unhandled error", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("unhandled error in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
