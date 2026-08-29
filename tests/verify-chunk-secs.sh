#!/bin/bash
# Verify that transcribing with --chunk-secs produces the same timestamps as
# the default single-chunk transcription.
#
# LLM-based STT may split words differently per chunk, so this checks
# timestamps: words that match by text must fall within 0.5s of each other on
# the global timeline (>= 90% of matched words).
#
# Usage: ./verify-chunk-secs.sh [GEMINI_API_KEY] (or set GEMINI_API_KEY env)
set -e

KEY="${1:-$GEMINI_API_KEY}"
if [ -z "$KEY" ]; then
    echo "Error: GEMINI_API_KEY not provided (arg or env)."
    exit 1
fi
export GEMINI_API_KEY="$KEY"

cd "$(dirname "$0")/.."
INPUT="samples/안될과학 개똥벌레.mp4"
BASE_STD="samples/verify-std"
BASE_CHUNK="samples/verify-chunk-len-1m"

cleanup() {
    rm -f "${BASE_STD}".* "${BASE_CHUNK}".*
}
cleanup

echo "=== 1) Standard transcription (single chunk) ==="
gtw "$INPUT" --output-base "$(basename "$BASE_STD")" --force --request-interval-secs 0

echo "=== 2) Chunked transcription (--chunk-secs 60 -> 3 chunks) ==="
gtw "$INPUT" --output-base "$(basename "$BASE_CHUNK")" --force --request-interval-secs 0 --chunk-secs 60

echo "=== 3) Compare transcript.json timestamps ==="
python3 - "${BASE_STD}.transcript.json" "${BASE_CHUNK}.transcript.json" <<'PY'
import json, sys

def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

std = load(sys.argv[1])
chunk = load(sys.argv[2])

def words(d):
    # Chunk words are stored with chunk-local timestamps; add the chunk
    # offset to compare on the global timeline.
    chunk_secs = d.get("chunk_secs", 0.0)
    multi = len(d["chunks"]) > 1
    out = []
    for c in d["chunks"]:
        off = c["index"] * chunk_secs if multi else 0.0
        for w in c["words"]:
            out.append((w["text"], round(w["start"] + off, 3)))
    return out

sw = words(std)
cw = words(chunk)
print(f"standard words: {len(sw)}, chunked words: {len(cw)}")

# LCS on word text to align the two sequences (chunked STT splits words
# differently, so a strict order match would undercount).
n, m = len(sw), len(cw)
dp = [[0] * (m + 1) for _ in range(n + 1)]
for i in range(n - 1, -1, -1):
    for j in range(m - 1, -1, -1):
        dp[i][j] = dp[i + 1][j + 1] + 1 if sw[i][0] == cw[j][0] else max(dp[i + 1][j], dp[i][j + 1])
lcs_len = dp[0][0]
print(f"LCS matched words: {lcs_len} ({lcs_len/max(n,m)*100:.1f}% text similarity)")

# Walk the DP table to collect matched pairs.
pairs = []
i = j = 0
while i < n and j < m:
    if sw[i][0] == cw[j][0]:
        pairs.append((sw[i], cw[j]))
        i += 1
        j += 1
    elif dp[i + 1][j] >= dp[i][j + 1]:
        i += 1
    else:
        j += 1

within = [p for p in pairs if abs(p[0][1] - p[1][1]) <= 0.5]
within_ratio = len(within) / len(pairs) if pairs else 0.0
print(f"matched timestamp pairs within 0.5s: {len(within)}/{len(pairs)} ({within_ratio*100:.1f}%)")

ok = pairs and within_ratio >= 0.90
if not ok:
    print("MISMATCH: timestamp agreement below threshold.")
    for a, b in pairs:
        if abs(a[1] - b[1]) > 0.5:
            print(f"  std ({a[0]}, {a[1]}) vs chunk ({b[0]}, {b[1]})")
    sys.exit(1)

print("PASS: standard and --chunk-secs 60 transcripts agree on timeline.")
PY

cleanup
