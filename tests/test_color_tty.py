"""Unit tests for issue-006: color the console only when stderr is a TTY.

The console ``StreamHandler`` uses :class:`_ColorFormatter` (extends
``_TzFormatter``) so ``ERROR``/``WARNING`` lines stand out in an interactive
terminal. The file handler (issue-005) must remain plain so log files
stay grep-able. The ``--color=auto|always|never`` flag lets the user
override the auto-detection.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import _logging as gtw_logging

# ---------------------------------------------------------------------------
# _ColorFormatter (lives in _logging.py; cli.py imports from there)
# ---------------------------------------------------------------------------


def _make_record(level: int, msg: str = "boom") -> logging.LogRecord:
    rec = logging.LogRecord(
        name="gtw.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return rec


def test_color_formatter_no_color_when_disabled():
    """``use_color=False`` must emit zero ANSI escapes."""
    fmt = gtw_logging._ColorFormatter(
        "%(asctime)s %(levelname)s %(message)s",
        use_color=False,
    )
    rec = _make_record(logging.ERROR, "boom")
    out = fmt.format(rec)
    assert "\x1b" not in out, f"expected plain output, got: {out!r}"
    assert "ERROR" in out and "boom" in out


def test_color_formatter_wraps_level_when_enabled():
    """``use_color=True`` wraps ``levelname`` in the level's ANSI code."""
    fmt = gtw_logging._ColorFormatter(
        "%(levelname)s %(message)s",
        use_color=True,
    )
    rec = _make_record(logging.ERROR, "boom")
    out = fmt.format(rec)
    assert out.startswith(gtw_logging._ColorFormatter.LEVEL_COLORS["ERROR"])
    assert out.endswith(gtw_logging._ColorFormatter.RESET)
    # Message body is intact.
    assert "ERROR" in out and "boom" in out


def test_color_formatter_inherits_tz_format_from_base():
    """``formatTime`` must still emit ISO-8601 with a tz offset."""
    fmt = gtw_logging._ColorFormatter(
        "%(asctime)s %(levelname)s %(message)s",
        use_color=False,
    )
    rec = _make_record(logging.INFO, "x")
    out = fmt.format(rec)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}",
        out,
    )


def test_color_formatter_levels_each_have_distinct_color():
    """Every standard level maps to a non-empty color code (or empty string)."""
    expected_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    actual = set(gtw_logging._ColorFormatter.LEVEL_COLORS.keys())
    assert expected_levels <= actual
    for level in expected_levels:
        # All non-empty codes begin with ESC[ (the canonical ANSI prefix).
        code = gtw_logging._ColorFormatter.LEVEL_COLORS[level]
        assert code.startswith("\x1b["), f"{level} → {code!r}"


def test_color_formatter_unknown_level_is_plain():
    """An unknown level name (e.g. a custom ``LogRecord.levelName``) is plain."""
    fmt = gtw_logging._ColorFormatter(
        "%(levelname)s %(message)s",
        use_color=True,
    )
    rec = _make_record(logging.INFO, "x")
    rec.levelname = "WEIRD"
    out = fmt.format(rec)
    assert "\x1b" not in out


# ---------------------------------------------------------------------------
# resolve_color_mode
# ---------------------------------------------------------------------------


def test_resolve_color_mode_auto_with_tty(monkeypatch):
    """``auto`` + TTY → ``True``."""
    monkeypatch.setattr(gtw_logging.sys.stderr, "isatty", lambda: True)
    assert gtw_logging.resolve_color_mode("auto") is True


def test_resolve_color_mode_auto_without_tty(monkeypatch):
    """``auto`` + pipe/redirect → ``False``."""
    monkeypatch.setattr(gtw_logging.sys.stderr, "isatty", lambda: False)
    assert gtw_logging.resolve_color_mode("auto") is False


def test_resolve_color_mode_always_forces_on(monkeypatch):
    """``always`` ignores ``isatty``."""
    monkeypatch.setattr(gtw_logging.sys.stderr, "isatty", lambda: False)
    assert gtw_logging.resolve_color_mode("always") is True


def test_resolve_color_mode_never_forces_off(monkeypatch):
    """``never`` ignores ``isatty``."""
    monkeypatch.setattr(gtw_logging.sys.stderr, "isatty", lambda: True)
    assert gtw_logging.resolve_color_mode("never") is False


def test_resolve_color_mode_invalid_value_raises():
    """Unknown values raise ``ValueError`` so the CLI surfaces a usage error."""
    with pytest.raises(ValueError):
        gtw_logging.resolve_color_mode("rainbow")


# ---------------------------------------------------------------------------
# CLI flag wiring
# ---------------------------------------------------------------------------


def test_cli_parser_color_flag_default_auto():
    """``--color`` defaults to ``auto`` (TTY-aware)."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4"])
    assert opts.color == "auto"


def test_cli_parser_color_flag_always():
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--color", "always"])
    assert opts.color == "always"


def test_cli_parser_color_flag_never():
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--color", "never"])
    assert opts.color == "never"


def test_cli_parser_color_flag_invalid_rejected():
    """``--color=rainbow`` is rejected by Click (Choice type)."""
    from click.testing import CliRunner

    from gemini_transcribe_wrapper import cli

    runner = CliRunner()
    result = runner.invoke(cli.app, ["sample.mp4", "--color", "rainbow"])
    assert result.exit_code != 0
    assert "color" in (result.output or "").lower() or "invalid" in (result.output or "").lower()


# ---------------------------------------------------------------------------
# File handler never colors (regression for issue-005 + 006)
# ---------------------------------------------------------------------------


def test_file_handler_uses_plain_formatter(tmp_path):
    """Even with ``--color=always``, the file handler stays plain."""
    handler = gtw_logging.setup_file_logging(tmp_path)
    try:
        # The handler's formatter is _TzFormatter, not _ColorFormatter.
        assert not isinstance(handler.formatter, gtw_logging._ColorFormatter)
        logging.getLogger("gtw.test").error("disk full")
        handler.flush()
        content = Path(handler.baseFilename).read_text(encoding="utf-8")
        assert "\x1b" not in content
        assert "disk full" in content
    finally:
        handler.close()


def test_color_formatter_done_with_api_key_is_green():
    """'Done with api key ...' log lines are colored green on the console."""
    fmt = gtw_logging._ColorFormatter(
        "%(levelname)s %(message)s",
        use_color=True,
    )
    rec = _make_record(logging.INFO, "Done with api key ****Mw9g: sample.srt, sample.txt")
    out = fmt.format(rec)
    assert out.startswith(gtw_logging._ColorFormatter.GREEN)
    assert out.endswith(gtw_logging._ColorFormatter.RESET)
    assert "Done with api key" in out


def test_color_formatter_extra_color_green():
    """Records with extra={'color': 'green'} are colored green on the console."""
    fmt = gtw_logging._ColorFormatter(
        "%(levelname)s %(message)s",
        use_color=True,
    )
    rec = _make_record(logging.INFO, "Custom success message")
    rec.color = "green"
    out = fmt.format(rec)
    assert out.startswith(gtw_logging._ColorFormatter.GREEN)
    assert out.endswith(gtw_logging._ColorFormatter.RESET)
