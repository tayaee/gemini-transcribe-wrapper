"""Multi-key Gemini API key + round-robin + 429 fallback.

When the user supplies multiple API keys (``--gemini-api-keys=k1,k2,...``),
:class:`stt.TranscribeClient` should:

1. Issue one chunk per key in round-robin order (advancing the pointer
   after every successful chunk, even if the chunk succeeded on the
   first key tried).
2. On a 429-with-hint, apply the existing cooldown+retry once on the
   same key. If the retry also fails, fall through to the next key.
3. On a 429-without-hint, skip the cooldown and try the next key.
4. On exhaustion of all keys, propagate the most recent 429 (still
   triggers ``QuotaExceededError`` at the batch boundary).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt

# --- helpers --------------------------------------------------------------


def _ok_interaction(text: str = "안녕하세요") -> MagicMock:
    mock_step = MagicMock()
    mock_step.type = "model_output"
    mock_content = MagicMock()
    mock_content.type = "text"
    mock_content.annotations = []
    mock_step.content = [mock_content]
    ok = MagicMock()
    ok.steps = [mock_step]
    ok.output_text = text
    return ok


def _retry_msg(seconds: float) -> str:
    return (
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota. "
        f"\\nPlease retry in {seconds}s.', 'code': 'too_many_requests'}}"
    )


class _MultiKeyClient(stt.TranscribeClient):
    """Drive ``interactions.create`` with a per-key scripted side effect list.

    Each key in ``self._api_keys`` gets its own list of side effects
    (raised in order, then OK on overflow). Records the call order so
    tests can assert which key handled which call.
    """

    def __init__(
        self,
        per_key_effects: dict[str, list],
    ) -> None:
        self._per_key_effects = {k: list(v) for k, v in per_key_effects.items()}
        # Insertion order is guaranteed by Python 3.7+ dicts; callers
        # always pass at least one key, so cast str is safe.
        self.api_key = cast(str, next(iter(per_key_effects)))
        self.api_logs: list[dict] = []
        self.request_interval_secs = 0.0
        self.tier = "free"
        self.model = stt.MODEL_ID

        mock_upload = MagicMock()
        mock_upload.uri = "files/test"
        mock_upload.name = "files/test"
        self.client = MagicMock()
        self.client.files.upload = MagicMock(return_value=mock_upload)
        self.client.files.delete = MagicMock()

        self._call_order: list[tuple[str, int]] = []  # (key, attempt#)
        self._calls_per_key: dict[str, int] = {k: 0 for k in per_key_effects}

        def _create(**_kwargs):
            # ``self.api_key`` is str|None on the parent; we always set it
            # to a real key before calling, so cast to str.
            active = cast(str, self.api_key)
            idx = self._calls_per_key[active]
            self._calls_per_key[active] = idx + 1
            self._call_order.append((active, idx + 1))
            effects = self._per_key_effects[active]
            if idx < len(effects):
                fx = effects[idx]
                if isinstance(fx, Exception):
                    raise fx
                return fx
            return _ok_interaction()

        self.client.interactions.create = MagicMock(side_effect=_create)

    def _generation_config(self):  # type: ignore[override]
        return MagicMock()


def _chunk(path: Path, name: str) -> Path:
    p = path / name
    p.write_bytes(b"fake-mp3")
    return p


# --- round-robin selection -------------------------------------------------


def test_round_robin_advances_after_each_successful_chunk(tmp_path, monkeypatch):
    """Three keys, three chunks → k0, k1, k2 in order."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k0, k1, k2 = "k0aaaaaa", "k1bbbbbb", "k2cccccc"
    client = _MultiKeyClient({k0: [], k1: [], k2: []})
    client._api_keys = [k0, k1, k2]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    for i in range(3):
        client.transcribe_chunk(chunk, chunk_index=i)

    keys_used = [k for k, _ in client._call_order]
    assert keys_used == [k0, k1, k2]


def test_round_robin_wraps_around_after_n_keys(tmp_path, monkeypatch):
    """After N successful chunks the pointer wraps to key 0 again."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k0, k1, k2 = "k0aaaaaa", "k1bbbbbb", "k2cccccc"
    client = _MultiKeyClient({k0: [], k1: [], k2: []})
    client._api_keys = [k0, k1, k2]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    for i in range(7):  # 7 chunks over 3 keys
        client.transcribe_chunk(chunk, chunk_index=i)

    keys_used = [k for k, _ in client._call_order]
    assert keys_used == [k0, k1, k2, k0, k1, k2, k0]


# --- 429 fallback: with retry hint ---------------------------------------


def test_429_with_hint_falls_through_to_next_key_on_retry_failure(
    tmp_path, monkeypatch, caplog
):
    """Key A 429s with hint → sleep → retry A still 429s → key B succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    # Key A: two 429s (initial + retry after cooldown). Key B: OK.
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(5.0)), RuntimeError(_retry_msg(5.0))],
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # Sleep happened once (A's cooldown). B succeeded without any sleep.
    assert len(sleeps) == 1
    assert 120.0 < sleeps[0] < 130.0  # 5s hint + 120s safety
    # Call order: A1 → A2 (retry) → B1 (fallback).
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_a[-4:], 2),
        (k_b[-4:], 1),
    ]
    # The "trying next key" log line was emitted for A.
    fallback_logs = [
        rec.message for rec in caplog.records if "trying next key" in rec.message
    ]
    assert any("429 retry via cooldown failed" in m for m in fallback_logs)


def test_429_with_hint_succeeds_on_retry_no_fallback(tmp_path, monkeypatch, caplog):
    """Key A 429s with hint → sleep → retry A succeeds → B is never tried."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(3.0))],  # one 429, then OK
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    assert len(sleeps) == 1
    assert 120.0 < sleeps[0] < 130.0  # 3s hint + 120s safety
    # Only A was tried (1st attempt + 1 retry). B untouched.
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_a[-4:], 2),
    ]


# --- 429 fallback: without retry hint ------------------------------------


def test_429_without_hint_tries_next_key_immediately(
    tmp_path, monkeypatch, caplog
):
    """Key A 429s with no hint → no sleep → key B succeeds right away."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError("429 no hint here")],
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # No cooldown sleep — fall through to next key.
    assert sleeps == []
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]


# --- all keys exhausted ---------------------------------------------------


def test_all_keys_exhausted_propagates_last_429(tmp_path, monkeypatch):
    """Every key 429s → raise the most recent 429."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b, k_c = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb", "AIzaKeyCccccccc"
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(1.0)), RuntimeError(_retry_msg(1.0))],
            k_b: [RuntimeError("429 plain")],
            k_c: [RuntimeError(_retry_msg(2.0)), RuntimeError(_retry_msg(2.0))],
        }
    )
    client._api_keys = [k_a, k_b, k_c]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with pytest.raises(RuntimeError) as excinfo:
        client.transcribe_chunk(chunk, chunk_index=0)

    # The most-recent 429 message is preserved.
    assert "Please retry in 2.0s" in str(excinfo.value)
    # Each key was tried at least once.
    assert {k for k, _ in client._call_order} == {k_a, k_b, k_c}


# --- per-key throttle isolation ------------------------------------------


def test_throttle_is_per_key(tmp_path, monkeypatch):
    """Calling k0 right after k1 must not trigger k0's cooldown.

    The rate limiter tracks per-key completion timestamps on disk
    (under ``$GTW_CACHE_DIR``). Marking only k0 as completed means
    k1's elapsed-since-completion is undefined, so k1 must NOT sleep.
    We force the disk-based lookup by clearing the global
    ``_LAST_API_COMPLETION_MONOTONIC`` so it doesn't shadow the
    per-key file.
    """
    monkeypatch.setenv("GTW_CACHE_DIR", str(tmp_path))
    stt.reset_api_rate_limiter()

    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))

    # Mark only k0 as recently completed.
    stt.record_api_call_completed(api_key="k0aaaaaa")
    # Clear the global monotonic so the disk lookup runs (per-key
    # isolation would otherwise be shadowed by the global).
    stt._LAST_API_COMPLETION_MONOTONIC = None
    stt._LAST_API_COMPLETION_WALL = None

    # k1 has no prior completion → no sleep.
    stt._throttle_api_call(60.0, api_key="k1bbbbbb", tier="free")
    assert sleeps == []

    # k0 has a recent completion → sleep ~60s.
    stt._throttle_api_call(60.0, api_key="k0aaaaaa", tier="free")
    assert len(sleeps) == 1
    assert 59.0 <= sleeps[0] <= 60.0

    stt.reset_api_rate_limiter()


# --- constructor ----------------------------------------------------------


def test_transcribe_client_resolves_env_keys_when_api_keys_omitted(
    tmp_path, monkeypatch
):
    """No ``api_keys=`` arg → env var $GEMINI_API_KEYS (CSV) takes over."""
    monkeypatch.setenv("GEMINI_API_KEYS", "k_env1,k_env2")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    client = stt.TranscribeClient(request_interval_secs=0.0)
    assert client._api_keys == ["k_env1", "k_env2"]


def test_transcribe_client_deduplicates_keys():
    """Duplicate keys (CLI + env) collapse to a single ordered list."""
    client = stt.TranscribeClient(
        api_keys=["k1", "k2", "k1"], request_interval_secs=0.0
    )
    assert client._api_keys == ["k1", "k2"]


# --- compact key masking (used in the multi-key startup log) -----------


def test_api_mask_key_shows_redacted_tag_and_last_4():
    """Compact format: ``[redacted]<last4>`` — no first-4 leak.

    The multi-key startup log line uses this to keep the line short
    even with 9+ keys; the single-key line uses it for consistency.
    """
    from gemini_transcribe_wrapper.api import _mask_key

    masked = _mask_key("AQ.AXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXMw9g")
    assert masked == "[redacted]Mw9g"


def test_api_mask_key_short_key_uses_only_redacted_tag():
    """Keys ≤ 4 chars are fully masked with just the [redacted] tag."""
    from gemini_transcribe_wrapper.api import _mask_key

    assert _mask_key("abcd") == "[redacted]"
    assert _mask_key("k1") == "[redacted]"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))