"""Pytest configuration and fixtures for unit tests."""

from __future__ import annotations

import pytest

try:
    import static_ffmpeg

    static_ffmpeg.add_paths()
except Exception:  # noqa: BLE001, S110 - best-effort environment init
    pass


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(monkeypatch):
    """Ensure all unit tests use a dummy fake API key and never touch real APIs."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_api_key_for_testing_only")
