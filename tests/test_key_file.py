"""Tests for ``--gemini-api-keys-file`` (file-backed key list + hot reload)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gemini_transcribe_wrapper._key_file import (
    KeyFileError,
    check_key_file_permissions,
    key_file_signature,
    load_keys_from_file,
    resolve_key_file,
)
from gemini_transcribe_wrapper.cli import _resolve_api_keys


def _write_key_file(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_load_keys_skips_blanks_comments_and_dupes(tmp_path: Path) -> None:
    f = _write_key_file(
        tmp_path / "keys.txt",
        "# comment\n\nkey-a\n  key-b  \nkey-a\n\n# trailing\n",
    )
    assert load_keys_from_file(f) == ["key-a", "key-b"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_loose_permissions_abort_with_chmod_hint(tmp_path: Path) -> None:
    f = tmp_path / "keys.txt"
    f.write_text("key-a\n", encoding="utf-8")
    f.chmod(0o644)
    with pytest.raises(KeyFileError) as excinfo:
        check_key_file_permissions(f)
    assert f"chmod 600 {f}" in str(excinfo.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_mode_600_passes(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "key-a\n")
    check_key_file_permissions(f)  # must not raise


def test_resolve_off_and_missing_auto(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_key_file("off") is None
    assert resolve_key_file("auto") is None  # no gemini-api-keys.txt present


def test_resolve_auto_picks_up_default_filename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_key_file(tmp_path / "gemini-api-keys.txt", "key-a\n")
    assert resolve_key_file("auto") == tmp_path / "gemini-api-keys.txt"


def test_resolve_explicit_missing_path_errors(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError):
        resolve_key_file(str(tmp_path / "nope.txt"))


def test_cli_keys_come_before_file_keys(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "file-1\nfile-2\ncli-1\n")
    assert _resolve_api_keys(["cli-1", "cli-2"], f) == [
        "cli-1",
        "cli-2",
        "file-1",
        "file-2",
    ]


def test_file_keys_suppress_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "env-1;env-2")
    f = _write_key_file(tmp_path / "keys.txt", "file-1\n")
    assert _resolve_api_keys([], f) == ["file-1"]


def test_signature_changes_with_content(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "key-a\n")
    before = key_file_signature(f)
    _write_key_file(f, "key-a\nkey-b\n")
    assert key_file_signature(f) != before


class _Client:
    """Minimal stand-in exercising just the reload path of TranscribeClient."""

    def __init__(self, keys: list[str], path: Path) -> None:
        from gemini_transcribe_wrapper.stt import TranscribeClient

        self.inner = TranscribeClient.__new__(TranscribeClient)
        self.inner._api_keys = list(keys)
        self.inner._live_pool = list(keys)
        self.inner._dead_pool = {}
        self.inner._clients = {}
        self.inner._rr_index = 0
        self.inner.api_key = keys[0] if keys else None
        self.inner._api_keys_file = path
        self.inner._api_keys_file_sig = key_file_signature(path)


def test_reload_resumes_after_last_used_key_in_file_order(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "k1\nk2\nk3\n")
    c = _Client(["k1", "k2", "k3"], f).inner
    c.api_key = "k2"  # last used

    # Unchanged file → no reload.
    assert c._maybe_reload_key_file() is False

    # Reorder + add a key.
    _write_key_file(f, "k3\nk2\nk4\n")
    assert c._maybe_reload_key_file() is True
    assert c._api_keys == ["k3", "k2", "k4"]
    # Last used k2 is at index 1 in the new order → next is k4.
    assert c.api_key == "k4"


def test_reload_restarts_when_last_used_key_removed(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "k1\nk2\n")
    c = _Client(["k1", "k2"], f).inner
    c.api_key = "k1"

    _write_key_file(f, "k7\nk8\n")
    assert c._maybe_reload_key_file() is True
    assert c._api_keys == ["k7", "k8"]
    assert c.api_key == "k7"


def test_reload_ignores_empty_file(tmp_path: Path) -> None:
    f = _write_key_file(tmp_path / "keys.txt", "k1\nk2\n")
    c = _Client(["k1", "k2"], f).inner

    _write_key_file(f, "# all keys removed\n")
    assert c._maybe_reload_key_file() is False
    assert c._api_keys == ["k1", "k2"]


def test_reload_drops_cached_clients_and_cooldowns_for_removed_keys(
    tmp_path: Path,
) -> None:
    import time

    f = _write_key_file(tmp_path / "keys.txt", "k1\nk2\n")
    c = _Client(["k1", "k2"], f).inner
    c._clients = {"k1": object(), "k2": object()}
    c._dead_pool = {"k1": time.monotonic() + 999}
    c._live_pool = ["k2"]

    _write_key_file(f, "k2\nk3\n")
    assert c._maybe_reload_key_file() is True
    assert "k1" not in c._clients
    assert c._dead_pool == {}
    assert c._live_pool == ["k2", "k3"]
