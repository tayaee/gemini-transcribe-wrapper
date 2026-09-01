"""Multi-key Gemini API key + round-robin + active/cooldown pool.

When the user supplies multiple API keys (``--gemini-api-keys=k1,k2,...``)
and a per-call throttle (``request_interval_secs=120``), a 429
encountered during a chunk is effectively "daily quota exhausted" — the
same key will keep 429ing for the rest of the day.

:class:`stt.TranscribeClient` should:

1. Issue one chunk per key in round-robin order against the
   ``_active_pool`` (advancing the pointer after every successful
   chunk, even if the chunk succeeded on the first key tried).
2. On **any** 429 (with or without a retry hint), immediately
   blacklist the key into ``_cooldown_pool`` and try the next active
   key. No same-key retry, no hint-based cooldown sleep.
3. When the active pool drains (every active key was 429'd during this
   chunk), sleep ``_COOLDOWN_SECS`` and reactivate every cooldown key
   in batch (preserving ``_api_keys`` order), then retry the chunk.
4. Non-quota errors (400/500/etc.) are not absorbed by pool rotation;
   they propagate so callers see the real failure.
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
        # Active/cooldown pool attributes (mirrors
        # ``TranscribeClient.__init__``) so tests don't have to set them
        # manually after the constructor runs.
        self._active_pool: list[str] = list(self._per_key_effects.keys())
        self._cooldown_pool: set[str] = set()
        # ``_cooldown_secs`` is intentionally ``None`` so tests can
        # ``monkeypatch.setattr(stt, "_COOLDOWN_SECS", ...)`` and have
        # the value picked up via the lazy fallback in
        # ``transcribe_chunk``.
        self._cooldown_secs: float | None = None

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


# --- 429 → immediate blacklist (no same-key retry) -----------------------


def test_429_with_hint_blacklists_immediately_no_retry(
    tmp_path, monkeypatch, caplog
):
    """Hint-bearing 429 → blacklist immediately, no same-key retry sleep.

    Old behavior slept ``retry_after + 120s`` and retried the same key.
    New behavior blacklists in one shot and falls through to the next
    active key.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    # Key A: a single 429 (would have triggered retry+cooldown in the
    # old code). Key B: OK.
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(5.0))],
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # Crucially: no sleep at all. Old behavior would have slept ~125s.
    assert sleeps == []
    # Call order: A1 (429) → B1 (success). A is NEVER retried.
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]
    # The "blacklisting" log line was emitted for A.
    bl_logs = [
        rec.message for rec in caplog.records if "blacklisting" in rec.message
    ]
    assert any("...aaaa" in m for m in bl_logs)
    # A moved to cooldown, B is the only active key.
    assert k_a not in client._active_pool
    assert client._active_pool == [k_b]
    assert k_a in client._cooldown_pool


def test_429_with_hint_no_succeeds_via_retry_no_fallback(
    tmp_path, monkeypatch, caplog
):
    """A 429 (with or without hint) never retries on the same key.

    Even though A's next scripted response would have succeeded, the
    blacklist is immediate — B takes the chunk.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(3.0))],  # 429 then would-OK
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    assert sleeps == []
    # A tried exactly once. B took the chunk.
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]


# --- 429 without hint -----------------------------------------------------


def test_429_without_hint_tries_next_key_immediately(
    tmp_path, monkeypatch, caplog
):
    """Key A 429s with no hint → blacklist immediately → key B succeeds.

    Hint and non-hint 429s follow the same blacklist path now (the
    2-minute throttle absorbs short-term rate limits, so any 429 here
    is treated as daily-quota exhaustion).
    """
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
    assert sleeps == []
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]
    assert client._active_pool == [k_b]
    assert client._cooldown_pool == {k_a}


# --- active pool drain → cooldown wait + reactivate ---------------------


def test_active_pool_drain_waits_then_reactivates_and_retries(
    tmp_path, monkeypatch, caplog
):
    """All keys 429 → sleep ``_COOLDOWN_SECS`` → reactivate → retry chunk.

    In the old design this test asserted the wrapper raised after the
    first full sweep. The new design sleeps and retries in a loop, so
    each key gets a single 429 on the first pass and a successful call
    on the reactivated pass.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)
    monkeypatch.setattr(stt, "_COOLDOWN_SECS", 0.05)  # speed up

    k_a, k_b, k_c = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb", "AIzaKeyCccccccc"
    # First call per key 429s; subsequent calls succeed. All three keys
    # 429 on the first pass, so the active pool drains and triggers the
    # cooldown-wait path.
    client = _MultiKeyClient(
        {
            k_a: [RuntimeError(_retry_msg(1.0))],
            k_b: [RuntimeError("429 plain")],
            k_c: [RuntimeError(_retry_msg(2.0))],
        }
    )
    client._api_keys = [k_a, k_b, k_c]
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # First pass: every key 429s once. Second pass (after cooldown
    # wait): first key (a) succeeds immediately, chunk returns.
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
        (k_c[-4:], 1),
        (k_a[-4:], 2),
    ]
    # Cooldown wait fired once.
    assert [s for s in sleeps if s >= 0.04] == [pytest.approx(0.05)]
    # The "Active pool drained" log was emitted.
    assert any(
        "Active pool drained" in rec.message and "Sleeping" in rec.message
        for rec in caplog.records
    )
    # After success, all three keys are back in the active pool.
    assert sorted(client._active_pool) == sorted([k_a, k_b, k_c])
    assert client._cooldown_pool == set()


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