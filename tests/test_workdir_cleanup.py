"""Tests for ``cli.cleanup_old_workdirs``.

The cleanup walks ``opts.temp_path`` (resolved like ``api._setup_workdir``
does — relative to ``opts.output_dir`` when given, else cwd) and removes
every ``*-work`` directory whose mtime is older than 24 hours (default).
Best-effort: per-directory errors are logged and skipped, never raised.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.cli import (
    TranscribeOptions,
    _format_age,
    _resolve_temp_dir,
    cleanup_old_workdirs,
)

# --- _format_age ----------------------------------------------------------


def test_format_age_subminute():
    assert _format_age(30) == "0m"


def test_format_age_minutes():
    assert _format_age(45 * 60) == "45m"


def test_format_age_hours_and_minutes():
    assert _format_age(3 * 3600 + 20 * 60) == "3h 20m"


def test_format_age_days_hours():
    assert _format_age(2 * 86400 + 5 * 3600) == "2d 5h"


def test_format_age_handles_negative():
    # Future mtime (clock skew / mocked) clamps to 0.
    assert _format_age(-100) == "0m"


# --- _resolve_temp_dir ----------------------------------------------------


def test_resolve_temp_dir_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = TranscribeOptions(temp_path=str(tmp_path / "abs-temp"))
    assert _resolve_temp_dir(opts) == (tmp_path / "abs-temp").resolve()


def test_resolve_temp_dir_relative_anchored_to_output_dir(tmp_path):
    opts = TranscribeOptions(
        temp_path="temp", output_dir=str(tmp_path / "out"),
    )
    assert _resolve_temp_dir(opts) == (tmp_path / "out" / "temp").resolve()


def test_resolve_temp_dir_relative_anchored_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = TranscribeOptions(temp_path="temp")
    assert _resolve_temp_dir(opts) == (tmp_path / "temp").resolve()


# --- cleanup_old_workdirs -------------------------------------------------


def _make_workdir(parent: Path, name: str, age_secs: float) -> Path:
    """Create ``parent/name`` and backdate its mtime by ``age_secs`` seconds."""
    d = parent / name
    d.mkdir()
    mtime = time.time() - age_secs
    # Use utime (atime, mtime) — both must move or os.utime refuses.
    import os

    os.utime(d, (mtime, mtime))
    return d


def test_cleanup_removes_only_old_workdirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    old = _make_workdir(temp_dir, "video.gemini-aaaa-work", age_secs=25 * 3600)
    fresh = _make_workdir(temp_dir, "video.gemini-bbbb-work", age_secs=1 * 3600)
    boundary = _make_workdir(temp_dir, "video.gemini-cccc-work", age_secs=24 * 3600 + 1)

    opts = TranscribeOptions(temp_path="temp")
    deleted = cleanup_old_workdirs(opts)

    assert deleted == 2
    assert not old.exists()
    assert not boundary.exists()
    assert fresh.exists()


def test_cleanup_ignores_non_workdirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    # Same age as "old" but doesn't end in -work — must survive.
    keep = _make_workdir(temp_dir, "other-data", age_secs=72 * 3600)
    file_too = temp_dir / "stray.txt"
    file_too.write_text("not a dir")

    opts = TranscribeOptions(temp_path="temp")
    deleted = cleanup_old_workdirs(opts)

    assert deleted == 0
    assert keep.exists()
    assert file_too.exists()


def test_cleanup_skips_when_temp_dir_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opts = TranscribeOptions(temp_path="does-not-exist")
    # Should not raise; returns 0.
    assert cleanup_old_workdirs(opts) == 0


def test_cleanup_logs_old_workdir_with_age(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    old = _make_workdir(temp_dir, "video.gemini-deadbeef-work", age_secs=2 * 86400 + 3 * 3600)

    opts = TranscribeOptions(temp_path="temp")
    caplog.set_level(logging.INFO, logger="gemini_transcribe_wrapper.cli")
    cleanup_old_workdirs(opts)

    messages = [rec.getMessage() for rec in caplog.records]
    matched = [m for m in messages if "Cleaning up old work directory" in m]
    assert len(matched) == 1
    msg = matched[0]
    assert str(old) in msg
    assert "2d 3h" in msg  # age formatted with days + hours
    assert not old.exists()


def test_cleanup_continues_on_rmtree_failure(tmp_path, monkeypatch, caplog):
    """A single rmtree failure must not abort the rest of the cleanup."""
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    doomed = _make_workdir(temp_dir, "video.gemini-fail-work", age_secs=48 * 3600)
    survivor = _make_workdir(temp_dir, "video.gemini-ok-work", age_secs=48 * 3600)

    # Force shutil.rmtree to raise on the first call only.
    original_rmtree = __import__("shutil").rmtree
    calls = {"n": 0}

    def fake_rmtree(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated")
        return original_rmtree(path, *a, **kw)

    monkeypatch.setattr("gemini_transcribe_wrapper.cli.shutil.rmtree", fake_rmtree)

    opts = TranscribeOptions(temp_path="temp")
    caplog.set_level(logging.WARNING, logger="gemini_transcribe_wrapper.cli")
    deleted = cleanup_old_workdirs(opts)

    assert deleted == 1
    assert doomed.exists()  # rmtree failed → kept
    assert not survivor.exists()  # rmtree succeeded → removed
    assert any("simulated" in rec.getMessage() for rec in caplog.records)


def test_cleanup_uses_now_override_for_determinism(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    workdir = _make_workdir(temp_dir, "video.gemini-fixed-work", age_secs=12 * 3600)

    # Pretend "now" is 100 hours later → the workdir is now 112h old → cleaned.
    future_now = time.time() + 100 * 3600
    opts = TranscribeOptions(temp_path="temp")
    deleted = cleanup_old_workdirs(opts, now=future_now)
    assert deleted == 1
    assert not workdir.exists()


def test_cleanup_custom_max_age_secs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    workdir = _make_workdir(temp_dir, "video.gemini-fast-work", age_secs=60 * 60)

    opts = TranscribeOptions(temp_path="temp")
    # 30-min cutoff removes 1h-old workdir.
    deleted = cleanup_old_workdirs(opts, max_age_secs=30 * 60)
    assert deleted == 1
    assert not workdir.exists()
