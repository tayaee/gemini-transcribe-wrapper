"""Per-input-file blacklist (issue-002).

Spec §4.2: A file that produced a non-429 error (400/500/403/etc.) is
skipped for ``ttl_secs`` (default 6h) after the failure. The blacklist
is persisted to disk so it survives process restarts and is keyed by
absolute path. Multiple status codes get separate files
(``http-status-{400,500}.json``) so an operator can clear them
independently.

The check, however, is global across keys — once any key observes a
poison file, all keys skip it for the TTL window. We can revisit if
real-world data shows per-key scoping helps (issue-002 notes).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gemini_transcribe_wrapper.blacklist import InputBlacklist
from gemini_transcribe_wrapper.models import TranscribeStatus

# --- helpers ---------------------------------------------------------------


def _cache(tmp_path: Path, key_tail: str = "key1abcd") -> Path:
    d = tmp_path / "cache" / key_tail
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(tmp_path: Path, name: str = "input.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"fake")
    return p


# --- schema_version + atomic write -----------------------------------------


def test_blacklist_add_writes_atomic_json(tmp_path):
    """``add()`` creates ``http-status-{code}.json`` atomically.

    The file must contain ``schema_version``, ``ttl_secs``, and an
    ``entries`` dict with ``first_blacklisted_at_epoch``,
    ``expires_at_epoch``, and ``status_code``.
    """
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=1_000_000.0)

    target = cache / "http-status-400.json"
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["schema_version"] == 1
    assert data["ttl_secs"] == 3600
    assert str(file.resolve()) in data["entries"]
    entry = data["entries"][str(file.resolve())]
    assert entry["status_code"] == 400
    assert entry["first_blacklisted_at_epoch"] == 1_000_000.0
    assert entry["expires_at_epoch"] == 1_003_600.0


def test_blacklist_distinct_status_codes_distinct_files(tmp_path):
    """400 and 500 live in separate files (semantic separation)."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=1_000_000.0)
    bl.add(status=500, now=1_000_000.0)

    assert (cache / "http-status-400.json").exists()
    assert (cache / "http-status-500.json").exists()


def test_blacklist_atomic_write_no_partial_on_concurrent_read(tmp_path):
    """The JSON file is written via ``tmp + os.replace`` — readers
    never see a half-written file."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)

    # First add creates the file
    bl.add(status=400, now=1_000_000.0)
    # Second add overwrites — must not produce a half-written file
    # in between. We assert by reading the file and confirming it's
    # always valid JSON.
    bl.add(status=400, now=1_000_001.0)
    data = json.loads((cache / "http-status-400.json").read_text())
    # The "first" timestamp is preserved (we don't reset on re-add).
    assert data["entries"][str(file.resolve())]["first_blacklisted_at_epoch"] == 1_000_000.0
    # But the "expires" advances.
    assert data["entries"][str(file.resolve())]["expires_at_epoch"] == 1_003_601.0


# --- is_blacklisted --------------------------------------------------------


def test_blacklist_check_returns_false_when_no_file(tmp_path):
    """No blacklist file → not blacklisted."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    assert bl.is_blacklisted(now=1_000_000.0) is False


def test_blacklist_check_returns_true_within_ttl(tmp_path):
    """Within TTL → blacklisted."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=1_000_000.0)
    assert bl.is_blacklisted(now=1_001_000.0) is True


def test_blacklist_check_returns_false_after_ttl_expires(tmp_path):
    """After TTL → silently re-tried."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=1_000_000.0)
    # TTL was 3600s, so at now=1_003_601 the entry is expired.
    assert bl.is_blacklisted(now=1_003_601.0) is False


def test_blacklist_check_keys_by_absolute_path(tmp_path):
    """A file moved under a different relative path is still recognized."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=1_000_000.0)
    # Construct a new InputBlacklist pointed at the same file via
    # a different relative path.
    bl2 = InputBlacklist(path=tmp_path / "./" / file.name, cache_dir=cache, ttl_secs=3600)
    assert bl2.is_blacklisted(now=1_000_500.0) is True


# --- survives process restart ----------------------------------------------


def test_blacklist_survives_reload_from_disk(tmp_path):
    """A fresh ``InputBlacklist`` instance reads the existing file."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=500, now=1_000_000.0)

    # Simulate process restart by creating a fresh instance.
    bl2 = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    assert bl2.is_blacklisted(now=1_000_500.0) is True


# --- non-429 status codes --------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_blacklist_handles_all_known_error_codes(tmp_path, status):
    """All common HTTP error codes route to their own file."""
    file = _file(tmp_path)
    cache = _cache(tmp_path)
    bl = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    bl.add(status=status, now=1_000_000.0)

    target = cache / f"http-status-{status}.json"
    assert target.exists()
    bl2 = InputBlacklist(path=file, cache_dir=cache, ttl_secs=3600)
    assert bl2.is_blacklisted(now=1_000_500.0) is True


# --- TranscribeStatus enum ------------------------------------------------


def test_blacklisted_status_enum_value_exists():
    """``TranscribeStatus.BLACKLISTED`` exists for the result path."""
    assert TranscribeStatus.BLACKLISTED.value == "blacklisted"


# --- api.py integration (smoke) --------------------------------------------


def test_api_returns_blacklisted_for_blacklisted_file(tmp_path, monkeypatch):
    """When ``InputBlacklist.is_blacklisted`` is True, ``gemini_transcribe``
    returns a result with ``status=BLACKLISTED`` without calling the SDK.
    """
    import time as _time

    from gemini_transcribe_wrapper import api

    monkeypatch.setenv("GTW_CACHE_DIR", str(tmp_path / "cache"))
    src = _file(tmp_path, "input.mp4")
    # Per spec §2, ``api_key_tail`` is the last 8 chars of the API key.
    # Match the directory layout api.py will read at runtime.
    key_tail = "fakeabcd1234"[-8:]  # → "abcd1234"
    cache = tmp_path / "cache" / key_tail
    cache.mkdir(parents=True)
    # Use a recent ``now`` so the entry is actually within TTL when
    # api.py's ``is_blacklisted()`` call checks ``time.time()``.
    now_real = _time.time()
    bl = InputBlacklist(path=src, cache_dir=cache, ttl_secs=3600)
    bl.add(status=400, now=now_real)

    # Track whether the SDK was called.
    called = {"count": 0}
    from gemini_transcribe_wrapper import stt

    orig_init = stt.TranscribeClient.__init__

    def _track(self, *a, **kw):
        called["count"] += 1
        return orig_init(self, *a, **kw)

    monkeypatch.setattr(stt.TranscribeClient, "__init__", _track)

    batch = api.gemini_transcribe(
        str(src),
        force=True,
        gemini_api_keys=["fakeabcd1234"],
    )
    assert len(batch.results) == 1
    assert batch.results[0].status == TranscribeStatus.BLACKLISTED
    # SDK was not invoked for the blacklisted file.
    assert called["count"] == 0
