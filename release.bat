@echo off
REM Guard against recursive invocation. If a step script (or any descendant)
REM ever calls release.bat again, this marker is already set and we abort
REM instead of looping forever.
if defined RELEASE_GUARD (
    echo Error: release.bat is already running in this session.
    echo Guard RELEASE_GUARD is set -- refusing to recurse.
    echo If this is not a nested call, unset RELEASE_GUARD before invoking.
    exit /b 1
)
set "RELEASE_GUARD=1"

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MSG=%~1"

echo ==========================================
echo  Starting Release Pipeline
echo ==========================================

REM 1. Quality Gate
echo.
echo === Step 1/5: Running Code Quality Gate ===
echo + call 1-code-quality-gate.bat
call "%~dp01-code-quality-gate.bat"
if errorlevel 1 exit /b 1

REM 2. Stage tracked modifications and push via 4 if changes exist
echo.
echo === Step 2/5: Checking for pending code changes ===
echo + git add -u
git add -u
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    if "!MSG!" == "" (
        if exist "%~dp0scripts\commit-message.bat" (
            for /F "usebackq delims=" %%S in (`call "%~dp0scripts\commit-message.bat" 2^>nul`) do set "MSG=%%S"
        )
    )
    if "!MSG!" == "" set "MSG=Update codebase before release"
    echo Staged changes detected; pushing to GitHub...
    echo + call 4-push-changes-to-github.bat !MSG!
    call "%~dp04-push-changes-to-github.bat" !MSG!
) else (
    echo Working tree is clean; no pending code changes to push.
)

REM 3. Bump version in pyproject.toml
echo.
echo === Step 3/5: Checking version & release readiness ===
echo + call 2-version.bat bump patch
call "%~dp02-version.bat" bump patch
set BUMP_EXIT=%ERRORLEVEL%

if %BUMP_EXIT% EQU 2 (
    echo.
    echo ---------------------------------------------------------------
    echo  Nothing to release:
    echo  Local version is already published on PyPI and no new commits exist.
    echo  Make your code changes and run release.bat again when ready.
    echo ---------------------------------------------------------------
    exit /b 0
)
if %BUMP_EXIT% NEQ 0 (
    echo Error: Version bump failed (exit code %BUMP_EXIT%).
    exit /b %BUMP_EXIT%
)

REM 4. Stage pyproject.toml & uv.lock, build, tag, and push to GitHub
echo.
echo === Step 4/5: Building package & pushing tag to GitHub ===
echo + call 5-push-tag-to-github.bat
call "%~dp05-push-tag-to-github.bat"
if errorlevel 1 exit /b 1

REM 5. Publish to PyPI
echo.
echo === Step 5/5: Publishing package to PyPI ===
echo + call 6-publish-to-pypi.bat
call "%~dp06-publish-to-pypi.bat"
if errorlevel 1 exit /b 1

echo.
echo ==========================================
echo  Release Complete! Successfully shipped to GitHub ^& PyPI.
echo ==========================================
