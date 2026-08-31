"""Public Python API: gemini_transcribe() orchestration."""

from __future__ import annotations

import glob as globlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

from .audio import compute_split_plan, extract_audio, probe_duration_secs, split_chunks
from .merge import align_and_build, build_metadata_json, commit_outputs
from .models import (
    BatchTranscribeResult,
    TranscribeInput,
    TranscribeLeftover,
    TranscribeOutput,
    TranscribeResult,
    TranscribeStatus,
)
from .stt import (
    MODEL_ID,
    TranscribeClient,
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

# Default chunk length per diarize mode. The user can still override via
# ``chunk_secs``; these are only the fallbacks when the user doesn't specify.
# The Gemini-3.5-transcribe API caps audio per call differently depending
# on whether speaker diarization is requested (60 min off, 30 min on). Both
# defaults already include a 1-min safety margin, so they also serve as the
# per-chunk ceiling passed to audio.compute_split_plan.
# - diarize=False (default): 3540s (59 min) — file fits in one API call up
#   to ~1 hour.
# - diarize=True : 1740s (29 min) — shorter chunks aid diarization quality
#   and stay under the 30-min per-call limit.
DEFAULT_CHUNK_SECS_NO_DIARIZE = 3540.0
DEFAULT_CHUNK_SECS_DIARIZE = 1740.0

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
    input_file: Path, output_dir: Path, output_base: str, temp_dir: str | None
) -> WorkContext:
    if temp_dir:
        base_dir = Path(temp_dir)
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
    gemini_api_key: str | None = None,
    language: str = "ko-KR",
    diarize: bool = False,
    tier: str = "free",
    create_srt: bool = True,
    create_txt: bool = True,
    create_metadata_json: bool = False,
    create_transcript_json: bool = True,
    force: bool = False,
    line_interval_secs: float = 1.0,
    paragraph_interval_secs: float = 2.5,
    request_interval_secs: float | None = None,
    chunk_secs: float | None = None,
    speakers: dict[str, str] | None = None,
    temp_dir: str | None = "temp",
    ffsubsync_srt: bool = False,
    custom_vocabulary: list[str] | None = None,
    audit_jsonl: str | Path | None = None,
    model: str = MODEL_ID,
) -> BatchTranscribeResult:
    """A free video transcription CLI using gemini-3.5-transcribe that outputs .diarized.srt, .srt, and .txt files.

    Cross-platform (auto ffmpeg/ffsubsync dependencies).

    Args:
        input_file: Input file path or glob pattern.
        output_dir: Directory for final output files (default: input dir).
        output_base: Base name for outputs (default: input stem).
        gemini_api_key: Gemini API key (default: $GEMINI_API_KEY).
        language: BCP-47 language code (default: "ko-KR").
        diarize: When True, enable speaker diarization in the API call and
            emit ``.diarized.*`` outputs. The wrapper then uses the shorter
            default chunk length (29m50s) for better diarization quality.
            When False (default), speaker labels are skipped at the API and
            the wrapper packs each chunk up to 29 min (59-min logical units,
            split into 2 API calls to stay under the 30-min per-call limit),
            better suited to the free-tier daily quota. ``--speakers`` is
            ignored when this is False.
        tier: Gemini API pricing tier ("free" or "paid", default: "free").
            When "free", enforces 60s cooldown between API calls.
            When "paid", cooldown is 0s unless overridden by ``request_interval_secs``.
        create_srt/create_txt: Whether to generate each output format
            (.srt, .txt). ``.diarized.srt`` is produced automatically when
            ``diarize`` is True.
        create_metadata_json: Whether to keep the .metadata.json output
            (default: off).
        create_transcript_json: Whether to keep the transcript file
            (default: on). The transcript stores the full transcription
            result (text + word timestamps + speakers) so outputs can be
            re-rendered without calling the API again: if it exists and
            outputs are missing, they are regenerated from it. The transcript
            filename picks up a ``.diarized.`` prefix when ``diarize`` is on.
        force: Re-process even if all outputs already exist.
        line_interval_secs / paragraph_interval_secs: TXT break gaps.
        request_interval_secs: Delay between STT API calls. Defaults to 120.0s for
            "free" tier, 0.0s for "paid" tier.
        chunk_secs: Optional fixed chunk length in seconds. Overrides the
            default for the chosen ``diarize`` mode (59 min off, 29 min on).
            A hard ceiling of 29 min (1740s) is always enforced because the
            Gemini API caps audio at ~30 min per call. Useful for debugging
            short clips.
        speakers: Optional mapping of raw speaker ids (e.g. "spk:0") to
            display names used in the .diarized.srt output. Speakers missing
            from the mapping keep their raw id, and a warning is emitted
            listing them. Ignored unless ``diarize`` is True.
        temp_dir: Where to place intermediate work files (default: 'temp').
            When set, all temp files (temp_audio.mp3, chunk_*.mp3, *.tmp,
            checkpoints) live under this directory instead of next to the output.
        ffsubsync_srt: When True, also write "<base>.ffsubsync.srt" aligned to
            the full audio via ffsubsync for manual comparison. The main
            .srt/.diarized.srt keep the raw transcript timestamps (default:
            off).
        audit_jsonl: Optional custom path for JSONL audit logging. Defaults to
            ``<os-temp>/gemini-transcribe-wrapper-<short-hostname>-<username>.audit.jsonl``,
            so each (host, user) pair on a shared NAS gets its own log file.

    Returns:
        BatchTranscribeResult with per-input TranscribeResult items. Each item
        carries `input` (echo of inputs/options), `output` (requested final
        outputs only), `leftover` (info files and kept-on-failure work files),
        `status` (SUCCESS or FAILED), and `error` (when failed).

    Raises:
        QuotaExceededError: When a Gemini API call fails with HTTP 429 / quota
            exhaustion. Aborts the batch immediately instead of continuing.
    """
    effective_interval = (
        (0.0 if tier == "paid" else 120.0)
        if request_interval_secs is None
        else float(request_interval_secs)
    )
    inputs = _expand_path(input_file)
    results = []
    for path in inputs:
        results.append(
            _process_one(
                input_path=path,
                output_dir=output_dir,
                output_base=output_base,
                gemini_api_key=gemini_api_key,
                language=language,
                diarize=diarize,
                tier=tier,
                create_srt=create_srt,
                create_txt=create_txt,
                create_metadata_json=create_metadata_json,
                create_transcript_json=create_transcript_json,
                force=force,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                request_interval_secs=effective_interval,
                chunk_secs=chunk_secs,
                speakers=speakers,
                temp_dir=temp_dir,
                ffsubsync_srt=ffsubsync_srt,
                custom_vocabulary=custom_vocabulary,
                audit_jsonl=audit_jsonl,
            )
        )
    return BatchTranscribeResult(results=results)


def _process_one(
    input_path: str,
    output_dir: str | None,
    output_base: str | None,
    gemini_api_key: str | None,
    language: str,
    diarize: bool,
    tier: str,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
    create_transcript_json: bool,
    force: bool,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    request_interval_secs: float,
    chunk_secs: float | None,
    speakers: dict[str, str] | None,
    temp_dir: str | None,
    ffsubsync_srt: bool,
    custom_vocabulary: list[str] | None = None,
    audit_jsonl: str | Path | None = None,
) -> TranscribeResult:
    input_file = Path(input_path)

    out_stem = Path(output_base) if output_base else Path(input_file.stem)
    out_dir = Path(output_dir) if output_dir else input_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    echo = TranscribeInput(
        input_file=str(input_file),
        output_dir=str(out_dir) if output_dir else None,
        output_base=out_stem.name,
        language=language,
        diarize=diarize,
        tier=tier,
        audit_jsonl=str(audit_jsonl) if audit_jsonl else None,
        create_srt=create_srt,
        create_txt=create_txt,
        create_metadata_json=create_metadata_json,
        create_transcript_json=create_transcript_json,
        ffsubsync_srt=ffsubsync_srt,
        force=force,
        temp_dir=temp_dir,
        line_interval_secs=line_interval_secs,
        paragraph_interval_secs=paragraph_interval_secs,
        request_interval_secs=request_interval_secs,
    )

    # speakers is meaningless without diarize.
    if speakers and not diarize:
        logger.warning(
            "--speakers was passed but diarization is off; ignoring speaker map. "
            "Re-run with --diarize to use --speakers."
        )
        speakers = None

    if not input_file.is_file():
        logger.error("Input file not found: %s", input_file)
        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.NOT_FOUND,
            error=f"Input file not found: {input_file}",
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

    transcript_path, transcript_migrate_to = _resolve_transcript_path(
        out_dir, out_stem.name, diarize
    )
    final_paths = _build_output_paths(
        out_dir, out_stem.name, diarize, create_srt, create_txt, create_metadata_json
    )
    chunks: list[Path] = []
    ctx: WorkContext | None = None

    try:
        # Re-render path: a valid transcript exists and the outputs are stale
        # relative to it (missing, or the transcript is newer). Regenerate
        # .diarized.srt/.srt/.txt from the stored transcript without calling
        # the API.
        if load_transcript(transcript_path) is not None and (
            force or not _outputs_valid(final_paths, [transcript_path])
        ):
            logger.info("Transcript found; re-rendering outputs from %s", transcript_path)
            try:
                return _render_from_transcript(
                    echo=echo,
                    out_dir=out_dir,
                    out_stem=out_stem.name,
                    transcript_path=transcript_path,
                    diarize=diarize,
                    create_srt=create_srt,
                    create_txt=create_txt,
                    create_metadata_json=create_metadata_json,
                    create_transcript_json=create_transcript_json,
                    line_interval_secs=line_interval_secs,
                    paragraph_interval_secs=paragraph_interval_secs,
                    speakers=speakers,
                    model=model,
                )
            except Exception as exc:  # noqa: BLE001 - fall through to full re-run
                logger.warning("Re-render from transcript failed (%s); re-transcribing", exc)

        # Skip condition: outputs are valid only when they all exist AND were
        # generated after the source file. If any target is missing or the
        # source is newer, regenerate.
        if not force and _outputs_valid(final_paths, [input_file]):
            logger.debug(
                "Outputs exist and are newer than source; skipping %s "
                "(use --force to redo)",
                input_file,
            )
            return TranscribeResult(
                input=echo,
                status=TranscribeStatus.SKIPPED,
                output=_existing_outputs(final_paths),
            )

        ctx = _setup_workdir(input_file, out_dir, out_stem.name, temp_dir)

        try:
            _check_api_key(gemini_api_key)
        except RuntimeError as exc:
            logger.error("Failed processing %s: %s", input_file, exc)
            return TranscribeResult(
                input=echo, status=TranscribeStatus.FAILED, error=str(exc)
            )

        total_secs = probe_duration_secs(input_file)
        logger.info("%s duration: %.1fs", input_file, total_secs)
        # The Gemini API has a different per-call audio limit depending on
        # whether speaker diarization is requested (60 min off, 30 min on).
        # Both defaults already bake in a 1-min safety margin, so they also
        # serve as the per-chunk ceiling passed to compute_split_plan.
        effective_chunk_secs = chunk_secs
        max_chunk_secs = (
            DEFAULT_CHUNK_SECS_DIARIZE if diarize else DEFAULT_CHUNK_SECS_NO_DIARIZE
        )
        if effective_chunk_secs is None:
            effective_chunk_secs = max_chunk_secs
        plan = compute_split_plan(
            total_secs,
            chunk_secs=effective_chunk_secs,
            max_chunk_secs=max_chunk_secs,
        )
        logger.info("Split plan: %s", _format_split_plan(plan))

        extract_audio(input_file, ctx.full_mp3, force=force)
        chunks = split_chunks(ctx.full_mp3, ctx.chunk_dir, plan)

        client = TranscribeClient(
            api_key=gemini_api_key,
            language=language,
            enable_diarization=diarize,
            request_interval_secs=request_interval_secs,
            tier=tier,
            custom_vocabulary=custom_vocabulary,
            source_file=str(input_file.resolve()),
            audit_jsonl=audit_jsonl,
            model=model,
        )
        results = transcribe_chunks_sequential(
            client,
            chunks,
            request_interval_secs=request_interval_secs,
        )

        srt_tmp = ctx.work_dir / f"{ctx.output_base}.srt.tmp"
        # Only generate the diarized SRT tmp when diarize is on; otherwise the
        # tmp/final pair would be created and immediately unlinked by
        # commit_outputs, wasting a write.
        diarized_srt_tmp: Path | None = None
        if diarize:
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
            ffsubsync_srt=ffsubsync_srt,
        )

        # Save the transcript (full transcription result for later re-render),
        # including the API call logs for this transcription.
        save_transcript(
            transcript_path,
            results,
            plan.chunk_secs,
            language,
            api_logs=getattr(client, "api_logs", None),
        )

        # Per-chunk checkpoints are informational files; delete unless the
        # caller requested the merged metadata output.
        if not create_metadata_json:
            for chunk in chunks:
                meta = chunk.with_suffix(".metadata.json")
                if meta.exists():
                    meta.unlink()

        tmp_map: dict[str, Path] = {"srt_tmp": srt_tmp, "txt_tmp": txt_tmp}
        final_map: dict[str, Path] = {
            "srt": out_dir / f"{ctx.output_base}.srt",
            "txt": out_dir / f"{ctx.output_base}.txt",
        }
        if diarize:
            assert diarized_srt_tmp is not None  # for type checkers
            tmp_map["diarized_srt_tmp"] = diarized_srt_tmp
            final_map["diarized_srt"] = out_dir / f"{ctx.output_base}.diarized.srt"
        if create_metadata_json:
            metadata_tmp = ctx.work_dir / f"{ctx.output_base}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, plan.chunk_secs, model=model), encoding="utf-8"
            )
            tmp_map["metadata_json_tmp"] = metadata_tmp
            final_map["metadata_json"] = out_dir / f"{ctx.output_base}.metadata.json"

        produced = commit_outputs(
            outputs={**tmp_map, **final_map},
            create_diarized_srt=diarize,
            create_srt=create_srt,
            create_txt=create_txt,
            create_metadata_json=create_metadata_json,
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

        # transcript.json is kept by default; --no-transcript-json removes it.
        if not create_transcript_json:
            try:
                transcript_path.unlink(missing_ok=True)
            except OSError:
                pass

        logger.info("Done: %s", ", ".join(produced) or "(nothing produced)")

        # Warn about speakers the custom mapping did not cover, with a
        # recommended re-render command.
        if speakers and diarize:
            _warn_unmapped_speakers(
                echo, results, plan.chunk_secs, out_dir, out_stem.name, speakers
            )

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=_to_output(produced),
        )
    except Exception as exc:
        # 429 / quota errors are not a per-file condition: retrying the next
        # file will hit the same limit. Abort the batch immediately so the
        # caller doesn't burn more quota on calls that are guaranteed to
        # fail. The hint was already logged in stt.transcribe_chunk.
        from .stt import _is_quota_error

        if _is_quota_error(exc):
            logger.error(
                "Aborting batch: quota / rate limit hit while processing %s. "
                "Remaining files will not be processed.",
                input_file,
            )
            raise QuotaExceededError(input_file, exc) from exc
        # On failure: keep intermediate mp3 files for resume. Report them as
        # leftover so the caller can clean up if desired.
        logger.error("Failed processing %s: %s", input_file, exc)
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
    diarize: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
    create_transcript_json: bool,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    speakers: dict[str, str] | None,
    model: str = MODEL_ID,
) -> TranscribeResult:
    """Regenerate .diarized.srt/.srt/.txt from a transcript (no API call)."""
    results = load_transcript(transcript_path)
    if results is None:
        raise ValueError(f"Invalid transcript: {transcript_path}")
    chunk_secs = load_transcript_chunk_secs(transcript_path)

    # Render into temp files first (atomic commit at the end).
    with tempfile.TemporaryDirectory(prefix=f".{out_stem}.render-", dir=str(out_dir)) as tmp:
        work = Path(tmp)
        srt_tmp = work / f"{out_stem}.srt.tmp"
        diarized_srt_tmp: Path | None = None
        if diarize:
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
        )

        tmp_map: dict[str, Path] = {"srt_tmp": srt_tmp, "txt_tmp": txt_tmp}
        final_map: dict[str, Path] = {
            "srt": out_dir / f"{out_stem}.srt",
            "txt": out_dir / f"{out_stem}.txt",
        }
        if diarize:
            assert diarized_srt_tmp is not None
            tmp_map["diarized_srt_tmp"] = diarized_srt_tmp
            final_map["diarized_srt"] = out_dir / f"{out_stem}.diarized.srt"
        if create_metadata_json:
            metadata_tmp = work / f"{out_stem}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, chunk_secs, model=model), encoding="utf-8"
            )
            tmp_map["metadata_json_tmp"] = metadata_tmp
            final_map["metadata_json"] = out_dir / f"{out_stem}.metadata.json"

        # Only overwrite targets that are stale relative to the transcript;
        # fresh ones are left untouched and reported as unchanged.
        regenerated: list[str] = []
        unchanged: list[str] = []
        for key, enabled in (
            ("diarized_srt", diarize),
            ("srt", create_srt),
            ("txt", create_txt),
            ("metadata_json", create_metadata_json),
        ):
            if not enabled:
                continue
            tmp_path = tmp_map.get(key + "_tmp")
            final_path = final_map.get(key)
            if tmp_path is None or final_path is None:
                continue
            if _outputs_valid([final_path], [transcript_path]):
                unchanged.append(str(final_path))
                continue
            os.replace(tmp_path, final_path)
            regenerated.append(str(final_path))

        if not create_transcript_json:
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
        if speakers and diarize:
            _warn_unmapped_speakers(echo, results, chunk_secs, out_dir, out_stem, speakers)

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=_to_output(produced),
        )


def _warn_unmapped_speakers(
    echo: TranscribeInput,
    results: list,
    chunk_secs: list[float] | tuple[float, ...] | float,
    out_dir: Path,
    out_stem: str,
    speaker_map: dict[str, str],
) -> None:
    """Log the speaker map and warn about speakers missing from the mapping."""
    from .merge import merge_cues

    cues = merge_cues(results, chunk_secs)
    used = sorted({c.speaker for c in cues if c.speaker})
    unmapped = [s for s in used if s not in speaker_map]

    if not unmapped:
        logger.info("All speakers mapped: %s", ", ".join(f"{s}={speaker_map[s]}" for s in used))
        return

    # Full map display: mapped + unmapped (raw).
    display = " ".join(f"{s}={speaker_map.get(s, s)}" for s in used)
    logger.warning(
        "Some speakers are not covered by --speakers mapping.\n"
        "  Speaker map: %s\n"
        "  Unmapped: %s",
        display,
        ", ".join(unmapped),
    )
    # Recommended command: delete the .diarized.srt and re-render with a
    # completed map, naming missing entries Name<index> for the user to edit.
    parts = []
    for s in used:
        if s not in speaker_map:
            n = s.rsplit(":", 1)[-1]
            parts.append(f"{s}=Name{n}")
        else:
            parts.append(f"{s}={speaker_map[s]}")
    recommended = "; ".join(parts) + ";"
    logger.warning(
        "To re-render with names, delete the .diarized.srt and re-run with the "
        "option, editing the Name# entries: rm '%s' && gemini-transcribe '%s' "
        "--speakers '%s'",
        out_dir / f"{out_stem}.diarized.srt",
        echo.input_file,
        recommended,
    )


def _build_output_paths(
    out_dir: Path,
    out_base: str,
    diarize: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
) -> list[Path | None]:
    out = []
    if diarize:
        out.append(out_dir / f"{out_base}.diarized.srt")
    if create_srt:
        out.append(out_dir / f"{out_base}.srt")
    if create_txt:
        out.append(out_dir / f"{out_base}.txt")
    if create_metadata_json:
        out.append(out_dir / f"{out_base}.metadata.json")
    return out


def _existing_outputs(paths: list[Path | None]) -> TranscribeOutput:
    mapping: dict[str, Path | None] = {}
    for key, p in zip(("diarized_srt", "srt", "txt", "metadata_json"), paths):
        if p is not None:
            mapping[key] = p
    return _to_output([str(p) for p in mapping.values() if p is not None])


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


def _all_exist(paths: list[Path | None]) -> bool:
    return bool(paths) and all(p is not None and p.exists() for p in paths)


def _outputs_valid(paths: list[Path | None], sources: list[Path]) -> bool:
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


def _mask_key(key: str) -> str:
    """Show only the first and last 4 chars of an API key."""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


def _check_api_key(api_key: str | None) -> None:
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "No Gemini API key found. Set GEMINI_API_KEY or pass --gemini-api-key."
        )
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info("Using GEMINI_API_KEY=%s", _mask_key(key))  # nosemgrep


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
    Multiple chunks: "2 chunks: 1 full (3540.0s), last chunk 293.5s"
    Three or more:   "3 chunks: 2 full (1740.0s each), last chunk 1520.0s"
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
