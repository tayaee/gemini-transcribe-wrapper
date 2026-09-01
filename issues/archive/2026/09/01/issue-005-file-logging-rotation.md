---
name: issue-005-file-logging-rotation
description: Add RotatingFileHandler to root logger: ~/.cache/gemini-transcribe-wrapper/logs/gemini-transcribe-wrapper.log[.1], 5MB maxBytes, backupCount=2.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-005: file-based logging with rotation (5 MB × 3)

## Why

Spec §4.4. The wrapper currently logs only to the console. After a
multi-hour batch with `--loop*` (issue-001), the user has no durable
record of what happened — a tmux detach, a notebook sleep, or an SSH
disconnect drops the entire log.

The user spec requires:

- Two destinations: file + console
- Default file path: `~/.cache/gemini-transcribe-wrapper/logs/gemini-transcribe-wrapper.log[.1]`
- Retention: **current + 2 past** = 3 files total
- Max **5 MB** per file
- ISO-8601 with timezone offset (already implemented via `_TzFormatter`)
- File: no color codes
- Honors `$GTW_CACHE_DIR` for tests

## What

In `cli.py`'s `_run`, after installing the console `StreamHandler`, add:

```python
log_dir = cache_dir() / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
file_handler = RotatingFileHandler(
    log_dir / "gemini-transcribe-wrapper.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB
    backupCount=2,              # current + 2 past = 3 files total
    encoding="utf-8",
    delay=True,                 # don't open until first record
)
file_handler.setFormatter(
    _TzFormatter("%(asctime)s %(levelname)s %(filename)s:%(lineno)s %(message)s")
)
root.addHandler(file_handler)
```

The file handler never uses ANSI color codes (formatter is plain).

If `cache_dir()` is not writable, we log to stderr but do **not** crash
the wrapper — the console stream still works.

## How to apply

- Add a new dev/internal flag `--no-file-log` (default: false) for
  users who want to opt out (e.g. CI runs that don't want log files
  at all).
- New module-level helper `setup_file_logging(cache_dir)` so tests can
  call it against a `tmp_path` fixture.
- The file handler is registered alongside the console handler; log
  records propagate to both.

## Files to touch

- `src/gemini_transcribe_wrapper/cli.py` — install `RotatingFileHandler`
- `src/gemini_transcribe_wrapper/_logging.py` (new) — small helper for
  shared setup, also used by tests
- `tests/test_file_logging.py` (new)

## Acceptance

- After running with `--loop*` for a few minutes, the log directory
  contains exactly 1–3 `gemini-transcribe-wrapper.log{,.[12]}` files.
- Each file ≤ 5 MB.
- The active log file contains the same records the console shows.
- The file has no ANSI escapes (assert via `re.search(r"\x1b", content)`).
- `cache_dir()` not writable → stderr warning, no crash.
- `--no-file-log` skips the file handler entirely.

## Notes

- `RotatingFileHandler` is in the stdlib (`logging.handlers`) — no new
  dependency.
- We use `delay=True` so the file is not created until the first log
  record; this keeps short-lived CI runs from leaving an empty file
  behind.
- A future iteration could split the log file by PT date for easier
  forensics; out of scope here.

## 구현 결과

- **구현 완료 일시**: 2026-09-01
- **변경 파일**:
  - `src/gemini_transcribe_wrapper/_logging.py` (new) — `setup_file_logging(cache_root)`
    helper: `RotatingFileHandler(maxBytes=5MB, backupCount=2, delay=True)`,
    `encoding="utf-8"`, ISO-8601 + tz offset formatter (`_TzFormatter`),
    unwritable-cache-dir fallback (warning + returns `None`),
    duplicate-handler guard.
  - `src/gemini_transcribe_wrapper/cli.py` — `--no-file-log` Click flag,
    `TranscribeOptions.no_file_log: bool = False`,
    `setup_file_logging()` 호출은 console handler 설정 직후,
    `if not opts.no_file_log` 게이트.
  - `tests/test_file_logging.py` (new) — 12 tests: 디렉토리 생성,
    handler 타입/크기/지연, root 부착, ANSI 없음, ISO 타임스탬프,
    회전(`.1` 생성), 백업 카운트 초과 정리, `--no-file-log` 플래그
    default/enabled, `$GTW_CACHE_DIR` 존중, unwritable fallback.
  - `regression-tests/verify-issue-005.sh` (new) — 11 mechanical checks.
- **계획과의 차이**: 없음. `delay=True` 와 5MB×3 (current + 2 past) 사양 정확히 구현.
- **검증 결과**:
  - `regression-tests/verify-issue-005.sh` → exit 0
  - `uv run ruff check` → clean
  - `uv run pytest` → 294 passed (282 + 12 신규)
  - 모든 기존 `verify-issue-00{1,2,3,4}.sh` → OK (regression 없음)
