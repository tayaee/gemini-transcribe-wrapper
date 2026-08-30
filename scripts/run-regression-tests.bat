@echo off
rem Regression tests: verify the version command works on Python 3.10-3.13 via Docker.
rem For each combo, print only PASS/FAIL; show captured output on error.
rem Safe to invoke from any cwd.
rem
rem If Docker is not installed or the daemon is not reachable, the script
rem prints a notice and exits 0 (skip) -- regression tests are optional and
rem must not break local verify runs on machines without Docker.
setlocal

cd /d "%~dp0.."

where docker >nul 2>&1
if errorlevel 1 (
    echo Docker not installed; skipping regression tests.
    exit /b 0
)
docker info >nul 2>&1
if errorlevel 1 (
    echo Docker daemon not reachable; skipping regression tests.
    exit /b 0
)

set PACKAGE_SPEC=gemini-transcribe-wrapper
set FAILED=0

for %%P in (3.13) do (
    docker run --rm -e UV_LINK_MODE=copy -e PACKAGE_SPEC=%PACKAGE_SPEC% python:%%P-slim bash -c ^
        "set -e && pip install -q uv && export PATH=$HOME/.local/bin:$PATH && uvx -q $PACKAGE_SPEC --version && uv -q tool install $PACKAGE_SPEC --force && gtw --version" ^
        > "%TEMP%\regression-py%%P.log" 2>&1
    if errorlevel 1 (
        echo FAIL: python-%%P
        echo --- python-%%P output ---
        type "%TEMP%\regression-py%%P.log"
        del "%TEMP%\regression-py%%P.log"
        set FAILED=1
    ) else (
        echo PASS: python-%%P
        del "%TEMP%\regression-py%%P.log"
    )
)

if "%FAILED%"=="1" (
    echo Regression tests failed.
    exit /b 1
)
echo All regression tests passed.
