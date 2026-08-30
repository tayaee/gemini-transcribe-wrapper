@echo off
setlocal

REM Step 4 of release: stage all pending changes, commit with the given
REM message, and push the current branch to GitHub. No version bump, no
REM tag (that's 5-push-tag-to-github). Mirrors 4-push-changes-to-github.sh.
REM
REM All positional args are joined into one commit message, so both
REM   %~nx0 "Fix SRT truncation"
REM and
REM   %~nx0 Fix SRT truncation
REM produce the same commit message.

set MSG=
:loop
if "%~1" == "" goto endloop
if not "%MSG%" == "" set MSG=%MSG%
set MSG=%MSG%%~1
shift
goto loop
:endloop

if "%MSG%" == "" (
    echo Usage: %~nx0 ^<commit-message...^>
    echo Example: %~nx0 "Fix SRT truncation when speech has no pauses"
    echo          %~nx0 Fix SRT truncation when speech has no pauses
    exit /b 1
)

echo + gh auth status
gh auth status >nul 2>&1
if errorlevel 1 (
    echo Error: Not logged in to GitHub. Run "gh auth login" first.
    exit /b 1
)
# echo DEBUG: GitHub auth OK.

echo + git add -A
git add -A
git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing to commit ^(working tree clean, no staged changes^).
    exit /b 0
)

git status

# echo Press ENTER to commit and push to GitHub...
# pause

echo + git commit -m "%MSG%"
git commit -m "%MSG%"
if errorlevel 1 (
    echo Error: Git commit failed.
    exit /b 1
)

echo + git push origin HEAD
git push origin HEAD
if errorlevel 1 (
    echo Error: Git push failed.
    exit /b 1
)

# echo Done. Pushed commit to GitHub.
