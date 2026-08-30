#!/bin/bash
# Step 5 of release: stage pyproject.toml + uv.lock, build, then commit,
# tag, and push to GitHub. Mirrors 5-push-tag-to-github.bat.

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

echo + git add pyproject.toml
git add pyproject.toml

echo + ./scripts/3-build.sh
./scripts/3-build.sh

echo + git add uv.lock
git add uv.lock

echo + git status
git status

if ! git diff --cached --quiet; then
    SUMMARY=""
    PREV_TAG="$(git describe --tags --abbrev=0 2>/dev/null || true)"
    if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" = "v$LOCAL" ]; then
        PREV_TAG="$(git describe --tags --abbrev=0 "$PREV_TAG^" 2>/dev/null || true)"
    fi

    COMMITS=""
    if [ -n "$PREV_TAG" ]; then
        COMMITS="$(git log "${PREV_TAG}..HEAD" --pretty=format:"%s" 2>/dev/null || true)"
    else
        COMMITS="$(git log -n 5 --pretty=format:"%s" 2>/dev/null || true)"
    fi

    if [ -n "$COMMITS" ] && [ -x "./scripts/commit-message.sh" ]; then
        SUMMARY="$(./scripts/commit-message.sh --from-log "$COMMITS" 2>/dev/null || true)"
    fi

    if [ -z "$SUMMARY" ] && [ -n "$COMMITS" ]; then
        SUMMARY="$(echo "$COMMITS" | head -n 1)"
    fi

    if [ -n "$SUMMARY" ]; then
        COMMIT_MSG="Version $LOCAL: $SUMMARY"
    else
        COMMIT_MSG="Version $LOCAL"
    fi
    echo + git commit -m "$COMMIT_MSG"
    git commit -m "$COMMIT_MSG"
fi

if ! git rev-parse "v$LOCAL" >/dev/null 2>&1; then
    echo + git tag "v$LOCAL"
    git tag "v$LOCAL"
fi

echo + git push origin HEAD
git push origin HEAD
echo + git push origin "v$LOCAL"
git push origin "v$LOCAL"
