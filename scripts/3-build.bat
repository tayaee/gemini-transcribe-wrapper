@echo off
REM Developer tool: sync uv.lock with pyproject.toml and rebuild the package.
REM Use this after editing version (or any project metadata) in pyproject.toml
REM so that uv.lock stays consistent before `git add` / `git commit`.

cd /d "%~dp0.."
if not exist pyproject.toml (
    echo Error: pyproject.toml not found. Run from the repo root.
    exit /b 1
)

echo Syncing uv.lock with pyproject.toml...
echo + uv -q lock
uv -q lock
if errorlevel 1 (
    echo Error: uv lock failed.
    exit /b 1
)

if exist dist (
    echo + rmdir /s /q dist
    rmdir /s /q dist
)

echo Building package (overwriting existing dist)...
echo + uv -q build
uv -q build
if errorlevel 1 (
    echo Error: uv build failed.
    exit /b 1
)

echo + dir dist
dir dist

echo Done. uv.lock and dist\ are now in sync with pyproject.toml.
