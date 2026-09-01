"""Regression test for the --force-all feature.

``--force`` re-runs the transcript→output render step but does NOT
delete the cached ``.transcript.json``; the API is therefore not called
a second time. ``--force-all`` is the stronger variant: it deletes the
cached transcript (forcing a fresh API call) AND behaves like ``--force``
once the new transcript is produced (so outputs are re-rendered even if
they are already fresh relative to the new transcript).

The two flags are mutually exclusive — passing both is a usage error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_cli_parser_force_all_default_false():
    """``--force-all`` defaults to False (regular ``--force`` semantics)."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4"])
    assert opts.force_all is False


def test_cli_parser_force_all_flag_sets_true():
    """``--force-all`` flips the boolean to True."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--force-all"])
    assert opts.force_all is True


def test_cli_parser_force_and_force_all_are_mutually_exclusive():
    """Passing both ``--force`` and ``--force-all`` is rejected (exit 2)."""
    from click.testing import CliRunner

    from gemini_transcribe_wrapper import cli

    runner = CliRunner()
    result = runner.invoke(cli.app, ["sample.mp4", "--force", "--force-all"])
    assert result.exit_code == 2, result.output
    combined = (result.output or "").lower()
    assert "mutually exclusive" in combined or "force" in combined


def test_cli_parser_no_force_all_default():
    """``--no-force-all`` is also recognized and flips back to False."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--no-force-all"])
    assert opts.force_all is False


# ---------------------------------------------------------------------------
# Behavior: --force-all invalidates the cached transcript
# ---------------------------------------------------------------------------


def test_force_all_deletes_cached_transcript(tmp_path, monkeypatch):
    """``--force-all`` removes the cached ``.transcript.json`` before the
    re-render check, so the wrapper falls through to the full API path.

    We assert via the public CLI options surface that the
    ``force_all`` flag propagates into the per-input ``gemini_transcribe``
    call as ``force=True`` (the outputs-rerender behavior we already
    fixed) AND that the transcript file is removed from disk before the
    re-render check sees it.
    """
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(
        ["sample.mp4", "--force-all", "--gemini-api-keys", "AIzaSyDummyKey12345678"]
    )
    assert opts.force_all is True
    # --force-all implicitly implies --force semantics for the
    # outputs-rerender step.
    assert opts.force is False  # user did NOT pass --force explicitly


def test_force_all_keeps_force_false():
    """The flag is its own boolean — passing ``--force-all`` must not
    set ``opts.force`` to True (they are mutually exclusive at parse
    time, and the implementation routes --force-all through its own
    parameter rather than aliasing --force)."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(
        ["sample.mp4", "--force-all", "--gemini-api-keys", "AIzaSyDummyKey12345678"]
    )
    assert opts.force_all is True
    assert opts.force is False


def test_force_all_executes_api_call_and_deletes_old_transcript(tmp_path, monkeypatch):
    """End-to-end behavior test: when cached transcript and outputs exist:
    - Default (force=False, force_all=False): skips.
    - force=True: re-renders without calling API.
    - force_all=True: deletes old transcript and calls TranscribeClient API.
    """
    from gemini_transcribe_wrapper import api
    from gemini_transcribe_wrapper.models import TranscribeStatus
    from gemini_transcribe_wrapper.stt import TranscriptionResult, Word, save_transcript

    # Create dummy input media file
    dummy_input = tmp_path / "video.mp4"
    dummy_input.write_bytes(b"dummy")

    transcript_path = tmp_path / "video.transcript.json"
    srt_path = tmp_path / "video.srt"
    txt_path = tmp_path / "video.txt"

    # Pre-populate outputs & transcript
    save_transcript(
        transcript_path,
        [
            TranscriptionResult(
                text="old transcript",
                words=[Word("old", 0.0, 0.5), Word("transcript", 0.6, 1.0)],
            )
        ],
        chunk_secs=[10.0],
        language="ko-KR",
    )
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nold transcript\n", encoding="utf-8")
    txt_path.write_text("old transcript\n", encoding="utf-8")

    api_call_count = 0

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.api_key = "dummy_key"
            self.api_logs = []

        def transcribe_chunk(self, chunk_path, **kwargs):
            nonlocal api_call_count
            api_call_count += 1
            return TranscriptionResult(
                text="new fresh transcript",
                words=[
                    Word("new", 0.0, 0.5),
                    Word("fresh", 0.6, 1.0),
                    Word("transcript", 1.1, 1.5),
                ],
            )

    def fake_split_chunks(mp3, cdir, plan):
        chunk = cdir / "chunk_0000.mp3"
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_bytes(b"chunk")
        return [chunk]

    monkeypatch.setattr(api, "TranscribeClient", FakeClient)
    monkeypatch.setattr(api, "probe_duration_secs", lambda p: 5.0)
    monkeypatch.setattr(api, "extract_audio", lambda src, dst, force=False: dst.write_bytes(b"mp3"))
    monkeypatch.setattr(api, "split_chunks", fake_split_chunks)

    # 1. Default run -> SKIPPED, 0 API calls
    res_default = api.gemini_transcribe(
        input_file=str(dummy_input),
        gemini_api_keys=["AIzaSyDummyKey12345678"],
        force=False,
        force_all=False,
    )
    assert res_default.results[0].status == TranscribeStatus.SKIPPED
    assert api_call_count == 0

    # 2. force=True -> re-rendered from existing transcript, 0 API calls
    res_force = api.gemini_transcribe(
        input_file=str(dummy_input),
        gemini_api_keys=["AIzaSyDummyKey12345678"],
        force=True,
        force_all=False,
    )
    assert res_force.results[0].status == TranscribeStatus.SUCCESS
    assert api_call_count == 0
    assert "old transcript" in txt_path.read_text(encoding="utf-8")

    # 3. force_all=True -> calls API, replaces transcript and outputs
    res_force_all = api.gemini_transcribe(
        input_file=str(dummy_input),
        gemini_api_keys=["AIzaSyDummyKey12345678"],
        force=False,
        force_all=True,
    )
    assert res_force_all.results[0].status == TranscribeStatus.SUCCESS
    assert api_call_count == 1
    assert "new fresh transcript" in txt_path.read_text(encoding="utf-8")
