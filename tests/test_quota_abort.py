"""Test that a 429 quota error aborts the batch (no further API calls).

Hitting the free-tier daily quota is not a per-file condition: every
subsequent file in the same run would also 429. The wrapper must raise
:class:`QuotaExceededError` from :func:`gemini_transcribe` after the first
quota hit so the CLI exits cleanly without burning more API calls.
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
    """First 429 must raise QuotaExceededError, not return a FAILED result."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _QuotaHitClient
    try:
        with pytest.raises(QuotaExceededError) as excinfo:
            api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
    finally:
        api.TranscribeClient = orig

    # The original 429 is preserved as the cause.
    assert "429" in str(excinfo.value.original)
    assert "quota" in str(excinfo.value.original).lower()


def test_quota_error_stops_at_first_file_in_glob_batch(tmp_path):
    """When the input expands to multiple files, only the first is attempted."""
    _make_audio(tmp_path, "a.mp4")
    _make_audio(tmp_path, "b.mp4")
    _make_audio(tmp_path, "c.mp4")

    orig = api.TranscribeClient
    client = _QuotaHitClient()
    api.TranscribeClient = lambda *a, **k: client
    try:
        with pytest.raises(QuotaExceededError):
            api.gemini_transcribe(
                str(tmp_path / "*.mp4"), force=True, gemini_api_key="fake",
            )
    finally:
        api.TranscribeClient = orig

    # First call hit quota, so we should not have started any other file.
    assert client.calls == 1


# --- CLI behavior ----------------------------------------------------------


def test_cli_exits_with_code_2_on_quota(monkeypatch, tmp_path):
    """Running the CLI on a 429-hitting client must exit with code 2."""
    from gemini_transcribe_wrapper import cli

    src = _make_audio(tmp_path)

    monkeypatch.setenv("GEMINI_API_KEY", "fake_key_for_testing")
    monkeypatch.setattr(api, "TranscribeClient", _QuotaHitClient)
    monkeypatch.setattr(sys, "argv", ["gtw", str(src)])

    rc = cli.main()
    assert rc == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
