"""Test PST-midnight wait helpers and STT 429-hint behavior.

When the Gemini API returns a 429 containing ``Please retry in Xs``,
:func:`stt._parse_retry_after_seconds` extracts ``X`` and
:func:`stt.TranscribeClient.transcribe_chunk` sleeps ``X + 60`` seconds then
retries the call once. 429s without the hint, or 429s whose retry also fails,
propagate immediately so :func:`stt._log_quota_hint` runs as before.
``sleep_until_pst_midnight`` is retained as a library helper (still used by
the CLI's Ctrl-C handling) and the PST-midnight time helpers are tested
directly here.
"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

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


class _ImmediateClient(stt.TranscribeClient):
    """Fails on first call, never retries (used to verify no auto-wait)."""

    def __init__(self) -> None:
        self.api_logs: list[dict] = []
        self.calls = 0
        self.api_key = "fake"

    def transcribe_chunk(
        self,
        chunk_mp3: Path | None,
        chunk_index: int = 0,
        source_file: str | Path | None = None,
        chunk_duration_secs: float | None = None,
    ) -> stt.TranscriptionResult:
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


def test_throttle_api_call_enforces_interval(monkeypatch):
    stt.reset_api_rate_limiter()
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    # First call: no previous call, no sleep
    stt._throttle_api_call(30.0)
    assert sleeps == []
    stt.record_api_call_completed()

    # Second call right after completion: sleeps ~30s
    stt._throttle_api_call(30.0)
    assert len(sleeps) == 1
    assert 29.0 <= sleeps[0] <= 30.0

    stt.reset_api_rate_limiter()


def test_throttle_api_call_log_includes_today_count_and_reset_countdown(
    monkeypatch, tmp_path, caplog
):
    """The throttling log line must include today's call count and reset time."""
    monkeypatch.setenv("GTW_CACHE_DIR", str(tmp_path))
    stt.reset_api_rate_limiter()

    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    api_key = "throttle-log-test-key"
    # Pre-populate today's count so the message embeds a real number.
    usage_counter.increment_today(api_key=api_key)
    usage_counter.increment_today(api_key=api_key)
    usage_counter.increment_today(api_key=api_key)

    # Pretend it's 4h20m before PST midnight so the countdown is predictable.
    base = datetime(2026, 8, 30, 19, 40, 0, tzinfo=usage_counter.PST)
    monkeypatch.setattr(usage_counter, "pst_now", lambda: base)

    # First call establishes a completion timestamp.
    stt._throttle_api_call(120.0, api_key=api_key)
    stt.record_api_call_completed(api_key=api_key)
    sleeps.clear()

    # Second call (right after) must sleep and log the new format.
    caplog.set_level(logging.INFO, logger="gemini_transcribe_wrapper.stt")
    stt._throttle_api_call(120.0, api_key=api_key)

    assert len(sleeps) == 1
    info_lines = [
        rec.message for rec in caplog.records
        if "Free-tier rate limit" in rec.message
    ]
    assert len(info_lines) == 1
    msg = info_lines[0]
    assert "The # of API call attempts today: 3" in msg
    # 4h20m to midnight -> "4 hours 20 minutes PST-08:00"
    assert "4 hours 20 minutes PST-08:00" in msg
    assert "sleeping 1" in msg  # ~120s sleep (elapsed=0.0s)

    stt.reset_api_rate_limiter()


def test_throttle_api_call_no_sleep_when_interval_zero(monkeypatch):
    stt.reset_api_rate_limiter()
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    stt._throttle_api_call(0.0)
    stt._throttle_api_call(0.0)
    assert sleeps == []

    stt.reset_api_rate_limiter()


def test_throttle_api_call_persists_across_process_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GTW_CACHE_DIR", str(tmp_path))
    stt.reset_api_rate_limiter()

    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    base_time = 1_000_000.0
    monkeypatch.setattr(stt.time, "time", lambda: base_time)

    # Process 1 finishes an API call at base_time
    stt.record_api_call_completed(api_key="fake-key-1")

    # Simulate Process 2 starting fresh (clearing in-memory timestamps)
    stt._LAST_API_COMPLETION_MONOTONIC = None
    stt._LAST_API_COMPLETION_WALL = None

    # Process 2 starts 10s after Process 1 completed
    monkeypatch.setattr(stt.time, "time", lambda: base_time + 10.0)
    stt._throttle_api_call(60.0, api_key="fake-key-1")

    assert len(sleeps) == 1
    assert 49.0 <= sleeps[0] <= 51.0

    stt.reset_api_rate_limiter()


def test_throttle_api_call_bypasses_for_paid_tier(monkeypatch):
    stt.reset_api_rate_limiter()
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    stt.record_api_call_completed(api_key="fake-key-1")
    # Even with request_interval_secs=60.0, paid tier does not sleep
    stt._throttle_api_call(60.0, api_key="fake-key-1", tier="paid")
    assert sleeps == []

    # Free tier sleeps
    stt._throttle_api_call(60.0, api_key="fake-key-1", tier="free")
    assert len(sleeps) == 1

    stt.reset_api_rate_limiter()


# --- 429 "Please retry in Xs" retry-once behavior --------------------------


class _QuotaClient(stt.TranscribeClient):
    """Test client: ``interactions.create`` follows a scripted list of side_effects."""

    def __init__(self, side_effects: list) -> None:
        self.api_key = "AIzaSyDummyKey12345678"
        self.api_logs: list[dict] = []
        self.request_interval_secs = 0.0

        mock_upload = MagicMock()
        mock_upload.uri = "files/test"
        mock_upload.name = "files/test"
        self.client = MagicMock()
        self.client.files.upload = MagicMock(return_value=mock_upload)
        self.client.files.delete = MagicMock()

        mock_step = MagicMock()
        mock_step.type = "model_output"
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.annotations = []
        mock_step.content = [mock_content]
        ok_interaction = MagicMock()
        ok_interaction.steps = [mock_step]
        ok_interaction.output_text = "안녕하세요"

        # Sentinel: pass _SENTINEL_OK to mean "return the successful interaction".
        _SENTINEL_OK = object()

        effects = list(side_effects)
        self._effects = effects
        self._ok = ok_interaction
        self._SENTINEL_OK = _SENTINEL_OK
        self.calls = 0

        def _create(**_kwargs):
            self.calls += 1
            if self.calls - 1 < len(self._effects):
                fx = self._effects[self.calls - 1]
                if isinstance(fx, Exception):
                    raise fx
                if fx is _SENTINEL_OK:
                    return self._ok
                return fx
            return self._ok

        self.client.interactions.create = MagicMock(side_effect=_create)

    def _generation_config(self):  # type: ignore[override]
        return MagicMock()


def _retry_msg(seconds: float) -> str:
    return (
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota. "
        f"\\nPlease retry in {seconds}s.', 'code': 'too_many_requests'}}"
    )


def test_parse_retry_after_seconds_extracts_hints():
    assert stt._parse_retry_after_seconds(_retry_msg(53.07)) == pytest.approx(53.07)
    assert stt._parse_retry_after_seconds("Please retry in 42s.") == 42.0
    assert stt._parse_retry_after_seconds("no hint here") is None
    assert stt._parse_retry_after_seconds("") is None
    # Case-insensitive
    assert stt._parse_retry_after_seconds("PLEASE RETRY IN 17s") == 17.0


def test_transcribe_chunk_retries_on_429_with_retry_hint(tmp_path, monkeypatch, caplog):
    """First 429 with 'Please retry in Xs' → sleep X+60s → retry → succeed."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    first_exc = RuntimeError(_retry_msg(30.0))
    # Build the client first so we can grab its _SENTINEL_OK for the retry step.
    client = _QuotaClient(side_effects=[first_exc])
    client._effects.append(client._SENTINEL_OK)

    chunk_mp3 = tmp_path / "chunk_000.mp3"
    chunk_mp3.write_bytes(b"dummy audio data")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(
            chunk_mp3,
            chunk_index=0,
            source_file="/input.mp4",
            chunk_duration_secs=120.0,
        )

    assert result.text == "안녕하세요"
    assert client.calls == 2  # first attempt + one retry
    assert sleeps == [pytest.approx(90.0)]  # 30 + 60
    # One log line on success (per user spec)
    cooldown_logs = [
        rec.message for rec in caplog.records
        if "succeeded via cooldown" in rec.message
    ]
    assert len(cooldown_logs) == 1
    sleeping_logs = [
        rec.message for rec in caplog.records
        if "sleeping 30.0+60 secs then retrying once" in rec.message
    ]
    assert len(sleeping_logs) == 1


def test_transcribe_chunk_does_not_retry_on_429_without_retry_hint(tmp_path, monkeypatch):
    """429 with no 'Please retry in Xs' → propagate immediately, no sleep, no retry."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    client = _QuotaClient(side_effects=[RuntimeError("429 plain error, no hint")])

    chunk_mp3 = tmp_path / "chunk_000.mp3"
    chunk_mp3.write_bytes(b"dummy audio data")

    with pytest.raises(RuntimeError, match="429"):
        client.transcribe_chunk(
            chunk_mp3,
            chunk_index=0,
            source_file="/input.mp4",
            chunk_duration_secs=120.0,
        )

    assert client.calls == 1
    assert sleeps == []


def test_transcribe_chunk_re_raises_original_on_retry_failure(tmp_path, monkeypatch):
    """Retry also fails → re-raise the ORIGINAL 429, not the second exception."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    first_exc = RuntimeError(_retry_msg(10.0))
    second_exc = RuntimeError("still failing after retry")
    client = _QuotaClient(side_effects=[first_exc, second_exc])

    chunk_mp3 = tmp_path / "chunk_000.mp3"
    chunk_mp3.write_bytes(b"dummy audio data")

    with pytest.raises(RuntimeError) as excinfo:
        client.transcribe_chunk(
            chunk_mp3,
            chunk_index=0,
            source_file="/input.mp4",
            chunk_duration_secs=120.0,
        )

    # The original (first) error is the one that propagates
    assert "Please retry in 10.0s" in str(excinfo.value)
    assert client.calls == 2
    assert sleeps == [pytest.approx(70.0)]  # 10 + 60


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
