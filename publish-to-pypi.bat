@echo off

if .%1. == .. (
    echo Usage: %~nx0 VERSION
    findstr version pyproject.toml
    exit /b 1
)

findstr /C:"version = \"%~1\"" pyproject.toml
if errorlevel 1 (
    echo Error: Version %1 not found in pyproject.toml
    exit /b 1
)
echo DEBUG: Version %1 confirmed.

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
call build.bat
if errorlevel 1 (
    echo Error: Build failed.
    exit /b 1
)

git add -u
git status

echo Press ENTER to publish package and code...
pause
uv -q publish
if errorlevel 1 (
    echo Error: Publish failed.
    exit /b 1
)

git add -u
git commit -m "Version %1"
git tag v%1
git push origin --tags

echo Done. Successfully published version %1.
