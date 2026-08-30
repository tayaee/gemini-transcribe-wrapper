#!/bin/bash
set -e
cd "$(dirname "$0")"

MSG="$1"

echo "=========================================="
echo " Starting Release Pipeline"
echo "=========================================="

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
    TARGET_MSG="${MSG:-Update codebase before release}"
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

# 5. Publish to PyPI
echo
echo "=== Step 5/5: Publishing package to PyPI ==="
echo + ./6-publish-to-pypi.sh
./6-publish-to-pypi.sh

echo
echo "=========================================="
echo " Release Complete! Successfully shipped to GitHub & PyPI."
echo "=========================================="
