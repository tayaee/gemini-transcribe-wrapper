"""Unit tests for the chunk split plan (default: pack 29m50s chunks)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.audio import compute_split_plan


def test_short_audio_single_chunk():
    plan = compute_split_plan(1790.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == 1790.0


def test_just_over_limit_two_chunks():
    # 29m51s -> one 1790s chunk + a 1s tail.
    plan = compute_split_plan(1791.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == 1790.0


def test_packs_full_chunks():
    # 59m40s -> 2 full 1790s chunks.
    plan = compute_split_plan(3580.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == 1790.0


def test_three_chunks_with_short_tail():
    # 60m -> 3 chunks: 1790 + 1790 + 20.
    plan = compute_split_plan(3600.0)
    assert plan.num_chunks == 3
    assert plan.chunk_secs == 1790.0


def test_no_chunk_exceeds_1790():
    for total in (1500, 1790, 1791, 3580, 3600, 7200, 10000):
        plan = compute_split_plan(float(total))
        assert plan.chunk_secs <= 1790.0
        assert plan.num_chunks * plan.chunk_secs >= total


def test_explicit_chunk_secs_still_works():
    plan = compute_split_plan(3600.0, chunk_secs=60.0)
    assert plan.num_chunks == 60
    assert plan.chunk_secs == 60.0
