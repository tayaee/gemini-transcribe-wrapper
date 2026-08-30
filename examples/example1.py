# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "gemini-transcribe-wrapper",
#     "python-dotenv",
# ]
# ///
"""Example 1: Basic transcription to .srt and .txt.

Usage:
    uv run examples/example1.py
    # or inside examples/:
    uv run example1.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import gemini_transcribe_wrapper as gtw


def main() -> None:
    # Load API key from .env or environment
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY is not set in environment or .env file.", file=sys.stderr)
        sys.exit(1)

    example_dir = Path(__file__).resolve().parent
    input_file = example_dir / "안될과학 개똥벌레.mp4"

    # Check if sample media exists
    if not input_file.exists():
        mp4_files = list(example_dir.glob("*.mp4"))
        if mp4_files:
            input_file = mp4_files[0]
        else:
            print(f"Sample media not found: {input_file}", file=sys.stderr)
            print("Run examples/download-example-videos.sh to download sample files.", file=sys.stderr)
            sys.exit(1)

    print("=== Sample 1: Basic Transcription (.srt and .txt) ===")
    print(f"Input file: {input_file}")

    # Call gtw.gemini_transcribe() with explicit API key
    batch_result = gtw.gemini_transcribe(
        input_file=str(input_file),
        output_dir=str(example_dir / "output_example1"),
        gemini_api_key=api_key,
    )

    # Print results
    for res in batch_result.results:
        if res.status == gtw.TranscribeStatus.SUCCESS:
            print("\n[SUCCESS]")
            print(f"  - SRT subtitle  : {res.output.srt}")
            print(f"  - TXT transcript: {res.output.txt}")
        else:
            print(f"\n[FAILED]: {res.error}", file=sys.stderr)


if __name__ == "__main__":
    main()
