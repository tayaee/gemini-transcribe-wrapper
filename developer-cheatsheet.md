# Developer cheatsheet

1. Add feature in `src/` and `tests/`.
2. Run `./1-code-quality-gate.sh` (or `1-code-quality-gate.bat`) — all 5 gates must PASS.
3. (Optional) Push feature commits to GitHub during development:
   ```bash
   ./4-push-changes-to-github.sh "Implement feature X"
   ```
4. Release to GitHub and PyPI (one-click automated pipeline):
   ```bash
   ./release.sh "Release message"
   ```
   This automatically runs quality gates, commits pending staged changes, bumps version (`2-version`), builds (`3-build`), pushes commit & tag `v<version>` to GitHub (`5-push-tag`), and publishes to PyPI (`6-publish-to-pypi`).
5. Verify the tag at `https://github.com/tayaee/gemini-transcribe-wrapper/releases/tag/v<version>`.
6. Verify the release at `https://pypi.org/project/gemini-transcribe-wrapper/#history`.
7. Install the new version: `uv tool install --python 3.13 gemini-transcribe-wrapper@latest --force`.
8. Confirm with `gtw -v` that the installed version matches.
