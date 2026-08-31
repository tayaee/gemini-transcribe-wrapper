@echo off
REM Secret scan over the working tree. Uses a native gitleaks binary when one
REM is on PATH, otherwise the official Docker image. If neither is available
REM the scan is skipped (exit 0), matching run-gitleaks.sh.
REM Rules and allowlists live in .gitleaks.toml at the repo root.
cd /d "%~dp0.."

where gitleaks >nul 2>&1
if not errorlevel 1 goto native

where docker >nul 2>&1
if errorlevel 1 goto skip
docker info >nul 2>&1
if errorlevel 1 goto skip

docker run --rm -v "%CD%:/repo" -w /repo ghcr.io/gitleaks/gitleaks:latest dir . --no-banner --redact %*
exit /b %errorlevel%

:native
gitleaks dir . --no-banner --redact %*
exit /b %errorlevel%

:skip
echo gitleaks binary and Docker both unavailable; skipping secret scan.
exit /b 0
