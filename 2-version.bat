@echo off
REM Display or bump the version in pyproject.toml.
REM Usage: 2-version                     REM print current version
REM        2-version bump [level]        REM bump version (level: major|minor|patch, default: patch)
REM Mirrors 2-version.sh; delegates to scripts/bump_version.py via uv run so the
REM PEP 723 inline metadata in scripts/bump_version.py is honored.

uv run "%~dp0scripts\bump_version.py" %*
