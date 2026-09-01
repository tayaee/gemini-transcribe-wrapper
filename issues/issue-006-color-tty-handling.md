---
name: issue-006-color-tty-handling
description: Add ANSI color to console handler when stderr is a TTY; disable on pipe/redirect; never color the file handler.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-006: color the console only when stderr is a TTY

## Why

Spec §4.5. The current `_TzFormatter` produces plain text. The user spec
is explicit:

> 파일에서의 coloring은 금지. 콘솔에서는 tty에 대해서는 칼라링을 켜고,
> redirection or pipe에 대해서는 칼라를 끈다.

In practice the user wants `gtw … 2>err.log` to produce a clean
parseable log file (no `\x1b[31mERROR\x1b[0m` debris), and `gtw …` in
an interactive terminal to highlight `ERROR` / `WARNING` lines for fast
scanning.

## What

Add color to the console `StreamHandler` only when
`sys.stderr.isatty()` returns `True`. Pipe/redirect/CI → no color codes.

We use the stdlib `logging` `Formatter` with a thin color-detection
wrapper. Implementation options:

1. **`colorlog` package** (~30 KB): already a well-maintained, MIT-licensed
   helper. Adds a runtime dep.
2. **Inline `_ColorFormatter`**: ~30 lines, no new dep. Maps levelnames
   to ANSI codes via a constant table.

**Recommendation: option 2** — the wrapper has zero runtime deps besides
`google-genai` and `static-ffmpeg`, and colorizing is a 30-line
problem. Keep the dep tree small.

```python
class _ColorFormatter(_TzFormatter):
    LEVEL_COLORS = {
        "DEBUG":    "\x1b[90m",   # bright black / gray
        "INFO":     "\x1b[37m",   # white
        "WARNING":  "\x1b[33m",   # yellow
        "ERROR":    "\x1b[31m",   # red
        "CRITICAL": "\x1b[35;1m", # bold magenta
    }
    RESET = "\x1b[0m"

    def __init__(self, *, use_color: bool, **kw) -> None:
        super().__init__(**kw)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not self._use_color:
            return msg
        color = self.LEVEL_COLORS.get(record.levelname, "")
        if not color:
            return msg
        return f"{color}{msg}{self.RESET}"
```

The console handler picks `_ColorFormatter(use_color=sys.stderr.isatty())`;
the file handler (issue-005) uses plain `_TzFormatter`.

A `--color=auto|always|never` flag lets the user override the auto-detection
(`always` for `2>&1 | cat`, `never` for unusual TTYs that lie).

## How to apply

- Refactor `_TzFormatter` so the format string and timezone helpers live
  in a base class; `_ColorFormatter` extends it.
- Wire `--color` (default `auto`) through `cli.py` to both handlers.
- When `--color=always` and stderr is not a TTY, we still emit ANSI
  codes (caller's responsibility to handle downstream).
- When `--color=never`, both handlers are plain (even if stderr is TTY).

## Files to touch

- `src/gemini_transcribe_wrapper/cli.py` — replace `_TzFormatter` with
  `_ColorFormatter` (or thin wrapper)
- `src/gemini_transcribe_wrapper/_logging.py` (new) — shared formatters
- `tests/test_file_logging.py` — assert file has no `\x1b` codes

## Acceptance

- `gtw --help 2>&1 | cat | grep -c $'\x1b'` → `0` (no color).
- Running `gtw` in a normal terminal shows colored `ERROR` / `WARNING`
  records.
- `gtw --color=never --gemini-api-keys "$K" sample.mp4` produces a
  plain-text log even in an interactive terminal.
- File log never contains `\x1b` codes (asserted in tests).

## Notes

- We deliberately do **not** color the timestamp or filename fields —
  only the levelname. This keeps log lines greppable across the three
  contexts (interactive, piped, file) and avoids subtle reordering when
  colors are stripped.
- On Windows the default terminal does not interpret ANSI unless the
  user enables it; we still emit the codes and let the terminal sort it
  out — the `--color=never` knob exists for users who don't want this.
