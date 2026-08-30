#!/bin/bash
# Generate a 1-line commit summary from git diff using LLM CLI tools (agy, cmdc, claude).
# Usage: ./scripts/commit-message.sh [agy|cmdc|claude]

set -e
cd "$(dirname "$0")/.."

PROVIDER="$1"

# Gather git status & diff (staged + tracked modified)
STATUS="$(git status --short 2>/dev/null || true)"
DIFF="$(git diff --cached 2>/dev/null || true)"
if [ -z "$DIFF" ]; then
    DIFF="$(git diff 2>/dev/null || true)"
fi

if [ -z "$STATUS" ] && [ -z "$DIFF" ]; then
    echo ""
    exit 0
fi

# Truncate diff if too long
TRUNCATED_DIFF="$(echo "$DIFF" | head -n 120)"

PROMPT="Generate a concise, 1-line git commit message summary in English (imperative mood, max 70 chars, no markdown, no backticks, no quotes, no prefix like 'commit:', no explanation) describing the following code changes:

$STATUS

$TRUNCATED_DIFF"

run_provider() {
    local p="$1"
    local out=""
    case "$p" in
        agy)
            if command -v agy >/dev/null 2>&1; then
                out="$(agy -p "$PROMPT" 2>/dev/null || true)"
            fi
            ;;
        cmdc)
            if command -v cmdc >/dev/null 2>&1; then
                out="$(cmdc -p "$PROMPT" 2>/dev/null || true)"
            fi
            ;;
        claude)
            if command -v claude >/dev/null 2>&1; then
                out="$(claude --model MiniMax-M3 -p "$PROMPT" 2>&1 || true)"
                if echo "$out" | grep -q "Failed to authenticate: OAuth session expired"; then
                    echo "Warning: claude OAuth session expired and could not be refreshed. Run 'claude login' to re-authenticate." >&2
                    out=""
                elif echo "$out" | grep -qiE "failed to authenticate|unrecognized_model|error:"; then
                    echo "Warning: claude invocation failed: $(echo "$out" | grep -E 'Failed|Error' | head -n 1)" >&2
                    out=""
                fi
            fi
            ;;
        *)
            echo "Error: Unknown provider '$p' (expected agy, cmdc, or claude)" >&2
            return 1
            ;;
    esac

    local clean=""
    if [ -n "$out" ]; then
        clean="$(echo "$out" | grep -v '^[[:space:]]*$' | head -n 1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["`'"'"']//' -e 's/["`'"'"']$//' -e 's/^[Cc]ommit:[[:space:]]*//')"
    fi
    echo "$clean"
}

MSG=""
if [ -n "$PROVIDER" ]; then
    MSG="$(run_provider "$PROVIDER")"
else
    # Try agy -> cmdc -> claude in sequence
    for prov in agy cmdc claude; do
        MSG="$(run_provider "$prov")"
        if [ -n "$MSG" ]; then
            break
        fi
    done
fi

echo "$MSG"
