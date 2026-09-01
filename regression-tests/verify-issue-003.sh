#!/usr/bin/env bash
# verify-issue-003.sh — per-key 30min cooldown (issue-003)
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §4.1.
# NOT a substitute for `uv run pytest` — that's run separately.
#
# Exit code 0 = all checks pass; non-zero = at least one check failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STT="src/gemini_transcribe_wrapper/stt.py"
MODELS="src/gemini_transcribe_wrapper/models.py"
API="src/gemini_transcribe_wrapper/api.py"
TEST_NEW="tests/test_per_key_cooldown.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. KEY_COOLDOWN_SECS module constant is 1800.0
if grep -qE '^KEY_COOLDOWN_SECS\s*[:=]\s*1800(\.0)?' "$STT"; then
    ok "KEY_COOLDOWN_SECS = 1800.0 in $STT"
else
    fail "KEY_COOLDOWN_SECS not 1800.0 in $STT"
fi

# 2. _live_pool / _dead_pool backing fields exist
if grep -q 'self\._live_pool' "$STT" && grep -q 'self\._dead_pool' "$STT"; then
    ok "_live_pool and _dead_pool backing fields present"
else
    fail "_live_pool / _dead_pool not found in $STT"
fi

# 3. _prune_dead_pool method exists
if grep -q 'def _prune_dead_pool' "$STT"; then
    ok "_prune_dead_pool method defined"
else
    fail "_prune_dead_pool method missing"
fi

# 4. SKIPPED_QUOTA enum value exists
if grep -q 'SKIPPED_QUOTA' "$MODELS"; then
    ok "TranscribeStatus.SKIPPED_QUOTA enum value present"
else
    fail "TranscribeStatus.SKIPPED_QUOTA missing from $MODELS"
fi

# 5. Single-key 429 → SKIPPED_QUOTA branch in api.py
if grep -q 'SKIPPED_QUOTA' "$API" && grep -q 'gemini_api_keys' "$API"; then
    ok "api.py branches on len(gemini_api_keys)==1 for SKIPPED_QUOTA"
else
    fail "api.py single-key SKIPPED_QUOTA branch missing"
fi

# 6. Old _active_pool / _cooldown_pool are still available as property aliases
if grep -q 'def _active_pool' "$STT" && grep -q 'def _cooldown_pool' "$STT"; then
    ok "_active_pool / _cooldown_pool preserved as property aliases"
else
    fail "_active_pool / _cooldown_pool aliases missing"
fi

# 7. test_per_key_cooldown.py exists and has at least 8 tests
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 8 ]; then
        ok "$TEST_NEW has $n tests (>= 8 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 8)"
    fi
else
    fail "$TEST_NEW missing"
fi

# 8. ruff + pytest pass (the standard verify pair)
echo ""
echo "Running ruff check --fix ..."
uv run ruff check --fix >/dev/null
ok "ruff check --fix clean"

echo ""
echo "Running pytest tests/test_per_key_cooldown.py ..."
uv run pytest tests/test_per_key_cooldown.py -q --no-header >/dev/null
ok "per-key cooldown test file passes"

echo ""
echo "verify-issue-003: ALL CHECKS PASSED"
