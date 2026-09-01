"""Unit tests for JSONL audit logging to <os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl."""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt


def test_sanitize_audit_token_lowercases_and_strips():
    assert stt._sanitize_audit_token("MyHost") == "myhost"
    assert stt._sanitize_audit_token("JANE.Doe") == "jane.doe"
    assert stt._sanitize_audit_token("user name") == "user_name"
    assert stt._sanitize_audit_token("../etc/passwd") == "etc_passwd"
    assert stt._sanitize_audit_token("") == "unknown"
    assert stt._sanitize_audit_token("___") == "unknown"


def test_get_computer_shortname_uses_short_hostname(monkeypatch):
    monkeypatch.setattr(stt.os, "getenv", lambda *_a, **_k: None)
    monkeypatch.setattr(stt.socket, "gethostname", lambda: "myhost.example.com")
    assert stt._get_computer_shortname() == "myhost"


def test_get_computer_shortname_prefers_computername_env(monkeypatch):
    monkeypatch.setattr(stt.os, "getenv", lambda key, default=None: "WINDOWS-PC" if key == "COMPUTERNAME" else default)
    monkeypatch.setattr(stt.socket, "gethostname", lambda: "linux.example.com")
    assert stt._get_computer_shortname() == "windows-pc"


def test_get_current_username_prefers_user_env(monkeypatch):
    monkeypatch.setattr(stt.os, "getenv", lambda key, default=None: "Alice" if key == "USER" else default)
    assert stt._get_current_username() == "alice"


def test_get_current_username_falls_back_to_getpass(monkeypatch):
    monkeypatch.setattr(stt.os, "getenv", lambda *_a, **_k: None)
    monkeypatch.setattr(stt.getpass, "getuser", lambda: "Bob")
    assert stt._get_current_username() == "bob"


def test_get_audit_log_path():
    p = stt.get_audit_log_path()
    assert p.parent == Path(stt.tempfile.gettempdir())
    assert p.name.startswith("gemini-transcribe-wrapper-")
    assert p.name.endswith(".audit.jsonl")
    # stem layout: gemini-transcribe-wrapper-<host>-<user>
    stem = p.name[: -len(".audit.jsonl")]
    parts = stem.split("-")
    assert parts[0] == "gemini"
    assert parts[1] == "transcribe"
    assert parts[2] == "wrapper"
    host, user = parts[3], parts[4]
    assert host == host.lower() and re.match(r"^[a-z0-9._-]+$", host)
    assert user == user.lower() and re.match(r"^[a-z0-9._-]+$", user)


def test_extract_status_code():
    assert stt._extract_status_code(None) == 200

    class MockErr(Exception):
        def __init__(self, code, msg=""):
            self.code = code
            super().__init__(msg)

    assert stt._extract_status_code(MockErr(429)) == 429
    assert stt._extract_status_code(MockErr(400)) == 400
    assert stt._extract_status_code(Exception("429 Resource has been exhausted (quota)")) == 429
    assert stt._extract_status_code(Exception("404 Not Found")) == 404
    assert stt._extract_status_code(Exception("Quota exceeded daily limit")) == 429
    assert stt._extract_status_code(Exception("Something unexpected")) == 500


def test_append_audit_log_writes_valid_jsonl(tmp_path):
    log_file = tmp_path / "audit.jsonl"
    stt.append_audit_log(
        input_file_path="/path/to/video.mp4",
        audio_chunk_file_path="/path/to/work/chunk_000.mp3",
        audio_chunk_playtime_s=1740.0,
        api_processing_time_s=12.345,
        api_http_status_code=200,
        api_key="AIzaSyDummyKey12345678",
        timestamp="2026-08-30T11:46:16-04:00",
        log_path=log_file,
    )
    stt.append_audit_log(
        input_file_path="/path/to/video.mp4",
        audio_chunk_file_path="/path/to/work/chunk_001.mp3",
        audio_chunk_playtime_s=500.5,
        api_processing_time_s=-1.0,
        api_http_status_code=429,
        api_key="AIzaSyDummyKey12345678",
        timestamp="2026-08-30T11:46:46-04:00",
        log_path=log_file,
    )

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    r1 = json.loads(lines[0])
    assert r1["timestamp"] == "2026-08-30T11:46:16-04:00"
    assert r1["api_key_tail"] == "12345678"
    assert r1["input_file_path"] == "/path/to/video.mp4"
    assert r1["audio_chunk_file_path"] == "/path/to/work/chunk_000.mp3"
    assert r1["audio_chunk_playtime_s"] == 1740.0
    assert r1["api_processing_time_s"] == 12.345
    assert r1["api_http_status_code"] == 200

    r2 = json.loads(lines[1])
    assert r2["timestamp"] == "2026-08-30T11:46:46-04:00"
    assert r2["api_key_tail"] == "12345678"
    assert r2["input_file_path"] == "/path/to/video.mp4"
    assert r2["audio_chunk_file_path"] == "/path/to/work/chunk_001.mp3"
    assert r2["audio_chunk_playtime_s"] == 500.5
    assert r2["api_processing_time_s"] == -1
    assert r2["api_http_status_code"] == 429


def test_transcribe_chunk_logs_audit_on_success_and_failure(tmp_path, monkeypatch):
    log_file = tmp_path / "test_audit.jsonl"
    monkeypatch.setattr(stt, "get_audit_log_path", lambda: log_file)

    chunk_mp3 = tmp_path / "chunk_000.mp3"
    chunk_mp3.write_bytes(b"dummy audio data")

    # 1. Success case
    # ``cooldown_secs=0.0`` disables the 600s pool-drain wait so a
    # misconfigured test can't hang the suite. The success path doesn't
    # touch it, but keeping the knob explicit prevents future regressions.
    client = stt.TranscribeClient(
        api_key="AIzaSyDummyKey12345678",
        request_interval_secs=0.0,
        cooldown_secs=0.0,
    )
    mock_upload = MagicMock()
    mock_upload.uri = "files/test"
    mock_upload.name = "files/test"
    client.client.files.upload = MagicMock(return_value=mock_upload)
    client.client.files.delete = MagicMock()

    mock_step = MagicMock()
    mock_step.type = "model_output"
    mock_content = MagicMock()
    mock_content.type = "text"
    mock_content.annotations = []
    mock_step.content = [mock_content]
    mock_interaction = MagicMock()
    mock_interaction.steps = [mock_step]
    mock_interaction.output_text = "안녕하세요"
    client.client.interactions.create = MagicMock(return_value=mock_interaction)

    result = client.transcribe_chunk(
        chunk_mp3,
        chunk_index=0,
        source_file="/original/input.mp4",
        chunk_duration_secs=120.0,
    )
    assert result.text == "안녕하세요"

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    r = json.loads(lines[0])
    assert r["api_key_tail"] == "12345678"
    assert r["input_file_path"] == "/original/input.mp4"
    assert r["audio_chunk_file_path"] == str(chunk_mp3.resolve())
    assert r["audio_chunk_playtime_s"] == 120.0
    assert r["api_processing_time_s"] >= 0
    assert r["api_http_status_code"] == 200
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", r["timestamp"])

    # 2. Failure case (non-quota error propagates immediately)
    # We use a 500-style error here, NOT a 429: a 429 triggers the
    # active/cooldown pool's quota handling (blacklist + retry on the
    # next key), which would loop forever with a single-key test client
    # even with cooldown_secs=0.0. The audit log write path is the
    # same for both error classes; only the status code differs.
    client.client.interactions.create = MagicMock(
        side_effect=RuntimeError("500 Internal Server Error")
    )
    with pytest.raises(RuntimeError):
        client.transcribe_chunk(
            chunk_mp3,
            chunk_index=1,
            source_file="/original/input.mp4",
            chunk_duration_secs=120.0,
        )

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    r_err = json.loads(lines[1])
    assert r_err["api_key_tail"] == "12345678"
    assert r_err["input_file_path"] == "/original/input.mp4"
    assert r_err["audio_chunk_file_path"] == str(chunk_mp3.resolve())
    assert r_err["audio_chunk_playtime_s"] == 120.0
    assert r_err["api_processing_time_s"] == -1
    assert r_err["api_http_status_code"] == 500


def test_cli_parser_audit_jsonl_default_and_custom(tmp_path):
    from gemini_transcribe_wrapper import cli

    # Without --audit-jsonl-file, the option stays unresolved (None). The
    # concrete path is decided later by TranscribeClient via stt.get_audit_log_path().
    opts_default = cli.build_options(["sample.mp4"])
    assert opts_default.audit_jsonl_file is None

    custom_path = str(tmp_path / "custom_audit.jsonl")
    opts_custom = cli.build_options(["sample.mp4", "--audit-jsonl-file", custom_path])
    assert opts_custom.audit_jsonl_file == custom_path


def test_format_cli_command_quotes_spaces():
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options([
        "path with space/file name.mp4",
        "--tier", "paid",
        "--output-dir", "out dir",
    ])
    cmd = cli.format_cli_command("gtw", opts)
    assert cmd.startswith("gtw ")
    assert "--tier paid" in cmd
    assert "file name.mp4" in cmd


def test_build_options_accepts_flag_after_path():
    """Click/Typer variadic args are greedy; pre-processor must route flags to the front."""
    from gemini_transcribe_wrapper import cli

    opts = cli.build_options(["sample.mp4", "--tier", "paid"])
    assert opts.path == ["sample.mp4"]
    assert opts.tier == "paid"


def test_cli_main_logs_effective_command(caplog, monkeypatch):
    import logging

    from gemini_transcribe_wrapper import cli

    # Mock gemini_transcribe so it doesn't do real transcription
    mock_batch = MagicMock()
    mock_batch.output_files.return_value = ["my_audio.srt"]
    mock_batch.results = []
    monkeypatch.setattr(cli, "gemini_transcribe", lambda **kwargs: mock_batch)

    with caplog.at_level(logging.INFO):
        exit_code = cli.main(["my_audio.mp3", "--tier", "free"])
        assert exit_code == 0

    plus_logs = [rec.message for rec in caplog.records if rec.message.startswith("+ ")]
    assert len(plus_logs) == 1
    assert "+ " in plus_logs[0]
    assert "my_audio.mp3" in plus_logs[0]
    assert "--tier free" in plus_logs[0]


def test_cli_version_output_has_no_blank_line(capsys):
    from gemini_transcribe_wrapper import cli

    exit_code = cli.main(["-v"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    lines = captured.strip().splitlines()
    assert len(lines) == 2
    assert not any(line == "" for line in lines)
    # Default tier is free -> new format with the ai.dev dashboard link.
    assert "Find your free-tier usage at https://ai.dev" in captured
    assert "rate limits at https://ai.google.dev/gemini-api/docs/rate-limits" in captured


def test_cli_help_output_does_not_contain_usage_summary(capsys):
    from gemini_transcribe_wrapper import cli

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["-h"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr().out
    assert "usage:" in captured.lower()
    assert "Find your free-tier usage" not in captured
