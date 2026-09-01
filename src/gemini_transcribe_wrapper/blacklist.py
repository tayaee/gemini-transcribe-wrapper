"""Per-input-file blacklist for non-429 errors (issue-002, spec §4.2).

When Gemini returns a 400/500/etc., rotating API keys does not help —
the file itself is the problem (corrupted, unsupported codec, too large,
etc.). Re-trying with a new key still consumes a call against an
already-exhausted quota and produces the same error.

The :class:`InputBlacklist` dataclass wraps a single file path; on
:meth:`add` it persists the file's absolute path into a JSON store under
the per-``api_key_tail`` cache directory. On the next pass (whether
immediate or via ``--loop*``), :meth:`is_blacklisted` returns True until
the TTL elapses, and the batch driver skips the file silently.

The blacklist file is keyed by status code so a future operator can
clear 4xx and 5xx entries independently if needed.

Atomicity: every write goes through ``tmp + os.replace`` so a process
crash mid-write can never leave a half-written JSON file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_TTL_SECS = 21_600  # 6 hours per spec §4.2


@dataclass
class InputBlacklist:
    """Per-input-file blacklist entry backed by an on-disk JSON store.

    The store is shared across all files within a single ``cache_dir``
    (typically one per ``api_key_tail``), with separate files for each
    HTTP status code bucket (``http-status-{400,500}.json``).

    Parameters
    ----------
    path:
        The input file path (absolute or relative — resolved on add).
    cache_dir:
        Directory holding the JSON store. Usually
        ``~/.cache/gemini-transcribe-wrapper/<api_key_tail>/``.
    ttl_secs:
        How long an entry stays active. Default 6h. The constructor
        clamps the value to the spec's allowed range (60..604800).
    """

    path: Path
    cache_dir: Path
    ttl_secs: int = DEFAULT_TTL_SECS
    _entries: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _loaded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # Clamp TTL into the spec-allowed range.
        if self.ttl_secs < 60:
            self.ttl_secs = 60
        elif self.ttl_secs > 604_800:
            self.ttl_secs = 604_800
        # Always work with absolute paths so globs/re-globs match.
        self.path = Path(self.path).resolve()

    # --- public API --------------------------------------------------------

    def is_blacklisted(self, now: float | None = None) -> bool:
        """True if the file has an entry whose TTL has not elapsed."""
        if now is None:
            now = time.time()
        self._ensure_loaded()
        entry = self._entries.get(str(self.path))
        if entry is None:
            return False
        return now < float(entry["expires_at_epoch"])

    def add(self, status: int, now: float | None = None) -> None:
        """Persist (or refresh) the blacklist entry for this file.

        Subsequent ``add`` calls for the same file preserve the
        ``first_blacklisted_at_epoch`` (so the TTL window is measured
        from the *first* 4xx/5xx, not the most recent), but advance the
        ``expires_at_epoch`` so a file that keeps failing stays
        blacklisted longer.
        """
        if now is None:
            now = time.time()
        self._ensure_loaded()
        path_key = str(self.path)
        existing = self._entries.get(path_key)
        first = (
            float(existing["first_blacklisted_at_epoch"])
            if existing
            else float(now)
        )
        expires = float(now) + float(self.ttl_secs)
        self._entries[path_key] = {
            "first_blacklisted_at_epoch": first,
            "expires_at_epoch": expires,
            "status_code": int(status),
        }
        self._save_all(status=status)

    # --- internal ----------------------------------------------------------

    @property
    def _filename(self) -> str:
        # Round status to the canonical 4xx / 5xx bucket — the spec
        # distinguishes these semantically. For other codes (e.g. a
        # future 999), we use the literal code in the filename so
        # nothing is silently dropped.
        return f"http-status-{int(self._status_hint())}.json"

    def _status_hint(self) -> int:
        """Pick the status code to use for the bucket file.

        Reads the most recently added entry's status_code. Used only
        by the ``_filename`` property to disambiguate which JSON file
        to load when no entry exists yet (defaults to 400).
        """
        if not self._entries:
            return 400
        # Use the latest-inserted status code (dict insertion order).
        return int(next(reversed(self._entries.values()))["status_code"])

    def _bucket_path(self, status: int) -> Path:
        return self.cache_dir / f"http-status-{int(status)}.json"

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        # Lazy-load all bucket files under cache_dir so a multi-status
        # file (e.g. one add() with 400, another with 500) is read
        # coherently.
        if not self.cache_dir.exists():
            return
        for path in sorted(self.cache_dir.glob("http-status-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Corrupt or unreadable file → skip. Next add() will
                # rewrite it cleanly.
                continue
            entries = data.get("entries", {})
            if isinstance(entries, dict):
                self._entries.update(entries)

    def _save_all(self, status: int) -> None:
        """Persist *all* known entries under the bucket for ``status``.

        We write the full set every time because each bucket file is
        the single source of truth for its status code. Writes are
        atomic via ``tmp + os.replace``.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Filter entries to those that actually belong to this status.
        scoped = {
            k: v
            for k, v in self._entries.items()
            if int(v.get("status_code", status)) == int(status)
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ttl_secs": int(self.ttl_secs),
            "entries": scoped,
        }
        target = self._bucket_path(status)
        # Atomic write: tmp + os.replace.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=str(self.cache_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup of the tmp file on failure.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
