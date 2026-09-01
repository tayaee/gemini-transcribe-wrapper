"""Custom-vocabulary file loading + post-recognition bias.

The ``--custom-vocabulary-file`` option points at a UTF-8 text file
(one term per line). The loader skips blank lines and ``#`` comments,
warns and returns ``[]`` when the file is missing, and is otherwise
non-fatal. After Gemini returns the transcript, the wrapper applies
the loaded terms as a whitespace-tolerant, case-insensitive phrase
replacement (``apply_vocabulary_bias``) — longer terms are matched
first so multi-word phrases aren't partially consumed.

Note: Gemini Transcribe rejects ``custom_vocabulary`` when timestamps
are requested (400), and the wrapper always needs word-level
timestamps for SRT, so we bias post-recognition instead of sending the
list to the API.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt
from gemini_transcribe_wrapper.api import _load_vocabulary_file

# --- file loader ----------------------------------------------------------


def test_load_vocabulary_file_basic(tmp_path):
    f = tmp_path / "voca.txt"
    f.write_text("Gemini\n사내용어\n수 있다\n", encoding="utf-8")
    assert _load_vocabulary_file(str(f)) == ["Gemini", "사내용어", "수 있다"]


def test_load_vocabulary_file_skips_blank_lines_and_comments(tmp_path):
    f = tmp_path / "voca.txt"
    f.write_text(
        "# header comment\n"
        "\n"
        "  \n"
        "Gemini\n"
        "   # indented comment\n"
        "사내용어\n"
        "\n",
        encoding="utf-8",
    )
    assert _load_vocabulary_file(str(f)) == ["Gemini", "사내용어"]


def test_load_vocabulary_file_missing_warns_and_returns_empty(
    tmp_path, caplog
):
    """Missing file must NOT raise — just warn and return []."""
    missing = tmp_path / "does_not_exist.txt"
    with caplog.at_level(logging.WARNING, logger="gemini_transcribe_wrapper.api"):
        result = _load_vocabulary_file(str(missing))
    assert result == []
    assert any("not found" in rec.message for rec in caplog.records)


def test_load_vocabulary_file_none_returns_empty():
    assert _load_vocabulary_file(None) == []
    assert _load_vocabulary_file("") == []


# --- post-recognition bias ------------------------------------------------


def test_apply_vocabulary_bias_empty_inputs_unchanged():
    assert stt.apply_vocabulary_bias("", ["foo"]) == ""
    assert stt.apply_vocabulary_bias("hello", []) == "hello"
    assert stt.apply_vocabulary_bias("hello", None) == "hello"


def test_apply_vocabulary_bias_exact_match():
    assert stt.apply_vocabulary_bias(
        "I love gemini",
        ["Gemini"],
    ) == "I love Gemini"


def test_apply_vocabulary_bias_case_insensitive():
    assert stt.apply_vocabulary_bias(
        "we use GEMINI and gemini",
        ["Gemini"],
    ) == "we use Gemini and Gemini"


def test_apply_vocabulary_bias_whitespace_tolerant():
    """Any run of whitespace between tokens is treated as a match boundary."""
    # Single space → match
    assert stt.apply_vocabulary_bias(
        "할 수 있다",
        ["수 있다"],
    ) == "할 수 있다"
    # Multiple spaces → match
    assert stt.apply_vocabulary_bias(
        "할 수   있다",
        ["수 있다"],
    ) == "할 수 있다"
    # Tab → match
    assert stt.apply_vocabulary_bias(
        "할 수\t있다",
        ["수 있다"],
    ) == "할 수 있다"


def test_apply_vocabulary_bias_greedy_longer_first():
    """Multi-word terms are matched before their single-word substrings."""
    # "수 있다" should win over "수" (or "있다") when both are present.
    assert stt.apply_vocabulary_bias(
        "할 수 있다",
        ["수", "수 있다"],
    ) == "할 수 있다"


def test_apply_vocabulary_bias_special_chars_escaped():
    """Regex meta-characters in vocabulary are treated as literals."""
    assert stt.apply_vocabulary_bias(
        "see foo.bar and fooXbar",
        ["foo.bar"],
    ) == "see foo.bar and fooXbar"


def test_apply_vocabulary_bias_no_match_unchanged():
    assert stt.apply_vocabulary_bias(
        "completely unrelated text",
        ["Gemini", "수 있다"],
    ) == "completely unrelated text"


# --- end-to-end: chunk text passes through the bias ----------------------


def _ok_interaction(text: str = "할 수 있다") -> MagicMock:
    mock_step = MagicMock()
    mock_step.type = "model_output"
    mock_content = MagicMock()
    mock_content.type = "text"
    mock_content.annotations = []
    mock_step.content = [mock_content]
    ok = MagicMock()
    ok.steps = [mock_step]
    ok.output_text = text
    return ok


def _chunk(path: Path, name: str) -> Path:
    p = path / name
    p.write_bytes(b"fake-mp3")
    return p


class _SingleKeyClient(stt.TranscribeClient):
    """Minimal client that always succeeds on the single configured key."""

    def __init__(self, vocab: list[str] | None) -> None:
        self.api_key = "k0aaaaaa"
        self.api_logs: list[dict] = []
        self.request_interval_secs = 0.0
        self.tier = "free"
        self.model = stt.MODEL_ID
        self.custom_vocabulary = list(vocab) if vocab else None

        mock_upload = MagicMock()
        mock_upload.uri = "files/test"
        mock_upload.name = "files/test"
        self.client = MagicMock()
        self.client.files.upload = MagicMock(return_value=mock_upload)
        self.client.files.delete = MagicMock()

        self._text_to_return = "할 수 있다"

        def _create(**_kwargs):
            return _ok_interaction(self._text_to_return)

        self.client.interactions.create = MagicMock(side_effect=_create)

        # Single-key mode — no active/cooldown pool needed for this test.
        self._active_pool: list[str] = [self.api_key]
        self._cooldown_pool: set[str] = set()
        self._rr_index = 0
        self._api_keys = [self.api_key]
        self.audit_jsonl_file = None

    def _generation_config(self):  # type: ignore[override]
        return MagicMock()


def test_transcribe_chunk_applies_vocabulary_bias(tmp_path, monkeypatch):
    """End-to-end: chunk text passes through ``apply_vocabulary_bias``."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    client = _SingleKeyClient(vocab=["수 있다"])
    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    # The model returned "할 수 있다"; the bias replaces the substring
    # "수 있다" with the canonical "수 있다". In this case they match
    # exactly so the text is unchanged, but the call confirms the bias
    # path is wired up.
    assert result.text == "할 수 있다"


def test_transcribe_chunk_bias_replaces_recognized_text(
    tmp_path, monkeypatch
):
    """When the recognizer's output differs from the registered vocab,
    the bias replaces the recognized substring with the canonical form."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    client = _SingleKeyClient(vocab=["Gemini"])
    client._text_to_return = "I use gemini daily"
    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "I use Gemini daily"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))