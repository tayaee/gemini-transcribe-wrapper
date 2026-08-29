#!/bin/bash -x
# Transcribe every mp4 in samples/ twice (standard + chunked) and diff them.
# Outputs land in <repo_root>/out/. Safe to invoke from any working directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SAMPLES_DIR="$SCRIPT_DIR"
OUT_DIR="$REPO_ROOT/out"

cd "$REPO_ROOT"

uv -q tool install -e .

mkdir -p "$OUT_DIR/standard" "$OUT_DIR/chunked"

for f in "$SAMPLES_DIR"/*.mp4; do
    base=$(basename "$f" .mp4)
    gtw "$f" --output-dir "$OUT_DIR/standard"
    gtw "$f" --output-dir "$OUT_DIR/chunked" --chunk-secs 60
    diff "$OUT_DIR/standard/$base.srt" "$OUT_DIR/chunked/$base.srt" && echo "MATCH: $base .srt"
    diff "$OUT_DIR/standard/$base.txt" "$OUT_DIR/chunked/$base.txt" && echo "MATCH: $base .txt"
done
