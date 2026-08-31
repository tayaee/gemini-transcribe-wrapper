"""CLI entry point for gemini-transcribe (Click-based)."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click
from click.testing import CliRunner

from . import __version__
from .api import QuotaExceededError, gemini_transcribe
from .models import TranscribeStatus
from .stt import MODEL_ID, get_audit_log_path
from .usage_counter import usage_summary_line


@dataclass
class TranscribeOptions:
    """All CLI options resolved into a structured object (replaces argparse.Namespace)."""

    path: list[str] = field(default_factory=list)
    version: bool = False
    output_dir: str | None = None
    output_base: str | None = None
    gemini_api_key: str | None = None
    language: str = "ko-KR"
    model: str = MODEL_ID
    diarize: bool = False
    srt: bool = True
    txt: bool = True
    metadata_json: bool = False
    transcript_json: bool = True
    ffsubsync_srt: bool = False
    force: bool = False
    tier: str = "free"
    line_interval_secs: float = 1.0
    paragraph_interval_secs: float = 2.5
    request_interval_secs: float | None = None
    chunk_secs: float | None = None
    speakers: str | None = None
    custom_vocabulary: str | None = None
    temp_dir: str = "temp"
    audit_jsonl: str | None = None
    verbose: bool = False


_DEFAULT_AUDIT_JSONL = str(get_audit_log_path())


def _make_command() -> click.Command:
    """Build the Click command. The callback appends TranscribeOptions to _LAST_OPTIONS."""

    @click.command(
        context_settings={"help_option_names": ["-h", "--help"]},
        help=(
            f"gemini-transcribe v{__version__} - "
            "A free video transcription CLI using gemini-3.5-transcribe that "
            "outputs .diarized.srt, .srt, and .txt files."
        ),
    )
    @click.option(
        "-v",
        "--version",
        is_flag=True,
        default=False,
        help="Show version and exit",
    )
    @click.option(
        "--output-dir",
        default=None,
        help="Directory for output files (default: alongside input)",
    )
    @click.option(
        "--output-base",
        default=None,
        help="Base name for output files (default: input stem)",
    )
    @click.option(
        "--gemini-api-key",
        default=None,
        help="Gemini API key (default: $GEMINI_API_KEY)",
    )
    @click.option(
        "--language",
        default="ko-KR",
        help="BCP-47 language code (default: ko-KR)",
    )
    @click.option(
        "--model",
        default=MODEL_ID,
        show_default=True,
        help="Gemini audio model id to use for transcription",
    )
    @click.option(
        "--diarize/--no-diarize",
        default=False,
        help=(
            "Enable speaker diarization (default: off). When on, the wrapper uses 29-min "
            "chunks for better diarization accuracy and emits .diarized.* outputs. When "
            "off, it cuts the file into 59-min logical units (each split into 2x 29-min "
            "API calls to stay under the 30-min per-call limit), keeping free-tier API "
            "usage low."
        ),
    )
    @click.option(
        "--srt/--no-srt",
        default=True,
        help="Generate .srt subtitles (default: on)",
    )
    @click.option(
        "--txt/--no-txt",
        default=True,
        help="Generate .txt transcript text (default: on)",
    )
    @click.option(
        "--metadata-json/--no-metadata-json",
        default=False,
        help="Keep .metadata.json output (default: off)",
    )
    @click.option(
        "--transcript-json/--no-transcript-json",
        default=True,
        help="Keep <base>.transcript.json for later re-render (default: on)",
    )
    @click.option(
        "--ffsubsync-srt/--no-ffsubsync-srt",
        default=False,
        help="Also write <base>.ffsubsync.srt aligned to audio (default: off)",
    )
    @click.option(
        "--force/--no-force",
        default=False,
        help="Re-process even if outputs exist",
    )
    @click.option(
        "--tier",
        type=click.Choice(["free", "paid"], case_sensitive=False),
        default="free",
        help=(
            "Gemini API pricing tier (default: free). When 'free', enforces 60s "
            "cooldown between API calls; when 'paid', rate limiting is disabled "
            "unless overridden by --request-interval-secs."
        ),
    )
    @click.option(
        "--line-interval-secs",
        type=float,
        default=1.0,
        help="TXT newline break threshold (default: 1.0)",
    )
    @click.option(
        "--paragraph-interval-secs",
        type=float,
        default=2.5,
        help="TXT paragraph break threshold (default: 2.5)",
    )
    @click.option(
        "--request-interval-secs",
        type=float,
        default=None,
        help="Delay between API calls (default: 120.0 for free tier, 0.0 for paid tier)",
    )
    @click.option(
        "--chunk-secs",
        type=float,
        default=None,
        help=(
            "Fixed chunk length in seconds (default: auto, 59-min logical units when "
            "--no-diarize, 29-min when --diarize; hard ceiling of 29 min enforced to "
            "fit the Gemini 30-min per-call limit)"
        ),
    )
    @click.option(
        "--speakers",
        default=None,
        help="Speaker name mapping for .diarized.srt, e.g. 'spk:0=궤도;spk:1=가람;'",
    )
    @click.option(
        "--custom-vocabulary",
        default=None,
        help="Custom vocabulary / bias phrases (comma/semicolon separated or text file path)",
    )
    @click.option(
        "--temp-dir",
        default="temp",
        help="Directory for intermediate temp files (default: temp)",
    )
    @click.option(
        "--audit-jsonl",
        default=_DEFAULT_AUDIT_JSONL,
        help=f"Path to JSONL audit log file (default: {_DEFAULT_AUDIT_JSONL})",
    )
    @click.option(
        "--verbose/--no-verbose",
        default=False,
        help="Verbose logging",
    )
    @click.argument("path", nargs=-1)
    def _root(
        path: tuple[str, ...],
        version: bool,
        output_dir: str | None,
        output_base: str | None,
        gemini_api_key: str | None,
        language: str,
        model: str,
        diarize: bool,
        srt: bool,
        txt: bool,
        metadata_json: bool,
        transcript_json: bool,
        ffsubsync_srt: bool,
        force: bool,
        tier: str,
        line_interval_secs: float,
        paragraph_interval_secs: float,
        request_interval_secs: float | None,
        chunk_secs: float | None,
        speakers: str | None,
        custom_vocabulary: str | None,
        temp_dir: str,
        audit_jsonl: str,
        verbose: bool,
    ) -> None:
        _LAST_OPTIONS.append(
            TranscribeOptions(
                path=list(path),
                version=version,
                output_dir=output_dir,
                output_base=output_base,
                gemini_api_key=gemini_api_key,
                language=language,
                model=model,
                diarize=diarize,
                srt=srt,
                txt=txt,
                metadata_json=metadata_json,
                transcript_json=transcript_json,
                ffsubsync_srt=ffsubsync_srt,
                force=force,
                tier=tier,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                request_interval_secs=request_interval_secs,
                chunk_secs=chunk_secs,
                speakers=speakers,
                custom_vocabulary=custom_vocabulary,
                temp_dir=temp_dir,
                audit_jsonl=audit_jsonl,
                verbose=verbose,
            )
        )

    return _root


app = _make_command()

# Container used to ferry the callback's return value out of Click's CliRunner,
# which doesn't expose the callback's return value on the Result object.
_LAST_OPTIONS: list[TranscribeOptions] = []


def build_options(argv: list[str] | None = None) -> TranscribeOptions:
    """Parse argv into a TranscribeOptions without running the main logic. For testability."""
    argv_list = list(argv) if argv is not None else []
    runner = CliRunner()
    _LAST_OPTIONS.clear()
    result = runner.invoke(
        app,
        argv_list,
        prog_name="gemini-transcribe",
        catch_exceptions=True,
    )
    if result.exit_code != 0:
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        raise SystemExit(result.exit_code)
    if not _LAST_OPTIONS:
        raise RuntimeError(f"Failed to parse options: {result.output!r}")
    return _LAST_OPTIONS[-1]


def parse_custom_vocabulary(spec: str | None) -> list[str] | None:
    """Parse comma/semicolon-delimited list or read phrases from a file."""
    if not spec:
        return None
    p = Path(spec)
    if p.is_file():
        lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line]
    import re

    raw_items = re.split(r"[,;]", spec)
    items = [item.strip() for item in raw_items if item.strip()]
    return items if items else None


def parse_speakers(spec: str) -> dict[str, str]:
    """Parse 'spk:0=궤도;spk:1=가람;' into {'spk:0': '궤도', 'spk:1': '가람'}."""
    mapping: dict[str, str] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Invalid --speakers entry (expected id=name): {part!r}")
        raw, name = part.split("=", 1)
        raw = raw.strip()
        name = name.strip()
        if not raw or not name:
            raise ValueError(f"Invalid --speakers entry (expected id=name): {part!r}")
        mapping[raw] = name
    return mapping


def format_cli_command(prog: str, opts: TranscribeOptions) -> str:
    """Format the full command line with all resolved effective options."""
    tokens: list[str] = [prog]

    if opts.tier:
        tokens.extend(["--tier", str(opts.tier)])
    if opts.gemini_api_key:
        tokens.extend(["--gemini-api-key", str(opts.gemini_api_key)])
    if opts.language:
        tokens.extend(["--language", str(opts.language)])
    if opts.model:
        tokens.extend(["--model", str(opts.model)])

    tokens.append("--diarize" if opts.diarize else "--no-diarize")
    tokens.append("--srt" if opts.srt else "--no-srt")
    tokens.append("--txt" if opts.txt else "--no-txt")
    tokens.append("--transcript-json" if opts.transcript_json else "--no-transcript-json")
    tokens.append("--metadata-json" if opts.metadata_json else "--no-metadata-json")

    if opts.ffsubsync_srt:
        tokens.append("--ffsubsync-srt")
    if opts.force:
        tokens.append("--force")
    if opts.verbose:
        tokens.append("--verbose")

    if opts.output_dir is not None:
        tokens.extend(["--output-dir", str(opts.output_dir)])
    if opts.output_base is not None:
        tokens.extend(["--output-base", str(opts.output_base)])
    if opts.temp_dir is not None:
        tokens.extend(["--temp-dir", str(opts.temp_dir)])
    if opts.audit_jsonl is not None:
        tokens.extend(["--audit-jsonl", str(opts.audit_jsonl)])
    if opts.speakers:
        tokens.extend(["--speakers", str(opts.speakers)])
    if opts.custom_vocabulary:
        tokens.extend(["--custom-vocabulary", str(opts.custom_vocabulary)])
    if opts.chunk_secs is not None:
        tokens.extend(["--chunk-secs", str(opts.chunk_secs)])

    if opts.line_interval_secs is not None:
        tokens.extend(["--line-interval-secs", str(opts.line_interval_secs)])
    if opts.paragraph_interval_secs is not None:
        tokens.extend(["--paragraph-interval-secs", str(opts.paragraph_interval_secs)])

    effective_interval = (
        (0.0 if opts.tier == "paid" else 120.0)
        if opts.request_interval_secs is None
        else float(opts.request_interval_secs)
    )
    tokens.extend(["--request-interval-secs", str(effective_interval)])

    for p in opts.path:
        tokens.append(str(p))

    if sys.platform == "win32":
        import subprocess

        return subprocess.list2cmdline(tokens)
    import shlex

    return shlex.join(tokens)


def _resolve_api_key(cli_key: str | None) -> str | None:
    """Resolve the effective Gemini API key: --gemini-api-key > $GEMINI_API_KEY > $GOOGLE_API_KEY."""
    return cli_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _run(opts: TranscribeOptions, prog: str) -> int:
    """Execute the parsed options. Returns exit code."""
    effective_key = _resolve_api_key(opts.gemini_api_key)

    if opts.version:
        print(f"{prog} {__version__}")
        if not effective_key:
            print(
                "Warning: GEMINI_API_KEY is not set. Set it or pass "
                "--gemini-api-key to transcribe."
            )
        print(usage_summary_line(api_key=effective_key, tier=opts.tier))
        return 0

    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s",
    )

    start_time = time.monotonic()

    if not opts.path:
        print(f"Usage: {prog} [OPTIONS] PATH [PATH ...]")
        print(f"Try '{prog} --help' for more information.")
        return 1

    logging.getLogger(__name__).info("+ %s", format_cli_command(prog, opts))

    produced_all: list[str] = []
    failed = False
    quota_exceeded = False

    speakers: dict[str, str] | None = None
    if opts.speakers:
        try:
            speakers = parse_speakers(opts.speakers)
        except ValueError as exc:
            logging.getLogger(__name__).error("Error: %s", exc)
            return 1

    custom_vocab = parse_custom_vocabulary(opts.custom_vocabulary)

    for pattern in opts.path:
        try:
            batch = gemini_transcribe(
                input_file=pattern,
                output_dir=opts.output_dir,
                output_base=opts.output_base,
                gemini_api_key=opts.gemini_api_key,
                language=opts.language,
                model=opts.model,
                diarize=opts.diarize,
                tier=opts.tier,
                create_srt=opts.srt,
                create_txt=opts.txt,
                create_metadata_json=opts.metadata_json,
                create_transcript_json=opts.transcript_json,
                force=opts.force,
                line_interval_secs=opts.line_interval_secs,
                paragraph_interval_secs=opts.paragraph_interval_secs,
                request_interval_secs=opts.request_interval_secs,
                chunk_secs=opts.chunk_secs,
                speakers=speakers,
                temp_dir=opts.temp_dir,
                ffsubsync_srt=opts.ffsubsync_srt,
                custom_vocabulary=custom_vocab,
                audit_jsonl=opts.audit_jsonl,
            )
            produced_all.extend(batch.output_files())
            if any(r.status == TranscribeStatus.FAILED for r in batch.results):
                failed = True
            for r in batch.results:
                if r.leftover_files():
                    logging.getLogger(__name__).debug(
                        "Leftover files for %s: %s",
                        r.input.input_file,
                        ", ".join(r.leftover_files()),
                    )
        except QuotaExceededError:
            # 429 / quota hit: no point trying the next pattern — it would
            # hit the same limit. Bail out with a distinct exit code.
            quota_exceeded = True
            break
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            logging.getLogger(__name__).error("Error: %s", exc)
            failed = True

    elapsed = time.monotonic() - start_time
    logging.getLogger(__name__).info("Total elapsed time: %.1fs", elapsed)

    print(usage_summary_line(api_key=effective_key, tier=opts.tier))

    if quota_exceeded:
        return 2
    if failed:
        return 1
    if not produced_all:
        logging.getLogger(__name__).info("No output files were produced.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parses argv with Click and runs the transcription logic.

    On parse error / help: prints to stdout and raises ``SystemExit`` (matches
    the old argparse-based behavior so external scripts keep working).
    """
    # Load .env (GEMINI_API_KEY etc.) if present in cwd
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    argv_list = list(argv) if argv is not None else sys.argv[1:]
    prog_name = Path(sys.argv[0]).name or "gemini-transcribe"

    runner = CliRunner()
    _LAST_OPTIONS.clear()
    result = runner.invoke(
        app,
        argv_list,
        prog_name=prog_name,
        catch_exceptions=True,
    )

    if result.exit_code != 0:
        if result.output:
            sys.stdout.write(result.output)
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        raise SystemExit(result.exit_code)

    if not _LAST_OPTIONS:
        # Help was shown; forward the help text to stdout and raise SystemExit(0)
        # to match argparse's behavior so external scripts that catch SystemExit
        # keep working.
        if result.output:
            sys.stdout.write(result.output)
        raise SystemExit(0)

    return _run(_LAST_OPTIONS[-1], prog_name)


if __name__ == "__main__":
    sys.exit(main())
