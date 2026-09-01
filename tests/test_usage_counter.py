"""Test the daily API usage counter (~/.cache/gemini-transcribe-wrapper/usage.json)."""

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import usage_counter

FREE_TIER_DAILY_LIMIT = usage_counter.FREE_TIER_DAILY_LIMIT


@pytest.fixture
def cache(tmp_path):
    os.environ["GTW_CACHE_DIR"] = str(tmp_path)
    yield tmp_path
    os.environ.pop("GTW_CACHE_DIR", None)


def test_fresh_counter_is_zero(cache):
    assert usage_counter.count_today(cache) == 0


def test_increment_writes_json_keyed_by_pst_date(cache):
    n = usage_counter.increment_today(cache)
    assert n == 1
    usage_file = cache / usage_counter.USAGE_FILE
    assert usage_file.exists()
    data = json.loads(usage_file.read_text(encoding="utf-8"))
    assert data.get(usage_counter.pst_date()) == 1


def test_increments_accumulate_per_pst_day(cache):
    usage_counter.increment_today(cache)
    usage_counter.increment_today(cache)
    usage_counter.increment_today(cache)
    assert usage_counter.count_today(cache) == 3


def test_other_days_count_separately(cache):
    usage_counter.increment_today(cache)
    usage_file = cache / usage_counter.USAGE_FILE
    other_day = (usage_counter.pst_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    data = json.loads(usage_file.read_text(encoding="utf-8"))
    data[other_day] = 7
    usage_file.write_text(json.dumps(data), encoding="utf-8")

    assert usage_counter.count_today(cache) == 1
    data = json.loads(usage_file.read_text(encoding="utf-8"))
    assert data[other_day] == 7


def test_summary_line_free_tier_format(cache, monkeypatch):
    """Free-tier default: usage dashboard link + key tail + reset countdown."""
    from datetime import datetime

    base = datetime(2026, 8, 30, 19, 40, 0, tzinfo=usage_counter.PT)  # 4h20m to midnight
    monkeypatch.setattr(usage_counter, "pt_now", lambda: base)

    line = usage_counter.usage_summary_line(cache)
    print("summary line:", line)
    assert line.startswith("Find your free-tier usage at https://ai.dev")
    assert "rate limits at https://ai.google.dev/gemini-api/docs/rate-limits" in line
    assert "midnight PT" in line
    # 4h 20m remaining
    assert "4 hours 20 minutes left" in line
    # No key set -> 'unset'
    assert "ending with 'unset'" in line


def test_summary_line_free_tier_with_key_tail(cache):
    """When a key is set, only the last 4 chars ('....<tail>') are exposed."""
    key = "AIzaSyD-1234567890abcdef"
    for _ in range(5):
        usage_counter.increment_today(cache, api_key=key)
    line = usage_counter.usage_summary_line(cache, api_key=key)
    print("summary line:", line)
    assert "ending with '....cdef'" in line
    # Whole key must NOT leak
    assert "AIzaSyD" not in line
    assert "1234567890" not in line


def test_summary_line_paid_tier_keeps_legacy_format(cache):
    """Paid tier falls back to the original 'API call attempts today ...' line."""
    for _ in range(3):
        usage_counter.increment_today(cache)
    line = usage_counter.usage_summary_line(cache, tier="paid")
    print("summary line:", line)
    assert line.endswith(f"(free tier limit: {FREE_TIER_DAILY_LIMIT})")
    assert "API call attempts today " in line
    assert "with key 'unset': attempted 3" in line
    assert "(PT)" in line

    key = "AIzaSyD-1234567890abcdef"
    for _ in range(2):
        usage_counter.increment_today(cache, api_key=key)
    line_key = usage_counter.usage_summary_line(cache, api_key=key, tier="paid")
    # Legacy 3 calls + 2 key calls = 5 total migrated calls
    assert "with key 'AIza****************cdef': attempted 5" in line_key
    assert line_key.endswith(f"(free tier limit: {FREE_TIER_DAILY_LIMIT})")


def test_pt_timezone():
    from zoneinfo import ZoneInfo
    assert usage_counter.PT == ZoneInfo("America/Los_Angeles")


def test_stt_increments_counter_per_api_call(cache):
    """Every real API call (interactions.create) must bump today's count."""
    from typing import Any, cast

    from gemini_transcribe_wrapper import stt

    calls = {"n": 0}
    api_key = "fake-key-stt"

    class FakeUploaded:
        uri = "files/fake-uri"
        name = "files/fake-name"

    class FakeInteraction:
        output_text = "안녕"

    class FakeFiles:
        def upload(self, file):
            return FakeUploaded()

        def delete(self, name):
            calls["n"] += 1

    class FakeInteractions:
        def create(self, model, input, generation_config):
            return FakeInteraction()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        files = FakeFiles()
        interactions = FakeInteractions()

    client = stt.TranscribeClient(api_key=api_key, request_interval_secs=0.0)
    client.client = cast(Any, FakeClient())
    chunk = Path(cache) / "chunk_000.mp3"
    chunk.write_bytes(b"fake-mp3")

    client.transcribe_chunk(chunk, chunk_index=0)
    assert usage_counter.count_today(cache, api_key=api_key) == 1
    assert calls["n"] == 1  # cleanup delete happened

    # A second chunk -> second call -> count 2.
    chunk2 = Path(cache) / "chunk_001.mp3"
    chunk2.write_bytes(b"fake-mp3")
    client.transcribe_chunk(chunk2, chunk_index=1)
    assert usage_counter.count_today(cache, api_key=api_key) == 2


# --- per-key isolation ------------------------------------------------------


def test_usage_file_uses_key_hash_in_filename(cache):
    """Different keys must yield different per-key usage files."""
    from gemini_transcribe_wrapper.usage_counter import _usage_file

    a = _usage_file(cache, api_key="key-A")
    b = _usage_file(cache, api_key="key-B")
    none = _usage_file(cache)

    assert a != b
    assert none.name == usage_counter.USAGE_FILE
    assert a.name.startswith("usage-") and a.name.endswith(".json")
    assert b.name.startswith("usage-") and b.name.endswith(".json")
    assert a.name != b.name


def test_usage_file_is_stable_for_same_key(cache):
    """Same key must always resolve to the same file (stable hash)."""
    from gemini_transcribe_wrapper.usage_counter import _usage_file

    assert _usage_file(cache, api_key="same-key") == _usage_file(cache, api_key="same-key")


def test_keys_have_independent_daily_counts(cache):
    """Incrementing key-A must not bump the count for key-B."""
    usage_counter.increment_today(cache, api_key="key-A")
    usage_counter.increment_today(cache, api_key="key-A")
    usage_counter.increment_today(cache, api_key="key-B")

    assert usage_counter.count_today(cache, api_key="key-A") == 2
    assert usage_counter.count_today(cache, api_key="key-B") == 1
    # Unscoped (no key) is independent of either.
    assert usage_counter.count_today(cache) == 0


def test_no_key_falls_back_to_legacy_usage_json(cache):
    """When no key is provided, the counter uses the legacy usage.json file."""
    usage_counter.increment_today(cache)
    usage_counter.increment_today(cache)
    assert usage_counter.count_today(cache) == 2
    legacy = cache / usage_counter.USAGE_FILE
    assert legacy.exists()


def test_legacy_count_migrates_to_per_key_on_first_call(cache):
    """The first per-key call after upgrade folds legacy count into per-key.

    Before the bug fix, an env-var-only setup tallied into the unscoped
    legacy file; users running the old code want their existing daily
    count carried over so the per-key display reflects reality.
    """
    legacy = cache / usage_counter.USAGE_FILE
    legacy.write_text(
        json.dumps({usage_counter.pst_date(): 9}), encoding="utf-8"
    )
    # Before any per-key write, the display falls back to legacy so the
    # user already sees 9/25 in `gtw -v`.
    assert usage_counter.count_today(cache, api_key="key-A") == 9
    # First per-key call: migrate 9 → per-key becomes 9 + 1 = 10, legacy cleared.
    n = usage_counter.increment_today(cache, api_key="key-A")
    assert n == 10
    assert usage_counter.count_today(cache, api_key="key-A") == 10
    assert usage_counter.count_today(cache, api_key="key-B") == 0
    # Legacy entry for today is gone.
    data = json.loads(legacy.read_text(encoding="utf-8")) if legacy.exists() else {}
    assert usage_counter.pst_date() not in data
    # Subsequent per-key calls just keep incrementing from 10 — no double count.
    n = usage_counter.increment_today(cache, api_key="key-A")
    assert n == 11
    assert usage_counter.count_today(cache, api_key="key-A") == 11


def test_per_key_files_coexist_with_legacy(cache):
    """Legacy usage.json and per-key files must not collide on independent use.

    Two independent counters per PST day:
    - ``usage.json`` (legacy, unscoped) is for callers that pass ``api_key=None``.
    - ``usage-<hash>.json`` (per-key) is for callers that pass an explicit key.

    The first per-key call on a fresh cache does NOT migrate the legacy
    count when the legacy entry came from a legitimate no-key call on the
    same day — it just increments from zero. (The real one-time migration
    is exercised by :func:`test_legacy_count_migrates_to_per_key_on_first_call`.)
    """
    # Bump legacy past midnight / to a different date so the per-key call
    # does NOT see "today" in the legacy file and won't migrate it.
    other_day = "1999-01-01"
    legacy = cache / usage_counter.USAGE_FILE
    legacy.write_text(json.dumps({other_day: 5}), encoding="utf-8")

    usage_counter.increment_today(cache, api_key="key-A")
    usage_counter.increment_today(cache, api_key="key-A")
    assert usage_counter.count_today(cache, api_key="key-A") == 2
    # Legacy entry for the OTHER day is untouched; today's entry was never created.
    data = json.loads(legacy.read_text(encoding="utf-8"))
    assert data.get(other_day) == 5
    assert usage_counter.pst_date() not in data


def test_key_hash_is_non_reversible(cache):
    """The hash must not leak the raw key."""
    from gemini_transcribe_wrapper.usage_counter import _key_hash

    h = _key_hash("super-secret-key-value")
    assert "super-secret-key-value" not in h
    assert len(h) <= 12  # truncated hex


if __name__ == "__main__":
    test_fresh_counter_is_zero(Path(tempfile.mkdtemp()))
    print("PASS: usage counter works")
