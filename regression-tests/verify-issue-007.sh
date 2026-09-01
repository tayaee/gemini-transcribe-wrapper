#!/usr/bin/env bash
# verify-issue-007.sh — api_key_tail 8-character consistency
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §2.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KEY_UTILS="src/gemini_transcribe_wrapper/_key_utils.py"
STT="src/gemini_transcribe_wrapper/stt.py"
API="src/gemini_transcribe_wrapper/api.py"
TEST_NEW="tests/test_api_key_tail.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. api_key_tail helper exists in _key_utils
if grep -qE 'def api_key_tail\(' "$KEY_UTILS"; then
    ok "api_key_tail helper exists in _key_utils"
else
    fail "api_key_tail helper missing"
fi

# 2. Default length = 8
if grep -qE 'length:\s*int\s*=\s*API_KEY_TAIL_LENGTH' "$KEY_UTILS" \
   && grep -qE 'API_KEY_TAIL_LENGTH\s*=\s*8' "$KEY_UTILS"; then
    ok "API_KEY_TAIL_LENGTH = 8 default"
else
    fail "API_KEY_TAIL_LENGTH default not 8"
fi

# 3. None / empty → "" guard
if grep -qE 'if not api_key' "$KEY_UTILS"; then
    ok "api_key_tail handles None/empty safely"
else
    fail "api_key_tail lacks None/empty guard"
fi

# 4. mask_key still returns 4-char form (UX re-emission unchanged)
if grep -qE 'def mask_key\(' "$KEY_UTILS" \
   && grep -q '\[redacted\]{key\[-4:\]\}' "$KEY_UTILS"; then
    ok "mask_key still produces [redacted]<last 4>"
else
    fail "mask_key format regression"
fi

# 5. stt.py 429 log line uses api_key_tail, not key[-4:]
if grep -qE 'f"\[redacted\]\{api_key_tail\(key\)\}"' "$STT"; then
    ok "stt.py 429 log uses api_key_tail(key) (8 chars)"
else
    fail "stt.py 429 log does not use api_key_tail"
fi

# 6. stt.py audit log uses api_key_tail
if grep -qE 'key_tail = api_key_tail\(api_key\)' "$STT"; then
    ok "stt.py audit log uses api_key_tail(api_key)"
else
    fail "stt.py audit log still inlines tail slicing"
fi

# 7. api.py blacklist usage uses api_key_tail
if grep -qE 'key_tail = api_key_tail\(first_key\)' "$API"; then
    ok "api.py blacklist uses api_key_tail(first_key)"
else
    fail "api.py blacklist still inlines tail slicing"
fi

# 8. api_key_tail import in stt.py and api.py
if grep -q 'from ._key_utils import' "$STT" \
   && grep -q 'from ._key_utils import' "$API"; then
    ok "stt.py + api.py import api_key_tail from ._key_utils"
else
    fail "Missing api_key_tail import in stt.py or api.py"
fi

# 9. No inlined key[-4:] or api_key[-4:] remains (mask_key excepted)
remaining=$(grep -rnE '(^|[^_a-zA-Z])key\[-4:\][^_a-zA-Z]?' src/ 2>/dev/null \
            | grep -v '_key_utils.py' || true)
if [ -z "$remaining" ]; then
    ok "No inlined key[-4:] remains in src/ (excluding _key_utils)"
else
    echo "$remaining"
    fail "inlined key[-4:] still present"
fi

# 10. Tests cover the helper + integration
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 8 ]; then
        ok "$TEST_NEW has $n tests (>= 8 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 8)"
    fi
    if grep -q 'api_key_tail' "$TEST_NEW" \
       && grep -q 'mask_key' "$TEST_NEW"; then
        ok "Tests cover api_key_tail + mask_key"
    else
        fail "Tests missing key coverage"
    fi
else
    fail "$TEST_NEW missing"
fi

# 11. ruff + pytest
echo ""
echo "Running ruff check --fix ..."
uv run ruff check --fix >/dev/null
ok "ruff check --fix clean"

echo ""
echo "Running pytest tests/test_api_key_tail.py ..."
uv run pytest tests/test_api_key_tail.py -q --no-header >/dev/null
ok "api key tail test file passes"

echo ""
echo "verify-issue-007: ALL CHECKS PASSED"
