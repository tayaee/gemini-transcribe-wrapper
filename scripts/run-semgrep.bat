@echo off
REM Run semgrep scan from the dev dependency group. Safe to invoke from any cwd.
cd /d "%~dp0.."
uv -q run --group dev semgrep scan --config auto --error src tests %*
