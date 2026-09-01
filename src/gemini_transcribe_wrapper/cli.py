"""CLI entry point for gemini-transcribe (Click-based)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click
from click.testing import CliRunner

from . import __version__
from .api import QuotaExceededError, gemini_transcribe
from .models import TranscribeStatus
from .stt import MODEL_ID
from .usage_counter import usage_summary_line


@dataclass
class TranscribeOptions:
    """All CLI options resolved into a structured object (replaces argparse.Namespace)."""

    path: list[str] = field(default_factory=list)
    version: bool = False
    output_dir: str | None = None
    output_base: str | None = None
    gemini_api_keys: list[str] = field(default_factory=list)
    language_codes: list[str] = field(default_factory=list)
    model: str = MODEL_ID
    diarized_srt_file: str | Path | bool | None = None
    srt_file: str | Path | bool | None = None
    txt_file: str | Path | bool | None = None
    transcript_json_file: str | Path | bool | None = None
    metadata_json_file: str | Path | bool | None = None
    force: bool = False
    tier: str = "free"
    line_interval_secs: float = 1.0
    paragraph_interval_secs: float = 2.5
    request_interval_secs: float | None = None
    max_chunk_secs: float | None = None
    speakers: str | None = None
    custom_vocabulary: str | None = None
    custom_vocabulary_file: str | None = None
    word_level_timestamps: bool = True
    temp_path: str = "temp"
    audit_jsonl_file: str | Path | bool | None = None
    log_level: str = "info"
    loop_until_no_input: bool = False
    loop_always: bool = False
    loop_poll_secs: int = 30
    no_file_log: bool = False
    color: str = "auto"


class _GTWCommand(click.Command):
    def format_help_text(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        text = (
            f"gemini-transcribe v{__version__} - "
            "A free video transcription CLI using gemini-3.5-transcribe that "
            "outputs .diarized.srt, .srt, and .txt files."
        )
        formatter.write_paragraph()
        with formatter.indentation():
            formatter.write_text(text)
        formatter.write_paragraph()
        formatter.write_heading("Examples")
        with formatter.indentation():
            formatter.write(
                f"{' ' * formatter.current_indent}"
                'uvx --python 3.12 --from gemini-transcribe-wrapper@latest gtw --gemini-api-keys "$GEMINI_API_KEY" -h\n'
                f"{' ' * formatter.current_indent}"
                'uvx --python 3.12 --from gemini-transcribe-wrapper@latest gtw --gemini-api-keys "$GEMINI_API_KEY" /path/to/*.mp4\n'
            )

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Moved here from ``-v`` so the version flag stays script-friendly
        # (outputs only the version number, e.g. ``0.0.63``) while the
        # free-tier usage hint still surfaces to new users reading --help.
        # Static on purpose: --help cannot compute per-key tails or time
        # until PT midnight.
        formatter.write_paragraph()
        formatter.write_heading("Free-Tier Usage")
        with formatter.indentation():
            formatter.write_text(
                "Track per-key usage at https://ai.dev (Google AI Studio "
                "dashboard) and review rate limits at "
                "https://ai.google.dev/gemini-api/docs/rate-limits. Daily "
                "free-tier quota resets at midnight Pacific Time. Run any "
                "transcription to see today's call count and time until "
                "reset printed at the end."
            )


def _make_command() -> click.Command:
    """Build the Click command. The callback appends TranscribeOptions to _LAST_OPTIONS."""

    @click.command(
        cls=_GTWCommand,
        context_settings={"help_option_names": ["-h", "--help"]},
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
        "--gemini-api-keys",
        default=None,
        help=(
            "Comma- or semicolon-separated list of Gemini API keys (e.g. 'k1,k2,k3' or 'k1;k2;k3'). "
            "Keys are used in round-robin order across chunks; on a 429 "
            "the wrapper moves the key into a cooldown pool and tries the "
            "next active key. "
            "Default: $GEMINI_API_KEYS or $GEMINI_API_KEY."
        ),
    )
    @click.option(
        "--gemini-api-key",
        "deprecated_gemini_api_key",
        default=None,
        help=(
            "[DEPRECATED — use --gemini-api-keys] Single Gemini API key. "
            "Treated as a one-element list passed to --gemini-api-keys."
        ),
    )
    @click.option(
        "--language-codes",
        default="ko-KR;en-US",
        help=(
            "Comma- or semicolon-separated BCP-47 language hints forwarded to Gemini as 'language_codes'. "
            "Default 'ko-KR;en-US'. Pass an empty string (--language-codes=\"\") to enable "
            "auto language detection (Gemini picks the spoken language). "
            "See https://ai.google.dev/gemini-api/docs/transcribe#supported-languages "
            "for the full list of supported codes."
        ),
    )
    @click.option(
        "--model",
        default=MODEL_ID,
        show_default=True,
        help="Gemini audio model id to use for transcription",
    )
    @click.option(
        "--diarized-srt-file",
        default=None,
        help=(
            "Path to output .diarized.srt file, 'auto' for default name, or 'off' to disable (default: off). "
            "When set to a path or 'auto', enables Gemini speaker diarization and writes the diarized SRT there. "
            "WARNING: Use only when strictly necessary. Enabling speaker diarization reduces the per-call "
            "audio limit from ~1 hour (59m) to ~30 min (29m), which doubles API calls and reduces overall throughput."
        ),
    )
    @click.option(
        "--srt-file",
        default=None,
        help=(
            "Path to output .srt file, or 'off' to disable (default: auto). "
            "When omitted, the filename is determined automatically."
        ),
    )
    @click.option(
        "--txt-file",
        default=None,
        help=(
            "Path to output .txt file, or 'off' to disable (default: auto). "
            "When omitted, the filename is determined automatically."
        ),
    )
    @click.option(
        "--transcript-json-file",
        default=None,
        help="Path to output .transcript.json file, or 'off' to disable (default: auto).",
    )
    @click.option(
        "--metadata-json-file",
        default=None,
        help="Path to output .metadata.json file, 'auto' for default name, or 'off' to disable (default: off).",
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
        "--max-chunk-secs",
        type=float,
        default=None,
        help=(
            "[DEVELOPER/INTERNAL ONLY — not intended for end users] Override the "
            "per-chunk ceiling in seconds (default: auto, 3540s when "
            "--no-diarize and --no-word-level-timestamps, 1740s when either is on; "
            "the hard ceiling matches the Gemini 30-min per-call limit when "
            "diarization or word-level timestamps are active)"
        ),
    )
    @click.option(
        "--speakers",
        default=None,
        help=(
            "Semicolon-separated speaker name mapping for .diarized.srt, "
            "e.g. 'spk:0=John Doe;spk:1=Jane Doe;'. (Note: only ';' is used as a "
            "delimiter because speaker names may contain commas)."
        ),
    )
    @click.option(
        "--custom-vocabulary",
        default=None,
        help="Custom vocabulary / bias phrases (comma- or semicolon-separated, or text file path)",
    )
    @click.option(
        "--custom-vocabulary-file",
        default=None,
        help="Path to a text file with one custom vocabulary term per line. "
             "Lines starting with '#' or empty lines are ignored. "
             "If the file is missing, a warning is printed and the option is ignored. "
             "Gemini Transcribe rejects custom_vocabulary when timestamps are requested, "
             "so the wrapper applies these as post-recognition bias instead of sending them to the API.",
    )
    @click.option(
        "--word-level-timestamps/--no-word-level-timestamps",
        default=True,
        help=(
            "[DEVELOPER/INTERNAL ONLY — not intended for end users] "
            "Enable/disable word-level timestamps in transcription output "
            "(default: on). When enabled, the Gemini API per-call audio limit "
            "drops from ~1 hour (59m) to ~30 min (29m), same as "
            "--diarized-srt-file. Disable to allow longer chunks when "
            "word-level timing is not needed."
        ),
    )
    @click.option(
        "--temp-path",
        default="temp",
        help="Directory for intermediate temp files (default: temp)",
    )
    @click.option(
        "--audit-jsonl-file",
        default=None,
        help=(
            "Path to JSONL audit log file (default: "
            "/tmp/gemini-transcribe-wrapper-<nodename>-<loginid>.audit.jsonl). "
            "When omitted, the wrapper resolves <nodename> to the lowercase "
            "hostname and <loginid> to the lowercase OS username, so each "
            "(computer, user) pair gets its own audit log."
        ),
    )
    @click.option(
        "--log-level",
        type=click.Choice(
            ["debug", "info", "warning", "error", "critical"],
            case_sensitive=False,
        ),
        default="info",
        help="Logging level (default: info; use 'debug' for verbose output)",
    )
    @click.option(
        "--loop-until-no-input",
        is_flag=True,
        default=False,
        help=(
            "Re-glob PATH after each pass and exit when the glob yields "
            "no matches. Use this for drop-folder workflows: process "
            "existing files, then wait silently for new arrivals and "
            "exit when the folder is drained."
        ),
    )
    @click.option(
        "--loop-always",
        is_flag=True,
        default=False,
        help=(
            "Like --loop-until-no-input but never exit on an empty pass — "
            "sleep --loop-poll-secs and re-glob forever. Use this for "
            "24/7 batch watchers."
        ),
    )
    @click.option(
        "--loop-poll-secs",
        type=click.IntRange(1, 3600),
        default=30,
        show_default=True,
        help=(
            "Seconds to sleep between empty passes under --loop-always "
            "(or after a quota 429 under either --loop* flag). Range "
            "1..3600; default 30."
        ),
    )
    @click.option(
        "--no-file-log",
        is_flag=True,
        default=False,
        help=(
            "Disable the rotating file log under "
            "<cache_dir>/logs/gemini-transcribe-wrapper.log. By default "
            "the wrapper mirrors console output to that file (5 MB × 3) "
            "so long-running --loop* sessions survive a tmux detach or "
            "SSH disconnect."
        ),
    )
    @click.option(
        "--color",
        type=click.Choice(["auto", "always", "never"], case_sensitive=False),
        default="auto",
        show_default=True,
        help=(
            "Color console output by log level. ``auto`` enables color "
            "only when stderr is a TTY (so ``2>err.log`` stays clean). "
            "``always`` forces color (useful with ``2>&1 | less -R``). "
            "``never`` disables color (useful on terminals that lie "
            "about isatty). The rotating file log is never colored."
        ),
    )
    @click.argument("path", nargs=-1)
    def _root(
        path: tuple[str, ...],
        version: bool,
        output_dir: str | None,
        output_base: str | None,
        gemini_api_keys: str | None,
        deprecated_gemini_api_key: str | None,
        language_codes: str,
        model: str,
        diarized_srt_file: str | None,
        srt_file: str | None,
        txt_file: str | None,
        transcript_json_file: str | None,
        metadata_json_file: str | None,
        force: bool,
        tier: str,
        line_interval_secs: float,
        paragraph_interval_secs: float,
        request_interval_secs: float | None,
        max_chunk_secs: float | None,
        speakers: str | None,
        custom_vocabulary: str | None,
        custom_vocabulary_file: str | None,
        word_level_timestamps: bool,
        temp_path: str,
        audit_jsonl_file: str | None,
        log_level: str,
        loop_until_no_input: bool,
        loop_always: bool,
        loop_poll_secs: int,
        no_file_log: bool,
        color: str,
    ) -> None:
        # Mutual exclusion check (issue-001). Both flags together would
        # be ambiguous: --loop-until-no-input says "exit when empty" and
        # --loop-always says "never exit". We reject the combination
        # with a clear error + exit 2.
        if loop_until_no_input and loop_always:
            raise click.UsageError(
                "--loop-until-no-input and --loop-always are mutually "
                "exclusive. Pick one."
            )
        # Parse the comma- or semicolon-separated --gemini-api-keys list.
        # Drop blanks, preserve order, drop dupes.
        parsed_keys: list[str] = []
        if gemini_api_keys:
            for part in re.split(r"[,;]", gemini_api_keys):
                part = part.strip()
                if part and part not in parsed_keys:
                    parsed_keys.append(part)
        # The deprecated singular --gemini-api-key flag is appended last
        # so explicit --gemini-api-keys takes precedence in ordering.
        if deprecated_gemini_api_key and deprecated_gemini_api_key.strip():
            v = deprecated_gemini_api_key.strip()
            if v not in parsed_keys:
                parsed_keys.append(v)
            logging.getLogger(__name__).warning(
                "--gemini-api-key is deprecated; use --gemini-api-keys=k1,k2,... "
                "instead. Treating '%s' as a one-element list.",
                _mask_cli_key(v),
            )
        # --language-codes is comma- or semicolon-separated; empty string enables auto detection.
        parsed_language_codes: list[str] = []
        if language_codes:
            for part in re.split(r"[,;]", language_codes):
                part = part.strip()
                if part and part not in parsed_language_codes:
                    parsed_language_codes.append(part)
        _LAST_OPTIONS.append(
            TranscribeOptions(
                path=list(path),
                version=version,
                output_dir=output_dir,
                output_base=output_base,
                gemini_api_keys=parsed_keys,
                language_codes=parsed_language_codes,
                model=model,
                diarized_srt_file=diarized_srt_file,
                srt_file=srt_file,
                txt_file=txt_file,
                transcript_json_file=transcript_json_file,
                metadata_json_file=metadata_json_file,
                force=force,
                tier=tier,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                request_interval_secs=request_interval_secs,
                max_chunk_secs=max_chunk_secs,
                speakers=speakers,
                custom_vocabulary=custom_vocabulary,
                custom_vocabulary_file=custom_vocabulary_file,
                word_level_timestamps=word_level_timestamps,
                temp_path=temp_path,
                audit_jsonl_file=audit_jsonl_file,
                log_level=log_level,
                loop_until_no_input=loop_until_no_input,
                loop_always=loop_always,
                loop_poll_secs=loop_poll_secs,
                no_file_log=no_file_log,
                color=color,
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
    """Parse comma- or semicolon-delimited list or read phrases from a file."""
    if not spec:
        return None
    p = Path(spec)
    if p.is_file():
        lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line]
    items = [item.strip() for item in re.split(r"[,;]", spec) if item.strip()]
    return items if items else None


def load_custom_vocabulary_file(path: str | None) -> list[str]:
    """CLI wrapper for :func:`api._load_vocabulary_file`.

    Kept as a thin shim so callers in this module (and tests) can keep
    using the ``cli.load_custom_vocabulary_file`` symbol. The single
    source of truth lives in :mod:`api`.
    """
    from .api import _load_vocabulary_file

    return _load_vocabulary_file(path)


def parse_speakers(spec: str) -> dict[str, str]:
    """Parse 'spk:0=John Doe;spk:1=Jane Doe;' into {'spk:0': 'John Doe', 'spk:1': 'Jane Doe'}."""
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
    """Format the full command line with all resolved effective options.

    API keys are redacted to ``****<last4>`` each (e.g. ``****Mw9g``) so
    the emitted command-line log line never leaks full credentials.
    """
    tokens: list[str] = [prog]

    if opts.tier:
        tokens.extend(["--tier", str(opts.tier)])
    if opts.gemini_api_keys:
        from .api import _mask_key

        redacted = ";".join(_mask_key(k) for k in opts.gemini_api_keys)
        tokens.extend(["--gemini-api-keys", redacted])
    if opts.language_codes:
        tokens.extend(["--language-codes", ";".join(opts.language_codes)])
    if opts.model:
        tokens.extend(["--model", str(opts.model)])

    if opts.diarized_srt_file is not None:
        tokens.extend(["--diarized-srt-file", str(opts.diarized_srt_file)])
    if opts.srt_file is not None:
        tokens.extend(["--srt-file", str(opts.srt_file)])
    if opts.txt_file is not None:
        tokens.extend(["--txt-file", str(opts.txt_file)])
    if opts.transcript_json_file is not None:
        tokens.extend(["--transcript-json-file", str(opts.transcript_json_file)])
    if opts.metadata_json_file is not None:
        tokens.extend(["--metadata-json-file", str(opts.metadata_json_file)])
    if opts.audit_jsonl_file is not None:
        tokens.extend(["--audit-jsonl-file", str(opts.audit_jsonl_file)])

    if opts.force:
        tokens.append("--force")
    if opts.log_level != "info":
        tokens.extend(["--log-level", opts.log_level])

    if opts.output_dir is not None:
        tokens.extend(["--output-dir", str(opts.output_dir)])
    if opts.output_base is not None:
        tokens.extend(["--output-base", str(opts.output_base)])
    if opts.temp_path is not None:
        tokens.extend(["--temp-path", str(opts.temp_path)])
    if opts.speakers:
        tokens.extend(["--speakers", str(opts.speakers)])
    if opts.custom_vocabulary:
        tokens.extend(["--custom-vocabulary", str(opts.custom_vocabulary)])
    if opts.custom_vocabulary_file:
        tokens.extend(["--custom-vocabulary-file", str(opts.custom_vocabulary_file)])
    if opts.max_chunk_secs is not None:
        tokens.extend(["--max-chunk-secs", str(opts.max_chunk_secs)])
    if not opts.word_level_timestamps:
        tokens.append("--no-word-level-timestamps")

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


def _resolve_api_keys(cli_keys: list[str]) -> list[str]:
    """Resolve the effective Gemini API key list.

    Precedence:
      1. Explicit ``--gemini-api-keys`` (CLI wins, env vars are ignored
         so users can intentionally pin a single key).
      2. ``$GEMINI_API_KEYS`` (semicolon-separated).
      3. ``$GEMINI_API_KEY`` (single, treated as a one-element list).
      4. ``$GOOGLE_API_KEY`` (single, treated as a one-element list).

    Order within each source is preserved and duplicates are dropped.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(k: object) -> None:
        k_s = k.strip() if isinstance(k, str) else ""
        if k_s and k_s not in seen:
            seen.add(k_s)
            out.append(k_s)

    # 1. CLI list — authoritative when present.
    for k in cli_keys:
        _add(k)
    if out:
        return out

    # 2-4. Fall back to env vars in precedence order.
    for k in (
        s.strip()
        for s in re.split(r"[,;]", os.environ.get("GEMINI_API_KEYS", ""))
        if s.strip()
    ):
        _add(k)
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(env_name)
        if v:
            _add(v)
    return out


def _mask_cli_key(key: str) -> str:
    """Mask a CLI-provided key for warning lines as ``[redacted]<last 4>``.

    Thin wrapper around :func:`gemini_transcribe_wrapper._key_utils.mask_key`
    kept for backward compatibility with existing callers/tests.
    """
    from ._key_utils import mask_key

    return mask_key(key)


_OLD_WORKDIR_MAX_AGE_SECS = 24 * 3600
_OLD_WORKDIR_SUFFIX = "-work"


def _format_age(secs: float) -> str:
    """Format a duration in seconds as a compact human age (e.g. ``2d 5h``, ``3h 20m``)."""
    secs_int = max(0, int(secs))
    days, rem = divmod(secs_int, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _resolve_temp_dir(opts: TranscribeOptions) -> Path:
    """Return the absolute temp directory the wrapper uses for workdirs.

    Mirrors the resolution in ``api._setup_workdir``: relative paths are
    anchored to ``--output-dir`` when given, otherwise to the current
    working directory. Returns the resolved path even if it does not yet
    exist on disk.
    """
    base = Path(opts.temp_path)
    if not base.is_absolute():
        anchor = Path(opts.output_dir) if opts.output_dir else Path.cwd()
        base = anchor / base
    return base


def cleanup_old_workdirs(
    opts: TranscribeOptions,
    *,
    now: float | None = None,
    max_age_secs: float = _OLD_WORKDIR_MAX_AGE_SECS,
) -> int:
    """Delete ``*-work`` directories under the temp dir older than ``max_age_secs``.

    Best-effort: per-directory errors (permission, race with another
    process) are logged and skipped, never raised. Returns the number of
    directories actually removed.

    Used by ``_run`` before the input-file loop so stale workdirs from a
    previous run (e.g. with incompatible ``--max-chunk-secs``) don't
    pollute disk or get mistakenly reused.
    """
    temp_dir = _resolve_temp_dir(opts)
    if not temp_dir.is_dir():
        return 0
    current = now if now is not None else time.time()
    cutoff = current - max_age_secs
    deleted = 0
    logger = logging.getLogger(__name__)
    for entry in sorted(temp_dir.iterdir()):
        if not entry.is_dir() or not entry.name.endswith(_OLD_WORKDIR_SUFFIX):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as exc:
            logger.debug("Skipping %s: stat failed (%s)", entry, exc)
            continue
        if mtime >= cutoff:
            continue
        age_str = _format_age(current - mtime)
        logger.info(
            "Cleaning up old work directory %s, created %s ago", entry, age_str
        )
        try:
            shutil.rmtree(entry)
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to remove old work directory %s: %s", entry, exc)
    return deleted


def _run(opts: TranscribeOptions, prog: str) -> int:
    """Execute the parsed options. Returns exit code."""
    effective_keys = _resolve_api_keys(opts.gemini_api_keys)
    # For the -v summary line and the "no key configured" warning we
    # only need the first key (preserves the legacy single-key UX).
    effective_key = effective_keys[0] if effective_keys else None

    if opts.version:
        # -v prints only the version (e.g. "0.0.63"). The free-tier usage
        # hint and the no-key warning used to be printed here; they moved
        # to --help (see ``_GTWCommand.format_epilog``) so the version
        # output stays script-friendly and parseable.
        print(__version__)
        return 0

    log_level = getattr(logging, opts.log_level.upper())

    # Issue-006: console handler picks a color formatter when stderr is a
    # TTY (or always/never per the user override). The shared formatter
    # classes live in :mod:`gemini_transcribe_wrapper._logging`.
    from . import _logging as gtw_logging

    use_color = gtw_logging.resolve_color_mode(opts.color)
    formatter = gtw_logging._ColorFormatter(
        "%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s",
        use_color=use_color,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Only install our tz-aware handler when the root logger has none.
    # Tests attach their own handlers (pytest's ``caplog``); clobbering
    # them would break log capture. Production starts with no handlers.
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(log_level)

    # Mirror console output to a rotating file (issue-005, spec §4.4).
    # Skipped under --no-file-log (CI runs, ephemeral containers, etc.)
    # or when the cache directory cannot be created (warning logged,
    # console handler still works). The file handler always uses the
    # plain _TzFormatter — no color codes ever leak into log files
    # (issue-006 §Acceptance).
    if not opts.no_file_log:
        gtw_logging.setup_file_logging()

    start_time = time.monotonic()

    if not opts.path:
        print(f"Usage: {prog} [OPTIONS] PATH [PATH ...]")
        print(f"Try '{prog} --help' for more information.")
        return 1

    # Best-effort cleanup of stale workdirs from prior runs (default: 24h+)
    # so they don't accumulate or get reused with incompatible options.
    cleanup_old_workdirs(opts)

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

    custom_vocab_list = parse_custom_vocabulary(opts.custom_vocabulary) or []
    custom_vocab_list += load_custom_vocabulary_file(opts.custom_vocabulary_file)
    custom_vocab = custom_vocab_list if custom_vocab_list else None

    def _run_one_pass() -> None:
        """One pass over ``opts.path`` (issue-001's loop driver calls this)."""
        nonlocal quota_exceeded, failed  # type: ignore[misc]
        for pattern in opts.path:
            try:
                batch = gemini_transcribe(
                    input_file=pattern,
                    output_dir=opts.output_dir,
                    output_base=opts.output_base,
                    gemini_api_keys=effective_keys,
                    language_codes=opts.language_codes or None,
                    model=opts.model,
                    srt_file=opts.srt_file,
                    txt_file=opts.txt_file,
                    diarized_srt_file=opts.diarized_srt_file,
                    transcript_json_file=opts.transcript_json_file,
                    metadata_json_file=opts.metadata_json_file,
                    tier=opts.tier,
                    force=opts.force,
                    line_interval_secs=opts.line_interval_secs,
                    paragraph_interval_secs=opts.paragraph_interval_secs,
                    request_interval_secs=opts.request_interval_secs,
                    max_chunk_secs=opts.max_chunk_secs,
                    speakers=speakers,
                    temp_path=opts.temp_path,
                    custom_vocabulary=custom_vocab,
                    custom_vocabulary_file=opts.custom_vocabulary_file,
                    audit_jsonl_file=opts.audit_jsonl_file,
                    word_level_timestamps=opts.word_level_timestamps,
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
                # 429 / quota hit: no point trying the next pattern — it
                # would hit the same limit. Re-raise so the loop driver
                # can decide whether to retry (--loop*) or exit (default).
                raise
            except Exception as exc:  # noqa: BLE001 - CLI boundary
                logging.getLogger(__name__).error("Error: %s", exc)
                failed = True

    if opts.loop_until_no_input or opts.loop_always:
        from . import _loop as loop_driver

        try:
            loop_driver.run_with_loop(
                patterns=list(opts.path),
                loop_until_no_input=opts.loop_until_no_input,
                loop_always=opts.loop_always,
                loop_poll_secs=opts.loop_poll_secs,
                run_pass=lambda _matches: _run_one_pass(),
            )
        except QuotaExceededError:
            # Loop driver re-raised because no loop flag was active OR
            # because the user explicitly wants the quota exit code.
            # (The driver only re-raises when no loop flag is set; with
            # a loop flag it always sleeps and retries, so we shouldn't
            # land here under --loop*. But guard anyway.)
            quota_exceeded = True
        except KeyboardInterrupt:
            # Loop driver returns 130; we just propagate the exit code
            # below via a sentinel attribute on opts.
            opts._loop_interrupted = True
    else:
        try:
            _run_one_pass()
        except QuotaExceededError:
            quota_exceeded = True

    elapsed = time.monotonic() - start_time
    logging.getLogger(__name__).info("Total elapsed time: %.1fs", elapsed)

    print(usage_summary_line(api_key=effective_key, tier=opts.tier))

    if quota_exceeded:
        return 2
    if getattr(opts, "_loop_interrupted", False):
        return 130
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
