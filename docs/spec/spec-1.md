# PRD & Implementation Specification: gemini-transcribe-wrapper (v1.3)

## 1. Project Overview

* Repository Name: gemini-transcribe-wrapper
* Package Name (PyPI): gemini-transcribe-wrapper
* CLI Command Entrypoint: gemini-transcribe
* Standard Recommended Invocation:
  uvx -q --from gemini-transcribe-wrapper gemini-transcribe --help
  uvx -q --from gemini-transcribe-wrapper gemini-transcribe "*.mp4"
* Python API: gemini_transcribe(...)
* Distribution Target: PyPI & GitHub (MIT License)
* Default Language: Korean (ko-KR)
* Core Philosophy: Zero-System-Prerequisite. Users do not need to manually install ffmpeg or other system binaries. It runs seamlessly on Linux, macOS, and Windows via uvx or pip install.
* Key Reliability Principle: Checkpoint/Resume support and Atomic Writes to prevent incomplete or corrupted outputs.

---

## 2. Zero-Config Cross-Platform Binary Management

System dependencies are eliminated by dynamically managing binaries and libraries at runtime.

1. Automatic ffmpeg / ffprobe Management (static-ffmpeg):
   * Eliminates OS-level package installation requirements (apt, brew, choco, etc.).
   * Bundles static-ffmpeg in dependencies to invoke static_ffmpeg.add_paths() on CLI startup, binding binaries to PATH dynamically.
2. Embedded ffsubsync:
   * Bundled as a direct Python dependency (ffsubsync) to be invoked via CLI subprocess or internal Python API.

---

## 3. System Architecture & Fault-Tolerant Pipeline

[Input Video/Audio] (*.mp4, *.mp3, etc.)
       |
       v
 [Environment Init] ---> Auto-download & bind static ffmpeg/ffprobe via static-ffmpeg
       |
       v
 [File Lock Check] (Acquire <file>.lck)
       |
       v
 [Check Skip Condition] (All target outputs exist and not --force)
       |
       v
 [Audio Normalization] ---> Standard MP3 (16kHz Mono via bundled ffmpeg)
       |
       v
 [Equal Chunk Splitting] ---> (20-25 min equal chunks: chunk_000.mp3 ~ chunk_NNN.mp3)
       |
       v
 [Checkpoint-based STT Loop] (Sequential Loop + 30s delay)
  |-- Check chunk_{idx}.metadata.json (If valid, SKIP API call & NO sleep)
  |-- Call Gemini 3.5 STT API (With Exponential Backoff)
  `-- Atomic Write: chunk_{idx}.metadata.json.tmp ---> chunk_{idx}.metadata.json
       |
       v
 [Merge & Alignment Pipeline] (All chunks verified complete)
  |-- Format .srt.tmp (Max 15 Korean chars/line, Max 2 lines/screen)
  |-- Format .spk.tmp (Speaker tags + Same layout rules as SRT)
  |-- Format .txt.tmp (Spellcheck/Filler removed, 60-char wrap, 2s/5s break rules)
  `-- ffsubsync Audio Alignment (Sync .srt.tmp with MP3, copy sync to .spk.tmp)
       |
       v
 [Atomic Commit & Cleanup]
  |-- Atomic Rename: *.tmp ---> final output files (.spk, .srt, .txt, .metadata.json)
  |-- Apply cleanup filters (--[no-]spk, --[no-]srt, --[no-]txt, --[no-]metadata)
  `-- Release <file>.lck

---

## 4. Detailed Technical Requirements

### 4.1. Resumability & Checkpointing

1. Chunk-Level Checkpoints (chunk_{idx}.metadata.json):
   * Save STT responses atomically to chunk_{idx}.metadata.json upon chunk completion.
   * On failure/resume:
     - Chunks with valid checkpoints skip API calls and bypass the 30-second delay.
     - Execution resumes from the first uncompleted chunk sequentially.
2. Chunk File Integrity:
   * Reuse existing valid chunk_{idx}.mp3 and temp_audio.mp3 files without re-extracting.

### 4.2. Atomic File Writes

1. Prevent Incomplete Outputs:
   * Target output files (.spk, .srt, .txt, .metadata.json) are created ONLY after all chunks, merging, formatting, and ffsubsync adjustments are 100% complete.
2. Write-to-Temp-and-Rename:
   * Write intermediate outputs to temporary files (*.tmp).
   * Perform atomic replacement via os.replace() upon final verification.
   * Ensure no corrupted or partial files remain if aborted.

### 4.3. Cross-Platform File Locking & Glob Expansion

1. Glob / Metapath Expansion:
   * Windows (cmd/PowerShell) compatibility: Expand incoming METAPATH patterns using glob.glob(p, recursive=True) at the Python level.
2. File Lock Mechanism (<input_file>.lck):
   * Use filelock to acquire <file_path>.lck before processing.
   * If lock acquisition fails (processed by another instance): Log a warning and skip to the next file.

### 4.4. Audio Normalization & Equal Chunk Splitting

1. Audio Conversion:
   * Convert inputs to 16kHz mono MP3 via bundled ffmpeg:
     ffmpeg -y -i <input> -vn -ar 16000 -ac 1 -b:a 64k <temp_audio>.mp3
2. Equal Split Algorithm:
   * Measure total audio duration T (seconds).
   * Target chunk size: pack each chunk to 1790s (29m50s) — the max safe
     length under the 30 min per-call cap — leaving the final chunk shorter.
   * If T <= 1790s: N = 1.
   * If T > 1790s: N = ceil(T / 1790); chunks 0..N-2 are 1790s, chunk N-1
     holds the remainder.

### 4.5. Rate Limiting & Gemini 3.5 Transcribe Integration

1. Authentication:
   * Read $GEMINI_API_KEYS (semicolon-separated) first; fall back to $GEMINI_API_KEY or $GOOGLE_API_KEY (single key, treated as a one-element list). CLI flags override env vars: `--gemini-api-keys=K1;K2;...` (preferred) or `--gemini-api-key=K1` (deprecated singular alias; emits a deprecation warning but is otherwise equivalent to a one-element list).
   * Multiple keys are cycled in round-robin order across chunks, advancing the pointer after every successful chunk.
   * On a 429 with retry hint, the wrapper sleeps (hint + 120s safety) and retries once on the same key; if the retry still 429s, it falls through to the next key. On a 429 without retry hint, the wrapper immediately tries the next key (no sleep). See [Multi-Key Strategy](../multi-key-strategy.md).
2. Free Tier Quota Compliance:
   * Sequential execution with a mandatory 30-second wait (time.sleep(30)) after each actual API call.
   * Exponential backoff retry logic on 429/503 errors (up to 5 retries).

### 4.6. Output Format Specifications

* .srt (Standard Subtitles): Max 15 Korean characters per line, max 2 lines per screen. Split cues at word boundaries if exceeded.
* .spk (Speaker Diarized Subtitles): Same layout rules as .srt with speaker tags (e.g., [Speaker 1] Hello).
* .txt (Editor / Notepad Formatted Text):
  * Grammar/spacing cleanup and filler words removal.
  * Line wrap at 60 characters (textwrap).
  * Silence gap >= --line-interval-secs (default: 2.0s): Single newline (\n).
  * Silence gap >= --paragraph-interval-secs (default: 5.0s): Paragraph break (\n\n).
  * Maximum of 1 consecutive blank line (no triple newlines).

### 4.7. Subtitle Sync Alignment (ffsubsync) & Cleanup

* Run ffsubsync using merged temporary subtitle (.srt.tmp) and full audio MP3.
* Replicate and apply the adjusted timestamp deltas to .spk.tmp.
* Cleanup rules:
  * --no-metadata (default): Delete all *.metadata.json files.
  * Delete respective files when --no-spk, --no-srt, or --no-txt is passed.
  * Delete temporary chunk files (chunk_*.mp3) upon successful completion.

---

## 5. Interface Specifications

### 5.1. Python API

```python
from gemini_transcribe_wrapper.models import BatchTranscribeResult

def gemini_transcribe(
    input_file: str,
    output_dir: str | None = None,
    output_stem: str | None = None,
    gemini_api_keys: list[str] | None = None,
    language: str = "ko-KR",
    diarize: bool = False,
    create_srt: bool = True,
    create_txt: bool = True,
    create_metadata_json: bool = False,
    create_transcript_json: bool = True,
    force: bool = False,
    line_interval_secs: float = 1.0,
    paragraph_interval_secs: float = 2.5,
    request_interval_secs: float = 60.0,
    chunk_secs: float | None = None,
    speakers: dict[str, str] | None = None,
    temp_path: str | None = "temp",
    ffsubsync_srt: bool = False,
) -> BatchTranscribeResult:
    """
    Transcribe a multimedia file using Gemini 3.5 Transcribe.
    Zero-config cross-platform (auto ffmpeg/ffsubsync dependencies).
    
    Returns:
        BatchTranscribeResult with per-input TranscribeResult items (input, output, leftover, status, error).
    """
    ...
```

