# gemini-transcribe-wrapper

A free video transcription CLI using gemini-3.5-transcribe that outputs .diarized.srt, .srt, and .txt files.

## Quick Start

### Prerequisites

- Get an API key from https://aistudio.google.com/api-keys/ for free.
- Install uv (https://docs.astral.sh/uv/getting-started/installation/).

### Install or upgrade the tool

*(Supports Python 3.10–3.13; Python 3.12 is recommended)*

#### Method 1: The wrapper is best run via **`uvx`** (one-shot, no install step — recommended for free-tier multi-key usage where each run spreads load differently):

Linux / macOS / Windows:

```bash
uvx --python 3.12 --from gemini-transcribe-wrapper@latest gtw --gemini-api-keys "<api keys separated by comma or semicolon>" -h
```

#### Method 2: For a persistent install (so you can run `gtw` directly without `uvx ...`):

Linux / macOS / Windows:

```bash
uv tool install --python 3.12 gemini-transcribe-wrapper@latest
gtw --gemini-api-keys "<api keys separated by comma or semicolon>" -h
```

### Transcribe for free

```bash
gtw --gemini-api-keys "<api keys separated by comma or semicolon>" /path/to/sample.mp4
```

Output (default: `--diarized-srt-file auto` — with speaker diarization):

```bash
sample.diarized.transcript.json
sample.diarized.srt
sample.srt
sample.txt
```

## What are improved by this project?

### 1. Transcription Quality & Usability

- **Raw AI Output to Ready-to-Use Subtitles**: Converts JSON transcription output directly into formatted `.srt` (with natural timestamp alignment and line breaking), readable `.txt` paragraphs, and `.diarized.srt` files in a single run.
- **Korean Transcription Error Correction**: Automatically post-processes common Gemini Korean misrecognitions (e.g. correcting erroneous romanized `"su"` into `"수"` in patterns like `"~할 su 있다/없다"`, `"~할 su밖에"`, `"~할 su도"`, `"~할 su가"` with proper Korean spacing and particle attachment).
- **Fast Speaker Labeling**: Allows iterative speaker renaming (`--speakers-txt-file`, automatically picking up `.speakers.txt`) instantly without re-calling the API by reusing saved transcripts.

### 2. Overcoming Free-Tier Limits & Constraints

- **Audio Length Limit Bypass**: Handles audio files of any length by auto-splitting into safe units (30-min chunks by default for subtitles, 60-min chunks when only `.txt` is output via `--diarized-srt-file=off --srt-file=off`) and transparently stitching timestamps together.
- **Rate Limit Throttling (Max 2 RPM)**: Applies a monotonic rate limiter (default 120s interval on free tier) across all chunks and multi-file batches to prevent 429 rate-limit errors.
- **Daily Quota Tracking**: Tracks daily Pacific-time API usage locally (`~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`) with masked key logging (e.g. `API call attempts today 2026-08-30 (PT) with key 'AIza****abcd': attempted 3 (free tier limit: ~25)`).
- **Multi-Key Round-Robin + Active/Cooldown Pool**: Pass multiple keys with `--gemini-api-keys=KEY1,KEY2,...`. The wrapper tracks active keys and keys in cooldown upon 429 errors. When all keys hit 429, it waits 600s before reloading keys into the live pool and retrying. Key usage state is persisted across runs in `~/.cache/gemini-transcribe-wrapper/last-used-api-key.json` to rotate evenly. With **16–20 free-tier keys** (2 Gmail accounts × 10 projects each) you can run continuously even when most keys have hit the daily cap. See [Multi-Key Strategy](docs/multi-key-strategy.md) for sizing guidance.
- **Key File with Hot Reload**: `--gemini-api-keys-file PATH` reads one key per line (defaults to `./gemini-api-keys.txt`, falling back to `~/.config/gemini-transcribe-wrapper/gemini-api-keys.txt`). On Linux/macOS the file must be `chmod 600` or the run aborts with the exact fix command. The file is watched during the run — edit it to add or retire keys and the rotation reloads every key before the next pick, resuming at the key that follows the last used one in the new file order.
- **Graceful 429 Abort (single key)**: On HTTP 429 (rate limit or quota exhausted) when running with a single key, calculates the exact sleep seconds until the Pacific midnight reset and aborts the batch gracefully.
- **Custom Vocabulary File (`--vocab-txt-file`)**: Register company-internal / frequently-misrecognized terms in a plain text file (one per line) and the wrapper biases the transcript toward those terms as a post-recognition step. Defaults to `auto` which automatically picks up `.vocab.txt` (or `<stem>.vocab.txt`) when present. Up to 1000 words are accepted by the model; Google recommends ≤100 lines for best results. See [Custom Vocabulary](docs/custom-vocabulary.md) for details.
- **Multi-Language Hint (`--language-codes`)**: Forward a comma-separated list of BCP-47 codes (default `ko-KR;en-US`) to Gemini as `language_codes`. Pass an empty string (`--language-codes=""`) to enable Gemini's auto language detection for mixed-language content. See [Language Hints](docs/language-codes.md) for details.

## Documentation & Guides

- [On Quota / Rate-Limit (HTTP 429)](docs/quota-and-rate-limits.md)
- [Multi-Key Strategy (active/cooldown pool)](docs/multi-key-strategy.md)
- [Custom Vocabulary File](docs/custom-vocabulary.md)
- [Language Hints (`--language-codes`)](docs/language-codes.md)
- [Batch Bulk Transcribing Tip](docs/batch-transcription.md)
- [Diarizing Tip (Speaker Labels)](docs/speaker-diarization.md)

## Relevant Repositories

- [https://github.com/tayaee/gemini-transcribe-wrapper](https://github.com/tayaee/gemini-transcribe-wrapper)
- [https://pypi.org/project/gemini-transcribe-wrapper](https://pypi.org/project/gemini-transcribe-wrapper/)

## License

MIT
