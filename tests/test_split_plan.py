"""Unit tests for the chunk split plan.

Chunks are front-loaded: the first N-1 chunks fill to ``max_chunk_secs`` and
the last chunk absorbs the remainder. ``SplitPlan.chunk_secs`` is a tuple of
per-chunk sizes (variable), and ``SplitPlan.offsets`` is the cumulative
start time of each chunk.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.audio import compute_split_plan

# Default (diarize=ON, 30-min API limit, 30-min ceiling) --------------------


def test_short_audio_single_chunk_default_ceiling():
    plan = compute_split_plan(100.0, max_chunk_secs=1800.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (100.0,)
    assert plan.offsets == (0.0,)


def test_at_default_ceiling_single_chunk():
    """A file exactly at the default 30-min ceiling fits in one chunk."""
    plan = compute_split_plan(1800.0, max_chunk_secs=1800.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (1800.0,)


def test_just_over_default_ceiling_front_loaded():
    """A 1801s file gets 2 chunks: 1 full (1800s) + tiny tail (1s)."""
    plan = compute_split_plan(1801.0, max_chunk_secs=1800.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (1800.0, 1.0)
    assert plan.offsets == (0.0, 1800.0)


def test_30_min_audio_front_loaded():
    """31 min (1860s) -> 1 full + 1 short tail (60s)."""
    plan = compute_split_plan(1860.0, max_chunk_secs=1800.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (1800.0, 60.0)


# diarize=OFF (60-min API limit, 60-min ceiling) --------------------------


def test_60_min_unit_single_chunk_under_no_diarize_ceiling():
    """A 60-min file (3600s) fits in one chunk under the no-diarize limit."""
    plan = compute_split_plan(3600.0, max_chunk_secs=3600.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (3600.0,)


def test_just_over_no_diarize_ceiling_front_loaded():
    """3601s -> 1 full + 1s tail."""
    plan = compute_split_plan(3601.0, max_chunk_secs=3600.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3600.0, 1.0)


def test_70_min_audio_two_chunks_under_no_diarize_ceiling():
    """70 min (4200s) -> 1 full + 600s tail."""
    plan = compute_split_plan(4200.0, max_chunk_secs=3600.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3600.0, 600.0)


def test_3833s_user_example():
    """User-reported 3833.5s file -> 1 full (3600s) + 233.5s tail."""
    plan = compute_split_plan(3833.5, max_chunk_secs=3600.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3600.0, 233.5)
    assert plan.offsets == (0.0, 3600.0)


def test_long_audio_front_loaded_three_full_chunks():
    """5000s @ max=1800 -> 2 full + 1 tail (1400s)."""
    plan = compute_split_plan(5000.0, max_chunk_secs=1800.0)
    assert plan.num_chunks == 3
    assert plan.chunk_secs == (1800.0, 1800.0, 1400.0)


def test_long_audio_never_exceeds_no_diarize_ceiling():
    for total in (1800, 3600, 7200, 18000, 86400):
        plan = compute_split_plan(float(total), max_chunk_secs=3600.0)
        for cs in plan.chunk_secs:
            assert cs <= 3600.0, (
                f"chunk_secs={cs} exceeded 60-min ceiling for total={total}"
            )
        assert sum(plan.chunk_secs) == total or abs(sum(plan.chunk_secs) - total) < 0.01


# Explicit max_chunk_secs (user override) ----------------------------------


def test_explicit_30_sec_chunks_for_120s_file():
    """120s file with max_chunk_secs=30 -> 4 chunks of 30s (last = full size).

    120 / 30 = 4 exactly. The initial num_chunks would be 5 (120//30+1=5),
    but the remainder-round-to-zero loop drops one chunk back to 4, and
    the remaining tail equals a full max-sized chunk.
    """
    plan = compute_split_plan(120.0, max_chunk_secs=30.0)
    assert plan.num_chunks == 4
    assert plan.chunk_secs == (30.0, 30.0, 30.0, 30.0)


def test_explicit_max_chunk_secs_front_loaded_for_90s_file():
    """90s file with max_chunk_secs=29 -> 29 + 29 + 29 + 3 (front-loaded)."""
    plan = compute_split_plan(90.0, max_chunk_secs=29.0)
    assert plan.num_chunks == 4
    assert plan.chunk_secs == (29.0, 29.0, 29.0, 3.0)


def test_explicit_max_chunk_secs_front_loaded_for_184s_file():
    """184s file with max_chunk_secs=59 -> 59 + 59 + 59 + 7 (front-loaded)."""
    plan = compute_split_plan(184.0, max_chunk_secs=59.0)
    assert plan.num_chunks == 4
    assert plan.chunk_secs == (59.0, 59.0, 59.0, 7.0)


def test_explicit_oversized_chunk_for_short_audio():
    """A 60s file with max_chunk_secs=3000 still yields 1 chunk (file fits)."""
    plan = compute_split_plan(60.0, max_chunk_secs=3000.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (60.0,)


def test_explicit_max_chunk_secs_under_total_no_equal_split():
    """When max_chunk_secs < total_secs, the split is front-loaded, NOT equal.

    Regression guard for the old equal-split mode: previously, supplying
    chunk_secs triggered an equal split. The new contract is always
    front-loaded (max-sized chunks + remainder).
    """
    plan = compute_split_plan(300.0, max_chunk_secs=60.0)
    # 300/60 = 5 exactly -> 5 chunks of 60s (remainder rounds to 0).
    assert plan.num_chunks == 5
    assert plan.chunk_secs == (60.0, 60.0, 60.0, 60.0, 60.0)

    plan = compute_split_plan(310.0, max_chunk_secs=60.0)
    # 310/60 = 5.17 -> 5 full + 10 tail.
    assert plan.num_chunks == 6
    assert plan.chunk_secs == (60.0, 60.0, 60.0, 60.0, 60.0, 10.0)


# Offsets ---------------------------------------------------------------


def test_offsets_are_cumulative():
    plan = compute_split_plan(3833.5, max_chunk_secs=3600.0)
    assert plan.offsets == (0.0, 3600.0)


def test_offsets_for_three_chunks():
    plan = compute_split_plan(5000.0, max_chunk_secs=1800.0)
    assert plan.offsets == (0.0, 1800.0, 3600.0)
