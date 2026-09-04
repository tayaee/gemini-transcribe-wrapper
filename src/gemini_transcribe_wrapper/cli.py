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
    output_stem: str | None = None
    gemini_api_keys: list[str] = field(default_factory=list)
    language_codes: list[str] = field(default_factory=list)
    model: str = MODEL_ID
    diarized_srt_file: str | Path | bool | None = None
    srt_file: str | Path | bool | None = None
    txt_file: str | Path | bool | None = None
    transcript_json_file: str | Path | bool | None = None
    metadata_json_file: str | Path | bool | None = None
    force: bool = False
    force_all: bool = False
    tier: str = "free"
    line_interval_secs: float = 0.2
    paragraph_interval_secs: float = 2.5
    txt_width: int = 65
    request_interval_secs: float | None = None
    max_chunk_secs: float | None = None
    speakers_txt_file: str | None = "auto"
    vocab_txt_file: str | None = "auto"
    word_level_timestamps: bool | None = None
    temp_path: str = "temp"
    audit_jsonl_file: str | Path | bool | None = None
    log_level: str = "info"
    loop_until_no_input: bool = False
    loop_always: bool = False
    loop_poll_secs: int = 30
    no_file_log: bool = False
    color: str = "auto"


_COMPACT_DESCRIPTIONS: dict[str, str] = {
    "gemini_api_keys": "Comma- or semicolon-separated Gemini API keys [default: $GEMINI_API_KEYS]",
    "speakers_txt_file": "Speaker mapping file for .diarized.srt, or 'off' [default: auto]",
    "vocab_txt_file": "Vocabulary file for recognition bias, or 'off' [default: auto]",
    "audit_jsonl_file": "Path to JSONL audit log file, or 'off'",
    "diarized_srt_file": "Output .diarized.srt path, 'auto', or 'off' [default: auto]",
    "metadata_json_file": "Output .metadata.json path, 'auto', or 'off' [default: off]",
    "output_dir": "Directory for output files [default: alongside input]",
    "output_stem": "Stem for output files [default: input stem]",
    "srt_file": "Output .srt path, 'auto', or 'off' [default: auto]",
    "temp_path": "Directory for intermediate temp files [default: temp]",
    "transcript_json_file": "Output transcript JSON path, 'auto', or 'off' [default: auto]",
    "txt_file": "Output .txt path, 'auto', or 'off' [default: auto]",
    "language_codes": "Spoken language hints (e.g. 'ko-KR,en-US') [default: ko-KR,en-US]",
    "max_chunk_secs": "Override chunk ceiling in seconds (default: auto, 1800s/3600s)",
    "model": "Gemini audio model id [default: gemini-3.5-transcribe]",
    "force": "Re-generate .diarized.srt, .srt, .txt",
    "force_all": "Force re-call Gemini API and regenerate transcript",
    "line_interval_secs": "TXT newline break threshold [default: 0.2]",
    "paragraph_interval_secs": "TXT paragraph break threshold [default: 2.5]",
    "txt_width": "Column width for .txt wrapping [default: 65]",
    "color": "Color console output by log level [default: auto]",
    "loop_always": "Re-glob forever with --loop-poll-secs delay",
    "loop_poll_secs": "Polling sleep seconds under --loop-always [default: 30]",
    "loop_until_no_input": "Drain folder and exit when no matching files remain",
    "request_interval_secs": "Delay between API calls [default: 120.0 free, 0.0 paid]",
    "tier": "Gemini API pricing tier [default: free]",
    "version": "Show version and exit",
    "help": "Show compact help and exit; pass 'all' (--help all) for full descriptions",
    "log_level": "Logging level (default: info; use 'debug' for verbose)",
}

_OPTION_GROUPS: list[tuple[str, list[str]]] = [
    (
        "API Key",
        ["gemini_api_keys"],
    ),
    (
        "Input Files",
        ["speakers_txt_file", "vocab_txt_file"],
    ),
    (
        "Output Files",
        [
            "audit_jsonl_file",
            "diarized_srt_file",
            "metadata_json_file",
            "output_dir",
            "output_stem",
            "srt_file",
            "temp_path",
            "transcript_json_file",
            "txt_file",
        ],
    ),
    (
        "Input Options",
        ["language_codes", "max_chunk_secs", "model"],
    ),
    (
        "Output Options",
        [
            "force",
            "force_all",
            "line_interval_secs",
            "paragraph_interval_secs",
            "txt_width",
        ],
    ),
    (
        "Other Options",
        [
            "color",
            "loop_always",
            "loop_poll_secs",
            "loop_until_no_input",
            "request_interval_secs",
            "tier",
        ],
    ),
    (
        "Help",
        [
            "version",
            "help",
            "log_level",
        ],
    ),
]


class _HelpOption(click.Option):
    """``--help`` option that supports ``--help all`` for full descriptions.

    ``--help`` (or ``-h``) alone shows compact 1-line descriptions.
    ``--help all`` (or ``-h all``) shows the full detailed descriptions,
    replacing the previous ``--help-all`` flag. Detection of the
    ``all`` qualifier happens in :func:`_normalize_help_argv` (called
    from ``main``/``build_options`` before Click sees the argv) because
    Click would otherwise consume ``all`` into the ``path`` positional
    argument before this callback runs.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("is_flag", True)
        kwargs.setdefault("expose_value", False)
        kwargs.setdefault("is_eager", True)
        kwargs.setdefault("flag_value", True)
        kwargs.setdefault("default", False)
        super().__init__(*args, **kwargs)

    def handle_parse_result(self, ctx: click.Context, opts, args):
        if opts.get(self.name) and not ctx.resilient_parsing:
            if _HELP_ALL_REQUESTED:
                ctx.meta["help_all"] = True
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit()
        return super().handle_parse_result(ctx, opts, args)


# Module-level flag set by ``_normalize_help_argv`` before Click parses
# argv. ``_HelpOption.handle_parse_result`` reads it to decide between
# compact (default) and full descriptions.
_HELP_ALL_REQUESTED: bool = False


def _normalize_help_argv(argv: list[str]) -> list[str]:
    """Rewrite ``--help all`` (or ``-h all``) to ``--help`` and record intent.

    Click would otherwise bind the trailing ``all`` to the variadic
    ``path`` positional argument (``nargs=-1``), so detection must
    happen before Click's parser runs. The function mutates
    ``_HELP_ALL_REQUESTED`` and returns a rewritten argv with the
    trailing ``all`` stripped.
    """
    global _HELP_ALL_REQUESTED
    _HELP_ALL_REQUESTED = False
    out: list[str] = []
    skip_next = False
    for i, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token in ("--help", "-h") and i + 1 < len(argv) and argv[i + 1] == "all":
            _HELP_ALL_REQUESTED = True
            out.append(token)
            skip_next = True
        else:
            out.append(token)
    return out


def _term_len(text: str) -> int:
    return len(click.unstyle(text))


def _write_aligned_dl(
    formatter: click.HelpFormatter,
    rows: list[tuple[str, str]],
    first_col: int,
    col_spacing: int = 2,
) -> None:
    for first, second in rows:
        indent = " " * formatter.current_indent
        formatter.write(f"{indent}{first}")
        if not second:
            formatter.write("\n")
            continue
        flen = _term_len(first)
        if flen <= first_col - col_spacing:
            formatter.write(" " * (first_col - flen))
        else:
            formatter.write("\n")
            formatter.write(" " * (first_col + formatter.current_indent))

        text_width = max(formatter.width - first_col - 2, 10)
        wrapped_text = click.wrap_text(second, text_width, preserve_paragraphs=True)
        lines = wrapped_text.splitlines()

        if lines:
            formatter.write(f"{lines[0]}\n")
            indent_cont = " " * (first_col + formatter.current_indent)
            for line in lines[1:]:
                formatter.write(f"{indent_cont}{line}\n")
        else:
            formatter.write("\n")


class _GTWCommand(click.Command):
    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Ensure terminal width is utilized so compact 1-line options do not wrap prematurely
        term_width = shutil.get_terminal_size(fallback=(120, 24)).columns
        formatter.width = max(formatter.width, term_width - 2, 110)

        records: dict[str, tuple[str, str]] = {}
        for param in self.get_params(ctx):
            rec = param.get_help_record(ctx)
            if rec:
                name = param.name
                opts = getattr(param, "opts", [])
                if any(o in ("-h", "--help") for o in opts):
                    name = "help"
                records[name] = rec

        is_full = bool(ctx.meta.get("help_all", False))

        all_group_data: list[tuple[str, list[tuple[str, str]]]] = []
        max_opt_len = 0
        seen: set[str] = set()
        for title, param_names in _OPTION_GROUPS:
            group_records = []
            for name in param_names:
                if name in records:
                    opt_spec, full_help = records[name]
                    if is_full:
                        help_text = full_help
                    else:
                        help_text = _COMPACT_DESCRIPTIONS.get(name, full_help)
                    group_records.append((opt_spec, help_text))
                    max_opt_len = max(max_opt_len, _term_len(opt_spec))
                    seen.add(name)
            if group_records:
                all_group_data.append((title, group_records))

        remaining = [records[k] for k in records if k not in seen]
        for opt_spec, _ in remaining:
            max_opt_len = max(max_opt_len, _term_len(opt_spec))

        # Uniform vertical alignment across ALL sections
        first_col = max_opt_len + 2

        for title, group_records in all_group_data:
            with formatter.section(title):
                _write_aligned_dl(formatter, group_records, first_col)

        if remaining:
            with formatter.section("Additional Options"):
                _write_aligned_dl(formatter, remaining, first_col)

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
                "uv tool install --python 3.12 gemini-transcribe-wrapper@latest\n"
                f"{' ' * formatter.current_indent}"
                'gtw --gemini-api-keys "<comma-separated-free-tier-api-keys>" -h\n'
                f"{' ' * formatter.current_indent}"
                'gtw --gemini-api-keys "<comma-separated-free-tier-api-keys>" *.mp4\n'
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
        context_settings={
            # Disable Click's default --help flag so our custom _HelpOption
            # can accept a following ``all`` argument (``--help all``).
            "help_option_names": [],
            "max_content_width": 240,
        },
    )
    @click.option(
        "-v",
        "--version",
        is_flag=True,
        default=False,
        help="Show version and exit",
    )
    @click.option(
        "-h",
        "--help",
        "help_mode",
        cls=_HelpOption,
        help=(
            "Show this message and exit. Pass 'all' (as ``--help all``) "
            "for full detailed descriptions."
        ),
    )
    @click.option(
        "--gemini-api-keys",
        default=None,
        help=(
            "(Optional) Comma- or semicolon-separated list of Gemini API keys (e.g. 'k1,k2,k3' or 'k1;k2;k3'). "
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
        hidden=True,
        help=(
            "(Optional) [DEPRECATED — use --gemini-api-keys] Single Gemini API key. "
            "Treated as a one-element list passed to --gemini-api-keys."
        ),
    )
    @click.option(
        "--speakers-txt-file",
        metavar="PATH",
        default="auto",
        help=(
            "(Optional) Path to speaker mapping text file for .diarized.srt (recommended format: "
            "one per line like '[spk:0]=John Doe:'), or 'auto' (default: auto). "
            "Specify 'off' to disable."
        ),
    )
    @click.option(
        "--vocab-txt-file",
        metavar="PATH",
        default="auto",
        help=(
            "(Optional) Path to vocabulary text file, or 'auto' (default: auto). "
            "When 'auto', automatically picks up .vocab.txt if it exists. "
            "Path can also be explicitly specified, or 'off' to disable. "
            "Lines starting with '#' or empty lines are ignored. "
            "Gemini Transcribe rejects custom_vocabulary when timestamps are requested, "
            "so the wrapper applies these as post-recognition bias instead of sending them to the API."
        ),
    )
    @click.option(
        "--audit-jsonl-file",
        metavar="PATH",
        default=None,
        help=(
            "(Optional) Path to JSONL audit log file (default: "
            "~/.cache/gemini-transcribe-wrapper/<api-key-tail>/audit.jsonl, "
            "or 'off' to disable). When omitted, each Gemini API key "
            "maintains its own separate audit log."
        ),
    )
    @click.option(
        "--diarized-srt-file",
        metavar="PATH",
        default=None,
        help=(
            "(Optional) Path to output .diarized.srt file, 'auto' for default name, or 'off' to disable (default: auto). "
            "When enabled, Gemini speaker diarization is performed and written to the diarized SRT. "
            "Pass --diarized-srt-file=off to disable speaker diarization."
        ),
    )
    @click.option(
        "--metadata-json-file",
        metavar="PATH",
        default=None,
        help="(Optional) Path to output .metadata.json file, 'auto' for default name, or 'off' to disable (default: off).",
    )
    @click.option(
        "--output-dir",
        metavar="PATH",
        default=None,
        help="(Optional) Directory for output files (default: alongside input)",
    )
    @click.option(
        "--output-stem",
        default=None,
        help="(Optional) Stem for output files (default: input stem)",
    )
    @click.option(
        "--srt-file",
        metavar="PATH",
        default=None,
        help=(
            "(Optional) Path to output .srt file, or 'off' to disable (default: auto). "
            "When omitted, the filename is determined automatically."
        ),
    )
    @click.option(
        "--temp-path",
        metavar="PATH",
        default="temp",
        help="(Optional) Directory for intermediate temp files (default: temp)",
    )
    @click.option(
        "--transcript-json-file",
        metavar="PATH",
        default=None,
        help=(
            "(Optional) Path to output transcript JSON file, 'auto' for default, or 'off' to disable (default: auto). "
            "When omitted, the filename is determined automatically."
        ),
    )
    @click.option(
        "--txt-file",
        metavar="PATH",
        default=None,
        help=(
            "(Optional) Path to output .txt file, or 'off' to disable (default: auto). "
            "When omitted, the filename is determined automatically."
        ),
    )
    @click.option(
        "--language-codes",
        metavar="CSV",
        default="ko-KR,en-US",
        help=(
            "(Optional) Comma-separated BCP-47 language hints forwarded to Gemini as 'language_codes' (e.g. 'ko-KR,en-US'). "
            "Default 'ko-KR,en-US'. Pass an empty string (--language-codes=\"\") to enable "
            "auto language detection (Gemini picks the spoken language). "
            "See https://ai.google.dev/gemini-api/docs/transcribe#supported-languages "
            "for the full list of supported codes."
        ),
    )
    @click.option(
        "--max-chunk-secs",
        type=float,
        default=None,
        help=(
            "(Optional) [DEVELOPER/INTERNAL ONLY — not intended for end users] Override the "
            "per-chunk ceiling in seconds (default: auto, 3600s when "
            "only .txt is output, 1800s when .srt or .diarized.srt is on; "
            "the hard ceiling matches the Gemini 30-min per-call limit when "
            "diarization or subtitles are active)"
        ),
    )
    @click.option(
        "--model",
        default=MODEL_ID,
        show_default=True,
        help="(Optional) Gemini audio model id to use for transcription",
    )
    @click.option(
        "--force/--no-force",
        default=False,
        help="(Optional) Re-generate .diarized.srt, .srt, .txt",
    )
    @click.option(
        "--force-all/--no-force-all",
        default=False,
        help=(
            "(Optional) Like --force, but also delete any cached .transcript.json so "
            "the Gemini API is called again. Useful when the transcript "
            "itself is wrong (model upgrade, vocabulary change, etc.). "
            "Mutually exclusive with --force."
        ),
    )
    @click.option(
        "--line-interval-secs",
        type=float,
        default=0.2,
        help="(Optional) TXT newline break threshold (default: 0.2)",
    )
    @click.option(
        "--paragraph-interval-secs",
        type=float,
        default=2.5,
        help="(Optional) TXT paragraph break threshold (default: 2.5)",
    )
    @click.option(
        "--txt-width",
        type=int,
        default=65,
        show_default=True,
        help="(Optional) Visual display column width for .txt line wrapping (default: 65; CJK fullwidth chars count as 2)",
    )
    @click.option(
        "--color",
        type=click.Choice(["auto", "always", "never"], case_sensitive=False),
        metavar="{auto|always|never}",
        default="auto",
        show_default=True,
        help=(
            "(Optional) Color console output by log level. ``auto`` enables color "
            "only when stderr is a TTY (so ``2>err.log`` stays clean). "
            "``always`` forces color (useful with ``2>&1 | less -R``). "
            "``never`` disables color (useful on terminals that lie "
            "about isatty). The rotating file log is never colored."
        ),
    )
    @click.option(
        "--log-level",
        type=click.Choice(
            ["debug", "info", "warning", "error", "critical"],
            case_sensitive=False,
        ),
        metavar="{debug|info|error}",
        default="info",
        help="(Optional) Logging level (default: info; use 'debug' for verbose output)",
    )
    @click.option(
        "--loop-always",
        is_flag=True,
        default=False,
        help=(
            "(Optional) Like --loop-until-no-input but never exit on an empty pass — "
            "sleep --loop-poll-secs and re-glob forever. Use this for "
            "24/7 batch watchers."
        ),
    )
    @click.option(
        "--loop-poll-secs",
        type=click.IntRange(1, 3600),
        metavar="INTEGER",
        default=30,
        show_default=True,
        help=(
            "(Optional) Seconds to sleep between empty passes under --loop-always "
            "(or after a quota 429 under either --loop* flag). Range "
            "1..3600; default 30."
        ),
    )
    @click.option(
        "--loop-until-no-input",
        is_flag=True,
        default=False,
        help=(
            "(Optional) Re-glob PATH after each pass and exit when the glob yields "
            "no matches. Use this for drop-folder workflows: process "
            "existing files, then wait silently for new arrivals and "
            "exit when the folder is drained."
        ),
    )
    @click.option(
        "--no-file-log",
        is_flag=True,
        default=False,
        hidden=True,
    )
    @click.option(
        "--request-interval-secs",
        type=float,
        default=None,
        help="(Optional) Delay between API calls (default: 120.0 for free tier, 0.0 for paid tier)",
    )
    @click.option(
        "--tier",
        type=click.Choice(["free", "paid"], case_sensitive=False),
        metavar="{free|paid}",
        default="free",
        help=(
            "(Optional) Gemini API pricing tier (default: free). When 'free', enforces 60s "
            "cooldown between API calls; when 'paid', rate limiting is disabled "
            "unless overridden by --request-interval-secs."
        ),
    )
    @click.argument("path", nargs=-1)
    def _root(
        path: tuple[str, ...],
        version: bool,
        output_dir: str | None,
        output_stem: str | None,
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
        force_all: bool,
        tier: str,
        line_interval_secs: float,
        paragraph_interval_secs: float,
        txt_width: int,
        request_interval_secs: float | None,
        max_chunk_secs: float | None,
        speakers_txt_file: str | None,
        vocab_txt_file: str | None,
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
        # ``--force`` re-renders outputs from an existing transcript;
        # ``--force-all`` deletes the cached transcript so the API is
        # called again. Both together would be ambiguous -- pick one.
        if force and force_all:
            raise click.UsageError(
                "--force and --force-all are mutually exclusive. "
                "Use --force to re-render outputs from the cached "
                "transcript, or --force-all to also redo the API call."
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
                output_stem=output_stem,
                gemini_api_keys=parsed_keys,
                language_codes=parsed_language_codes,
                model=model,
                diarized_srt_file=diarized_srt_file,
                srt_file=srt_file,
                txt_file=txt_file,
                transcript_json_file=transcript_json_file,
                metadata_json_file=metadata_json_file,
                force=force,
                force_all=force_all,
                tier=tier,
                line_interval_secs=line_interval_secs,
                paragraph_interval_secs=paragraph_interval_secs,
                txt_width=txt_width,
                request_interval_secs=request_interval_secs,
                max_chunk_secs=max_chunk_secs,
                speakers_txt_file=speakers_txt_file,
                vocab_txt_file=vocab_txt_file,
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
    argv_list = _normalize_help_argv(argv_list)
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


def load_vocab_txt_file(
    path: str | Path | None = "auto",
    input_file: Path | str | None = None,
) -> list[str]:
    """CLI wrapper for :func:`api._load_vocabulary_file`."""
    from .api import _load_vocabulary_file

    return _load_vocabulary_file(path, input_file=input_file)


# Backward compatibility alias
load_custom_vocabulary_file = load_vocab_txt_file


def load_speakers_file(
    path: str | Path | None = "auto",
    input_file: Path | str | None = None,
) -> dict[str, str]:
    """CLI wrapper for :func:`api._load_speakers_file`."""
    from .api import _load_speakers_file

    return _load_speakers_file(path, input_file=input_file)


def parse_speakers(spec: str) -> dict[str, str]:
    """Parse speaker mappings from text content or spec."""
    from .api import parse_speakers as _api_parse_speakers

    return _api_parse_speakers(spec)


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
    if opts.force_all:
        tokens.append("--force-all")
    if opts.log_level != "info":
        tokens.extend(["--log-level", opts.log_level])

    if opts.output_dir is not None:
        tokens.extend(["--output-dir", str(opts.output_dir)])
    if opts.output_stem is not None:
        tokens.extend(["--output-stem", str(opts.output_stem)])
    if opts.temp_path is not None:
        tokens.extend(["--temp-path", str(opts.temp_path)])
    if opts.speakers_txt_file and opts.speakers_txt_file != "auto":
        tokens.extend(["--speakers-txt-file", str(opts.speakers_txt_file)])
    if opts.vocab_txt_file and opts.vocab_txt_file != "auto":
        tokens.extend(["--vocab-txt-file", str(opts.vocab_txt_file)])
    if opts.max_chunk_secs is not None:
        tokens.extend(["--max-chunk-secs", str(opts.max_chunk_secs)])

    if opts.line_interval_secs is not None:
        tokens.extend(["--line-interval-secs", str(opts.line_interval_secs)])
    if opts.paragraph_interval_secs is not None:
        tokens.extend(["--paragraph-interval-secs", str(opts.paragraph_interval_secs)])
    if opts.txt_width != 65:
        tokens.extend(["--txt-width", str(opts.txt_width)])

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
    gtw_logging.silence_noisy_loggers()

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

    def _run_one_pass() -> None:
        """One pass over ``opts.path`` (issue-001's loop driver calls this)."""
        nonlocal quota_exceeded, failed  # type: ignore[misc]
        for pattern in opts.path:
            try:
                batch = gemini_transcribe(
                    input_file=pattern,
                    output_dir=opts.output_dir,
                    output_stem=opts.output_stem,
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
                    force_all=opts.force_all,
                    line_interval_secs=opts.line_interval_secs,
                    paragraph_interval_secs=opts.paragraph_interval_secs,
                    txt_width=opts.txt_width,
                    request_interval_secs=opts.request_interval_secs,
                    max_chunk_secs=opts.max_chunk_secs,
                    speakers_txt_file=opts.speakers_txt_file,
                    temp_path=opts.temp_path,
                    vocab_txt_file=opts.vocab_txt_file,
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

    loop_interrupted = False
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
            # Loop driver returns 130; propagate the exit code below.
            loop_interrupted = True
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
    if loop_interrupted:
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
    argv_list = _normalize_help_argv(argv_list)
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
