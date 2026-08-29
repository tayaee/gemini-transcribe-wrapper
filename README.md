# gemini-transcribe-wrapper

Zero-config wrapper for free-tier Gemini 3.5 Transcribe that breaks long audio into
API-safe chunks and merges the results back into `.speakers.srt` / `.srt` / `.txt`.

- GitHub: https://github.com/tayaee/gemini-transcribe-wrapper
- PyPI: https://pypi.org/project/gemini-transcribe-wrapper/

## Quick Start

### Prerequisites

- Get an API key for free from https://aistudio.google.com/api-keys/.
- Install uv (https://docs.astral.sh/uv/getting-started/installation/).

### Install the tool

Linux / macOS:

```bash
export GEMINI_API_KEY=your_key_here
uv tool install --python 3.13 gemini-transcribe-wrapper@latest
gtw -v
```

Windows (PowerShell):

```powershell
$env:GEMINI_API_KEY = "your_key_here"
uv tool install --python 3.13 gemini-transcribe-wrapper@latest
gtw -v
```

Windows (Command Prompt):

```cmd
set GEMINI_API_KEY=your_key_here
uv tool install --python 3.13 gemini-transcribe-wrapper@latest
gtw -v
```

### Transcribe for free (see Limites below)

With `GEMINI_API_KEY` set (from step 2):

```bash
gtw sample.mp4
```

Or pass the key directly:

```bash
gtw --gemini-api-key YOUR_API_KEY sample.mp4
gtw --gemini-api-key YOUR_API_KEY *.mp4
```

Done: `sample.speakers.srt`, `sample.srt`, `sample.txt` appear next to the input.
Any local video/audio works (`.mp4`, `.mp3`, etc.).

## Limits

This wrapper exists to get past the Gemini free tier limits:

- Max 2 API calls per minute: chunks are transcribed sequentially with a
  built-in delay (default 30s) between API calls.
- Max 30 minutes of audio per call: long files are split into 29m50s
  chunks (packed to the max so the 25 calls/day budget is used efficiently),
  transcribed, then merged.
- Max 25 API calls per day: once the daily quota is used up, the API
  returns HTTP 429. If you hit it, the tool prints the limits above:
  free tier users should try again tomorrow, or switch to a paid tier
  (enable billing) to keep going immediately.

Usage tracking: every API call is counted per day (PST, resets at midnight
Pacific) and saved to `~/.cache/gemini-transcribe-wrapper/usage.json`. Every
run (`gtw -v`, `gtw --help`, or after transcription) prints today's count on
the last line, e.g.
`2026-08-28T220317-08:00 (PST) API calls today: 3/25 (free tier limit: 25)`.

Chunked transcription keeps the same global timeline as a single pass (word
timestamps match within 0.5s; verified by `verify-chunk-secs.sh`). If you want
an ffsubsync-aligned SRT for manual comparison, pass `--ffsubsync-srt` to also
write `<base>.ffsubsync.srt` (aligned to the full audio via
`uvx --python 3.13 ffsubsync <audio> -i <srt> --max-offset-seconds=120
--gss --overwrite-input`); the main `.srt`/`.speakers.srt` always keep the
raw transcript timestamps.

`.speakers.srt` (speaker diarization, still a standard .srt for media
players), `.srt` (no speaker labels), and `.txt`
(clean text) are all generated at once by default. Don't need one? Turn it
off:

```bash
gtw sample.mp4 --no-speakers-srt --no-txt   # keep only .srt
gtw sample.mp4 --no-srt                     # keep .speakers.srt + .txt
```

See `gtw --help` for all options (speaker name
mapping, transcript re-rendering, temp dir, etc.).

## License

MIT
