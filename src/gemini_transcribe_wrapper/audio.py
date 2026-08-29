"""Audio helpers: normalization, duration probing, and equal chunk splitting via bundled static ffmpeg."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import static_ffmpeg

    static_ffmpeg.add_paths()
except Exception:  # noqa: BLE001, S110 - best-effort environment init
    pass


class FFmpegError(RuntimeError):
    pass


def _run_ff(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            "ffmpeg/ffprobe not found. Install the 'static-ffmpeg' package or add ffmpeg to PATH."
        ) from exc
    if proc.returncode != 0:
        raise FFmpegError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )
    return proc.stdout


def probe_duration_secs(media_path: Path) -> float:
    """Return media duration in seconds using ffprobe."""
    out = _run_ff(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media_path),
        ]
    )
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise FFmpegError(f"Failed to read duration of {media_path}") from exc


@dataclass(frozen=True)
class SplitPlan:
    num_chunks: int
    chunk_secs: float


def compute_split_plan(total_secs: float, chunk_secs: float | None = None) -> SplitPlan:
    """Determine equal chunk split for a given total duration.

    When chunk_secs is provided it is used as the chunk length directly
    (rounding to a whole number of chunks, remainder absorbed by the last
    chunk). Otherwise the default algorithm applies: no chunk exceeds 25
    minutes, splitting edge remainders equally (e.g. 26 min -> 13m+13m).
    """
    if chunk_secs is not None and chunk_secs > 0:
        num_chunks = max(1, round(total_secs / chunk_secs))
        return SplitPlan(num_chunks=num_chunks, chunk_secs=total_secs / num_chunks)

    if total_secs <= 1500.0:
        return SplitPlan(num_chunks=1, chunk_secs=total_secs)
    num = 2
    while total_secs / num > 1500.0:
        num += 1
    return SplitPlan(num_chunks=num, chunk_secs=total_secs / num)


def extract_audio(input_path: Path, out_mp3: Path, force: bool = False) -> None:
    """Normalize input to 16kHz mono 64kbps MP3 via bundled ffmpeg."""
    if out_mp3.exists() and not force:
        return
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    _run_ff(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-b:a",
            "64k",
            str(out_mp3),
        ]
    )


def split_chunks(full_mp3: Path, chunk_dir: Path, plan: SplitPlan) -> list[Path]:
    """Split full MP3 into N equal-duration chunks: chunk_000.mp3 ... chunk_NNN.mp3."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    width = max(3, len(str(plan.num_chunks - 1)))
    for idx in range(plan.num_chunks):
        out = chunk_dir / f"chunk_{idx:0{width}d}.mp3"
        if out.exists():
            chunks.append(out)
            continue
        start = idx * plan.chunk_secs
        # -t with -ss: give last chunk exact remaining duration to avoid
        # cutting mid-stream; use segment copy is not possible for mp3
        # so re-encode with same normalization settings for consistency.
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{plan.chunk_secs:.3f}",
            "-i",
            str(full_mp3),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-b:a",
            "64k",
            str(out),
        ]
        _run_ff(cmd)
        chunks.append(out)
    return chunks


_CHUNK_RE = re.compile(r"^chunk_(\d+)\.mp3$")


def existing_chunks(chunk_dir: Path) -> list[Path]:
    """Return chunk files sorted by numeric index."""
    if not chunk_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for p in chunk_dir.iterdir():
        m = _CHUNK_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    found.sort()
    return [p for _, p in found]
