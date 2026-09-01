"""Unit tests for the --language-codes CLI option and downstream wiring.

Verifies:
- ``--language-codes`` parses a semicolon-separated list, defaulting to
  ``["ko-KR", "en-US"]``.
- Empty string (``--language-codes=""``) yields an empty list, enabling
  Gemini's auto language detection downstream.
- The list is forwarded into ``TranscribeClient.language_codes`` via
  ``gemini_transcribe``.
- ``_generation_config`` includes ``language_codes`` when set, and
  omits the field entirely when both codes are empty (auto detection).
- ``format_cli_command`` round-trips the list as a semicolon-separated value.
- The deprecated ``--language`` flag is no longer accepted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.cli import (
    TranscribeOptions,
    build_options,
    format_cli_command,
)
from gemini_transcribe_wrapper.stt import TranscribeClient

# --- CLI parsing ---------------------------------------------------------


def test_language_codes_default_is_ko_kr_en_us():
    """The flag defaults to ``ko-KR;en-US``."""
    opts = build_options(["input.mp4"])
    assert opts.language_codes == ["ko-KR", "en-US"]


def test_language_codes_explicit_separator():
    """Explicit semicolon-separated string is parsed into an ordered list."""
    opts = build_options(
        ["--language-codes", "ko-KR;en-US;ja-JP", "input.mp4"]
    )
    assert opts.language_codes == ["ko-KR", "en-US", "ja-JP"]


def test_language_codes_strips_whitespace_and_blanks():
    """Whitespace and blank entries are stripped."""
    opts = build_options(
        ["--language-codes", "ko-KR; en-US ;; ja-JP", "input.mp4"]
    )
    assert opts.language_codes == ["ko-KR", "en-US", "ja-JP"]


def test_language_codes_dedupes():
    """Duplicate codes collapse to a single ordered list."""
    opts = build_options(
        ["--language-codes", "ko-KR;en-US;ko-KR;en-US", "input.mp4"]
    )
    assert opts.language_codes == ["ko-KR", "en-US"]


def test_language_codes_empty_string_enables_auto_detection():
    """``--language-codes=""`` yields an empty list (auto detection)."""
    opts = build_options(["--language-codes", "", "input.mp4"])
    assert opts.language_codes == []


def test_language_codes_single_value():
    """A single value still parses (no separator → one-element list)."""
    opts = build_options(["--language-codes", "fr-FR", "input.mp4"])
    assert opts.language_codes == ["fr-FR"]


def test_language_option_no_longer_accepted():
    """``--language`` is removed; passing it must not be silently honored.

    Click rejects unknown options with a non-zero exit, so we assert
    that the option name is no longer recognized.
    """
    with pytest.raises(SystemExit):
        build_options(["--language", "fr-FR", "input.mp4"])


# --- format_cli_command round-trip ---------------------------------------


def test_format_cli_command_emits_language_codes():
    """Round-trip an explicit list through format_cli_command."""
    opts = TranscribeOptions(
        path=["input.mp4"],
        language_codes=["ko-KR", "en-US", "ja-JP"],
        tier="free",
    )
    out = format_cli_command("gemini-transcribe", opts)
    assert "--language-codes" in out.split()
    assert "ko-KR;en-US;ja-JP" in out


def test_format_cli_command_omits_language_codes_when_empty():
    """Empty list → no --language-codes token in the emitted command."""
    opts = TranscribeOptions(
        path=["input.mp4"],
        language_codes=[],
        tier="free",
    )
    out = format_cli_command("gemini-transcribe", opts)
    assert "--language-codes" not in out.split()


def test_format_cli_command_uses_default_codes_when_unset():
    """When the caller passes the default list, format_cli_command emits it."""
    opts = TranscribeOptions(
        path=["input.mp4"],
        language_codes=["ko-KR", "en-US"],
        tier="free",
    )
    out = format_cli_command("gemini-transcribe", opts)
    assert "ko-KR;en-US" in out


# --- TranscribeClient wiring ---------------------------------------------


def test_transcribe_client_stores_language_codes():
    """The list flows into the client's attribute, preserving order."""
    client = TranscribeClient(
        api_key="fake-key",
        language_codes=["ko-KR", "en-US", "ja-JP"],
    )
    assert client.language_codes == ["ko-KR", "en-US", "ja-JP"]


def test_transcribe_client_strips_empty_entries():
    """Empty/whitespace entries are removed from the stored list."""
    client = TranscribeClient(
        api_key="fake-key",
        language_codes=["ko-KR", "", "  ", "en-US"],
    )
    assert client.language_codes == ["ko-KR", "en-US"]


def test_transcribe_client_none_codes_yields_none():
    """``None`` input → attribute is ``None`` (auto detection path)."""
    client = TranscribeClient(api_key="fake-key")
    assert client.language_codes is None


def test_transcribe_client_empty_list_yields_none():
    """An empty list input → attribute is ``None`` (auto detection path)."""
    client = TranscribeClient(api_key="fake-key", language_codes=[])
    assert client.language_codes is None


# --- _generation_config priority -----------------------------------------


def test_generation_config_uses_language_codes_when_set():
    """When ``language_codes`` is set, the field is forwarded verbatim."""
    client = TranscribeClient(
        api_key="fake-key",
        language_codes=["en-US", "ja-JP"],
    )
    gen = client._generation_config()
    tc = gen.transcription_config
    assert tc is not None
    assert getattr(tc, "language_codes", None) == ["en-US", "ja-JP"]


def test_generation_config_omits_field_when_codes_empty():
    """Empty codes → field omitted from generation config (auto detection)."""
    client = TranscribeClient(api_key="fake-key", language_codes=None)
    gen = client._generation_config()
    tc = gen.transcription_config
    assert tc is not None
    # Field is absent or None → Gemini auto-detects.
    assert getattr(tc, "language_codes", "missing") in (None, "missing")


if __name__ == "__main__":
    test_language_codes_default_is_ko_kr_en_us()
    test_language_codes_explicit_separator()
    test_language_codes_strips_whitespace_and_blanks()
    test_language_codes_dedupes()
    test_language_codes_empty_string_enables_auto_detection()
    test_language_codes_single_value()
    test_language_option_no_longer_accepted()
    test_format_cli_command_emits_language_codes()
    test_format_cli_command_omits_language_codes_when_empty()
    test_format_cli_command_uses_default_codes_when_unset()
    test_transcribe_client_stores_language_codes()
    test_transcribe_client_strips_empty_entries()
    test_transcribe_client_none_codes_yields_none()
    test_transcribe_client_empty_list_yields_none()
    test_generation_config_uses_language_codes_when_set()
    test_generation_config_omits_field_when_codes_empty()
