# Developer Cheatsheet

### 1. Daily Development & Quality Gates
1. Edit code in `src/` and `tests/`.
2. Verify quality gates:
   ```bash
   ./1-code-quality-gate.sh
   ```

---

### 2. Workflow Scenarios

#### Scenario A: Push Changes to GitHub (Accumulate commits without releasing)
```bash
git add <files>
./4-push-changes-to-github.sh "Commit message"
```

#### Scenario B: Publish Release to PyPI (All changes already pushed)
```bash
./release.sh
```
*Bumps version, creates Git tag `vX.Y.Z`, pushes tag to GitHub, and GitHub Actions automatically publishes to PyPI.*

#### Scenario C: Publish Release to PyPI (With final pending changes)
```bash
git add <files>   # optional if modifying only existing tracked files
./release.sh "Final change description"
```
*Automatically commits & pushes pending changes, bumps version, creates & pushes Git tag, and GitHub Actions publishes to PyPI.*

---

### 3. Post-Release Verification & Update
1. Track GitHub Actions build & publish: `https://github.com/tayaee/gemini-transcribe-wrapper/actions`
2. Verify package on PyPI: `https://pypi.org/project/gemini-transcribe-wrapper/#history`
3. Update local CLI tool (supports Python 3.10–3.13; 3.12 recommended):
   ```bash
   uv tool install --python 3.12 gemini-transcribe-wrapper@latest --force
   gtw -v
   ```
