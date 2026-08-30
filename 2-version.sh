#!/bin/bash
# Display or bump the version in pyproject.toml.
# Usage: ./2-version.sh                  # print current version
#        ./2-version.sh bump [level]     # bump version (level: major|minor|patch, default: patch)
# Mirrors 2-version.bat; delegates to 2-version.py.

cd "$(dirname "$0")"
exec python3 "$(dirname "$0")/2-version.py" "$@"
