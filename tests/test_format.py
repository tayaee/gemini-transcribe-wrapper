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
    build_txt,
    fix_korean_su_text,
    format_srt,
    group_words_to_cues,
    sanitize_words,
)
from gemini_transcribe_wrapper.stt import (
    TranscribeClient,
    TranscriptionResult,
    Word,
)


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


def test_korean_phrase_not_truncated_to_two_lines():
    """Regression: a Korean phrase that fits in MAX_WORDS_PER_CUE but wraps to
    3 lines must still emit the trailing word in the SRT (it used to be
    silently dropped by split_cue_text's [:MAX_LINES_PER_CUE] slice).

    The real-world example was the last sentence of a 16-minute lecture:
    "가장 하단에 요렇게 나오는 것들을 확인하실 수 있을 겝니다." — the SRT
    ended at "있을" while .transcript.json and .txt correctly contained
    "겝니다.".
    """
    K_W1 = chr(0xAC00) + chr(0xC7A5)
    K_W2 = chr(0xD558) + chr(0xB2E8) + chr(0xC5D0)
    K_W3 = chr(0xC694) + chr(0xB807) + chr(0xAC8C)
    K_W4 = chr(0xB098) + chr(0xC624) + chr(0xB294)
    K_W5 = chr(0xAC83) + chr(0xB4E4) + chr(0xC744)
    K_W6 = chr(0xD655) + chr(0xC778) + chr(0xD558) + chr(0xC2E4)
    K_W7 = chr(0xC218)
    K_W8 = chr(0xC788) + chr(0xC744)
    K_TRAIL = chr(0xAC9D) + chr(0xB2C8) + chr(0xB2E4) + "."
    words = [
        Word(K_W1, 0.0, 0.2),
        Word(K_W2, 0.2, 0.5),
        Word(K_W3, 0.5, 0.8),
        Word(K_W4, 0.8, 1.1),
        Word(K_W5, 1.1, 1.4),
        Word(K_W6, 1.4, 1.7),
        Word(K_W7, 1.7, 1.9),
        Word(K_W8, 1.9, 2.1),
        Word(K_TRAIL, 2.1, 2.5),
    ]
    cues = group_words_to_cues(words)
    srt = format_srt(cues)
    assert K_TRAIL in srt, (
        f"trailing word {K_TRAIL!r} was truncated from the SRT:\n{srt}"
    )


def test_fix_korean_su_text():
    assert fix_korean_su_text("먹을 su") == "먹을 수"
    assert fix_korean_su_text("찾을 su") == "찾을 수"
    assert fix_korean_su_text("될 su") == "될 수"
    assert fix_korean_su_text("살 su") == "살 수"
    assert fix_korean_su_text("만들 su") == "만들 수"
    assert fix_korean_su_text("아실 su") == "아실 수"
    assert fix_korean_su_text("하실 su") == "하실 수"
    assert fix_korean_su_text("보실 su") == "보실 수"
    assert fix_korean_su_text("확인하실 su 있습니다") == "확인하실 수 있습니다"
    assert fix_korean_su_text("보실 su도 있습니다") == "보실 수도 있습니다"
    assert fix_korean_su_text("하실 su가 없습니다") == "하실 수가 없습니다"
    assert fix_korean_su_text("아실 su밖에 없습니다") == "아실 수밖에 없습니다"
    assert fix_korean_su_text("할 su 있다") == "할 수 있다"
    assert fix_korean_su_text("볼 su 없다") == "볼 수 없다"
    assert fix_korean_su_text("할 su 도") == "할 수도"
    assert fix_korean_su_text("볼 su 가") == "볼 수가"
    assert fix_korean_su_text("할 su 밖에") == "할 수밖에"
    assert fix_korean_su_text("그럴 su 밖에 없다") == "그럴 수밖에 없다"
    assert fix_korean_su_text("할 Su 도 있다") == "할 수도 있다"
    assert fix_korean_su_text("볼 SU 가 없다") == "볼 수가 없다"
    assert fix_korean_su_text("할 Su 있습니다") == "할 수 있습니다"
    assert fix_korean_su_text("볼 SU 없었습니다") == "볼 수 없었습니다"
    assert fix_korean_su_text("su도") == "수도"
    assert fix_korean_su_text("su가") == "수가"
    assert fix_korean_su_text("su밖에") == "수밖에"
    assert fix_korean_su_text("su있다") == "수있다"
    assert fix_korean_su_text("su없는") == "수없는"
    # Unrelated English text or non-rieul Hangul should not be touched
    assert fix_korean_su_text("하 su") == "하 su"
    assert fix_korean_su_text("su won") == "su won"
    assert fix_korean_su_text("summer is nice") == "summer is nice"


def test_sanitize_words_korean_su_followed_by_iss_or_eops():
    words = [
        Word("먹을", 0.0, 0.5),
        Word("su", 0.5, 0.8),
        Word("찾을", 0.8, 1.2),
        Word("su", 1.2, 1.5),
        Word("있다", 1.5, 1.8),
        Word("아실", 1.8, 2.1),
        Word("su", 2.1, 2.4),
        Word("보실", 2.4, 2.7),
        Word("su", 2.7, 3.0),
        Word("있습니다", 3.0, 3.3),
        Word("확인하실", 3.3, 3.7),
        Word("su", 3.7, 4.0),
        Word("도", 4.0, 4.2),
        Word("할", 4.2, 4.5),
        Word("su", 4.5, 4.8),
        Word("있다", 4.8, 5.1),
        Word("볼", 5.1, 5.4),
        Word("Su,", 5.4, 5.7),
        Word("없습니다", 5.7, 6.0),
        Word("그럴", 6.0, 6.3),
        Word("su", 6.3, 6.5),
        Word("밖에", 6.5, 6.8),
        Word("없다", 6.8, 7.1),
        Word("하", 7.1, 7.3),
        Word("su", 7.3, 7.5),
        Word("won", 7.5, 7.9),
        Word("su있다", 7.9, 8.2),
        Word("su도", 8.2, 8.5),
        Word("su밖에", 8.5, 8.9),
    ]
    cleaned = sanitize_words(words)
    texts = [w.text for w in cleaned]
    assert texts == [
        "먹을", "수",
        "찾을", "수", "있다",
        "아실", "수",
        "보실", "수", "있습니다",
        "확인하실", "수도",
        "할", "수", "있다",
        "볼", "수,", "없습니다",
        "그럴", "수밖에", "없다",
        "하", "su", "won",
        "수있다",
        "수도",
        "수밖에",
    ]


def test_srt_and_txt_with_su_replacement():
    words = [
        Word("할", 0.0, 0.5),
        Word("su", 0.5, 0.8),
        Word("있습니다", 0.8, 1.2),
    ]
    cues = group_words_to_cues(words)
    srt = format_srt(cues)
    assert "할 수 있습니다" in srt
    assert "su" not in srt

    txt = build_txt(TranscriptionResult(text="할 su 있습니다", words=words))
    assert "할 수 있습니다" in txt
    assert "su" not in txt


def test_custom_vocabulary_is_not_sent_to_api():
    """custom_vocabulary is incompatible with timestamps (Gemini API rejects the
    combination, see error 400: 'custom_vocabulary is incompatible with timestamps').
    We always need word-level timestamps for SRT, so the vocab must not be sent.
    The Korean '수' bug is handled by fix_korean_su_text post-processing instead."""
    client = TranscribeClient(
        api_key="fake-key",
        language="ko-KR",
        custom_vocabulary=["특수용어A", "특수용어B"],
    )
    gen_config = client._generation_config()
    tc = gen_config.transcription_config
    assert tc is not None
    # custom_vocabulary must NOT be set on the API config
    assert getattr(tc, "custom_vocabulary", None) is None


if __name__ == "__main__":
    test_sanitize_swaps_swapped_timestamps()
    test_sanitize_keeps_normal_word_intact()
    test_sanitize_clamps_unreasonable_duration_to_start()
    test_group_words_handles_phantom_gap_word()
    test_format_srt_includes_full_transcript_after_fix()
    test_continuous_speech_splits_into_many_cues()
    test_srt_full_content_for_long_continuous_speech()
    test_korean_phrase_not_truncated_to_two_lines()
    test_fix_korean_su_text()
    test_sanitize_words_korean_su_followed_by_iss_or_eops()
    test_srt_and_txt_with_su_replacement()
    test_custom_vocabulary_is_not_sent_to_api()
    print("PASS: format sanitization tests")
