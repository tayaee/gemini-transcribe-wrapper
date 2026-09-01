#!/bin/bash
# Guard against recursive invocation. If a step script (or any descendant)
# ever calls release.sh again, this marker is already set and we abort
# instead of looping forever.
if [ -n "${RELEASE_GUARD:-}" ]; then
    echo "Error: release.sh is already running in this session." >&2
    echo "Guard RELEASE_GUARD is set -- refusing to recurse." >&2
    echo "If this is not a nested call, unset RELEASE_GUARD before invoking." >&2
    exit 1
fi
export RELEASE_GUARD=1

set -e
cd "$(dirname "$0")"

MSG="$*"

echo "=========================================="
echo " Starting Release Pipeline"
echo "=========================================="

echo ""
echo commit-message: [$MSG]

# 1. Quality Gate
echo
echo "=== Step 1/5: Running Code Quality Gate ==="
echo + ./1-code-quality-gate.sh
./1-code-quality-gate.sh

# 2. Stage tracked modifications (git add -u) and push via 4 if changes exist
echo
echo "=== Step 2/5: Checking for pending code changes ==="
echo + git add -u
git add -u
if ! git diff --cached --quiet; then
    TARGET_MSG="$MSG"
    if [ -z "$TARGET_MSG" ] && [ -x "./scripts/commit-message.sh" ]; then
        TARGET_MSG="$(./scripts/commit-message.sh 2>/dev/null || true)"
    fi
    if [ -z "$TARGET_MSG" ]; then
        TARGET_MSG="Update codebase before release"
    fi
    echo "Staged changes detected; pushing to GitHub..."
    echo + ./4-push-changes-to-github.sh "$TARGET_MSG"
    ./4-push-changes-to-github.sh "$TARGET_MSG"
else
    echo "Working tree is clean; no pending code changes to push."
fi

# 3. Bump version in pyproject.toml
echo
echo "=== Step 3/5: Checking version & release readiness ==="
echo + ./2-version.sh bump patch
set +e
./2-version.sh bump patch
BUMP_EXIT=$?
set -e

if [ $BUMP_EXIT -eq 2 ]; then
    echo
    echo "---------------------------------------------------------------"
    echo " Nothing to release:"
    echo " Local version is already published on PyPI and no new commits exist."
    echo " Make your code changes and run ./release.sh again when ready."
    echo "---------------------------------------------------------------"
    exit 0
elif [ $BUMP_EXIT -ne 0 ]; then
    echo "Error: Version bump failed (exit code $BUMP_EXIT)."
    exit $BUMP_EXIT
fi

# 4. Commit pyproject.toml/uv.lock, build package, tag vX.Y.Z, and push to GitHub
echo
echo "=== Step 4/5: Building package & pushing tag to GitHub ==="
echo + ./5-push-tag-to-github.sh
./5-push-tag-to-github.sh

# 5. PyPI Publishing (delegated to GitHub Actions Trusted Publishing)
echo
echo "=== Step 5/5: PyPI Publishing via GitHub Actions (Trusted Publishing) ==="
echo "Git tag pushed. GitHub Actions workflow 'Publish to PyPI' has been triggered."
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    echo "Tracking GitHub Actions workflow execution..."
    sleep 3
    RUN_ID="$(gh run list --workflow=publish.yml --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
    if [ -n "$RUN_ID" ]; then
        gh run watch "$RUN_ID" || true
    fi
fi
echo "Track online: https://github.com/tayaee/gemini-transcribe-wrapper/actions"

echo
echo "=========================================="
echo " Release Complete! Successfully shipped to GitHub & PyPI."
echo "=========================================="

echo
echo PyPi: https://pypi.org/project/gemini-transcribe-wrapper/
echo GitHub: https://github.com/tayaee/gemini-transcribe-wrapper
echo '+ git log --oneline --pretty=format:"%ai %h %s" -n 10'
git log --oneline --pretty=format:"%ai %h %s" -n 10

