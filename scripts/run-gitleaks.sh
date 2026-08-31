#!/bin/bash
# Secret scan over the working tree. Uses a native `gitleaks` binary when one
# is on PATH, otherwise the official Docker image. If neither is available the
# scan is skipped (exit 0) so local verify runs still work on machines without
# Docker -- same policy as run-regression-tests.sh.
# Rules and allowlists live in .gitleaks.toml at the repo root.
set -e
cd "$(dirname "$0")/.."

if command -v gitleaks >/dev/null 2>&1; then
    exec gitleaks dir . --no-banner --redact "$@"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo + exec docker run --rm -v "$(pwd):/repo" -w /repo ghcr.io/gitleaks/gitleaks:latest dir . --no-banner --redact "$@"
    exec docker run --rm -v "$(pwd):/repo" -w /repo ghcr.io/gitleaks/gitleaks:latest dir . --no-banner --redact "$@"
fi

echo "gitleaks binary and Docker both unavailable; skipping secret scan."
exit 0
