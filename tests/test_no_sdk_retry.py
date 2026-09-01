"""Tests for the SDK-retry-disabling HttpOptions on ``genai.Client``.

The google-genai SDK defaults to ~6 retries with exponential backoff,
which collides with Gemini's per-minute RPM limits on the free tier:
when our wrapper already handles 429s via per-key blacklist + cooldown,
the SDK's hidden retry loop just burns through the daily quota on a
single key before our wrapper ever gets a chance to rotate. These
tests pin down that every ``genai.Client`` constructed in the wrapper
carries ``HttpOptions(retry_options={'attempts': 1})``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt


def _attempts_from_kwargs(kwargs: dict) -> int | None:
    """Return the ``attempts`` value passed via ``http_options`` kwargs."""
    http_options = kwargs.get("http_options")
    if http_options is None:
        return None
    retry = getattr(http_options, "retry_options", None)
    if retry is None:
        return None
    return getattr(retry, "attempts", None)


def test_module_level_http_options_disables_sdk_retry():
    """The module-level HttpOptions constant must clamp attempts to 1."""
    opts = stt._NO_RETRY_HTTP_OPTIONS
    assert opts is not None
    retry = opts.retry_options
    assert retry is not None
    assert retry.attempts == 1


def test_initial_client_construction_uses_no_retry_options(monkeypatch):
    """The bootstrap client in ``__init__`` must use ``_NO_RETRY_HTTP_OPTIONS``."""
    captured: dict = {}
    real_client_cls = stt.genai.Client

    def fake_client(*args, **kwargs):
        captured["kwargs"] = kwargs
        return real_client_cls.__new__(real_client_cls)

    monkeypatch.setattr(stt.genai, "Client", fake_client)
    stt.TranscribeClient(
        api_key="test_key_aaaaaaaa",
        request_interval_secs=0.0,
    )

    kwargs = captured["kwargs"]
    assert kwargs.get("http_options") is stt._NO_RETRY_HTTP_OPTIONS
    # attempts=1 means the SDK will not retry on 429 / 5xx — the wrapper's
    # own blacklist + next-key rotation drives recovery instead.
    assert _attempts_from_kwargs(kwargs) == 1


def test_client_for_caches_with_no_retry_per_key(monkeypatch):
    """``_client_for`` must hand each per-key client the no-retry options."""
    captured: list[dict] = []
    real_client_cls = stt.genai.Client

    def fake_client(*args, **kwargs):
        captured.append(kwargs)
        return real_client_cls.__new__(real_client_cls)

    monkeypatch.setattr(stt.genai, "Client", fake_client)

    client = stt.TranscribeClient(
        api_keys=["key_a_aaaaaa", "key_b_bbbbbb"],
        request_interval_secs=0.0,
    )
    # Bootstrap capture includes the constructor's own Client call; reset.
    captured.clear()
    # Force the multi-key cache path (not the legacy single-client path).
    client._clients = {}
    client._client_for("key_a_aaaaaa")
    client._client_for("key_b_bbbbbb")

    assert len(captured) == 2
    for kwargs in captured:
        assert kwargs.get("http_options") is stt._NO_RETRY_HTTP_OPTIONS
        assert _attempts_from_kwargs(kwargs) == 1

