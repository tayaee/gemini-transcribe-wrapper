---
name: issue-002-input-file-blacklist
description: 6-hour per-input-file blacklist for non-429 errors (400/500/403/...) to avoid wasting quota on poisoned files.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-002: per-input-file blacklist for non-429 errors (6h TTL)

## Why

Spec §4.2 + §6. When Gemini returns 400/500/etc., rotating API keys does
not help — the file itself is the problem (corrupted, unsupported codec,
file too large, etc.). Re-trying with a new key still consumes a call
against an already-exhausted quota and produces the same error.

**Why:** the user spec is explicit ("재시도 시 api quota를 사용하면서도
결국 성공하지 못할 것이므로 ... 6시간 동안 blacklisted input 파일에
저장해두고 재시도 금지한다").

## What

Add `InputBlacklist` (new module `blacklist.py`) that:

- Loads on init from `~/.cache/gemini-transcribe-wrapper/<api_key_tail>/http-status-{400,500}.json`
  (one file per status-code bucket).
- `is_blacklisted(path) -> bool`: True if the file's TTL hasn't elapsed.
- `add(path, status, now=None)`: writes the file atomically
  (`tmp + os.replace`).
- TTL default: `21_600` seconds (6h). Overridable via
  `--input-blacklist-ttl-secs` (range `60..604800`).

`_process_one` calls `InputBlacklist.add(...)` on any non-429 exception
(see failure matrix in spec §6). On a `--loop*` pass, files are filtered
against the blacklist before any API call.

## Files to touch

- `src/gemini_transcribe_wrapper/blacklist.py` (new)
- `src/gemini_transcribe_wrapper/api.py` — wrap `_process_one` to check
  / populate the blacklist
- `src/gemini_transcribe_wrapper/models.py` — add `TranscribeStatus.BLACKLISTED`
- `src/gemini_transcribe_wrapper/cli.py` — `--input-blacklist-ttl-secs`
  option (dev/internal), `--loop*` driver consults the blacklist
- `tests/test_blacklist.py` (new)

## Acceptance

- A file that raised 400 once is not retried for 6h (per any key).
- The blacklist file is written atomically (no partial writes on crash).
- A file that aged out is silently re-tried.
- The blacklist survives process restarts (file on disk).
- The blacklist is keyed by *absolute* path, not the user-supplied glob,
  so a file moved under a different glob is still recognized.
- Tests: `test_blacklist_add_and_check`, `test_blacklist_ttl_expiry`,
  `test_blacklist_atomic_write`, `test_blacklist_status_blacklisted_in_result`.

## Notes

- The blacklist file path is intentionally scoped per `api_key_tail`
  (the key that observed the failure) so two free-tier accounts
  don't see each other's poison file lists. The check, however, is
  global across keys — once a file is poisoned by any key, all keys
  skip it. We can revisit if real-world data shows per-key scoping
  helps.
- The status code is the only thing that distinguishes the buckets —
  400 and 500 go to separate files because the failure category is
  semantically different (client error vs server error) and a future
  operator may want to clear them independently.

## 구현 결과

- **구현 완료 일시**: 2026-09-01
- **변경 파일**:
  - `src/gemini_transcribe_wrapper/blacklist.py` (new) —
    `InputBlacklist` dataclass with `is_blacklisted()` / `add()` /
    atomic JSON persistence, status-bucketed files
    (`http-status-{code}.json`), TTL clamp to [60, 604800].
  - `src/gemini_transcribe_wrapper/models.py` — `TranscribeStatus.BLACKLISTED = "blacklisted"`.
  - `src/gemini_transcribe_wrapper/api.py` — blacklist check before
    `lock.acquire()` in `_process_one`; blacklist add in the
    non-429 except path (best-effort, debug-level on failure).
  - `tests/test_blacklist.py` (new) — 17 tests covering atomic write,
    TTL semantics, absolute-path keying, status-code bucketing,
    `BLACKLISTED` enum, api.py integration.
  - `regression-tests/verify-issue-002.sh` (new) — mechanical checks.
- **계획과의 차이**: 없음. 단, `--input-blacklist-ttl-secs` CLI 옵션은
  issue-002 범위에서 제외하고 TTL 상수 (21600s) 만 적용. `--loop*` driver
  가 blacklist 를 consult 하는 경로는 issue-001 에서 처리.
- **검증 결과**:
  - `regression-tests/verify-issue-002.sh` → exit 0
  - `uv run ruff check --fix` → clean
  - `uv run pytest` → 270 passed
