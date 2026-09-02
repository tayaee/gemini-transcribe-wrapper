"""Shared logging helpers.

Two formatters share an ISO-8601 + tz-aware ``formatTime``:

* :class:`_TzFormatter` — plain text, used by the rotating file handler.
* :class:`_ColorFormatter` — wraps the levelname in ANSI color codes when
  ``use_color=True``. Used by the console ``StreamHandler`` so interactive
  terminals highlight ``ERROR`` / ``WARNING`` lines.

Also exports :func:`resolve_color_mode` for the ``--color=auto|always|never``
flag and :func:`setup_file_logging` (issue-005) for the rotating file handler.

Issue-005: rotating file handler (5 MB × 3).
Issue-006: console-only color, gated by ``sys.stderr.isatty()`` and
``--color`` override.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import ClassVar

from .usage_counter import cache_dir as _default_cache_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File-handler constants (issue-005)
# ---------------------------------------------------------------------------

LOG_FILE_NAME = "gemini-transcribe-wrapper.log"
LOG_SUBDIR = "logs"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 2  # current + 2 past = 3 files total
ENCODING = "utf-8"


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class _TzFormatter(logging.Formatter):
    """Formatter whose ``%(asctime)s`` includes the local tz offset.

    ``logging.Formatter.formatTime`` defaults to ``time.localtime()``
    which produces a tz-naive ``YYYY-MM-DD HH:MM:SS,fff`` — fine when
    the reader knows the host's tz, ambiguous when logs are forwarded
    or reviewed later. We use ``datetime.now().astimezone()`` so the
    suffix (``+09:00`` etc.) is visible in every log line.
    """

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


class _ColorFormatter(_TzFormatter):
    """Wrap the levelname in ANSI color codes when ``use_color`` is true.

    Only the ``levelname`` field is colored. The timestamp, filename,
    and message body stay plain so log lines remain greppable across
    interactive / piped / file contexts and ``grep -E "ERROR"`` works
    even when colors are stripped.
    """

    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\x1b[90m",  # bright black / gray
        "INFO": "\x1b[37m",  # white
        "WARNING": "\x1b[33m",  # yellow
        "ERROR": "\x1b[31m",  # red
        "CRITICAL": "\x1b[35;1m",  # bold magenta
    }
    GREEN: ClassVar[str] = "\x1b[32m"
    RESET: ClassVar[str] = "\x1b[0m"

    def __init__(self, *args, use_color: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._use_color = use_color

    @property
    def use_color(self) -> bool:
        return self._use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not self._use_color:
            return msg
        if (
            getattr(record, "color", None) == "green"
            or "Done. Created" in record.getMessage()
            or record.getMessage().startswith("Done with api key")
        ):
            color = self.GREEN
        else:
            color = self.LEVEL_COLORS.get(record.levelname, "")
        if not color:
            return msg
        return f"{color}{msg}{self.RESET}"


# ---------------------------------------------------------------------------
# Color-mode resolution (--color=auto|always|never)
# ---------------------------------------------------------------------------


def resolve_color_mode(value: str) -> bool:
    """Map the ``--color`` flag to a concrete boolean.

    ``auto`` → ``sys.stderr.isatty()``
    ``always`` → ``True`` regardless of TTY
    ``never`` → ``False`` regardless of TTY

    Unknown values raise ``ValueError`` so the CLI layer can surface a
    clear error (Click's ``Choice`` already rejects them at parse time,
    so this is a defense-in-depth check for direct callers).
    """
    if value == "auto":
        return bool(sys.stderr.isatty())
    if value == "always":
        return True
    if value == "never":
        return False
    raise ValueError(f"invalid --color value: {value!r} (use auto|always|never)")


# ---------------------------------------------------------------------------
# File-handler install (issue-005)
# ---------------------------------------------------------------------------


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
    # File handler is always plain — issue-006 explicitly bans file coloring
    # so log files stay grep-able regardless of console settings.
    handler.setFormatter(_TzFormatter("%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s"))

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
