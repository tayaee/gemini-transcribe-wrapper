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

echo + gh auth status
if ! gh auth status >/dev/null 2>&1; then
    echo "Error: Not logged in to GitHub. Run 'gh auth login' first."
    exit 1
fi
echo "DEBUG: GitHub auth OK ($(gh api user -q .login 2>/dev/null || echo unknown))."

echo + git add -A
git add -A
if git diff --cached --quiet; then
    echo "Nothing to commit (working tree clean, no staged changes)."
    exit 0
fi

echo + git status
git status

echo + git commit -m "$MSG"
git commit -m "$MSG"

echo + git push origin HEAD
git push origin HEAD

# echo "Done. Pushed commit to GitHub."
