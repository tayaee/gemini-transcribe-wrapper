"""Active / cooldown pool behavior for :class:`stt.TranscribeClient`.

When multiple Gemini API keys are supplied and the user already enforces
a 2-minute throttle between calls (``request_interval_secs=120``), a 429
encountered in the chunk loop is effectively "daily quota exhausted" —
the same key will keep 429ing for the rest of the day.

Behavior under test (vs. the old per-key cooldown+retry path):

1. ``_active_pool`` is initialized from ``_api_keys``; ``_cooldown_pool``
   starts empty.
2. A 429 in the chunk loop **immediately** blacklists the key into the
   cooldown pool — no same-key retry, no hint parsing, no sleep.
3. The chunk loop walks the active pool in round-robin order; blacklisted
   keys are not revisited during the same chunk.
4. After a successful chunk, ``_rr_index`` advances past the success key
   (still inside the active pool) so the next chunk keeps spreading load.
5. When the active pool drains (every active key was 429'd during this
   chunk), the wrapper sleeps ``_COOLDOWN_SECS`` and reactivates the
   cooldown pool in batch (preserving ``_api_keys`` order), then retries
   the same chunk from the start.
6. Non-quota errors (400/500/etc.) are **not** absorbed by pool rotation;
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


class _ScriptedClient(stt.TranscribeClient):
    """Per-key scripted side effects, plus iteration/breaker hooks for tests."""

    def __init__(self, per_key_effects: dict[str, list]) -> None:
        self._per_key_effects = {k: list(v) for k, v in per_key_effects.items()}
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

        self._call_order: list[tuple[str, int]] = []
        self._calls_per_key: dict[str, int] = {k: 0 for k in per_key_effects}
        # ``_cooldown_secs`` is intentionally ``None`` so tests can
        # ``monkeypatch.setattr(stt, "_COOLDOWN_SECS", ...)`` and have
        # the value picked up via the lazy fallback in
        # ``transcribe_chunk``.
        self._cooldown_secs: float | None = None

        def _create(**_kwargs):
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


# --- pool initialization --------------------------------------------------


def test_active_pool_initialized_from_api_keys():
    """After ``__init__``, all configured keys are in ``_active_pool`` and
    ``_cooldown_pool`` is empty."""
    client = stt.TranscribeClient(
        api_keys=["k1aaaaaa", "k2bbbbbb", "k3cccccc"], request_interval_secs=0.0
    )
    assert client._active_pool == ["k1aaaaaa", "k2bbbbbb", "k3cccccc"]
    assert client._cooldown_pool == set()
    assert client._rr_index == 0


# --- 429 → immediate blacklist (no same-key retry) -----------------------


def test_429_blacklists_key_immediately_without_retry(tmp_path, monkeypatch):
    """Hint-bearing 429 must NOT trigger a same-key cooldown+retry.

    Old behavior slept ``retry_after + 120s`` and retried the same key.
    New behavior blacklists the key in one shot and moves on.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _ScriptedClient(
        {
            k_a: [RuntimeError(_retry_msg(5.0))],  # just one 429
            k_b: [],  # always succeeds
        }
    )
    client._api_keys = [k_a, k_b]
    client._active_pool = [k_a, k_b]
    client._cooldown_pool = set()
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # Crucially: no sleep. Old behavior would sleep ~125s on k_a's retry.
    assert sleeps == []
    # k_a tried exactly once. k_b picks up the chunk.
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]
    # k_a moved to cooldown, k_b still active.
    assert client._active_pool == [k_b]
    assert client._cooldown_pool == {k_a}


def test_blacklisted_key_skipped_on_subsequent_chunks(tmp_path, monkeypatch):
    """After k_a 429s in chunk 0, chunk 1 starts from k_b (k_a not tried)."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _ScriptedClient(
        {
            k_a: [RuntimeError(_retry_msg(1.0))],  # first call 429s
            k_b: [],  # always succeeds
        }
    )
    client._api_keys = [k_a, k_b]
    client._active_pool = [k_a, k_b]
    client._cooldown_pool = set()
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    # Chunk 0: k_a 429 → blacklist → k_b succeeds.
    client.transcribe_chunk(chunk, chunk_index=0)
    # Chunk 1: starts from k_b (active pool rotated), k_a NOT retried.
    client.transcribe_chunk(chunk, chunk_index=1)

    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
        (k_b[-4:], 2),  # chunk 1 uses k_b again (only active key)
    ]
    assert client._active_pool == [k_b]
    assert client._cooldown_pool == {k_a}


# --- partial drain: some 429, some OK ------------------------------------


def test_partial_429_keeps_remaining_keys_active(tmp_path, monkeypatch):
    """A 429 on k_a does not put k_b or k_c into cooldown."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b, k_c = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb", "AIzaKeyCccccccc"
    client = _ScriptedClient(
        {
            k_a: [RuntimeError(_retry_msg(7.0))],
            k_b: [],
            k_c: [],
        }
    )
    client._api_keys = [k_a, k_b, k_c]
    client._active_pool = [k_a, k_b, k_c]
    client._cooldown_pool = set()
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    assert sleeps == []  # no same-key retry sleep
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
        (k_b[-4:], 1),
    ]
    # k_a blacklisted; k_b and k_c remain active.
    assert client._active_pool == [k_b, k_c]
    assert client._cooldown_pool == {k_a}


# --- non-quota error propagation -----------------------------------------


def test_non_quota_error_propagates_without_touching_other_keys(
    tmp_path, monkeypatch
):
    """A 500 (or any non-quota error) is not absorbed by pool rotation."""
    sleeps: list[float] = []
    monkeypatch.setattr(stt.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _ScriptedClient(
        {
            k_a: [RuntimeError("500 internal server error")],
            k_b: [],
        }
    )
    client._api_keys = [k_a, k_b]
    client._active_pool = [k_a, k_b]
    client._cooldown_pool = set()
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with pytest.raises(RuntimeError, match="500"):
        client.transcribe_chunk(chunk, chunk_index=0)

    # k_a failed, k_b never tried (non-quota means rotation won't help).
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k_a[-4:], 1),
    ]
    # Pool state unchanged: k_a was NOT blacklisted (non-quota).
    assert client._active_pool == [k_a, k_b]
    assert client._cooldown_pool == set()


# --- active pool drain → cooldown wait + reactivate ----------------------


def test_active_pool_drain_triggers_cooldown_wait_and_reactivation(
    tmp_path, monkeypatch, caplog
):
    """All keys 429 → sleep until soonest recovery → reactivate → retry.

    New per-key cooldown model (issue-003): keys enter ``_dead_pool``
    with ``cooldown_until = now + KEY_COOLDOWN_SECS``. The outer loop
    sleeps only until the soonest key recovers (not the full cooldown),
    so when all keys 429 we sleep once and all keys come back together.

    We monkeypatch ``KEY_COOLDOWN_SECS`` to a tiny value and advance
    the mocked monotonic clock on every ``time.sleep`` so the
    ``_prune_dead_pool`` call at the top of the outer loop actually
    sees the recovery time has elapsed.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(stt, "KEY_COOLDOWN_SECS", 0.05)  # speed up
    fake_now = [10_000.0]

    def _sleep(s: float) -> None:
        sleeps.append(s)
        fake_now[0] += s

    monkeypatch.setattr(stt.time, "sleep", _sleep)
    monkeypatch.setattr(stt.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a, k_b = "AIzaKeyAaaaaaaa", "AIzaKeyBbbbbbbbb"
    client = _ScriptedClient({k_a: [], k_b: []})
    client._api_keys = [k_a, k_b]
    client._active_pool = [k_a, k_b]
    client._dead_pool = {}
    client._rr_index = 0

    calls: dict[str, int] = {k_a: 0, k_b: 0}

    def _scripted_create(**_kwargs):
        active = cast(str, client.api_key)
        calls[active] += 1
        client._call_order.append((active, calls[active]))
        if calls[active] == 1:
            raise RuntimeError(_retry_msg(1.0))
        return _ok_interaction()

    client.client.interactions.create = MagicMock(side_effect=_scripted_create)

    chunk = _chunk(tmp_path, "chunk_000.mp3")

    with caplog.at_level(logging.INFO):
        result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # First iteration: k_a 429 → dead → k_b 429 → dead → live pool empty.
    # Outer loop sleeps ~0.05s. Second iteration: both keys recovered,
    # k_a succeeds on call #2 so k_b is not retried in this chunk.
    assert calls[k_a] == 2
    assert calls[k_b] == 1
    # Cooldown sleep fired once (~0.05s — soonest recovery, not full 1800s).
    assert [s for s in sleeps if s > 0.0] == [pytest.approx(0.05, abs=1e-9)]
    # The cooldown wait log was emitted (new wording: "Live pool empty").
    assert any(
        "All keys in the live api key pool ran into error 429" in rec.message
        and "sleeping" in rec.message.lower()
        for rec in caplog.records
    )
    # Final pool state: both keys back in live (recovery succeeded).
    assert client._live_pool == [k_a, k_b]
    assert client._dead_pool == {}


def test_active_pool_reactivation_preserves_api_keys_order(
    tmp_path, monkeypatch
):
    """Reactivation preserves the original ``_api_keys`` ordering for
    round-robin fairness, even when the cooldown set's iteration order
    would be different.

    Updated for the per-key cooldown model (issue-003): ``_prune_dead_pool``
    uses ``_api_keys`` order to repopulate ``_live_pool``, so the second
    iteration's round-robin starts from the same position as the first.
    """
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)
    monkeypatch.setattr(stt, "KEY_COOLDOWN_SECS", 0.0)
    sleeps: list[float] = []
    fake_now = [10_000.0]

    def _sleep(s: float) -> None:
        sleeps.append(s)
        fake_now[0] += s

    monkeypatch.setattr(stt.time, "sleep", _sleep)
    monkeypatch.setattr(stt.time, "monotonic", lambda: fake_now[0])

    k0, k1, k2 = "AIzaKey0aaaaaa", "AIzaKey1bbbbbb", "AIzaKey2cccccc"
    # All 429 once, then OK.
    calls: dict[str, int] = {k0: 0, k1: 0, k2: 0}

    def _scripted_create(**_kwargs):
        active = cast(str, client.api_key)
        calls[active] += 1
        client._call_order.append((active, calls[active]))
        if calls[active] == 1:
            raise RuntimeError(_retry_msg(1.0))
        return _ok_interaction()

    client = _ScriptedClient({k0: [], k1: [], k2: []})
    client.client.interactions.create = MagicMock(side_effect=_scripted_create)
    client._api_keys = [k0, k1, k2]
    client._live_pool = [k0, k1, k2]
    client._dead_pool = {}
    client._rr_index = 0

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # First iteration: k0 429 → dead → k1 429 → dead → k2 429 → dead → pool empty.
    # Prune moves them back into live in ``_api_keys`` order [k0, k1, k2].
    # Second iteration: k0 succeeds (call #2 returns OK).
    # So call order: k0, k1, k2, k0 (chunk succeeds on second iteration's k0).
    assert [(k[-4:], n) for k, n in client._call_order] == [
        (k0[-4:], 1),
        (k1[-4:], 1),
        (k2[-4:], 1),
        (k0[-4:], 2),
    ]
    # After success, _rr_index advances to position 1 in live pool.
    assert client._rr_index == 1


# --- per-instance cooldown_secs override ---------------------------------


def test_cooldown_secs_kwarg_overrides_module_default():
    """``TranscribeClient(cooldown_secs=...)`` lets callers override the
    module-level ``_COOLDOWN_SECS`` constant without monkeypatching.

    This is the escape hatch used by the audit-log test to skip the
    600s wait when a single-key client hits the pool-drain path.
    """
    client = stt.TranscribeClient(
        api_keys=["k1aaaaaa", "k2bbbbbb"],
        cooldown_secs=12.5,
    )
    assert client._cooldown_secs == 12.5


def test_cooldown_secs_kwarg_none_falls_back_to_module_default():
    """``cooldown_secs=None`` (the default) keeps the module-level constant
    so ``monkeypatch.setattr(stt, "_COOLDOWN_SECS", ...)`` still works
    in tests that bypass the constructor.
    """
    client = stt.TranscribeClient(api_keys=["k1aaaaaa"])
    assert client._cooldown_secs is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))