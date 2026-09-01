---
name: issue-007-api-key-tail-consistency
description: Use the 8-character api_key_tail everywhere (currently a few log lines use key[-4:] which is inconsistent with the spec and the audit-log writer).
metadata:
  type: issue
  spec: spec-2-free-tier-quota-hardening
---

# issue-007: `api_key_tail` length consistency (8 chars everywhere)

## Why

Spec §2. The user-defined convention is `api_key_tail = api_key[-8:]`.
This is the value used in:

- the audit log record (`append_audit_log`)
- the per-key audit file path (issue-004)
- the per-key blacklist file path (issue-002)
- the per-key usage counter filename
- log lines that include the key for debugging

Today, **`stt.py:832`** logs `[redacted]{key[-4:]}` — only the last 4
characters. That breaks the convention and makes correlating log lines
with audit entries needlessly annoying.

## What

Replace the inconsistent `key[-4:]` uses with a single helper:

```python
# src/gemini_transcribe_wrapper/_key_utils.py (or extend existing)
def api_key_tail(api_key: str | None, *, length: int = 8) -> str:
    """Return the last ``length`` chars of ``api_key``, or ``""`` if None.

    Convention is 8 chars (see spec §2); pass ``length=4`` if a legacy
    4-char tail is needed.
    """
    if not api_key:
        return ""
    return api_key[-length:] if len(api_key) >= length else api_key
```

All current uses of `key[-8:]` and `key[-4:]` are routed through this
helper. The audit log writer (which already uses 8) is unchanged in
behavior, but its call site is rewritten for consistency.

## How to apply

- Search the entire `src/` tree for `key\[-\d:\]` and `api_key\[-\d:\]`
  patterns; replace with `_key_utils.api_key_tail(...)`.
- Existing `_mask_key` in `stt.py` (uses last 4) is kept as-is for the
  deprecation-warning message; we only standardize the `api_key_tail`
  for log/audit/blacklist contexts.
- The CLI's `--gemini-api-keys=…` re-emission already uses
  `_mask_key(k)` (last 4 masked, e.g. `AIza****abcd`); this is the
  intended UX for re-emission and stays unchanged.

## Files to touch

- `src/gemini_transcribe_wrapper/_key_utils.py` — new helper
- `src/gemini_transcribe_wrapper/stt.py` — replace `key[-4:]` in the
  `logger.info("Removing key %s ...")` line
- Anywhere else found by the search

## Acceptance

- `grep -rE "key\[-[0-9]+:\]|api_key\[-[0-9]+:\]" src/` returns only the
  helper definition (and tests that pin the legacy behavior).
- A log line that mentions a key shows 8 chars (e.g.
  `[redacted]mnopqrst` for a key ending in `mnopqrst`), not 4.
- The audit log record's `api_key_tail` field is identical to the
  tail shown in the matching log line.

## Notes

- We keep both 8 and 4 available in `api_key_tail(..., length=N)` so
  we don't lose the option to use the shorter form for UX purposes
  (e.g. the CLI re-emission line) — only the audit/log paths are
  standardized to 8.
- The 8-char tail matches what the user spec calls `api-key-tail`.
