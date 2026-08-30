"""gemini-transcribe-wrapper: Zero-config Gemini 3.5 Transcribe wrapper."""

from importlib import metadata
from pathlib import Path

try:
    import static_ffmpeg

    static_ffmpeg.add_paths()
except Exception:  # noqa: BLE001, S110 - best-effort environment init
    pass

from .api import gemini_transcribe
from .models import (
    BatchTranscribeResult,
    TranscribeInput,
    TranscribeLeftover,
    TranscribeOutput,
    TranscribeResult,
    TranscribeStatus,
)

__all__ = [
    "BatchTranscribeResult",
    "TranscribeInput",
    "TranscribeLeftover",
    "TranscribeOutput",
    "TranscribeResult",
    "TranscribeStatus",
    "gemini_transcribe",
]

try:
    __version__ = metadata.version("gemini-transcribe-wrapper")
except metadata.PackageNotFoundError:  # pragma: no cover - uninstalled source tree
    # Fall back to the version declared in pyproject.toml.
    import re

    _pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    _m = re.search(r'^version\s*=\s*"([^"]+)"', _pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    __version__ = _m.group(1) if _m else "0.0.0"
