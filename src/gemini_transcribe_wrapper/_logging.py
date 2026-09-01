"""Shared file-logging helper.

Issue-005 (spec §4.4): installs a rotating file handler so console output
is mirrored to ``<cache_dir>/logs/gemini-transcribe-wrapper.log`` (5 MB × 3).
The console and file handlers share an ISO-8601 / tz-aware formatter; the
file handler never emits ANSI color codes (issue-006 layers coloring on
top of the console handler only).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .usage_counter import cache_dir as _default_cache_dir

logger = logging.getLogger(__name__)

LOG_FILE_NAME = "gemini-transcribe-wrapper.log"
LOG_SUBDIR = "logs"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 2  # current + 2 past = 3 files total
ENCODING = "utf-8"


def _default_log_path(cache_root: Path | None = None) -> Path:
    """Resolve ``<cache_dir>/logs/gemini-transcribe-wrapper.log``.

    Honors ``$GTW_CACHE_DIR`` (via :func:`usage_counter.cache_dir`) unless
    the caller passes an explicit ``cache_root`` (used by tests against
    ``tmp_path``).
    """
    root = cache_root if cache_root is not None else _default_cache_dir()
    return root / LOG_SUBDIR / LOG_FILE_NAME


def _safe_mkdir(path: Path) -> None:
    """``mkdir(parents=True, exist_ok=True)`` — exposed for monkeypatching."""
    path.mkdir(parents=True, exist_ok=True)


def _make_formatter() -> logging.Formatter:
    """Return a formatter that includes the local tz offset in ``asctime``."""
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _TzFormatter() -> logging.Formatter:
    """Return a :class:`logging.Formatter` whose ``formatTime`` emits ISO-8601
    with a tz offset (e.g. ``2026-09-01T12:34:56.789+09:00``).

    Implemented as a local subclass to mirror the formatter already used
    by ``cli.py``'s console handler. The file handler uses the same shape
    so logs from both destinations are line-for-line comparable.
    """
    from datetime import datetime

    class _TzFormatterImpl(logging.Formatter):
        def formatTime(
            self,
            record: logging.LogRecord,
            datefmt: str | None = None,
        ) -> str:
            return (
                datetime.fromtimestamp(record.created)
                .astimezone()
                .isoformat(timespec="milliseconds")
            )

    return _TzFormatterImpl(
        "%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s"
    )


def setup_file_logging(cache_root: Path | None = None) -> RotatingFileHandler | None:
    """Attach a rotating file handler to the root logger.

    The file path defaults to ``$GTW_CACHE_DIR/logs/gemini-transcribe-wrapper.log``
    (or ``~/.cache/gemini-transcribe-wrapper/logs/...``) unless ``cache_root``
    is supplied — used by tests.

    Returns the installed :class:`RotatingFileHandler` on success, or
    ``None`` when the log directory cannot be created. A warning is logged
    to the module logger in the failure case so the user sees why file
    logging was skipped (the console handler is still attached).
    """
    log_path = _default_log_path(cache_root)

    try:
        _safe_mkdir(log_path.parent)
    except OSError as exc:
        # Unwritable cache_dir: warn but don't crash — console logging
        # still works. Issue-005 §Acceptance: "cache_dir() not writable →
        # stderr warning, no crash."
        logger.warning(
            "Could not create log directory %s: %s — file logging disabled.",
            log_path.parent,
            exc,
        )
        return None

    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding=ENCODING,
        delay=True,  # don't open until first record (issue §Notes)
    )
    handler.setFormatter(_TzFormatter())

    root = logging.getLogger()
    # Tests may install multiple handlers; never duplicate the file handler.
    if any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", None) == handler.baseFilename
        for h in root.handlers
    ):
        handler.close()
        return handler

    root.addHandler(handler)
    return handler
