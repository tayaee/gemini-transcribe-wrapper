"""Regression test for the --force re-render bug.

Bug: ``--force`` correctly bypasses the "outputs exist → skip" check at
the outer level, but ``_render_from_transcript`` then runs and reports
each output file as "Unchanged (already up to date)" because the SRT
file is newer than the transcript JSON that produced it. The net
result is no files are actually re-written, defeating --force.

The fix: ``_render_from_transcript`` must accept a ``force`` flag and
skip the staleness check when set, so the atomic ``os.replace`` runs
unconditionally for every enabled output.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.models import TranscribeInput
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word, save_transcript


def _make_transcript_result() -> TranscriptionResult:
    return TranscriptionResult(
        text="hello world",
        words=[Word("hello", 0.0, 1.0), Word("world", 1.1, 2.0)],
    )


def _write_transcript(transcript_path: Path) -> None:
    save_transcript(
        transcript_path,
        [_make_transcript_result()],
        chunk_secs=[10.0],
        language="ko-KR",
    )


def test_render_from_transcript_respects_force(tmp_path):
    """When ``force=True``, all enabled outputs must be regenerated even
    if they are already newer than the transcript (which is the normal
    state of a previously-rendered file pair).
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_stem = "speech"

    transcript_path = out_dir / f"{out_stem}.transcript.json"
    srt_target = out_dir / f"{out_stem}.srt"
    txt_target = out_dir / f"{out_stem}.txt"

    _write_transcript(transcript_path)

    # First render (no force): produces the SRT/TXT files, both newer
    # than the transcript.
    echo = TranscribeInput(
        input_file=str(tmp_path / "input.mp4"),
        output_dir=str(out_dir),
        output_base=out_stem,
        model="gemini-3.5-transcribe",
    )
    api._render_from_transcript(
        echo=echo,
        out_dir=out_dir,
        out_stem=out_stem,
        transcript_path=transcript_path,
        srt_enabled=True,
        txt_enabled=True,
        diarized_enabled=False,
        metadata_enabled=False,
        srt_target=srt_target,
        txt_target=txt_target,
        diarized_target=out_dir / f"{out_stem}.diarized.srt",
        metadata_target=out_dir / f"{out_stem}.metadata.json",
        transcript_enabled=True,
        line_interval_secs=1.0,
        paragraph_interval_secs=2.5,
        speakers=None,
    )

    assert srt_target.exists()
    assert txt_target.exists()

    # Snapshot the original SRT/TXT content + mtimes.
    original_srt = srt_target.read_text(encoding="utf-8")
    original_txt = txt_target.read_text(encoding="utf-8")
    srt_mtime_before = srt_target.stat().st_mtime
    txt_mtime_before = txt_target.stat().st_mtime

    # Make sure mtimes can change (some FSes have 1s resolution).
    time.sleep(0.05)

    # Second render WITH force=True: must regenerate both files.
    api._render_from_transcript(
        echo=echo,
        out_dir=out_dir,
        out_stem=out_stem,
        transcript_path=transcript_path,
        srt_enabled=True,
        txt_enabled=True,
        diarized_enabled=False,
        metadata_enabled=False,
        srt_target=srt_target,
        txt_target=txt_target,
        diarized_target=out_dir / f"{out_stem}.diarized.srt",
        metadata_target=out_dir / f"{out_stem}.metadata.json",
        transcript_enabled=True,
        line_interval_secs=1.0,
        paragraph_interval_secs=2.5,
        speakers=None,
        force=True,  # the fix
    )

    # Both files must be re-written (atomic os.replace → fresh mtime).
    assert srt_target.stat().st_mtime > srt_mtime_before, (
        "SRT was not regenerated under --force"
    )
    assert txt_target.stat().st_mtime > txt_mtime_before, (
        "TXT was not regenerated under --force"
    )
    # Content matches the transcript (deterministic).
    assert srt_target.read_text(encoding="utf-8") == original_srt
    assert txt_target.read_text(encoding="utf-8") == original_txt


def test_render_from_transcript_without_force_skips_when_fresh(tmp_path):
    """Without ``force``, the second render reports "Unchanged" for
    outputs that are already newer than the transcript — the existing
    behavior. This is the regression guard for the fix.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_stem = "speech"

    transcript_path = out_dir / f"{out_stem}.transcript.json"
    srt_target = out_dir / f"{out_stem}.srt"
    txt_target = out_dir / f"{out_stem}.txt"

    _write_transcript(transcript_path)
    echo = TranscribeInput(
        input_file=str(tmp_path / "input.mp4"),
        output_dir=str(out_dir),
        output_base=out_stem,
        model="gemini-3.5-transcribe",
    )
    common = {
        "echo": echo,
        "out_dir": out_dir,
        "out_stem": out_stem,
        "transcript_path": transcript_path,
        "srt_enabled": True,
        "txt_enabled": True,
        "diarized_enabled": False,
        "metadata_enabled": False,
        "srt_target": srt_target,
        "txt_target": txt_target,
        "diarized_target": out_dir / f"{out_stem}.diarized.srt",
        "metadata_target": out_dir / f"{out_stem}.metadata.json",
        "transcript_enabled": True,
        "line_interval_secs": 1.0,
        "paragraph_interval_secs": 2.5,
        "speakers": None,
    }
    api._render_from_transcript(**common)
    mtime_before = srt_target.stat().st_mtime

    time.sleep(0.05)
    # No force → fresh outputs are reported Unchanged and not re-written.
    api._render_from_transcript(**common)
    assert srt_target.stat().st_mtime == mtime_before, (
        "SRT was unexpectedly rewritten without --force"
    )
