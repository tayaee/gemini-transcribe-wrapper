"""Unit tests for CLI parsing of multi-key options.

Verifies:
- ``--gemini-api-keys=k1;k2;k3`` parses to ``["k1", "k2", "k3"]``.
- ``--gemini-api-key=k1`` (deprecated singular) parses to ``["k1"]`` and
  logs a deprecation warning with the masked key.
- The two flags can be combined; explicit plural values come first.
- ``format_cli_command`` round-trips a multi-key value as
  ``--gemini-api-keys k1;k2;k3`` (and never emits the singular alias).
- ``_resolve_api_keys`` merges CLI + env precedence and dedupes.
- ``_mask_cli_key`` masks all but first/last 4 chars.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.cli import (
    TranscribeOptions,
    _mask_cli_key,
    _resolve_api_keys,
    build_options,
    format_cli_command,
)

# --- --gemini-api-keys parsing --------------------------------------------


def test_gemini_api_keys_parses_separator():
    """Semicolon-separated values parse into a list, preserving order."""
    opts = build_options(["--gemini-api-keys", "k1;k2;k3", "input.mp4"])
    assert opts.gemini_api_keys == ["k1", "k2", "k3"]


def test_gemini_api_keys_strips_whitespace_and_blanks():
    """Whitespace and empty entries are stripped."""
    opts = build_options(
        ["--gemini-api-keys", "k1; k2 ;; k3", "input.mp4"]
    )
    assert opts.gemini_api_keys == ["k1", "k2", "k3"]


def test_gemini_api_keys_dedupes():
    """Duplicate keys collapse to a single ordered list."""
    opts = build_options(["--gemini-api-keys", "k1;k2;k1;k3;k2", "input.mp4"])
    assert opts.gemini_api_keys == ["k1", "k2", "k3"]


def test_gemini_api_keys_default_is_empty_list():
    """Without the flag, ``gemini_api_keys`` is an empty list."""
    opts = build_options(["input.mp4"])
    assert opts.gemini_api_keys == []


def test_gemini_api_keys_single_value_still_works():
    """Single value (no separator) still parses to one-element list."""
    opts = build_options(["--gemini-api-keys", "k1", "input.mp4"])
    assert opts.gemini_api_keys == ["k1"]


# --- --gemini-api-key (deprecated) parsing -------------------------------


def test_gemini_api_key_singular_treated_as_one_element_list(caplog):
    """The deprecated singular flag yields a one-element list and logs a warning."""
    with caplog.at_level(logging.WARNING, logger="gemini_transcribe_wrapper.cli"):
        opts = build_options(["--gemini-api-key", "k1", "input.mp4"])

    assert opts.gemini_api_keys == ["k1"]

    # Deprecation warning emitted (the example value in the help text
    # mentions a list so we don't check for the literal key value).
    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("--gemini-api-key is deprecated" in m for m in warnings)
    # The masked short key "**" should appear in the warning.
    assert any("'**'" in m for m in warnings)


def test_gemini_api_key_singular_does_not_crash_when_masked():
    """Short keys (≤8 chars) should produce a fully-masked string without IndexError."""
    masked = _mask_cli_key("k1")
    assert "*" in masked
    assert "k1" not in masked


def test_plural_takes_precedence_over_singular(caplog):
    """When both flags are given, the explicit plural value comes first
    and the deprecated singular is appended (deduped).
    """
    with caplog.at_level(logging.WARNING, logger="gemini_transcribe_wrapper.cli"):
        opts = build_options(
            [
                "--gemini-api-keys",
                "k1;k2",
                "--gemini-api-key",
                "k3",
                "input.mp4",
            ]
        )
    assert opts.gemini_api_keys == ["k1", "k2", "k3"]


def test_plural_and_singular_same_key_dedupes(caplog):
    """Same key on both flags → just one entry (no duplicates)."""
    with caplog.at_level(logging.WARNING, logger="gemini_transcribe_wrapper.cli"):
        opts = build_options(
            [
                "--gemini-api-keys",
                "k1;k2",
                "--gemini-api-key",
                "k2",
                "input.mp4",
            ]
        )
    assert opts.gemini_api_keys == ["k1", "k2"]


# --- format_cli_command round-trip ---------------------------------------


def test_format_cli_command_emits_plural_form():
    """Round-trip a multi-key value through format_cli_command.

    The command-line log line redacts keys to ``[redacted]<last4>`` so
    the emitted string never contains a full API key.
    """
    opts = TranscribeOptions(
        path=["input.mp4"],
        gemini_api_keys=["k1aaa", "k2bbb", "k3ccc"],
        tier="free",
    )
    out = format_cli_command("gemini-transcribe", opts)
    tokens = out.split()
    # Plural flag with the redacted semicolon-joined value ([redacted] + last 4 chars).
    assert "--gemini-api-keys" in tokens
    assert "'[redacted]1aaa;[redacted]2bbb;[redacted]3ccc'" in out
    # Raw keys must not appear in the emitted command.
    assert "k1aaa" not in out
    assert "k2bbb" not in out
    assert "k3ccc" not in out
    # Singular alias should never be emitted as its own token.
    assert "--gemini-api-key" not in tokens


def test_format_cli_command_omits_when_no_keys():
    """No keys → neither flag is emitted."""
    opts = TranscribeOptions(path=["input.mp4"], tier="free")
    out = format_cli_command("gemini-transcribe", opts)
    tokens = out.split()
    assert "--gemini-api-keys" not in tokens
    assert "--gemini-api-key" not in tokens


# --- _resolve_api_keys precedence ----------------------------------------


def test_resolve_api_keys_cli_only(monkeypatch):
    """Explicit CLI values win; env vars are ignored when CLI provides them."""
    monkeypatch.setenv("GEMINI_API_KEYS", "env1;env2")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    out = _resolve_api_keys(["cli1", "cli2"])
    assert out == ["cli1", "cli2"]


def test_resolve_api_keys_falls_back_to_env_plural(monkeypatch):
    """No CLI keys → $GEMINI_API_KEYS (semicolon-separated) is used."""
    monkeypatch.setenv("GEMINI_API_KEYS", "env1;env2")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    out = _resolve_api_keys([])
    assert out == ["env1", "env2"]


def test_resolve_api_keys_falls_back_to_env_singular(monkeypatch):
    """No CLI + no plural env → $GEMINI_API_KEY is used as one-element list."""
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "single_env")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    out = _resolve_api_keys([])
    assert out == ["single_env"]


def test_resolve_api_keys_falls_back_to_google(monkeypatch):
    """All else empty → $GOOGLE_API_KEY saves us."""
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google_env")
    out = _resolve_api_keys([])
    assert out == ["google_env"]


def test_resolve_api_keys_priority_order(monkeypatch):
    """When multiple sources are present, CLI > $GEMINI_API_KEYS > $GEMINI_API_KEY > $GOOGLE_API_KEY."""
    monkeypatch.setenv("GEMINI_API_KEYS", "pl_env")
    monkeypatch.setenv("GEMINI_API_KEY", "sing_env")
    monkeypatch.setenv("GOOGLE_API_KEY", "google_env")
    out = _resolve_api_keys(["cli1"])
    assert out == ["cli1"]  # CLI wins

    out = _resolve_api_keys([])
    assert out == ["pl_env", "sing_env", "google_env"]  # env precedence


def test_resolve_api_keys_dedupes_within_cli(monkeypatch):
    """Duplicate CLI keys collapse to a single ordered list (env not consulted)."""
    monkeypatch.setenv("GEMINI_API_KEYS", "k1;k2")
    out = _resolve_api_keys(["k1", "k99", "k1"])
    assert out == ["k1", "k99"]


def test_resolve_api_keys_empty_when_no_sources(monkeypatch):
    """No CLI + no env → empty list."""
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    out = _resolve_api_keys([])
    assert out == []


# --- _mask_cli_key -------------------------------------------------------


def test_mask_cli_key_long():
    """Keys longer than 8 chars show first 4 + stars + last 4."""
    masked = _mask_cli_key("AIzaSyDlong_api_key_xxx_xyzzzzz")
    assert masked.startswith("AIza")
    assert masked.endswith("zzzz")
    assert "*" in masked
    assert "long_api_key_xxx_xyz" not in masked


def test_mask_cli_key_short_is_fully_masked():
    """Keys ≤ 8 chars are fully masked with stars."""
    assert _mask_cli_key("k1") == "**"
    assert _mask_cli_key("abcdefgh") == "********"


def test_gemini_api_keys_comma_and_semicolon():
    """Both comma and semicolon work as separators for --gemini-api-keys."""
    opts1 = build_options(["--gemini-api-keys", "k1,k2,k3", "input.mp4"])
    assert opts1.gemini_api_keys == ["k1", "k2", "k3"]

    opts2 = build_options(["--gemini-api-keys", "k1;k2,k3", "input.mp4"])
    assert opts2.gemini_api_keys == ["k1", "k2", "k3"]


def test_language_codes_comma_and_semicolon():
    """Both comma and semicolon work as separators for --language-codes."""
    opts1 = build_options(["--language-codes", "ko-KR,en-US,ja-JP", "input.mp4"])
    assert opts1.language_codes == ["ko-KR", "en-US", "ja-JP"]

    opts2 = build_options(["--language-codes", "ko-KR;en-US,ja-JP", "input.mp4"])
    assert opts2.language_codes == ["ko-KR", "en-US", "ja-JP"]


def test_custom_vocabulary_comma_and_semicolon():
    """Both comma and semicolon work as separators for parse_custom_vocabulary."""
    from gemini_transcribe_wrapper.cli import parse_custom_vocabulary

    assert parse_custom_vocabulary("word1,word2,word3") == ["word1", "word2", "word3"]
    assert parse_custom_vocabulary("word1;word2,word3") == ["word1", "word2", "word3"]


def test_speakers_allows_commas_in_names():
    """--speakers uses ONLY semicolon so names may contain commas."""
    from gemini_transcribe_wrapper.cli import parse_speakers

    mapping = parse_speakers("spk:0=Doe, John, Jr.;spk:1=Smith, Jane, Ph.D.;")
    assert mapping == {
        "spk:0": "Doe, John, Jr.",
        "spk:1": "Smith, Jane, Ph.D.",
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
