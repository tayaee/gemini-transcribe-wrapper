"""Unit tests for --srt-file, --txt-file, and --diarized-srt-file CLI options and API parameters."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import api
from gemini_transcribe_wrapper.cli import (
    TranscribeOptions,
    build_options,
    format_cli_command,
)
from gemini_transcribe_wrapper.merge import _resolve_output_target
from gemini_transcribe_wrapper.stt import TranscriptionResult, Word


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.api_logs: list[dict] = []
        self.enable_diarization = kwargs.get("enable_diarization", False)

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        self.api_logs.append({"chunk": chunk_index})
        return TranscriptionResult(
            text="테스트 자막입니다.",
            words=[
                Word("테스트", 0.0, 1.0, "spk:0"),
                Word("자막입니다.", 1.2, 2.0, "spk:1"),
            ],
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


# --- CLI Option Parsing ---------------------------------------------------


def test_cli_options_defaults():
    """When omitted, output file options default to None."""
    opts = build_options(["input.mp4"])
    assert opts.srt_file is None
    assert opts.txt_file is None
    assert opts.transcript_json_file is None
    assert opts.audit_jsonl_file is None
    assert opts.diarized_srt_file is None
    assert opts.metadata_json_file is None


def test_cli_options_custom_paths():
    """Explicit paths are preserved in TranscribeOptions."""
    opts = build_options([
        "--srt-file", "custom/sub.srt",
        "--txt-file", "custom/text.txt",
        "--transcript-json-file", "custom/t.json",
        "--audit-jsonl-file", "custom/audit.jsonl",
        "--diarized-srt-file", "custom/diarized.srt",
        "--metadata-json-file", "custom/meta.json",
        "input.mp4",
    ])
    assert opts.srt_file == "custom/sub.srt"
    assert opts.txt_file == "custom/text.txt"
    assert opts.transcript_json_file == "custom/t.json"
    assert opts.audit_jsonl_file == "custom/audit.jsonl"
    assert opts.diarized_srt_file == "custom/diarized.srt"
    assert opts.metadata_json_file == "custom/meta.json"


def test_cli_options_auto_and_off_tokens():
    """'auto' and 'off' are parsed as option values."""
    opts = build_options([
        "--srt-file", "off",
        "--txt-file", "off",
        "--transcript-json-file", "off",
        "--audit-jsonl-file", "off",
        "--diarized-srt-file", "auto",
        "--metadata-json-file", "auto",
        "input.mp4",
    ])
    assert opts.srt_file == "off"
    assert opts.txt_file == "off"
    assert opts.transcript_json_file == "off"
    assert opts.audit_jsonl_file == "off"
    assert opts.diarized_srt_file == "auto"
    assert opts.metadata_json_file == "auto"


# --- format_cli_command ----------------------------------------------------


def test_format_cli_command_includes_options():
    opts = TranscribeOptions(
        path=["input.mp4"],
        srt_file="out.srt",
        txt_file="off",
        transcript_json_file="off",
        audit_jsonl_file="audit.jsonl",
        diarized_srt_file="auto",
        metadata_json_file="out.metadata.json",
    )
    cmd = format_cli_command("gtw", opts)
    assert "--srt-file out.srt" in cmd
    assert "--txt-file off" in cmd
    assert "--transcript-json-file off" in cmd
    assert "--audit-jsonl-file audit.jsonl" in cmd
    assert "--diarized-srt-file auto" in cmd
    assert "--metadata-json-file out.metadata.json" in cmd


def test_format_cli_command_omits_when_none():
    opts = TranscribeOptions(path=["input.mp4"])
    cmd = format_cli_command("gtw", opts)
    assert "--srt-file" not in cmd
    assert "--txt-file" not in cmd
    assert "--transcript-json-file" not in cmd
    assert "--audit-jsonl-file" not in cmd
    assert "--diarized-srt-file" not in cmd
    assert "--metadata-json-file" not in cmd


# --- Target Resolution (_resolve_output_target) ---------------------------


def test_resolve_output_target_none():
    default_p = Path("/tmp/test.srt")
    assert _resolve_output_target(None, default_p, default_enabled=True) == (True, default_p)
    assert _resolve_output_target(None, default_p, default_enabled=False) == (False, default_p)


def test_resolve_output_target_booleans():
    default_p = Path("/tmp/test.srt")
    assert _resolve_output_target(True, default_p, default_enabled=False) == (True, default_p)
    assert _resolve_output_target(False, default_p, default_enabled=True) == (False, default_p)


def test_resolve_output_target_auto_string():
    default_p = Path("/tmp/test.srt")
    assert _resolve_output_target("auto", default_p, default_enabled=False) == (True, default_p)
    assert _resolve_output_target("AUTO", default_p, default_enabled=True) == (True, default_p)


def test_resolve_output_target_disabled_strings():
    default_p = Path("/tmp/test.srt")
    for token in ["", "no", "NO", " off ", "false", "None", "0"]:
        assert _resolve_output_target(token, default_p, default_enabled=True) == (False, default_p)


def test_resolve_output_target_custom_path():
    default_p = Path("/tmp/test.srt")
    custom_p = Path("/custom/my.srt")
    assert _resolve_output_target("custom/my.srt", default_p, default_enabled=False) == (
        True,
        Path("custom/my.srt"),
    )
    assert _resolve_output_target(custom_p, default_p, default_enabled=False) == (
        True,
        custom_p,
    )


# --- End-to-End API Behavior ----------------------------------------------


def test_api_defaults_create_srt_txt_and_diarized(tmp_path):
    """By default, .srt, .txt, and .diarized.srt are created."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
    finally:
        api.TranscribeClient = orig

    assert (tmp_path / "input.srt").exists()
    assert (tmp_path / "input.txt").exists()
    assert (tmp_path / "input.diarized.srt").exists()
    assert res.results[0].output.srt is not None
    assert res.results[0].output.txt is not None
    assert res.results[0].output.diarized_srt is not None


def test_api_srt_file_no_suppresses_srt(tmp_path):
    """Passing srt_file='no' or '' or False prevents .srt creation."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", srt_file="no"
        )
    finally:
        api.TranscribeClient = orig

    assert not (tmp_path / "input.srt").exists()
    assert (tmp_path / "input.txt").exists()
    assert res.results[0].output.srt is None
    assert res.results[0].output.txt is not None


def test_api_txt_file_empty_string_suppresses_txt(tmp_path):
    """Passing txt_file='' prevents .txt creation."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", txt_file=""
        )
    finally:
        api.TranscribeClient = orig

    assert (tmp_path / "input.srt").exists()
    assert not (tmp_path / "input.txt").exists()
    assert res.results[0].output.srt is not None
    assert res.results[0].output.txt is None


def test_api_custom_output_paths(tmp_path):
    """Explicit paths are respected for srt_file, txt_file, diarized_srt_file, metadata_json_file."""
    src = _make_audio(tmp_path)
    custom_srt = tmp_path / "custom_subtitles.srt"
    custom_txt = tmp_path / "custom_text.txt"
    custom_diarized = tmp_path / "custom_diarized.srt"
    custom_meta = tmp_path / "custom_metadata.json"

    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
            srt_file=custom_srt,
            txt_file=str(custom_txt),
            diarized_srt_file=custom_diarized,
            metadata_json_file=custom_meta,
        )
    finally:
        api.TranscribeClient = orig

    assert custom_srt.exists()
    assert custom_txt.exists()
    assert custom_diarized.exists()
    assert custom_meta.exists()
    assert not (tmp_path / "input.srt").exists()
    assert not (tmp_path / "input.txt").exists()
    assert not (tmp_path / "input.diarized.srt").exists()
    assert not (tmp_path / "input.metadata.json").exists()

    out = res.results[0].output
    assert out.srt == str(custom_srt)
    assert out.txt == str(custom_txt)
    assert out.diarized_srt == str(custom_diarized)
    assert out.metadata_json == str(custom_meta)


def test_api_metadata_json_default_off(tmp_path):
    """By default, .metadata.json is not generated unless metadata_json_file is set."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(str(src), force=True, gemini_api_key="fake")
    finally:
        api.TranscribeClient = orig

    assert not (tmp_path / "input.metadata.json").exists()
    assert res.results[0].output.metadata_json is None


def test_api_auto_enables_diarized_and_metadata(tmp_path):
    """Passing 'auto' for diarized_srt_file and metadata_json_file generates default-named outputs."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
            diarized_srt_file="auto",
            metadata_json_file="auto",
        )
    finally:
        api.TranscribeClient = orig

    assert (tmp_path / "input.diarized.srt").exists()
    assert (tmp_path / "input.metadata.json").exists()
    assert res.results[0].output.diarized_srt == str(tmp_path / "input.diarized.srt")
    assert res.results[0].output.metadata_json == str(tmp_path / "input.metadata.json")


def test_api_transcript_json_file_off(tmp_path):
    """Passing transcript_json_file='off' prevents .transcript.json from being kept."""
    src = _make_audio(tmp_path)
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(src),
            force=True,
            gemini_api_key="fake",
            transcript_json_file="off",
        )
    finally:
        api.TranscribeClient = orig

    assert not (tmp_path / "input.transcript.json").exists()
    assert res.results[0].output.transcript_json is None


def test_multi_file_batch_with_auto_allowed(tmp_path):
    """Multi-file batches allow 'auto' or 'off' without error."""
    _make_audio(tmp_path, "input1.mp4")
    _make_audio(tmp_path, "input2.mp4")
    orig = api.TranscribeClient
    api.TranscribeClient = _FakeClient
    try:
        res = api.gemini_transcribe(
            str(tmp_path / "*.mp4"),
            force=True,
            gemini_api_key="fake",
            diarized_srt_file="auto",
            srt_file="auto",
            txt_file="off",
        )
    finally:
        api.TranscribeClient = orig

    assert len(res.results) == 2
    assert (tmp_path / "input1.diarized.srt").exists()
    assert (tmp_path / "input2.diarized.srt").exists()
    assert (tmp_path / "input1.srt").exists()
    assert (tmp_path / "input2.srt").exists()
    assert not (tmp_path / "input1.txt").exists()
    assert not (tmp_path / "input2.txt").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
