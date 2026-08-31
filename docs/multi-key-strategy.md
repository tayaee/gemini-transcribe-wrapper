# Multi-Key Strategy

When the free-tier daily quota (~25 calls / day / key) is the bottleneck, you
can give the wrapper several Gemini API keys and let it spread the load and
recover from 429s automatically.

## TL;DR

```bash
gtw --gemini-api-keys KEY1,KEY2,KEY3 sample.mp4
```

That's it. The wrapper:

1. **Cycles keys in round-robin order** across chunks (advancing the pointer
   after every successful chunk).
2. **On a 429 with retry hint**, sleeps `(hint + 120s)` and retries once on
   the same key. If the retry also 429s, falls through to the next key.
3. **On a 429 without retry hint**, skips the cooldown and tries the next
   key immediately.
4. **When all keys are exhausted**, propagates the most-recent 429. The
   batch boundary still raises `QuotaExceededError` (CLI exit code `2`).

## CLI

| Flag | Status | Behavior |
| --- | --- | --- |
| `--gemini-api-keys=K1,K2,...` | **Preferred** | Comma-separated list. Whitespace and blanks are stripped; duplicates are removed. |
| `--gemini-api-key=K1` | Deprecated | Logs a one-time `--gemini-api-key is deprecated; use --gemini-api-keys` warning, then behaves like `--gemini-api-keys=K1`. |
| `$GEMINI_API_KEYS` (CSV) | Env fallback | Used when no CLI flag is provided. |
| `$GEMINI_API_KEY` (single) | Env fallback | Used as a one-element list when neither CLI nor `$GEMINI_API_KEYS` is set. |
| `$GOOGLE_API_KEY` (single) | Env fallback | Used as a last resort. |

CLI flags always win over environment variables (no silent merging). To force
a specific subset of keys, pass them explicitly on the command line.

## Python API

```python
from gemini_transcribe_wrapper import gemini_transcribe

batch = gemini_transcribe(
    input_file="interview.mp4",
    gemini_api_keys=["KEY1", "KEY2", "KEY3"],
    # ... other options ...
)
```

`gemini_api_key=` (singular) is still accepted as a deprecated kwarg that
appends to the list (with a debug log). Prefer the plural form.

## Round-robin ordering

For a chunk sequence `c0, c1, c2, ...` with keys `[K1, K2, K3]`, the wrapper
issues calls in the order `K1, K2, K3, K1, K2, K3, ...` and advances the
round-robin pointer only after a successful chunk (so a chunk that fails on
K1 and then succeeds on K2 still leaves K2 as the *next* key, not K3).

## Per-call 429 fallback detail

```
for each key in round-robin order:
    install key on client
    try the API call
        on success:
            advance pointer, return result
        on 429 with retry hint:
            sleep (hint + 120s safety)
            retry once with same key
                on success:
                    advance pointer, return result
                on failure:
                    audit-log this key, try next key
        on 429 without retry hint:
            audit-log this key, try next key (no sleep)
        on non-quota error (400/500/...):
            audit-log + propagate immediately (no key rotation)
all keys exhausted:
    raise most-recent 429 → QuotaExceededError at the batch boundary
```

The audit JSONL records **one entry per failed key**, not a single
"all-keys-exhausted" entry, so post-mortem debugging can see exactly which
keys were tried and how each one failed.

## Per-key state isolation

- **Daily quota counter**: tracks `usage-<sha256(key)[:12]>.json` per key.
  A user with three free-tier keys effectively gets 75 calls/day, not 25.
- **Rate-limit throttle timestamp**: stored both in-memory and on disk in
  `last_api_completion-<sha256(key)[:12]>.json`. Marking K1 as recently
  completed does **not** delay a call on K2.
- **Round-robin pointer**: in-memory only; resumes from key 0 after a
  process restart (intentional — disk-sticking the pointer would couple
  unrelated processes).

## When to use multi-key vs single key

- **Single key**: short jobs (<25 chunks), CI jobs, paid tier.
- **Multi-key**: large batches (>25 chunks) on free tier; multi-day jobs
  where rotating keys helps; any scenario where 429s would otherwise
  require manual intervention.

## Caveats

- The wrapper does **not** verify that keys are distinct accounts. Two
  keys pointing at the same backend quota still share a quota and you'll
  burn out faster than expected.
- The wrapper does **not** retry across batch boundaries. Once a batch
  ends (one batch = one input file or one pattern), a 429 on the *last*
  chunk of the last file still aborts cleanly with exit code `2`. Re-run
  the batch after the cooldown to resume (resume is automatic for
  completed chunks).
- The deprecated `--gemini-api-key` flag logs a one-time deprecation
  warning on each invocation. It will be removed in a future major
  release; migrate to `--gemini-api-keys` now.

## Examples

Three free-tier keys, three hours of audio (≈12 chunks):

```bash
export GEMINI_API_KEYS=K1,K2,K3
gtw long_meeting.mp4          # round-robins, falls through on 429s
```

Force exactly two keys for one run (overriding whatever is in the env):

```bash
gtw --gemini-api-keys=$ROTATING_KEY_1,$ROTATING_KEY_2 batch.mp4
```

Pay-tier with a single key (no rotation needed):

```bash
gtw --tier paid --gemini-api-key $PAID_KEY sample.mp4
```
