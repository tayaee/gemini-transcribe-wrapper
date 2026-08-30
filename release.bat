@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MSG=%~1"

REM 1. Quality Gate
call 1-code-quality-gate.bat
if errorlevel 1 exit /b 1

REM 2. If pending changes exist, commit and push to GitHub via 4
for /F %%I in ('git status --porcelain 2^>nul') do (
    echo Pending changes detected in working tree; pushing to GitHub...
    if "!MSG!" == "" set "MSG=Update codebase before release"
    call 4-push-changes-to-github.bat !MSG!
    goto :pushed_changes
)
:pushed_changes

REM 3. Bump version in pyproject.toml
call 2-version.bat bump patch

REM 4. Commit pyproject.toml/uv.lock, build, tag, and push to GitHub
call 5-push-tag-to-github.bat
if errorlevel 1 exit /b 1

REM 5. Publish to PyPI
call 6-publish-to-pypi.bat
if errorlevel 1 exit /b 1

echo Done! Full release cycle completed successfully.
