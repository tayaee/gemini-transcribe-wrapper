# gemini-transcribe-wrapper

Zero-config wrapper for Gemini 3.5 Transcribe that breaks long audio into
API-safe chunks and merges the results back into `.speakers.srt` / `.srt` / `.txt`.

- GitHub: https://github.com/tayaee/gemini-transcribe-wrapper
- PyPI: https://pypi.org/project/gemini-transcribe-wrapper/

## Quick Start

1. Get an API key

Sign up at [Google AI Studio](https://aistudio.google.com/) and create an API key.

2. Install the tool

Linux / macOS:

```bash
export GEMINI_API_KEY=your_key_here
uvx -q gemini-transcribe-wrapper --help
```

Windows (PowerShell):

```powershell
$env:GEMINI_API_KEY = "your_key_here"
uvx -q gemini-transcribe-wrapper --help
```

Windows (Command Prompt):

```cmd
set GEMINI_API_KEY=your_key_here
uvx -q gemini-transcribe-wrapper --help
```

3. Transcribe

With `GEMINI_API_KEY` set (from step 2):

```bash
uvx -q gemini-transcribe-wrapper sample.mp4
```

Or pass the key directly:

```bash
uvx -q gemini-transcribe-wrapper --gemini-api-key YOUR_API_KEY sample.mp4
uvx -q gemini-transcribe-wrapper --gemini-api-key YOUR_API_KEY *.mp4
```

Done: `sample.speakers.srt`, `sample.srt`, `sample.txt` appear next to the input.
Any local video/audio works (`.mp4`, `.mp3`, etc.).

## How it works

This wrapper exists to get past the Gemini free tier limits:

- Max 2 API calls per minute: chunks are transcribed sequentially with a
  built-in delay (default 30s) between API calls.
- Max 30 minutes of audio per call: long files are split into equal
  ≤25 min chunks, transcribed, then merged and aligned with `ffsubsync`.
- Max 25 API calls per day: once the daily quota is used up, the API
  returns HTTP 429. If you hit it, the tool prints the limits above:
  free tier users should try again tomorrow, or switch to a paid tier
  (enable billing) to keep going immediately.

`.speakers.srt` (speaker diarization, still a standard .srt for media
players), `.srt` (no speaker labels), and `.txt`
(clean text) are all generated at once by default. Don't need one? Turn it
off:

```bash
uvx -q gemini-transcribe-wrapper sample.mp4 --no-speakers-srt --no-txt   # keep only .srt
uvx -q gemini-transcribe-wrapper sample.mp4 --no-srt                     # keep .speakers.srt + .txt
```

See `uvx -q gemini-transcribe-wrapper --help` for all options (speaker name
mapping, transcript re-rendering, temp dir, etc.).

## Shortcut: install once, use `gtw`

Typing `uvx -q gemini-transcribe-wrapper ...` each time is verbose. If you want
a short command, install the tool once and use the `gtw` shortcut: the exact
same tool, installed alongside it.

Linux / macOS:

```bash
uv -q tool install gemini-transcribe-wrapper
export GEMINI_API_KEY=your_key_here
gtw sample.mp4
```

Windows (PowerShell):

```powershell
uv -q tool install gemini-transcribe-wrapper
$env:GEMINI_API_KEY = "your_key_here"
gtw sample.mp4
```

After `uv tool install`, both `gemini-transcribe-wrapper` and `gtw` work
interchangeably.

## License

MIT
