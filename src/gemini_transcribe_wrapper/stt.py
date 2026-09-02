"""Gemini 3.5 Transcribe client wrapper with checkpointed chunk transcription."""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors
from google.genai._gaos.types import interactions as _gaos_interactions
from google.genai.types import HttpOptions, HttpRetryOptions

from ._key_utils import api_key_tail

logger = logging.getLogger(__name__)

MODEL_ID = "gemini-3.5-transcribe"

# Single-attempt HTTP options for our genai.Client instances. The SDK
# defaults to ~6 retries with exponential backoff, which blows past
# Gemini's per-minute RPM limits on the free tier when the wrapper's
# own round-robin/blacklist logic is already handling 429 recovery.
# With ``attempts=1`` every call goes out exactly once; the wrapper's
# own per-key cooldown / blacklist / retry-on-next-key loop drives all
# the recovery work instead of the SDK's hidden retry loop.
_NO_RETRY_HTTP_OPTIONS = HttpOptions(retry_options=HttpRetryOptions(attempts=1))

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
    # transcription bug is handled by fix_korean_su_text post-processing instead.
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


def apply_vocabulary_bias(text: str, vocabulary: list[str] | None) -> str:
    """Bias transcript toward user-registered vocabulary terms.

    Gemini Transcribe rejects ``custom_vocabulary`` when timestamps are
    requested (400 error), and the wrapper always needs word-level
    timestamps for SRT. So we apply the user vocabulary as a
    post-recognition step instead of sending it to the API.

    Replacement rules:

    * Case-insensitive (``Gemini`` matches ``gemini`` / ``GEMINI``).
    * Whitespace-tolerant: any run of whitespace between tokens counts
      as a single match boundary (``"수 있다"`` matches ``"수  있다"``).
    * Greedy: longer phrases are matched first so multi-word terms are
      not partially consumed by shorter ones.

    Returns ``text`` unchanged if either side is empty.
    """
    if not text or not vocabulary:
        return text
    result = text
    for vocab in sorted(vocabulary, key=len, reverse=True):
        v = vocab.strip()
        if not v:
            continue
        tokens = v.split()
        if not tokens:
            continue
        pattern_str = r"\s+".join(re.escape(t) for t in tokens)
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
        except re.error:
            continue
        result = pattern.sub(v, result)
    return result


_RETRY_AFTER_RE = re.compile(r"please retry in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)

# Per-key minimum cooldown after a 429 / daily-quota hit (issue-003,
# spec §4.1). 30 minutes is long enough that an exhausted key won't
# re-429 immediately on retry and short enough that the loop is
# responsive when a key genuinely recovers mid-day.
KEY_COOLDOWN_SECS = 1800.0

# Backward-compat alias. Older code (and a handful of tests) still
# monkeypatch ``stt._COOLDOWN_SECS`` to speed up the recovery wait;
# keep the name resolvable so those tests keep working.
_COOLDOWN_SECS = KEY_COOLDOWN_SECS


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


def get_audit_log_path(api_key: str | None = None) -> Path:
    """Return the default audit-log path for an API key.

    Location:
    ``~/.cache/gemini-transcribe-wrapper/<api_key_tail>/audit.jsonl``
    where ``api_key_tail = api_key[-8:]``. One file per key, so
    each key maintains its own separate audit log.

    When ``api_key`` is empty/None, returns ``~/.cache/gemini-transcribe-wrapper/audit.jsonl``.
    """
    from .usage_counter import cache_dir

    if api_key:
        key_tail = api_key_tail(api_key)
        return cache_dir() / key_tail / "audit.jsonl"
    return cache_dir() / "audit.jsonl"


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
    log_path: Path | str | bool | None = None,
) -> None:
    """Append a single audit record to the per-host/per-user JSONL audit log.

    The default path is ``<os-temp>/gemini-transcribe-wrapper-<host>-<user>.audit.jsonl``;
    pass ``log_path`` to override.
    """
    if timestamp is None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    key_tail = api_key_tail(api_key)
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
    if log_path is False:
        return
    if isinstance(log_path, (str, Path)):
        target = Path(log_path)
    else:
        # Route to the per-key path when an api_key was provided;
        # otherwise fall back to the legacy <temp>/<host>-<user> file
        # (preserved for backward-compat during the v1.x → v2 migration).
        target = get_audit_log_path(api_key=api_key)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
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
    # wait until PT midnight for the counter to reset.
    from .usage_counter import seconds_until_pt_midnight

    seconds_left = seconds_until_pt_midnight()
    if seconds_left <= 60:
        logger.error(
            "To retry: wait about 1 minute, then re-run. "
            "PT midnight is less than a minute away, so quota resets soon."
        )
    else:
        hours = int(seconds_left // 3600)
        minutes = int((seconds_left % 3600) // 60)
        total_seconds = int(seconds_left)
        logger.error(
            "To retry: wait about 1 minute for a short-term 429, "
            "or wait %dh %dm (sleep %ds) until PT midnight for the daily quota to reset, "
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
_LAST_API_COMPLETION_MONOTONIC_BY_KEY: dict[str | None, float] = {}
_LAST_API_COMPLETION_WALL_BY_KEY: dict[str | None, float] = {}
_GLOBAL_RR_INDEX: int = 0
_GLOBAL_DEAD_POOL: dict[str, float] = {}


def _resolve_keys_from_env() -> list[str]:
    """Resolve API keys from environment variables.

    Precedence (matches the legacy single-key behavior):
        ``$GEMINI_API_KEYS`` (comma- or semicolon-separated) → ``$GEMINI_API_KEY`` →
        ``$GOOGLE_API_KEY``. Empty strings are dropped.
    """
    out: list[str] = []
    plural = os.environ.get("GEMINI_API_KEYS", "")
    if plural:
        out.extend(part.strip() for part in re.split(r"[,;]", plural) if part.strip())
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(var)
        if val and val.strip() and val.strip() not in out:
            out.append(val.strip())
    return out


def reset_api_rate_limiter() -> None:
    """Reset in-memory and on-disk rate limiter timestamps (for testing)."""
    global _LAST_API_COMPLETION_MONOTONIC, _LAST_API_COMPLETION_WALL
    global _GLOBAL_RR_INDEX
    _LAST_API_COMPLETION_MONOTONIC = None
    _LAST_API_COMPLETION_WALL = None
    _LAST_API_COMPLETION_MONOTONIC_BY_KEY.clear()
    _LAST_API_COMPLETION_WALL_BY_KEY.clear()
    _GLOBAL_RR_INDEX = 0
    _GLOBAL_DEAD_POOL.clear()
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
    _LAST_API_COMPLETION_MONOTONIC_BY_KEY[api_key] = now_mono
    _LAST_API_COMPLETION_WALL_BY_KEY[api_key] = now_wall
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
        if api_key in _LAST_API_COMPLETION_MONOTONIC_BY_KEY:
            return time.monotonic() - _LAST_API_COMPLETION_MONOTONIC_BY_KEY[api_key]
        if api_key is None:
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
            _format_hours_minutes,
            seconds_until_pt_midnight,
        )

        remaining = int(seconds_until_pt_midnight())
        reset_hours, reset_minutes = divmod(remaining // 60, 60)
        time_left = _format_hours_minutes(reset_hours, reset_minutes)
        logger.info(
            "Sleeping %.1fs to avoid 429 error on free tier. "
            "%.1fs elapsed since last API call completed (< %.0fs interval). "
            "If 429s persist, the daily limit may be hit — retry after "
            "midnight PT (in %s).",
            sleep_secs,
            elapsed,
            request_interval_secs,
            time_left,
        )
        time.sleep(sleep_secs)


class TranscribeClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_keys: list[str] | None = None,
        language_codes: list[str] | None = None,
        enable_diarization: bool = True,
        request_interval_secs: float = 120.0,
        tier: str = "free",
        cooldown_secs: float | None = None,
        custom_vocabulary: list[str] | None = None,
        source_file: str | Path | None = None,
        audit_jsonl_file: str | Path | bool | None = None,
        model: str = MODEL_ID,
        word_level_timestamps: bool = True,
    ) -> None:
        # Resolve the effective key list, preserving order and dropping
        # blanks. ``api_key`` is the legacy single-key kwarg kept for
        # backward compatibility — it is treated as a one-element list.
        resolved_keys: list[str] = []
        if api_keys:
            resolved_keys.extend(k.strip() for k in api_keys if k and k.strip())
        if api_key and api_key.strip() and api_key.strip() not in resolved_keys:
            resolved_keys.append(api_key.strip())
        if not resolved_keys:
            resolved_keys = _resolve_keys_from_env()
        # Drop duplicates while preserving first-seen order.
        seen: set[str] = set()
        deduped: list[str] = []
        for k in resolved_keys:
            if k not in seen:
                seen.add(k)
                deduped.append(k)
        self._api_keys: list[str] = deduped
        # Live pool: keys currently eligible for round-robin.
        # Dead pool: keys that hit 429 (daily quota), each with its own
        # ``cooldown_until`` epoch (monotonic clock). A key auto-recovers
        # into ``_live_pool`` when its ``cooldown_until`` has elapsed —
        # see ``_prune_dead_pool``. This replaces the older batch model
        # (every 10 minutes, reactivate *all* cooldown keys at once) with
        # a per-key minimum 30-minute skip (issue-003, spec §4.1).
        now = time.monotonic()
        for k, until in list(_GLOBAL_DEAD_POOL.items()):
            if now >= until:
                _GLOBAL_DEAD_POOL.pop(k, None)
        self._dead_pool: dict[str, float] = {
            k: until for k, until in _GLOBAL_DEAD_POOL.items() if k in deduped
        }
        self._live_pool: list[str] = [k for k in deduped if k not in self._dead_pool]
        if self._live_pool:
            self._rr_index: int = _GLOBAL_RR_INDEX % len(self._live_pool)
            self.api_key: str | None = self._live_pool[self._rr_index]
        else:
            self._rr_index = 0
            self.api_key = self._api_keys[0] if self._api_keys else None
        self._clients: dict[str, genai.Client] = {}
        # Build the initial client. We do NOT route through ``_client_for``
        # because that helper reads ``self.client`` for the legacy path,
        # which doesn't exist yet at this point in ``__init__``.
        if self.api_key:
            self.client = genai.Client(
                api_key=self.api_key, http_options=_NO_RETRY_HTTP_OPTIONS,
            )
        else:
            self.client = genai.Client(http_options=_NO_RETRY_HTTP_OPTIONS)
        # Multi-language hint list. When ``None`` (or empty) we let the
        # Gemini API auto-detect the language; the ``language_codes``
        # field is omitted from the generation config in that case.
        self.language_codes: list[str] | None = (
            [c.strip() for c in language_codes if c and c.strip()]
            if language_codes
            else None
        )
        self.enable_diarization = enable_diarization
        self.word_level_timestamps = word_level_timestamps
        self.request_interval_secs = request_interval_secs
        self.tier = tier
        # Per-instance cooldown wait when the active pool drains. ``None``
        # (default) falls back to the module-level ``_COOLDOWN_SECS`` so
        # monkeypatching the constant in tests keeps working — see the
        # lookup inside ``transcribe_chunk`` for the lazy resolution.
        self._cooldown_secs: float | None = (
            float(cooldown_secs) if cooldown_secs is not None else None
        )
        self.custom_vocabulary = list(custom_vocabulary) if custom_vocabulary else None
        self.source_file = str(Path(source_file).resolve()) if source_file else None
        if audit_jsonl_file is False or (
            isinstance(audit_jsonl_file, str)
            and audit_jsonl_file.strip().lower() in {"off", "no", "false", "none", "0", ""}
        ):
            self.audit_jsonl_file: Path | bool | None = False
        elif (
            audit_jsonl_file is True
            or (isinstance(audit_jsonl_file, str) and audit_jsonl_file.strip().lower() == "auto")
            or audit_jsonl_file is None
        ):
            self.audit_jsonl_file = None
        else:
            self.audit_jsonl_file = Path(audit_jsonl_file)
        self.model = model
        self.api_logs: list[dict[str, Any]] = []

    def _client_for(self, key: str | None) -> genai.Client:
        """Return a ``genai.Client`` for ``key``.

        Falls back to whatever ``self.client`` is currently set to when
        no per-key cache exists. This covers two cases:

        * The single-key (legacy) path — the constructor only built one
          ``self.client`` for the active key.
        * Test doubles that bypass ``__init__`` and pre-set ``self.client``
          directly with a ``MagicMock``.

        For multi-key, lazily builds and caches one ``genai.Client`` per
        distinct key so round-robin doesn't pay SDK construction cost
        on every chunk.
        """
        keys = getattr(self, "_api_keys", None) or []
        cache = getattr(self, "_clients", None)
        if cache is None or len(keys) <= 1:
            # Legacy / test path: defer to ``self.client``.
            return self.client
        if not key:
            return self.client
        cached = cache.get(key)
        if cached is None:
            cached = genai.Client(api_key=key, http_options=_NO_RETRY_HTTP_OPTIONS)
            cache[key] = cached
        return cached

    # --- pool aliases (backward compat with old field names) ----------
    #
    # The old attribute names ``_active_pool`` and ``_cooldown_pool``
    # remain reachable so existing tests (test_active_cooldown_pool.py)
    # and any external monkeypatchers keep working. New code should use
    # ``_live_pool`` and ``_dead_pool`` directly.

    @property
    def _active_pool(self) -> list[str]:
        """Read-only view of the live (round-robin-eligible) pool."""
        return list(getattr(self, "_live_pool", []) or [])

    @_active_pool.setter
    def _active_pool(self, value: list[str]) -> None:
        self._live_pool = list(value)

    @property
    def _cooldown_pool(self) -> set[str]:
        """Read-only view of the dead pool as a set of key tails."""
        dead = getattr(self, "_dead_pool", {}) or {}
        return set(dead.keys())

    @_cooldown_pool.setter
    def _cooldown_pool(self, value: set[str]) -> None:
        # Test-setup convenience: when tests assign a fresh set, treat
        # the keys as "dead now with cooldown_until = +inf" so the prune
        # helper doesn't immediately move them back. Tests that want a
        # recoverable cooldown set ``_dead_pool`` directly.
        current = dict(getattr(self, "_dead_pool", {}) or {})
        if not value:
            self._dead_pool = {}
            return
        for k in value:
            current.setdefault(k, float("inf"))
        self._dead_pool = current

    def _prune_dead_pool(self, now: float) -> None:
        """Move any dead key whose ``cooldown_until <= now`` back to live.

        Preserves the original ``_api_keys`` ordering so the next
        round-robin iteration is fair across all recovered keys (issue-003,
        spec §4.1).
        """
        if not self._dead_pool:
            return
        recovered: list[str] = []
        for k in list(self._dead_pool.keys()):
            if self._dead_pool[k] <= now:
                recovered.append(k)
                del self._dead_pool[k]
                _GLOBAL_DEAD_POOL.pop(k, None)
        if not recovered:
            return
        # Append recovered keys in their original ``_api_keys`` order so
        # round-robin fairness is preserved. Keys not in ``_api_keys``
        # (defensive) are appended after in arbitrary order.
        original = list(getattr(self, "_api_keys", []) or [])
        current_and_recovered = set(self._live_pool) | set(recovered)
        ordered = [k for k in original if k in current_and_recovered]
        extras = [k for k in current_and_recovered if k not in ordered]
        self._live_pool = ordered + extras

    def _generation_config(self) -> _gaos_interactions.GenerationConfig:
        transcription: dict[str, Any] = {}
        # ``language_codes`` (multi-language hint list). An empty/None
        # value skips the field entirely so Gemini auto-detects the
        # spoken language.
        if self.language_codes:
            transcription["language_codes"] = list(self.language_codes)
        mode: dict[str, Any] = {"type": "verbatim"}
        if self.enable_diarization:
            mode["diarization_mode"] = "speaker"
        if self.word_level_timestamps:
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
        last_quota_exc: Exception | None = None
        try:
            keys = list(getattr(self, "_api_keys", []) or [])
            if not keys:
                # Legacy test client (or fully unset) — fall back to the
                # singular ``self.api_key`` for backward compatibility.
                legacy = getattr(self, "api_key", None)
                if legacy:
                    keys = [legacy]
            if not keys:
                raise RuntimeError(
                    "No Gemini API keys configured. Set $GEMINI_API_KEYS, "
                    "$GEMINI_API_KEY, or pass --gemini-api-keys."
                )
            # NOTE: The Files API scopes file URIs per API key — uploading
            # once and rotating ``self.client`` for the interactions call
            # triggers ``403 Forbidden`` on the new key. So each inner-loop
            # iteration creates its own upload + interactions session using
            # the same key. On the first 429 we abandon *this* key's session
            # (any uploaded file is leaked; Google cleans up server-side
            # after ~48h) and the next iteration uploads again with the
            # next key.

            # Per-key 30-minute cooldown (issue-003, spec §4.1):
            #   - On 429, the offending key moves into ``_dead_pool``
            #     with ``cooldown_until = monotonic + KEY_COOLDOWN_SECS``.
            #   - Each loop iteration calls ``_prune_dead_pool`` so any
            #     key whose cooldown has elapsed automatically returns
            #     to live — preserves round-robin fairness across
            #     independent key recoveries.
            #   - When the live pool drains but the dead pool isn't
            #     empty, sleep only until the *soonest* key recovers
            #     (not the full 30 minutes). This is the biggest win vs
            #     the old batch-reactivation model: most keys are out of
            #     sync, so sleeping 30 min for the slowest one wastes
            #     time on the fast ones.
            while True:
                self._prune_dead_pool(now=time.monotonic())
                active = list(self._live_pool)
                dead = self._dead_pool
                if not active:
                    if not dead:
                        # No keys at all — safety net (shouldn't happen
                        # because __init__ ensures at least one key).
                        assert last_quota_exc is not None
                        _log_quota_hint(last_quota_exc)
                        self._log_api_call(
                            chunk_index,
                            1,
                            started_at,
                            0.0,
                            "failed",
                            error=str(last_quota_exc),
                        )
                        raise last_quota_exc
                    # Single-key exception (issue-003, spec §3): when
                    # only one key is configured, a 429 means the
                    # *file* can't be processed right now but the
                    # caller (``api._process_one``) maps the quota
                    # exception to ``SKIPPED_QUOTA`` and moves on to
                    # the next file. Don't loop forever here — raise
                    # immediately so the batch can continue.
                    if len(getattr(self, "_api_keys", []) or []) == 1:
                        assert last_quota_exc is not None
                        _log_quota_hint(last_quota_exc)
                        self._log_api_call(
                            chunk_index,
                            1,
                            started_at,
                            0.0,
                            "failed",
                            error=str(last_quota_exc),
                        )
                        raise last_quota_exc
                    # Sleep only until the soonest key recovers. If a
                    # caller set ``_cooldown_secs`` to override the
                    # module default, honor it here so the test suite
                    # can shrink the wait without rewriting the keys.
                    base = (
                        self._cooldown_secs
                        if self._cooldown_secs is not None
                        else KEY_COOLDOWN_SECS
                    )
                    soonest = min(dead.values())
                    sleep_for = max(0.0, soonest - time.monotonic())
                    # Cap the wait at ``base`` so a stale ``cooldown_until``
                    # in the dict (e.g. set manually in a test) doesn't
                    # pin us for hours.
                    sleep_for = min(sleep_for, base)
                    logger.info(
                        "Live pool empty (%d keys still dead, soonest "
                        "recovery in %.0fs). Sleeping and retrying chunk %d.",
                        len(dead),
                        sleep_for,
                        chunk_index,
                    )
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    continue  # prune + retry chunk

                start_idx = getattr(self, "_rr_index", 0) % len(active)
                blacklisted_this_loop: list[str] = []

                for offset in range(len(active)):
                    idx = (start_idx + offset) % len(active)
                    key = active[idx]
                    if offset == 0:
                        _throttle_api_call(
                            getattr(self, "request_interval_secs", 120.0),
                            api_key=key,
                            tier=getattr(self, "tier", "free"),
                        )
                    # Install this key on the client for per-call helpers
                    # (usage counter / audit / record_api_call_completed).
                    # No ``_throttle_api_call`` here — between-key retries
                    # within a single chunk must run back-to-back so we can
                    # sweep all 15 keys on the first 429 without delay.
                    # Inter-chunk pacing is handled by the throttle at the
                    # top of ``transcribe_chunk``.
                    self.client = self._client_for(key)
                    self.api_key = key
                    from .usage_counter import increment_today

                    increment_today(api_key=key)

                    attempt_start = time.monotonic()
                    try:
                        # Per-key session: upload + interactions.create
                        # under the same key. The Files API scopes URIs
                        # per key, so we cannot share an upload across
                        # multiple ``self.client`` instances.
                        uploaded = self.client.files.upload(file=str(chunk_mp3))
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
                        # Non-quota errors (400/500): rotating keys won't
                        # help. Audit-log, log reference URL, re-raise.
                        if not _is_quota_error(first_exc):
                            status_code = _extract_status_code(first_exc)
                            append_audit_log(
                                input_file_path=effective_source_file,
                                audio_chunk_file_path=effective_chunk_file,
                                audio_chunk_playtime_s=chunk_dur,
                                api_processing_time_s=-1.0,
                                api_http_status_code=status_code,
                                api_key=key,
                                timestamp=iso_ts,
                                log_path=getattr(self, "audit_jsonl_file", None),
                            )
                            logger.error(
                                "Gemini API call failed. Reference: %s",
                                MODEL_REF_URL,
                            )
                            self._log_api_call(
                                chunk_index,
                                1,
                                started_at,
                                time.monotonic() - attempt_start,
                                "failed",
                                error=str(first_exc),
                            )
                            raise
                        # 429 / quota error: abandon this key's session,
                        # blacklist it for this chunk, and try the next
                        # active key back-to-back (no inter-key throttle —
                        # we want to sweep all remaining keys immediately
                        # on the first 429). Any 429 encountered here is
                        # treated as daily-quota exhaustion.
                        append_audit_log(
                            input_file_path=effective_source_file,
                            audio_chunk_file_path=effective_chunk_file,
                            audio_chunk_playtime_s=chunk_dur,
                            api_processing_time_s=-1.0,
                            api_http_status_code=_extract_status_code(first_exc),
                            api_key=key,
                            timestamp=iso_ts,
                            log_path=getattr(self, "audit_jsonl_file", None),
                        )
                        remaining_count = max(0, len(self._live_pool) - 1)
                        if remaining_count > 0 and offset + 1 < len(active):
                            next_key = active[(start_idx + offset + 1) % len(active)]
                            logger.info(
                                "Removing key %s from the round-robin pool "
                                "after a 429. %d api %s left in the live "
                                "round-robin api key pool. Picking up next api-key %s.",
                                api_key_tail(key),
                                remaining_count,
                                "key" if remaining_count == 1 else "keys",
                                api_key_tail(next_key),
                            )
                        else:
                            logger.info(
                                "Removing key %s from the round-robin pool "
                                "after a 429. %d api %s left in the live "
                                "round-robin api key pool.",
                                api_key_tail(key),
                                remaining_count,
                                "key" if remaining_count == 1 else "keys",
                            )
                        # Mark this key dead with a per-key 30-min cooldown.
                        # The next call to ``_prune_dead_pool`` at the top
                        # of the outer loop will move it back to live once
                        # the cooldown elapses (issue-003, spec §4.1).
                        self._dead_pool[key] = time.monotonic() + KEY_COOLDOWN_SECS
                        _GLOBAL_DEAD_POOL[key] = self._dead_pool[key]
                        if key in self._live_pool:
                            self._live_pool.remove(key)
                        last_quota_exc = first_exc
                        blacklisted_this_loop.append(key)
                        continue

                    # Success on this key.
                    duration = time.monotonic() - attempt_start
                    text = getattr(interaction, "output_text", None) or ""
                    words = _extract_words(interaction)
                    if text:
                        from .format import fix_korean_su_text

                        text = fix_korean_su_text(text)
                    if not text and words:
                        text = " ".join(w.text for w in words)
                    # Post-recognition vocabulary bias (Gemini API rejects
                    # ``custom_vocabulary`` when timestamps are requested,
                    # so we apply user terms here as a best-effort bias).
                    vocab = getattr(self, "custom_vocabulary", None)
                    if text and vocab:
                        text = apply_vocabulary_bias(text, vocab)
                    # Apply this iteration's blacklistings (keys that
                    # hit 429 earlier in the inner loop) *before*
                    # advancing the round-robin pointer so the index
                    # math stays consistent.
                    for bl_key in blacklisted_this_loop:
                        if bl_key in self._live_pool:
                            self._live_pool.remove(bl_key)
                        # Only stamp a fresh cooldown if the key doesn't
                        # already have one — preserves the original
                        # "first-429 wins" semantics.
                        self._dead_pool.setdefault(
                            bl_key, time.monotonic() + KEY_COOLDOWN_SECS
                        )
                        _GLOBAL_DEAD_POOL[bl_key] = self._dead_pool[bl_key]
                    blacklisted_this_loop.clear()
                    # Advance the round-robin so the *next* chunk uses
                    # the following key in the (post-blacklist) live
                    # pool. ``max(1, ...)`` guards against zero-length
                    # modulo if the pool somehow drained to a single
                    # surviving key (edge case).
                    current_active = list(self._live_pool)
                    if key in current_active:
                        idx_in_pool = current_active.index(key)
                        self._rr_index = (idx_in_pool + 1) % max(1, len(current_active))
                        global _GLOBAL_RR_INDEX
                        _GLOBAL_RR_INDEX = self._rr_index

                    self._log_api_call(
                        chunk_index, 1, started_at, duration, "success"
                    )
                    append_audit_log(
                        input_file_path=effective_source_file,
                        audio_chunk_file_path=effective_chunk_file,
                        audio_chunk_playtime_s=chunk_dur,
                        api_processing_time_s=duration,
                        api_http_status_code=200,
                        api_key=key,
                        timestamp=iso_ts,
                        log_path=getattr(self, "audit_jsonl_file", None),
                    )
                    return TranscriptionResult(text=text, words=words)

                # Inner loop exhausted without success: apply this chunk's
                # blacklistings in one batch. Outer while loop will then
                # either reactivate any expired cooldown keys (via
                # ``_prune_dead_pool``) or sleep until the soonest key
                # recovers before retrying.
                for key in blacklisted_this_loop:
                    if key in self._live_pool:
                        self._live_pool.remove(key)
                    # Only stamp a fresh cooldown if the key doesn't
                    # already have one (issue-003, spec §4.1).
                    self._dead_pool.setdefault(
                        key, time.monotonic() + KEY_COOLDOWN_SECS
                    )
                    _GLOBAL_DEAD_POOL[key] = self._dead_pool[key]
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
                    log_path=getattr(self, "audit_jsonl_file", None),
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
            logger.info(
                "Chunk %d: checkpoint found at %s, skipping API call", idx, meta,
            )
            results.append(existing)
            continue

        live_pool = getattr(client, "_live_pool", None)
        if live_pool:
            start_idx = getattr(client, "_rr_index", 0) % len(live_pool)
            chunk_key = live_pool[start_idx]
        else:
            chunk_key = getattr(client, "api_key", None)
        key_tail = api_key_tail(chunk_key)
        logger.info(  # nosemgrep: python-logger-credential-disclosure - only 8-char tail is logged
            "api-key=%s Chunk %d/%d: transcribing %s",
            key_tail,
            idx + 1,
            len(chunks),
            chunk.name,
        )
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
