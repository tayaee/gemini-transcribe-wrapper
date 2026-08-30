"""Daily API usage counter persisted under ~/.cache/gemini-transcribe-wrapper/.

The Gemini free tier allows ~5 API calls per day; the counter resets at
midnight Pacific Standard Time (UTC-08:00, no DST).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

FREE_TIER_DAILY_LIMIT = "~5"
PST = timezone(timedelta(hours=-8))
USAGE_FILE = "usage.json"
_KEY_HASH_LEN = 12  # hex chars of SHA-256 used in per-key usage filenames.


def cache_dir() -> Path:
    """~/.cache/gemini-transcribe-wrapper (GTW_CACHE_DIR overrides for tests)."""
    override = os.environ.get("GTW_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "gemini-transcribe-wrapper"


def pst_now() -> datetime:
    """Current time in PST (UTC-08:00, fixed, no DST)."""
    return datetime.now(PST)


def pst_date(now: datetime | None = None) -> str:
    """PST date (YYYY-MM-DD) for the given time (default: now)."""
    return (now or pst_now()).strftime("%Y-%m-%d")


def _key_hash(api_key: str | None) -> str:
    """Return a short, stable, non-reversible hash of an API key.

    Used to scope daily usage counts per key, so different Gemini API keys
    do not share a quota counter. Empty string when no key is provided
    (the unscoped legacy file ``usage.json``).
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:_KEY_HASH_LEN]


def _load(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _legacy_count_today(cache: Path | None = None) -> int:
    """Today's count from the legacy unscoped ``usage.json``.

    Used as a fallback so users who ran an older version (which always
    wrote to ``usage.json`` when no key was passed) still see their real
    daily call count under the per-key display. Returns 0 when the legacy
    file is missing.
    """
    path = (cache or cache_dir()) / USAGE_FILE
    return _load(path).get(pst_date(), 0)


def _save(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _usage_file(cache: Path | None = None, api_key: str | None = None) -> Path:
    """Return the counter file for ``api_key`` under ``cache``.

    Without a key, the unscoped legacy ``usage.json`` is returned. With a
    key, a per-key ``usage-<sha256[:12]>.json`` is used so two different
    keys cannot share a daily quota.
    """
    base = (cache or cache_dir()) / USAGE_FILE
    h = _key_hash(api_key)
    if not h:
        return base
    return base.parent / f"usage-{h}.json"


def count_today(cache: Path | None = None, api_key: str | None = None) -> int:
    """API calls made so far today (PST) for ``api_key``. 0 when no counter file exists yet.

    When a per-key file is used and today's entry is missing, the legacy
    unscoped ``usage.json`` count is shown — :func:`increment_today` migrates
    it into the per-key file on the first call after upgrade, so the two
    never double-count.
    """
    key_count = _load(_usage_file(cache, api_key)).get(pst_date(), 0)
    if key_count > 0 or not api_key:
        return key_count
    return _legacy_count_today(cache)


def increment_today(cache: Path | None = None, api_key: str | None = None) -> int:
    """Increment today's (PST) API call count for ``api_key`` and return the new count.

    On the first per-key call after an upgrade, fold the legacy unscoped
    ``usage.json`` count into the per-key file so users who ran the older
    version still see their real daily tally. Best-effort: a counter write
    failure must never break transcription, so it is logged and swallowed
    (the count returned may then be stale).
    """
    path = _usage_file(cache, api_key)
    try:
        lock = FileLock(str(path) + ".lock")
        with lock:
            data = _load(path)
            day = pst_date()
            # One-time legacy migration: when the per-key file has no entry
            # for today yet, carry over the unscoped legacy count so the
            # user's pre-upgrade calls aren't silently lost.
            legacy = (
                _load((cache or cache_dir()) / USAGE_FILE).get(day, 0)
                if api_key
                else 0
            )
            base = max(data.get(day, 0), legacy)
            data[day] = base + 1
            _save(path, data)
            # Clear the migrated legacy entry so subsequent increments
            # don't double-count it.
            if legacy and api_key:
                legacy_path = (cache or cache_dir()) / USAGE_FILE
                legacy_data = _load(legacy_path)
                legacy_data.pop(day, None)
                if legacy_data:
                    _save(legacy_path, legacy_data)
                else:
                    legacy_path.unlink(missing_ok=True)
        return data[day]
    except Exception:  # counter must never break transcription
        logger.exception("Failed to update usage counter at %s", path)
        return count_today(path, api_key)


def _mask_key(key: str | None) -> str:
    """Show only the first and last 4 chars of an API key, or 'unset' if empty."""
    if not key:
        return "unset"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


def usage_summary_line(cache: Path | None = None, api_key: str | None = None) -> str:
    """One-line usage summary ending with today's count and the free tier limit."""
    used = count_today(cache, api_key)
    day = pst_date()
    masked = _mask_key(api_key)
    return (
        f"API calls today {day} (PST-08:00) with key '{masked}': "
        f"attempted {used} (free tier limit: {FREE_TIER_DAILY_LIMIT})"
    )


def seconds_until_pst_midnight(now: datetime | None = None) -> float:
    """Seconds remaining until the next PST midnight (00:00 PST = 08:00 UTC).

    At exactly 00:00:00 PST, returns 0 — the quota has just reset and no wait
    is needed. Otherwise, returns the seconds until the next 00:00 PST.
    """
    current = now or pst_now()
    tod = (
        current.hour * 3600
        + current.minute * 60
        + current.second
        + current.microsecond / 1e6
    )
    if tod == 0:
        return 0.0
    return 86400.0 - tod


def sleep_until_pst_midnight(
    check_interval_secs: float = 3600.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Sleep until the next PST midnight, logging the remaining time each step.

    Splits the wait into chunks of up to ``check_interval_secs`` (default 1h),
    logging the remaining time before each ``sleep_fn`` call. Intended for
    free-tier batch runs that hit the 25 RPD daily quota or 429 errors and
    need to wait for the quota to reset. ``sleep_fn`` is injectable for tests.

    Ctrl-C during the wait exits quietly: a fresh ``KeyboardInterrupt`` is
    re-raised with ``from None`` so the SDK's preceding quota exception does
    not get flushed into the traceback at exit.
    """
    while True:
        remaining = seconds_until_pst_midnight()
        if remaining <= 0:
            logger.warning(
                "Free-tier wait: PST midnight reached (quota reset); resuming API calls."
            )
            return
        chunk = min(check_interval_secs, remaining)
        logger.warning(
            "Free-tier wait: %.1fh remaining until PST midnight; sleeping %.0fs.",
            remaining / 3600.0,
            chunk,
        )
        try:
            sleep_fn(chunk)
        except KeyboardInterrupt:
            logger.warning(
                "Free-tier wait cancelled by user (Ctrl-C); exiting quietly."
            )
            raise KeyboardInterrupt from None
