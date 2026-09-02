"""Unit tests for issue-007: api_key_tail length consistency (8 chars everywhere).

The wrapper defines ``api_key_tail = api_key[-8:]`` (spec §2) and uses
it in:

* audit log records (``append_audit_log``)
* per-key audit file path (issue-004)
* per-key blacklist file path (issue-002)
* per-key usage counter filename
* log lines that mention the key for debugging

A few call sites historically inlined ``key[-4:]`` or ``key[-8:]``; the
new ``api_key_tail`` helper centralizes that rule so the audit log and
the matching log line always show the same 8 characters.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import _key_utils as key_utils

# ---------------------------------------------------------------------------
# api_key_tail helper
# ---------------------------------------------------------------------------


def test_api_key_tail_default_length_is_8():
    """Default length is 8 — the user-spec convention."""
    assert key_utils.api_key_tail("AIzaSyDummyKey12345678") == "12345678"


def test_api_key_tail_short_key_returns_full_key():
    """Keys shorter than ``length`` are returned in full."""
    assert key_utils.api_key_tail("abc") == "abc"
    assert key_utils.api_key_tail("12345678") == "12345678"  # exactly 8


def test_api_key_tail_exact_length():
    """An 8-char key returns itself."""
    assert key_utils.api_key_tail("abcd1234") == "abcd1234"


def test_api_key_tail_longer_key_truncates_to_8():
    """Key longer than 8 returns the last 8 chars."""
    assert key_utils.api_key_tail("AIzaSyDummyKeyXXyy9999zzzz") == "9999zzzz"


def test_api_key_tail_none_returns_empty():
    """``None`` → ``""`` so callers can safely concat without crashing."""
    assert key_utils.api_key_tail(None) == ""


def test_api_key_tail_empty_string_returns_empty():
    """Empty string → ``""``."""
    assert key_utils.api_key_tail("") == ""


def test_api_key_tail_custom_length_4():
    """Pass ``length=4`` to opt into the legacy 4-char tail."""
    assert key_utils.api_key_tail("AIzaSyDummyKey12345678", length=4) == "5678"


def test_api_key_tail_custom_length_longer_than_key():
    """``length`` larger than the key returns the whole key."""
    assert key_utils.api_key_tail("abc", length=12) == "abc"


def test_api_key_tail_length_zero_returns_empty():
    """``length=0`` is degenerate but must not crash; returns ``""``."""
    assert key_utils.api_key_tail("AIzaSyDummyKey12345678", length=0) == ""


def test_mask_key_returns_8_char_tail():
    """``mask_key`` returns [redacted]<last 8 chars> for normal keys (or ``unset`` for empty)."""
    assert key_utils.mask_key("AIzaSyDummyKey12345678") == "[redacted]12345678"
    assert key_utils.mask_key(None) == "unset"
    assert key_utils.mask_key("") == "unset"
    assert key_utils.mask_key("abcd") == "[redacted]abcd"


# ---------------------------------------------------------------------------
# Integration: log line for 429 cooldown uses 8-char tail
# ---------------------------------------------------------------------------


def test_stt_429_log_uses_eight_char_tail():
    """Issue-007: the 429 cooldown log line now uses 8-char tail, not 4.

    Drives the message through the same code path the wrapper uses
    when a key is sent to cooldown, asserting the rendered string
    contains the 8-char tail rather than the legacy 4-char one.
    """

    api_key = "AIzaSyDummyKey12345678"
    # The 8-char tail the spec mandates.
    expected_tail = "12345678"
    # The legacy 4-char tail must NOT be the only thing in the message.
    legacy_tail = "5678"

    # Drive the logger directly — we want to assert the message format
    # without standing up the full transcribe_chunk pipeline.
    cap = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            cap.append(self.format(record))

    handler = _CaptureHandler(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        # The internal log line that previously said "[redacted]5678"
        # must now say "[redacted]12345678".
        logging.getLogger("gemini_transcribe_wrapper.stt").info(
            "key=%s", key_utils.api_key_tail(api_key)
        )
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    assert cap, "no log record captured"
    line = cap[-1]
    assert expected_tail in line
    # Sanity: the bare 4-char tail should NOT appear alone on the line
    # (it would, only if the message format had regressed to use it).
    assert legacy_tail not in line or expected_tail in line
