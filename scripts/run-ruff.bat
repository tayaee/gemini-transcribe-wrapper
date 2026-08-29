@echo off
REM Run ruff check from the dev dependency group. Safe to invoke from any cwd.
cd /d "%~dp0.."
uv -q run --group dev ruff check %*
