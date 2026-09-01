---
name: issue-001-loop-flags
description: Add --loop-until-no-input and --loop-always CLI flags for continuous glob processing.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-001: `--loop-until-no-input` / `--loop-always` CLI flags

## Why

Spec §3.1 (see `docs/spec/spec-2-free-tier-quota-hardening.md`). Free-tier
quota caps at ~25 calls/key/day; long-running drop-folder workflows want
to keep a `gtw` process alive across many files. The current CLI does one
pass over `opts.path` and exits.

**Why:** users hit the daily cap mid-day and have no way to retry without
manually re-invoking the wrapper.

## What

Add two mutually exclusive flags:

- `--loop-until-no-input`: glob → process all → re-glob → if empty, exit.
  If non-empty, process the new arrivals and repeat.
- `--loop-always`: same as above, but never exit on an empty pass — sleep
  `--loop-poll-secs` (default `30`, range `1..3600`) and re-glob.

Both flags wrap the existing `for pattern in opts.path:` block in
`_run`; they do not touch the inner chunk loop.

## How to apply

- Pass both flags → `ValueError` ("--loop-until-no-input and --loop-always
  are mutually exclusive") + exit code 2.
- Pass neither → behavior identical to today.
- A `QuotaExceededError` raised under `--loop*` does **not** exit; the
  loop catches it, sleeps the configured poll interval, and tries the
  next pass.
- A `KeyboardInterrupt` (Ctrl-C) cleanly exits the loop (no surprise
  exit codes).

## Files to touch

- `src/gemini_transcribe_wrapper/cli.py` — new Click options
- `src/gemini_transcribe_wrapper/_loop.py` (new) — poll/re-glob driver
- `src/gemini_transcribe_wrapper/models.py` — no new status enum needed
- `tests/test_loop.py` (new)

## Acceptance

- `gtw ... --loop-until-no-input '*.mp4'` processes existing matches,
  re-globs, exits when the glob is empty.
- `gtw ... --loop-always '*.mp4'` keeps running with no matches present.
- Adding a file to the directory mid-run is picked up within
  `--loop-poll-secs`.
- Passing both loop flags → exit code 2, error printed.
- All existing tests still pass.
- New tests: `test_loop_until_no_input_exits_when_empty`,
  `test_loop_always_does_not_exit_when_empty`,
  `test_loop_flags_are_mutually_exclusive`.

## Notes

- `--loop-poll-secs` is a new knob; default `30`. Documented in spec §3.2.
- The loop's exit code on Ctrl-C is `130` (POSIX convention).

## 구현 결과

- **구현 완료 일시**: 2026-09-01
- **변경 파일**:
  - `src/gemini_transcribe_wrapper/_loop.py` (new) — `run_with_loop`
    driver with KeyboardInterrupt → 130, QuotaExceededError → sleep+retry,
    `_glob_matches()` helper, `_clamp_poll_secs()`.
  - `src/gemini_transcribe_wrapper/cli.py` — 3 new Click options
    (`--loop-until-no-input`, `--loop-always`, `--loop-poll-secs`
    IntRange [1, 3600]), mutual exclusion check, `_run_one_pass()`
    wrapper extracted from main loop, loop-driver invocation gated
    on the flags, sentinel `_loop_interrupted` → exit 130.
  - `tests/test_loop.py` (new) — 10 tests covering mutual exclusion,
    driver behavior for both flags, KeyboardInterrupt, QuotaExceededError
    retry, poll_secs clamping.
  - `regression-tests/verify-issue-001.sh` (new) — mechanical checks.
- **계획과의 차이**: 없음. `_run_one_pass()` 래퍼 추출은 기존 메인 루프
  본문을 재사용하기 위한 내부 리팩토링.
- **검증 결과**:
  - `regression-tests/verify-issue-001.sh` → exit 0
  - `uv run ruff check --fix` → clean
  - `uv run pytest` → 280 passed
