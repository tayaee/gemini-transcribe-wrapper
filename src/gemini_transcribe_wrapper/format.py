"""Output formatting: SRT / SPK / TXT generation with atomic writes."""

from __future__ import annotations

import logging
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .stt import TranscriptionResult, Word

logger = logging.getLogger(__name__)

MAX_CHARS_PER_LINE = 30
MAX_LINES_PER_CUE = 2
MAX_WORDS_PER_CUE = 12
TXT_WRAP_WIDTH = 40


def _fmt_ts(secs: float) -> str:
    """Format seconds as SRT timestamp HH:MM:SS,mmm."""
    secs = max(0.0, secs)
    ms = round((secs - int(secs)) * 1000)
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    speaker: str | None = None


MAX_WORD_DURATION_SECS = 5.0

RIEUL_JONGSEONG_CHARS = "".join(
    chr(0xAC00 + (cho * 21 + jung) * 28 + 8)
    for cho in range(19)
    for jung in range(21)
)


def has_rieul_jongseong(ch: str) -> bool:
    """Return True if character is a complete Hangul syllable with 'ㄹ' (rieul) final consonant."""
    if not ch or len(ch) != 1:
        return False
    code = ord(ch) - 0xAC00
    return 0 <= code <= 11171 and (code % 28 == 8)


def fix_korean_su_text(text: str) -> str:
    """Replace misrecognized 'su' with Korean '수' / '수도' / '수가' / '수밖에'.

    - If preceded by a Hangul syllable with 종성 'ㄹ' (e.g. '할', '볼', '알', '먹을', '아실', '될', etc.)
      or followed by '있', '없', '도', '가', '밖', replaces 'su' with '수' / '수도' / '수가' / '수밖에'.
    """
    if not text:
        return ""
    # 1. 'su 밖에' -> '수밖에', 'su 밖' -> '수밖'
    t = re.sub(r"(?i)\bsu\s*밖에\b", "수밖에", text)
    t = re.sub(r"(?i)\bsu\s*밖", "수밖", t)
    t = re.sub(r"(?i)\bsu(?=밖)", "수", t)
    # 2. 'su 도' -> '수도', 'su 가' -> '수가'
    t = re.sub(r"(?i)\bsu\s*도\b", "수도", t)
    t = re.sub(r"(?i)\bsu\s*가\b", "수가", t)
    # 3. 'su도' -> '수도', 'su가' -> '수가'
    t = re.sub(r"(?i)\bsu(?=[도가])", "수", t)
    # 4. 'su' as a separate word followed by optional whitespace and '있' or '없': e.g. "할 su 있다" -> "할 수 있다"
    t = re.sub(r"(?i)\bsu\b(?=\s*[있없])", "수", t)
    # 5. 'su' attached directly to '있' or '없': e.g. "su있다" -> "수있다"
    t = re.sub(r"(?i)\bsu(?=[있없])", "수", t)
    # 6. Preceded by Hangul syllable with 종성 'ㄹ': e.g. "할 su", "볼 su", "먹을 su", "아실 su", "될 su"
    t = re.sub(rf"(?i)(?<=[{RIEUL_JONGSEONG_CHARS}])\s+su\b", " 수", t)
    t = re.sub(rf"(?i)(?<=[{RIEUL_JONGSEONG_CHARS}])su\b", "수", t)
    return t


def _sanitize_word(w: Word) -> Word:
    """Sanitize a single word's timestamps: swap inverted ``start``/``end`` and clamp excessive duration."""
    start, end = w.start, w.end
    if start > end:
        start, end = end, start
    if end - start > MAX_WORD_DURATION_SECS:
        end = start
    return Word(text=w.text, start=start, end=end, speaker=w.speaker)


def sanitize_words_rule_timestamps(words: list[Word]) -> list[Word]:
    """[규칙 1] 단어 타임스탬프 비정상 값(start > end 역전, MAX_WORD_DURATION_SECS 초과) 보정."""
    return [_sanitize_word(w) for w in words]


_SU_PARTICLE_MAP: tuple[tuple[str, str], ...] = (
    ("밖에", "수밖에"),
    ("도", "수도"),
    ("가", "수가"),
)


def sanitize_words_rule_particle_merge(words: list[Word]) -> list[Word]:
    """[규칙 2] 조사 결합: 독립된 'su' 뒤에 조사('밖에', '도', '가')가 오면 1개 단어로 병합 (예: '수밖에', '수도', '수가')."""
    merged: list[Word] = []
    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        clean_bare = w.text.strip().strip(".,!?:;\"'()[]{}")
        if clean_bare.lower() == "su" and i + 1 < n:
            next_w = words[i + 1]
            next_clean = next_w.text.strip().lstrip(".,!?:;\"'([{")
            matched = False
            for particle, replacement in _SU_PARTICLE_MAP:
                if next_clean == particle or next_clean.startswith(particle):
                    merged_text = re.sub(r"^" + particle, replacement, next_clean)
                    merged.append(
                        Word(
                            text=merged_text,
                            start=w.start,
                            end=next_w.end,
                            speaker=w.speaker or next_w.speaker,
                        )
                    )
                    i += 2
                    matched = True
                    break
            if matched:
                continue

        merged.append(w)
        i += 1
    return merged


def sanitize_words_rule_rieul_preceded(words: list[Word]) -> list[Word]:
    """[규칙 3-A] 선행어 기반: 앞 단어가 관형형 어미 종성 'ㄹ'로 끝나는 경우 독립 'su'를 '수'로 교정 (띄어쓰기 유지)."""
    result: list[Word] = []
    for i, w in enumerate(words):
        clean_bare = w.text.strip().strip(".,!?:;\"'()[]{}")
        if clean_bare.lower() == "su" and i > 0:
            prev_clean = words[i - 1].text.strip().rstrip(".,!?:;\"'()[]{}")
            if prev_clean and has_rieul_jongseong(prev_clean[-1]):
                w = Word(text=re.sub(r"(?i)\bsu\b", "수", w.text), start=w.start, end=w.end, speaker=w.speaker)
        result.append(w)
    return result


def sanitize_words_rule_iss_eops_followed(words: list[Word]) -> list[Word]:
    """[규칙 3-B] 후행 용언 기반: 뒤 단어가 보조용언 '있~' 또는 '없~'으로 시작하는 경우 독립 'su'를 '수'로 교정 (띄어쓰기 유지)."""
    result: list[Word] = []
    n = len(words)
    for i, w in enumerate(words):
        clean_bare = w.text.strip().strip(".,!?:;\"'()[]{}")
        if clean_bare.lower() == "su" and i + 1 < n:
            next_clean = words[i + 1].text.strip().lstrip(".,!?:;\"'([{")
            if next_clean and next_clean[0] in ("있", "없"):
                w = Word(text=re.sub(r"(?i)\bsu\b", "수", w.text), start=w.start, end=w.end, speaker=w.speaker)
        result.append(w)
    return result


def sanitize_words_rule_attached_prefix(words: list[Word]) -> list[Word]:
    """[규칙 4] 붙여쓴 단어: 'su'에 '있/없/도/가/밖'이 붙어있는 단어('su있다', 'su없다', 'su도', 'su가', 'su밖에')의 'su'를 '수'로 교정."""
    result: list[Word] = []
    for w in words:
        if re.match(r"(?i)^su[있없도가밖]", w.text.strip()):
            w = Word(text=re.sub(r"(?i)^su(?=[있없도가밖])", "수", w.text), start=w.start, end=w.end, speaker=w.speaker)
        result.append(w)
    return result


def sanitize_words(words: list[Word]) -> list[Word]:
    """Word-timestamp sanitization and Korean 'su' misrecognition correction pipeline.

    Single-responsibility functions (Rules) executed in order:
      1. sanitize_words_rule_timestamps       : fix timestamp anomalies
      2. sanitize_words_rule_particle_merge    : merge attached particles ('수밖에' / '수도' / '수가')
      3. sanitize_words_rule_rieul_preceded    : standalone 'su' ➡️ '수' (preserves spacing) when previous word ends in ㄹ jongseong
      4. sanitize_words_rule_iss_eops_followed : standalone 'su' ➡️ '수' (preserves spacing) when followed by 있~/없~ verb stem
      5. sanitize_words_rule_attached_prefix   : correct attached misrecognition prefixes
    """
    if not words:
        return []
    words = sanitize_words_rule_timestamps(words)
    words = sanitize_words_rule_particle_merge(words)
    words = sanitize_words_rule_rieul_preceded(words)
    words = sanitize_words_rule_iss_eops_followed(words)
    words = sanitize_words_rule_attached_prefix(words)
    return words


def group_words_to_cues(words: list[Word], max_gap: float = 1.5) -> list[Cue]:
    """Group words into cues, breaking at pauses > max_gap or speaker change.

    Each cue is then split into chunks of at most :data:`MAX_WORDS_PER_CUE`
    words so a run of continuous speech (no pauses) still produces many
    short subtitles instead of one giant cue whose text gets truncated to
    two lines by ``format_srt``.
    """
    cues: list[Cue] = []
    if not words:
        return cues
    clean = sanitize_words(words)
    groups: list[list[Word]] = [[clean[0]]]
    for w in clean[1:]:
        gap = w.start - groups[-1][-1].end
        if gap > max_gap or w.speaker != groups[-1][-1].speaker:
            groups.append([w])
        else:
            groups[-1].append(w)
    for group in groups:
        cues.extend(_split_words_into_cues(group))
    return cues


def _split_words_into_cues(words: list[Word]) -> list[Cue]:
    """Split a word group into Cues so each fits in :data:`MAX_LINES_PER_CUE` lines.

    Splits when either the word count exceeds :data:`MAX_WORDS_PER_CUE` or
    the greedy wrap of the joined text would exceed :data:`MAX_LINES_PER_CUE`
    lines. The wrap simulation is what keeps ``split_cue_text`` from
    silently truncating trailing lines: a 9-word Korean phrase like
    "가장 하단에 요렇게 나오는 것들을 확인하실 수 있을 겝니다." easily fits in
    12 words but wraps to 3 lines, so a plain width check would still let
    ``format_srt`` drop the "겝니다." line entirely. Simulating the same
    greedy fill that ``split_cue_text`` uses catches that case.
    """
    if not words:
        return []

    def _line_count(ws: list[Word]) -> int:
        # Use the uncapped wrapper so we see the true line count; split_cue_text
        # would silently truncate at MAX_LINES_PER_CUE and hide overflow.
        return len(wrap_cue_text(_join_words(ws)))

    cues: list[Cue] = []
    start = 0
    # Invariant: words[start:i] fits in MAX_LINES_PER_CUE lines. We try to
    # add words[i] and close the cue BEFORE adding it would push us over.
    for i in range(1, len(words) + 1):
        candidate = words[start:i]
        if (i - start) > MAX_WORDS_PER_CUE or _line_count(candidate) > MAX_LINES_PER_CUE:
            cues.append(_words_to_cue(words[start:i - 1]))
            start = i - 1
    cues.append(_words_to_cue(words[start:]))
    return cues


def _words_to_cue(words: list[Word]) -> Cue:
    text = _join_words(words)
    speaker = next((w.speaker for w in words if w.speaker), None)
    return Cue(
        start=words[0].start,
        end=words[-1].end,
        text=text,
        speaker=speaker,
    )


def _join_words(words: list[Word]) -> str:
    parts: list[str] = []
    for w in words:
        if not parts or w.text in (".", ",", "!", "?", ")", "]", "}", "%", ":", ";"):
            parts.append(w.text)
        else:
            parts.append(" " + w.text)
    return "".join(parts).strip()


def split_cue_text(text: str) -> list[str]:
    """Split cue text into <=2 lines of <=15 chars, preferring word boundaries."""
    lines = wrap_cue_text(text)
    return lines[:MAX_LINES_PER_CUE]


def wrap_cue_text(text: str) -> list[str]:
    """Wrap cue text into as many lines as needed, each <= MAX_CHARS_PER_LINE width.

    Unlike :func:`split_cue_text`, this does not cap the output at
    :data:`MAX_LINES_PER_CUE`: callers that need to emit a single cue should
    truncate via :func:`split_cue_text`, but internal splitting logic uses
    this uncapped version so it can see the true line count and rebalance
    before any content is silently dropped.
    """
    text = text.strip()
    if not text:
        return []

    def _char_len(s: str) -> int:
        # Korean characters are double-width; treat them as 2 for readability.
        return sum(2 if "\uac00" <= c <= "\ud7a3" else 1 for c in s)

    def _wrap_line(line: str) -> list[str]:
        if _char_len(line) <= MAX_CHARS_PER_LINE:
            return [line]
        # Greedy fill keeping char-width <= limit
        out: list[str] = []
        cur = ""
        for tok in line.split():
            candidate = (cur + " " + tok).strip()
            if cur and _char_len(candidate) > MAX_CHARS_PER_LINE:
                out.append(cur)
                cur = tok
            else:
                cur = candidate
        if cur:
            out.append(cur)
        return out

    lines: list[str] = []
    for line in text.split("\n"):
        lines.extend(_wrap_line(line))
    return lines


def format_srt(cues: list[Cue]) -> str:
    out: list[str] = []
    for i, cue in enumerate(cues, 1):
        lines = split_cue_text(cue.text)
        if not lines:
            continue
        out.append(str(i))
        out.append(f"{_fmt_ts(cue.start)} --> {_fmt_ts(cue.end)}")
        out.extend(lines)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def format_diarized_srt(
    cues: list[Cue], speaker_map: dict[str, str] | None = None
) -> str:
    """Format speaker-diarized subtitles as an .srt (media-player compatible).

    Standard SRT cue layout with a speaker tag prefix, e.g.:
    `[궤도] 네. 이번 시간엔`. speaker_map maps raw speaker ids (e.g. "spk:0")
    to display names; speakers missing from the map keep their raw id.
    """
    out: list[str] = []
    for i, cue in enumerate(cues, 1):
        lines = split_cue_text(cue.text)
        if not lines:
            continue
        raw = cue.speaker or ""
        label = speaker_map.get(raw, raw) if speaker_map and raw else raw
        speaker_tag = f"[{label}] " if label else ""
        out.append(str(i))
        out.append(f"{_fmt_ts(cue.start)} --> {_fmt_ts(cue.end)}")
        out.append(speaker_tag + ("\n".join(lines)))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _clean_for_txt(text: str) -> str:
    """Remove common Korean filler words, fix 'su' -> '수', and normalize whitespace."""
    cleaned = fix_korean_su_text(text)
    # Remove repeated single filler syllables surrounded by spaces
    cleaned = re.sub(r"\b(?:어|음|아)\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def build_txt(
    result: TranscriptionResult,
    line_interval_secs: float = 0.9,
    paragraph_interval_secs: float = 2.5,
) -> str:
    """Build editor-formatted text from word timestamps with break rules.

    - Gap >= paragraph_interval_secs -> paragraph break (\n\n)
    - Gap >= line_interval_secs      -> single newline (\n)
    - 60-char line wrap via textwrap; max 1 consecutive blank line.
    """
    if not result.words:
        return textwrap.fill(
            _clean_for_txt(result.text), width=TXT_WRAP_WIDTH, break_long_words=False
        ) + "\n"

    sanitized_words = sanitize_words(result.words)
    paragraphs: list[str] = []
    lines: list[str] = []
    line_words: list[str] = []
    last_end: float | None = None

    def flush_line() -> None:
        if line_words:
            lines.append(" ".join(line_words))
            line_words.clear()

    def flush_paragraph() -> None:
        flush_line()
        if lines:
            paragraphs.append("\n".join(lines))
            lines.clear()

    for w in sanitized_words:
        if last_end is not None:
            gap = w.start - last_end
            if gap >= paragraph_interval_secs:
                flush_paragraph()
            elif gap >= line_interval_secs:
                flush_line()
        line_words.append(w.text)
        last_end = w.end

    flush_paragraph()

    wrapped: list[str] = []
    for para in paragraphs:
        for line in para.split("\n"):
            wrapped.append(
                textwrap.fill(line, width=TXT_WRAP_WIDTH, break_long_words=False)
            )
        wrapped.append("")

    text = "\n".join(wrapped)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
