"""Daily API usage counter persisted under ~/.cache/gemini-transcribe-wrapper/.

The Gemini free tier allows ~25 API calls per day; the counter resets at
midnight Pacific Time (PT, America/Los_Angeles).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock

logger = logging.getLogger(__name__)

FREE_TIER_DAILY_LIMIT = "~25"
PT = ZoneInfo("America/Los_Angeles")
PST = PT  # backward compatibility alias
USAGE_FILE = "usage.json"
_KEY_HASH_LEN = 12  # hex chars of SHA-256 used in per-key usage filenames.


def cache_dir() -> Path:
    """~/.cache/gemini-transcribe-wrapper (GTW_CACHE_DIR overrides for tests)."""
    override = os.environ.get("GTW_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "gemini-transcribe-wrapper"


def pt_now() -> datetime:
    """Current time in Pacific Time (America/Los_Angeles)."""
    return datetime.now(PT)


def pst_now() -> datetime:
    """Legacy alias for :func:`pt_now`."""
    return pt_now()


def pt_date(now: datetime | None = None) -> str:
    """PT date (YYYY-MM-DD) for the given time (default: now)."""
    return (now or pt_now()).strftime("%Y-%m-%d")


def pst_date(now: datetime | None = None) -> str:
    """Legacy alias for :func:`pt_date`."""
    return pt_date(now)


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
    return _load(path).get(pt_date(), 0)


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
    """API calls made so far today (PT) for ``api_key``. 0 when no counter file exists yet.

    When a per-key file is used and today's entry is missing, the legacy
    unscoped ``usage.json`` count is shown — :func:`increment_today` migrates
    it into the per-key file on the first call after upgrade, so the two
    never double-count.
    """
    key_count = _load(_usage_file(cache, api_key)).get(pt_date(), 0)
    if key_count > 0 or not api_key:
        return key_count
    return _legacy_count_today(cache)


def increment_today(cache: Path | None = None, api_key: str | None = None) -> int:
    """Increment today's (PT) API call count for ``api_key`` and return the new count.

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
            day = pt_date()
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
    """Mask an API key for log/summary lines as ``[redacted]<last 4>``.

    Thin wrapper around :func:`gemini_transcribe_wrapper._key_utils.mask_key`
    kept for backward compatibility with existing callers.
    """
    from ._key_utils import mask_key

    return mask_key(key)


def _key_tail(key: str | None) -> str:
    """Return ``[redacted]<last 4>`` for the API key (or ``unset`` if empty).

    Used in the free-tier summary line so the user can confirm at a glance
    which key the daily count belongs to without exposing the full key.
    Format matches :func:`mask_key` for consistency across every
    user-visible log line.
    """
    from ._key_utils import mask_key

    return mask_key(key)


def _format_hours_minutes(hours: int, minutes: int) -> str:
    """Format hours and minutes with proper singular/plural suffixes."""
    h_str = f"{hours} hour" if hours == 1 else f"{hours} hours"
    m_str = f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{h_str} {m_str}"


def _hours_minutes_until_midnight(now: datetime | None = None) -> tuple[int, int]:
    """Return ``(hours, minutes)`` remaining until the next PT midnight."""
    remaining = int(seconds_until_pt_midnight(now))
    hours, rem_min = divmod(remaining // 60, 60)
    return int(hours), int(rem_min)


def usage_summary_line(
    cache: Path | None = None,
    api_key: str | None = None,
    tier: str = "free",
) -> str:
    """One-line usage summary for the active tier.

    Free tier (default) — points at the Google AI dev dashboard and rate
    limit docs, names the API key by its tail, and tells the user how long
    until the daily free-tier quota resets at PT midnight.

    Paid tier — keeps the legacy ``API call attempts today ...`` format so
    existing scripts/users see the same line.
    """
    if tier == "free":
        tail = _key_tail(api_key)
        hours, minutes = _hours_minutes_until_midnight()
        time_left = _format_hours_minutes(hours, minutes)
        return (
            f"Find your free-tier usage at https://ai.dev for your API key "
            f"ending with '{tail}' and rate limits at "
            f"https://ai.google.dev/gemini-api/docs/rate-limits. "
            f"Your free tier limits will reset at midnight PT "
            f"({time_left} left)."
        )
    used = count_today(cache, api_key)
    day = pt_date()
    masked = _mask_key(api_key)
    return (
        f"API call attempts today {day} (PT) with key '{masked}': "
        f"attempted {used} (free tier limit: {FREE_TIER_DAILY_LIMIT})"
    )


def seconds_until_pt_midnight(now: datetime | None = None) -> float:
    """Seconds remaining until the next PT midnight (00:00 Pacific Time).

    At exactly 00:00:00 PT, returns 0 — the quota has just reset and no wait
    is needed. Otherwise, returns the seconds until the next 00:00 PT.
    """
    current = now or pt_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=PT)
    else:
        current = current.astimezone(PT)
    next_midnight = datetime(
        current.year, current.month, current.day, 0, 0, 0, tzinfo=PT
    )
    if current == next_midnight:
        return 0.0
    next_midnight += timedelta(days=1)
    return max(0.0, (next_midnight - current).total_seconds())


def seconds_until_pst_midnight(now: datetime | None = None) -> float:
    """Legacy alias for :func:`seconds_until_pt_midnight`."""
    return seconds_until_pt_midnight(now)


def sleep_until_pt_midnight(
    check_interval_secs: float = 3600.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Sleep until the next PT midnight, logging the remaining time each step.

    Splits the wait into chunks of up to ``check_interval_secs`` (default 1h),
    logging the remaining time before each ``sleep_fn`` call. Intended for
    free-tier batch runs that hit the 25 RPD daily quota or 429 errors and
    need to wait for the quota to reset. ``sleep_fn`` is injectable for tests.

    Ctrl-C during the wait exits quietly: a fresh ``KeyboardInterrupt`` is
    re-raised with ``from None`` so the SDK's preceding quota exception does
    not get flushed into the traceback at exit.
    """
    while True:
        remaining = seconds_until_pt_midnight()
        if remaining <= 0:
            logger.warning(
                "Free-tier wait: PT midnight reached (quota reset); resuming API calls."
            )
            return
        chunk = min(check_interval_secs, remaining)
        logger.warning(
            "Free-tier wait: %.1fh remaining until PT midnight; sleeping %.0fs.",
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


def sleep_until_pst_midnight(
    check_interval_secs: float = 3600.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Legacy alias for :func:`sleep_until_pt_midnight`."""
    return sleep_until_pt_midnight(check_interval_secs=check_interval_secs, sleep_fn=sleep_fn)
