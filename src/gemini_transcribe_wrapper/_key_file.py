"""Loading Gemini API keys from a plain-text file (``--gemini-api-keys-file``).

The file holds one API key per line. Blank lines and ``#`` comment lines
are ignored; order is preserved and duplicates are dropped so the
round-robin pointer math in :mod:`gemini_transcribe_wrapper.stt` can rely
on a stable, file-order key list.

Because the file holds secrets, POSIX platforms require ``0600``
permissions — anything looser aborts the run with the exact ``chmod``
command needed to fix it. Windows has no equivalent bit, so the check is
skipped there.

The rotation loop re-checks :func:`key_file_signature` before picking the
next key, so edits to the file take effect mid-run without a restart.

With the option left at ``auto`` the file is looked up as
``./gemini-api-keys.txt`` first, then
``~/.config/gemini-transcribe-wrapper/gemini-api-keys.txt``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

# Filename looked up when the option is left at its ``auto`` default --
# first in the current working directory, then in :func:`config_dir`.
DEFAULT_KEY_FILENAME = "gemini-api-keys.txt"


def config_dir() -> Path:
    """~/.config/gemini-transcribe-wrapper (GTW_CONFIG_DIR overrides for tests)."""
    override = os.environ.get("GTW_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "gemini-transcribe-wrapper"


class KeyFileError(Exception):
    """Raised when the key file is missing or has unsafe permissions."""


def _is_windows() -> bool:
    """Return whether we're on Windows (indirect so tests can monkeypatch)."""
    return os.name == "nt"


def check_key_file_permissions(path: Path) -> None:
    """Abort unless ``path`` is ``0600`` on POSIX platforms.

    Windows has no POSIX permission bits, so the check is a no-op there.
    """
    if _is_windows():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise KeyFileError(
            f"API key file {path} has unsafe permissions {mode:04o} "
            f"(expected 0600). It holds secrets and must not be readable "
            f"by other users.\n"
            f"Fix it with:\n"
            f"    chmod 600 {path}"
        )


def load_keys_from_file(path: Path) -> list[str]:
    """Return the keys in ``path``, one per line, in file order.

    Blank lines and ``#`` comments are skipped, whitespace is stripped,
    and duplicates are dropped (first occurrence wins).
    """
    keys: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            keys.append(line)
    return keys


def key_file_signature(path: Path) -> tuple[int, int, str] | None:
    """Return a change-detection signature for ``path`` (``None`` if unreadable).

    ``(mtime_ns, size, sha256)`` — the content hash is included because a
    fast edit can land within the same mtime tick, and key files are
    small enough that hashing them per chunk is free.
    """
    try:
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, digest)


def resolve_key_file(option_value: str | None) -> Path | None:
    """Resolve the ``--gemini-api-keys-file`` value to a concrete path.

    * ``off`` / ``none`` / empty → ``None`` (feature disabled).
    * ``auto`` (default) → the first of ``./gemini-api-keys.txt`` and
      ``~/.config/gemini-transcribe-wrapper/gemini-api-keys.txt`` that
      exists, else ``None``. The cwd copy wins so a project can override
      the shared home-directory key list. A missing auto file is not an
      error.
    * Anything else → that path, which must exist.

    Permissions are validated for every path actually returned.
    """
    if option_value is None:
        return None
    value = option_value.strip()
    if not value or value.lower() in {"off", "none", "false", "no", "0"}:
        return None
    if value.lower() == "auto":
        for candidate in (
            Path.cwd() / DEFAULT_KEY_FILENAME,
            config_dir() / DEFAULT_KEY_FILENAME,
        ):
            if candidate.exists():
                path = candidate
                break
        else:
            return None
    else:
        path = Path(value).expanduser()
        if not path.exists():
            raise KeyFileError(f"API key file not found: {path}")
        if not path.is_file():
            raise KeyFileError(f"API key file is not a regular file: {path}")
    check_key_file_permissions(path)
    return path
