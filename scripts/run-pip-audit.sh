#!/bin/bash
set -e
cd "$(dirname "$0")/.."
uv -q run --with pip-audit pip-audit "$@"
