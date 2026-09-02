"""Merge chunk results and commit outputs atomically."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import overload

from .format import (
    Cue,
    atomic_write,
    build_txt,
    format_diarized_srt,
    format_srt,
    group_words_to_cues,
)
from .stt import MODEL_ID, TranscriptionResult, Word

logger = logging.getLogger(__name__)

_OFF_OUTPUT_TOKENS: frozenset[str] = frozenset(
    {"", "no", "off", "false", "none", "0"}
)


@overload
def _resolve_output_target(
    value: None, default_path: Path, default_enabled: bool
) -> tuple[bool, Path]: ...


@overload
def _resolve_output_target(
    value: str | Path | bool, default_path: Path, default_enabled: bool
) -> tuple[bool, Path]: ...


def _resolve_output_target(
    value: str | Path | bool | None,
    default_path: Path,
    default_enabled: bool,
) -> tuple[bool, Path]:
    """Normalize an output-file flag to ``(enabled, final_path)``.

    - ``None`` (flag omitted) → (default_enabled, default_path).
    - ``False`` or string in ``{"", "no", "off", "false", "none", "0"}`` → (False, default_path).
    - ``True`` or string ``"auto"`` (case-insensitive) → (True, default_path).
    - non-empty string or ``Path`` → (True, Path(value)).

    Returns ``(enabled, final_path)``.
    """
    if value is None:
        return default_enabled, default_path
    if value is False:
        return False, default_path
    if value is True:
        return True, default_path
    if isinstance(value, str):
        val_clean = value.strip().lower()
        if val_clean in _OFF_OUTPUT_TOKENS:
            return False, default_path
        if val_clean == "auto":
            return True, default_path
        return True, Path(value)
    return True, Path(value)


def merge_cues(
    results: list[TranscriptionResult],
    chunk_secs: list[float] | tuple[float, ...] | float = (),
) -> list[Cue]:
    """Merge per-chunk word cues, offsetting each chunk by its start time."""
    if isinstance(chunk_secs, (int, float)):
        durations: list[float] | tuple[float, ...] = [float(chunk_secs)] * len(results)
    else:
        durations = chunk_secs
    all_words = []
    cum = 0.0
    for idx, res in enumerate(results):
        offset = cum
        for w in res.words:
            all_words.append(
                w.__class__(
                    text=w.text,
                    start=w.start + offset,
                    end=w.end + offset,
                    speaker=w.speaker,
                )
            )
        if idx < len(durations):
            cum += float(durations[idx])
    if not all_words:
        return []
    return group_words_to_cues(all_words)


def align_and_build(
    results: list[TranscriptionResult],
    chunk_secs: list[float] | tuple[float, ...],
    full_mp3: Path,
    out_base: Path,
    srt_tmp: Path,
    diarized_srt_tmp: Path | None,
    txt_tmp: Path,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    skip_sync: bool = False,
    speakers: dict[str, str] | None = None,
    txt_width: int = 65,
) -> None:
    """Build .srt.tmp/.diarized.srt.tmp/.txt.tmp from the transcript.

    The main .srt/.diarized.srt keep the raw transcript timestamps.
    speakers maps raw speaker ids (e.g. "spk:0") to display names.
    ``diarized_srt_tmp`` may be None when the caller is in plain (no-diarize)
    mode; in that case the diarized SRT is skipped entirely.
    """
    cues = merge_cues(results, chunk_secs)

    srt_content = format_srt(cues)
    diarized_srt_content = format_diarized_srt(cues, speaker_map=speakers)
    txt_content = build_txt(
        _merged_result(results, chunk_secs),
        line_interval_secs=line_interval_secs,
        paragraph_interval_secs=paragraph_interval_secs,
        txt_width=txt_width,
    )

    atomic_write(srt_tmp, srt_content)
    if diarized_srt_tmp is not None:
        atomic_write(diarized_srt_tmp, diarized_srt_content)
    atomic_write(txt_tmp, txt_content)


def _merged_result(
    results: list[TranscriptionResult],
    chunk_secs: list[float] | tuple[float, ...] = (),
) -> TranscriptionResult:
    """Concatenate chunk results, offsetting word timestamps by chunk position."""
    all_words: list = []
    text_parts: list[str] = []
    cum = 0.0
    for idx, res in enumerate(results):
        offset = cum
        text_parts.append(res.text)
        for w in res.words:
            all_words.append(
                Word(
                    text=w.text,
                    start=w.start + offset,
                    end=w.end + offset,
                    speaker=w.speaker,
                )
            )
        if idx < len(chunk_secs):
            cum += float(chunk_secs[idx])
    return TranscriptionResult(text="".join(text_parts), words=all_words)


def build_metadata_json(
    results: list[TranscriptionResult],
    chunk_secs: list[float] | tuple[float, ...],
    model: str = MODEL_ID,
) -> str:
    """Build merged .metadata.json content from chunk transcription results.

    Contains the full transcript text plus per-chunk word-level details with
    absolute timestamps and speaker labels.
    """
    chunks = []
    cum = 0.0
    for idx, res in enumerate(results):
        offset = cum
        chunks.append(
            {
                "chunk_index": idx,
                "text": res.text,
                "words": [
                    {
                        "text": w.text,
                        "start": w.start + offset,
                        "end": w.end + offset,
                        "speaker": w.speaker,
                    }
                    for w in res.words
                ],
            }
        )
        if idx < len(chunk_secs):
            cum += float(chunk_secs[idx])
    return json.dumps(
        {"model": model, "chunks": chunks},
        ensure_ascii=False,
        indent=2,
    )


def commit_outputs(
    targets: dict[str, tuple[bool, Path]],
    tmp_paths: dict[str, Path],
    cleanup_patterns: list[str],
    chunk_mp3s: list[Path],
) -> list[str]:
    """Atomically rename tmp outputs to finals, then apply cleanup filters.

    - ``targets`` maps each output key to ``(enabled, final_path)``.
      ``enabled=True`` means "produce this output"; ``False`` means
      "delete any stale final + tmp file at that location".
    - ``tmp_paths`` maps each output key to its transient ``.tmp`` file
      in the work dir. Only consulted when ``enabled=True``.

    Both dicts share the same key set (``"srt"``, ``"txt"``,
    ``"diarized_srt"``, ``"metadata_json"``).
    """
    produced: list[str] = []
    for key, (enabled, final) in targets.items():
        final_path = Path(final)
        if not enabled:
            tmp = tmp_paths.get(key)
            if tmp and Path(tmp).exists():
                Path(tmp).unlink()
            if final_path.exists():
                final_path.unlink()
            continue
        tmp = tmp_paths.get(key)
        if tmp is None or not Path(tmp).exists():
            continue
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(Path(tmp), final_path)
        produced.append(str(final_path))

    # Cleanup filters (run unconditionally — they target stray temp files
    # that don't follow the targets/tmp_paths contract).
    for pattern in cleanup_patterns:
        for p in Path(".").glob(pattern):
            if p.is_file():
                p.unlink()

    # Delete chunk mp3s on success
    for chunk in chunk_mp3s:
        try:
            chunk.unlink(missing_ok=True)
        except OSError:
            pass
    return produced
