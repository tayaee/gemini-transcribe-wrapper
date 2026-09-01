# gemini-transcribe-wrapper

A free video transcription CLI using gemini-3.5-transcribe that outputs .diarized.srt, .srt, and .txt files.

## Quick Start

### Prerequisites

- Get an API key from https://aistudio.google.com/api-keys/ for free.
- Install uv (https://docs.astral.sh/uv/getting-started/installation/).

### Install the tool

*(Supports Python 3.10–3.13; Python 3.12 is recommended)*

The wrapper is best run via **`uvx`** (one-shot, no install step — recommended
for free-tier multi-key usage where each run spreads load differently):

Linux / macOS:

```bash
# Set your multiple free-tier API keys in shell variables first:
export key1=AIzaSyA...
export key2=AIzaSyB...

# Then run any number of input files in one go:
uvx --python 3.12 --from gemini-transcribe-wrapper@latest gtw --gemini-api-keys $key1,...,$key10 *.mp4
```

For a persistent install (so you can run `gtw` directly without `uvx ...`):

```bash
export GEMINI_API_KEY=your_key_here
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw -v
```

Windows (Command Prompt) — persistent install:

```cmd
set GEMINI_API_KEY=your_key_here
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw -v
```

### Transcribe for free

```bash
# Recommended — multi-key (10 free-tier keys for active/cooldown pool):
uvx --python 3.12 --from gemini-transcribe-wrapper@latest gtw --gemini-api-keys $key1,...,$key10 *.mp4

# Single-key (if you only have one free-tier key or use a paid tier):
gtw sample.mp4   # with `GEMINI_API_KEY` set
# or
gtw --gemini-api-keys YOUR_API_KEY sample.mp4
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
- **Multi-Key Round-Robin + Active/Cooldown Pool**: Pass several keys with `--gemini-api-keys=KEY1,KEY2,...`. The wrapper keeps a separate `_active_pool` (round-robin target) and `_cooldown_pool` (keys that hit 429). On a 429 the key is moved into the cooldown pool immediately — no same-key retry. When the active pool drains, the wrapper sleeps `_COOLDOWN_SECS` (10 min default) and reactivates every cooldown key in batch, then retries the chunk. With ~15–20 free-tier keys you can run continuously even when most keys have hit the daily cap. See [Multi-Key Strategy](docs/multi-key-strategy.md) for details.
- **Graceful 429 Abort (single key)**: On HTTP 429 (rate limit or quota exhausted) when running with a single key, calculates the exact sleep seconds until the Pacific midnight reset and aborts the batch immediately (exit code `2`) to prevent wasting quota.
- **Custom Vocabulary File (`--custom-vocabulary-file`)**: Register company-internal / frequently-misrecognized terms in a plain text file (one per line) and the wrapper biases the transcript toward those terms as a post-recognition step. Up to 1000 words are accepted by the model; Google recommends ≤100 lines for best results. Missing file → warning + silently ignored. See [Custom Vocabulary](docs/custom-vocabulary.md) for details.

## Documentation & Guides

- [On Quota / Rate-Limit (HTTP 429)](docs/quota-and-rate-limits.md)
- [Multi-Key Strategy (active/cooldown pool)](docs/multi-key-strategy.md)
- [Custom Vocabulary File](docs/custom-vocabulary.md)
- [Batch Bulk Transcribing Tip](docs/batch-transcription.md)
- [Diarizing Tip (Speaker Labels)](docs/speaker-diarization.md)
- [Python API Examples](examples/)

## Relevant Repositories

- [GitHub](https://github.com/tayaee/gemini-transcribe-wrapper)
- [PyPI](https://pypi.org/project/gemini-transcribe-wrapper/)

## License

MIT
