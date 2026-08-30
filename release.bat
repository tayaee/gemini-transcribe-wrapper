@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MSG=%~1"

REM 1. Quality Gate
echo + call 1-code-quality-gate.bat
call 1-code-quality-gate.bat
if errorlevel 1 exit /b 1

REM 2. Stage tracked modifications (git add -u) and push via 4 if changes exist
echo + git add -u
git add -u
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    if "!MSG!" == "" set "MSG=Update codebase before release"
    echo + call 4-push-changes-to-github.bat !MSG!
    call 4-push-changes-to-github.bat !MSG!
)

REM 3. Bump version in pyproject.toml
echo + call 2-version.bat bump patch
call 2-version.bat bump patch

REM 4. Stage pyproject.toml & uv.lock, build, tag, and push to GitHub
echo + call 5-push-tag-to-github.bat
call 5-push-tag-to-github.bat
if errorlevel 1 exit /b 1

REM 5. Publish to PyPI
echo + call 6-publish-to-pypi.bat
call 6-publish-to-pypi.bat
if errorlevel 1 exit /b 1

echo Done! Full release cycle completed successfully.
