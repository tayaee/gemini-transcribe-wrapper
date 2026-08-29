#!/bin/bash
set -e

if [ -z "$1" ]; then
    echo "Usage: $0 VERSION"
    grep "version" pyproject.toml
    exit 1
fi

if ! grep -q "version = \"$1\"" pyproject.toml; then
    echo "Error: Version $1 not found in pyproject.toml"
    exit 1
fi
echo "DEBUG: Version $1 confirmed."

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

if [ -d "dist" ]; then
    rm -f dist/*
fi

echo "Building package..."
uv -q build

git add -u
git status

echo "Press ENTER to publish package and code..."
read -r

uv -q publish

git add -u
git commit -m "Version $1"
git tag "v$1"
git push origin --tags

echo "Done. Successfully published version $1."
