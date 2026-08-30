@echo off
setlocal

REM Step 5 of release: stage pyproject.toml + uv.lock, build, then commit,
REM tag, and push to GitHub. Mirrors 5-push-tag-to-github.sh.

REM Extract name and version from pyproject.toml (matches grep -oP on sh side).
set PKG_NAME=
set VERSION=
for /F "usebackq tokens=2 delims==" %%V in (`findstr /R /C:"^name = " pyproject.toml`) do (
    set "VAL=%%V"
    setlocal enabledelayedexpansion
    set "VAL=!VAL:"=!"
    for /F "tokens=1" %%W in ("!VAL!") do endlocal & set PKG_NAME=%%W
)
for /F "usebackq tokens=2 delims==" %%V in (`findstr /R /C:"^version = " pyproject.toml`) do (
    set "VAL=%%V"
    setlocal enabledelayedexpansion
    set "VAL=!VAL:"=!"
    for /F "tokens=1" %%W in ("!VAL!") do endlocal & set VERSION=%%W
)

if "%PKG_NAME%" == "" (
    echo Error: could not parse name from pyproject.toml
    exit /b 1
)
if "%VERSION%" == "" (
    echo Error: could not parse version from pyproject.toml
    exit /b 1
)
echo DEBUG: Package %PKG_NAME%, local version %VERSION%.

if not "%~1" == "" if not "%~1" == "%VERSION%" (
    echo Error: CLI arg '%~1' does not match pyproject.toml version '%VERSION%'.
    exit /b 1
)

gh auth status >nul 2>&1
if errorlevel 1 (
    echo Error: Not logged in to GitHub. Run "gh auth login" first.
    exit /b 1
)
echo DEBUG: GitHub auth OK.

echo + git add pyproject.toml
git add pyproject.toml

echo + call 3-build.bat
call 3-build.bat
if errorlevel 1 (
    echo Error: Build failed.
    exit /b 1
)

echo + git add uv.lock
git add uv.lock

echo + git status
git status

git diff --cached --quiet
if errorlevel 1 (
    echo + git commit -m "Version %VERSION%"
    git commit -m "Version %VERSION%"
    if errorlevel 1 (
        echo Error: Git commit failed.
        exit /b 1
    )
)

git rev-parse "v%VERSION%" >nul 2>&1
if errorlevel 1 (
    echo + git tag "v%VERSION%"
    git tag "v%VERSION%"
)

echo + git push origin HEAD
git push origin HEAD
if errorlevel 1 (
    echo Error: Git push branch failed.
    exit /b 1
)
echo + git push origin "v%VERSION%"
git push origin "v%VERSION%"
if errorlevel 1 (
    echo Error: Git push tag failed.
    exit /b 1
)

echo Done. Pushed v%VERSION% to GitHub.
