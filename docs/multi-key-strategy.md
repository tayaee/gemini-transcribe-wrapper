# Multi-Key Strategy

When the free-tier daily quota (~25 calls / day / key) is the bottleneck, you
can give the wrapper several Gemini API keys and let it spread the load and
recover from 429s automatically.

## TL;DR

```bash
gtw --gemini-api-keys KEY1;KEY2;KEY3 sample.mp4
```

That's it. The wrapper:

1. **Starts with all keys in the active pool.** Round-robin walks the
   `_active_pool`, advancing the pointer after every successful chunk.
2. **On a 429, immediately blacklists the key.** No same-key retry, no
   hint parsing, no cooldown sleep. The key moves into the
   `_cooldown_pool` and the next active key is tried. The 2-minute
   `request_interval_secs` throttle already absorbs short-term rate
   limits, so any 429 encountered in the chunk loop is treated as
   daily-quota exhaustion.
3. **When the active pool drains**, sleeps `_COOLDOWN_SECS` (10 minutes
   by default) and reactivates **every** cooldown key in batch,
   preserving the original `_api_keys` order. Then retries the same
   chunk from the start with the freshly reactivated pool.
4. **Non-quota errors (400/500/...) are not absorbed** by pool rotation
   and propagate immediately so callers see the real failure.

## CLI

| Flag | Status | Behavior |
| --- | --- | --- |
| `--gemini-api-keys=K1;K2;...` | **Preferred** | Semicolon-separated list. Whitespace and blanks are stripped; duplicates are removed. |
| `--gemini-api-keys-file=PATH` | **Preferred** | File with one key per line (blank lines and `#` comments ignored). Keys are appended *after* any `--gemini-api-keys` entries, in file order. Default `auto` picks up `./gemini-api-keys.txt` when present; `off` disables it. |
| `--gemini-api-key=K1` | Deprecated | Logs a one-time `--gemini-api-key is deprecated; use --gemini-api-keys` warning, then behaves like `--gemini-api-keys=K1`. |
| `$GEMINI_API_KEYS` (semicolon-separated) | Env fallback | Used when no CLI flag is provided. |
| `$GEMINI_API_KEY` (single) | Env fallback | Used as a one-element list when neither CLI nor `$GEMINI_API_KEYS` is set. |
| `$GOOGLE_API_KEY` (single) | Env fallback | Used as a last resort. |

CLI flags always win over environment variables (no silent merging). To force
a specific subset of keys, pass them explicitly on the command line.

### Key file (`--gemini-api-keys-file`)

Because the file holds secrets, on Linux/macOS it must be `chmod 600`.
Anything looser aborts the run with the exact command to fix it:

```
API key file /path/to/gemini-api-keys.txt has unsafe permissions 0644 (expected 0600).
Fix it with:
    chmod 600 /path/to/gemini-api-keys.txt
```

Windows has no equivalent permission bit, so the check is skipped there.

The file is **watched while the run is in progress**. Before each key pick the
wrapper compares the file's mtime/size/content hash against what it loaded; on
any change it re-reads *all* keys and rebuilds the live/cooldown pools from the
new file order. Rotation then resumes at the key **following the last used one
in the new file order** — so adding keys to the bottom of the file, or removing
an exhausted key, takes effect without restarting a long batch. If the last used
key is no longer in the file, rotation restarts from the top. An unreadable or
empty file is logged as a warning and the previously loaded keys are kept.

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
issues calls in the order `K1, K2, K3, K1, K2, K3, ...` **while the active
pool is full**. After a chunk that failed on K1 (429 → cooldown) and then
succeeded on K2, K2 is the *next* key, not K3.

If K3 also 429s during chunk 2, the active pool drains, the wrapper sleeps
`_COOLDOWN_SECS`, reactivates every cooldown key (back to `[K1, K2, K3]`),
and retries chunk 2 — typically K1 (or whichever key recovered first)
succeeds on the second pass.

## Active / Cooldown pool flow

```
on each chunk:
    loop:
        active = copy of _active_pool
        if active is empty:
            if _cooldown_pool is empty:
                # safety net (shouldn't happen — __init__ guarantees ≥1 key)
                raise most recent quota error
            log "Active pool drained (N keys in cooldown). Sleeping 600s..."
            sleep _COOLDOWN_SECS
            _active_pool = reorder(_api_keys, filter=in _cooldown_pool)
            _cooldown_pool = set()
            _rr_index = 0
            continue    # retry the same chunk with the fresh pool

        for each key in active (round-robin from _rr_index):
            install key on client
            try the API call
                on success:
                    blacklisted = earlier 429s in this iteration
                    apply blacklisting to _active_pool / _cooldown_pool
                    advance _rr_index past this key
                    return result
                on 429:
                    audit-log this key
                    log "Key ...abcd hit 429 (daily quota); blacklisting..."
                    append to blacklisted_this_loop (NOT removed yet)
                    continue
                on non-quota error (400/500/...):
                    audit-log + propagate immediately (no key rotation)

        # inner loop exhausted without success:
        apply blacklisted_this_loop to _active_pool / _cooldown_pool
        # → next iteration of outer loop sees empty active pool
        #   → triggers the cooldown wait + reactivation path
```

The audit JSONL records **one entry per failed key**, not a single
"all-keys-exhausted" entry, so post-mortem debugging can see exactly which
keys were tried and how each one failed.

## Per-key state isolation

- **Active / Cooldown pool**: in-memory only on the `TranscribeClient`
  instance. Resets to a single full active pool after a process restart.
- **Daily quota counter**: tracks `usage-<sha256(key)[:12]>.json` per key.
  A user with three free-tier keys effectively gets 75 calls/day, not 25.
- **Rate-limit throttle timestamp**: stored both in-memory and on disk in
  `last_api_completion-<sha256(key)[:12]>.json`. Marking K1 as recently
  completed does **not** delay a call on K2.
- **Round-robin pointer** (`_rr_index`): in-memory only; resumes from
  key 0 after a process restart (intentional — disk-sticking the pointer
  would couple unrelated processes).

## When to use multi-key vs single key

- **Single key**: short jobs (<25 chunks), CI jobs, paid tier.
- **Multi-key**: large batches (>25 chunks) on free tier; multi-day jobs
  where rotating keys helps; any scenario where 429s would otherwise
  require manual intervention.
- **Heavy multi-key (15–20 keys)**: continuous background transcription
  on free tier. The active/cooldown pool keeps the run alive as long as
  *some* key has not yet hit its daily cap.

## Sizing a free-tier key pool

If your goal is **continuous background transcription on the free
tier** (no waiting for the daily cap to reset), plan for **16–20 keys**.
The wrapper itself only needs ~10 keys to outlast the per-key daily
quota; the extra 6–10 keys are headroom so that:

- Keys that 429 early in the day do not exhaust the pool.
- The 10-minute cooldown reactivation cycle always has at least a few
  warm keys ready by the time the rest cool down.
- Account-level anomalies (a key accidentally leaked, a project
  deleted, etc.) don't drop you below the minimum needed to keep
  transcription running unattended.

### Where the keys come from

Each Gmail account on Google AI Studio can create **up to 10
projects**, and **each project yields 1 free-tier API key**. So a
single Gmail account tops out at 10 free keys — not enough on its own
for 16–20-key continuous transcription.

To reach the recommended pool size you need **2 Gmail accounts**:

| Gmail accounts | Projects per account | Total free keys |
| --- | --- | --- |
| 1 | 10 | 10 (marginal — risks running out mid-day) |
| **2** | **10 each** | **20 (recommended for continuous use)** |
| 3+ | 10 each | 30+ (diminishing returns, useful only for >24h jobs) |

Practical setup:

1. Create a second Gmail account (or use an existing one you control).
2. Sign in to <https://aistudio.google.com/api-keys/> from each account
   in turn and create the projects / API keys.
3. Export the keys as shell variables, then run the wrapper with all
   of them:
   ```bash
   export key1=AIzaSyA...   # account #1, project #1
   export key2=AIzaSyB...   # account #1, project #2
   # ... up to key10 for account #1 ...
   export key11=AIzaSyK...  # account #2, project #1
   # ... up to key20 for account #2 ...
   uvx --python 3.12 --from gemini-transcribe-wrapper@latest \
       gtw --gemini-api-keys $key1;...;$key20 *.mp4
   ```
4. The active/cooldown pool keeps the rotation healthy across both
   accounts — a 429 on account #1's keys is independent of account #2's
   daily quota, so cross-account rotation is what makes continuous
   transcription possible.

> **Note**: Google may change the per-account project limit or the
> free-tier daily quota at any time. The 16–20 / 2-account guidance is
> calibrated to the limits as of this writing — if Google raises the
> quota per key, fewer keys are needed; if it tightens, scale up.

## Caveats

- The wrapper does **not** verify that keys are distinct accounts. Two
  keys pointing at the same backend quota still share a quota and you'll
  burn out faster than expected.
- The wrapper does **not** retry across batch boundaries. Once a batch
  ends (one batch = one input file or one pattern), the active pool state
  is discarded. The next batch starts with a fresh active pool of all
  configured keys.
- The deprecated `--gemini-api-key` flag logs a one-time deprecation
  warning on each invocation. It will be removed in a future major
  release; migrate to `--gemini-api-keys` now.
- A chunk whose active pool drains triggers a **10-minute sleep**. There
  is no user-visible abort between chunks — Ctrl-C is the only way to
  interrupt that sleep. Use the `--request-interval-secs` knob to shorten
  the per-call throttle, but the cooldown wait itself is fixed at
  `_COOLDOWN_SECS` (600s).

## Examples

Three free-tier keys, three hours of audio (≈12 chunks):

```bash
export GEMINI_API_KEYS=K1;K2;K3
gtw long_meeting.mp4          # round-robins, falls through on 429s
```

Force exactly two keys for one run (overriding whatever is in the env):

```bash
gtw --gemini-api-keys=$ROTATING_KEY_1;$ROTATING_KEY_2 batch.mp4
```

Pay-tier with a single key (no rotation needed):

```bash
gtw --tier paid --gemini-api-key $PAID_KEY sample.mp4
```

Continuous background job with a deep key pool (15–20 free-tier keys):

```bash
# gemini-api-keys.txt: one key per line, chmod 600
gtw --gemini-api-keys-file gemini-api-keys.txt long_running/*.mp4
# Edit the file mid-run to add or retire keys — the rotation picks the
# change up before the next key, no restart needed.
# The active/cooldown pool keeps most keys warm; only the ones that hit
# 429 in the last 10 minutes are skipped.
```

## Migration from older behavior

Older versions of this wrapper (≤ 0.0.54) used a **same-key cooldown +
retry** path: on a 429 with a `Please retry in Xs` hint, the wrapper
slept `(hint + 120s)` and retried the same key once. If that retry
also 429'd, it fell through to the next key, and ultimately raised
the most-recent 429 when all keys were exhausted.

The current implementation drops that path entirely. Why:

1. Gemini's 429 message format does not let us distinguish "daily
   quota exhausted" from "short-term rate-limit" reliably — both
   return the same `Please retry in Xs` text with very similar hint
   values.
2. The wrapper already enforces a 2-minute per-call throttle, which
   absorbs short-term rate limits. Anything that still hits 429 in
   the chunk loop is, in practice, a daily-quota exhaustion that
   the same key will keep returning for the rest of the day.
3. Retrying on the same key burns another API call against an
   already-exhausted quota, which is wasteful.

The new behavior treats any 429 encountered in the chunk loop as
"key exhausted, move it to cooldown" and never retries the same key
during the same chunk. When the active pool drains, the wrapper
sleeps 10 minutes and reactivates all cooldown keys in batch — this
is the only retry loop.