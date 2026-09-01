# Language Hints (`--language-codes`)

Gemini Transcribe accepts an optional `language_codes` field in its
generation config to hint at which spoken languages are likely to appear
in the audio. This wrapper exposes that field as the
`--language-codes` CLI flag.

## TL;DR

```bash
# Default — hint Korean and English
gtw --gemini-api-keys $k1;...;$k10 *.mp4

# Pure English
gtw --language-codes en-US *.mp4

# Mixed Korean + Japanese + English (in priority order)
gtw --language-codes ko-KR;ja-JP;en-US *.mp4

# Auto language detection (let Gemini pick)
gtw --language-codes "" *.mp4
```

## CLI

| Flag | Behavior |
| --- | --- |
| `--language-codes=ko-KR;en-US` | Semicolon-separated list of BCP-47 codes. Forwarded verbatim to Gemini. Default: `ko-KR;en-US`. See [the supported languages list](https://ai.google.dev/gemini-api/docs/transcribe#supported-languages) for the full set of codes Gemini accepts. |
| `--language-codes=""` | Empty string → field is omitted from the request and Gemini auto-detects the spoken language. |

## Why hint languages?

Without a hint, Gemini Transcribe picks a single language from the
audio on its own. For mixed-language content (Korean with English
product names, code-switching interviews, etc.) the wrong primary
language can drop accuracy on the minority language.

A semicolon-separated list tells the model **all** the languages it should
be ready to recognize. Order is preserved — codes earlier in the list
are typically treated as the dominant language, but Gemini uses the
hint to bias recognition rather than as a hard requirement.

## Auto detection

If you don't know what's in the audio (or you expect a wide mix),
omit the hint entirely with `--language-codes=""`. The wrapper
then sends **no** `language_codes` field and Gemini picks the
language(s) automatically.

```bash
gtw --language-codes "" unknown_language.mp4
```

This is the safest choice for content where the language varies
file-by-file.

## Default

The default `--language-codes=ko-KR;en-US` reflects this project's
primary use case: Korean interviews / lectures / meetings that
sprinkle English product names and technical terms. If your content
is purely one language, override with `--language-codes=en-US` (or
any other single code) to avoid the model hedging between two
candidates.

For the full list of BCP-47 codes Gemini Transcribe accepts, see
<https://ai.google.dev/gemini-api/docs/transcribe#supported-languages>.

## Python API

```python
from gemini_transcribe_wrapper import gemini_transcribe

# Multi-language hint
batch = gemini_transcribe(
    input_file="interview.mp4",
    gemini_api_keys=["K1", "K2", "K3"],
    language_codes=["ko-KR", "en-US", "ja-JP"],
)

# Auto detection
batch = gemini_transcribe(
    input_file="mixed.mp4",
    gemini_api_keys=["K1"],
    language_codes=[],   # or None
)
```

## How it reaches Gemini

`TranscribeClient._generation_config()` decides which field to send:

```
if self.language_codes:           # list provided, even single-element
    transcription["language_codes"] = list(self.language_codes)
# else: field omitted → auto detection
```

The list is passed through verbatim (whitespace stripped, blanks
removed) — the wrapper does not validate BCP-47 format. An invalid
code returns an error from Gemini rather than from the wrapper.

## Caveats

- **Order is a hint, not a guarantee.** Gemini uses `language_codes`
  to bias recognition but doesn't promise that the first code is
  always chosen.
- **Auto detection can mis-identify.** If your content is mostly one
  language but occasionally drops a foreign word, a single-code hint
  (`--language-codes=en-US`) is usually more accurate than no hint
  at all.
- **The wrapper doesn't re-detect per chunk.** A long file is split
  into chunks; the same `language_codes` is sent for every chunk.
  This is intentional (chunk-level language can vary, but the API
  reuses the hint across the whole request) and matches Gemini's own
  behavior.
- **`--language` (single-code legacy flag) was removed.** Only
  `--language-codes` is supported. For a single-code hint, pass a
  one-element list (e.g. `--language-codes=en-US`).

## When to use what

| Scenario | Recommended |
| --- | --- |
| Korean content with English tech terms | `--language-codes=ko-KR;en-US` (default) |
| Pure single language | `--language-codes=ko-KR` (or `en-US`, etc.) |
| Code-switching between Korean / Japanese | `--language-codes=ko-KR;ja-JP` |
| Truly unknown / mixed language | `--language-codes=""` |
