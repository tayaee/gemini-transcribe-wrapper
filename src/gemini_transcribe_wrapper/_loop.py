"""Loop driver for ``--loop-until-no-input`` / ``--loop-always`` (issue-001).

Spec §3.1. The CLI's existing pass loop is wrapped in this driver so
the wrapper can keep polling a glob across many files (and across the
free-tier daily quota reset). The driver handles:

- Re-globbing after each pass (when ``loop_until_no_input`` or
  ``loop_always`` is set).
- Sleeping ``loop_poll_secs`` between empty passes when ``loop_always``.
- Catching ``QuotaExceededError`` so a 429 doesn't exit the loop —
  the driver logs, sleeps, and tries the next pass.
- Catching ``KeyboardInterrupt`` cleanly with exit code 130 (POSIX).

This module deliberately has no Click dependency so the driver can be
unit-tested with simple stubs (see ``tests/test_loop.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Exit code for clean Ctrl-C termination (POSIX convention).
EXIT_INTERRUPT = 130

# Default + bounds for ``--loop-poll-secs``.
DEFAULT_POLL_SECS = 30
MIN_POLL_SECS = 1
MAX_POLL_SECS = 3600


def _clamp_poll_secs(value: float) -> int:
    """Clamp ``value`` into ``[MIN_POLL_SECS, MAX_POLL_SECS]``."""
    v = int(value)
    if v < MIN_POLL_SECS:
        return MIN_POLL_SECS
    if v > MAX_POLL_SECS:
        return MAX_POLL_SECS
    return v


def _glob_matches(patterns: list[str] | tuple[str, ...]) -> list[str]:
    """Resolve every pattern under the *current working directory*.

    The CLI normally expands each ``opts.path`` pattern via the OS
    shell; here we run an in-process ``glob.glob`` so the driver stays
    test-friendly (no shell quoting issues, no subprocess).

    Files are returned as ``str`` paths in the order glob produces them
    (alphabetical on most filesystems), de-duplicated.
    """
    import glob

    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        for hit in glob.glob(pattern):
            if hit not in seen:
                seen.add(hit)
                out.append(hit)
    return out


def run_with_loop(
    *,
    patterns: list[str] | tuple[str, ...],
    loop_until_no_input: bool,
    loop_always: bool,
    loop_poll_secs: int,
    run_pass: Callable[[list[str]], object],
) -> int:
    """Run ``run_pass`` once (or repeatedly, with polling).

    Parameters
    ----------
    patterns:
        Glob patterns to resolve each iteration. The same patterns are
        re-evaluated every pass — this is how new arrivals get picked
        up under ``--loop-until-no-input`` / ``--loop-always``.
    loop_until_no_input, loop_always:
        At most one is True. With both False, ``run_pass`` runs once
        and we return.
    loop_poll_secs:
        Sleep duration between empty passes when ``loop_always``.
    run_pass:
        Callable that receives the current glob matches and returns
        ``(matches, results)``. Exceptions are translated by the
        driver (see Returns below).

    Returns
    -------
    int
        Exit code. ``130`` on ``KeyboardInterrupt``; ``0`` if the
        loop exits naturally. ``run_pass``'s return value is opaque
        to the driver — the CLI layer maps it to its own exit code.
    """
    poll = _clamp_poll_secs(loop_poll_secs)

    while True:
        try:
            matches = _glob_matches(patterns)
        except KeyboardInterrupt:
            logger.info("Loop interrupted by user (Ctrl-C). Exiting.")
            return EXIT_INTERRUPT

        try:
            run_pass(matches)
        except KeyboardInterrupt:
            logger.info("Loop interrupted by user (Ctrl-C). Exiting.")
            return EXIT_INTERRUPT
        except Exception as exc:
            # QuotaExceededError: do NOT exit. Log, sleep, retry next pass.
            from .api import QuotaExceededError

            if isinstance(exc, QuotaExceededError):
                logger.warning(
                    "Quota hit during loop pass; sleeping %.0fs before "
                    "the next attempt.",
                    poll,
                )
                if loop_until_no_input or loop_always:
                    time.sleep(poll)
                    continue
                # No loop flag → caller wants the QuotaExceededError to
                # propagate so the CLI can return its dedicated exit 2.
                raise

            # Any other exception: re-raise so the CLI's existing
            # outer ``except Exception`` handler decides the exit code.
            raise

        # If neither flag is set, we're done after one pass.
        if not (loop_until_no_input or loop_always):
            return 0

        # Loop flag is set: decide whether to re-glob.
        if not matches:
            if loop_until_no_input:
                # No matches on this pass → exit (per spec §3.1).
                logger.info(
                    "Loop exiting: no input files matched %r after a pass.",
                    list(patterns),
                )
                return 0
            # loop_always: sleep and re-glob.
            logger.debug(
                "Loop poll: no matches; sleeping %.0fs before next pass.",
                poll,
            )
            time.sleep(poll)
            continue

        # Matches present, loop flag set: continue immediately to the
        # next pass. The CLI's "produced N files this pass" log line
        # is emitted by ``run_pass`` itself; we don't need a sleep
        # because the global ``request_interval_secs`` throttle already
        # spaces out per-key API calls.
        continue
