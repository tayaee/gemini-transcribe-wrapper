#!/usr/bin/env bash
# verify-issue-004.sh — audit log moves to per-key cache directory
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §4.3.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STT="src/gemini_transcribe_wrapper/stt.py"
TEST_NEW="tests/test_audit_log.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. get_audit_log_path accepts an api_key argument
if grep -qE 'def get_audit_log_path\(api_key' "$STT"; then
    ok "get_audit_log_path(api_key=...) signature"
else
    fail "get_audit_log_path missing api_key argument"
fi

# 2. New path uses cache_dir() and api_key[-8:]
if grep -q 'cache_dir()' "$STT" && grep -q 'api_key\[-8:\]' "$STT"; then
    ok "Per-key path under cache_dir() with key_tail"
else
    fail "Per-key path not constructed correctly"
fi

# 3. append_audit_log routes to per-key path when api_key is set
if grep -q 'get_audit_log_path(api_key=api_key)' "$STT"; then
    ok "append_audit_log routes to per-key path"
else
    fail "append_audit_log not using per-key path"
fi

# 4. Legacy fallback path preserved (no api_key → old <temp>/<host>-<user>)
if grep -q 'gemini-transcribe-wrapper-' "$STT" && grep -q '<host>-<user>' "$STT"; then
    ok "Legacy fallback path preserved"
else
    fail "Legacy fallback path missing"
fi

# 5. Parent dir is auto-created (mkdir parents=True)
if grep -q 'mkdir(parents=True, exist_ok=True)' "$STT"; then
    ok "Parent directory auto-created on write"
else
    fail "mkdir(parents=True, exist_ok=True) not found"
fi

# 6. Tests assert the new default path
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 5 ]; then
        ok "$TEST_NEW has $n tests (>= 5 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 5)"
    fi
    if grep -q 'GTW_CACHE_DIR' "$TEST_NEW"; then
        ok "Tests honor GTW_CACHE_DIR override"
    else
        fail "Tests do not honor GTW_CACHE_DIR"
    fi
else
    fail "$TEST_NEW missing"
fi

# 7. ruff + pytest
echo ""
echo "Running ruff check --fix ..."
uv run ruff check --fix >/dev/null
ok "ruff check --fix clean"

echo ""
echo "Running pytest tests/test_audit_log.py ..."
uv run pytest tests/test_audit_log.py -q --no-header >/dev/null
ok "audit log test file passes"

echo ""
echo "verify-issue-004: ALL CHECKS PASSED"
