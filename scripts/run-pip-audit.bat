@echo off
REM Run pip-audit against the project's dependencies. Safe to invoke from any cwd.
cd /d "%~dp0.."
uv -q run --with pip-audit pip-audit %*
