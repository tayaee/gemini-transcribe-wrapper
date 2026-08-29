#!/bin/bash
set -e
cd "$(dirname "$0")"
uv -q run --group dev semgrep scan --config auto --error src tests "$@"
