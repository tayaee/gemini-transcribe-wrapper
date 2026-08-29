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


def group_words_to_cues(words: list[Word], max_gap: float = 1.5) -> list[Cue]:
    """Group words into cues, breaking at pauses > max_gap."""
    cues: list[Cue] = []
    if not words:
        return cues
    cur_words = [words[0]]
    for w in words[1:]:
        gap = w.start - cur_words[-1].end
        if gap > max_gap or w.speaker != cur_words[-1].speaker:
            cues.append(_words_to_cue(cur_words))
            cur_words = [w]
        else:
            cur_words.append(w)
    if cur_words:
        cues.append(_words_to_cue(cur_words))
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
    return lines[:MAX_LINES_PER_CUE]


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


def format_speakers_srt(
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
    """Remove common Korean filler words and normalize whitespace."""
    cleaned = text
    # Remove repeated single filler syllables surrounded by spaces
    cleaned = re.sub(r"\b(?:어|음|아)\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def build_txt(
    result: TranscriptionResult,
    line_interval_secs: float = 1.0,
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

    for w in result.words:
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
