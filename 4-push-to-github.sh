#!/bin/bash
# Step 4 of release: stage pyproject.toml + uv.lock, build, then commit,
# tag, and push to GitHub. Mirrors 4-push-to-github.bat.

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

if ! gh auth status >/dev/null 2>&1; then
    echo "Error: Not logged in to GitHub. Run 'gh auth login' first."
    exit 1
fi
echo "DEBUG: GitHub auth OK ($(gh api user -q .login 2>/dev/null || echo unknown))."

echo "Staging pyproject.toml (version bump)..."
git add pyproject.toml

echo "Building package (lock + dist)..."
./3-build.sh

echo "Staging uv.lock (sync from build)..."
git add uv.lock

git status

read -r -p "Press ENTER to commit, tag, and push to GitHub..."

git commit -m "Version $LOCAL"
git tag "v$LOCAL"
git push origin --tags

echo "Done. Pushed v$LOCAL to GitHub."
