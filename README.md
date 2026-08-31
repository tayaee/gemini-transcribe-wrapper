# gemini-transcribe-wrapper

A free video transcription CLI using gemini-3.5-transcribe that outputs .diarized.srt, .srt, and .txt files.

## Quick Start

### Prerequisites

- Get an API key from https://aistudio.google.com/api-keys/ for free.
- Install uv (https://docs.astral.sh/uv/getting-started/installation/).

### Install the tool

*(Supports Python 3.10–3.13; Python 3.12 is recommended)*

Linux / macOS:

```bash
export GEMINI_API_KEY=your_key_here
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw -v
```

Windows (Command Prompt):

```cmd
set GEMINI_API_KEY=your_key_here
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw -v
```

### Transcribe for free

```bash
gtw sample.mp4   # with `GEMINI_API_KEY` set
# or
gtw --gemini-api-key YOUR_API_KEY sample.mp4
# or, with multiple keys for round-robin + 429 fallback:
gtw --gemini-api-keys KEY1,KEY2,KEY3 sample.mp4
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

### 1. Transcription Quality & Usability

- **Raw AI Output to Ready-to-Use Subtitles**: Converts JSON transcription output directly into formatted `.srt` (with natural timestamp alignment and line breaking), readable `.txt` paragraphs, and (with `--diarize`) `.diarized.srt` files in a single run.
- **Korean Transcription Error Correction**: Automatically post-processes common Gemini Korean misrecognitions (e.g. correcting erroneous romanized `"su"` into `"수"` in patterns like `"~할 su 있다/없다"`, `"~할 su밖에"`, `"~할 su도"`, `"~할 su가"` with proper Korean spacing and particle attachment).
- **Fast Speaker Labeling**: Allows iterative speaker renaming (`--speakers 'spk:0=Host;spk:1=Guest'`) instantly without re-calling the API by reusing saved transcripts.

### 2. Overcoming Free-Tier Limits & Constraints

- **Audio Length Limit Bypass**: Handles audio files of any length by auto-splitting into safe units (59-min logical units for `--no-diarize`, 29-min chunks for `--diarize`) and transparently stitching timestamps together.
- **Rate Limit Throttling (Max 2 RPM)**: Applies a monotonic rate limiter (default 60s interval) across all chunks and multi-file batches to prevent 429 rate-limit errors.
- **Daily Quota Tracking**: Tracks daily Pacific-time API usage locally (`~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`) with masked key logging (e.g. `API calls today 2026-08-30 (PST-08:00) with key 'AIza****abcd': attempted 3 (free tier limit: ~25)`).
- **Free-Tier-Friendly Defaults**: `--no-diarize` is the default to maximize audio duration per API call and minimize API consumption.
- **Multi-Key Round-Robin + 429 Fallback**: Pass several keys with `--gemini-api-keys=KEY1,KEY2,...`. The wrapper cycles keys in round-robin order across chunks; on a 429 with retry hint it cools down on the current key once and retries, then falls through to the next key. All keys exhausted → graceful abort with exit code `2`. See [Multi-Key Strategy](docs/multi-key-strategy.md) for details.
- **Graceful 429 Abort**: On HTTP 429 (rate limit or quota exhausted) when running with a single key, calculates the exact sleep seconds until the Pacific midnight reset and aborts the batch immediately (exit code `2`) to prevent wasting quota.

## Documentation & Guides

- [On Quota / Rate-Limit (HTTP 429)](docs/quota-and-rate-limits.md)
- [Multi-Key Strategy (round-robin + 429 fallback)](docs/multi-key-strategy.md)
- [Batch Bulk Transcribing Tip](docs/batch-transcription.md)
- [Diarizing Tip (Speaker Labels)](docs/speaker-diarization.md)
- [Python API Examples](examples/)

## Relevant Repositories

- [GitHub](https://github.com/tayaee/gemini-transcribe-wrapper)
- [PyPI](https://pypi.org/project/gemini-transcribe-wrapper/)

## License

MIT
