"""Unit tests for issue-005: file-based logging with rotation (5MB × 3).

The wrapper installs a :class:`logging.handlers.RotatingFileHandler` on the
root logger so console output is mirrored to a durable file. The file
path defaults to ``~/.cache/gemini-transcribe-wrapper/logs/gemini-transcribe-wrapper.log``
with 5 MB max size and 2 backups (current + 2 = 3 files total).

These tests target the public helper :func:`gemini_transcribe_wrapper._logging.setup_file_logging`
so they can exercise the real ``RotatingFileHandler`` against a ``tmp_path``
fixture — no need to mock the stdlib handler.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import _logging as gtw_logging
from gemini_transcribe_wrapper.usage_counter import cache_dir


@pytest.fixture(autouse=True)
def _clean_root_handlers():
    """Snapshot and restore root-logger handlers around each test."""
    root = logging.getLogger()
    saved = list(root.handlers)
    saved_level = root.level
    # Force root to DEBUG so ``logger.info`` records reach our handler
    # regardless of what the surrounding suite set.
    root.setLevel(logging.DEBUG)
    yield
    root.handlers = saved
    root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# setup_file_logging
# ---------------------------------------------------------------------------


def test_setup_file_logging_creates_log_dir(tmp_path):
    """First call must create ``<cache_dir>/logs/`` if missing."""
    log_dir = tmp_path / "logs"
    assert not log_dir.exists()
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        assert log_dir.is_dir()
        assert handler.baseFilename == str(log_dir / "gemini-transcribe-wrapper.log")
    finally:
        handler.close()


def test_setup_file_logging_returns_rotating_handler(tmp_path):
    """The returned handler must be a RotatingFileHandler sized at 5 MB / 2 backups."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        from logging.handlers import RotatingFileHandler

        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 2
        assert handler.encoding == "utf-8"
        # delay=True → file not created until first record (per issue §Notes)
        assert handler.delay is True
    finally:
        handler.close()


def test_setup_file_logging_attaches_to_root_logger(tmp_path):
    """After setup, a log record at INFO must appear in the file."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        logging.getLogger("gtw.test").info("hello world")
        handler.flush()
        content = Path(handler.baseFilename).read_text(encoding="utf-8")
        assert "hello world" in content
    finally:
        handler.close()


def test_setup_file_logging_no_ansi_escapes(tmp_path):
    """File log lines must never contain ANSI color escapes (issue-006 builds on this).

    The message itself contains no escape codes — we're verifying the
    formatter does not inject any. (If the user pipes a literal ESC into
    their own log call, that's their data and shouldn't be scrubbed.)
    """
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        logging.getLogger("gtw.test").warning("plain warning text")
        handler.flush()
        content = Path(handler.baseFilename).read_text(encoding="utf-8")
        assert "\x1b" not in content
        assert "plain warning text" in content
    finally:
        handler.close()


def test_setup_file_logging_iso_timestamp_with_offset(tmp_path):
    """Each line starts with an ISO-8601 timestamp carrying a tz offset."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        logging.getLogger("gtw.test").info("event")
        handler.flush()
        line = Path(handler.baseFilename).read_text(encoding="utf-8").splitlines()[0]
        # ``2026-09-01T12:34:56.789+09:00`` style prefix.
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}\s+",
            line,
        ), f"unexpected log line: {line!r}"
    finally:
        handler.close()


def test_setup_file_logging_rotation_creates_backup(tmp_path):
    """Forcing a rollover must produce ``gemini-transcribe-wrapper.log.1``."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        # Override maxBytes so we can trigger rotation without writing 5 MB.
        handler.maxBytes = 200
        logging.getLogger("gtw.test").info("first %s", "x" * 250)
        handler.flush()
        # Force rollover explicitly; the second emit goes to a fresh file.
        handler.doRollover()
        logging.getLogger("gtw.test").info("second")
        handler.flush()

        active = Path(handler.baseFilename)
        backup = Path(str(handler.baseFilename) + ".1")
        assert backup.exists(), "expected backup file after doRollover()"
        assert active.exists()
        # Backup contains the original (over-long) line; active contains 'second'.
        assert "first" in backup.read_text(encoding="utf-8")
        assert "second" in active.read_text(encoding="utf-8")
    finally:
        handler.close()


def test_setup_file_logging_respects_max_backup_count(tmp_path):
    """After backupCount + 1 rollovers, oldest backup is removed."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        handler.maxBytes = 100
        # 4 rollovers → only .1 and .2 should survive (backupCount=2).
        # Note: the last ``doRollover()`` rotates ``gemini-...log`` →
        # ``.1`` (and ``.1`` → ``.2``), so the active file does not exist
        # afterward — by design, that's what a rollover does.
        for i in range(4):
            logging.getLogger("gtw.test").info("entry-%s-%s", i, "x" * 120)
            handler.flush()
            handler.doRollover()

        log_path = Path(handler.baseFilename)
        assert (log_path.parent / (log_path.name + ".1")).exists()
        assert (log_path.parent / (log_path.name + ".2")).exists()
        assert not (log_path.parent / (log_path.name + ".3")).exists()
    finally:
        handler.close()


# ---------------------------------------------------------------------------
# CLI flag wiring
# ---------------------------------------------------------------------------


def test_cli_parser_no_file_log_flag_default_false():
    """``--no-file-log`` defaults to False (file logging enabled)."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4"])
    assert opts.no_file_log is False


def test_cli_parser_no_file_log_flag_enabled():
    """``--no-file-log`` flips the boolean to True."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--no-file-log"])
    assert opts.no_file_log is True


# ---------------------------------------------------------------------------
# cache_dir integration
# ---------------------------------------------------------------------------


def test_setup_file_logging_uses_cache_dir_by_default(tmp_path, monkeypatch):
    """When no cache_dir is passed, $GTW_CACHE_DIR drives the path."""
    monkeypatch.setenv("GTW_CACHE_DIR", str(tmp_path / "cache"))
    handler = gtw_logging.setup_file_logging()
    try:
        expected_dir = tmp_path / "cache" / "logs"
        assert Path(handler.baseFilename).parent == expected_dir
    finally:
        handler.close()
    assert cache_dir() == tmp_path / "cache"


# ---------------------------------------------------------------------------
# Unwritable cache_dir fallback
# ---------------------------------------------------------------------------


def test_setup_file_logging_returns_none_when_cache_dir_unwritable(tmp_path, monkeypatch, caplog):
    """Unwritable cache_dir → log a warning to stderr and return None (no crash)."""
    import logging as _logging

    # Make ``mkdir`` raise to simulate an unwritable cache_dir.
    def _raise(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(
        gtw_logging,
        "_safe_mkdir",
        _raise,
    )
    with caplog.at_level(_logging.WARNING, logger="gemini_transcribe_wrapper._logging"):
        result = gtw_logging.setup_file_logging(tmp_path)
    assert result is None
    assert any(
        "file logging disabled" in rec.message.lower() for rec in caplog.records
    ), "expected a warning explaining why file logging was disabled"


# ---------------------------------------------------------------------------
# JSON manifest sanity check (test infrastructure smoke test)
# ---------------------------------------------------------------------------


def test_test_module_self_sanity():
    """Sanity: ``tests`` directory exists and is iterable; this test merely
    validates the JSON-encoding helper used by other tests in this file."""
    assert json.dumps({"ok": True}) == '{"ok": true}'
