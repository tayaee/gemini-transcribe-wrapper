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
from .usage_counter import usage_summary_line


class _HelpAction(argparse.Action):
    """Help action that appends the daily API usage summary as the last line."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        print()
        # --help is processed before args parse, so only env-var keys are in scope.
        print(usage_summary_line(api_key=_resolve_api_key(None)))
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    # Use the invoked command name (gemini-transcribe or the gt shortcut) as prog.
    prog = Path(sys.argv[0]).name or "gemini-transcribe"
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"{prog} v{__version__} - Zero-config Gemini 3.5 "
        "Transcribe wrapper (auto ffmpeg/ffsubsync).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help",
        action=_HelpAction,
        nargs=0,
        help="show this help message and exit",
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
    parser.add_argument("--line-interval-secs", type=float, default=1.0, help="TXT newline break threshold (default: 1.0)")
    parser.add_argument("--paragraph-interval-secs", type=float, default=2.5, help="TXT paragraph break threshold (default: 2.5)")
    parser.add_argument("--request-interval-secs", type=float, default=30.0, help="Delay between API calls (default: 30.0)")
    parser.add_argument("--chunk-secs", type=float, default=None, help="Fixed chunk length in seconds (default: auto, 59-min logical units when --no-diarize, 29-min when --diarize; hard ceiling of 29 min enforced to fit the Gemini 30-min per-call limit)")
    parser.add_argument("--speakers", default=None, help="Speaker name mapping for .diarized.srt, e.g. 'spk:0=궤도;spk:1=가람;'")
    parser.add_argument("--temp-dir", default=None, help="Directory for intermediate temp files (default: alongside output)")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser


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
        print()
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

    for pattern in args.path:
        try:
            batch = gemini_transcribe(
                input_file=pattern,
                output_dir=args.output_dir,
                output_base=args.output_base,
                gemini_api_key=args.gemini_api_key,
                language=args.language,
                diarize=args.diarize,
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
