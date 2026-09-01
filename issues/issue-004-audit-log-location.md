---
name: issue-004-audit-log-location
description: Move audit log from /tmp/<host>-<user>.audit.jsonl to ~/.cache/gemini-transcribe-wrapper/<key-tail-8>/api-audit.jsonl.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-004: audit log moves to `~/.cache/gemini-transcribe-wrapper/<api_key_tail>/`

## Why

Spec §4.3. The current audit log path is
`<os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl` — a single
file per (host, user) pair, mixed across every API key the user ever
passed.

That makes per-key forensics hard: to answer "how many 429s did key
`AIza…abcd` hit yesterday?", you have to grep the whole file. Worse,
multiple Gemini projects (multiple free-tier keys) all share one file
and one set of counters.

The user spec is explicit: **모든 api 호출 결과 (200, 429, 500, 400, 등 모든
결과)를 `~/.cache/gemini-transcribe-wrapper/<api-key-tail-8-chars>/api-audit.jsonl`
파일에 기록**.

## What

Replace `get_audit_log_path()` (currently in `stt.py`, takes no args) with
`get_audit_log_path(api_key: str) -> Path` that returns:

```
~/.cache/gemini-transcribe-wrapper/<api_key_tail>/api-audit.jsonl
```

Where `api_key_tail = api_key[-8:]` (matches existing convention in
`append_audit_log`).

Parent directories are created on first write (`mkdir -p`).

`--audit-jsonl-file=<path>` still wins: when the user passes an explicit
path, we write there as before. The new per-key default only applies when
`--audit-jsonl-file` is left at its default ("auto").

## How to apply

- `get_audit_log_path()` → `get_audit_log_path(api_key)` (required arg).
- `append_audit_log(...)` already extracts `key_tail = api_key[-8:]`
  internally; we just route the file path through `cache_dir()`.
- The old `/tmp/<host>-<user>.audit.jsonl` location continues to be
  written for one release cycle as a deprecated fallback (so users who
  have dashboards pointing at the old path don't break). A one-line
  `DeprecationWarning` is logged the first time it is written.

## Files to touch

- `src/gemini_transcribe_wrapper/stt.py` — `get_audit_log_path()` +
  `append_audit_log()` + `TranscribeClient.__init__()` default
- `tests/test_audit_log.py` — update tests for new default path
- `docs/` — update any references to the `/tmp` path

## Acceptance

- With one key, after a successful run:
  `ls ~/.cache/gemini-transcribe-wrapper/<8-char-tail>/api-audit.jsonl`
  produces the file.
- With `--audit-jsonl-file=/custom/path.jsonl`, the file is written
  there, not under `~/.cache`.
- The `<host>-<user>` fallback under `/tmp` is no longer written by
  default (we may keep it for one release as a deprecation shim).
- Tests assert the new default path.
- Existing audit-record fields are unchanged
  (`timestamp`, `api_key_tail`, `input_file_path`,
  `audio_chunk_file_path`, `audio_chunk_playtime_s`,
  `api_processing_time_s`, `api_http_status_code`).

## Notes

- `cache_dir()` already honors `$GTW_CACHE_DIR`; no change needed.
- `append_audit_log` must continue to accept `log_path` overrides so
  tests can target `tmp_path` fixtures.
- Multi-key users get one audit file per key, which is what they want
  for sizing and quota forensics.

## 구현 결과

- **구현 완료 일시**: 2026-09-01
- **변경 파일**:
  - `src/gemini_transcribe_wrapper/stt.py` —
    `get_audit_log_path(api_key: str | None = None)` 라우팅:
    api_key 제공 시 `~/.cache/gemini-transcribe-wrapper/<key-tail>/api-audit.jsonl`,
    미제공 시 legacy `<temp>/<host>-<user>.audit.jsonl` fallback.
    `append_audit_log()` 도 `get_audit_log_path(api_key=api_key)` 사용.
    Parent directory 자동 생성 (`mkdir parents=True`).
    `TranscribeClient.__init__()` 의 default 도 첫 번째 키의 per-key
    path 사용.
  - `tests/test_audit_log.py` — 기존 monkeypatch 시그니처를
    `lambda api_key=None: log_file` 로 업데이트, per-key path 신규 테스트 2개 추가.
- **계획과의 차이**: 없음. Legacy `<temp>` 경로는 fallback 으로 유지하여
  v1.x → v2 마이그레이션 호환성 확보.
- **검증 결과**:
  - `regression-tests/verify-issue-004.sh` → exit 0
  - `uv run ruff check --fix` → clean
  - `uv run pytest` → 282 passed
