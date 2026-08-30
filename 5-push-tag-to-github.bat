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
call "%~dp03-build.bat"
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
    set "SUMMARY="
    set "PREV_TAG="
    for /F "delims=" %%T in ('git describe --tags --abbrev=0 2^>nul') do set "PREV_TAG=%%T"
    if "!PREV_TAG!" == "v%VERSION%" (
        set "PREV_TAG="
        for /F "delims=" %%T in ('git describe --tags --abbrev=0 "v%VERSION%^^" 2^>nul') do set "PREV_TAG=%%T"
    )
    set "COMMITS="
    if not "!PREV_TAG!" == "" (
        for /F "delims=" %%C in ('git log "!PREV_TAG!..HEAD" --pretty^=format:"%%s" 2^>nul') do (
            if "!COMMITS!" == "" (
                set "COMMITS=%%C"
            ) else (
                set "COMMITS=!COMMITS!, %%C"
            )
        )
    ) else (
        for /F "delims=" %%C in ('git log -n 5 --pretty^=format:"%%s" 2^>nul') do (
            if "!COMMITS!" == "" (
                set "COMMITS=%%C"
            ) else (
                set "COMMITS=!COMMITS!, %%C"
            )
        )
    )
    if not "!COMMITS!" == "" if exist "%~dp0scripts\commit-message.bat" (
        for /F "usebackq delims=" %%S in (`call "%~dp0scripts\commit-message.bat" --from-log "!COMMITS!" 2^>nul`) do set "SUMMARY=%%S"
    )
    if "!SUMMARY!" == "" if not "!COMMITS!" == "" (
        for /F "tokens=1 delims=," %%F in ("!COMMITS!") do set "SUMMARY=%%F"
    )
    if not "!SUMMARY!" == "" (
        set "COMMIT_MSG=Version %VERSION%: !SUMMARY!"
    ) else (
        set "COMMIT_MSG=Version %VERSION%"
    )
    echo + git commit -m "!COMMIT_MSG!"
    git commit -m "!COMMIT_MSG!"
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
