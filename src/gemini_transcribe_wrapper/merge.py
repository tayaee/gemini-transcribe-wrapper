"""Merge chunk results, run ffsubsync alignment, and commit outputs atomically."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from .format import (
    Cue,
    atomic_write,
    build_txt,
    format_diarized_srt,
    format_srt,
    group_words_to_cues,
)
from .stt import TranscriptionResult, Word

logger = logging.getLogger(__name__)


def merge_cues(
    results: list[TranscriptionResult], chunk_secs: list[float] | tuple[float, ...]
) -> list[Cue]:
    """Merge per-chunk word cues, offsetting each chunk by its start time."""
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
        if idx < len(chunk_secs):
            cum += float(chunk_secs[idx])
    if not all_words:
        return []
    return group_words_to_cues(all_words)


def run_ffsubsync(srt_path: Path, audio_mp3: Path) -> Path | None:
    """Align SRT timestamps to the full audio via ffsubsync.

    Uses the robust recipe: uvx --python 3.13 ffsubsync <audio> -i <srt>
    --max-offset-seconds=120 --gss --overwrite-input. The input srt is
    overwritten with the aligned version in place.
    """
    try:
        from ffsubsync import ffsubsync  # noqa: F401 - ensures package present
    except ImportError:
        logger.warning("ffsubsync not available; skipping subtitle alignment")
        return None

    # Copy the srt to a temp file; --overwrite-input edits it in place.
    work_srt = srt_path.with_name(srt_path.stem + ".sync" + srt_path.suffix)
    shutil.copyfile(srt_path, work_srt)

    cmd = [
        "uvx",
        "--python",
        "3.13",
        "ffsubsync",
        str(audio_mp3),
        "-i",
        str(work_srt),
        "--max-offset-seconds=120",
        "--gss",
        "--overwrite-input",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if work_srt.exists() and work_srt.stat().st_size > 0 and proc.returncode == 0:
            shutil.copyfile(work_srt, srt_path)
            return srt_path
        if proc.returncode != 0:
            logger.warning(
                "ffsubsync exited %d: %s", proc.returncode, proc.stderr[-500:]
            )
    except Exception:  # noqa: BLE001 - fall back to unaligned
        logger.warning("ffsubsync failed; keeping original timestamps")
    finally:
        try:
            work_srt.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _apply_delta_to_srt(srt_path: Path, delta: float) -> str:
    """Shift all SRT timestamps by delta seconds."""
    import re

    def _shift(match: re.Match) -> str:
        parts = match.group(0).replace(",", ".").split(" --> ")
        out = []
        for ts in parts:
            h, m, s = ts.split(":")
            secs = int(h) * 3600 + int(m) * 60 + float(s) + delta
            secs = max(0.0, secs)
            ms = round((secs - int(secs)) * 1000)
            total = int(secs)
            hh, rem = divmod(total, 3600)
            mm, ss = divmod(rem, 60)
            out.append(f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}")
        return " --> ".join(out)

    return re.sub(
        r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}",
        _shift,
        srt_path.read_text(encoding="utf-8"),
    )


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
    ffsubsync_srt: bool = False,
) -> None:
    """Build .srt.tmp/.diarized.srt.tmp/.txt.tmp from the transcript.

    The main .srt/.diarized.srt keep the raw transcript timestamps. When
    ffsubsync_srt is True, an additional "<base>.ffsubsync.srt" is written
    with timestamps aligned to the full audio for manual comparison.

    If skip_sync is True (e.g. re-rendering from a transcript without the
    source audio), the ffsubsync extra file is skipped. speakers maps raw
    speaker ids (e.g. "spk:0") to display names. ``diarized_srt_tmp`` may be
    None when the caller is in plain (no-diarize) mode; in that case the
    diarized SRT is skipped entirely.
    """
    cues = merge_cues(results, chunk_secs)

    srt_content = format_srt(cues)
    diarized_srt_content = format_diarized_srt(cues, speaker_map=speakers)
    txt_content = build_txt(
        _merged_result(results, chunk_secs),
        line_interval_secs=line_interval_secs,
        paragraph_interval_secs=paragraph_interval_secs,
    )

    atomic_write(srt_tmp, srt_content)
    if diarized_srt_tmp is not None:
        atomic_write(diarized_srt_tmp, diarized_srt_content)
    atomic_write(txt_tmp, txt_content)

    # Optionally produce an ffsubsync-aligned SRT as an extra file, leaving
    # the main outputs untouched.
    if ffsubsync_srt and not skip_sync and full_mp3.exists():
        srt_for_sync = srt_tmp.with_name(srt_tmp.name[: -len(".tmp")])
        srt_for_sync.write_text(srt_content, encoding="utf-8")
        aligned = run_ffsubsync(srt_for_sync, full_mp3)
        if aligned is not None and aligned.exists():
            extra = srt_tmp.with_name(
                srt_tmp.name[: -len(".srt.tmp")] + ".ffsubsync.srt"
            )
            atomic_write(extra, aligned.read_text(encoding="utf-8"))
        try:
            srt_for_sync.unlink(missing_ok=True)
        except OSError:
            pass


def _estimate_delta(original: Path, aligned: Path) -> float:
    """Estimate per-cue delta between original and aligned SRT (average)."""
    def _timestamps(path: Path) -> list[float]:
        import re

        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(
                r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})",
                line,
            )
            if m:
                out.append(
                    int(m.group(1)) * 3600
                    + int(m.group(2)) * 60
                    + int(m.group(3))
                    + int(m.group(4)) / 1000
                )
        return out

    orig = _timestamps(original)
    algn = _timestamps(aligned)
    if not orig or not algn or len(orig) != len(algn):
        return 0.0
    deltas = [a - o for o, a in zip(orig, algn)]
    return sum(deltas) / len(deltas)


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
        {"model": "gemini-3.5-transcribe", "chunks": chunks},
        ensure_ascii=False,
        indent=2,
    )


def commit_outputs(
    outputs: dict[str, Path],
    create_diarized_srt: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
    cleanup_patterns: list[str],
    chunk_mp3s: list[Path],
) -> list[str]:
    """Atomically rename tmp outputs to finals, then apply cleanup filters."""
    produced: list[str] = []
    mapping = {
        "diarized_srt": create_diarized_srt,
        "srt": create_srt,
        "txt": create_txt,
        "metadata_json": create_metadata_json,
    }
    for key, enabled in mapping.items():
        tmp = outputs.get(key + "_tmp")
        final = outputs.get(key)
        if not enabled:
            if tmp and tmp.exists():
                tmp.unlink()
            if final and final.exists():
                final.unlink()
            continue
        if tmp is None or final is None:
            continue
        os.replace(tmp, final)
        produced.append(str(final))

    # Cleanup filters
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
