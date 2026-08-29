# gemini-transcribe-wrapper

Transcribe hours of audio for free with Google AI model `gemini-3.5-transcribe` — auto-chunked, rate-limit handled, and exported straight to diarized SRT and TXT.

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
gtw sample.mp4   // with `GEMINI_API_KEY` set
or
gtw --gemini-api-key YOUR_API_KEY sample.mp4
```

Output:

```bash
sample.diarized.srt
sample.srt
sample.txt
```

## What are improved by this project?

This wrapper automatically overcomes Google Gemini API's free tier limits and constraints (as of Aug 2026):
- Audio Length Limit (Max 30m/call): Automatically splits long audio into 29m50s optimal chunks, transcribes them, and merges them seamlessly with correct timestamps.
- Rate Limit (Max 2 RPM): Applies a built-in delay (default 30s) between sequential requests to prevent rate-limit errors.
- Daily Quota (Max 25 RPD): Tracks daily Pacific-time API usage locally (~/.cache/.../usage.json) and warns clearly before or upon hitting HTTP 429 quota limits.
- Raw Response to Ready-to-Use Subtitles: Converts AI transcription output directly into speaker-diarized .diarized.srt, standard .srt, and clean .txt files in a single run.
- Multi-day Batch Wait (--free-tier-wait-on-429): On 429 or when 25 free-tier calls/day (PST) are reached, sleep until PST midnight in 1-hour chunks (logging the remaining time) and resume. Designed for unattended multi-day batch runs on the free tier.

## Batch Bulk Transcribing Tip

The free tier caps 25 calls/day per PST day, so a multi-day batch on the free tier needs to wait for the quota to reset. Run it once with `--free-tier-wait-on-429` and leave it; the wrapper will log the remaining time, sleep in 1-hour chunks until PST midnight, and resume on its own.

```bash
# Transcribe every .mp4 in the current folder, waiting for the quota to reset as needed.
gtw '*.mp4' --free-tier-wait-on-429
```

Sample log on a quota hit:

```
WARNING Free-tier wait: 6.3h remaining until PST midnight; sleeping 3600s.
WARNING Free-tier wait: 5.3h remaining until PST midnight; sleeping 3600s.
...
WARNING Free-tier wait: PST midnight reached (quota reset); resuming API calls.
```

Tip: per-key daily counts are tracked under `~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`, so swapping in a fresh API key effectively doubles the daily budget.

## Diarizing Tip

`.diarized.srt` is initially tagged with raw speaker ids (`spk:0`, `spk:1`, ...). Map them to real names one pass at a time with `--speakers`: delete `.diarized.srt`, re-run with a more complete map, repeat until no `spk:` string remains.

```bash
# Pass 0: no --speakers — every cue keeps its raw spk:# tag.
gtw interview.mp4

# Wrapper logs the unmapped speakers and prints the re-render recipe:
# WARNING Some speakers are not covered by --speakers mapping.
#   Speaker map: spk:0 spk:1 spk:2
#   Unmapped: spk:0, spk:1, spk:2
# WARNING To re-render with names, delete the .diarized.srt and re-run with the
#   option, editing the Name# entries: rm 'interview.diarized.srt' &&
#   gtw 'interview.mp4' --speakers 'spk:0=Name0;spk:1=Name1;spk:2=Name2;'

# Pass 1: rename spk:0 → Host. Edit the recipe, delete the old .diarized.srt, re-run.
rm interview.diarized.srt
gtw interview.mp4 --speakers 'spk:0=Host;'

# Pass 2: add spk:1 → Guest. Same drill.
rm interview.diarized.srt
gtw interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;'

# Pass 3: add spk:2 → Interpreter. Now every tag is a real name.
rm interview.diarized.srt
gtw interview.mp4 --speakers 'spk:0=Host;spk:1=Guest;spk:2=Interpreter;'
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

Tip: each pass is a no-API re-render — the wrapper reads `<base>.transcript.json` (kept by default) and rewrites `.diarized.srt` from it, so iterating is fast and free.

## Relevant Repositories

- GitHub: https://github.com/tayaee/gemini-transcribe-wrapper
- PyPI: https://pypi.org/project/gemini-transcribe-wrapper/

## License

MIT
