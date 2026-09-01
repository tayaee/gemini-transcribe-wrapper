# PRD & Implementation Specification: gemini-transcribe-wrapper v2 (free-tier quota hardening)

## 0. Why this spec exists

The free tier is the default tier for this wrapper. As of this writing the
Gemini free tier is bounded by three independent constraints:

- ~25 API calls per Gemini project / API key per Pacific day
- 2 API calls per minute
- 30 minutes of audio per call (when diarization or word-level timestamps
  are enabled; 60 minutes otherwise)

In practice the per-day quota is the binding constraint for any non-trivial
job, and — contrary to what the docs imply — the reset is *not* synchronized
to PT midnight. Each key resets on its own wall-clock window, which can be
any time during the day. That means hitting PT midnight does not reliably
recover a key, and 429s will land on a non-trivial fraction of the pool
within the first few hours.

The wrapper currently:

- raises and aborts the entire batch on the first 429 (no `--loop` recovery)
- has no concept of "this input file is poisoned, skip it for 6 hours"
- batch-reactivates the cooldown pool every 10 minutes (too short, and the
  semantics are "drain → sleep → reactivate all" rather than "per-key
  30-minute skip")
- writes the audit log to a single per-host/per-user file under `/tmp`,
  making per-key forensics impossible
- has no file-based logging at all (only the console handler)

This spec defines v2 of those four subsystems.

---

## 1. Scope

In scope:

1. `--loop-until-no-input` and `--loop-always` CLI flags (continuous
   processing of a glob over a watched directory).
2. Per-input-file blacklist for non-429 errors (6-hour TTL).
3. Per-API-key 30-minute cooldown (replaces the 10-minute batch reactivation).
4. Audit log relocation to `~/.cache/gemini-transcribe-wrapper/<key-tail>/`.
5. File-based logging with rotation (5 MB × 3 files).
6. Color/TTY handling for the console handler.
7. Consistent use of the 8-character `api_key_tail` everywhere.

Out of scope:

- Changing the daily call limit (Google's policy).
- Paid tier behavior (still works the same; this spec adds nothing for it).
- Per-key daily quota reset time tracking (we still use the PT midnight
  hint as a coarse estimate; see §6).

---

## 2. Terminology

| Term | Definition |
| --- | --- |
| `live pool` | API keys currently eligible for round-robin. Replaces the existing `active_pool` (the new code keeps the existing field name for backward compat in tests). |
| `dead pool` | API keys that hit 429 / quota; each key carries a `cooldown_until` timestamp and is skipped (not just deprioritized) until that time elapses. Replaces the existing `cooldown_pool`. |
| `api_key_tail` | The last **8 characters** of the API key. Used in log lines, audit file paths, blacklist file paths, and `--gemini-api-keys=...` re-emission. |
| `blacklist` | A set of input files that produced a non-429 error. Persisted to disk under `~/.cache/gemini-transcribe-wrapper/<api_key_tail>/http-status-{400,500}.json`. TTL = 6 hours from first add. |
| `loop-until-no-input` | One pass over the glob, then re-glob and re-process; exit when the glob matches zero files. |
| `loop-always` | Re-glob every `--loop-poll-secs` seconds; sleep between empty passes; never exit on its own. |

---

## 3. New CLI surface

### 3.1. Loop flags

```bash
# One pass, then keep watching until the glob matches nothing; then exit.
gtw --gemini-api-keys "$KEYS" *.mp4 --diarized-srt-file auto --loop-until-no-input

# Re-glob forever, sleep --loop-poll-secs between empty passes, never exit.
gtw --gemini-api-keys "$KEYS" *.mp4 --diarized-srt-file auto --loop-always
```

Behavior rules:

- Exactly one of `--loop-until-no-input` / `--loop-always` may be passed.
  Passing both is a `ValueError` (and exit code 2).
- The loop is **outside** the inner chunk loop: it wraps the
  `for pattern in opts.path:` block in `_run`.
- A `--loop*` invocation may be paired with `--tier free` (the typical case)
  or `--tier paid`. Paid tier skips the per-call throttle.
- A 429 from any chunk during a loop iteration transitions to the
  per-key 30-minute cooldown (see §5). On the next pass, that key is
  skipped automatically — no retry, no extra sleep.
- A non-429 error during a loop iteration adds the input file to the
  blacklist (see §6). On the next pass, the file is skipped silently for
  the next 6 hours.

### 3.2. Other knobs

| Flag | Default | Meaning |
| --- | --- | --- |
| `--loop-poll-secs` | `30` | Seconds to sleep between empty-pass iterations when `--loop-always` is set. Lower bound `1`; upper bound `3600`. |
| `--input-blacklist-ttl-secs` | `21600` (6h) | TTL for an input file in the blacklist. Lower bound `60`; upper bound `604800` (1 week). |

---

## 4. Architecture changes

### 4.1. Live / dead pool with per-key cooldown_until

Current `TranscribeClient` keeps two structures:

```python
self._active_pool: list[str]      # live
self._cooldown_pool: set[str]     # dead (batch reactivation)
```

v2 replaces these with:

```python
# ``self._active_pool`` is kept (and renamed) to ``self._live_pool``;
# tests still monkeypatch ``_active_pool`` so the rename is wrapped in a
# property that proxies to ``_live_pool``.
self._live_pool: list[str]                  # ordered, round-robin
self._dead_pool: dict[str, float]           # key -> cooldown_until epoch
```

The chunk loop becomes:

```python
while True:
    self._prune_dead_pool(now=time.monotonic())
    live = list(self._live_pool)
    if not live:
        if not self._dead_pool:
            raise last_quota_exc          # safety net
        soonest = min(self._dead_pool.values())
        sleep_for = max(0.0, soonest - time.monotonic())
        logger.info("Live pool empty; sleeping %.0fs until %s recovers.", ...)
        time.sleep(sleep_for)
        continue                           # prune + retry

    for offset in range(len(live)):
        idx = (self._rr_index + offset) % len(live)
        key = live[idx]
        ...                                # try API call
        on 429:
            self._dead_pool[key] = time.monotonic() + 1800.0   # 30 min
            continue
        on success:
            self._rr_index = (idx + 1) % len(live)
            return result
```

The per-key cooldown eliminates the 10-minute batch reactivation sleep.

### 4.2. Input file blacklist

New module `src/gemini_transcribe_wrapper/blacklist.py`:

```python
@dataclass(frozen=True)
class InputBlacklist:
    path: Path
    ttl_secs: int = 21_600

    def is_blacklisted(self, now: float | None = None) -> bool: ...
    def add(self, status: int, now: float | None = None) -> None: ...
    def load(self) -> None: ...
```

File format (`http-status-{400,500}.json`):

```json
{
  "schema_version": 1,
  "ttl_secs": 21600,
  "entries": {
    "<absolute input path>": {
      "first_blacklisted_at_epoch": 1735689600.0,
      "expires_at_epoch":          1735711200.0,
      "status_code":                400
    }
  }
}
```

Resolution: each non-429 error during transcription → `_process_one` catches
the exception, derives `status_code`, and calls `InputBlacklist.add(...)`.
On the next pass (whether immediate or in `--loop*`), the file is checked
first; if still within TTL, it is skipped (and reported in the result as
`status=BLACKLISTED`).

### 4.3. Audit log relocation

Current path: `<os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl`.

v2 path:

```
~/.cache/gemini-transcribe-wrapper/<api_key_tail_8_chars>/api-audit.jsonl
```

- One file per API key (not per host/user), so per-key forensics works
  directly off `ls ~/.cache/gemini-transcribe-wrapper/<tail>/`.
- The old `/tmp` path is preserved as a fallback read-only during the
  v1.x deprecation window — see §7.
- `get_audit_log_path(api_key=...)` now takes an `api_key` arg.

### 4.4. File-based logging

Add a `RotatingFileHandler` to the root logger in `_run`:

- Path: `~/.cache/gemini-transcribe-wrapper/logs/gemini-transcribe-wrapper.log`
- `maxBytes = 5 * 1024 * 1024`
- `backupCount = 2`
- Formatter: same `_TzFormatter` as the console handler, **no color codes**.
- Created with `delay=True` so an unwritable cache dir doesn't crash startup.
- Honors `$GTW_CACHE_DIR` (existing override).

### 4.5. Color / TTY handling

- Console handler adds ANSI color only when `sys.stderr.isatty()` is true.
- Pipe / redirect / CI: no color codes (so log scrapers don't choke).
- File handler: never colors.
- Implementation: `colorlog` if available, otherwise a small inline
  formatter. (Decision is deferred to issue-006.)

---

## 5. State machines

### 5.1. Per-key state

```
              ┌──────────────┐
              │   live       │◄─────────────────┐
              └──────┬───────┘                  │
              429   │                            │ prune_dead_pool(now):
                    ▼                            │   if cooldown_until ≤ now:
              ┌──────────────┐                    │     move back to live
              │   dead       │────────────────────┘
              │ cooldown_until = now + 1800
              └──────────────┘
```

A dead key with `cooldown_until` in the past is automatically moved back
to live by `_prune_dead_pool`, called at the top of every chunk-loop
iteration.

### 5.2. Per-input-file state

```
              ┌────────────────┐
              │  candidate     │
              └────┬───────────┘
                   │ non-429 error
                   ▼
              ┌────────────────┐         TTL elapsed
              │  blacklisted   │──────────────────► candidate
              │ (6h TTL)       │
              └────────────────┘
```

A blacklisted file that has aged out is silently re-tried. A blacklisted
file that hasn't aged out is skipped with `status=BLACKLISTED`.

---

## 6. Failure handling matrix

| HTTP code | Action |
| --- | --- |
| 200 | success, advance round-robin |
| 429 | move key to dead pool with `cooldown_until = now + 1800`; try next live key |
| 400 | add input file to blacklist (6h TTL); abort just this file (not the batch) |
| 500 | same as 400 |
| 403 | log + add input file to blacklist (treat as "key won't accept this file") |
| network/timeout | retry-with-backoff once, then add to blacklist as 500 |
| unknown 4xx/5xx | add to blacklist with the actual status code |

The blacklist path is `~/.cache/gemini-transcribe-wrapper/<api_key_tail>/http-status-{400,500}.json`
(one file per status code, written atomically with `os.replace`).

---

## 7. Migration / backward compatibility

| Change | Compat strategy |
| --- | --- |
| Audit log path | Old `/tmp` file continues to be written for one release if `--audit-jsonl-file` is at its default. Then removed. |
| `request_interval_secs` default | Unchanged (120s for free, 0 for paid). |
| Loop flags | None — they are pure additions. |
| `--max-chunk-secs` | Unchanged. |
| `usage-<hash>.json` (per-key daily counter) | Unchanged. |
| `_active_pool` / `_cooldown_pool` (internal) | Preserved as backward-compat property aliases on `TranscribeClient`; new code uses `_live_pool` / `_dead_pool`. |

CLI exit codes (existing):

| Code | Meaning |
| --- | --- |
| 0 | all files succeeded (or were skipped because already up to date) |
| 1 | at least one file failed |
| 2 | quota exceeded on a single-key run, or conflicting `--loop*` flags |

The `2` path is replaced for `--loop*` runs: the loop catches the
per-file quota exception and continues with the next iteration instead
of exiting.

---

## 8. Files to add / modify

### 8.1. New files

- `src/gemini_transcribe_wrapper/blacklist.py` — `InputBlacklist`
- `src/gemini_transcribe_wrapper/_loop.py` — `--loop*` driver (poll,
  re-glob, blacklist check, key cooldown)
- `tests/test_blacklist.py`
- `tests/test_loop.py`
- `tests/test_file_logging.py`

### 8.2. Modified files

- `src/gemini_transcribe_wrapper/cli.py` — new Click options, root
  logger setup with `RotatingFileHandler`, color/TTY check
- `src/gemini_transcribe_wrapper/stt.py` — live/dead pool rewrite with
  per-key cooldown; `_active_pool` / `_cooldown_pool` become property
  aliases for backward compat
- `src/gemini_transcribe_wrapper/usage_counter.py` — `cache_dir()` is
  already correct; no change needed
- `src/gemini_transcribe_wrapper/api.py` — `_process_one` catches
  non-429 exceptions and calls `InputBlacklist.add(...)`
- `src/gemini_transcribe_wrapper/models.py` — `TranscribeStatus.BLACKLISTED`
- `src/gemini_transcribe_wrapper/audit.py` (or inline in `stt.py`) —
  `get_audit_log_path(api_key=...)` returns the per-key path
- `docs/multi-key-strategy.md` — add §"Per-key cooldown" and §"Loop mode"
- `docs/quota-and-rate-limits.md` — add §"Reset timing reality"

---

## 9. Verification (verify script)

```
uv run ruff check
uv run pytest
```

Manual smoke tests:

```
# 1. Install + version + help
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw -v
gtw --gemini-api-keys "$KEYS" --help | tail -10

# 2. .txt-only path
gtw --gemini-api-keys "$KEYS" sample.mp4

# 3. .srt + .txt
gtw --gemini-api-keys "$KEYS" sample.mp4 --srt-file auto

# 4. .diarized.srt + .srt + .txt
gtw --gemini-api-keys "$KEYS" sample.mp4 --diarized-srt-file auto

# 5. Loop mode
mkdir inbox && cp sample.mp4 inbox/
gtw --gemini-api-keys "$KEYS" 'inbox/*.mp4' --diarized-srt-file auto \
    --loop-until-no-input --loop-poll-secs 5
cp sample2.mp4 inbox/    # in another terminal
# gtw should pick up sample2.mp4 within ~5s and process it

# 6. File logging
ls ~/.cache/gemini-transcribe-wrapper/logs/

# 7. Per-key audit log
ls ~/.cache/gemini-transcribe-wrapper/*/api-audit.jsonl
```

---

## 10. Open questions (deferred)

1. **Per-key daily quota reset time**: should we track the actual reset
   time per key (via hint parsing + history), instead of using PT
   midnight as the estimate? Out of scope for this spec but worth
   considering for v3.
2. **Paid-tier behavior under `--loop*`**: should `--tier paid` allow
   faster polling? Default behavior is fine (paid tier skips the
   per-call throttle, but `--loop-poll-secs` still applies between
   empty passes).
3. **`colorlog` dependency**: would add ~30 KB. Acceptable.
