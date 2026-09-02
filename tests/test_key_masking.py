"""Tests for the shared API-key masking helper.

Every user-visible log/print site that mentions an API key must funnel
through :func:`gemini_transcribe_wrapper._key_utils.mask_key` so the
format (``[redacted]<last 4>`` for normal keys, ``[redacted]`` for short
keys, ``unset`` for missing keys) stays consistent. This file locks
that contract down.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper._key_utils import mask_key


def test_mask_key_long_key_shows_last_eight():
    """Realistic Gemini keys (39+ chars) render as the 8-char tail."""
    key = "AQ.AXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXMw9g"
    assert mask_key(key) == "XXXXMw9g"


def test_mask_key_five_char_key_shows_only_tail():
    """Boundary: a 5-char key returns all 5 chars."""
    assert mask_key("abcde") == "abcde"


def test_mask_key_four_char_key():
    """4-char key returns all 4 chars."""
    assert mask_key("abcd") == "abcd"


def test_mask_key_three_char_key():
    """3-char key returns all 3 chars."""
    assert mask_key("k1z") == "k1z"


def test_mask_key_empty_string_returns_unset():
    """Empty string distinguishes 'no key' from a key."""
    assert mask_key("") == "unset"


def test_mask_key_none_returns_unset():
    """Missing key (``None``) also maps to ``unset``."""
    assert mask_key(None) == "unset"


def test_mask_key_does_not_lead_with_key_prefix():
    """Guard: never leak the first 4 chars (the legacy ``AIza...`` prefix)."""
    for sample in [
        "AQ.AXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXMw9g",
        "AIzaSyDlong_api_key_xxx_xyzzzzz",
        "sk-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
    ]:
        masked = mask_key(sample)
        assert not masked.startswith(sample[:4]), (
            f"prefix leaked in {masked!r} for {sample!r}"
        )


def test_mask_key_distinguishes_keys_by_tail():
    """Two different long keys produce two different masked forms."""
    a = mask_key("AIzaSyD-1234567890abcdef")
    b = mask_key("AIzaSyD-1234567890xyzz")
    assert a == "90abcdef"
    assert b == "7890xyzz"
    assert a != b
