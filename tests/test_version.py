"""Unit tests for 2-version.py decision logic.

Covers the bump-gating rules, especially the corner case where PyPI is
ahead of the latest git tag (a user published without tagging first) —
in that case the "since" reference must fall back to the commit that
introduced the last-released version in pyproject.toml, not the latest
git tag.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "2-version.py"
spec = importlib.util.spec_from_file_location("gtw_version", SPEC_PATH)
assert spec is not None and spec.loader is not None
gtw_version = importlib.util.module_from_spec(spec)
sys.modules["gtw_version"] = gtw_version
spec.loader.exec_module(gtw_version)


def test_pypi_ahead_of_tag_uses_introducer_commit():
    """PyPI=0.0.15 but tag=v0.0.13 — count must use the 0.0.15 introducer."""
    should, msg = gtw_version.decide_bump("0.0.15", "0.0.13", "0.0.15")
    assert should is True
    # The reference must be 0.0.15 (not 0.0.13) and must cite the commit
    # that introduced 0.0.15 so the count excludes already-released work.
    assert "since v0.0.15" in msg
    assert "dfd4c54" in msg  # introducer commit short hash
    assert "since v0.0.13" not in msg


def test_tag_matches_last_released_no_commit_suffix():
    """When the git tag exists for last_released, no commit suffix is added."""
    should, msg = gtw_version.decide_bump("0.0.13", "0.0.13", None)
    assert should is True
    assert "since v0.0.13" in msg
    assert "(commit" not in msg


def test_find_introducer_commit_returns_oldest_match():
    intro = gtw_version.find_version_introducing_commit("0.0.15")
    assert intro is not None
    assert intro.startswith("dfd4c54")  # "Fix incorrect cue handling"


def test_compare_versions_basic():
    assert gtw_version.compare_versions("0.0.15", "0.0.13") == 1
    assert gtw_version.compare_versions("0.0.13", "0.0.15") == -1
    assert gtw_version.compare_versions("1.2.3", "1.2.3") == 0


def test_bump_version_patch():
    assert gtw_version.bump_version("0.0.13", "patch") == "0.0.14"
    assert gtw_version.bump_version("0.0.15", "patch") == "0.0.16"


def test_bump_version_minor():
    assert gtw_version.bump_version("0.0.13", "minor") == "0.1.0"


def test_bump_version_major():
    assert gtw_version.bump_version("0.0.13", "major") == "1.0.0"


def test_local_ahead_of_release_does_not_bump():
    """Local=0.0.16, last released=0.0.15: must NOT bump (already ahead)."""
    should, msg = gtw_version.decide_bump("0.0.16", "0.0.13", "0.0.15")
    assert should is False
    assert "already ahead" in msg.lower() or "ahead of" in msg.lower()


def test_local_behind_release_does_not_bump():
    """Local=0.0.12, last released=0.0.15: must NOT bump (stale)."""
    should, msg = gtw_version.decide_bump("0.0.12", "0.0.13", "0.0.15")
    assert should is False
    assert "BEHIND" in msg or "behind" in msg.lower()


def test_no_release_history_suggests_first_release():
    should, msg = gtw_version.decide_bump("0.1.0", None, None)
    assert should is False
    assert "first release" in msg.lower() or "previous release" in msg.lower()