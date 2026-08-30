# /// script
# requires-python = ">=3.10"
# ///
"""Display or bump the ``version`` field in pyproject.toml.

The bump is gated by the current release state so that running
``bump`` repeatedly with no intervening code change is a no-op:

  bump fires only when local version == max(latest git tag, latest
  PyPI release) AND there are commits since the last released version.

The "since" reference prefers the matching git tag, but falls back to
the commit that introduced the last released version in pyproject.toml
when no tag exists (e.g. the user published to PyPI before tagging on
GitHub). This keeps the displayed commit count accurate regardless of
whether tagging was done.

In every other state the script prints an informative hint instead of
bumping. This makes the script safe to chain into release automation
("just call bump, and the version will advance iff there's a release
to ship").

Usage (via uv so PEP 723 metadata is honored):
    uv run 2-version.py                 # print the current version
    uv run 2-version.py bump [level]    # bump if pending changes exist
                                        # (level: major|minor|patch, default: patch)

The companion ``2-version.sh`` / ``2-version.bat`` wrappers invoke
``uv run`` so users can just run ``./2-version.sh [bump [level]]``.
"""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent / "pyproject.toml"
TAG_PREFIX = "v"
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
NAME_RE = re.compile(r'^name\s*=\s*"([^"]+)"', re.MULTILINE)
VALID_LEVELS = ("major", "minor", "patch")


def parse_version(text):
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit("Error: could not find version in pyproject.toml")
    return match.group(1), match


def bump_version(version, level):
    parts = version.split(".")
    if len(parts) != 3:
        raise SystemExit(
            f"Error: expected semver MAJOR.MINOR.PATCH, got '{version}'"
        )
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError as exc:
        raise SystemExit(
            f"Error: non-integer component in version '{version}': {exc}"
        )
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def compare_versions(a, b):
    """Return -1 / 0 / 1 for a vs b in semver order."""
    pa = tuple(int(x) for x in a.split("."))
    pb = tuple(int(x) for x in b.split("."))
    return (pa > pb) - (pa < pb)


def get_pkg_name(text):
    match = NAME_RE.search(text)
    return match.group(1) if match else None


def get_latest_tag():
    """Return the latest semver git tag (without ``v`` prefix), or None."""
    result = subprocess.run(
        ["git", "tag", "-l", f"{TAG_PREFIX}*", "--sort=-v:refname"],
        capture_output=True,
        text=True,
        check=True,
        cwd=PYPROJECT.parent,
    )
    for tag in result.stdout.splitlines():
        tag = tag.strip()
        if not tag.startswith(TAG_PREFIX):
            continue
        version = tag[len(TAG_PREFIX):]
        if SEMVER_RE.match(version):
            return version
    return None


def find_version_introducing_commit(version):
    """Return the oldest commit that introduced ``version`` in pyproject.toml.

    Used as the "since" reference for counting pending commits when the
    user has been publishing without creating a git tag. Always returns the
    oldest matching commit (i.e. where ``version`` first appeared, not
    subsequent edits). Returns None if no matching commit is found in
    history.
    """
    result = subprocess.run(
        [
            "git",
            "log",
            "--all",
            "--format=%H",
            "-S",
            f'version = "{version}"',
            "--",
            "pyproject.toml",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=PYPROJECT.parent,
    )
    commits = [c for c in result.stdout.splitlines() if c]
    return commits[-1] if commits else None  # oldest match is the introducer


def _release_ref(version, latest_tag):
    """Pick the best git ref to count commits since for ``version``.

    Prefer the tag (clean canonical ref). Fall back to the commit that
    introduced ``version`` in pyproject.toml when no tag exists — this is
    the normal state right after a manual bump that was published to PyPI
    before being pushed to GitHub. Returns ``(ref_string, ref_kind)`` or
    ``(None, None)`` when neither is found.
    """
    if latest_tag == version:
        return f"{TAG_PREFIX}{version}", "tag"
    commit = find_version_introducing_commit(version)
    if commit:
        return commit, "introducer-commit"
    return None, None


def count_commits_since(ref):
    """Return commit count between ``ref`` and HEAD."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{ref}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=PYPROJECT.parent,
    )
    return int(result.stdout.strip())


def fetch_pypi_version(pkg_name):
    """Return latest PyPI version for ``pkg_name``, or None on any error."""
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{pkg_name}/json", timeout=10
        ) as resp:
            data = json.load(resp)
        return data["info"]["version"]
    except Exception:  # noqa: BLE001 - best-effort fetch; any failure -> unknown
        return None


def print_version():
    text = PYPROJECT.read_text(encoding="utf-8")
    version, _ = parse_version(text)
    print(version)


def decide_bump(local_version, latest_tag, latest_pypi):
    """Return ``(should_bump: bool, message: str)``.

    Bump only when local == last_released AND there are commits since
    the latest tag. Otherwise the message explains the skip and what to
    do instead.
    """
    last_released = None
    for v in (latest_tag, latest_pypi):
        if v and (last_released is None or compare_versions(v, last_released) > 0):
            last_released = v

    if last_released is None:
        return False, (
            "No previous release found (no GitHub tag, no PyPI release).\n"
            "For the first release:\n"
            "  1. Edit pyproject.toml directly to set the initial version.\n"
            f"  2. git tag {TAG_PREFIX}<version> && git push origin --tags\n"
            "  3. uv -q publish    (or use 6-publish-to-pypi.sh after tagging)\n"
            "Subsequent bumps will be automatic."
        )

    cmp = compare_versions(local_version, last_released)
    if cmp < 0:
        return False, (
            f"Local version {local_version} is BEHIND last release "
            f"{TAG_PREFIX}{last_released}.\n"
            "Unusual state — check your working tree (stale checkout?)."
        )

    if cmp > 0:
        hints = []
        if latest_tag and compare_versions(local_version, latest_tag) > 0:
            hints.append(
                f"run 5-push-tag-to-github.sh to push {TAG_PREFIX}{local_version}"
            )
        if latest_pypi and compare_versions(local_version, latest_pypi) > 0:
            hints.append(f"run 6-publish-to-pypi.sh to publish {local_version}")
        hint = (" " + " and ".join(hints) + ".") if hints else ""
        return False, (
            f"Local version {local_version} is already ahead of last release "
            f"{TAG_PREFIX}{last_released}.{hint}"
        )

    # local == last_released.
    ref, ref_kind = _release_ref(last_released, latest_tag)
    if ref is None:
        return False, (
            f"Local version {local_version} matches the last released "
            f"version but no reference commit could be located.\n"
            f"Tag the current HEAD first: "
            f"git tag {TAG_PREFIX}{local_version} && git push origin --tags"
        )

    ref_label = f"{TAG_PREFIX}{last_released}"
    if ref_kind == "introducer-commit":
        ref_label = f"{ref_label} (commit {ref[:7]})"

    commits = count_commits_since(ref)
    if commits == 0:
        msg = (
            f"No new commits since {ref_label}; nothing to release."
        )
        if latest_pypi and compare_versions(local_version, latest_pypi) < 0:
            msg += (
                f" PyPI is at {latest_pypi} — run 6-publish-to-pypi.sh to "
                f"publish {local_version}."
            )
        return False, msg

    return True, f"{commits} new commit(s) since {ref_label}"


def apply_bump(level):
    text = PYPROJECT.read_text(encoding="utf-8")
    current, match = parse_version(text)
    pkg_name = get_pkg_name(text)
    latest_pypi = fetch_pypi_version(pkg_name) if pkg_name else None
    latest_tag = get_latest_tag()

    should, reason = decide_bump(current, latest_tag, latest_pypi)
    if not should:
        print(f"Skipping bump: {reason}")
        return bool(latest_pypi and compare_versions(current, latest_pypi) > 0)

    new_version = bump_version(current, level)
    new_text = text[: match.start(1)] + new_version + text[match.end(1):]
    PYPROJECT.write_text(new_text, encoding="utf-8")
    print(f"{current} -> {new_version}  ({reason})")
    return True


def main(argv):
    if len(argv) == 1:
        print_version()
        return 0
    if argv[1] != "bump":
        raise SystemExit(
            f"Error: unknown command '{argv[1]}'. "
            f"Usage: 2-version.py [bump [major|minor|patch]]"
        )
    level = argv[2] if len(argv) > 2 else "patch"
    if level not in VALID_LEVELS:
        raise SystemExit(
            f"Error: bump level must be one of {VALID_LEVELS} (got '{level}')"
        )
    ok = apply_bump(level)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
