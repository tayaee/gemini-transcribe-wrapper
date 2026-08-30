@rem Developer tool: install the local repo and run gtw. Safe from any cwd.
cd /d "%~dp0.."
uv -q tool install -e .
gtw %*
