"""Merge chunk results, run ffsubsync alignment, and commit outputs atomically."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .format import (
    Cue,
    atomic_write,
    build_txt,
    format_spk,
    format_srt,
    group_words_to_cues,
)
from .stt import TranscriptionResult, Word

logger = logging.getLogger(__name__)


def merge_cues(results: list[TranscriptionResult], chunk_secs: float) -> list[Cue]:
    """Merge per-chunk word cues, offsetting each chunk by its start time."""
    all_words = []
    for idx, res in enumerate(results):
        offset = idx * chunk_secs
        for w in res.words:
            all_words.append(
                w.__class__(
                    text=w.text,
                    start=w.start + offset,
                    end=w.end + offset,
                    speaker=w.speaker,
                )
            )
    if not all_words:
        return []
    return group_words_to_cues(all_words)


def run_ffsubsync(srt_path: Path, audio_mp3: Path, srt_out: Path) -> Path | None:
    """Align SRT timestamps to the full audio via ffsubsync.

    Returns path to the aligned SRT, or None if alignment was skipped/failed.
    """
    try:
        from ffsubsync import ffsubsync  # noqa: F401 - ensures package present
    except ImportError:
        logger.warning("ffsubsync not available; skipping subtitle alignment")
        return None

    tmp = srt_out.with_name(srt_out.stem + ".aligned" + srt_out.suffix)
    tmp.parent.mkdir(parents=True, exist_ok=True)

    ffsubsync_bin = shutil.which("ffsubsync")
    if ffsubsync_bin is None:
        # Console script not on PATH (e.g. uv tool install): resolve next to
        # the active interpreter (bin/ on POSIX, Scripts/ on Windows).
        exe_dir = Path(sys.executable).parent
        candidates = [exe_dir / "ffsubsync", exe_dir / "Scripts" / "ffsubsync.exe"]
        for c in candidates:
            if c.exists():
                ffsubsync_bin = str(c)
                break
    if ffsubsync_bin is None:
        logger.warning("ffsubsync executable not found; skipping subtitle alignment")
        return None

    cmd = [
        ffsubsync_bin,
        str(audio_mp3),
        "-i",
        str(srt_path),
        "-o",
        str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if tmp.exists() and tmp.stat().st_size > 0:
            return tmp
        # Empty or failed alignment: discard output, keep original timestamps.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if proc.returncode != 0:
            logger.warning(
                "ffsubsync exited %d: %s", proc.returncode, proc.stderr[-500:]
            )
    except Exception:  # noqa: BLE001 - fall back to unaligned
        logger.warning("ffsubsync failed; keeping original timestamps")
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
    chunk_secs: float,
    full_mp3: Path,
    out_base: Path,
    srt_tmp: Path,
    spk_tmp: Path,
    txt_tmp: Path,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    skip_sync: bool = False,
    speakers: dict[str, str] | None = None,
) -> None:
    """Build .srt.tmp/.spk.tmp/.txt.tmp, then align SRT/SPK with ffsubsync.

    If skip_sync is True (e.g. re-rendering from a transcript without the
    source audio), timestamps are used as-is. speakers maps raw speaker ids
    (e.g. "spk:0") to display names for the .spk output.
    """
    cues = merge_cues(results, chunk_secs)

    srt_content = format_srt(cues)
    spk_content = format_spk(cues, speaker_map=speakers)
    txt_content = build_txt(
        _merged_result(results, chunk_secs),
        line_interval_secs=line_interval_secs,
        paragraph_interval_secs=paragraph_interval_secs,
    )

    atomic_write(srt_tmp, srt_content)
    atomic_write(spk_tmp, spk_content)
    atomic_write(txt_tmp, txt_content)

    # ffsubsync requires a known subtitle extension (e.g. .srt), so run it on a
    # properly-suffixed intermediate file and fold the result back into .tmp.
    # srt_tmp is "<base>.srt.tmp" -> sync file is "<base>.srt".
    if not skip_sync:
        srt_for_sync = srt_tmp.with_name(srt_tmp.name[: -len(".tmp")])
        srt_for_sync.write_text(srt_content, encoding="utf-8")
        aligned = run_ffsubsync(srt_for_sync, full_mp3, srt_for_sync)
        if aligned is not None and aligned.exists():
            # Compute average shift from original SRT and apply to SPK as well.
            delta = _estimate_delta(srt_for_sync, aligned)
            os.replace(aligned, srt_for_sync)
            atomic_write(srt_tmp, srt_for_sync.read_text(encoding="utf-8"))
            if delta:
                shifted = _apply_delta_to_srt(spk_tmp, delta)
                atomic_write(spk_tmp, shifted)
        try:
            srt_for_sync.unlink(missing_ok=True)
            if aligned is not None and aligned.exists():
                aligned.unlink(missing_ok=True)
            else:
                # Remove any leftover ffsubsync output file.
                leftover = srt_for_sync.with_name(
                    srt_for_sync.stem + ".aligned" + srt_for_sync.suffix
                )
                leftover.unlink(missing_ok=True)
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


def _merged_result(results: list[TranscriptionResult], chunk_secs: float = 0.0) -> TranscriptionResult:
    """Concatenate chunk results, offsetting word timestamps by chunk position."""
    all_words: list = []
    text_parts: list[str] = []
    for idx, res in enumerate(results):
        offset = idx * chunk_secs
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
    return TranscriptionResult(text="".join(text_parts), words=all_words)


def build_metadata_json(results: list[TranscriptionResult], chunk_secs: float) -> str:
    """Build merged .metadata.json content from chunk transcription results.

    Contains the full transcript text plus per-chunk word-level details with
    absolute timestamps and speaker labels.
    """
    chunks = []
    for idx, res in enumerate(results):
        offset = idx * chunk_secs
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
    return json.dumps(
        {"model": "gemini-3.5-transcribe", "chunks": chunks},
        ensure_ascii=False,
        indent=2,
    )


def commit_outputs(
    outputs: dict[str, Path],
    create_spk: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
    cleanup_patterns: list[str],
    chunk_mp3s: list[Path],
) -> list[str]:
    """Atomically rename tmp outputs to finals, then apply cleanup filters."""
    produced: list[str] = []
    mapping = {
        "spk": create_spk,
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
