@echo off
REM Code quality gate: chain pyright, ruff, semgrep, pip-audit, gitleaks, pytest,
REM and Docker regression tests. Mirrors 1-code-quality-gate.sh. Safe to invoke
REM from any cwd.
REM Each step displays "=== name: purpose ===" so the output is self-documenting.

cd /d "%~dp0"
setlocal enabledelayedexpansion

set PASS=0
set FAIL=0
set FAILED_LIST=

set LOGDIR=%TEMP%\cqg
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM pyright: static type checker. Goes deeper than syntax -- flags wrong types,
REM missing attributes, bad overload resolution before the code runs.
call :run_one run-pyright.bat "static type checker (wrong types, missing attrs)" scripts\run-pyright.bat

REM ruff: fast linter. Enforces style and flags likely bugs (unused imports,
REM shadowed builtins, etc.). run-ruff.bat wraps `ruff check` (non-mutating).
call :run_one run-ruff.bat "fast linter (style + likely-bug patterns)" scripts\run-ruff.bat

REM semgrep: pattern-based static analysis. Configured with --config auto
REM --error on src/ + tests/, so any finding fails the gate. Acts as the
REM security review (CWE top 25, secrets, dangerous APIs).
call :run_one run-semgrep.bat "security scan (CWE top 25, secrets, dangerous APIs)" scripts\run-semgrep.bat

REM pip-audit: dependency vulnerability scan. Where semgrep looks at our own
REM code, this checks the packages we pull in against the PyPI advisory
REM database, so a known CVE in a transitive dep fails the gate.
call :run_one run-pip-audit.bat "dependency vulnerability scan (known CVEs in deps)" scripts\run-pip-audit.bat

REM gitleaks: secret scan. pip-audit covers known-vulnerable deps; this covers
REM credentials accidentally written into the tree. Allowlists (fake test keys,
REM binary media) live in .gitleaks.toml. Skips itself if neither a gitleaks
REM binary nor Docker is available.
call :run_one run-gitleaks.bat "secret scan (credentials in the working tree)" scripts\run-gitleaks.bat

REM pytest: actual test execution. The only step that exercises runtime
REM behavior -- the earlier gates are all static.
call :run_one run-pytest.bat "unit tests (runtime correctness of src/ + tests/)" scripts\run-pytest.bat

REM regression: end-to-end against a packaged install. With PACKAGE_SPEC=/src
REM here it installs the local source into a fresh Python 3.12 Docker
REM container and runs `gtw --version` -- catches issues that only surface
REM after install (entry point wiring, missing deps, packaging metadata).
REM Set PACKAGE_SPEC=<PyPI-name> to verify a real release instead.
set PACKAGE_SPEC=/src
call :run_one run-regression-tests.bat "end-to-end against installed package (local source)" scripts\run-regression-tests.bat
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
set PURPOSE=%~2
set SCRIPT=%~3
echo === %NAME%: %PURPOSE% (%SCRIPT%) ===
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
