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
    """Per-chunk split for a long audio file.

    ``chunk_secs`` is a list of *variable* per-chunk sizes: chunks are
    filled front-to-back to ``max_chunk_secs`` (e.g. 59 min for no-diarize,
    29 min for diarize), with the final chunk absorbing the remainder.
    """

    num_chunks: int
    chunk_secs: tuple[float, ...]
    total_secs: float

    @property
    def offsets(self) -> tuple[float, ...]:
        """Cumulative start time of each chunk on the global timeline."""
        offsets: list[float] = []
        cum = 0.0
        for cs in self.chunk_secs:
            offsets.append(cum)
            cum += cs
        return tuple(offsets)


def compute_split_plan(
    total_secs: float,
    max_chunk_secs: float,
) -> SplitPlan:
    """Determine chunk split for a given total duration.

    Front-loaded only: the first N-1 chunks fill to ``max_chunk_secs`` and
    the last chunk absorbs the remainder. There is no equal-split option —
    regardless of total length, chunks come out as a sequence of
    ``max_chunk_secs``-sized units followed by a single (smaller) tail.

    This means each chunk is at its maximum allowed size — fewer wasted
    seconds per API call, and the chunk count tracks ``total_secs /
    max_chunk_secs`` exactly.

    Examples (max=1740s, diarize ON):
      - 90s     -> (90.0,)                            (single chunk, file fits)
      - 1740s   -> (1740.0,)                          (single full chunk)
      - 1741s   -> (1740.0, 1.0)                      (1 full + 1s tail)
      - 1800s   -> (1740.0, 60.0)                     (1 full + short tail)
      - 5000s   -> (1740.0, 1740.0, 1520.0)           (2 full + tail)

    Examples (max=3540s, diarize OFF):
      - 3540s   -> (3540.0,)                          (single full chunk)
      - 3600s   -> (3540.0, 60.0)                     (1 full + short tail)
      - 3833.5s -> (3540.0, 293.5)                    (1 full + tail)
    """
    if total_secs <= max_chunk_secs:
        return SplitPlan(
            num_chunks=1,
            chunk_secs=(float(total_secs),),
            total_secs=total_secs,
        )
    num_chunks = max(2, int(total_secs // max_chunk_secs) + 1)

    # Enforce the ceiling: bump num_chunks up if the rounded value would
    # produce a chunk larger than max_chunk_secs.
    while total_secs / num_chunks > max_chunk_secs:
        num_chunks += 1

    return _front_load(total_secs, num_chunks, max_chunk_secs)


def _front_load(
    total_secs: float,
    num_chunks: int,
    max_chunk_secs: float,
) -> SplitPlan:
    """Distribute ``total_secs`` across ``num_chunks`` (front-loaded).

    The first N-1 chunks fill to ``max_chunk_secs`` and the last chunk
    absorbs the remainder. If the remainder happens to round to zero or
    below, we drop a chunk (the previous one was already max-sized).
    """
    if num_chunks <= 1:
        return SplitPlan(
            num_chunks=1,
            chunk_secs=(float(total_secs),),
            total_secs=total_secs,
        )
    full_secs = max_chunk_secs
    remainder = total_secs - full_secs * (num_chunks - 1)
    # If the remainder happens to fit exactly in fewer chunks, drop them.
    while num_chunks > 1 and remainder <= 0:
        num_chunks -= 1
        remainder = total_secs - full_secs * (num_chunks - 1)
    if num_chunks == 1:
        return SplitPlan(
            num_chunks=1,
            chunk_secs=(float(total_secs),),
            total_secs=total_secs,
        )
    sizes = [full_secs] * (num_chunks - 1) + [float(remainder)]
    return SplitPlan(
        num_chunks=num_chunks,
        chunk_secs=tuple(sizes),
        total_secs=total_secs,
    )


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
    """Split full MP3 into N variable-duration chunks: chunk_000.mp3 ... chunk_NNN.mp3.

    Each chunk uses its own size from ``plan.chunk_secs`` and starts at
    ``plan.offsets[idx]``. The last chunk absorbs the remainder.
    """
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    width = max(3, len(str(plan.num_chunks - 1)))
    for idx in range(plan.num_chunks):
        out = chunk_dir / f"chunk_{idx:0{width}d}.mp3"
        if out.exists():
            chunks.append(out)
            continue
        start = plan.offsets[idx]
        duration = plan.chunk_secs[idx]
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
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
