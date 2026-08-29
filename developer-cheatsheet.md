# Developer cheatsheet

1. Add feature in `src/` and `tests/`.
2. Run `./code-quality-gate.sh` — every gate must PASS.
3. Bump `version` in `pyproject.toml`.
4. Run `./build.sh` to sync `uv.lock` and rebuild `dist/`, then `git add pyproject.toml uv.lock`.
5. Run `./publish-to-pypi.sh` — confirms version differs from latest PyPI, publishes, commits, tags `v<version>`, pushes to GitHub.
6. Verify the tag at `https://github.com/tayaee/gemini-transcribe-wrapper/releases/tag/v<version>`.
7. Verify the release at `https://pypi.org/project/gemini-transcribe-wrapper/#history`.
8. Install the new version: `uvx tool install --python 3.13 gemini-transcribe-wrapper@latest --force`.
9. Confirm with `gtw -v` that the installed version matches.
