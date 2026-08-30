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
REM %TEMP% often ends with a trailing space on Windows, which breaks
REM redirects to "%TEMP%\...". Use a sibling of this script (no spaces
REM in its path) for the Python helper, and capture its stdout via a
REM file read to avoid for-loop parens-parsing surprises.
set "VER_OUT=%~dp0gtw-pypi-ver.txt"
python "%~dp0scripts\get-pypi-version.py" "%PKG_NAME%" > "%VER_OUT%" 2>nul
if not errorlevel 1 set /p REMOTE=<"%VER_OUT%"
del "%VER_OUT%" >nul 2>&1
if "%REMOTE%" == "" goto :no_prior_release
if "%REMOTE%" == "%VERSION%" goto :already_published
echo DEBUG: PyPI latest is %REMOTE%, local is %VERSION%. OK to publish.
goto :after_version_check

:no_prior_release
echo DEBUG: No prior release on PyPI ^^(this will be the first publish^^).
goto :after_version_check

:already_published
echo Notice: PyPI already has %VERSION%. Skipping publish.
exit /b 0

:after_version_check

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

echo + call 3-build.bat
call "%~dp03-build.bat"
if errorlevel 1 (
    echo Error: Build failed.
    exit /b 1
)

REM echo Press ENTER to publish package to PyPI...
REM pause

echo + uv -q publish
uv -q publish
if errorlevel 1 (
    echo Error: Publish failed.
    exit /b 1
)

echo Done. Successfully published version %VERSION% to PyPI.
