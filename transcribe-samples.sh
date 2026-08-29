#!/bin/bash -x
uv -q tool install -e .

mkdir -p out/standard out/chunked

for f in samples/*.mp4; do
    base=$(basename "$f" .mp4)
    gtw "$f" --output-dir out/standard
    gtw "$f" --output-dir out/chunked --chunk-secs 60
    diff "out/standard/$base.srt" "out/chunked/$base.srt" && echo "MATCH: $base .srt"
    diff "out/standard/$base.txt" "out/chunked/$base.txt" && echo "MATCH: $base .txt"
done
