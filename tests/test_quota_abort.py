"""Test batch behavior on 429 quota errors.

Two regimes (issue-003, spec §3):

- **Multi-key**: when the pool drains because every key 429'd, the
  wrapper raises :class:`QuotaExceededError` from :func:`gemini_transcribe`
  so the CLI exits with code 2 and no further API calls are burned.

- **Single-key**: a 429 means the file couldn't be processed right now
  but the next file (after the key recovers) is still worth attempting.
  The wrapper returns a result with status ``SKIPPED_QUOTA`` and continues
  with the remaining files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.api import QuotaExceededError


class _QuotaHitClient:
    """Raises a 429 on the first transcribe_chunk call."""

    def __init__(self, *args, **kwargs):
        self.api_logs: list[dict] = []
        self.calls = 0

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        self.calls += 1
        raise RuntimeError(
            "Error code: 429 - You exceeded your current quota, "
            "please check your plan and billing details. "
            "Quota exceeded for metric: generate_content_free_tier_input_token_count, "
            "limit: 10000, model: gemini-3.5-transcribe."
        )


def _make_audio(td: Path, name: str = "input.mp4") -> Path:
    src = td / name
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2",
            "-ar", "16000",
            "-ac", "1",
            str(src),
        ],
        capture_output=True,
        check=True,
    )
    return src


def test_quota_error_aborts_batch_with_quota_exceeded_error(tmp_path):
    """Multi-key 429 → :class:`QuotaExceededError` (batch abort).

    When the pool drains because every key 429'd, the wrapper raises
    ``QuotaExceededError`` so the CLI exits with code 2 and no further
    API calls are burned. Verified by configuring two quota-hit keys.
    """
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _QuotaHitClient
    try:
        with pytest.raises(QuotaExceededError) as excinfo:
            api.gemini_transcribe(
                str(src),
                force=True,
                gemini_api_keys=["fake_a", "fake_b"],
            )
    finally:
        api.TranscribeClient = orig

    # The original 429 is preserved as the cause.
    assert "429" in str(excinfo.value.original)
    assert "quota" in str(excinfo.value.original).lower()


def test_quota_error_stops_at_first_file_in_glob_batch(tmp_path):
    """Multi-key glob aborts after the first 429 — no other files attempted."""
    _make_audio(tmp_path, "a.mp4")
    _make_audio(tmp_path, "b.mp4")
    _make_audio(tmp_path, "c.mp4")

    orig = api.TranscribeClient
    client = _QuotaHitClient()
    api.TranscribeClient = lambda *a, **k: client
    try:
        with pytest.raises(QuotaExceededError):
            api.gemini_transcribe(
                str(tmp_path / "*.mp4"),
                force=True,
                gemini_api_keys=["fake_a", "fake_b"],
            )
    finally:
        api.TranscribeClient = orig

    # First call hit quota, so we should not have started any other file.
    assert client.calls == 1


# --- single-key SKIPPED_QUOTA (issue-003) ---------------------------------


def test_single_key_quota_returns_skipped_quota_result(tmp_path):
    """Single-key 429 → ``SKIPPED_QUOTA`` result, no raise, batch continues."""
    from gemini_transcribe_wrapper.models import TranscribeStatus

    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _QuotaHitClient
    try:
        batch = api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
        )
    finally:
        api.TranscribeClient = orig

    assert len(batch.results) == 1
    assert batch.results[0].status == TranscribeStatus.SKIPPED_QUOTA
    # Original 429 is preserved in the error string for forensics.
    assert "429" in (batch.results[0].error or "")


def test_single_key_quota_continues_glob_to_next_file(tmp_path):
    """Single-key 429 skips the file but tries the next file in the glob."""
    from gemini_transcribe_wrapper.models import TranscribeStatus

    _make_audio(tmp_path, "a.mp4")
    _make_audio(tmp_path, "b.mp4")
    _make_audio(tmp_path, "c.mp4")

    orig = api.TranscribeClient
    client = _QuotaHitClient()
    api.TranscribeClient = lambda *a, **k: client
    try:
        batch = api.gemini_transcribe(
            str(tmp_path / "*.mp4"),
            force=True,
            gemini_api_key="fake",
        )
    finally:
        api.TranscribeClient = orig

    # All three files were attempted (each got one quota hit).
    assert client.calls == 3
    assert len(batch.results) == 3
    assert all(r.status == TranscribeStatus.SKIPPED_QUOTA for r in batch.results)


# --- CLI behavior ----------------------------------------------------------


def test_cli_exits_with_code_2_on_quota(monkeypatch, tmp_path):
    """Multi-key 429 → CLI exits with code 2 (batch aborted)."""
    from gemini_transcribe_wrapper import cli

    src = _make_audio(tmp_path)

    # Multi-key: when the pool drains, the wrapper raises
    # QuotaExceededError and the CLI exits with code 2.
    monkeypatch.setenv("GEMINI_API_KEYS", "fake_a,fake_b")
    monkeypatch.setattr(api, "TranscribeClient", _QuotaHitClient)
    monkeypatch.setattr(sys, "argv", ["gtw", str(src)])

    rc = cli.main()
    assert rc == 2


def test_cli_exits_with_code_1_on_single_key_quota(monkeypatch, tmp_path):
    """Single-key 429 → CLI exits 1 (no output files produced).

    Distinct from the multi-key path (which exits 2). SKIPPED_QUOTA
    is reported per-file at WARNING level so the user sees the
    per-file status; the CLI exit code 1 reflects "no output was
    produced" rather than the multi-key "batch aborted" code 2.
    """
    from gemini_transcribe_wrapper import cli

    src = _make_audio(tmp_path)

    monkeypatch.setenv("GEMINI_API_KEY", "fake_key_for_testing")
    monkeypatch.setattr(api, "TranscribeClient", _QuotaHitClient)
    monkeypatch.setattr(sys, "argv", ["gtw", str(src)])

    rc = cli.main()
    # rc=1 ("no output files produced"), NOT rc=2 (multi-key abort).
    assert rc == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
