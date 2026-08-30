"""Unit tests for SRT/TXT formatting, including sanitizing bad word timestamps.

The Gemini API occasionally returns a single word whose start_offset and
end_offset look swapped (start > end). Without sanitization, that one bad
word creates a phantom multi-hundred-second gap that collapses the rest of
the transcript into one giant cue, which then gets truncated to two lines
in the SRT — i.e. all the real content silently disappears.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.format import (
    _sanitize_word,
    format_srt,
    group_words_to_cues,
)
from gemini_transcribe_wrapper.stt import Word


def test_sanitize_swaps_swapped_timestamps():
    """When start > end the sanitizer mirrors the fields back into order."""
    bad = Word(text="and", start=990.10, end=10.10)
    fixed = _sanitize_word(bad)
    assert fixed.start == 10.10
    assert fixed.end == 990.10 or fixed.end <= fixed.start + 5.0
    # Either way, end must not exceed a single-word duration after sanitize.
    assert fixed.end - fixed.start <= 5.0


def test_sanitize_keeps_normal_word_intact():
    good = Word(text="hello", start=1.0, end=1.5)
    fixed = _sanitize_word(good)
    assert fixed.start == 1.0
    assert fixed.end == 1.5


def test_sanitize_clamps_unreasonable_duration_to_start():
    """A single word must not be allowed to span multiple seconds."""
    bad = Word(text="huge", start=10.0, end=9999.0)
    fixed = _sanitize_word(bad)
    assert fixed.start == 10.0
    assert fixed.end == 10.0


def test_group_words_handles_phantom_gap_word():
    """One swapped-timestamp word must not collapse the rest into one cue.

    Without the sanitizer, gap(prev.end=9.80, this.start=990.10) = 980.30s
    triggers a new cue, then gap(this.end=10.10, next.start=10.10) = 0s
    keeps all following words in the second cue, producing a 400+ word cue
    that gets truncated to two lines.
    """
    words = [
        Word("service", 8.70, 9.80),
        # Gemini bug: start > end (swapped). The "real" positions are
        # 9.80-10.10 (continuous with neighbours).
        Word("and", 990.10, 10.10),
        Word("from", 10.10, 10.30),
        Word("my", 10.30, 10.60),
        Word("first", 10.60, 11.00),
        Word("days", 11.00, 11.40),
        Word("here", 11.40, 11.80),
    ]
    cues = group_words_to_cues(words)
    # The swapped word must NOT split the cue: all 7 words belong together
    # in a single 8.70-11.80s cue.
    assert len(cues) == 1, f"expected 1 cue, got {len(cues)}"
    assert cues[0].start == 8.70
    assert cues[0].end == 11.80


def test_format_srt_includes_full_transcript_after_fix():
    """End-to-end: with a swapped-timestamp word present, the SRT must still
    produce a cue whose text contains words from before AND after the bad
    word — i.e. no silent truncation to a tiny snippet."""
    words = [
        Word("share", 0.10, 0.30),
        Word("the", 0.30, 0.40),
        Word("stage", 0.40, 0.80),
        Word("service", 8.70, 9.80),
        Word("and", 990.10, 10.10),  # the bug
        Word("from", 10.10, 10.30),
        Word("first", 10.30, 10.60),
        Word("days", 10.60, 11.00),
    ]
    cues = group_words_to_cues(words)
    srt = format_srt(cues)
    # Words from before AND after the phantom-gap word must survive.
    assert "share" in srt
    assert "days" in srt, "post-bug words got silently dropped from SRT"


def test_continuous_speech_splits_into_many_cues():
    """Continuous speech with no pauses > max_gap must still produce many
    short cues, not one giant cue whose text gets truncated to two lines.

    A 16-minute lecture with virtually no pauses is the real-world case: with
    the previous logic, all 2093 words landed in one cue and 99% of the text
    was silently dropped.
    """
    # 50 words, one every 0.5s, no pauses — would have been 1 cue before.
    words = [
        Word(f"w{i}", float(i) * 0.5, float(i) * 0.5 + 0.4) for i in range(50)
    ]
    cues = group_words_to_cues(words)
    # Must produce many cues so no single cue holds all 50 words.
    assert len(cues) >= 4, f"expected several cues, got {len(cues)}"
    # No cue may exceed MAX_WORDS_PER_CUE words' worth of text.
    for cue in cues:
        # Count words in this cue by re-splitting on spaces; the joiner
        # doesn't add spaces around punctuation, so use approximate length.
        assert len(cue.text) <= 120, (
            f"cue text too long ({len(cue.text)} chars): {cue.text!r}"
        )


def test_srt_full_content_for_long_continuous_speech():
    """End-to-end: a long transcript with continuous speech must produce a
    sizeable SRT (every word should appear in some cue), not a tiny file
    where most words are silently dropped.
    """
    words = [
        Word(f"w{i}", float(i) * 0.3, float(i) * 0.3 + 0.25) for i in range(100)
    ]
    cues = group_words_to_cues(words)
    srt = format_srt(cues)
    # Spot-check that words from the start, middle, and end all appear.
    assert "w0" in srt, "first word missing"
    assert "w50" in srt, "middle word missing"
    assert "w99" in srt, "last word missing"


if __name__ == "__main__":
    test_sanitize_swaps_swapped_timestamps()
    test_sanitize_keeps_normal_word_intact()
    test_sanitize_clamps_unreasonable_duration_to_start()
    test_group_words_handles_phantom_gap_word()
    test_format_srt_includes_full_transcript_after_fix()
    test_continuous_speech_splits_into_many_cues()
    test_srt_full_content_for_long_continuous_speech()
    print("PASS: format sanitization tests")