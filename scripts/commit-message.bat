@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "PROVIDER=%~1"

set "STATUS="
for /F "delims=" %%I in ('git status --short 2^>nul') do (
    set "STATUS=!STATUS! %%I"
)

set "DIFF="
for /F "delims=" %%I in ('git diff --cached 2^>nul') do (
    set "DIFF=!DIFF! %%I"
)
if "!DIFF!" == "" (
    for /F "delims=" %%I in ('git diff 2^>nul') do (
        set "DIFF=!DIFF! %%I"
    )
)

if "!STATUS!" == "" if "!DIFF!" == "" (
    echo.
    exit /b 0
)

set "PROMPT=Generate a concise, 1-line git commit message summary in English (imperative mood, max 70 chars, no markdown, no backticks, no quotes, no prefix like 'commit:', no explanation) describing the following code changes: !STATUS! !DIFF!"

set "MSG="

if not "%PROVIDER%" == "" (
    call :run_prov %PROVIDER%
    goto :done
)

REM Try agy -> cmdc -> claude
call :run_prov agy
if not "!MSG!" == "" goto :done

call :run_prov cmdc
if not "!MSG!" == "" goto :done

call :run_prov claude
if not "!MSG!" == "" goto :done

:done
echo !MSG!
exit /b 0

:run_prov
set "P=%~1"
set "OUT="
if "%P%" == "agy" (
    where agy >nul 2>&1
    if not errorlevel 1 (
        for /F "usebackq delims=" %%O in (`agy -p "%PROMPT%" 2^>nul`) do (
            if "!OUT!" == "" set "OUT=%%O"
        )
    )
) else if "%P%" == "cmdc" (
    where cmdc >nul 2>&1
    if not errorlevel 1 (
        for /F "usebackq delims=" %%O in (`cmdc -p "%PROMPT%" 2^>nul`) do (
            if "!OUT!" == "" set "OUT=%%O"
        )
    )
) else if "%P%" == "claude" (
    where claude >nul 2>&1
    if not errorlevel 1 (
        for /F "usebackq delims=" %%O in (`claude --model MiniMax-M3 -p "%PROMPT%" 2^>^&1`) do (
            echo "%%O" | findstr /C:"Failed to authenticate" >nul 2>&1
            if not errorlevel 1 (
                echo Warning: claude OAuth session expired and could not be refreshed. Run 'claude login' to re-authenticate. 1>&2
                set "OUT="
                goto :skip_claude
            )
            if "!OUT!" == "" set "OUT=%%O"
        )
        :skip_claude
    )
)
if not "!OUT!" == "" (
    set "MSG=!OUT!"
)
exit /b 0
