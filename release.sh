#!/bin/bash
set -e
cd "$(dirname "$0")"

MSG="$1"

# 1. Quality Gate
echo + ./1-code-quality-gate.sh
./1-code-quality-gate.sh

# 2. Stage tracked modifications (git add -u) and push via 4 if changes exist
echo + git add -u
git add -u
if ! git diff --cached --quiet; then
    TARGET_MSG="${MSG:-Update codebase before release}"
    echo + ./4-push-changes-to-github.sh "$TARGET_MSG"
    ./4-push-changes-to-github.sh "$TARGET_MSG"
fi

# 3. Bump version in pyproject.toml (skips if already ahead or no new commits)
echo + ./2-version.sh bump patch
./2-version.sh bump patch

# 4. Commit pyproject.toml/uv.lock, build package, tag vX.Y.Z, and push to GitHub
echo + ./5-push-tag-to-github.sh
./5-push-tag-to-github.sh

# 5. Publish to PyPI
echo + ./6-publish-to-pypi.sh
./6-publish-to-pypi.sh

echo "Done! Full release cycle completed successfully."
