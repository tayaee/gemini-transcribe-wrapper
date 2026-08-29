#!/bin/bash
# Regression tests: verify the version command works on Python 3.10-3.13 via
# Docker. For each combo, print only PASS/FAIL; show captured output on error.
# Set PACKAGE_SPEC to override the package source (default: PyPI name).
set -e

PACKAGE_SPEC="${PACKAGE_SPEC:-gemini-transcribe-wrapper}"
MOUNT=()
if [[ "$PACKAGE_SPEC" == ./* || "$PACKAGE_SPEC" == /* ]]; then
    MOUNT=(-v "$(pwd):/src" -w /src)
fi

fail=0
for PY in 3.10 3.13; do
    log="/tmp/regression-py$PY.log"
    if docker run --rm "${MOUNT[@]}" -e UV_LINK_MODE=copy \
        -e PACKAGE_SPEC="$PACKAGE_SPEC" \
        "python:$PY-slim" bash -c '
            set -e
            pip install -q uv
            export PATH="$HOME/.local/bin:$PATH"
            uvx -q "$PACKAGE_SPEC" --version
            uv -q tool install "$PACKAGE_SPEC" --force
            gtw --version
        ' >"$log" 2>&1; then
        echo "PASS: python-$PY"
    else
        echo "FAIL: python-$PY"
        echo "--- python-$PY output ---"
        cat "$log"
        rm -f "$log"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo "Regression tests failed."
    exit 1
fi
echo "All regression tests passed."
