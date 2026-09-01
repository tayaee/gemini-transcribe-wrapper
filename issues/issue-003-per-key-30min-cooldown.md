---
name: issue-003-per-key-30min-cooldown
description: Replace 10-minute batch reactivation with per-API-key 30-minute cooldown. Single-key mode skips files (does not abort) until the key recovers.
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-003: per-key 30-minute cooldown (replaces batch reactivation)

## Why

Spec §4.1 + §5.1. Current behavior: when the live pool drains, the
wrapper sleeps `_COOLDOWN_SECS = 600s` and reactivates the entire
cooldown pool in one batch. The user spec requires per-key minimum
**30-minute cooldown** and a different semantics:

- The cooldown applies **per key**, not as a batch sleep.
- A dead key recovers **independently** — others stay dead.
- In single-key mode, after a 429 the wrapper should *skip* the rest
  of the pass (or wait) rather than abort the entire batch.

**Why:** the 10-minute batch reactivation (a) is too short for the actual
daily-quota reset which happens later in the day, and (b) treats all
dead keys as a single unit, which is wrong because real-world reset
timing is per-key and distributed throughout the day.

## What

Replace `_active_pool` / `_cooldown_pool` with:

- `_live_pool: list[str]` (round-robin target)
- `_dead_pool: dict[str, float]` (key → `cooldown_until` epoch)

On a 429: `self._dead_pool[key] = time.monotonic() + 1800.0`.

The chunk loop calls `self._prune_dead_pool(now)` at the top of every
iteration: any dead key whose `cooldown_until <= now` is moved back to
live.

When `_live_pool` is empty but `_dead_pool` isn't, sleep
`min(cooldown_until) - now` seconds (not the full 1800) and retry.

In single-key mode, after a 429 the wrapper emits
`status=SKIPPED_QUOTA` for the rest of the current pass; the next
`--loop*` iteration will retry when the cooldown expires.

## How to apply

- `_COOLDOWN_SECS = 600` → `KEY_COOLDOWN_SECS = 1800`
- New method `_prune_dead_pool(now)` on `TranscribeClient`
- New `TranscribeStatus.SKIPPED_QUOTA` for single-key skip behavior
- Old `_active_pool` / `_cooldown_pool` are kept as property aliases
  that read from `_live_pool` / `_dead_pool` for backward compat with
  any external tests / monkeypatchers
- New helper `_cooldown_for_key(key) -> float | None` for the CLI loop
  driver to consult

## Files to touch

- `src/gemini_transcribe_wrapper/stt.py` — pool rewrite + prune helper
- `src/gemini_transcribe_wrapper/models.py` — `SKIPPED_QUOTA` status
- `src/gemini_transcribe_wrapper/api.py` — handle single-key skip path
- `tests/test_active_cooldown_pool.py` — update / extend for new fields
- `docs/multi-key-strategy.md` — update the "Active/Cooldown pool flow"
  diagram and the "Migration from older behavior" note

## Acceptance

- A dead key with `cooldown_until` in the past auto-reactivates on the
  next chunk iteration without any explicit sleep.
- When all live keys are dead, the wrapper sleeps only until the
  *soonest* key recovers (not 30 minutes).
- Single-key mode + 429 → rest of the batch pass is skipped with
  `SKIPPED_QUOTA`; the wrapper does not exit.
- Multi-key mode behavior is unchanged from a user-visible standpoint
  except for the longer per-key cooldown.
- `KEY_COOLDOWN_SECS = 1800` is asserted in a test.

## Notes

- `monkeypatch.setattr(stt, "_COOLDOWN_SECS", ...)` in the existing
  test suite must continue to work. We achieve that by keeping the
  module-level constant but renaming it to `KEY_COOLDOWN_SECS` and
  updating test imports in the same patch.
- The single-key skip path returns the same exit code as a normal
  run (`1` if any file was skipped due to quota); the loop continues
  via `--loop*` (issue-001).
