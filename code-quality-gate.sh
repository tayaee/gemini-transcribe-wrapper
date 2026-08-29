#!/bin/bash
cd "$(dirname "$0")"
set -u

pass=0
fail=0
failed_tools=()

run_one() {
    local name="$1"
    shift
    echo "=== $name ==="
    if "$@" >/tmp/cqg-$name.log 2>&1; then
        echo "PASS: $name"
        pass=$((pass + 1))
    else
        echo "FAIL: $name"
        fail=$((fail + 1))
        failed_tools+=("$name")
    fi
}

run_one pytest ./run_pytest.sh
run_one ruff ./run_ruff.sh
run_one pyright ./run_pyright.sh

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
