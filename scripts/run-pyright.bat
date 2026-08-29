@echo off
REM Run pyright from the dev dependency group. Safe to invoke from any cwd.
cd /d "%~dp0.."
uv -q run --group dev pyright %*
