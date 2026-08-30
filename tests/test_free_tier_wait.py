"""Test --free-tier-wait-on-429: PST-midnight wait helpers and STT integration."""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt, usage_counter
from gemini_transcribe_wrapper.usage_counter import (
    FREE_TIER_DAILY_LIMIT,
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

    In production, ``sleep_until_pst_midnight`` is called from inside an
    ``except`` block handling the SDK's quota exception. A raw
    ``KeyboardInterrupt`` raised during the sleep would carry that SDK
    exception in ``__context__`` and Python would print the full chain at
    exit. Re-raising with ``from None`` sets ``__suppress_context__=True``
    so only a clean ``KeyboardInterrupt`` propagates.
    """
    base = datetime(2026, 8, 28, 20, 0, 0, tzinfo=PST)  # 4h to midnight
    monkeypatch.setattr(usage_counter, "pst_now", lambda: base)

    def fake_sleep(_secs: float) -> None:
        raise KeyboardInterrupt

    # Stand in for the SDK's quota exception so the auto-chain has somewhere
    # to attach if our fix regresses.
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
    # The fix: __suppress_context__ True means Python won't print the
    # __context__ chain (the SDK exception) when the KeyboardInterrupt
    # reaches the top of the program.
    assert captured.__suppress_context__ is True


# --- STT integration: quota pre-check + 429 retry ---------------------------


@pytest.fixture
def cache(tmp_path):
    os.environ["GTW_CACHE_DIR"] = str(tmp_path)
    yield tmp_path
    os.environ.pop("GTW_CACHE_DIR", None)


class _FakeResult:
    def __init__(self, text="테스트", words=None):
        from gemini_transcribe_wrapper.stt import Word

        self.text = text
        self.words = words or [Word(text, 0.0, 1.0, "spk_1")]


class _FlakyClient:
    """Fails with a 429 on the first call, succeeds on the second."""

    def __init__(self, *args, **kwargs):
        self.api_logs: list[dict] = []
        self.calls = 0
        self.free_tier_wait_on_429 = kwargs.get("free_tier_wait_on_429", False)

    def transcribe_chunk(self, chunk_mp3, chunk_index=0):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 quota exceeded (free_tier daily)")
        return _FakeResult()


def _make_chunk(cache: Path, name: str) -> Path:
    p = cache / name
    p.write_bytes(b"fake-mp3")
    return p


def test_transcribe_chunks_sequential_retries_on_429_when_flag_set(monkeypatch, cache):
    """On 429 with --free-tier-wait-on-429, the chunk is retried after the wait."""
    sleeps: list[float] = []

    def fake_sleep_until_pst_midnight():
        sleeps.append(1.0)

    # Patch the *source* module (usage_counter) — the function is imported
    # into stt by name, so patching the local name in stt wouldn't help.
    monkeypatch.setattr(usage_counter, "sleep_until_pst_midnight", fake_sleep_until_pst_midnight)

    client = _FlakyClient(free_tier_wait_on_429=True)
    chunks = [_make_chunk(cache, "chunk_000.mp3")]
    results = stt.transcribe_chunks_sequential(
        client, chunks, request_interval_secs=0.0, free_tier_wait_on_429=True  # type: ignore[arg-type]
    )

    assert client.calls == 2  # first 429, then retry succeeds
    assert len(results) == 1
    assert sleeps == [1.0]  # wait was triggered exactly once


def test_transcribe_chunks_sequential_does_not_retry_on_429_when_flag_off(monkeypatch, cache):
    """Without the flag, a 429 propagates immediately to the caller."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        usage_counter, "sleep_until_pst_midnight", lambda: sleeps.append(1.0)
    )

    client = _FlakyClient(free_tier_wait_on_429=False)
    chunks = [_make_chunk(cache, "chunk_000.mp3")]
    with pytest.raises(RuntimeError, match="429"):
        stt.transcribe_chunks_sequential(
            client, chunks, request_interval_secs=0.0, free_tier_wait_on_429=False  # type: ignore[arg-type]
        )
    assert client.calls == 1
    assert sleeps == []  # wait was never triggered


def test_transcribe_chunks_sequential_waits_when_quota_already_at_limit(monkeypatch, cache):
    """When today's PST count >= 25, sleep before the first chunk."""
    # Pre-fill the quota to the limit.
    for _ in range(FREE_TIER_DAILY_LIMIT):
        usage_counter.increment_today(cache)
    assert usage_counter.count_today(cache) == FREE_TIER_DAILY_LIMIT

    sleeps: list[float] = []
    monkeypatch.setattr(
        usage_counter, "sleep_until_pst_midnight", lambda: sleeps.append(1.0)
    )

    # Client returns success on first try.
    class OkClient:
        def __init__(self, *args, **kwargs):
            self.api_logs: list[dict] = []
            self.calls = 0

        def transcribe_chunk(self, chunk_mp3, chunk_index=0):
            self.calls += 1
            return _FakeResult()

    client = OkClient()
    chunks = [_make_chunk(cache, "chunk_000.mp3")]
    stt.transcribe_chunks_sequential(
        client, chunks, request_interval_secs=0.0, free_tier_wait_on_429=True  # type: ignore[arg-type]
    )

    assert client.calls == 1
    assert sleeps == [1.0]  # wait triggered once before the chunk


def test_transcribe_chunks_sequential_no_wait_when_quota_below_limit_and_no_429(monkeypatch, cache):
    """With a fresh quota and no 429, no wait should ever fire."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        usage_counter, "sleep_until_pst_midnight", lambda: sleeps.append(1.0)
    )

    class OkClient:
        def __init__(self, *args, **kwargs):
            self.api_logs: list[dict] = []
            self.calls = 0

        def transcribe_chunk(self, chunk_mp3, chunk_index=0):
            self.calls += 1
            return _FakeResult()

    client = OkClient()
    chunks = [_make_chunk(cache, f"chunk_{i:03d}.mp3") for i in range(3)]
    stt.transcribe_chunks_sequential(
        client, chunks, request_interval_secs=0.0, free_tier_wait_on_429=True  # type: ignore[arg-type]
    )

    assert client.calls == 3
    assert sleeps == []


# --- CLI flag parsing -------------------------------------------------------


def test_cli_flag_parses_and_defaults_to_false():
    from gemini_transcribe_wrapper.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["input.mp4"])
    assert args.free_tier_wait_on_429 is False

    args = parser.parse_args(["--free-tier-wait-on-429", "input.mp4"])
    assert args.free_tier_wait_on_429 is True


# --- End-to-end through gemini_transcribe() ---------------------------------


def test_gemini_transcribe_threads_flag_into_client(monkeypatch, cache):
    """The free_tier_wait_on_429 flag must reach TranscribeClient and the orchestrator."""
    captured: dict = {}

    class CapturingClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        def transcribe_chunk(self, chunk_mp3, chunk_index=0):
            return _FakeResult()

    def fake_sequential(client, chunks, **kwargs):
        captured["sequential_kwargs"] = kwargs
        return [_FakeResult() for _ in chunks]

    # Patch on the *importing* module — api.py binds its own references to
    # `TranscribeClient` and `transcribe_chunks_sequential` at import time.
    from gemini_transcribe_wrapper import api as _api

    monkeypatch.setattr(_api, "transcribe_chunks_sequential", fake_sequential)
    monkeypatch.setattr(_api, "TranscribeClient", CapturingClient)

    import subprocess

    with __import__("tempfile").TemporaryDirectory() as td:
        td = Path(td)
        src = td / "input.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(src),
            ],
            capture_output=True,
            check=True,
        )

        _api.gemini_transcribe(
            str(src), force=True, gemini_api_key="fake", free_tier_wait_on_429=True
        )

    assert captured["client_kwargs"].get("free_tier_wait_on_429") is True
    assert captured["sequential_kwargs"].get("free_tier_wait_on_429") is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
