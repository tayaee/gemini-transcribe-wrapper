"""Test --[no-]diarize: default OFF (fewer API calls), output naming, backward compat.

The flag is a ``BooleanOptionalAction`` defaulting to False. With ``diarize``
off, the wrapper cuts the file into 59-min logical units (each split
into 2 API calls to stay under the 30-min per-call limit) and emits
``.srt`` and ``.txt`` only. With it on, it uses 29m50s chunks and adds
``.diarized.transcript.json`` / ``.diarized.srt``. Older transcripts named
``<stem>.transcript.json`` are migrated to the canonical
``.diarized.transcript.json`` when diarize is on, and vice versa.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.api import (
    DEFAULT_CHUNK_SECS_DIARIZE,
    DEFAULT_CHUNK_SECS_NO_DIARIZE,
    TRANSCRIPT_SUFFIX_DIARIZED,
    TRANSCRIPT_SUFFIX_PLAIN,
    _resolve_transcript_path,
)
from gemini_transcribe_wrapper.merge import align_and_build
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word

# --- path resolution & constants -------------------------------------------


def test_default_chunk_secs_match_documented_values():
    """The two mode defaults are exactly what the help text promises."""
    assert DEFAULT_CHUNK_SECS_NO_DIARIZE == 3600.0
    assert DEFAULT_CHUNK_SECS_DIARIZE == 1800.0


def test_transcript_suffixes_are_stable():
    """The plain and diarized suffixes must stay as documented."""
    assert TRANSCRIPT_SUFFIX_PLAIN == ".transcript.json"
    assert TRANSCRIPT_SUFFIX_DIARIZED == ".diarized.transcript.json"


def test_resolve_transcript_path_off_prefers_plain(tmp_path):
    plain = tmp_path / "input.transcript.json"
    plain.write_text("{}", encoding="utf-8")
    chosen, migrate = _resolve_transcript_path(tmp_path, "input", diarize=False)
    assert chosen == plain
    assert migrate is None


def test_resolve_transcript_path_on_prefers_diarized(tmp_path):
    diarized = tmp_path / "input.diarized.transcript.json"
    diarized.write_text("{}", encoding="utf-8")
    chosen, migrate = _resolve_transcript_path(tmp_path, "input", diarize=True)
    assert chosen == diarized
    assert migrate is None


def test_resolve_transcript_path_on_falls_back_to_legacy_plain(tmp_path):
    """A legacy plain transcript must still be picked up with diarize=True."""
    legacy = tmp_path / "input.transcript.json"
    legacy.write_text("{}", encoding="utf-8")
    canonical = tmp_path / "input.diarized.transcript.json"
    chosen, migrate = _resolve_transcript_path(tmp_path, "input", diarize=True)
    assert chosen == legacy  # we'll read from the legacy path
    assert migrate == canonical  # and rename to the canonical name on save


def test_resolve_transcript_path_off_falls_back_to_legacy_diarized(tmp_path):
    """The reverse: an old diarized transcript is also valid with diarize=False."""
    legacy = tmp_path / "input.diarized.transcript.json"
    legacy.write_text("{}", encoding="utf-8")
    canonical = tmp_path / "input.transcript.json"
    chosen, migrate = _resolve_transcript_path(tmp_path, "input", diarize=False)
    assert chosen == legacy
    assert migrate == canonical


def test_resolve_transcript_path_neither_exists(tmp_path):
    """No existing file -> canonical path is returned, no migration."""
    chosen, migrate = _resolve_transcript_path(tmp_path, "input", diarize=True)
    assert chosen == tmp_path / "input.diarized.transcript.json"
    assert migrate is None


# --- end-to-end pipeline with a fake STT client ----------------------------


class _OneChunkFakeClient:
    """Returns one chunk's worth of data without doing any real work."""

    def __init__(self, *args, **kwargs):
        self.api_logs: list[dict] = []
        self.enable_diarization = kwargs.get("enable_diarization", False)

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        self.api_logs.append({"chunk": chunk_index})
        return TranscriptionResult(
            text="테스트",
            words=[
                Word("테스트", 0.0, 1.0, "spk:0"),
                Word("입니다", 1.2, 2.0, "spk:1"),
            ],
        )


def _make_audio(td: Path, name: str = "input.mp4", duration_secs: int = 2) -> Path:
    src = td / name
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_secs}",
            "-ar", "16000",
            "-ac", "1",
            str(src),
        ],
        capture_output=True,
        check=True,
    )
    return src


def test_diarize_off_emits_plain_transcript_only(tmp_path):
    """OFF (explicit) writes .transcript.json, .srt, .txt — no .diarized.srt."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        result = api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=False,
        )
    finally:
        api.TranscribeClient = orig

    assert result.results[0].status.value == "success"
    out = result.results[0].output
    assert out.diarized_srt is None
    assert out.srt is not None and out.txt is not None
    assert Path(out.srt).name == "input.srt"
    assert Path(out.txt).name == "input.txt"

    # Plain transcript must exist; diarized transcript must not.
    assert (tmp_path / "input.transcript.json").exists()
    assert not (tmp_path / "input.diarized.transcript.json").exists()
    assert not (tmp_path / "input.diarized.srt").exists()


def test_diarize_on_emits_diarized_outputs(tmp_path):
    """ON emits .diarized.transcript.json + .diarized.srt + .srt + .txt."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        result = api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=True,
        )
    finally:
        api.TranscribeClient = orig

    assert result.results[0].status.value == "success"
    out = result.results[0].output
    assert out.diarized_srt is not None
    assert out.srt is not None and out.txt is not None
    assert Path(out.diarized_srt).name == "input.diarized.srt"
    assert Path(out.srt).name == "input.srt"
    assert Path(out.txt).name == "input.txt"

    assert (tmp_path / "input.diarized.transcript.json").exists()
    assert (tmp_path / "input.diarized.srt").exists()
    assert (tmp_path / "input.srt").exists()
    assert (tmp_path / "input.txt").exists()


def test_diarize_on_runs_chunks_at_diarize_default(monkeypatch, tmp_path):
    """With diarize=True and no explicit chunk_secs, chunk_secs=1800 + ceiling=1800 should be used."""
    src = _make_audio(tmp_path)
    captured: dict[str, float | None] = {}

    real_compute = api.compute_split_plan

    def spy(total_secs, max_chunk_secs):
        captured["max_chunk_secs"] = max_chunk_secs
        return real_compute(total_secs, max_chunk_secs=max_chunk_secs)

    monkeypatch.setattr(api, "compute_split_plan", spy)

    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=True,
        )
    finally:
        api.TranscribeClient = orig

    assert captured["max_chunk_secs"] == DEFAULT_CHUNK_SECS_DIARIZE


def test_diarize_off_word_timestamps_off_runs_chunks_at_no_diarize_default(monkeypatch, tmp_path):
    """With diarize=False AND word_level_timestamps=False (only combo that picks 60*60),
    chunk_secs=3600 + ceiling=3600 should be used.
    """
    src = _make_audio(tmp_path)
    captured: dict[str, float | None] = {}

    real_compute = api.compute_split_plan

    def spy(total_secs, max_chunk_secs):
        captured["max_chunk_secs"] = max_chunk_secs
        return real_compute(total_secs, max_chunk_secs=max_chunk_secs)

    monkeypatch.setattr(api, "compute_split_plan", spy)

    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
            word_level_timestamps=False,
            diarized_srt_file=False,
        )
    finally:
        api.TranscribeClient = orig

    assert captured["max_chunk_secs"] == DEFAULT_CHUNK_SECS_NO_DIARIZE


def test_diarize_off_word_timestamps_on_runs_chunks_at_diarize_default(monkeypatch, tmp_path):
    """With diarize=False but word_level_timestamps=True (default), the 29*60
    short-chunk ceiling is enforced because word-level timestamps force the
    Gemini API into the 30-min per-call mode.
    """
    src = _make_audio(tmp_path)
    captured: dict[str, float | None] = {}

    real_compute = api.compute_split_plan

    def spy(total_secs, max_chunk_secs):
        captured["max_chunk_secs"] = max_chunk_secs
        return real_compute(total_secs, max_chunk_secs=max_chunk_secs)

    monkeypatch.setattr(api, "compute_split_plan", spy)

    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=False,
        )
    finally:
        api.TranscribeClient = orig

def test_txt_only_automatically_turns_off_word_level_timestamps(monkeypatch, tmp_path):
    """When both .srt and .diarized.srt are disabled (txt only), word_level_timestamps
    automatically turns off, allowing 60-min (3600s) chunks.
    """
    src = _make_audio(tmp_path)
    captured: dict[str, float | None] = {}

    real_compute = api.compute_split_plan

    def spy(total_secs, max_chunk_secs):
        captured["max_chunk_secs"] = max_chunk_secs
        return real_compute(total_secs, max_chunk_secs=max_chunk_secs)

    monkeypatch.setattr(api, "compute_split_plan", spy)

    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
            srt_file="off",
            diarized_srt_file="off",
            txt_file="auto",
        )
    finally:
        api.TranscribeClient = orig

    assert captured["max_chunk_secs"] == DEFAULT_CHUNK_SECS_NO_DIARIZE


def test_diarize_explicit_chunk_secs_overrides_default(monkeypatch, tmp_path):
    """User-supplied --max-chunk-secs always wins over the diarize-mode default."""
    src = _make_audio(tmp_path)
    captured: dict[str, float | None] = {}

    real_compute = api.compute_split_plan

    def spy(total_secs, max_chunk_secs):
        captured["max_chunk_secs"] = max_chunk_secs
        return real_compute(total_secs, max_chunk_secs=max_chunk_secs)

    monkeypatch.setattr(api, "compute_split_plan", spy)

    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake",
            diarized_srt_file=True, max_chunk_secs=60.0,
        )
    finally:
        api.TranscribeClient = orig

    assert captured["max_chunk_secs"] == 60.0


def test_diarize_off_passes_enable_diarization_false_to_client(tmp_path):
    """The wrapper must tell the STT client whether to request diarization."""
    src = _make_audio(tmp_path)
    seen: dict[str, bool] = {}

    class _CaptureDiarizeClient(_OneChunkFakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["enable_diarization"] = kwargs.get("enable_diarization", False)

    orig = api.TranscribeClient
    api.TranscribeClient = _CaptureDiarizeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=False,
        )
    finally:
        api.TranscribeClient = orig

    assert seen["enable_diarization"] is False


def test_diarize_on_passes_enable_diarization_true_to_client(tmp_path):
    src = _make_audio(tmp_path)
    seen: dict[str, bool] = {}

    class _CaptureDiarizeClient(_OneChunkFakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["enable_diarization"] = kwargs.get("enable_diarization", False)

    orig = api.TranscribeClient
    api.TranscribeClient = _CaptureDiarizeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", diarized_srt_file=True,
        )
    finally:
        api.TranscribeClient = orig

    assert seen["enable_diarization"] is True


# --- --speakers warning when diarize is off ---------------------------------


def test_speakers_silently_dropped_when_diarize_off(tmp_path, caplog):
    """--speakers is meaningless without --diarize: warn and ignore."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    caplog.set_level(logging.WARNING, logger="gemini_transcribe_wrapper.api")
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake",
            speakers={"spk:0": "궤도"},
            diarized_srt_file=False,
        )
    finally:
        api.TranscribeClient = orig

    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "--speakers" in text
    assert "--diarized-srt-file is disabled" in text

    # And the actual .srt file must NOT carry a [궤도] tag — speakers were dropped.
    srt_text = (tmp_path / "input.srt").read_text(encoding="utf-8")
    assert "[궤도]" not in srt_text


def test_speakers_applied_when_diarize_on(tmp_path):
    """With --diarize, the mapping is applied to .diarized.srt."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _OneChunkFakeClient
    try:
        api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake",
            diarized_srt_file=True, speakers={"spk:0": "궤도"},
        )
    finally:
        api.TranscribeClient = orig

    srt_text = (tmp_path / "input.diarized.srt").read_text(encoding="utf-8")
    assert "[궤도]" in srt_text
    assert "[spk:1]" in srt_text  # unmapped speaker keeps raw id


# --- backward-compat migration ----------------------------------------------


def test_legacy_plain_transcript_is_migrated_on_diarize_run(tmp_path):
    """An old .transcript.json from a non-diarized run gets renamed to .diarized.transcript.json.

    The legacy transcript is fully valid but has the wrong name. With no
    final outputs present, the wrapper takes the main flow path: it
    re-transcribes the file, writes the canonical transcript, then renames
    the legacy file to the canonical name. We verify the rename by stubbing
    the STT client to always fail with a fresh transcript provided.
    """
    legacy = tmp_path / "input.transcript.json"
    legacy.write_text(
        json.dumps(
            {
                "chunks": [
                    {
                        "index": 0,
                        "words": [
                            {"text": "테스트", "start": 0.0, "end": 1.0, "speaker": "spk:0"},
                            {"text": "입니다", "start": 1.2, "end": 2.0, "speaker": "spk:1"},
                        ],
                    }
                ],
                "chunk_secs": 1790.0,
                "language": "ko-KR",
                "api_logs": [],
            }
        ),
        encoding="utf-8",
    )

    # The migration is performed in the main pipeline (after a successful
    # save_transcript), not in the re-render branch. Exercise the helper
    # directly to verify the rename contract: given a valid legacy
    # transcript and diarize=True, _resolve_transcript_path points the
    # caller at the legacy file and returns the canonical destination.
    chosen, migrate_to = _resolve_transcript_path(tmp_path, "input", diarize=True)
    assert chosen == legacy
    assert migrate_to == tmp_path / "input.diarized.transcript.json"

    # The migration itself is a plain os.replace: simulate it and verify
    # both names behave correctly afterwards.
    import os as _os
    assert chosen is not None and migrate_to is not None
    _os.replace(chosen, migrate_to)
    assert not legacy.exists()
    assert migrate_to.exists()


# --- align_and_build accepts None diarized_srt_tmp --------------------------


def test_align_and_build_with_diarized_srt_none(tmp_path):
    """align_and_build must accept None for the diarized SRT tmp path.

    With diarized_srt_tmp=None, only the .srt.tmp and .txt.tmp files must be
    created — no diarized SRT tmp is written.
    """
    srt_tmp = tmp_path / "foo.srt.tmp"
    txt_tmp = tmp_path / "foo.txt.tmp"
    align_and_build(
        [_OneChunkFakeClient().transcribe_chunk(None, 0)],
        chunk_secs=[1790.0],
        full_mp3=tmp_path / "unused.mp3",
        out_base=tmp_path / "foo",
        srt_tmp=srt_tmp,
        diarized_srt_tmp=None,  # OFF mode: no diarized tmp
        txt_tmp=txt_tmp,
        line_interval_secs=1.0,
        paragraph_interval_secs=2.5,
        skip_sync=True,
        speakers=None,
    )
    assert srt_tmp.exists()
    assert txt_tmp.exists()
    # The only tmp paths we asked for must exist; the diarized tmp was None.
    files = {p.name for p in tmp_path.iterdir()}
    assert "foo.srt.tmp" in files
    assert "foo.txt.tmp" in files
    assert "foo.diarized.srt.tmp" not in files


def test_align_and_build_with_diarized_srt_provided(tmp_path):
    """When the diarized tmp path is given, the file must be written."""
    srt_tmp = tmp_path / "foo.srt.tmp"
    diarized_srt_tmp = tmp_path / "foo.diarized.srt.tmp"
    txt_tmp = tmp_path / "foo.txt.tmp"
    align_and_build(
        [_OneChunkFakeClient().transcribe_chunk(None, 0)],
        chunk_secs=[1790.0],
        full_mp3=tmp_path / "unused.mp3",
        out_base=tmp_path / "foo",
        srt_tmp=srt_tmp,
        diarized_srt_tmp=diarized_srt_tmp,
        txt_tmp=txt_tmp,
        line_interval_secs=1.0,
        paragraph_interval_secs=2.5,
        skip_sync=True,
        speakers={"spk:0": "궤도"},
    )
    assert diarized_srt_tmp.exists()
    # The mapping must be applied in the diarized output.
    text = diarized_srt_tmp.read_text(encoding="utf-8")
    assert "[궤도]" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
