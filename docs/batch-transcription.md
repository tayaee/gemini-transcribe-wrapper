# Batch Bulk Transcribing Tip

The free tier caps 25 calls/day per PT day. For multi-day batches, either:

- **Run in shorter bursts** so you naturally pause before hitting the quota, or
- **Swap in a fresh API key** (each key has its own counter under `~/.cache/gemini-transcribe-wrapper/usage-<sha256(key)[:12]>.json`), or
- **Wait for the PT reset** — on a 429 the wrapper prints the exact sleep seconds needed; wrap your batch in a shell loop:

```bash
# Run, on quota hit wait until PT midnight, then re-run.
while ! gtw '*.mp4'; do
  echo "Batch hit a quota; waiting for PT reset..."
  # Read the sleep seconds from the previous log, or sleep 1h and retry.
  sleep 3600
done
```

Tip: setting `--diarized-srt-file=off` halves (or better) the number of API calls vs default diarization for long audio, since each call covers ~2× the audio length.
