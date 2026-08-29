#!/bin/bash
set -e

PKG_NAME="$(grep -oP '(?<=^name = ")[^"]*' pyproject.toml | head -n1)"
LOCAL="$(grep -oP '(?<=version = ")[^"]*' pyproject.toml | head -n1)"
if [ -z "$PKG_NAME" ] || [ -z "$LOCAL" ]; then
    echo "Error: could not parse name/version from pyproject.toml"
    exit 1
fi
echo "DEBUG: Package $PKG_NAME, local version $LOCAL."

if [ -n "$1" ] && [ "$1" != "$LOCAL" ]; then
    echo "Error: CLI arg '$1' does not match pyproject.toml version '$LOCAL'."
    exit 1
fi

echo "Fetching latest version from PyPI..."
REMOTE="$(curl -fsSL "https://pypi.org/pypi/$PKG_NAME/json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' \
    2>/dev/null || true)"
if [ -z "$REMOTE" ]; then
    echo "DEBUG: No prior release on PyPI (this will be the first publish)."
elif [ "$REMOTE" = "$LOCAL" ]; then
    echo "Error: PyPI already has $LOCAL. Bump version in pyproject.toml first."
    echo
    echo "Commits on GitHub since v$REMOTE (not yet on PyPI):"
    if git rev-parse "v$REMOTE" >/dev/null 2>&1; then
        git log "v$REMOTE..HEAD" --oneline
    else
        echo "  (tag v$REMOTE not found locally; showing last 20 commits)"
        git log --oneline -20
    fi
    exit 1
else
    echo "DEBUG: PyPI latest is $REMOTE, local is $LOCAL. OK to publish."
fi

if [ -z "$UV_PUBLISH_TOKEN" ]; then
    echo "UV_PUBLISH_TOKEN not set, exit."
    exit 1
fi
echo "DEBUG: Found UV_PUBLISH_TOKEN, good."

if ! gh auth status >/dev/null 2>&1; then
    echo "Error: Not logged in to GitHub. Run 'gh auth login' first."
    exit 1
fi
echo "DEBUG: GitHub auth OK ($(gh api user -q .login 2>/dev/null || echo unknown))."

echo "Staging pyproject.toml (version bump)..."
git add pyproject.toml

echo "Building package (lock + dist)..."
./build.sh

echo "Staging uv.lock (sync from build)..."
git add uv.lock

git status

read -r -p "Press ENTER to publish package and code..."

uv -q publish

git add -u
git commit -m "Version $LOCAL"
git tag "v$LOCAL"
git push origin --tags

echo "Done. Successfully published version $LOCAL."
