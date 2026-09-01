"""Shared helpers for masking API keys in user-visible logs and CLI output.

Centralizes the ``[redacted]<last 4>`` format used across log lines,
CLI warnings, and the free-tier usage summary. Avoids leaking key
prefixes while still letting users distinguish which key they're
seeing at a glance.

Importing from here instead of inlining the format everywhere keeps
the three call sites in lock-step — change the rule here and every
log line, CLI warning, and summary line updates together.
"""


def mask_key(key: str | None) -> str:
    """Return ``[redacted]<last 4>`` for ``key``.

    Returns ``"unset"`` when ``key`` is ``None`` or empty (matches the
    free-tier summary's "no key configured" wording) and the bare
    ``[redacted]`` tag for keys of 4 characters or fewer (where showing
    even the tail would leak the whole key).
    """
    if not key:
        return "unset"
    if len(key) <= 4:
        return "[redacted]"
    return f"[redacted]{key[-4:]}"
