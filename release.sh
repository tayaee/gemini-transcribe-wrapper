#!/bin/bash
set -e
cd "$(dirname "$0")"

MSG="$1"

# 1. Quality Gate
./1-code-quality-gate.sh

# 2. If there are pending uncommitted changes, commit and push them to GitHub via 4
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git status --porcelain)" ]; then
    echo "Pending changes detected in working tree; pushing to GitHub..."
    ./4-push-changes-to-github.sh "${MSG:-Update codebase before release}"
fi

# 3. Bump version in pyproject.toml (skips if already ahead or no new commits)
./2-version.sh bump patch

# 4. Commit pyproject.toml/uv.lock, build package, tag vX.Y.Z, and push to GitHub
./5-push-tag-to-github.sh

# 5. Publish to PyPI
./6-publish-to-pypi.sh

echo "Done! Full release cycle completed successfully."
