"""Shared helpers for masking API keys in user-visible logs and CLI output.

Centralizes the ``[redacted]<last 4>`` format used across log lines,
CLI warnings, and the free-tier usage summary. Avoids leaking key
prefixes while still letting users distinguish which key they're
seeing at a glance.

Importing from here instead of inlining the format everywhere keeps
the three call sites in lock-step — change the rule here and every
log line, CLI warning, and summary line updates together.

Issue-007 also adds :func:`api_key_tail`, the canonical 8-char tail
used by audit logs, blacklist filenames, and per-key cache paths.
"""

from __future__ import annotations

# Conventional tail length used by audit logs / blacklist filenames /
# per-key cache paths. Defined as a constant so other modules can import
# it (e.g. ``from ._key_utils import API_KEY_TAIL_LENGTH``) without
# hard-coding the magic number 8.
API_KEY_TAIL_LENGTH = 8


def api_key_tail(api_key: str | None, *, length: int = API_KEY_TAIL_LENGTH) -> str:
    """Return the last ``length`` chars of ``api_key``, or ``""`` if ``None``.

    Convention is 8 chars (see spec §2); pass ``length=4`` if a legacy
    4-char tail is needed (e.g. for a compact summary line).

    Keys shorter than ``length`` are returned in full. ``None`` and empty
    strings both yield ``""`` so callers can safely concatenate the
    result without an extra ``if`` guard.
    """
    if not api_key:
        return ""
    if length <= 0:
        return ""
    return api_key[-length:] if len(api_key) >= length else api_key


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

