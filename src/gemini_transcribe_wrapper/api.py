"""Public Python API: gemini_transcribe() orchestration."""

from __future__ import annotations

import glob as globlib
import json
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
    TranscribeClient,
    load_transcript,
    save_transcript,
    transcribe_chunks_sequential,
)

logger = logging.getLogger(__name__)


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


def gemini_transcribe(
    input_file: str,
    output_dir: str | None = None,
    output_base: str | None = None,
    gemini_api_key: str | None = None,
    language: str = "ko-KR",
    create_speakers_srt: bool = True,
    create_srt: bool = True,
    create_txt: bool = True,
    create_metadata_json: bool = False,
    create_transcript_json: bool = True,
    force: bool = False,
    line_interval_secs: float = 1.0,
    paragraph_interval_secs: float = 2.5,
    request_interval_secs: float = 30.0,
    chunk_secs: float | None = None,
    speakers: dict[str, str] | None = None,
    temp_dir: str | None = None,
    ffsubsync_srt: bool = False,
    free_tier_wait_on_429: bool = False,
) -> BatchTranscribeResult:
    """Transcribe a multimedia file using Gemini 3.5 Transcribe.

    Zero-config cross-platform (auto ffmpeg/ffsubsync dependencies).

    Args:
        input_file: Input file path or glob pattern.
        output_dir: Directory for final output files (default: input dir).
        output_base: Base name for outputs (default: input stem).
        gemini_api_key: Gemini API key (default: $GEMINI_API_KEY).
        language: BCP-47 language code (default: "ko-KR").
        create_speakers_srt/create_srt/create_txt: Whether to generate each
            output format (.speakers.srt, .srt, .txt).
        create_metadata_json: Whether to keep the .metadata.json output
            (default: off).
        create_transcript_json: Whether to keep <base>.transcript.json
            (default: on). The transcript stores the full transcription result
            (text + word timestamps + speakers) so .speakers.srt/.srt/.txt can
            be re-rendered without calling the API again: if it exists and
            outputs are missing, they are regenerated from it.
        force: Re-process even if all outputs already exist.
        line_interval_secs / paragraph_interval_secs: TXT break gaps.
        request_interval_secs: Delay between STT API calls.
        chunk_secs: Optional fixed chunk length in seconds. When set, the
            audio is split into equal chunks of ~this length instead of using
            the default packs 29m50s chunks (max 30 min per API call).
            Useful for debugging short clips.
        speakers: Optional mapping of raw speaker ids (e.g. "spk:0") to display
            names used in the .speakers.srt output. Speakers missing from the
            mapping keep their raw id, and a warning is emitted listing them.
        temp_dir: Where to place intermediate work files. When set, all temp
            files (temp_audio.mp3, chunk_*.mp3, *.tmp, checkpoints) live under
            this directory instead of next to the output.
        ffsubsync_srt: When True, also write "<base>.ffsubsync.srt" aligned to
            the full audio via ffsubsync for manual comparison. The main
            .srt/.speakers.srt keep the raw transcript timestamps (default:
            off).
        free_tier_wait_on_429: When True, sleep until PST midnight (in 1-hour
            chunks, logging the remaining time) whenever the daily free-tier
            quota is reached or a 429 is hit, then resume. Intended for
            unattended multi-day batch runs on the free tier.

    Returns:
        BatchTranscribeResult with per-input TranscribeResult items. Each item
        carries `input` (echo of inputs/options), `output` (requested final
        files) and `leftover` (info files / artifacts left behind, suitable for
        caller-side cleanup).
    """
    files = _expand_path(input_file)
    results: list[TranscribeResult] = []
    for f in files:
        results.append(
            _process_one(
                f,
                output_dir=output_dir,
                output_base=output_base,
                gemini_api_key=gemini_api_key,
                language=language,
                create_speakers_srt=create_speakers_srt,
                create_srt=create_srt,
                create_txt=create_txt,
                create_metadata_json=create_metadata_json,
                create_transcript_json=create_transcript_json,
                force=force,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                request_interval_secs=request_interval_secs,
                chunk_secs=chunk_secs,
                speakers=speakers,
                temp_dir=temp_dir,
                ffsubsync_srt=ffsubsync_srt,
                free_tier_wait_on_429=free_tier_wait_on_429,
            )
        )
    return BatchTranscribeResult(results=results)


def _process_one(
    input_path: str,
    output_dir: str | None,
    output_base: str | None,
    gemini_api_key: str | None,
    language: str,
    create_speakers_srt: bool,
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
    free_tier_wait_on_429: bool,
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
        create_speakers_srt=create_speakers_srt,
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
        free_tier_wait_on_429=free_tier_wait_on_429,
    )

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

    transcript_path = out_dir / f"{out_stem.name}.transcript.json"
    final_paths = _build_output_paths(
        out_dir, out_stem.name, create_speakers_srt, create_srt, create_txt, create_metadata_json
    )
    chunks: list[Path] = []
    ctx: WorkContext | None = None

    try:
        # Re-render path: a valid transcript exists and the outputs are stale
        # relative to it (missing, or the transcript is newer). Regenerate
        # .speakers.srt/.srt/.txt from the stored transcript without calling
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
                    create_speakers_srt=create_speakers_srt,
                    create_srt=create_srt,
                    create_txt=create_txt,
                    create_metadata_json=create_metadata_json,
                    create_transcript_json=create_transcript_json,
                    line_interval_secs=line_interval_secs,
                    paragraph_interval_secs=paragraph_interval_secs,
                    speakers=speakers,
                )
            except Exception as exc:  # noqa: BLE001 - fall through to full re-run
                logger.warning("Re-render from transcript failed (%s); re-transcribing", exc)

        # Skip condition: outputs are valid only when they all exist AND were
        # generated after the source file. If any target is missing or the
        # source is newer, regenerate.
        if not force and _outputs_valid(final_paths, [input_file]):
            logger.info(
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
        plan = compute_split_plan(total_secs, chunk_secs=chunk_secs)
        logger.info("Split plan: %d chunk(s), %.1fs each", plan.num_chunks, plan.chunk_secs)

        extract_audio(input_file, ctx.full_mp3, force=force)
        chunks = split_chunks(ctx.full_mp3, ctx.chunk_dir, plan)

        client = TranscribeClient(
            api_key=gemini_api_key,
            language=language,
            enable_diarization=create_speakers_srt,
            free_tier_wait_on_429=free_tier_wait_on_429,
        )
        results = transcribe_chunks_sequential(
            client,
            chunks,
            request_interval_secs=request_interval_secs,
            free_tier_wait_on_429=free_tier_wait_on_429,
        )

        srt_tmp = ctx.work_dir / f"{ctx.output_base}.srt.tmp"
        speakers_srt_tmp = ctx.work_dir / f"{ctx.output_base}.speakers.srt.tmp"
        txt_tmp = ctx.work_dir / f"{ctx.output_base}.txt.tmp"

        align_and_build(
            results,
            chunk_secs=plan.chunk_secs,
            full_mp3=ctx.full_mp3,
            out_base=ctx.work_dir / ctx.output_base,
            srt_tmp=srt_tmp,
            speakers_srt_tmp=speakers_srt_tmp,
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

        tmp_map = {
            "speakers_srt_tmp": speakers_srt_tmp,
            "srt_tmp": srt_tmp,
            "txt_tmp": txt_tmp,
        }
        final_map = {
            "speakers_srt": out_dir / f"{ctx.output_base}.speakers.srt",
            "srt": out_dir / f"{ctx.output_base}.srt",
            "txt": out_dir / f"{ctx.output_base}.txt",
        }
        if create_metadata_json:
            metadata_tmp = ctx.work_dir / f"{ctx.output_base}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, plan.chunk_secs), encoding="utf-8"
            )
            tmp_map["metadata_json_tmp"] = metadata_tmp
            final_map["metadata_json"] = out_dir / f"{ctx.output_base}.metadata.json"

        produced = commit_outputs(
            outputs={**tmp_map, **final_map},
            create_speakers_srt=create_speakers_srt,
            create_srt=create_srt,
            create_txt=create_txt,
            create_metadata_json=create_metadata_json,
            cleanup_patterns=[],
            chunk_mp3s=chunks,
        )
        _cleanup_workdir(ctx, keep_chunks=False)

        # transcript.json is kept by default; --no-transcript-json removes it.
        if not create_transcript_json:
            try:
                transcript_path.unlink(missing_ok=True)
            except OSError:
                pass

        logger.info("Done: %s", ", ".join(produced) or "(nothing produced)")

        # Warn about speakers the custom mapping did not cover, with a
        # recommended re-render command.
        if speakers:
            _warn_unmapped_speakers(
                echo, results, plan.chunk_secs, out_dir, out_stem.name, speakers
            )

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=_to_output(produced),
        )
    except Exception as exc:  # noqa: BLE001 - convert any failure into a result
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
    create_speakers_srt: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
    create_transcript_json: bool,
    line_interval_secs: float,
    paragraph_interval_secs: float,
    speakers: dict[str, str] | None,
) -> TranscribeResult:
    """Regenerate .speakers.srt/.srt/.txt from a transcript.json (no API call)."""
    results = load_transcript(transcript_path)
    if results is None:
        raise ValueError(f"Invalid transcript: {transcript_path}")
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    chunk_secs = float(data.get("chunk_secs", 0.0))

    # Render into temp files first (atomic commit at the end).
    with tempfile.TemporaryDirectory(prefix=f".{out_stem}.render-", dir=str(out_dir)) as tmp:
        work = Path(tmp)
        srt_tmp = work / f"{out_stem}.srt.tmp"
        speakers_srt_tmp = work / f"{out_stem}.speakers.srt.tmp"
        txt_tmp = work / f"{out_stem}.txt.tmp"

        align_and_build(
            results,
            chunk_secs=chunk_secs,
            full_mp3=work / "unused.mp3",  # no audio needed for re-render
            out_base=work / out_stem,
            srt_tmp=srt_tmp,
            speakers_srt_tmp=speakers_srt_tmp,
            txt_tmp=txt_tmp,
            line_interval_secs=line_interval_secs,
            paragraph_interval_secs=paragraph_interval_secs,
            skip_sync=True,
            speakers=speakers,
        )

        tmp_map = {
            "speakers_srt_tmp": speakers_srt_tmp,
            "srt_tmp": srt_tmp,
            "txt_tmp": txt_tmp,
        }
        final_map = {
            "speakers_srt": out_dir / f"{out_stem}.speakers.srt",
            "srt": out_dir / f"{out_stem}.srt",
            "txt": out_dir / f"{out_stem}.txt",
        }
        if create_metadata_json:
            metadata_tmp = work / f"{out_stem}.metadata.json.tmp"
            metadata_tmp.write_text(
                build_metadata_json(results, chunk_secs), encoding="utf-8"
            )
            tmp_map["metadata_json_tmp"] = metadata_tmp
            final_map["metadata_json"] = out_dir / f"{out_stem}.metadata.json"

        # Only overwrite targets that are stale relative to the transcript;
        # fresh ones are left untouched and reported as unchanged.
        regenerated: list[str] = []
        unchanged: list[str] = []
        for key, enabled in (
            ("speakers_srt", create_speakers_srt),
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
        if speakers:
            _warn_unmapped_speakers(echo, results, chunk_secs, out_dir, out_stem, speakers)

        return TranscribeResult(
            input=echo,
            status=TranscribeStatus.SUCCESS,
            output=_to_output(produced),
        )


def _warn_unmapped_speakers(
    echo: TranscribeInput,
    results: list,
    chunk_secs: float,
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
    # Recommended command: delete the .speakers.srt and re-render with a
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
        "To re-render with names, delete the .speakers.srt and re-run with the "
        "option, editing the Name# entries: rm '%s' && gemini-transcribe '%s' "
        "--speakers '%s'",
        out_dir / f"{out_stem}.speakers.srt",
        echo.input_file,
        recommended,
    )


def _build_output_paths(
    out_dir: Path,
    out_base: str,
    create_speakers_srt: bool,
    create_srt: bool,
    create_txt: bool,
    create_metadata_json: bool,
) -> list[Path | None]:
    out = []
    if create_speakers_srt:
        out.append(out_dir / f"{out_base}.speakers.srt")
    if create_srt:
        out.append(out_dir / f"{out_base}.srt")
    if create_txt:
        out.append(out_dir / f"{out_base}.txt")
    if create_metadata_json:
        out.append(out_dir / f"{out_base}.metadata.json")
    return out


def _existing_outputs(paths: list[Path | None]) -> TranscribeOutput:
    mapping: dict[str, Path | None] = {}
    for key, p in zip(("speakers_srt", "srt", "txt", "metadata_json"), paths):
        if p is not None:
            mapping[key] = p
    return _to_output([str(p) for p in mapping.values() if p is not None])


def _to_output(produced: list[str]) -> TranscribeOutput:
    out = TranscribeOutput()
    for p in produced:
        name = Path(p).name
        if name.endswith(".speakers.srt"):
            out.speakers_srt = p
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
