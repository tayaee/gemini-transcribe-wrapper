"""Pytest configuration and fixtures for unit tests."""

from __future__ import annotations

import pytest

try:
    import static_ffmpeg

    static_ffmpeg.add_paths()
except Exception:  # noqa: BLE001, S110 - best-effort environment init
    pass


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(monkeypatch, tmp_path_factory):
    """Ensure all unit tests use a dummy fake API key and never touch real APIs.

    ``GTW_CONFIG_DIR`` is redirected at an empty temp directory so the
    ``auto`` key-file lookup never finds the developer's real
    ``~/.config/gemini-transcribe-wrapper/gemini-api-keys.txt`` — without
    this, tests that expect a single key would silently pick up every key
    in that file.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_api_key_for_testing_only")
    monkeypatch.setenv(
        "GTW_CONFIG_DIR", str(tmp_path_factory.mktemp("gtw-config-isolated"))
    )
