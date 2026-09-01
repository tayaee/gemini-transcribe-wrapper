"""Per-key 30-minute cooldown behavior (issue-003).

Replaces the old batch-reactivation model (one fixed 600s wait then all
cooldown keys reactivated) with per-key ``cooldown_until`` timestamps.

New invariants:

1. On a 429, the offending key goes into ``_dead_pool`` with
   ``cooldown_until = time.monotonic() + KEY_COOLDOWN_SECS`` (1800s).
2. ``_prune_dead_pool(now)`` moves any key whose ``cooldown_until``
   has elapsed back into ``_live_pool`` (preserving ``_api_keys`` order).
3. When ``_live_pool`` is empty but ``_dead_pool`` isn't, sleep only
   until the soonest key recovers — not the full 1800s.
4. Single-key mode: on 429, the wrapper emits ``SKIPPED_QUOTA`` for
   the rest of the current pass (no abort).
5. Old attribute names ``_active_pool`` / ``_cooldown_pool`` remain
   available via property aliases so existing tests still work.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper import stt
from gemini_transcribe_wrapper.models import TranscribeStatus


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


def _retry_msg(seconds: float = 1.0) -> str:
    return (
        "Error code: 429 - {'error': {'message': 'You exceeded your current quota. "
        f"\\nPlease retry in {seconds}s.', 'code': 'too_many_requests'}}"
    )


class _ScriptedClient(stt.TranscribeClient):
    """Per-key scripted side effects + hooks."""

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
        self._cooldown_secs: float | None = None

        def _create(**_kwargs):
            active = cast(str, self.api_key)
            idx = self._calls_per_key[active]
            self._calls_per_key[active] = idx + 1
            self._call_order.append((active, idx + 1))
            effects = self._per_key_effects[active]
            if idx < len(effects) and isinstance(effects[idx], Exception):
                raise effects[idx]
            return _ok_interaction()

        self.client.interactions.create = MagicMock(side_effect=_create)

    def _generation_config(self):  # type: ignore[override]
        return MagicMock()


def _chunk(path: Path, name: str) -> Path:
    p = path / name
    p.write_bytes(b"fake-mp3")
    return p


# --- KEY_COOLDOWN_SECS module constant --------------------------------


def test_key_cooldown_secs_is_1800_seconds():
    """Spec §4.1: per-key minimum cooldown is 30 minutes (1800s)."""
    assert stt.KEY_COOLDOWN_SECS == 1800.0


def test_legacy_cooldown_secs_constant_still_exists():
    """Backward-compat: the old ``_COOLDOWN_SECS`` name remains importable
    so monkeypatch-based tests keep working."""
    assert hasattr(stt, "_COOLDOWN_SECS")
    # The old constant was 600.0; we keep it as an alias to the new
    # value (1800.0) but the name must still resolve.
    assert isinstance(stt._COOLDOWN_SECS, float)


# --- _live_pool / _dead_pool backing fields ----------------------------


def test_live_pool_initialized_from_api_keys():
    """New canonical attribute ``_live_pool`` mirrors ``_api_keys``."""
    client = stt.TranscribeClient(
        api_keys=["k1aaaaaa", "k2bbbbbb", "k3cccccc"], request_interval_secs=0.0
    )
    assert client._live_pool == ["k1aaaaaa", "k2bbbbbb", "k3cccccc"]
    assert client._dead_pool == {}


def test_legacy_active_pool_and_cooldown_pool_are_property_aliases():
    """``_active_pool`` and ``_cooldown_pool`` remain accessible.

    Existing tests (test_active_cooldown_pool.py) and downstream code
    reference these names; we keep them as property aliases so nothing
    breaks.
    """
    client = stt.TranscribeClient(
        api_keys=["k1aaaaaa", "k2bbbbbb"], request_interval_secs=0.0
    )
    # Read via aliases
    assert client._active_pool == ["k1aaaaaa", "k2bbbbbb"]
    assert client._cooldown_pool == set()
    # Write via aliases (used by existing tests' setup)
    client._active_pool = ["kx", "ky"]
    assert client._live_pool == ["kx", "ky"]
    client._cooldown_pool = {"kx"}
    assert "kx" in client._dead_pool


# --- per-key cooldown_until timestamp on 429 ---------------------------


def test_429_marks_key_dead_with_per_key_cooldown_until(tmp_path, monkeypatch):
    """On a 429, ``_dead_pool[key]`` is set to now + KEY_COOLDOWN_SECS."""
    monkeypatch.setattr(stt.time, "sleep", lambda s: None)
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    fake_now = [1_000_000.0]
    monkeypatch.setattr(stt.time, "monotonic", lambda: fake_now[0])

    k_a = "AIzaKeyAaaaaaaa"
    k_b = "AIzaKeyBbbbbbbbb"
    client = _ScriptedClient(
        {k_a: [RuntimeError(_retry_msg(5.0))], k_b: []}
    )
    client._api_keys = [k_a, k_b]
    client._live_pool = [k_a, k_b]
    client._dead_pool = {}

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # k_a must be in dead_pool with cooldown_until = fake_now + 1800.
    assert k_a in client._dead_pool
    assert client._dead_pool[k_a] == pytest.approx(fake_now[0] + 1800.0)
    # k_b is still live and was the one that succeeded.
    assert k_b in client._live_pool
    assert k_a not in client._live_pool


# --- prune_dead_pool moves expired keys back ---------------------------


def test_prune_dead_pool_moves_recovered_keys_back_to_live():
    """When ``cooldown_until`` has elapsed, the key is auto-reactivated
    and re-enters the live pool in its original ``_api_keys`` order."""
    client = stt.TranscribeClient(
        api_keys=["k0aaaaaa", "k1bbbbbb", "k2cccccc"], request_interval_secs=0.0
    )
    fake_now = [10_000.0]
    # k1 is dead (cooldown ends at 10_010); k2 is dead but already past
    # its cooldown (cooldown_until = 9_000); k0 is dead but its cooldown
    # hasn't elapsed (10_050).
    client._dead_pool = {
        "k0aaaaaa": fake_now[0] + 50.0,
        "k1bbbbbb": fake_now[0] + 10.0,
        "k2cccccc": fake_now[0] - 100.0,
    }
    client._live_pool = []

    client._prune_dead_pool(now=fake_now[0])

    # k2 recovered and is back in live (only one, so order = its slot).
    assert "k2cccccc" in client._live_pool
    assert client._dead_pool == {
        "k0aaaaaa": fake_now[0] + 50.0,
        "k1bbbbbb": fake_now[0] + 10.0,
    }


def test_prune_dead_pool_preserves_api_keys_order():
    """When multiple keys recover at the same time, they enter live in
    their original ``_api_keys`` order (round-robin fairness)."""
    client = stt.TranscribeClient(
        api_keys=["k0aaaaaa", "k1bbbbbb", "k2cccccc"], request_interval_secs=0.0
    )
    fake_now = [10_000.0]
    # All three are dead but past cooldown.
    client._dead_pool = {
        "k0aaaaaa": fake_now[0] - 1.0,
        "k1bbbbbb": fake_now[0] - 2.0,
        "k2cccccc": fake_now[0] - 3.0,
    }
    client._live_pool = []

    client._prune_dead_pool(now=fake_now[0])

    assert client._live_pool == ["k0aaaaaa", "k1bbbbbb", "k2cccccc"]
    assert client._dead_pool == {}


def test_prune_dead_pool_no_op_when_all_still_dead():
    client = stt.TranscribeClient(
        api_keys=["k0aaaaaa", "k1bbbbbb"], request_interval_secs=0.0
    )
    fake_now = [10_000.0]
    client._dead_pool = {
        "k0aaaaaa": fake_now[0] + 100.0,
        "k1bbbbbb": fake_now[0] + 200.0,
    }
    client._live_pool = []

    client._prune_dead_pool(now=fake_now[0])

    assert client._live_pool == []
    assert set(client._dead_pool.keys()) == {"k0aaaaaa", "k1bbbbbb"}


# --- sleep until soonest recovery (not full 1800s) --------------------


def test_sleeps_only_until_soonest_recovery_when_live_empty(
    tmp_path, monkeypatch
):
    """When live is empty but dead isn't, sleep ``soonest - now`` not 1800s."""
    sleeps: list[float] = []
    fake_now = [20_000.0]

    def _sleep(s: float) -> None:
        sleeps.append(s)
        # Advance the monotonic clock by the sleep duration so the
        # next ``_prune_dead_pool`` at the top of the outer loop
        # actually sees the recovery time has elapsed.
        fake_now[0] += s

    monkeypatch.setattr(stt.time, "sleep", _sleep)
    monkeypatch.setattr(stt.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k_a = "AIzaKeyAaaaaaaa"
    k_b = "AIzaKeyBbbbbbbbb"
    # k_a recovers soon (dead_until = 20_010); k_b recovers later (20_030).
    client = _ScriptedClient({k_a: [], k_b: []})
    client._api_keys = [k_a, k_b]
    client._live_pool = []
    client._dead_pool = {
        k_a: fake_now[0] + 10.0,
        k_b: fake_now[0] + 30.0,
    }
    # Patch so call #1 raises, then OK (so after prune + retry, k_a succeeds).
    calls: dict[str, int] = {k_a: 0, k_b: 0}

    def _scripted_create(**_kwargs):
        active = cast(str, client.api_key)
        calls[active] += 1
        client._call_order.append((active, calls[active]))
        return _ok_interaction()

    client.client.interactions.create = MagicMock(side_effect=_scripted_create)

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    result = client.transcribe_chunk(chunk, chunk_index=0)

    assert result.text == "안녕하세요"
    # Sleep duration must equal soonest - now (10s), not 30s and not 1800s.
    assert sleeps == [pytest.approx(10.0)]


# --- single-key skip-on-quota status ----------------------------------


def test_skipped_quota_status_enum_exists():
    """Single-key 429 path returns ``SKIPPED_QUOTA`` (issue-003 §3).

    The wiring is exercised in test_no_sdk_retry.py / api.py integration
    tests; here we only assert the enum value exists and is the right
    string so downstream serializers stay stable.
    """
    assert TranscribeStatus.SKIPPED_QUOTA.value == "skipped_quota"


def test_transcribe_chunk_raises_quota_when_single_key_pool_drains(
    tmp_path, monkeypatch
):
    """Single-key 429 → ``transcribe_chunk`` raises the quota exception
    so ``_process_one`` can map it to ``SKIPPED_QUOTA`` (issue-003 §4)."""
    monkeypatch.setattr(stt.time, "sleep", lambda s: None)
    monkeypatch.setattr(stt, "_throttle_api_call", lambda *a, **k: None)

    k = "AIzaOnlyOneKeyy"
    client = _ScriptedClient({k: [RuntimeError(_retry_msg(1.0))] * 5})
    client._api_keys = [k]
    client._live_pool = [k]
    client._dead_pool = {}

    chunk = _chunk(tmp_path, "chunk_000.mp3")
    with pytest.raises(RuntimeError, match="429"):
        client.transcribe_chunk(chunk, chunk_index=0)

    # The single key is now dead with the full 30-min cooldown.
    assert k in client._dead_pool
    assert client._dead_pool[k] > 0.0  # populated


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
