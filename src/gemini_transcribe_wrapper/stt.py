"""Gemini 3.5 Transcribe client wrapper with checkpointed chunk transcription."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors
from google.genai._gaos.types import interactions as _gaos_interactions

logger = logging.getLogger(__name__)

MODEL_ID = "gemini-3.5-transcribe"

_OFFSET_RE = re.compile(r"([0-9.]+)s")


def _parse_offset(offset: str | None) -> float:
    """Parse an offset string like '12.345s' into seconds (0.0 on missing)."""
    if not offset:
        return 0.0
    m = _OFFSET_RE.match(offset.strip())
    if not m:
        try:
            return float(offset)
        except ValueError:
            return 0.0
    return float(m.group(1))


@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: str | None = None


@dataclass
class TranscriptionResult:
    text: str
    words: list[Word] = field(default_factory=list)


def _extract_words(interaction: Any) -> list[Word]:
    """Extract word-level annotations from an interaction.

    Supports two response shapes:
    1. word_info annotations on text content (current GAOS SDK)
    2. a `transcription` object with .words (WordInfo: word/start_offset/end_offset)
    """
    from .format import _sanitize_word

    words: list[Word] = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for content in getattr(step, "content", None) or []:
            if getattr(content, "type", None) != "text":
                continue
            for annotation in getattr(content, "annotations", None) or []:
                if getattr(annotation, "type", None) == "word_info":
                    words.append(
                        _sanitize_word(
                            Word(
                                text=getattr(annotation, "text", "") or "",
                                start=_parse_offset(getattr(annotation, "start_offset", None)),
                                end=_parse_offset(getattr(annotation, "end_offset", None)),
                                speaker=getattr(annotation, "speaker", None),
                            )
                        )
                    )
            transcription = getattr(content, "transcription", None)
            if transcription is not None and not words:
                for w in getattr(transcription, "words", None) or []:
                    words.append(
                        _sanitize_word(
                            Word(
                                text=getattr(w, "word", "") or getattr(w, "text", "") or "",
                                start=_parse_offset(getattr(w, "start_offset", None)),
                                end=_parse_offset(getattr(w, "end_offset", None)),
                                speaker=getattr(w, "speaker", None)
                                or getattr(transcription, "speaker_label", None),
                            )
                        )
                    )
    return words


def _is_retryable(exc: Exception) -> bool:
    # 429 (rate limit / quota) is NOT retried: backoff won't help a daily
    # quota, and we want to surface the friendly hint immediately.
    if _is_quota_error(exc):
        return False
    if isinstance(exc, (errors.APIError,)) and exc.code is not None:
        return exc.code in (500, 502, 503, 504)
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _is_quota_error(exc: Exception) -> bool:
    """True for 429 Too Many Requests (rate limit / daily quota exceeded)."""
    message = str(exc)
    if "429" in message or "quota" in message.lower():
        return True
    if isinstance(exc, errors.APIError):
        return exc.code == 429
    return False


MODEL_REF_URL = "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe"

_QUOTA_HINT = (
    "You hit the Gemini API rate limits:\n"
    "  - max 2 API calls per minute\n"
    "  - max 30 minutes of audio per call\n"
    "  - max 25 API calls per day (free tier)\n"
    f"Reference: {MODEL_REF_URL}"
)


def _log_quota_hint(exc: Exception) -> None:
    if not _is_quota_error(exc):
        return
    logger.error("Rate limit / quota exceeded (429): %s", exc)
    message = str(exc).lower()
    if "free_tier" in message or "daily" in message:
        logger.error(
            "It looks like you hit the free tier daily quota (25 calls/day).\n"
            "  - Free tier: try again tomorrow, or\n"
            "  - Switch to a paid tier (enable billing) to keep going now."
        )
    else:
        logger.error(
            "It looks like you hit a short-term rate limit.\n"
            "  - Free tier: wait a bit and retry, or\n"
            "  - Switch to a paid tier (enable billing) for higher limits."
        )
    logger.error(_QUOTA_HINT)


def _sleep_backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> None:
    delay = min(cap, base * (2 ** attempt))
    logger.warning("Retrying in %.1fs (attempt %d)", delay, attempt + 1)
    time.sleep(delay)


class TranscribeClient:
    def __init__(
        self,
        api_key: str | None = None,
        language: str = "ko-KR",
        enable_diarization: bool = True,
        max_retries: int = 5,
        free_tier_wait_on_429: bool = False,
    ) -> None:
        self.api_key = api_key
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.language = language
        self.enable_diarization = enable_diarization
        self.max_retries = max_retries
        self.free_tier_wait_on_429 = free_tier_wait_on_429
        self.api_logs: list[dict[str, Any]] = []

    def _generation_config(self) -> _gaos_interactions.GenerationConfig:
        transcription: dict[str, Any] = {}
        if self.language:
            transcription["language_codes"] = [self.language]
        mode: dict[str, Any] = {"type": "verbatim"}
        if self.enable_diarization:
            mode["diarization_mode"] = "speaker"
        mode["timestamp_granularities"] = ["word"]
        transcription["mode"] = mode
        return _gaos_interactions.GenerationConfig(
            transcription_config=_gaos_interactions.TranscriptionConfig(**transcription)
        )

    def _log_api_call(
        self,
        chunk_index: int,
        attempts: int,
        started_at: str,
        duration_secs: float,
        status: str,
        error: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "chunk_index": chunk_index,
            "attempts": attempts,
            "started_at": started_at,
            "duration_secs": round(duration_secs, 3),
            "status": status,
        }
        if error:
            entry["error"] = error
        self.api_logs.append(entry)

    def transcribe_chunk(self, chunk_mp3: Path, chunk_index: int = 0) -> TranscriptionResult:
        """Transcribe a single MP3 chunk with exponential-backoff retries."""
        started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        attempts = 0
        last_exc: Exception | None = None
        uploaded = self.client.files.upload(file=str(chunk_mp3))
        try:
            for attempt in range(self.max_retries + 1):
                attempts = attempt + 1
                attempt_start = time.monotonic()
                try:
                    from .usage_counter import increment_today

                    increment_today(api_key=self.api_key)
                    interaction = self.client.interactions.create(
                        model=MODEL_ID,
                        input=[
                            {
                                "type": "audio",
                                "uri": uploaded.uri,
                                "mime_type": "audio/mpeg",
                            }
                        ],
                        generation_config=self._generation_config(),
                    )
                    duration = time.monotonic() - attempt_start
                    text = getattr(interaction, "output_text", None) or ""
                    words = _extract_words(interaction)
                    if not text and words:
                        text = " ".join(w.text for w in words)
                    self._log_api_call(
                        chunk_index, attempts, started_at, duration, "success"
                    )
                    return TranscriptionResult(text=text, words=words)
                except Exception as exc:
                    last_exc = exc
                    if attempt >= self.max_retries or not _is_retryable(exc):
                        _log_quota_hint(exc)
                        if not _is_quota_error(exc):
                            logger.error(
                                "Gemini API call failed. Reference: %s", MODEL_REF_URL
                            )
                        self._log_api_call(
                            chunk_index,
                            attempts,
                            started_at,
                            time.monotonic() - attempt_start,
                            "failed",
                            error=str(exc),
                        )
                        raise
                    if _is_quota_error(exc):
                        _log_quota_hint(exc)
                    _sleep_backoff(attempt)
            raise RuntimeError(f"Transcription failed after retries: {last_exc}")
        finally:
            try:
                name = uploaded.name or ""
                if name:
                    self.client.files.delete(name=name)
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass


def checkpoint_path(chunk_mp3: Path) -> Path:
    return chunk_mp3.with_suffix(".metadata.json")


# --- transcript.json serialization -----------------------------------------
#
# <input>.transcript.json stores the full transcription result (per-chunk text
# plus word-level timestamps/speakers) so .diarized.srt/.srt/.txt can be re-rendered
# without calling the Gemini API again (e.g. for format fine-tuning).

TRANSCRIPT_SCHEMA_VERSION = 2


def transcript_to_dict(
    results: list[TranscriptionResult],
    chunk_secs: float,
    language: str,
    api_logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize transcription results plus API call logs."""
    data: dict[str, Any] = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "language": language,
        "chunk_secs": chunk_secs,
        "chunks": [
            {
                "index": idx,
                "text": res.text,
                "words": [
                    {
                        "text": w.text,
                        "start": w.start,
                        "end": w.end,
                        "speaker": w.speaker,
                    }
                    for w in res.words
                ],
            }
            for idx, res in enumerate(results)
        ],
    }
    if api_logs is not None:
        data["api_logs"] = {
            "entries": api_logs,
            "total_api_secs": round(sum(e.get("duration_secs", 0.0) for e in api_logs), 3),
            "call_count": len(api_logs),
        }
    return data


def transcript_from_dict(data: dict[str, Any]) -> list[TranscriptionResult]:
    results: list[TranscriptionResult] = []
    for chunk in data.get("chunks", []):
        words = [
            Word(
                text=w.get("text", ""),
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                speaker=w.get("speaker"),
            )
            for w in chunk.get("words", [])
        ]
        results.append(TranscriptionResult(text=chunk.get("text", ""), words=words))
    return results


def save_transcript(
    path: Path,
    results: list[TranscriptionResult],
    chunk_secs: float,
    language: str,
    api_logs: list[dict[str, Any]] | None = None,
) -> None:
    """Atomically write the transcript JSON."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            transcript_to_dict(results, chunk_secs, language, api_logs=api_logs),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_transcript(path: Path) -> list[TranscriptionResult] | None:
    """Load transcript results; None if missing/invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    results = transcript_from_dict(data)
    if not results:
        return None
    return results


def load_checkpoint(meta_path: Path) -> TranscriptionResult | None:
    """Load a valid chunk checkpoint; None if missing/invalid."""
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    words = [
        Word(
            text=w.get("text", ""),
            start=float(w.get("start", 0.0)),
            end=float(w.get("end", 0.0)),
            speaker=w.get("speaker"),
        )
        for w in data.get("words", [])
    ]
    text = data.get("text", "")
    if not text and not words:
        return None
    return TranscriptionResult(text=text, words=words)


def save_checkpoint(meta_path: Path, result: TranscriptionResult) -> None:
    """Atomically persist a chunk checkpoint (write .tmp then os.replace)."""
    data = {
        "text": result.text,
        "words": [
            {
                "text": w.text,
                "start": w.start,
                "end": w.end,
                "speaker": w.speaker,
            }
            for w in result.words
        ],
    }
    tmp = meta_path.with_suffix(".metadata.json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    import os

    os.replace(tmp, meta_path)


def transcribe_chunks_sequential(
    client: TranscribeClient,
    chunks: list[Path],
    request_interval_secs: float = 30.0,
    free_tier_wait_on_429: bool = False,
) -> list[TranscriptionResult]:
    """Transcribe each chunk in order, reusing valid checkpoints (skip API + skip sleep).

    With ``free_tier_wait_on_429=True``:
    - Before each chunk, if today's PST count has already reached the free-tier
      daily limit, sleep until PST midnight before the next API call.
    - On a 429 (quota) error, sleep until PST midnight and retry the same chunk.

    The wait is performed in 1-hour chunks with the remaining time logged, so a
    long multi-day batch can be left running unattended.
    """
    from .usage_counter import (
        FREE_TIER_DAILY_LIMIT,
        count_today,
        sleep_until_pst_midnight,
    )

    results: list[TranscriptionResult] = []
    for idx, chunk in enumerate(chunks):
        if free_tier_wait_on_429 and count_today() >= FREE_TIER_DAILY_LIMIT:
            logger.warning(
                "Free-tier daily quota reached (%d/%d calls today, PST); "
                "waiting until PST midnight before next chunk.",
                count_today(),
                FREE_TIER_DAILY_LIMIT,
            )
            sleep_until_pst_midnight()

        meta = checkpoint_path(chunk)
        existing = load_checkpoint(meta)
        if existing is not None:
            logger.info("Chunk %d: checkpoint found, skipping API call", idx)
            results.append(existing)
            continue

        logger.info("Chunk %d/%d: transcribing %s", idx + 1, len(chunks), chunk.name)
        # On 429 with --free-tier-wait-on-429, sleep until PST midnight and retry.
        # Other exceptions propagate immediately to the caller.
        while True:
            try:
                result = client.transcribe_chunk(chunk, chunk_index=idx)
                break
            except Exception as exc:
                if free_tier_wait_on_429 and _is_quota_error(exc):
                    logger.warning(
                        "Hit 429 with --free-tier-wait-on-429 (chunk %d); "
                        "sleeping until PST midnight and retrying.",
                        idx,
                    )
                    sleep_until_pst_midnight()
                    continue
                raise

        save_checkpoint(meta, result)
        results.append(result)

        if idx < len(chunks) - 1 and request_interval_secs > 0:
            logger.info("Waiting %.0fs before next chunk (free-tier quota)", request_interval_secs)
            time.sleep(request_interval_secs)
    return results


def api_logs_summary(client: TranscribeClient) -> dict[str, Any]:
    """Summarize collected API call logs: entries + total duration."""
    logs = list(client.api_logs)
    total = sum(entry.get("duration_secs", 0.0) for entry in logs)
    return {
        "entries": logs,
        "total_api_secs": round(total, 3),
        "call_count": len(logs),
    }
