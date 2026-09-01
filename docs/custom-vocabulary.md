# Custom Vocabulary File

Register company-internal terms, product names, and frequently-misrecognized
phrases in a plain text file and have the wrapper bias the transcript toward
those terms as a post-recognition step.

## TL;DR

```bash
# myvoca.txt (one term per line, # for comments)
#Company Glossary
ProjectX
Gemini
internal-codename
수 있다
```

```bash
gtw --custom-vocabulary-file myvoca.txt sample.mp4
```

## Why this exists

Gemini Transcribe **rejects the `custom_vocabulary` API parameter** when
timestamps are requested (HTTP 400: *"custom_vocabulary is incompatible with
timestamps"*). This wrapper always needs word-level timestamps for SRT
output, so the vocabulary cannot be sent to the API directly.

Instead, the wrapper applies the registered terms as a post-recognition
**phrase replacement** (similar to how `fix_korean_su_text` corrects the
common Gemini 'su' → '수' misrecognition in Korean). This is the closest
approximation we can offer without sacrificing timestamped output.

## File format

- **UTF-8 plain text**, one term per line.
- Leading and trailing whitespace on a line is stripped.
- **Blank lines** and lines beginning with `#` (after stripping) are
  silently skipped — use them for section comments.
- No length limit on a single line, but Google recommends ≤100 lines for
  best results (the model itself accepts up to 1000 words; the
  recommendation is about quality, not the API limit).

### Example file

```
# Company glossary
ProjectX
Gemini Pro
internal-codename

# Frequently misrecognized
수 있다
수 없다
사내용어
```

## CLI

| Flag | Behavior |
| --- | --- |
| `--custom-vocabulary=TERM1,TERM2,...` | Inline list (or a path to a file — auto-detected by extension). Comma- or semicolon-separated. |
| `--custom-vocabulary-file=PATH` | Dedicated vocabulary file path. Always treated as a file. |

If both are provided, terms from both are **merged** (inline first, then
file), with duplicates removed implicitly by the bias algorithm (the
greedy match resolves overlaps).

## Missing-file behavior

If `--custom-vocabulary-file` points at a non-existent path, the wrapper
**prints a warning and continues with no vocabulary bias** — it does not
exit with an error. This keeps long-running batch jobs from failing on a
typo in the vocabulary path.

```
WARNING  gemini_transcribe_wrapper.api:api.py Custom vocabulary file not found: myvoca.txt. Ignoring.
```

To avoid even this warning, omit the flag entirely instead of pointing it
at an empty path.

## How the bias works

After Gemini returns the transcript, the wrapper applies
`apply_vocabulary_bias(text, vocabulary)`:

1. Terms are sorted **longest first** so multi-word phrases win over
   their single-word substrings.
2. Each term is matched against the transcript as a **regex with `\s+`**
   between whitespace-separated tokens, so `"수 있다"` matches
   `"수  있다"` / `"수\t있다"` (any run of whitespace counts as a single
   match boundary).
3. Matching is **case-insensitive**.
4. The first regex metacharacter in a term is **escaped** (`re.escape`),
   so `.` or `(` in vocabulary terms are treated as literals.
5. Matches are replaced with the **canonical form** from the vocabulary
   file (preserving the user's chosen casing).

The replacement is applied once per term in the vocabulary. The text is
not re-scanned after a substitution, so a vocabulary entry that mentions
another entry won't recurse infinitely.

### Worked examples

```
vocabulary = ["Gemini", "ProjectX", "수 있다"]

"I love gemini"              → "I love Gemini"
"projectx and Gemini"        → "ProjectX and Gemini"     (case-insensitive)
"할 수   있다"                → "할 수 있다"               (whitespace-tolerant)
"할 su 있다"                 → "할 su 있다"               (no match — vocab didn't include "su 있다")
```

## Python API

```python
from gemini_transcribe_wrapper import gemini_transcribe

batch = gemini_transcribe(
    input_file="interview.mp4",
    custom_vocabulary=["ProjectX", "Gemini"],          # inline list
    custom_vocabulary_file="myvoca.txt",                # file path
)
```

Both kwargs are optional and independent. They are merged in
`_process_one` before being handed to `TranscribeClient`.

## Limitations

- **No model-side bias.** This is a string-replacement step that runs
  *after* Gemini has already produced the transcript. It cannot teach
  the model to "hear" a new word correctly — it can only coerce the
  final text to use the registered spelling when a recognizer result
  is close enough to match.
- **No fuzzy match.** If Gemini returns `"프로젝트 X"` for vocabulary
  `"ProjectX"`, the bias will not catch it. We rely on the recognizer
  producing a substring that matches the vocabulary token after
  whitespace normalization and case folding.
- **CJK spacing sensitivity.** Korean phrases with attached particles
  (`"수있어요"` vs vocab `"수 있다"`) are not matched by the
  whitespace-tolerant regex, because the regex requires `\s+` between
  vocabulary tokens. To catch both `"수 있다"` and `"수있다"`, register
  both forms in the vocabulary file.
- **Vocabulary length.** Google's API accepts up to 1000 terms; the
  wrapper does not enforce any hard limit, but performance degrades
  with very large lists because each one runs a separate regex
  substitution across the full transcript. Keep the file ≤100 lines
  for best results.
- **No timestamps are biased.** Only the joined transcript text is
  touched. If Gemini assigns word timestamps to `"gemini"`, those
  timestamps remain attached to the same word position even after the
  text is rewritten to `"Gemini"`. SRT cue boundaries are unaffected.

## Examples

### Single-language glossary

```bash
# en-vocab.txt
# Acronyms the recognizer keeps mis-hearing
API
SDK
OAuth
TLS
ProjectX
Gemini Pro
```

```bash
gtw --custom-vocabulary-file en-vocab.txt recording.mp4
```

### Korean phrase corpus

```bash
# ko-vocab.txt
# 자주 틀리는 패턴
수 있다
수 없다
수도 있다
수가 있다
수밖에
```

```bash
gtw --custom-vocabulary-file ko-vocab.txt lecture.mp4
```

### Combining inline list + file

```bash
gtw --custom-vocabulary "ProjectX,ProjectY" \
    --custom-vocabulary-file extra-glossary.txt \
    sample.mp4
# Both sources are merged; bias is applied across the combined list.
```

## When to use this

- **Internal product / project names** that Gemini doesn't know about.
- **Acronyms** that the recognizer collapses to the wrong letter run.
- **Recurring misrecognitions** specific to your content (technical
  jargon, code-switching, etc.).

This is **not** a substitute for cleaning up audio quality, and it
won't help if Gemini doesn't recognize the word *at all* — it only
helps when the recognizer produces something close to the desired
spelling.