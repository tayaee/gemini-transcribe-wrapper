"""Test the daily API usage counter (~/.cache/gemini-transcribe-wrapper/usage.json)."""

import json
import os
import sys
import tempfile
from datetime import timedelta, timezone
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


def test_summary_line_embeds_pst_time_count_and_limit(cache):
    for _ in range(3):
        usage_counter.increment_today(cache)
    line = usage_counter.usage_summary_line(cache)
    print("summary line:", line)
    assert line.endswith(f"{FREE_TIER_DAILY_LIMIT})")
    assert "API calls today " in line and f": 3/{FREE_TIER_DAILY_LIMIT}" in line
    assert "PST-08:00" in line


def test_pst_offset_is_fixed_utc_minus_8():
    assert usage_counter.PST == timezone(timedelta(hours=-8))


def test_stt_increments_counter_per_api_call(cache):
    """Every real API call (interactions.create) must bump today's count."""
    from typing import Any, cast

    from gemini_transcribe_wrapper import stt

    calls = {"n": 0}

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

    client = stt.TranscribeClient(api_key="fake")
    client.client = cast(Any, FakeClient())
    chunk = Path(cache) / "chunk_000.mp3"
    chunk.write_bytes(b"fake-mp3")

    client.transcribe_chunk(chunk, chunk_index=0)
    assert usage_counter.count_today(cache) == 1
    assert calls["n"] == 1  # cleanup delete happened

    # A second chunk -> second call -> count 2.
    chunk2 = Path(cache) / "chunk_001.mp3"
    chunk2.write_bytes(b"fake-mp3")
    client.transcribe_chunk(chunk2, chunk_index=1)
    assert usage_counter.count_today(cache) == 2


if __name__ == "__main__":
    test_fresh_counter_is_zero(Path(tempfile.mkdtemp()))
    print("PASS: usage counter works")
