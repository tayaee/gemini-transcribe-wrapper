#!/usr/bin/env bash
# verify-issue-001.sh — --loop-until-no-input / --loop-always flags
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §3.1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOOP="src/gemini_transcribe_wrapper/_loop.py"
CLI="src/gemini_transcribe_wrapper/cli.py"
TEST_NEW="tests/test_loop.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. _loop module exists
if [ -f "$LOOP" ]; then
    ok "_loop.py module present"
else
    fail "$LOOP missing"
fi

# 2. run_with_loop function defined
if grep -q 'def run_with_loop' "$LOOP"; then
    ok "run_with_loop driver function defined"
else
    fail "run_with_loop missing"
fi

# 3. KeyboardInterrupt handling (exit code 130)
if grep -q 'EXIT_INTERRUPT\|130' "$LOOP"; then
    ok "KeyboardInterrupt → exit 130"
else
    fail "KeyboardInterrupt handling missing"
fi

# 4. QuotaExceededError retry inside loop
if grep -q 'QuotaExceededError' "$LOOP"; then
    ok "QuotaExceededError handling inside loop"
else
    fail "QuotaExceededError handling missing"
fi

# 5. CLI options registered
if grep -q -- '--loop-until-no-input' "$CLI" && grep -q -- '--loop-always' "$CLI" && grep -q -- '--loop-poll-secs' "$CLI"; then
    ok "Click options --loop-until-no-input / --loop-always / --loop-poll-secs registered"
else
    fail "Click loop options missing"
fi

# 6. Mutual exclusion check (split across lines in source)
if grep -q 'loop_until_no_input and loop_always' "$CLI"; then
    ok "Mutual exclusion check present"
else
    fail "Mutual exclusion check missing"
fi

# 7. TranscribeOptions dataclass has new fields
if grep -q 'loop_until_no_input' "$CLI" && grep -q 'loop_always' "$CLI" && grep -q 'loop_poll_secs' "$CLI"; then
    ok "TranscribeOptions has loop fields"
else
    fail "TranscribeOptions loop fields missing"
fi

# 8. _run_one_pass wraps the existing loop
if grep -q '_run_one_pass' "$CLI"; then
    ok "_run_one_pass wrapper extracted from main loop"
else
    fail "_run_one_pass wrapper missing"
fi

# 9. test_loop.py exists with >= 5 tests
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 5 ]; then
        ok "$TEST_NEW has $n tests (>= 5 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 5)"
    fi
else
    fail "$TEST_NEW missing"
fi

# 10. ruff + pytest
echo ""
echo "Running ruff check --fix ..."
uv run ruff check --fix >/dev/null
ok "ruff check --fix clean"

echo ""
echo "Running pytest tests/test_loop.py ..."
uv run pytest tests/test_loop.py -q --no-header >/dev/null
ok "loop test file passes"

echo ""
echo "verify-issue-001: ALL CHECKS PASSED"
