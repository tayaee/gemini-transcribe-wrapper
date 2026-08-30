"""Unit tests for JSONL audit logging to <os-temp>/google_transcribe_wrapper_audit.jsonl."""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt


def test_get_audit_log_path():
    p = stt.get_audit_log_path()
    assert p.name == "google_transcribe_wrapper_audit.jsonl"


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
    client = stt.TranscribeClient(api_key="AIzaSyDummyKey12345678", request_interval_secs=0.0)
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

    # 2. Failure case (429)
    client.client.interactions.create = MagicMock(side_effect=RuntimeError("429 Resource exhausted"))
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
    assert r_err["api_http_status_code"] == 429
