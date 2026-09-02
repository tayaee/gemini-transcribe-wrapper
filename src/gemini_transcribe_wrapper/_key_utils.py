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

import json
import os
from datetime import datetime
from pathlib import Path

# Conventional tail length used by audit logs / blacklist filenames /
# per-key cache paths. Defined as a constant so other modules can import
# it (e.g. ``from ._key_utils import API_KEY_TAIL_LENGTH``) without
# hard-coding the magic number 8.
API_KEY_TAIL_LENGTH = 8
LAST_USED_KEY_FILE = "last-used-api-key.json"


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
    """Return the last 8 characters for ``key`` (or ``"unset"`` if None/empty)."""
    if not key:
        return "unset"
    return api_key_tail(key)


def save_last_used_api_key(key: str, cache: Path | None = None) -> None:
    """Record the last used API key to ~/.cache/gemini-transcribe-wrapper/last-used-api-key.json."""
    if not key or not key.strip():
        return
    try:
        from .usage_counter import cache_dir

        cd = cache or cache_dir()
        cd.mkdir(parents=True, exist_ok=True)
        path = cd / LAST_USED_KEY_FILE
        tmp = path.with_suffix(".json.tmp")
        data = {
            "last_used_api_key": key.strip(),
            "last_used_api_key_tail": api_key_tail(key.strip()),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001, S110
        pass


def load_last_used_api_key(cache: Path | None = None) -> str | None:
    """Load the last used API key from ~/.cache/gemini-transcribe-wrapper/last-used-api-key.json."""
    try:
        from .usage_counter import cache_dir

        path = (cache or cache_dir()) / LAST_USED_KEY_FILE
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        val = data.get("last_used_api_key")
        return str(val).strip() if val else None
    except Exception:  # noqa: BLE001
        return None

