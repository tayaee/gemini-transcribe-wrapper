#!/bin/bash
# Developer tool: sync uv.lock with pyproject.toml and rebuild the package.
# Use this after editing version (or any project metadata) in pyproject.toml
# so that uv.lock stays consistent before `git add` / `git commit`.

set -e
cd "$(dirname "$0")/.."

if [ ! -f pyproject.toml ]; then
    echo "Error: pyproject.toml not found. Run from the repo root."
    exit 1
fi

echo "Syncing uv.lock with pyproject.toml..."
echo + uv -q lock
uv -q lock

if [ -e dist ]; then
    echo + /bin/rm -rf dist
    /bin/rm -rf dist
fi

echo "Building package (overwriting existing dist/*)..."
echo + uv -q build
uv -q build

echo + ls -l dist
ls -l dist

echo "Done. uv.lock and dist/ are now in sync with pyproject.toml."
