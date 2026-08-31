"""Gemini 3.5 Transcribe client wrapper with checkpointed chunk transcription."""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
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


DEFAULT_KO_CUSTOM_VOCABULARY: list[str] = [
    # Kept for reference only — no longer sent to the API.
    # Gemini 3.5 Transcribe rejects custom_vocabulary when timestamps are
    # requested ("custom_vocabulary is incompatible with timestamps"), and
    # the wrapper always needs word-level timestamps for SRT. The Korean '수'
    # 받아쓰기 bug is handled by fix_korean_su_text post-processing instead.
    "수 있다",
    "수 없다",
    "수도 있다",
    "수도 없다",
    "수가 있다",
    "수가 없다",
    "수밖에",
    "수밖에 없다",
]


def _extract_words(interaction: Any) -> list[Word]:
    """Extract word-level annotations from an interaction.

    Supports two response shapes:
    1. word_info annotations on text content (current GAOS SDK)
    2. a `transcription` object with .words (WordInfo: word/start_offset/end_offset)
    """
    from .format import sanitize_words

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
                        Word(
                            text=getattr(annotation, "text", "") or "",
                            start=_parse_offset(getattr(annotation, "start_offset", None)),
                            end=_parse_offset(getattr(annotation, "end_offset", None)),
                            speaker=getattr(annotation, "speaker", None),
                        )
                    )
            transcription = getattr(content, "transcription", None)
            if transcription is not None and not words:
                for w in getattr(transcription, "words", None) or []:
                    words.append(
                        Word(
                            text=getattr(w, "word", "") or getattr(w, "text", "") or "",
                            start=_parse_offset(getattr(w, "start_offset", None)),
                            end=_parse_offset(getattr(w, "end_offset", None)),
                            speaker=getattr(w, "speaker", None)
                            or getattr(transcription, "speaker_label", None),
                        )
                    )
    return sanitize_words(words)


def _is_quota_error(exc: Exception) -> bool:
    """True for 429 Too Many Requests (rate limit / daily quota exceeded)."""
    message = str(exc)
    if "429" in message or "quota" in message.lower():
        return True
    if isinstance(exc, errors.APIError):
        return exc.code == 429
    return False


_RETRY_AFTER_RE = re.compile(r"please retry in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)

# Extra safety padding added on top of Gemini's "Please retry in Xs" hint
# when a 429 is encountered, so the retry fires after the quota cools down.
_RETRY_SAFETY_SECS = 120.0


def _parse_retry_after_seconds(message: str) -> float | None:
    """Parse Gemini's ``Please retry in Xs`` hint from an error message.

    Returns the seconds as a float, or ``None`` if the hint is not present.
    """
    m = _RETRY_AFTER_RE.search(message or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def _sanitize_audit_token(value: str) -> str:
    """Lowercase and strip filesystem-unsafe characters from a path component.

    Keeps ASCII alphanumerics, hyphens, underscores, and dots; everything else
    becomes ``_``. This keeps the audit log name portable across NAS, Linux,
    macOS, and Windows shares.
    """
    lowered = (value or "").strip().lower()
    sanitized = re.sub(r"[^a-z0-9._-]+", "_", lowered).strip("._-")
    return sanitized or "unknown"


def _get_computer_shortname() -> str:
    """Return the local computer's short hostname (no domain), lowercased."""
    for source in (os.getenv("COMPUTERNAME"), socket.gethostname(), "unknown"):
        if not source:
            continue
        short = source.split(".")[0].strip()
        if short:
            return _sanitize_audit_token(short)
    return "unknown"


def _get_current_username() -> str:
    """Return the current OS username, lowercased."""
    for source in (
        os.getenv("USER"),
        os.getenv("USERNAME"),
        os.getenv("LOGNAME"),
    ):
        if source and source.strip():
            return _sanitize_audit_token(source)
    try:
        return _sanitize_audit_token(getpass.getuser())
    except Exception:  # noqa: BLE001
        return "unknown"


def get_audit_log_path() -> Path:
    """Return path to ``<os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl``.

    The host and user segments are lowercased and sanitized so each
    (computer, user) pair gets its own audit log — useful when multiple
    users share a NAS-mounted temp directory.
    """
    filename = f"gemini-transcribe-wrapper-{_get_computer_shortname()}-{_get_current_username()}.audit.jsonl"
    return Path(tempfile.gettempdir()) / filename


def _extract_status_code(exc: Exception | None) -> int:
    """Extract HTTP status code (e.g. 200, 429, 400) from an exception."""
    if exc is None:
        return 200
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    msg = str(exc)
    m = re.search(r"\b(4[0-9]{2}|5[0-9]{2})\b", msg)
    if m:
        return int(m.group(1))
    if "quota" in msg.lower() or "429" in msg:
        return 429
    return 500


def append_audit_log(
    input_file_path: str,
    audio_chunk_file_path: str,
    audio_chunk_playtime_s: float,
    api_processing_time_s: float,
    api_http_status_code: int,
    api_key: str | None = None,
    timestamp: str | None = None,
    log_path: Path | None = None,
) -> None:
    """Append a single audit record to the per-host/per-user JSONL audit log.

    The default path is ``<os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl``;
    pass ``log_path`` to override.
    """
    if timestamp is None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    key_tail = api_key[-8:] if api_key else ""
    record = {
        "timestamp": timestamp,
        "api_key_tail": key_tail,
        "input_file_path": input_file_path,
        "audio_chunk_file_path": audio_chunk_file_path,
        "audio_chunk_playtime_s": round(float(audio_chunk_playtime_s), 3),
        "api_processing_time_s": (
            round(float(api_processing_time_s), 3)
            if api_http_status_code == 200 and api_processing_time_s >= 0
            else -1
        ),
        "api_http_status_code": int(api_http_status_code),
    }
    target = log_path or get_audit_log_path()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to append audit log to %s: %s", target, exc)


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
            "It looks like you hit the free tier daily quota (25 calls/day)."
        )
    else:
        logger.error(
            "It looks like you hit a short-term rate limit."
        )
    logger.error(_QUOTA_HINT)
    # Retry suggestions. We do NOT retry automatically — the user decides.
    # Short-term 429: Gemini usually suggests waiting ~1 minute. Daily quota:
    # wait until PST midnight for the counter to reset.
    from .usage_counter import seconds_until_pst_midnight

    seconds_left = seconds_until_pst_midnight()
    if seconds_left <= 60:
        logger.error(
            "To retry: wait about 1 minute, then re-run. "
            "PST midnight is less than a minute away, so quota resets soon."
        )
    else:
        hours = int(seconds_left // 3600)
        minutes = int((seconds_left % 3600) // 60)
        total_seconds = int(seconds_left)
        logger.error(
            "To retry: wait about 1 minute for a short-term 429, "
            "or wait %dh %dm (sleep %ds) until PST midnight for the daily quota to reset, "
            "then re-run.",
            hours,
            minutes,
            total_seconds,
        )
    logger.error(
        "Switching to a paid tier (enable billing) removes the free-tier limits."
    )


_LAST_API_COMPLETION_MONOTONIC: float | None = None
_LAST_API_COMPLETION_WALL: float | None = None


def reset_api_rate_limiter() -> None:
    """Reset in-memory and on-disk rate limiter timestamps (for testing)."""
    global _LAST_API_COMPLETION_MONOTONIC, _LAST_API_COMPLETION_WALL
    _LAST_API_COMPLETION_MONOTONIC = None
    _LAST_API_COMPLETION_WALL = None
    try:
        from .usage_counter import cache_dir

        cd = cache_dir()
        for p in cd.glob("last_api_completion*.json"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:  # noqa: BLE001, S110
        pass


def record_api_call_completed(api_key: str | None = None) -> None:
    """Record that an STT API call and its cleanup have completed."""
    global _LAST_API_COMPLETION_MONOTONIC, _LAST_API_COMPLETION_WALL
    now_mono = time.monotonic()
    now_wall = time.time()
    _LAST_API_COMPLETION_MONOTONIC = now_mono
    _LAST_API_COMPLETION_WALL = now_wall
    try:
        from .usage_counter import _key_hash, cache_dir

        cd = cache_dir()
        cd.mkdir(parents=True, exist_ok=True)
        kh = _key_hash(api_key)
        filename = f"last_api_completion-{kh}.json" if kh else "last_api_completion.json"
        path = cd / filename
        path.write_text(json.dumps({"completed_at": now_wall}), encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass


def _get_last_completion_elapsed(api_key: str | None = None) -> float | None:
    """Return elapsed seconds since last API completion, or None if no record."""
    if _LAST_API_COMPLETION_MONOTONIC is not None:
        return time.monotonic() - _LAST_API_COMPLETION_MONOTONIC
    try:
        from .usage_counter import _key_hash, cache_dir

        cd = cache_dir()
        kh = _key_hash(api_key)
        filename = f"last_api_completion-{kh}.json" if kh else "last_api_completion.json"
        path = cd / filename
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            completed_at = float(data.get("completed_at", 0.0))
            if completed_at > 0:
                return max(0.0, time.time() - completed_at)
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _throttle_api_call(
    request_interval_secs: float,
    api_key: str | None = None,
    tier: str = "free",
) -> None:
    """Enforce minimum cooldown (e.g. 60s for free tier) after the previous STT API call completed."""
    if request_interval_secs <= 0 or tier == "paid":
        return
    elapsed = _get_last_completion_elapsed(api_key)
    if elapsed is not None and elapsed < request_interval_secs:
        sleep_secs = request_interval_secs - elapsed
        from .usage_counter import (
            count_today,
            seconds_until_pst_midnight,
        )

        used_today = count_today(api_key=api_key)
        remaining = int(seconds_until_pst_midnight())
        reset_hours, reset_minutes = divmod(remaining // 60, 60)
        logger.info(
            "Free-tier rate limit: %.1fs elapsed since last API call completed "
            "(< %.0fs interval); sleeping %.1fs. "
            "The # of API call attempts today: %d. "
            "The daily limit will reset in %d hours %d minutes PST-08:00.",
            elapsed,
            request_interval_secs,
            sleep_secs,
            used_today,
            int(reset_hours),
            int(reset_minutes),
        )
        time.sleep(sleep_secs)


class TranscribeClient:
    def __init__(
        self,
        api_key: str | None = None,
        language: str = "ko-KR",
        enable_diarization: bool = True,
        request_interval_secs: float = 120.0,
        tier: str = "free",
        custom_vocabulary: list[str] | None = None,
        source_file: str | Path | None = None,
        audit_jsonl: str | Path | None = None,
        model: str = MODEL_ID,
    ) -> None:
        # Resolve the effective key from env vars early so the daily usage
        # counter (incremented per API call) and the genai.Client use the
        # same key. Without this, an env-var-only setup would tally API
        # calls under the unscoped usage.json while ``gtw -v`` reports 0/25
        # because the summary line reads the per-key usage-<hash>.json.
        resolved = (
            api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self.api_key = resolved
        self.client = (
            genai.Client(api_key=resolved) if resolved else genai.Client()
        )
        self.language = language
        self.enable_diarization = enable_diarization
        self.request_interval_secs = request_interval_secs
        self.tier = tier
        self.custom_vocabulary = list(custom_vocabulary) if custom_vocabulary else None
        self.source_file = str(Path(source_file).resolve()) if source_file else None
        self.audit_jsonl = Path(audit_jsonl) if audit_jsonl else get_audit_log_path()
        self.model = model
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

    def transcribe_chunk(
        self,
        chunk_mp3: Path | None,
        chunk_index: int = 0,
        source_file: str | Path | None = None,
        chunk_duration_secs: float | None = None,
    ) -> TranscriptionResult:
        _throttle_api_call(
            getattr(self, "request_interval_secs", 120.0),
            api_key=getattr(self, "api_key", None),
            tier=getattr(self, "tier", "free"),
        )

        if source_file is not None:
            effective_source_file = str(source_file)
        elif getattr(self, "source_file", None):
            effective_source_file = str(self.source_file)
        elif chunk_mp3 is not None and isinstance(chunk_mp3, (str, Path)):
            effective_source_file = str(Path(chunk_mp3).resolve())
        else:
            effective_source_file = "unknown"

        effective_chunk_file = (
            str(Path(chunk_mp3).resolve())
            if chunk_mp3 is not None and isinstance(chunk_mp3, (str, Path))
            else str(chunk_mp3 or "")
        )
        if chunk_duration_secs is None:
            try:
                from .audio import probe_duration_secs

                chunk_dur = probe_duration_secs(chunk_mp3) if chunk_mp3 is not None and isinstance(chunk_mp3, Path) and chunk_mp3.exists() else 0.0
            except Exception:  # noqa: BLE001
                chunk_dur = 0.0
        else:
            chunk_dur = float(chunk_duration_secs)

        started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        iso_ts = datetime.now().astimezone().isoformat(timespec="seconds")
        uploaded = None
        try:
            uploaded = self.client.files.upload(file=str(chunk_mp3))
            attempt_start = time.monotonic()
            try:
                from .usage_counter import increment_today

                increment_today(api_key=self.api_key)
                # First attempt
                try:
                    interaction = self.client.interactions.create(
                        model=self.model,
                        input=[
                            {
                                "type": "audio",
                                "uri": uploaded.uri,
                                "mime_type": "audio/mpeg",
                            }
                        ],
                        generation_config=self._generation_config(),
                    )
                except Exception as first_exc:
                    # 429 retry: if Gemini hints "Please retry in Xs",
                    # sleep X+safety and retry once. If retry also fails, re-raise
                    # the original exception so existing error handling runs.
                    retry_after = (
                        _parse_retry_after_seconds(str(first_exc))
                        if _is_quota_error(first_exc)
                        else None
                    )
                    if retry_after is None or retry_after <= 0:
                        raise
                    sleep_secs = retry_after + _RETRY_SAFETY_SECS
                    logger.info(
                        "Caught 429 with 'Please retry in %.1fs' hint; "
                        "sleeping %ds (hint %.1fs + safety %ds) then retrying once.",
                        retry_after,
                        int(sleep_secs),
                        retry_after,
                        int(_RETRY_SAFETY_SECS),
                    )
                    time.sleep(sleep_secs)
                    attempt_start = time.monotonic()  # reset so duration reflects the retry
                    try:
                        interaction = self.client.interactions.create(
                            model=self.model,
                            input=[
                                {
                                    "type": "audio",
                                    "uri": uploaded.uri,
                                    "mime_type": "audio/mpeg",
                                }
                            ],
                            generation_config=self._generation_config(),
                        )
                    except Exception:  # noqa: BLE001 - re-raise original, not this one
                        # Retry also failed; re-raise the original error
                        raise first_exc from None
                    logger.info(
                        "429 retry succeeded via cooldown; continuing."
                    )
                duration = time.monotonic() - attempt_start
                text = getattr(interaction, "output_text", None) or ""
                words = _extract_words(interaction)
                if text:
                    from .format import fix_korean_su_text

                    text = fix_korean_su_text(text)
                if not text and words:
                    text = " ".join(w.text for w in words)
                self._log_api_call(
                    chunk_index, 1, started_at, duration, "success"
                )
                append_audit_log(
                    input_file_path=effective_source_file,
                    audio_chunk_file_path=effective_chunk_file,
                    audio_chunk_playtime_s=chunk_dur,
                    api_processing_time_s=duration,
                    api_http_status_code=200,
                    api_key=getattr(self, "api_key", None),
                    timestamp=iso_ts,
                    log_path=getattr(self, "audit_jsonl", None),
                )
                return TranscriptionResult(text=text, words=words)
            except Exception as exc:
                status_code = _extract_status_code(exc)
                append_audit_log(
                    input_file_path=effective_source_file,
                    audio_chunk_file_path=effective_chunk_file,
                    audio_chunk_playtime_s=chunk_dur,
                    api_processing_time_s=-1.0,
                    api_http_status_code=status_code,
                    api_key=getattr(self, "api_key", None),
                    timestamp=iso_ts,
                    log_path=getattr(self, "audit_jsonl", None),
                )
                _log_quota_hint(exc)
                if not _is_quota_error(exc):
                    logger.error(
                        "Gemini API call failed. Reference: %s", MODEL_REF_URL
                    )
                self._log_api_call(
                    chunk_index,
                    1,
                    started_at,
                    time.monotonic() - attempt_start,
                    "failed",
                    error=str(exc),
                )
                raise
        except Exception as exc:
            if uploaded is None:
                status_code = _extract_status_code(exc)
                append_audit_log(
                    input_file_path=effective_source_file,
                    audio_chunk_file_path=effective_chunk_file,
                    audio_chunk_playtime_s=chunk_dur,
                    api_processing_time_s=-1.0,
                    api_http_status_code=status_code,
                    api_key=getattr(self, "api_key", None),
                    timestamp=iso_ts,
                    log_path=getattr(self, "audit_jsonl", None),
                )
            raise
        finally:
            if uploaded is not None:
                try:
                    name = uploaded.name or ""
                    if name:
                        self.client.files.delete(name=name)
                except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                    pass
            record_api_call_completed(api_key=getattr(self, "api_key", None))


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
    chunk_secs: list[float] | tuple[float, ...],
    language: str,
    api_logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize transcription results plus API call logs.

    ``chunk_secs`` is the per-chunk size list (variable for front-loaded
    splits). Older transcripts (schema 2) stored a single float; we always
    write a list for forward compatibility.
    """
    data: dict[str, Any] = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "language": language,
        "chunk_secs": [float(c) for c in chunk_secs],
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
    chunk_secs: list[float] | tuple[float, ...],
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


def load_transcript_chunk_secs(path: Path) -> list[float]:
    """Load the per-chunk size list from a transcript, with backward compat.

    Older transcripts stored ``chunk_secs`` as a single float (uniform chunk
    size). We coerce that to a list of that size repeated over the chunk
    count. Missing/invalid values fall back to ``[1790.0]``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [1790.0]
    raw = data.get("chunk_secs", 1790.0)
    if isinstance(raw, list):
        return [float(c) for c in raw]
    if isinstance(raw, (int, float)):
        # Old schema: uniform chunk size. Expand to one entry per chunk.
        num = len(data.get("chunks", [])) or 1
        return [float(raw)] * num
    return [1790.0]


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
    request_interval_secs: float = 120.0,
) -> list[TranscriptionResult]:
    """Transcribe each chunk in order, reusing valid checkpoints (skip API + skip sleep).

    Checkpoints short-circuit any further work for a chunk (no API call, no
    sleep). For chunks that need a real API call, rate limiting is enforced
    dynamically before each API call via _throttle_api_call.
    """
    if hasattr(client, "request_interval_secs"):
        client.request_interval_secs = request_interval_secs
    results: list[TranscriptionResult] = []
    for idx, chunk in enumerate(chunks):
        meta = checkpoint_path(chunk)
        existing = load_checkpoint(meta)
        if existing is not None:
            logger.info("Chunk %d: checkpoint found, skipping API call", idx)
            results.append(existing)
            continue

        logger.info("Chunk %d/%d: transcribing %s", idx + 1, len(chunks), chunk.name)
        # Single API attempt; any exception (including 429) propagates to the
        # caller. The caller prints retry suggestions (see _log_quota_hint).
        result = client.transcribe_chunk(chunk, chunk_index=idx)

        save_checkpoint(meta, result)
        results.append(result)
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
