#!/bin/bash -x
# Download YouTube clips into samples/.
# Safe to invoke from any working directory.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$SCRIPT_DIR"

uvx -q --python 3.13 --with yt-dlp --with static-ffmpeg yt-dlp --remux-video mp4 --download-sections "*00:00-03:00" "https://www.youtube.com/watch?v=AqaOiXXaYaU" -o "$OUT_DIR/안될과학 개똥벌레.mp4"
uvx -q --python 3.13 --with yt-dlp --with static-ffmpeg yt-dlp --remux-video mp4 --download-sections "*00:00-03:00" "https://www.youtube.com/watch?v=sJ1q9daiOpI" -o "$OUT_DIR/Jackson Hall Speech 20260828.mp4"
