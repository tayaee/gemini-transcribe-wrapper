"""Public Python API: gemini_transcribe() orchestration."""

from __future__ import annotations

import glob as globlib
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

from ._key_utils import api_key_tail
from .audio import compute_split_plan, extract_audio, probe_duration_secs, split_chunks
from .merge import (
    _OFF_OUTPUT_TOKENS,
    _resolve_output_target,
    align_and_build,
    build_metadata_json,
    commit_outputs,
)
from .models import (
    BatchTranscribeResult,
    TranscribeInput,
    TranscribeLeftover,
    TranscribeOutput,
    TranscribeResult,
    TranscribeStatus,
)
from .stt import (
    _GLOBAL_DEAD_POOL,
    MODEL_ID,
    TranscribeClient,
    get_audit_log_path,
    load_transcript,
    load_transcript_chunk_secs,
    save_transcript,
    transcribe_chunks_sequential,
)

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """Raised when the Gemini API returns 429 (rate limit / quota).

    Unlike other transcription failures, this is not a per-file condition —
    hitting the quota means subsequent files in the same batch will also
    fail, so the wrapper aborts the entire batch rather than continuing
    to make API calls that will only hit the same limit.
    """

    def __init__(self, source: Path | str, original: Exception) -> None:
        self.source = source
        self.original = original
        super().__init__(f"{source}: {original}")


def _find_auto_vocabulary_file(input_file: Path | str | None) -> Path | None:
    """Find a candidate .vocab.txt file automatically.

    Searches in order:
    1. ``<stem>.vocab.txt`` in the input file's directory (e.g. ``video.vocab.txt``)
    2. ``<filename>.vocab.txt`` in the input file's directory (e.g. ``video.mp4.vocab.txt``)
    3. ``.vocab.txt`` in the input file's directory
    4. ``.vocab.txt`` in the current working directory
    """
    candidates: list[Path] = []
    if input_file:
        in_p = Path(input_file).resolve()
        candidates.append(in_p.with_suffix(".vocab.txt"))
        candidates.append(in_p.parent / f"{in_p.name}.vocab.txt")
        candidates.append(in_p.parent / ".vocab.txt")
    candidates.append(Path.cwd() / ".vocab.txt")

    seen: set[Path] = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if cand.is_file():
                return cand
        except (OSError, ValueError):
            continue
    return None


def _load_vocabulary_file(
    path: str | Path | None = "auto",
    input_file: Path | str | None = None,
) -> list[str]:
    """Load custom-vocabulary terms from a text file (one per line).

    - If ``path`` is ``"auto"`` (default): automatically search for a
      matching vocabulary file (e.g. ``<stem>.vocab.txt``,
      ``<dir>/.vocab.txt``, or ``./.vocab.txt``). If found, loads terms;
      otherwise silently returns ``[]``.
    - If ``path`` is an off token (e.g. ``"off"``, ``"none"``, ``"false"``):
      returns ``[]`` without searching.
    - If ``path`` is empty or ``None``:
      If ``input_file`` is given, behaves as ``"auto"``.
      If neither is given, returns ``[]``.
    - If ``path`` is an explicit path that is missing: logs a warning and
      returns ``[]`` (never raises).
    - Blank lines and lines beginning with ``#`` (after stripping) are
      silently skipped (treat them as comments).
    """
    if path is not None:
        val = str(path).strip().lower()
        if val in _OFF_OUTPUT_TOKENS:
            return []
        if val == "auto":
            auto_file = _find_auto_vocabulary_file(input_file)
            if not auto_file:
                return []
            logging.getLogger(__name__).info(
                "Auto detected .vocab file: %s", auto_file
            )
            p = auto_file
        elif not val:
            return []
        else:
            p = Path(path)
    else:
        if input_file is not None:
            auto_file = _find_auto_vocabulary_file(input_file)
            if not auto_file:
                return []
            logging.getLogger(__name__).info(
                "Auto detected .vocab file: %s", auto_file
            )
            p = auto_file
        else:
            return []

    if not p.is_file():
        logging.getLogger(__name__).warning(
            "Custom vocabulary file not found: %s. Ignoring.", path,
        )
        return []
    items: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    return items


def parse_speakers(content: str) -> dict[str, str]:
    """Parse speaker mappings from text content or spec.

    Expected format is one speaker mapping per line:
        spk:0=John Doe
        spk:1=Jane Doe
        [spk:0]=John Doe:
        [spk:0] =John Doe:

    Also tolerates multiple semicolon-separated mappings on the same line:
        spk:0=John Doe; spk:1=Jane Doe

    Lines starting with '#' and empty entries are ignored.
    Spaces around '=' are preserved so exact pattern matching (including spaces)
    works for string replacement.
    """
    mapping: dict[str, str] = {}
    for line in content.splitlines():
        line_clean = line.rstrip("\r\n")
        if not line_clean.strip() or line_clean.strip().startswith("#"):
            continue
        for part in line_clean.split(";"):
            if not part.strip() or part.strip().startswith("#"):
                continue
            if "=" not in part:
                raise ValueError(f"Invalid speaker entry (expected id=name): {part.strip()!r}")
            raw, name = part.split("=", 1)
            raw = raw.lstrip()
            name = name.rstrip()
            if not raw.strip() or not name.strip():
                raise ValueError(f"Invalid speaker entry (expected id=name): {part.strip()!r}")
            mapping[raw] = name
    return mapping


def _find_auto_speakers_file(input_file: Path | str | None) -> Path | None:
    """Find a candidate .speakers.txt file automatically.

    Searches in order:
    1. ``<stem>.speakers.txt`` in the input file's directory (e.g. ``video.speakers.txt``)
    2. ``<filename>.speakers.txt`` in the input file's directory (e.g. ``video.mp4.speakers.txt``)
    3. ``.speakers.txt`` in the input file's directory
    4. ``.speakers.txt`` in the current working directory
    """
    candidates: list[Path] = []
    if input_file:
        in_p = Path(input_file).resolve()
        candidates.append(in_p.with_suffix(".speakers.txt"))
        candidates.append(in_p.parent / f"{in_p.name}.speakers.txt")
        candidates.append(in_p.parent / ".speakers.txt")
    candidates.append(Path.cwd() / ".speakers.txt")

    seen: set[Path] = set()
    for cand in candidates:
        try:
            resolved = cand.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if cand.is_file():
                return cand
        except (OSError, ValueError):
            continue
    return None


def _load_speakers_file(
    path: str | Path | None = "auto",
    input_file: Path | str | None = None,
) -> dict[str, str]:
    """Load speaker mapping from a text file.

    - If ``path`` is ``"auto"`` (default): automatically search for a
      matching speaker file (e.g. ``<stem>.speakers.txt``,
      ``.speakers.txt``, etc.). If found, loads mapping;
      otherwise silently returns ``{}``.
    - If ``path`` is an off token (e.g. ``"off"``, ``"none"``, ``"false"``):
      returns ``{}`` without searching.
    - If ``path`` is empty or ``None``:
      If ``input_file`` is given, behaves as ``"auto"``.
      If neither is given, returns ``{}``.
    - If ``path`` is an explicit path that is missing: logs a warning and
      returns ``{}`` (never raises).
    """
    if path is not None:
        val = str(path).strip().lower()
        if val in _OFF_OUTPUT_TOKENS:
            return {}
        if val == "auto":
            auto_file = _find_auto_speakers_file(input_file)
            if not auto_file:
                return {}
            logging.getLogger(__name__).info(
                "Auto detected .speakers file: %s", auto_file
            )
            p = auto_file
        elif not val:
            return {}
        else:
            p = Path(path)
    else:
        if input_file is not None:
            auto_file = _find_auto_speakers_file(input_file)
            if not auto_file:
                return {}
            logging.getLogger(__name__).info(
                "Auto detected .speakers file: %s", auto_file
            )
            p = auto_file
        else:
            return {}

    if not p.is_file():
        logging.getLogger(__name__).warning(
            "Speaker mapping file not found: %s. Ignoring.", path,
        )
        return {}
    try:
        content = p.read_text(encoding="utf-8")
        return parse_speakers(content)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logging.getLogger(__name__).warning(
            "Failed to parse speaker mapping file %s: %s. Ignoring.", path, exc
        )
        return {}

# Default chunk length per diarize mode. The user can still override via
# ``chunk_secs``; these are only the fallbacks when the user doesn't specify.
# The Gemini-3.5-transcribe API caps audio per call differently depending
# on whether speaker diarization or word-level timestamps are requested
# (60 min when neither is active, 30 min when either is active).
# - Neither diarize nor word timestamps: 3600s (60 min) — file fits in one
#   API call up to 1 hour.
# - Diarize OR word timestamps: 1800s (30 min) — shorter chunks stay under
#   the 30-min per-call limit.
DEFAULT_CHUNK_SECS_NO_DIARIZE = 3600.0
DEFAULT_CHUNK_SECS_DIARIZE = 1800.0

TRANSCRIPT_SUFFIX_DIARIZED = ".diarized.transcript.json"
TRANSCRIPT_SUFFIX_PLAIN = ".transcript.json"


@dataclass
class WorkContext:
    input_file: Path
    output_dir: Path
    output_base: str
    work_dir: Path
    full_mp3: Path
    chunk_dir: Path


def _setup_workdir(
    input_file: Path, output_dir: Path, output_base: str, temp_path: str | None
) -> WorkContext:
    if temp_path:
        base_dir = Path(temp_path)
        if not base_dir.is_absolute():
            base_dir = output_dir / base_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        work_dir = base_dir / f"{output_base}.gemini-work"
    else:
        work_dir = output_dir / f".{output_base}.gemini-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    full_mp3 = work_dir / "temp_audio.mp3"
    chunk_dir = work_dir / "chunks"
    return WorkContext(
        input_file=input_file,
        output_dir=output_dir,
        output_base=output_base,
        work_dir=work_dir,
        full_mp3=full_mp3,
        chunk_dir=chunk_dir,
    )


def _expand_path(pattern: str) -> list[str]:
    """Expand glob/path patterns (Windows cmd/PowerShell compat)."""
    if globlib.has_magic(pattern):
        matches = sorted(globlib.glob(pattern, recursive=True))
        return matches
    return [pattern]


def _resolve_transcript_path(
    out_dir: Path, out_stem: str, diarize: bool
) -> tuple[Path, Path | None]:
    """Pick the canonical transcript path and flag any legacy fallback.

    When ``diarize`` is True the canonical name is
    ``<stem>.diarized.transcript.json``; otherwise it is the plain
    ``<stem>.transcript.json``. For backward compat with transcripts written
    by older versions (which always wrote the plain name), we also accept
    the alternate name as a fallback. When a fallback is found, the second
    element of the returned tuple is the fallback path so the caller can
    migrate it (rename to the canonical name) after a successful save.

    Returns ``(canonical_path, fallback_path_or_None)``.
    """
    canonical = out_dir / (
        f"{out_stem}{TRANSCRIPT_SUFFIX_DIARIZED}"
        if diarize
        else f"{out_stem}{TRANSCRIPT_SUFFIX_PLAIN}"
    )
    alternate = out_dir / (
        f"{out_stem}{TRANSCRIPT_SUFFIX_PLAIN}"
        if diarize
        else f"{out_stem}{TRANSCRIPT_SUFFIX_DIARIZED}"
    )
    if canonical.exists():
        return canonical, None
    if alternate.exists():
        return alternate, canonical  # will be migrated
    return canonical, None


def gemini_transcribe(
    input_file: str,
    output_dir: str | None = None,
    output_base: str | None = None,
    gemini_api_keys: list[str] | None = None,
    gemini_api_key: str | None = None,  # deprecated single-key alias
    srt_file: str | Path | bool | None = None,
    txt_file: str | Path | bool | None = None,
    transcript_json_file: str | Path | bool | None = None,
    audit_jsonl_file: str | Path | bool | None = None,
    diarized_srt_file: str | Path | bool | None = None,
    metadata_json_file: str | Path | bool | None = None,
    tier: str = "free",
    force: bool = False,
    force_all: bool = False,
    line_interval_secs: float = 0.2,
    paragraph_interval_secs: float = 2.5,
    request_interval_secs: float | None = None,
    max_chunk_secs: float | None = None,
    speakers: dict[str, str] | None = None,
    speakers_txt_file: str | None = "auto",
    temp_path: str | None = "temp",
    custom_vocabulary: list[str] | None = None,
    custom_vocabulary_file: str | None = "auto",
    language_codes: list[str] | None = None,
    model: str = MODEL_ID,
    word_level_timestamps: bool = True,
    txt_width: int = 65,
    # Backward compatibility aliases
    create_transcript_json: bool | None = None,
    create_metadata_json: bool | None = None,
) -> BatchTranscribeResult:
    """A free video transcription CLI using gemini-3.5-transcribe that outputs .diarized.srt, .srt, and .txt files.

    Cross-platform (auto ffmpeg dependencies).

    Args:
        input_file: Input file path or glob pattern.
        output_dir: Directory for final output files (default: input dir).
        output_base: Base name for outputs (default: input stem).
        gemini_api_keys: Ordered list of Gemini API keys for round-robin
            usage and 429 fallback (default: $GEMINI_API_KEYS, or
            $GEMINI_API_KEY / $GOOGLE_API_KEY as a single-key fallback).
            Each chunk consumes one key in round-robin order; on a 429
            the wrapper retries with cooldown on the same key, then falls
            through to the next key until either one succeeds or all
            keys are exhausted. Each key's daily call count is tracked
            independently.
        gemini_api_key: Deprecated single-key alias for ``gemini_api_keys``.
            If given, the key is appended to the list (after any explicit
            ``gemini_api_keys`` entries). Prefer ``gemini_api_keys``.
        language_codes: Ordered list of BCP-47 language hints forwarded
            to Gemini as ``language_codes``. When ``None`` or empty,
            the wrapper omits the field and lets Gemini auto-detect
            the spoken language. Default: ``["ko-KR", "en-US"]``.
        srt_file: SRT output target (default: 'auto'). 'off' to disable; path to override.
        txt_file: TXT output target (default: 'auto'). 'off' to disable; path to override.
        transcript_json_file: Transcript JSON output target (default: 'auto'). 'off' to disable; path to override.
        audit_jsonl_file: Audit JSONL output target (default: 'auto'). 'off' to disable; path to override.
        diarized_srt_file: Diarized SRT output target (default: 'auto'). 'off' to disable; path to override.
        metadata_json_file: Metadata JSON output target (default: 'off'). 'auto' or path to enable.
        tier: Gemini API pricing tier ("free" or "paid", default: "free").
            When "free", enforces 60s cooldown between API calls.
            When "paid", cooldown is 0s unless overridden by ``request_interval_secs``.
        force: Re-process even if all outputs already exist.
        line_interval_secs / paragraph_interval_secs: TXT break gaps.
        request_interval_secs: Delay between STT API calls. Defaults to 120.0s for
            "free" tier, 0.0s for "paid" tier.
        max_chunk_secs: Optional per-chunk ceiling in seconds (developer/internal
            only). Overrides the default for the chosen diarization mode
            (3600s off, 1800s on). A hard ceiling of 1800s is enforced when
            speaker diarization or word-level timestamps are enabled because
            the Gemini API caps audio at ~30 min per call in those modes.
        word_level_timestamps: Include word-level timestamps in the
            transcription output (default: ``True``). When enabled, the
            Gemini API per-call audio limit drops from ~1 hour to ~30 min,
            same as speaker diarization.
        speakers: Optional mapping of raw speaker ids (e.g. "spk:0") to
            display names used in the .diarized.srt output. Speakers missing
            from the mapping keep their raw id, and a warning is emitted
            listing them. Ignored unless ``diarized_srt_file`` resolves to
            enabled.
        temp_path: Where to place intermediate work files (default: 'temp').
            When set, all temp files (temp_audio.mp3, chunk_*.mp3, *.tmp,
            checkpoints) live under this directory instead of next to the output.

    Returns:
        BatchTranscribeResult with per-input TranscribeResult items. Each item
        carries `input` (echo of inputs/options), `output` (requested final
        outputs only), `leftover` (info files and kept-on-failure work files),
        `status` (SUCCESS or FAILED), and `error` (when failed).

    Raises:
        QuotaExceededError: When a Gemini API call fails with HTTP 429 / quota
            exhaustion. Aborts the batch immediately instead of continuing.
    """
    if create_transcript_json is not None and transcript_json_file is None:
        transcript_json_file = create_transcript_json
    if create_metadata_json is not None and metadata_json_file is None:
        metadata_json_file = create_metadata_json

    # Merge the deprecated single-key kwarg into the new list kwarg.
    merged_keys: list[str] = []
    if gemini_api_keys:
        merged_keys.extend(k.strip() for k in gemini_api_keys if k and k.strip())
    if gemini_api_key and gemini_api_key.strip() and gemini_api_key.strip() not in merged_keys:
        merged_keys.append(gemini_api_key.strip())

    from ._logging import silence_noisy_loggers

    silence_noisy_loggers()

    effective_interval = (
        (0.0 if tier == "paid" else 120.0)
        if request_interval_secs is None
        else float(request_interval_secs)
    )
    inputs = _expand_path(input_file)
    # Resolve every output target up front. The same Path is reused per
    # input; explicit custom paths only make sense for single-input
    # batches, so flag that case before starting work.
    explicit_path_used = any(
        _is_explicit_output_path(v)
        for v in (
            srt_file,
            txt_file,
            transcript_json_file,
            audit_jsonl_file,
            diarized_srt_file,
            metadata_json_file,
        )
    )

    if explicit_path_used and len(inputs) > 1:
        raise ValueError(
            "Explicit output file paths require a "
            f"single input file; got {len(inputs)} matches for {input_file!r}."
        )
    results = []
    for path in inputs:
        results.append(
            _process_one(
                input_path=path,
                output_dir=output_dir,
                output_base=output_base,
                gemini_api_keys=merged_keys,
                language_codes=language_codes,
                srt_file=srt_file,
                txt_file=txt_file,
                transcript_json_file=transcript_json_file,
                audit_jsonl_file=audit_jsonl_file,
                diarized_srt_file=diarized_srt_file,
                metadata_json_file=metadata_json_file,
                tier=tier,
                force=force,
                force_all=force_all,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                request_interval_secs=effective_interval,
                max_chunk_secs=max_chunk_secs,
                speakers=speakers,
                speakers_txt_file=speakers_txt_file,
                temp_path=temp_path,
                custom_vocabulary=custom_vocabulary,
                custom_vocabulary_file=custom_vocabulary_file,
                model=model,
                word_level_timestamps=word_level_timestamps,
                txt_width=txt_width,
            )
        )
    return BatchTranscribeResult(results=results)


def _is_explicit_output_path(value: object) -> bool:
    """True when ``value`` is a non-sentinel, non-None output-target value.

    Used by ``gemini_transcribe`` to reject multi-input batches that pass
    an explicit output path; passing None, True, False, "auto", or a disabled token
    is fine for any number of inputs because each input still gets its own
    default-named output.
    """
    if value is None or value is False or value is True:
        return False
    if isinstance(value, str):
        val = value.strip().lower()
        return val not in _OFF_OUTPUT_TOKENS and val != "auto"
    return True


def _process_one(
    input_path: str,
    output_dir: str | None,
    output_base: str | None,
    gemini_api_keys: list[str] | None,
    srt_file: str | Path | bool | None,
    txt_file: str | Path | bool | None,
    transcript_json_file: str | Path | bool | None,
    audit_jsonl_file: str | Path | bool | None,
    diarized_srt_file: str | Path | bool | None,
    metadata_json_file: str | Path | bool | None,
    tier: str,
    force: bool,
    force_all: bool,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    request_interval_secs: float,
    max_chunk_secs: float | None,
    speakers: dict[str, str] | None,
    temp_path: str | None,
    speakers_txt_file: str | None = "auto",
    custom_vocabulary: list[str] | None = None,
    custom_vocabulary_file: str | None = "auto",
    language_codes: list[str] | None = None,
    model: str = MODEL_ID,
    word_level_timestamps: bool = True,
    txt_width: int = 65,
) -> TranscribeResult:
    input_file = Path(input_path)

    out_stem = Path(output_base) if output_base else Path(input_file.stem)
    out_dir = Path(output_dir) if output_dir else input_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve output targets into (enabled, final_path) pairs.
    srt_default = out_dir / f"{out_stem}.srt"
    txt_default = out_dir / f"{out_stem}.txt"
    diarized_default = out_dir / f"{out_stem}.diarized.srt"
    metadata_default = out_dir / f"{out_stem}.metadata.json"
    audit_default = get_audit_log_path()

    srt_enabled, srt_target = _resolve_output_target(
        srt_file, srt_default, default_enabled=True
    )
    txt_enabled, txt_target = _resolve_output_target(
        txt_file, txt_default, default_enabled=True
    )
    diarized_enabled, diarized_target = _resolve_output_target(
        diarized_srt_file, diarized_default, default_enabled=True
    )
    metadata_enabled, metadata_target = _resolve_output_target(
        metadata_json_file, metadata_default, default_enabled=False
    )
    audit_enabled, audit_target = _resolve_output_target(
        audit_jsonl_file, audit_default, default_enabled=True
    )

    transcript_canonical, transcript_migrate_to = _resolve_transcript_path(
        out_dir, out_stem.name, diarized_enabled
    )
    transcript_enabled, transcript_target = _resolve_output_target(
        transcript_json_file, transcript_canonical, default_enabled=True
    )
    if transcript_target != transcript_canonical:
        transcript_path = transcript_target
        transcript_migrate_to = None
    else:
        transcript_path = transcript_canonical

    echo = TranscribeInput(
        input_file=str(input_file),
        output_dir=str(out_dir) if output_dir else None,
        output_base=out_stem.name,
        language_codes=list(language_codes) if language_codes else None,
        srt_file=str(srt_file) if srt_file is not None else None,
        txt_file=str(txt_file) if txt_file is not None else None,
        transcript_json_file=str(transcript_json_file) if transcript_json_file is not None else None,
        audit_jsonl_file=str(audit_jsonl_file) if audit_jsonl_file is not None else None,
        diarized_srt_file=str(diarized_srt_file) if diarized_srt_file is not None else None,
        metadata_json_file=str(metadata_json_file) if metadata_json_file is not None else None,
        tier=tier,
        force=force,
        force_all=force_all,
        temp_path=temp_path,
        line_interval_secs=line_interval_secs,
        paragraph_interval_secs=paragraph_interval_secs,
        request_interval_secs=request_interval_secs,
    )

    # Load speaker mapping from file if configured, and merge with explicit dict
    file_speakers = _load_speakers_file(speakers_txt_file, input_file=input_file)
    combined_speakers = dict(file_speakers)
    if speakers:
        combined_speakers.update(speakers)
    speakers = combined_speakers or None

    # speakers is meaningless without speaker diarization.
    if speakers and not diarized_enabled:
        logger.warning(
            "--speakers / --speakers-txt-file was passed but --diarized-srt-file is disabled; "
            "ignoring the speaker map. Enable --diarized-srt-file to use speaker mapping."
        )
        speakers = None

    if not input_file.is_file():
        logger.error("Input file not found: %s", input_file)
        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.NOT_FOUND,
            error=f"Input file not found: {input_file}",
        )

    # Blacklist check (issue-002, spec §4.2): if this file was previously
    # observed to produce a non-429 error, skip silently until the TTL
    # elapses. Avoids wasting API quota on poisoned inputs (corrupted,
    # unsupported codec, file too large, etc.).
    if gemini_api_keys:
        from .blacklist import InputBlacklist
        from .usage_counter import cache_dir

        first_key = gemini_api_keys[0]
        key_tail = api_key_tail(first_key)
        bl = InputBlacklist(
            path=input_file,
            cache_dir=cache_dir() / key_tail,
            ttl_secs=21_600,
        )
        if bl.is_blacklisted():
            logger.info(
                "Skipping %s: file is on the input blacklist (non-429 "
                "error within the last 6h). Use --force to bypass.",
                input_file,
            )
            return TranscribeResult(
                input=echo,
                status=TranscribeStatus.BLACKLISTED,
                error="file is on the input blacklist (non-429 error)",
            )

    lock_path = out_dir / f"{out_stem.name}.lck"
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        logger.warning("Lock %s held by another process; skipping %s", lock_path, input_file)
        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.LOCKED,
            error=f"Lock held by another process: {lock_path}",
        )

    final_paths = _build_output_paths(
        out_dir,
        out_stem.name,
        srt_enabled,
        txt_enabled,
        diarized_enabled,
        metadata_enabled,
        srt_target,
        txt_target,
        diarized_target,
        metadata_target,
    )
    chunks: list[Path] = []
    ctx: WorkContext | None = None

    try:
        if force_all:
            # Delete cached transcript(s) so we force a fresh API call.
            for tp in (transcript_path, transcript_canonical, transcript_migrate_to):
                if tp and tp.exists():
                    try:
                        tp.unlink()
                        logger.info("Deleted cached transcript %s for --force-all", tp)
                    except OSError as exc:
                        logger.warning("Failed to delete cached transcript %s: %s", tp, exc)

        # Re-render path: a valid transcript exists and the outputs are stale
        # relative to it (missing, or the transcript is newer). Regenerate
        # .diarized.srt/.srt/.txt from the stored transcript without calling
        # the API.
        if not force_all and load_transcript(transcript_path) is not None and (
            force or not _outputs_valid(final_paths, [transcript_path])
        ):
            logger.info("Transcript found; re-rendering outputs from %s", transcript_path)
            try:
                return _render_from_transcript(
                    echo=echo,
                    out_dir=out_dir,
                    out_stem=out_stem.name,
                    transcript_path=transcript_path,
                    srt_enabled=srt_enabled,
                    txt_enabled=txt_enabled,
                    diarized_enabled=diarized_enabled,
                    metadata_enabled=metadata_enabled,
                    srt_target=srt_target,
                    txt_target=txt_target,
                    diarized_target=diarized_target,
                    metadata_target=metadata_target,
                    transcript_enabled=transcript_enabled,
                    line_interval_secs=line_interval_secs,
                    paragraph_interval_secs=paragraph_interval_secs,
                    speakers=speakers,
                    model=model,
                    force=force,
                    txt_width=txt_width,
                )
            except Exception as exc:  # noqa: BLE001 - fall through to full re-run
                logger.warning("Re-render from transcript failed (%s); re-transcribing", exc)

        # Skip condition: outputs are valid only when they all exist AND were
        # generated after the source file. If any target is missing or the
        # source is newer, regenerate.
        if not (force or force_all) and _outputs_valid(final_paths, [input_file]):
            logger.info(
                "Outputs exist and are newer than source; skipping %s "
                "(use --force to redo)",
                input_file,
            )
            return TranscribeResult(
                input=echo,
                status=TranscribeStatus.SKIPPED,
                output=TranscribeOutput(
                    srt=str(srt_target) if srt_enabled else None,
                    txt=str(txt_target) if txt_enabled else None,
                    diarized_srt=str(diarized_target) if diarized_enabled else None,
                    metadata_json=str(metadata_target) if metadata_enabled else None,
                    transcript_json=str(transcript_target) if transcript_enabled and transcript_path.exists() else None,
                ),
            )

        ctx = _setup_workdir(input_file, out_dir, out_stem.name, temp_path)

        try:
            _check_api_key(gemini_api_keys)
        except RuntimeError as exc:
            err_msg = str(exc) or repr(exc)
            logger.error("Failed processing %s: %s", input_file, err_msg)
            return TranscribeResult(
                input=echo, status=TranscribeStatus.FAILED, error=err_msg
            )

        total_secs = probe_duration_secs(input_file)
        logger.info("%s duration: %.1fs", input_file, total_secs)
        # The Gemini API has a different per-call audio limit depending on
        # whether speaker diarization or word-level timestamps are requested
        # (60 min when neither is active, 30 min when either is active), which
        # serve as the default per-chunk ceiling passed to compute_split_plan.
        needs_short_chunks = diarized_enabled or word_level_timestamps
        default_max_chunk_secs = (
            DEFAULT_CHUNK_SECS_DIARIZE if needs_short_chunks else DEFAULT_CHUNK_SECS_NO_DIARIZE
        )
        # Front-loaded split: every chunk is at the ceiling, last chunk
        # absorbs the remainder. No equal-split mode.
        effective_max_chunk_secs = (
            max_chunk_secs if max_chunk_secs is not None else default_max_chunk_secs
        )
        if max_chunk_secs is None:
            logger.info(
                "Per-chunk ceiling: %.1fs (1800s for .srt or .diarized.srt, or 3600s for .txt will be used by default)",
                default_max_chunk_secs,
            )
        else:
            logger.info(
                "Per-chunk ceiling: %.1fs (source: user-supplied --max-chunk-secs; default is 1800s for .srt or .diarized.srt, or 3600s for .txt)",
                max_chunk_secs,
            )
        plan = compute_split_plan(
            total_secs,
            max_chunk_secs=effective_max_chunk_secs,
        )
        logger.info("Split plan: %s", _format_split_plan(plan))

        extract_audio(input_file, ctx.full_mp3, force=(force or force_all))
        chunks = split_chunks(ctx.full_mp3, ctx.chunk_dir, plan)

        # Merge inline list + file-loaded terms into a single vocab. The
        # file load is non-fatal: a missing file just yields [] and logs a
        # warning (see ``_load_vocabulary_file``).
        combined_vocab = list(custom_vocabulary or [])
        combined_vocab += _load_vocabulary_file(
            custom_vocabulary_file, input_file=input_file
        )
        combined_vocab = combined_vocab or None

        client = TranscribeClient(
            api_keys=gemini_api_keys,
            language_codes=language_codes,
            enable_diarization=diarized_enabled,
            request_interval_secs=request_interval_secs,
            tier=tier,
            custom_vocabulary=combined_vocab,
            source_file=str(input_file.resolve()),
            audit_jsonl_file=(
                audit_target
                if _is_explicit_output_path(audit_jsonl_file)
                else (None if audit_enabled else False)
            ),
            model=model,
            word_level_timestamps=word_level_timestamps,
        )
        results = transcribe_chunks_sequential(
            client,
            chunks,
            request_interval_secs=request_interval_secs,
        )

        srt_tmp = ctx.work_dir / f"{ctx.output_base}.srt.tmp"
        # Only generate the diarized SRT tmp when diarized_srt_file is on;
        # otherwise the tmp/final pair would be created and immediately
        # unlinked by commit_outputs, wasting a write.
        diarized_srt_tmp: Path | None = None
        if diarized_enabled:
            diarized_srt_tmp = ctx.work_dir / f"{ctx.output_base}.diarized.srt.tmp"
        txt_tmp = ctx.work_dir / f"{ctx.output_base}.txt.tmp"
        align_and_build(
            results,
            chunk_secs=plan.chunk_secs,
            full_mp3=ctx.full_mp3,
            out_base=ctx.work_dir / ctx.output_base,
            srt_tmp=srt_tmp,
            diarized_srt_tmp=diarized_srt_tmp,
            txt_tmp=txt_tmp,
            line_interval_secs=line_interval_secs,
            paragraph_interval_secs=paragraph_interval_secs,
            speakers=speakers,
            txt_width=txt_width,
        )

        # Save the transcript (full transcription result for later re-render),
        # including the API call logs for this transcription.
        if transcript_enabled:
            language_header = (
                ",".join(language_codes) if language_codes else "auto"
            )
            save_transcript(
                transcript_path,
                results,
                plan.chunk_secs,
                language_header,
                api_logs=getattr(client, "api_logs", None),
            )
            key_tail = api_key_tail(getattr(client, "api_key", None))
            logger.info(  # nosemgrep: python-logger-credential-disclosure - key is masked via api_key_tail
                "api-key=%s Created %s",
                key_tail,
                transcript_path,
            )
        else:
            try:
                transcript_path.unlink(missing_ok=True)
            except OSError:
                pass

        # Per-chunk checkpoints are informational files; delete unless the
        # caller requested the merged metadata output.
        if not metadata_enabled:
            for chunk in chunks:
                meta = chunk.with_suffix(".metadata.json")
                if meta.exists():
                    meta.unlink()

        # Tell commit_outputs how to finalize every output key. Disabled
        # entries are still in targets (with enabled=False) so commit can
        # clean up any pre-existing final + the .tmp it just wrote; enabled
        # entries get tmp_paths entries for the rename.
        targets: dict[str, tuple[bool, Path]] = {
            "srt": (srt_enabled, srt_target),
            "txt": (txt_enabled, txt_target),
        }
        tmp_paths: dict[str, Path] = {
            "srt": srt_tmp,
            "txt": txt_tmp,
        }
        if diarized_enabled:
            assert diarized_srt_tmp is not None  # for type checkers
            targets["diarized_srt"] = (True, diarized_target)
            tmp_paths["diarized_srt"] = diarized_srt_tmp
        if metadata_enabled:
            metadata_tmp = ctx.work_dir / f"{ctx.output_base}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, plan.chunk_secs, model=model), encoding="utf-8"
            )
            targets["metadata_json"] = (True, metadata_target)
            tmp_paths["metadata_json"] = metadata_tmp

        produced = commit_outputs(
            targets=targets,
            tmp_paths=tmp_paths,
            cleanup_patterns=[],
            chunk_mp3s=chunks,
        )
        _cleanup_workdir(ctx, keep_chunks=False)

        # Migrate legacy transcript (if we read from the alternate name) by
        # moving it to the canonical location. Done after commit so a failure
        # here doesn't leave two files lying around.
        if (
            transcript_migrate_to is not None
            and transcript_path != transcript_migrate_to
            and transcript_path.exists()
        ):
            try:
                os.replace(transcript_path, transcript_migrate_to)
                transcript_path = transcript_migrate_to
            except OSError:
                logger.warning(
                    "Could not migrate legacy transcript %s to %s; both will remain.",
                    transcript_path,
                    transcript_migrate_to,
                )

        key_tail = api_key_tail(getattr(client, "api_key", None))
        for out_file in produced:
            logger.info(  # nosemgrep: python-logger-credential-disclosure - key is masked via api_key_tail
                "api-key=%s Created %s",
                key_tail,
                out_file,
                extra={"color": "green"},
            )

        # Warn about speakers the custom mapping did not cover, with a
        # recommended re-render command.
        if speakers and diarized_enabled:
            _warn_unmapped_speakers(
                echo, results, plan.chunk_secs, out_dir, out_stem.name, speakers,
                diarized_target,
            )

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=TranscribeOutput(
                srt=str(srt_target) if srt_enabled and str(srt_target) in produced else None,
                txt=str(txt_target) if txt_enabled and str(txt_target) in produced else None,
                diarized_srt=str(diarized_target) if diarized_enabled and str(diarized_target) in produced else None,
                metadata_json=str(metadata_target) if metadata_enabled and str(metadata_target) in produced else None,
                transcript_json=str(transcript_target) if transcript_enabled and transcript_path.exists() else None,
            ),
        )
    except Exception as exc:
        # 429 / quota errors are not a per-file condition: retrying the next
        # file will hit the same limit. Abort the batch immediately so the
        # caller doesn't burn more quota on calls that are guaranteed to
        # fail. The hint was already logged in stt.transcribe_chunk.
        #
        # Single-key exception (issue-003, spec §3): when only one API key
        # is configured, a 429 means the *file* couldn't be processed but
        # the next file (or the next batch, after the cooldown) is still
        # worth attempting. Report ``SKIPPED_QUOTA`` for this input and
        # keep the batch going.
        from .stt import _is_quota_error

        if _is_quota_error(exc):
            if len(gemini_api_keys or []) == 1:
                logger.warning(
                    "Skipping %s: quota / rate limit hit while processing "
                    "with the only configured API key. The batch will "
                    "continue with the remaining files.",
                    input_file,
                )
                if ctx is not None:
                    leftover = _collect_leftover(
                        ctx, chunks=chunks or None, keep_chunks=True
                    )
                else:
                    leftover = TranscribeLeftover()
                return TranscribeResult(
                    input=echo,
                    status=TranscribeStatus.SKIPPED_QUOTA,
                    error=str(exc),
                    leftover=leftover,
                )
            logger.error(
                "Aborting batch: quota / rate limit hit while processing %s. "
                "Remaining files will not be processed.",
                input_file,
            )
            raise QuotaExceededError(input_file, exc) from exc
        # On failure: keep intermediate mp3 files for resume. Report them as
        # leftover so the caller can clean up if desired. Also blacklist
        # the input file (issue-002, spec §4.2) so subsequent passes skip
        # it instead of burning more API quota on the same poison input.
        err_msg = str(exc) or repr(exc)
        logger.error("Failed processing %s: %s", input_file, err_msg)
        if gemini_api_keys:
            from .blacklist import InputBlacklist
            from .stt import _extract_status_code
            from .usage_counter import cache_dir

            first_key = gemini_api_keys[0]
            key_tail = api_key_tail(first_key)
            bl = InputBlacklist(
                path=input_file,
                cache_dir=cache_dir() / key_tail,
                ttl_secs=21_600,
            )
            try:
                bl.add(status=_extract_status_code(exc))
            except Exception as bl_exc:  # noqa: BLE001 - best-effort
                logger.debug("Blacklist add failed for %s: %s", input_file, bl_exc)
        if ctx is not None:
            leftover = _collect_leftover(ctx, chunks=chunks or None, keep_chunks=True)
        else:
            leftover = TranscribeLeftover()
        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.FAILED,
            error=str(exc),
            leftover=leftover,
        )
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001, S110 - best-effort lock release
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _render_from_transcript(
    echo: TranscribeInput,
    out_dir: Path,
    out_stem: str,
    transcript_path: Path,
    srt_enabled: bool,
    txt_enabled: bool,
    diarized_enabled: bool,
    metadata_enabled: bool,
    srt_target: Path,
    txt_target: Path,
    diarized_target: Path,
    metadata_target: Path,
    transcript_enabled: bool,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    speakers: dict[str, str] | None,
    model: str = MODEL_ID,
    force: bool = False,
    txt_width: int = 65,
) -> TranscribeResult:
    """Regenerate .diarized.srt/.srt/.txt from a transcript (no API call).

    When ``force=True``, every enabled output is regenerated regardless
    of staleness (matches the CLI's ``--force`` semantics). Otherwise the
    staleness check is honored: outputs newer than the transcript are
    left untouched and reported as "Unchanged".
    """
    results = load_transcript(transcript_path)
    if results is None:
        raise ValueError(f"Invalid transcript: {transcript_path}")
    chunk_secs = load_transcript_chunk_secs(transcript_path)

    # Render into temp files first (atomic commit at the end).
    with tempfile.TemporaryDirectory(prefix=f".{out_stem}.render-", dir=str(out_dir)) as tmp:
        work = Path(tmp)
        srt_tmp = work / f"{out_stem}.srt.tmp"
        diarized_srt_tmp: Path | None = None
        if diarized_enabled:
            diarized_srt_tmp = work / f"{out_stem}.diarized.srt.tmp"
        txt_tmp = work / f"{out_stem}.txt.tmp"

        align_and_build(
            results,
            chunk_secs=chunk_secs,
            full_mp3=work / "unused.mp3",  # no audio needed for re-render
            out_base=work / out_stem,
            srt_tmp=srt_tmp,
            diarized_srt_tmp=diarized_srt_tmp,
            txt_tmp=txt_tmp,
            line_interval_secs=line_interval_secs,
            paragraph_interval_secs=paragraph_interval_secs,
            skip_sync=True,
            speakers=speakers,
            txt_width=txt_width,
        )

        targets: dict[str, tuple[bool, Path]] = {
            "srt": (srt_enabled, srt_target),
            "txt": (txt_enabled, txt_target),
        }
        tmp_paths: dict[str, Path] = {
            "srt": srt_tmp,
            "txt": txt_tmp,
        }
        if diarized_enabled:
            assert diarized_srt_tmp is not None
            targets["diarized_srt"] = (True, diarized_target)
            tmp_paths["diarized_srt"] = diarized_srt_tmp
        if metadata_enabled:
            metadata_tmp = work / f"{out_stem}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, chunk_secs, model=model), encoding="utf-8"
            )
            targets["metadata_json"] = (
                True,
                metadata_target,
            )
            tmp_paths["metadata_json"] = metadata_tmp

        # Only overwrite targets that are stale relative to the transcript;
        # fresh ones are left untouched and reported as unchanged.
        # ``--force`` bypasses the staleness check so every enabled output
        # is regenerated unconditionally (matches the CLI's --force UX).
        regenerated: list[str] = []
        unchanged: list[str] = []
        for key, (enabled, final_path) in targets.items():
            if not enabled:
                continue
            tmp_path = tmp_paths.get(key)
            if tmp_path is None or final_path is None:
                continue
            if not force and _outputs_valid([final_path], [transcript_path]):
                unchanged.append(str(final_path))
                continue
            os.replace(tmp_path, final_path)
            regenerated.append(str(final_path))

        if not transcript_enabled:
            try:
                transcript_path.unlink(missing_ok=True)
            except OSError:
                pass

        if regenerated:
            logger.info("Re-rendered from transcript: %s", ", ".join(regenerated))
        if unchanged:
            logger.info("Unchanged (already up to date): %s", ", ".join(unchanged))

        produced = regenerated + unchanged

        # Warn about speakers the custom mapping did not cover.
        if speakers and diarized_enabled:
            _warn_unmapped_speakers(
                echo, results, chunk_secs, out_dir, out_stem, speakers, diarized_target
            )

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=TranscribeOutput(
                srt=str(srt_target) if srt_enabled and str(srt_target) in produced else None,
                txt=str(txt_target) if txt_enabled and str(txt_target) in produced else None,
                diarized_srt=str(diarized_target) if diarized_enabled and str(diarized_target) in produced else None,
                metadata_json=str(metadata_target) if metadata_enabled and str(metadata_target) in produced else None,
                transcript_json=str(transcript_path) if transcript_enabled and transcript_path.exists() else None,
            ),
        )


def _warn_unmapped_speakers(
    echo: TranscribeInput,
    results: list,
    chunk_secs: list[float] | tuple[float, ...] | float,
    out_dir: Path,
    out_stem: str,
    speakers: dict[str, str],
    diarized_target: Path,
) -> None:
    """Log a warning listing unmapped speakers with a re-render CLI recipe."""
    speaker_map = dict(speakers)
    used = {
        w.speaker
        for r in results
        for w in r.words
        if w.speaker and w.speaker.startswith("spk:")
    }

    def _is_covered(s: str) -> bool:
        tag = f"[{s}] "
        return any(k in tag for k in speaker_map)

    unmapped = sorted([s for s in used if not _is_covered(s)])
    if not unmapped:
        return
    # Full map display: mapped + unmapped (raw).
    display_parts = []
    for s in used:
        matching_k = next((k for k in speaker_map if k in f"[{s}] "), None)
        if matching_k is not None:
            display_parts.append(f"{matching_k}={speaker_map[matching_k]}")
        else:
            display_parts.append(f"{s}={s}")
    display = " ".join(display_parts)
    logger.warning(
        "Some speakers are not covered by speaker mapping.\n"
        "  Speaker map: %s\n"
        "  Unmapped: %s",
        display,
        ", ".join(unmapped),
    )
    # Recommended command: delete the diarized SRT and re-render with a
    # completed map in a .speakers.txt file (or via --speakers-txt-file).
    parts = []
    for s in used:
        matching_k = next((k for k in speaker_map if k in f"[{s}] "), None)
        if matching_k is None:
            n = s.rsplit(":", 1)[-1]
            parts.append(f"[{s}]=Name{n}:")
        else:
            parts.append(f"{matching_k}={speaker_map[matching_k]}")
    recommended = "; ".join(parts) + ";"
    speakers_file = out_dir / f"{out_stem}.speakers.txt"
    logger.warning(
        "To re-render with names, save the mapping to '%s', delete the diarized SRT, and re-run:\n"
        "  echo '%s' > '%s' && rm '%s' && gemini-transcribe --diarized-srt-file='%s' '%s'",
        speakers_file.name,
        recommended,
        speakers_file,
        diarized_target,
        diarized_target,
        echo.input_file,
    )


def _build_output_paths(
    out_dir: Path,
    out_base: str,
    srt_enabled: bool,
    txt_enabled: bool,
    diarized_enabled: bool,
    metadata_enabled: bool,
    srt_target: Path,
    txt_target: Path,
    diarized_target: Path,
    metadata_target: Path,
) -> list[Path]:
    """Return only the enabled output paths, for skip-detection.

    Disabled outputs are omitted entirely so ``_outputs_valid`` does not
    treat them as "missing" (which would force re-runs every time).
    """
    out: list[Path] = []
    if diarized_enabled:
        out.append(diarized_target)
    if srt_enabled:
        out.append(srt_target)
    if txt_enabled:
        out.append(txt_target)
    if metadata_enabled:
        out.append(metadata_target)
    return out


def _existing_outputs(paths: list[Path]) -> TranscribeOutput:
    """Map a list of existing enabled output paths to a TranscribeOutput.

    Paths are classified by suffix, so the list can be in any order and
    may include any subset of the possible outputs.
    """
    return _to_output([str(p) for p in paths])


def _to_output(produced: list[str]) -> TranscribeOutput:
    out = TranscribeOutput()
    for p in produced:
        name = Path(p).name
        if name.endswith(".diarized.srt"):
            out.diarized_srt = p
        elif name.endswith(".srt"):
            out.srt = p
        elif name.endswith(".txt"):
            out.txt = p
        elif name.endswith(".metadata.json"):
            out.metadata_json = p
        elif name.endswith(".transcript.json"):
            out.transcript_json = p
    return out


def _collect_leftover(
    ctx: WorkContext, chunks: list[Path] | None, keep_chunks: bool
) -> TranscribeLeftover:
    """Collect leftover files (metadata checkpoints, intermediates, workdir)."""
    leftover = TranscribeLeftover()
    if keep_chunks and chunks:
        for chunk in chunks:
            if chunk.exists():
                leftover.intermediate_files.append(str(chunk))
            meta = chunk.with_suffix(".metadata.json")
            if meta.exists():
                leftover.metadata_files.append(str(meta))
    if ctx.full_mp3.exists():
        leftover.intermediate_files.append(str(ctx.full_mp3))
    if ctx.chunk_dir.is_dir():
        for p in sorted(ctx.chunk_dir.glob("*.mp3")):
            if str(p) not in leftover.intermediate_files:
                leftover.intermediate_files.append(str(p))
        for p in sorted(ctx.chunk_dir.glob("*.metadata.json")):
            if str(p) not in leftover.metadata_files:
                leftover.metadata_files.append(str(p))
    if ctx.work_dir.is_dir():
        leftover.work_dir = str(ctx.work_dir)
    return leftover


def _all_exist(paths: Sequence[Path | None]) -> bool:
    return bool(paths) and all(p is not None and p.exists() for p in paths)


def _outputs_valid(paths: Sequence[Path | None], sources: Sequence[Path]) -> bool:
    """Outputs are valid only if they all exist and are newer than every source.

    If any target is missing or any source is newer than a target, the
    outputs are stale and must be regenerated.
    """
    if not _all_exist(paths):
        return False
    source_mtimes = [s.stat().st_mtime for s in sources if s.exists()]
    if not source_mtimes:
        return True
    newest_source = max(source_mtimes)
    return all(
        p is not None and p.stat().st_mtime >= newest_source for p in paths
    )


def _mask_key(key: str | None) -> str:
    """Show ``[redacted]<last 4>`` for an API key (compact, no prefix leak).

    Thin re-export of :func:`gemini_transcribe_wrapper._key_utils.mask_key`
    kept for backward compatibility with existing callers/tests.
    """
    from ._key_utils import mask_key

    return mask_key(key)


def _check_api_key(api_keys: list[str] | None) -> None:
    """Validate that at least one Gemini API key is resolvable.

    Accepts a list of explicit keys; falls back to env vars
    (``$GEMINI_API_KEYS`` → ``$GEMINI_API_KEY`` → ``$GOOGLE_API_KEY``).
    Emits the ``Using GEMINI_API_KEY=<masked>`` log line (showing the
    first key) and raises ``RuntimeError`` if nothing is found.
    """
    candidates: list[str] = []
    if api_keys:
        candidates.extend(k.strip() for k in api_keys if k and k.strip())
    if not candidates:
        plural = os.environ.get("GEMINI_API_KEYS", "")
        if plural:
            candidates.extend(part.strip() for part in re.split(r"[,;]", plural) if part.strip())
        for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            val = os.environ.get(var)
            if val and val.strip():
                candidates.append(val.strip())
    # Dedupe, preserve order, drop empties.
    seen: set[str] = set()
    deduped: list[str] = []
    for k in candidates:
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)
    if not deduped:
        raise RuntimeError(
            "No Gemini API key found. Set $GEMINI_API_KEYS, $GEMINI_API_KEY, "
            "or pass --gemini-api-keys=key1,key2,... (deprecated: "
            "--gemini-api-key KEY)."
        )
    now = time.monotonic()
    for k, until in list(_GLOBAL_DEAD_POOL.items()):
        if now >= until:
            _GLOBAL_DEAD_POOL.pop(k, None)
    live_keys = [k for k in deduped if k not in _GLOBAL_DEAD_POOL]
    all_masked = ", ".join(_mask_key(k) for k in deduped)
    live_masked = ", ".join(_mask_key(k) for k in live_keys)

    logger.info(  # nosemgrep: python-logger-credential-disclosure
        "All api keys given (%d): %s, live api keys (%d): %s",
        len(deduped),
        all_masked,
        len(live_keys),
        live_masked,
    )


def _cleanup_workdir(ctx: WorkContext, keep_chunks: bool) -> None:
    """Remove work dir; keep chunk mp3s (and checkpoints) on failure for resume."""
    if keep_chunks:
        return
    shutil.rmtree(ctx.work_dir, ignore_errors=True)
    try:
        parent = ctx.work_dir.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _format_split_plan(plan) -> str:
    """Render the split plan for human-readable logging.

    Single chunk:    "1 chunk: 3833.5s"
    Multiple chunks: "2 chunks: 1 full (3600.0s), last chunk 233.5s"
    Three or more:   "3 chunks: 2 full (1800.0s each), last chunk 1400.0s"
    """
    n = plan.num_chunks
    sizes = list(plan.chunk_secs)
    if n == 1:
        return f"1 chunk: {sizes[0]:.1f}s"
    full = sizes[:-1]
    last = sizes[-1]
    full_size = full[0] if full else 0.0
    if all(abs(s - full_size) < 0.01 for s in full):
        full_desc = f"{len(full)} full ({full_size:.1f}s each)"
    else:
        full_desc = (
            f"{len(full)} front-loaded (" + ", ".join(f"{s:.1f}s" for s in full) + ")"
        )
    return f"{n} chunks: {full_desc}, last chunk {last:.1f}s"
