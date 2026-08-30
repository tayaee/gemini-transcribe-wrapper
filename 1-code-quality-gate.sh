#!/bin/bash
# 1-code-quality-gate: chain the per-tool scripts into a single quality gate.
# Each step is run sequentially; if any step exits non-zero, the gate fails.
# The "=== name: purpose ===" header explains why each tool is in the chain,
# so the output is self-documenting for first-time readers.
# Mirrors 1-code-quality-gate.bat.

cd "$(dirname "$0")"
set -u

pass=0
fail=0
failed_tools=()

# run_one <name> <purpose> <command...>
# Display:    "=== <name>: <purpose> ==="
# Log file:   /tmp/cqg-<name>.log (short tool identifier only)
# On failure: appends <name> to failed_tools[] for the post-run summary.
run_one() {
    local name="$1"
    local purpose="$2"
    shift 2
    echo "=== $name: $purpose ==="
    if "$@" >/tmp/cqg-$name.log 2>&1; then
        echo "PASS: $name"
        pass=$((pass + 1))
    else
        echo "FAIL: $name"
        fail=$((fail + 1))
        failed_tools+=("$name")
    fi
}

# pyright: static type checker. Goes deeper than syntax -- flags wrong types,
# missing attributes, bad overload resolution before the code runs.
run_one run-pyright.sh \
    "static type checker (wrong types, missing attrs)" \
    ./scripts/run-pyright.sh

# ruff: fast linter. Enforces style and flags likely bugs (unused imports,
# shadowed builtins, etc.). `run-ruff.sh` wraps `ruff check` (non-mutating;
# use `ruff check --fix` separately when you want auto-fix).
run_one run-ruff.sh \
    "fast linter (style + likely-bug patterns)" \
    ./scripts/run-ruff.sh

# semgrep: pattern-based static analysis. Configured here with
# --config auto --error on src/ + tests/, so any finding fails the gate.
# Acts as the security review (CWE top 25, secrets, dangerous APIs).
run_one run-semgrep.sh \
    "security scan (CWE top 25, secrets, dangerous APIs)" \
    ./scripts/run-semgrep.sh

# pytest: actual test execution. The only step that exercises runtime
# behavior -- the earlier gates are all static.
run_one run-pytest.sh \
    "unit tests (runtime correctness of src/ + tests/)" \
    ./scripts/run-pytest.sh

# regression: end-to-end against a packaged install. With PACKAGE_SPEC=/src
# here it installs the local source into a fresh Python 3.12 Docker
# container and runs `gtw --version` -- catches issues that only surface
# after install (entry point wiring, missing deps, packaging metadata).
# Set PACKAGE_SPEC=<PyPI-name> to verify a real release instead.
PACKAGE_SPEC=/src run_one run-regression-tests.sh \
    "end-to-end against installed package (local source)" \
    ./scripts/run-regression-tests.sh

echo
echo "Summary: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
    echo "Failed tools: ${failed_tools[*]}"
    for t in "${failed_tools[@]}"; do
        echo "--- $t log tail ---"
        tail -20 "/tmp/cqg-$t.log"
    done
    exit 1
fi
echo "All quality gates passed."
