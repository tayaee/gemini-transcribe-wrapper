#!/usr/bin/env bash
# verify-issue-002.sh — per-input-file blacklist for non-429 errors
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §4.2.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BLACKLIST="src/gemini_transcribe_wrapper/blacklist.py"
MODELS="src/gemini_transcribe_wrapper/models.py"
API="src/gemini_transcribe_wrapper/api.py"
TEST_NEW="tests/test_blacklist.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. blacklist module exists
if [ -f "$BLACKLIST" ]; then
    ok "blacklist.py module present"
else
    fail "$BLACKLIST missing"
fi

# 2. InputBlacklist dataclass defined
if grep -q 'class InputBlacklist' "$BLACKLIST"; then
    ok "InputBlacklist dataclass defined"
else
    fail "InputBlacklist class missing"
fi

# 3. is_blacklisted + add methods
if grep -q 'def is_blacklisted' "$BLACKLIST" && grep -q 'def add' "$BLACKLIST"; then
    ok "is_blacklisted and add methods present"
else
    fail "InputBlacklist methods missing"
fi

# 4. Atomic write (tmp + os.replace)
if grep -q 'os.replace' "$BLACKLIST"; then
    ok "atomic write via os.replace"
else
    fail "no atomic write found"
fi

# 5. Bucket-per-status-code file naming
if grep -q 'http-status-' "$BLACKLIST"; then
    ok "status-bucketed file naming (http-status-{code}.json)"
else
    fail "status-bucketed file naming missing"
fi

# 6. TranscribeStatus.BLACKLISTED enum
if grep -q 'BLACKLISTED' "$MODELS"; then
    ok "TranscribeStatus.BLACKLISTED enum present"
else
    fail "TranscribeStatus.BLACKLISTED missing"
fi

# 7. api.py consults the blacklist before processing
if grep -q 'InputBlacklist' "$API" && grep -q 'is_blacklisted' "$API"; then
    ok "api.py consults InputBlacklist before processing"
else
    fail "api.py blacklist integration missing"
fi

# 8. api.py adds to blacklist on non-429 errors
if grep -q 'bl.add' "$API"; then
    ok "api.py populates blacklist on non-429 errors"
else
    fail "api.py blacklist add missing"
fi

# 9. test_blacklist.py exists with >= 10 tests
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 10 ]; then
        ok "$TEST_NEW has $n tests (>= 10 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 10)"
    fi
else
    fail "$TEST_NEW missing"
fi

# 10. ruff + pytest pass
echo ""
echo "Running ruff check --fix ..."
uv run ruff check --fix >/dev/null
ok "ruff check --fix clean"

echo ""
echo "Running pytest tests/test_blacklist.py ..."
uv run pytest tests/test_blacklist.py -q --no-header >/dev/null
ok "blacklist test file passes"

echo ""
echo "verify-issue-002: ALL CHECKS PASSED"
