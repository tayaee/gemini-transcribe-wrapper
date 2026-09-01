#!/usr/bin/env bash
# verify-issue-005.sh — file-based logging with rotation (5MB × 3)
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §4.4.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOGGING_MOD="src/gemini_transcribe_wrapper/_logging.py"
CLI="src/gemini_transcribe_wrapper/cli.py"
TEST_NEW="tests/test_file_logging.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. _logging module exists
if [ -f "$LOGGING_MOD" ]; then
    ok "_logging.py module exists"
else
    fail "_logging.py module missing"
fi

# 2. setup_file_logging helper exists and takes cache_root
if grep -qE 'def setup_file_logging\(cache_root' "$LOGGING_MOD"; then
    ok "setup_file_logging(cache_root) signature"
else
    fail "setup_file_logging(cache_root) signature not found"
fi

# 3. RotatingFileHandler wired with 5MB / backupCount=2 (via constants)
if grep -q 'RotatingFileHandler' "$LOGGING_MOD" \
   && grep -qE 'MAX_BYTES\s*=\s*5\s*\*\s*1024\s*\*\s*1024' "$LOGGING_MOD" \
   && grep -qE 'BACKUP_COUNT\s*=\s*2' "$LOGGING_MOD" \
   && grep -q 'maxBytes=MAX_BYTES' "$LOGGING_MOD" \
   && grep -q 'backupCount=BACKUP_COUNT' "$LOGGING_MOD"; then
    ok "RotatingFileHandler(5MB, backupCount=2)"
else
    fail "RotatingFileHandler sizing not configured correctly"
fi

# 4. delay=True (issue §Notes)
if grep -q 'delay=True' "$LOGGING_MOD"; then
    ok "delay=True so file is not opened until first record"
else
    fail "delay=True not found"
fi

# 5. Path uses cache_dir / logs
if grep -q 'LOG_SUBDIR' "$LOGGING_MOD" && grep -q 'LOG_FILE_NAME' "$LOGGING_MOD"; then
    ok "Path is <cache_dir>/logs/<file>"
else
    fail "Log file path constants missing"
fi

# 6. Unwritable cache_dir → warning + no crash (returns None)
if grep -qE 'def setup_file_logging' "$LOGGING_MOD" \
   && grep -q 'return None' "$LOGGING_MOD" \
   && grep -q 'file logging disabled' "$LOGGING_MOD"; then
    ok "Unwritable cache_dir handled gracefully"
else
    fail "Unwritable cache_dir fallback not implemented"
fi

# 7. CLI flag --no-file-log wired in cli.py
if grep -q -- '--no-file-log' "$CLI" \
   && grep -q 'no_file_log' "$CLI"; then
    ok "--no-file-log CLI flag wired"
else
    fail "--no-file-log CLI flag missing"
fi

# 8. CLI calls setup_file_logging() after console handler setup
if grep -q 'setup_file_logging()' "$CLI"; then
    ok "CLI invokes setup_file_logging()"
else
    fail "CLI does not call setup_file_logging()"
fi

# 9. CLI respects --no-file-log (gated by 'if not opts.no_file_log')
if grep -q 'if not opts.no_file_log' "$CLI"; then
    ok "CLI gates setup_file_logging on opts.no_file_log"
else
    fail "opts.no_file_log gate missing"
fi

# 10. Tests assert the new behavior
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 8 ]; then
        ok "$TEST_NEW has $n tests (>= 8 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 8)"
    fi
    if grep -q 'setup_file_logging' "$TEST_NEW" \
       && grep -q 'RotatingFileHandler' "$TEST_NEW"; then
        ok "Tests cover setup_file_logging + RotatingFileHandler"
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
echo "Running pytest tests/test_file_logging.py ..."
uv run pytest tests/test_file_logging.py -q --no-header >/dev/null
ok "file logging test file passes"

echo ""
echo "verify-issue-005: ALL CHECKS PASSED"
