# Integration Tests

Tests in this directory perform real end-to-end API calls against Google Gemini API using live multimedia files.

## Prerequisites

- Set your real Gemini API key:
  ```bash
  export GEMINI_API_KEY="your-real-api-key"
  ```

## Available Integration Tests

- `verify-chunk-secs.sh`: Transcribes real sample video (`examples/안될과학 개똥벌레.mp4`) with both single chunk and `--chunk-secs 60`, then verifies timestamp alignment.
