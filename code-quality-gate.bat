@echo off
REM Code quality gate: chain pyright, ruff, semgrep, pytest, and Docker
REM regression tests. Mirrors code-quality-gate.sh. Safe to invoke from any cwd.

cd /d "%~dp0"
setlocal enabledelayedexpansion

set PASS=0
set FAIL=0
set FAILED_LIST=

set LOGDIR=%TEMP%\cqg
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

call :run_one run-pyright.sh         scripts\run-pyright.sh
call :run_one run-ruff.sh            scripts\run-ruff.sh
call :run_one run-semgrep.sh         scripts\run-semgrep.sh
call :run_one run-pytest.sh          scripts\run-pytest.sh
set PACKAGE_SPEC=/src
call :run_one run-regression-tests.sh scripts\run-regression-tests.sh
set PACKAGE_SPEC=

echo.
echo Summary: %PASS% passed, %FAIL% failed
if "%FAIL%"=="0" (
    echo All quality gates passed.
    exit /b 0
)

echo Failed tools: %FAILED_LIST%
for %%T in (%FAILED_LIST%) do (
    echo --- %%T log tail ---
    powershell -NoProfile -Command "Get-Content -Path '%LOGDIR%\cqg-%%T.log' -Tail 20"
)
exit /b 1

:run_one
set NAME=%~1
set SCRIPT=%~2
echo === %NAME% ===
call %SCRIPT% > "%LOGDIR%\cqg-%NAME%.log" 2>&1
if errorlevel 1 (
    echo FAIL: %NAME%
    set /a FAIL+=1
    if defined FAILED_LIST (set FAILED_LIST=!FAILED_LIST! %NAME%) else (set FAILED_LIST=%NAME%)
) else (
    echo PASS: %NAME%
    set /a PASS+=1
)
exit /b 0
