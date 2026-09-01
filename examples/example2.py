# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "gemini-transcribe-wrapper",
#     "python-dotenv",
# ]
# ///
"""Example 2: Speaker diarization (.diarized.srt) and batch transcription.

Usage:
    uv run examples/example2.py
    # or inside examples/:
    uv run example2.py
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
    input_pattern = str(example_dir / "안될과학 개똥벌레.mp4")

    # Check for target files
    mp4_files = list(example_dir.glob("*.mp4"))
    if not mp4_files:
        print(f"No mp4 files found in: {example_dir}", file=sys.stderr)
        print("Run examples/download-example-videos.sh to download sample files.", file=sys.stderr)
        sys.exit(1)

    print("=== Sample 2: Diarization (.diarized.srt) and Batch Processing ===")
    print(f"Input pattern: {input_pattern}")
    print(f"Found {len(mp4_files)} file(s).")

    # Optional speaker name mapping
    speakers_map = {
        "spk:0": "Speaker 1",
        "spk:1": "Speaker 2",
    }

    # Call gtw.gemini_transcribe() with explicit API key and diarization enabled
    batch_result = gtw.gemini_transcribe(
        input_file=input_pattern,
        output_dir=str(example_dir / "output_example2"),
        gemini_api_key=api_key,
        diarized_srt_file=True,
        speakers=speakers_map,
        language_codes=["ko-KR"],
    )

    # Print results
    print(f"\nCompleted {len(batch_result.results)} file(s):")
    for res in batch_result.results:
        print(f"\n* File: {res.input.input_file}")
        if res.status == gtw.TranscribeStatus.SUCCESS:
            print(f"  - Diarized SRT : {res.output.diarized_srt}")
            print(f"  - Plain SRT    : {res.output.srt}")
            print(f"  - TXT text     : {res.output.txt}")
        else:
            print(f"  - Failed: {res.error}", file=sys.stderr)


if __name__ == "__main__":
    main()
