"""Daily API usage counter persisted under ~/.cache/gemini-transcribe-wrapper/.

The Gemini free tier allows 25 API calls per day; the counter resets at
midnight Pacific Standard Time (UTC-08:00, no DST).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

FREE_TIER_DAILY_LIMIT = 25
PST = timezone(timedelta(hours=-8))
USAGE_FILE = "usage.json"


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


def _load(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _save(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _usage_file(cache: Path | None = None) -> Path:
    return (cache or cache_dir()) / USAGE_FILE


def count_today(cache: Path | None = None) -> int:
    """API calls made so far today (PST). 0 when no counter file exists yet."""
    return _load(_usage_file(cache)).get(pst_date(), 0)


def increment_today(cache: Path | None = None) -> int:
    """Increment today's (PST) API call count and return the new count.

    Best-effort: a counter write failure must never break transcription, so
    it is logged and swallowed (the count returned may then be stale).
    """
    path = _usage_file(cache)
    try:
        lock = FileLock(str(path) + ".lock")
        with lock:
            data = _load(path)
            day = pst_date()
            data[day] = data.get(day, 0) + 1
            _save(path, data)
        return data[day]
    except Exception:  # counter must never break transcription
        logger.exception("Failed to update usage counter at %s", path)
        return count_today(path)


def usage_summary_line(cache: Path | None = None) -> str:
    """One-line usage summary ending with today's count and the free tier limit."""
    used = count_today(cache)
    day = pst_date()
    return (
        f"API calls today {day} (PST-08:00): {used}/{FREE_TIER_DAILY_LIMIT} "
        f"(free tier limit: {FREE_TIER_DAILY_LIMIT})"
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
        sleep_fn(chunk)
