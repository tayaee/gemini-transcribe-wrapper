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
sample.speakers.srt
sample.srt
sample.txt
```

## What are improved by this project?

This wrapper automatically overcomes Google Gemini API's free tier limits and constraints (as of Aug 2026):
- Audio Length Limit (Max 30m/call): Automatically splits long audio into 29m50s optimal chunks, transcribes them, and merges them seamlessly with correct timestamps.
- Rate Limit (Max 2 RPM): Applies a built-in delay (default 30s) between sequential requests to prevent rate-limit errors.
- Daily Quota (Max 25 RPD): Tracks daily Pacific-time API usage locally (~/.cache/.../usage.json) and warns clearly before or upon hitting HTTP 429 quota limits.
- Raw Response to Ready-to-Use Subtitles: Converts AI transcription output directly into speaker-diarized .speakers.srt, standard .srt, and clean .txt files in a single run.
- Multi-day Batch Wait (--free-tier-wait-on-429): On 429 or when 25 free-tier calls/day (PST) are reached, sleep until PST midnight in 1-hour chunks (logging the remaining time) and resume. Designed for unattended multi-day batch runs on the free tier.

## Relevant Repositories

- GitHub: https://github.com/tayaee/gemini-transcribe-wrapper
- PyPI: https://pypi.org/project/gemini-transcribe-wrapper/

## License

MIT
