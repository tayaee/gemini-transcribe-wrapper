"""--loop-until-no-input / --loop-always (issue-001, spec §3.1).

The CLI runs a pass over ``opts.path`` and exits. With a loop flag, it
wraps that pass in an outer poll loop:

- ``--loop-until-no-input``: re-glob after each pass; exit when empty.
- ``--loop-always``: same, but never exit on empty — sleep
  ``--loop-poll-secs`` and re-glob.

Both flags intercept ``QuotaExceededError`` so the wrapper doesn't
``break`` out of the loop on the first 429 — it sleeps and retries.
``KeyboardInterrupt`` exits cleanly with code 130.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import cli

# --- helpers ---------------------------------------------------------------


def _make_opts(**overrides):
    """Build a minimal opts namespace mimicking ``_LAST_OPTIONS[-1]``."""
    base = {
        "path": ["*.mp4"],
        "version": False,
        "output_dir": None,
        "output_base": None,
        "gemini_api_keys": ["kfake1234abcd"],
        "language_codes": [],
        "model": "gemini-3.5-transcribe",
        "diarized_srt_file": None,
        "srt_file": None,
        "txt_file": None,
        "transcript_json_file": None,
        "metadata_json_file": None,
        "force": True,
        "tier": "free",
        "line_interval_secs": 1.0,
        "paragraph_interval_secs": 2.5,
        "request_interval_secs": 120.0,
        "max_chunk_secs": None,
        "speakers": None,
        "custom_vocabulary_file": "auto",
        "word_level_timestamps": True,
        "temp_path": "temp",
        "audit_jsonl_file": None,
        "log_level": "info",
        "loop_until_no_input": False,
        "loop_always": False,
        "loop_poll_secs": 30,
    }
    base.update(overrides)
    return MagicMock(**base)


# --- mutual exclusion ----------------------------------------------------


def test_loop_flags_are_mutually_exclusive(monkeypatch, tmp_path):
    """Passing both --loop-until-no-input and --loop-always → exit 2."""
    # Empty tmp_path; no real files needed.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["gtw", "--loop-until-no-input", "--loop-always", "*.mp4"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2


def test_loop_flag_passes_cli_validation():
    """The Click command rejects both loop flags with a clear error."""
    from click.testing import CliRunner

    from gemini_transcribe_wrapper.cli import app

    runner = CliRunner()
    # Empty path so we don't accidentally hit a real file glob.
    result = runner.invoke(
        app,
        ["--loop-until-no-input", "--loop-always", "/nonexistent/*.mp4"],
        catch_exceptions=True,
    )
    assert result.exit_code == 2
    assert "--loop-until-no-input and --loop-always are mutually exclusive" in result.output


# --- _loop driver unit tests -----------------------------------------------


def test_loop_until_no_input_exits_when_no_matches(monkeypatch):
    """--loop-until-no-input with no matches → exits after first empty pass."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    monkeypatch.setattr(loop_mod, "_glob_matches", lambda patterns: [])
    sleeps: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: sleeps.append(s))

    rc = loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=True,
        loop_always=False,
        loop_poll_secs=30,
        run_pass=lambda matches: (matches, []),
    )
    assert rc == 0
    assert sleeps == []  # no sleep on first empty pass (immediate exit)


def test_loop_until_no_input_exits_when_matches_drained(monkeypatch):
    """--loop-until-no-input exits once a pass yields zero matches."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    # First pass: 2 matches. Second pass: 0 matches → exit.
    responses = iter([["a.mp4", "b.mp4"], []])
    monkeypatch.setattr(loop_mod, "_glob_matches", lambda patterns: next(responses))

    rc = loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=True,
        loop_always=False,
        loop_poll_secs=30,
        run_pass=lambda matches: (matches, []),
    )
    assert rc == 0


def test_loop_always_keeps_polling_when_empty(monkeypatch):
    """--loop-always sleeps and re-globs even when no matches are present."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    # Always empty → driver should sleep poll_secs every iteration.
    # After 2 sleeps we raise to break out (so the test terminates).
    call_count = {"n": 0}

    def _always_empty(patterns):
        call_count["n"] += 1
        if call_count["n"] > 2:
            raise KeyboardInterrupt
        return []

    sleeps: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(loop_mod, "_glob_matches", _always_empty)

    rc = loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=False,
        loop_always=True,
        loop_poll_secs=12,
        run_pass=lambda matches: (matches, []),
    )
    # KeyboardInterrupt → exit 130.
    assert rc == 130
    # Two sleeps of poll_secs before the KeyboardInterrupt fires.
    assert sleeps == [12.0, 12.0]


def test_loop_passes_glob_matches_into_run_pass(monkeypatch):
    """``run_pass`` receives the current glob matches as its first arg."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    monkeypatch.setattr(loop_mod, "_glob_matches", lambda patterns: ["x.mp4"])

    captured: list[list[str]] = []

    def _run_pass(matches):
        captured.append(list(matches))
        if len(captured) >= 2:
            raise KeyboardInterrupt
        return (matches, [])

    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: None)

    loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=False,
        loop_always=True,
        loop_poll_secs=1,
        run_pass=_run_pass,
    )
    assert captured == [["x.mp4"], ["x.mp4"]]


def test_loop_quota_exceeded_sleeps_and_retries(monkeypatch):
    """``QuotaExceededError`` under --loop-always does NOT exit; the
    driver logs, sleeps, and retries on the next pass."""
    from gemini_transcribe_wrapper import _loop as loop_mod
    from gemini_transcribe_wrapper.api import QuotaExceededError

    call_count = {"n": 0}

    def _alternating(patterns):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return ["a.mp4"]
        if call_count["n"] == 2:
            raise KeyboardInterrupt
        return []  # subsequent passes are empty

    sleeps: list[float] = []
    monkeypatch.setattr(loop_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(loop_mod, "_glob_matches", _alternating)

    def _raise_quota(matches):
        # Match the constructor signature of QuotaExceededError.
        raise QuotaExceededError("a.mp4", Exception("429"))

    loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=False,
        loop_always=True,
        loop_poll_secs=5,
        run_pass=_raise_quota,
    )
    # QuotaExceededError → sleep once (between pass #1 and pass #2).
    # Pass #2 raises KeyboardInterrupt which exits with 130 (no extra sleep).
    assert sleeps == [5.0]


def test_loop_keyboard_interrupt_returns_130(monkeypatch):
    """A ``KeyboardInterrupt`` from the inner pass → exit 130."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    monkeypatch.setattr(loop_mod, "_glob_matches", lambda patterns: ["x.mp4"])

    def _raise(matches):
        raise KeyboardInterrupt

    rc = loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=True,
        loop_always=True,  # contradictory; driver doesn't care at runtime
        loop_poll_secs=1,
        run_pass=_raise,
    )
    assert rc == 130


def test_loop_no_flags_returns_pass_exit_code(monkeypatch):
    """With both flags off, ``run_with_loop`` runs ONE pass and returns
    the exit code (or 0 if none)."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    monkeypatch.setattr(loop_mod, "_glob_matches", lambda patterns: ["a.mp4"])
    rc = loop_mod.run_with_loop(
        patterns=["*.mp4"],
        loop_until_no_input=False,
        loop_always=False,
        loop_poll_secs=30,
        run_pass=lambda matches: (matches, []),
    )
    assert rc == 0


def test_loop_poll_secs_clamped_to_range():
    """``--loop-poll-secs`` is clamped to ``[1, 3600]`` at the CLI layer
    (handled by Click's IntRange). The driver accepts the clamped value."""
    from gemini_transcribe_wrapper import _loop as loop_mod

    # Driver doesn't clamp itself; it trusts whatever is passed.
    assert loop_mod._clamp_poll_secs(0) == 1
    assert loop_mod._clamp_poll_secs(5000) == 3600
    assert loop_mod._clamp_poll_secs(30) == 30
