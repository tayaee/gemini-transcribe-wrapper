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

# Default (diarize=ON, 30-min API limit, 29-min ceiling) --------------------


def test_short_audio_single_chunk_default_ceiling():
    plan = compute_split_plan(100.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (100.0,)
    assert plan.offsets == (0.0,)


def test_at_default_ceiling_single_chunk():
    """A file exactly at the default 29-min ceiling fits in one chunk."""
    plan = compute_split_plan(1740.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (1740.0,)


def test_just_over_default_ceiling_front_loaded():
    """A 1741s file gets 2 chunks: 1 full (1740s) + tiny tail (1s)."""
    plan = compute_split_plan(1741.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (1740.0, 1.0)
    assert plan.offsets == (0.0, 1740.0)


def test_30_min_audio_front_loaded():
    """30 min (1800s) -> 1 full + 1 short tail (60s)."""
    plan = compute_split_plan(1800.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (1740.0, 60.0)


# diarize=OFF (60-min API limit, 59-min ceiling) --------------------------


def test_59_min_unit_single_chunk_under_no_diarize_ceiling():
    """A 59-min file (3540s) fits in one chunk under the no-diarize limit."""
    plan = compute_split_plan(3540.0, max_chunk_secs=3540.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (3540.0,)


def test_just_over_no_diarize_ceiling_front_loaded():
    """3541s -> 1 full + 1s tail."""
    plan = compute_split_plan(3541.0, max_chunk_secs=3540.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3540.0, 1.0)


def test_60_min_audio_two_chunks_under_no_diarize_ceiling():
    """60 min (3600s) -> 1 full + 60s tail."""
    plan = compute_split_plan(3600.0, max_chunk_secs=3540.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3540.0, 60.0)


def test_3833s_user_example():
    """User-reported 3833.5s file -> 1 full (3540s) + 293.5s tail."""
    plan = compute_split_plan(3833.5, max_chunk_secs=3540.0)
    assert plan.num_chunks == 2
    assert plan.chunk_secs == (3540.0, 293.5)
    assert plan.offsets == (0.0, 3540.0)


def test_long_audio_front_loaded_three_full_chunks():
    """5000s @ max=1740 -> 2 full + 1 tail (1520s)."""
    plan = compute_split_plan(5000.0, max_chunk_secs=1740.0)
    assert plan.num_chunks == 3
    assert plan.chunk_secs == (1740.0, 1740.0, 1520.0)


def test_long_audio_never_exceeds_no_diarize_ceiling():
    for total in (1800, 3540, 3600, 7200, 18000, 86400):
        plan = compute_split_plan(float(total), max_chunk_secs=3540.0)
        for cs in plan.chunk_secs:
            assert cs <= 3540.0, (
                f"chunk_secs={cs} exceeded 59-min ceiling for total={total}"
            )
        assert sum(plan.chunk_secs) == total or abs(sum(plan.chunk_secs) - total) < 0.01


# Explicit chunk_secs (user override) --------------------------------------


def test_explicit_30_sec_chunks_for_120s_file():
    """120s file with chunk_secs=30 -> 4 equal chunks of 30s (last equals rest)."""
    plan = compute_split_plan(120.0, chunk_secs=30.0)
    assert plan.num_chunks == 4
    assert plan.chunk_secs == (30.0, 30.0, 30.0, 30.0)


def test_explicit_target_above_default_ceiling_clamps_count():
    """A 59-min target with the default 29-min ceiling splits into equal chunks."""
    plan = compute_split_plan(3540.0, chunk_secs=3540.0)
    # 3540/3 = 1180 (uniform, since user supplied chunk_secs)
    assert plan.num_chunks == 3
    assert plan.chunk_secs == (1180.0, 1180.0, 1180.0)


def test_explicit_target_above_relaxed_ceiling_stays_single_chunk():
    """A 59-min target with the 59-min ceiling stays at 1 chunk."""
    plan = compute_split_plan(3540.0, chunk_secs=3540.0, max_chunk_secs=3540.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (3540.0,)


def test_explicit_oversized_chunk_for_short_audio():
    """A 60s file with chunk_secs=3000 still yields 1 chunk (file fits)."""
    plan = compute_split_plan(60.0, chunk_secs=3000.0)
    assert plan.num_chunks == 1
    assert plan.chunk_secs == (60.0,)


# Offsets ---------------------------------------------------------------


def test_offsets_are_cumulative():
    plan = compute_split_plan(3833.5, max_chunk_secs=3540.0)
    assert plan.offsets == (0.0, 3540.0)


def test_offsets_for_three_chunks():
    plan = compute_split_plan(5000.0, max_chunk_secs=1740.0)
    assert plan.offsets == (0.0, 1740.0, 3480.0)
