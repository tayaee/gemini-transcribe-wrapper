"""CLI entry point for gemini-transcribe."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__
from .api import QuotaExceededError, gemini_transcribe
from .models import TranscribeStatus
from .stt import get_audit_log_path
from .usage_counter import usage_summary_line


def build_parser() -> argparse.ArgumentParser:
    # Use the invoked command name (gemini-transcribe or the gt shortcut) as prog.
    prog = Path(sys.argv[0]).name or "gemini-transcribe"
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"{prog} v{__version__} - Zero-config Gemini 3.5 "
        "Transcribe wrapper (auto ffmpeg/ffsubsync).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=True,
    )
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="Show version and exit",
    )
    parser.add_argument("path", nargs="*", help="Input file or glob pattern (*.mp4, *.mp3, ...)")
    parser.add_argument("--output-dir", default=None, help="Directory for output files (default: alongside input)")
    parser.add_argument("--output-base", default=None, help="Base name for output files (default: input stem)")
    parser.add_argument("--gemini-api-key", default=None, help="Gemini API key (default: $GEMINI_API_KEY)")
    parser.add_argument("--language", default="ko-KR", help="BCP-47 language code (default: ko-KR)")
    parser.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=False, help="Enable speaker diarization (default: off). When on, the wrapper uses 29-min chunks for better diarization accuracy and emits .diarized.* outputs. When off, it cuts the file into 59-min logical units (each split into 2x 29-min API calls to stay under the 30-min per-call limit), keeping free-tier API usage low.")
    parser.add_argument("--srt", action=argparse.BooleanOptionalAction, default=True, help="Generate .srt subtitles (default: on)")
    parser.add_argument("--txt", action=argparse.BooleanOptionalAction, default=True, help="Generate .txt transcript text (default: on)")
    parser.add_argument("--metadata-json", action=argparse.BooleanOptionalAction, default=False, help="Keep .metadata.json output (default: off)")
    parser.add_argument("--transcript-json", action=argparse.BooleanOptionalAction, default=True, help="Keep <base>.transcript.json for later re-render (default: on)")
    parser.add_argument("--ffsubsync-srt", action="store_true", help="Also write <base>.ffsubsync.srt aligned to audio (default: off)")
    parser.add_argument("--force", action="store_true", help="Re-process even if outputs exist")
    parser.add_argument("--tier", choices=["free", "paid"], default="free", help="Gemini API pricing tier (default: free). When 'free', enforces 60s cooldown between API calls; when 'paid', rate limiting is disabled unless overridden by --request-interval-secs.")
    parser.add_argument("--line-interval-secs", type=float, default=1.0, help="TXT newline break threshold (default: 1.0)")
    parser.add_argument("--paragraph-interval-secs", type=float, default=2.5, help="TXT paragraph break threshold (default: 2.5)")
    parser.add_argument("--request-interval-secs", type=float, default=None, help="Delay between API calls (default: 60.0 for free tier, 0.0 for paid tier)")
    parser.add_argument("--chunk-secs", type=float, default=None, help="Fixed chunk length in seconds (default: auto, 59-min logical units when --no-diarize, 29-min when --diarize; hard ceiling of 29 min enforced to fit the Gemini 30-min per-call limit)")
    parser.add_argument("--speakers", default=None, help="Speaker name mapping for .diarized.srt, e.g. 'spk:0=궤도;spk:1=가람;'")
    parser.add_argument("--custom-vocabulary", default=None, help="Custom vocabulary / bias phrases (comma/semicolon separated or text file path)")
    parser.add_argument("--temp-dir", default="temp", help="Directory for intermediate temp files (default: temp)")
    parser.add_argument("--audit-jsonl", default=str(get_audit_log_path()), help=f"Path to JSONL audit log file (default: {get_audit_log_path()})")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


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


def format_cli_command(prog: str, args: argparse.Namespace) -> str:
    """Format the full command line with all resolved effective options."""
    tokens: list[str] = [prog]

    if getattr(args, "tier", None):
        tokens.extend(["--tier", str(args.tier)])
    if getattr(args, "gemini_api_key", None):
        tokens.extend(["--gemini-api-key", str(args.gemini_api_key)])
    if getattr(args, "language", None):
        tokens.extend(["--language", str(args.language)])

    tokens.append("--diarize" if getattr(args, "diarize", False) else "--no-diarize")
    tokens.append("--srt" if getattr(args, "srt", True) else "--no-srt")
    tokens.append("--txt" if getattr(args, "txt", True) else "--no-txt")
    tokens.append("--transcript-json" if getattr(args, "transcript_json", True) else "--no-transcript-json")
    tokens.append("--metadata-json" if getattr(args, "metadata_json", False) else "--no-metadata-json")

    if getattr(args, "ffsubsync_srt", False):
        tokens.append("--ffsubsync-srt")
    if getattr(args, "force", False):
        tokens.append("--force")
    if getattr(args, "verbose", False):
        tokens.append("--verbose")

    if getattr(args, "output_dir", None) is not None:
        tokens.extend(["--output-dir", str(args.output_dir)])
    if getattr(args, "output_base", None) is not None:
        tokens.extend(["--output-base", str(args.output_base)])
    if getattr(args, "temp_dir", None) is not None:
        tokens.extend(["--temp-dir", str(args.temp_dir)])
    if getattr(args, "audit_jsonl", None) is not None:
        tokens.extend(["--audit-jsonl", str(args.audit_jsonl)])
    if getattr(args, "speakers", None):
        tokens.extend(["--speakers", str(args.speakers)])
    if getattr(args, "custom_vocabulary", None):
        tokens.extend(["--custom-vocabulary", str(args.custom_vocabulary)])
    if getattr(args, "chunk_secs", None) is not None:
        tokens.extend(["--chunk-secs", str(args.chunk_secs)])

    if getattr(args, "line_interval_secs", None) is not None:
        tokens.extend(["--line-interval-secs", str(args.line_interval_secs)])
    if getattr(args, "paragraph_interval_secs", None) is not None:
        tokens.extend(["--paragraph-interval-secs", str(args.paragraph_interval_secs)])

    effective_interval = (
        (0.0 if getattr(args, "tier", "free") == "paid" else 60.0)
        if getattr(args, "request_interval_secs", None) is None
        else float(args.request_interval_secs)
    )
    tokens.extend(["--request-interval-secs", str(effective_interval)])

    for p in getattr(args, "path", []):
        tokens.append(str(p))

    if sys.platform == "win32":
        import subprocess

        return subprocess.list2cmdline(tokens)
    import shlex

    return shlex.join(tokens)


def _resolve_api_key(cli_key: str | None) -> str | None:
    """Resolve the effective Gemini API key: --gemini-api-key > $GEMINI_API_KEY > $GOOGLE_API_KEY."""
    return cli_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def main(argv: list[str] | None = None) -> int:
    # Load .env (GEMINI_API_KEY etc.) if present in cwd
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    prog = Path(sys.argv[0]).name or "gemini-transcribe"
    effective_key = _resolve_api_key(args.gemini_api_key)

    if args.version:
        print(f"{prog} {__version__}")
        if not effective_key:
            print(
                "Warning: GEMINI_API_KEY is not set. Set it or pass "
                "--gemini-api-key to transcribe."
            )
        print(usage_summary_line(api_key=effective_key))
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    start_time = time.monotonic()

    if not args.path:
        parser.print_usage()
        return 1

    logging.getLogger(__name__).info("+ %s", format_cli_command(prog, args))

    produced_all: list[str] = []
    failed = False
    quota_exceeded = False

    speakers: dict[str, str] | None = None
    if args.speakers:
        try:
            speakers = parse_speakers(args.speakers)
        except ValueError as exc:
            logging.getLogger(__name__).error("Error: %s", exc)
            return 1

    custom_vocab = parse_custom_vocabulary(args.custom_vocabulary)

    for pattern in args.path:
        try:
            batch = gemini_transcribe(
                input_file=pattern,
                output_dir=args.output_dir,
                output_base=args.output_base,
                gemini_api_key=args.gemini_api_key,
                language=args.language,
                diarize=args.diarize,
                tier=args.tier,
                create_srt=args.srt,
                create_txt=args.txt,
                create_metadata_json=args.metadata_json,
                create_transcript_json=args.transcript_json,
                force=args.force,
                line_interval_secs=args.line_interval_secs,
                paragraph_interval_secs=args.paragraph_interval_secs,
                request_interval_secs=args.request_interval_secs,
                chunk_secs=args.chunk_secs,
                speakers=speakers,
                temp_dir=args.temp_dir,
                ffsubsync_srt=args.ffsubsync_srt,
                custom_vocabulary=custom_vocab,
                audit_jsonl=args.audit_jsonl,
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
    logging.getLogger(__name__).info(
        "Total elapsed time: %.1fs", elapsed
    )

    print(usage_summary_line(api_key=effective_key))

    if quota_exceeded:
        return 2
    if failed:
        return 1
    if not produced_all:
        logging.getLogger(__name__).info("No output files were produced.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
