#!/bin/bash
# Step 4 of release: stage all pending changes, commit with the given
# message, and push the current branch to GitHub. No version bump, no
# tag (that's 5-push-tag-to-github). Mirrors 4-push-changes-to-github.bat.
#
# All positional args are joined into one commit message, so both
#   $0 "Fix SRT truncation"
# and
#   $0 Fix SRT truncation
# produce the same commit message.

set -e

if [ $# -eq 0 ]; then
    echo "Usage: $0 <commit-message...>"
    echo "Example: $0 \"Fix SRT truncation when speech has no pauses\""
    echo "         $0 Fix SRT truncation when speech has no pauses"
    exit 1
fi
MSG="$*"

if ! gh auth status >/dev/null 2>&1; then
    echo "Error: Not logged in to GitHub. Run 'gh auth login' first."
    exit 1
fi
echo "DEBUG: GitHub auth OK ($(gh api user -q .login 2>/dev/null || echo unknown))."

# Stage every modified/untracked file, except anything the user added
# locally that shouldn't be committed (none currently expected).
git add -A

# Bail out early if there is nothing to commit.
if git diff --cached --quiet; then
    echo "Nothing to commit (working tree clean, no staged changes)."
    exit 1
fi

git status

read -r -p "Press ENTER to commit and push to GitHub..."

echo + git commit -m "$MSG"
git commit -m "$MSG"

echo + git push origin HEAD
git push origin HEAD

echo "Done. Pushed commit to GitHub."
