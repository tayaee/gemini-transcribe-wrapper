"""Pytest configuration and fixtures for unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(monkeypatch):
    """Ensure all unit tests use a dummy fake API key and never touch real APIs."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini_api_key_for_testing_only")
