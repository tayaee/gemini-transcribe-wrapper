#!/usr/bin/env bash
# verify-issue-006.sh — color the console only when stderr is a TTY
#
# Mechanical checks for the acceptance criteria in
# docs/spec/spec-2-free-tier-quota-hardening.md §4.5.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOGGING_MOD="src/gemini_transcribe_wrapper/_logging.py"
CLI="src/gemini_transcribe_wrapper/cli.py"
TEST_NEW="tests/test_color_tty.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# 1. _ColorFormatter class exists and extends _TzFormatter
if grep -qE 'class _ColorFormatter\(_TzFormatter\)' "$LOGGING_MOD"; then
    ok "_ColorFormatter extends _TzFormatter"
else
    fail "_ColorFormatter subclass relationship missing"
fi

# 2. LEVEL_COLORS table covers DEBUG/INFO/WARNING/ERROR/CRITICAL
for level in DEBUG INFO WARNING ERROR CRITICAL; do
    if grep -q "\"$level\":" "$LOGGING_MOD"; then
        ok "LEVEL_COLORS contains $level"
    else
        fail "LEVEL_COLORS missing $level"
    fi
done

# 3. RESET constant present
if grep -qE 'RESET[: =].*"\\x1b\[0m"' "$LOGGING_MOD"; then
    ok "RESET = ESC[0m"
else
    fail "RESET constant missing"
fi

# 4. resolve_color_mode: auto/always/never
if grep -qE 'def resolve_color_mode\(value: str\)' "$LOGGING_MOD" \
   && grep -q '"auto"' "$LOGGING_MOD" \
   && grep -q '"always"' "$LOGGING_MOD" \
   && grep -q '"never"' "$LOGGING_MOD"; then
    ok "resolve_color_mode(auto|always|never)"
else
    fail "resolve_color_mode missing or incomplete"
fi

# 5. auto uses sys.stderr.isatty()
if grep -q 'sys.stderr.isatty' "$LOGGING_MOD"; then
    ok "auto mode uses sys.stderr.isatty()"
else
    fail "auto mode does not check isatty"
fi

# 6. CLI exposes --color
if grep -q -- '--color' "$CLI" \
   && grep -q 'click.Choice(\["auto", "always", "never"\]' "$CLI"; then
    ok "CLI --color with auto|always|never Choice"
else
    fail "--color CLI option missing"
fi

# 7. CLI passes opts.color through resolve_color_mode()
if grep -q 'resolve_color_mode(opts.color)' "$CLI"; then
    ok "CLI resolves opts.color via resolve_color_mode()"
else
    fail "CLI does not call resolve_color_mode"
fi

# 8. CLI uses _ColorFormatter for console handler
if grep -q '_ColorFormatter(' "$CLI"; then
    ok "CLI instantiates _ColorFormatter"
else
    fail "CLI does not use _ColorFormatter"
fi

# 9. File handler still uses plain _TzFormatter (no color in files)
if grep -q '_TzFormatter("%(asctime)s' "$LOGGING_MOD"; then
    ok "File handler formatter is plain _TzFormatter"
else
    fail "File handler should use plain _TzFormatter"
fi

# 10. Tests cover formatter + flag
if [ -f "$TEST_NEW" ]; then
    n=$(grep -c '^def test_' "$TEST_NEW" || true)
    if [ "$n" -ge 10 ]; then
        ok "$TEST_NEW has $n tests (>= 10 required)"
    else
        fail "$TEST_NEW has only $n tests (need >= 10)"
    fi
    if grep -q '_ColorFormatter' "$TEST_NEW" \
       && grep -q 'resolve_color_mode' "$TEST_NEW" \
       && grep -q 'isatty' "$TEST_NEW"; then
        ok "Tests cover formatter + flag + isatty"
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
echo "Running pytest tests/test_color_tty.py ..."
uv run pytest tests/test_color_tty.py -q --no-header >/dev/null
ok "color tty test file passes"

echo ""
echo "verify-issue-006: ALL CHECKS PASSED"
