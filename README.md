# gemini-transcribe-wrapper

Transcribe hours of audio for free with Google AI model `gemini-3.5-transcribe` — auto-chunked, rate-limit aware, and exported straight to SRT and TXT (with optional speaker diarization).

## Quick Start

### Prerequisites

- Get an API key from https://aistudio.google.com/api-keys/ for free.
- Install uv (https://docs.astral.sh/uv/getting-started/installation/).

### Install the tool

Linux / macOS:

```bash
export GEMINI_API_KEY=your_key_here
uv tool install --python 3.13 gemini-transcribe-wrapper@latest
gtw -v
```

Windows (Command Prompt):

```cmd
set GEMINI_API_KEY=your_key_here
uv tool install --python 3.13 gemini-transcribe-wrapper@latest
gtw -v
```

### Transcribe for free

```bash
gtw sample.mp4   # with `GEMINI_API_KEY` set
# or
gtw --gemini-api-key YOUR_API_KEY sample.mp4
```

Output (default: `--no-diarize` — no speaker labels, fewer API calls):

```bash
sample.transcript.json
sample.srt
sample.txt
```

For speaker-diarized output, opt in with `--diarize`:

```bash
gtw --diarize sample.mp4
```

Output (with `--diarize`):

```bash
sample.diarized.transcript.json
sample.diarized.srt
sample.srt
sample.txt
```

## What are improved by this project?

This wrapper automatically overcomes Google Gemini API's free tier limits and constraints (as of Aug 2026):

- **Audio Length Limit** — depends on whether you ask for speaker diarization:
  - `--no-diarize` (default): up to ~60 min per API call. The wrapper cuts the file into 59-min logical units, each sent as one API call.
  - `--diarize`: up to ~30 min per API call. The wrapper cuts into 29-min chunks to stay under the limit with a 1-min safety margin.
- **Rate Limit** (Max 2 RPM): Applies a monotonic rate limiter (default 60s interval) across all chunks and multi-file batches to prevent 429 rate-limit errors.
- **Daily Quota Tracking**: Tracks daily Pacific-time API usage locally (`~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`) with masked key logging (e.g. `API calls today 2026-08-30 (PST-08:00) with key 'AIza****abcd': attempted 3 (free tier limit: ~5)`).
- **Consolidated Temp Directory**: Intermediate chunks, converted audio, and resume checkpoints are placed inside `temp/<output_base>.gemini-work` (configurable via `--temp-dir`, default: `temp`), automatically cleaned up on success.
- **Raw Response to Ready-to-Use Subtitles**: Converts AI transcription output directly into `.srt`, `.txt`, and (with `--diarize`) `.diarized.srt` files in a single run.
- **Free-Tier-Friendly Defaults**: `--no-diarize` is the default to minimize API calls; the wrapper packs each call to the per-mode maximum.
- **429 = Stop the Batch**: On HTTP 429 (rate limit or quota exhausted), the wrapper prints retry suggestions and aborts the batch immediately. The CLI exits with code `2` to distinguish quota errors from other failures (code `1`).

## Documentation & Guides

- [On Quota / Rate-Limit (HTTP 429)](docs/quota-and-rate-limits.md)
- [Batch Bulk Transcribing Tip](docs/batch-transcription.md)
- [Diarizing Tip (Speaker Labels)](docs/speaker-diarization.md)
- [Python API Samples](samples/)

## Relevant Repositories

- [GitHub](https://github.com/tayaee/gemini-transcribe-wrapper)
- [PyPI](https://pypi.org/project/gemini-transcribe-wrapper/)

## License

MIT
