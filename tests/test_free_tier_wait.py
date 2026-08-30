"""Test PST-midnight wait helpers and STT 429-hint behavior.

The ``--free-tier-wait-on-429`` flag was removed: 429s now propagate
immediately. The hint logged by :func:`stt._log_quota_hint` tells the user
how to retry. ``sleep_until_pst_midnight`` is retained as a library helper
(still used by the CLI's Ctrl-C handling) and the PST-midnight time helpers
are tested directly here.
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt, usage_counter
from gemini_transcribe_wrapper.usage_counter import (
    PST,
    seconds_until_pst_midnight,
    sleep_until_pst_midnight,
)

# --- seconds_until_pst_midnight --------------------------------------------


def test_seconds_until_pst_midnight_is_zero_at_midnight():
    at_midnight = datetime(2026, 8, 29, 0, 0, 0, tzinfo=PST)
    assert seconds_until_pst_midnight(at_midnight) == 0.0


def test_seconds_until_pst_midnight_is_full_day_at_midnight_minus_1s():
    # Just before midnight -> ~1 second remaining.
    just_before = datetime(2026, 8, 28, 23, 59, 59, tzinfo=PST)
    remaining = seconds_until_pst_midnight(just_before)
    assert 0.0 < remaining <= 1.5


def test_seconds_until_pst_midnight_returns_positive_for_normal_time():
    noon = datetime(2026, 8, 28, 12, 0, 0, tzinfo=PST)
    remaining = seconds_until_pst_midnight(noon)
    # noon PST -> midnight PST next day = 12h.
    assert 12 * 3600 - 1 <= remaining <= 12 * 3600 + 1


def test_seconds_until_pst_midnight_never_negative():
    # Far in the future (non-midnight), should still be positive.
    future = datetime(2099, 1, 1, 12, 0, 0, tzinfo=PST)
    assert seconds_until_pst_midnight(future) > 0


# --- sleep_until_pst_midnight ----------------------------------------------


def _make_advancing_clock(monkeypatch, base: datetime):
    """Patch usage_counter.pst_now so each fake-sleep advances the clock."""
    state = {"now": base}

    def fake_now():
        return state["now"]

    def fake_sleep(secs: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=secs)

    monkeypatch.setattr(usage_counter, "pst_now", fake_now)
    return state, fake_sleep


def test_sleep_until_pst_midnight_returns_immediately_at_midnight(monkeypatch):
    """When 0 seconds remain, no sleep call should be made."""
    at_midnight = datetime(2026, 8, 29, 0, 0, 0, tzinfo=PST)
    monkeypatch.setattr(usage_counter, "pst_now", lambda: at_midnight)
    sleeps: list[float] = []
    sleep_until_pst_midnight(sleep_fn=sleeps.append)
    assert sleeps == []


def test_sleep_until_pst_midnight_chunks_into_one_hour_steps(monkeypatch):
    """With 3h5m remaining, expect one 3600s sleep, then a smaller final sleep."""
    base = datetime(2026, 8, 28, 20, 55, 0, tzinfo=PST)  # 3h5m to midnight
    sleeps: list[float] = []
    _state, fake_sleep = _make_advancing_clock(monkeypatch, base)

    def recording_sleep(secs):
        sleeps.append(secs)
        fake_sleep(secs)  # advance the mocked clock

    sleep_until_pst_midnight(sleep_fn=recording_sleep)
    # First chunk is the full 1h; subsequent chunks sum to the remaining 2h5m.
    assert sleeps[0] == 3600.0
    assert sum(sleeps[1:]) == pytest.approx(2 * 3600 + 5 * 60)


def test_sleep_until_pst_midnight_chunks_respect_remaining_less_than_one_hour(monkeypatch):
    """When remaining < 1h, only one (smaller) sleep is issued."""
    base = datetime(2026, 8, 28, 23, 30, 0, tzinfo=PST)  # 30m to midnight
    sleeps: list[float] = []
    _, fake_sleep = _make_advancing_clock(monkeypatch, base)

    def recording_sleep(secs):
        sleeps.append(secs)
        fake_sleep(secs)

    sleep_until_pst_midnight(sleep_fn=recording_sleep)
    assert len(sleeps) == 1
    assert 29 * 60 < sleeps[0] <= 30 * 60


def test_sleep_until_pst_midnight_chunks_respect_custom_interval(monkeypatch):
    """A 60s interval should produce many short sleeps until midnight."""
    base = datetime(2026, 8, 28, 23, 58, 0, tzinfo=PST)  # 2m to midnight
    sleeps: list[float] = []
    _, fake_sleep = _make_advancing_clock(monkeypatch, base)

    def recording_sleep(secs):
        sleeps.append(secs)
        fake_sleep(secs)

    sleep_until_pst_midnight(check_interval_secs=60.0, sleep_fn=recording_sleep)
    # 2 minutes -> 60s + 60s.
    assert sleeps == [60.0, 60.0]


def test_sleep_until_pst_midnight_quiet_exit_on_ctrl_c(monkeypatch):
    """Ctrl-C during the wait must exit without flushing a prior SDK exception.

    A raw ``KeyboardInterrupt`` raised during the sleep would carry the
    surrounding except block's exception in ``__context__`` and Python
    would print the full chain at exit. Re-raising with ``from None`` sets
    ``__suppress_context__=True`` so only a clean ``KeyboardInterrupt``
    propagates.
    """
    base = datetime(2026, 8, 28, 20, 0, 0, tzinfo=PST)  # 4h to midnight
    monkeypatch.setattr(usage_counter, "pst_now", lambda: base)

    def fake_sleep(_secs: float) -> None:
        raise KeyboardInterrupt

    class FakeSDKError(Exception):
        pass

    captured: KeyboardInterrupt | None = None
    try:
        raise FakeSDKError("simulated SDK 429")
    except FakeSDKError:
        try:
            sleep_until_pst_midnight(sleep_fn=fake_sleep)
        except KeyboardInterrupt as kb:
            captured = kb

    assert captured is not None, "KeyboardInterrupt must propagate out"
    assert captured.__suppress_context__ is True


# --- 429 hint: retry suggestions ------------------------------------------


class _CapLogHandler(logging.Handler):
    """Capture log records for assertion in tests."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _quota_error(free_tier: bool = True) -> Exception:
    msg = (
        "Error code: 429 - You exceeded your current quota. "
        "Quota exceeded for metric: generate_content_free_tier_input_token_count, "
        "limit: 10000, model: gemini-3.5-transcribe. "
        "Please retry in 681ms."
    )
    if not free_tier:
        msg = msg.replace("free_tier", "rpm")  # short-term rate-limit flavor
    return RuntimeError(msg)


def test_quota_hint_suggests_retry_after_one_minute_or_pst_midnight(monkeypatch, caplog):
    """The 429 hint must include both 'wait ~1 minute' and 'wait until PST midnight' guidance."""
    # Pretend it's 4h20m before PST midnight so we exercise the >1m branch.
    base = datetime(2026, 8, 28, 19, 40, 0, tzinfo=PST)
    monkeypatch.setattr(usage_counter, "pst_now", lambda: base)

    caplog.set_level(logging.ERROR, logger="gemini_transcribe_wrapper.stt")
    stt._log_quota_hint(_quota_error(free_tier=True))
    text = "\n".join(rec.getMessage() for rec in caplog.records)

    assert "wait about 1 minute" in text
    assert "PST midnight" in text
    assert "4h 20m" in text
    # Exact seconds so the user can script a sleep.
    assert "sleep 15600s" in text  # 4h20m = 4*3600 + 20*60 = 15600
    assert "paid tier" in text


def test_quota_hint_short_message_when_midnight_near(monkeypatch, caplog):
    """When midnight is <1 minute away, show a single 'quota resets soon' line."""
    base = datetime(2026, 8, 28, 23, 59, 30, tzinfo=PST)  # 30s to midnight
    monkeypatch.setattr(usage_counter, "pst_now", lambda: base)

    caplog.set_level(logging.ERROR, logger="gemini_transcribe_wrapper.stt")
    stt._log_quota_hint(_quota_error(free_tier=True))
    text = "\n".join(rec.getMessage() for rec in caplog.records)

    assert "wait about 1 minute" in text
    assert "less than a minute away" in text


def test_quota_hint_silent_for_non_quota_errors(caplog):
    """Non-429 errors must not produce the retry-suggestion banner."""
    caplog.set_level(logging.ERROR, logger="gemini_transcribe_wrapper.stt")
    stt._log_quota_hint(RuntimeError("400 Bad Request - invalid argument"))
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert text == ""


# --- transcribe_chunks_sequential no longer waits on 429 ------------------


class _ImmediateClient:
    """Fails on first call, never retries (used to verify no auto-wait)."""

    def __init__(self) -> None:
        self.api_logs: list[dict] = []
        self.calls = 0

    def transcribe_chunk(self, chunk_mp3: Path, chunk_index: int = 0):
        self.calls += 1
        raise RuntimeError("429 quota exceeded")


def _chunk(path: Path, name: str) -> Path:
    p = path / name
    p.write_bytes(b"fake-mp3")
    return p


def test_transcribe_chunks_sequential_propagates_429_without_retry(tmp_path, monkeypatch):
    """A 429 must propagate immediately; no sleep_until_pst_midnight call."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        usage_counter, "sleep_until_pst_midnight", lambda: sleeps.append(1.0)
    )

    client = _ImmediateClient()
    chunks = [_chunk(tmp_path, "chunk_000.mp3")]
    with pytest.raises(RuntimeError, match="429"):
        stt.transcribe_chunks_sequential(
            client, chunks, request_interval_secs=0.0
        )

    assert client.calls == 1
    assert sleeps == []  # no auto-wait triggered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
