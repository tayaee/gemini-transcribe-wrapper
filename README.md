# gemini-transcribe-wrapper

Transcribe hours of audio for free with Google AI model `gemini-3.5-transcribe` — auto-chunked, rate-limit aware, and exported straight to SRT and TXT (with optional speaker diarization). [GitHub](https://github.com/tayaee/gemini-transcribe-wrapper) [PyPI](https://pypi.org/project/gemini-transcribe-wrapper/)

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
- **Rate Limit** (Max 2 RPM): Applies a built-in delay (default 30s) between sequential requests to prevent rate-limit errors.
- **Daily Quota** (Max 25 RPD or input-token quota): Tracks daily Pacific-time API usage locally (`~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`) and warns clearly when limits approach.
- **Raw Response to Ready-to-Use Subtitles**: Converts AI transcription output directly into `.srt`, `.txt`, and (with `--diarize`) `.diarized.srt` files in a single run.
- **Free-Tier-Friendly Defaults**: `--no-diarize` is the default to minimize API calls; the wrapper packs each call to the per-mode maximum.
- **429 = Stop the Batch**: On HTTP 429 (rate limit or quota exhausted), the wrapper prints retry suggestions and aborts the batch immediately. The CLI exits with code `2` to distinguish quota errors from other failures (code `1`).

## On Quota / Rate-Limit (HTTP 429)

The wrapper does **not** retry automatically on 429. When a 429 is detected it:

1. Logs the raw error and the quota category (free-tier daily quota vs short-term rate limit).
2. Prints retry suggestions: "wait about 1 minute for a short-term 429, or wait Xh Ym (sleep Zs) until PST midnight for the daily quota to reset, then re-run."
3. Aborts the rest of the batch (no point burning more calls on a guaranteed 429).
4. Returns exit code `2`.

Sample log on a quota hit:

```
ERROR Rate limit / quota exceeded (429): Error code: 429 - You exceeded your current quota ...
ERROR It looks like you hit the free tier daily quota (25 calls/day).
ERROR You hit the Gemini API rate limits:
  - max 2 API calls per minute
  - max 30 minutes of audio per call
  - max 25 API calls per day (free tier)
ERROR To retry: wait about 1 minute for a short-term 429, or wait 5h 40m (sleep 20400s) until PST midnight for the daily quota to reset, then re-run.
ERROR Switching to a paid tier (enable billing) removes the free-tier limits.
ERROR Aborting batch: quota / rate limit hit while processing <file>. Remaining files will not be processed.
```

## Batch Bulk Transcribing Tip

The free tier caps 25 calls/day per PST day. For multi-day batches, either:

- **Run in shorter bursts** so you naturally pause before hitting the quota, or
- **Swap in a fresh API key** (each key has its own counter under `~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`), or
- **Wait for the PST reset** — on a 429 the wrapper prints the exact sleep seconds needed; wrap your batch in a shell loop:

```bash
# Run, on quota hit wait until PST midnight, then re-run.
while ! gtw '*.mp4' --diarize; do
  echo "Batch hit a quota; waiting for PST reset..."
  # Read the sleep seconds from the previous log, or sleep 1h and retry.
  sleep 3600
done
```

Tip: `--no-diarize` halves (or better) the number of API calls vs `--diarize` for the same audio, since each call covers ~2× the audio length.

## Diarizing Tip (Speaker Labels)

`--diarize` produces `.diarized.srt` with raw speaker ids (`spk:0`, `spk:1`, ...). Map them to real names one pass at a time with `--speakers`: delete `.diarized.srt`, re-run with a more complete map, repeat until no `spk:` string remains.

```bash
# Pass 0: no --speakers — every cue keeps its raw spk:# tag.
gtw --diarize interview.mp4

# Wrapper logs the unmapped speakers and prints the re-render recipe:
# WARNING Some speakers are not covered by --speakers mapping.
#   Speaker map: spk:0 spk:1 spk:2
#   Unmapped: spk:0, spk:1, spk:2
# WARNING To re-render with names, delete the .diarized.srt and re-run with the
#   option, editing the Name# entries: rm 'interview.diarized.srt' &&
#   gtw 'interview.mp4' --diarize --speakers 'spk:0=Name0;spk:1=Name1;spk:2=Name2;'

# Pass 1: rename spk:0 → Host. Edit the recipe, delete the old .diarized.srt, re-run.
rm interview.diarized.srt
gtw --diarize interview.mp4 --speakers 'spk:0=Host;'

# Pass 2: add spk:1 → Guest. Same drill.
rm interview.diarized.srt
gtw --diarize interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;'

# Pass 3: add spk:2 → Interpreter. Now every tag is a real name.
rm interview.diarized.srt
gtw --diarize interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;spk:2=Interpreter;'
```

Simulation of the iterative renaming (`.diarized.srt` excerpt after each pass):

```
# Pass 0 (no --speakers): all raw ids
[spk:0] Hello everyone, welcome to the show.
[spk:1] Today we have a special guest with us.
[spk:2] Thanks for having me, it's great to be here.

# Pass 1 (--speakers 'spk:0=Host;')
[Host]     Hello everyone, welcome to the show.
[spk:1]    Today we have a special guest with us.
[spk:2]    Thanks for having me, it's great to be here.

# Pass 2 (add spk:1=Guest)
[Host]     Hello everyone, welcome to the show.
[Guest]    Today we have a special guest with us.
[spk:2]    Thanks for having me, it's great to be here.

# Pass 3 (add spk:2=Interpreter) — done, no spk: left
[Host]       Hello everyone, welcome to the show.
[Guest]      Today we have a special guest with us.
[Interpreter] Thanks for having me, it's great to be here.
```

Tip: each pass is a no-API re-render — the wrapper reads `<base>.diarized.transcript.json` (kept by default) and rewrites `.diarized.srt` from it, so iterating is fast and free.

Note: `--speakers` is ignored when `--diarize` is off (the default). Pass `--diarize` to enable speaker mapping.

## Relevant Repositories


## License

MIT
