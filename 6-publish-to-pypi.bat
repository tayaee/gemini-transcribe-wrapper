@echo off
setlocal

REM Step 6 of release: verify version is newer than the latest PyPI release,
REM then publish via uv. Mirrors 6-publish-to-pypi.sh.

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

echo Fetching latest version from PyPI...
set REMOTE=
curl -fsSL "https://pypi.org/pypi/%PKG_NAME%/json" -o "%TEMP%\pypi-meta.json" >nul 2>&1
if not errorlevel 1 (
    for /F "usebackq tokens=2 delims==" %%V in (`findstr /C:"\"version\"" "%TEMP%\pypi-meta.json"`) do (
        set "VAL=%%V"
        setlocal enabledelayedexpansion
        set "VAL=!VAL:"=!"
        set "VAL=!VAL:,=!"
        for /F "tokens=1" %%W in ("!VAL!") do endlocal & set REMOTE=%%W
    )
    del "%TEMP%\pypi-meta.json" >nul 2>&1
)
if "%REMOTE%" == "" (
    echo DEBUG: No prior release on PyPI ^^(this will be the first publish^^).
) else if "%REMOTE%" == "%VERSION%" (
    echo Error: PyPI already has %VERSION%. Bump version in pyproject.toml first.
    exit /b 1
) else (
    echo DEBUG: PyPI latest is %REMOTE%, local is %VERSION%. OK to publish.
)

if "%UV_PUBLISH_TOKEN%" == "" (
    echo UV_PUBLISH_TOKEN not set, exit.
    exit /b 1
)
echo DEBUG: Found UV_PUBLISH_TOKEN, good.

gh auth status >nul 2>&1
if errorlevel 1 (
    echo Error: Not logged in to GitHub. Run "gh auth login" first.
    exit /b 1
)
echo DEBUG: GitHub auth OK.

echo Building package (lock + dist)...
call 3-build.bat
if errorlevel 1 (
    echo Error: Build failed.
    exit /b 1
)

echo Press ENTER to publish package to PyPI...
pause

echo + uv -q publish
uv -q publish
if errorlevel 1 (
    echo Error: Publish failed.
    exit /b 1
)

echo Done. Successfully published version %VERSION% to PyPI.
