"""Retry, backoff, pacing, and batching for flaky/throttled service calls.

Ports the reference project's approach (retry up to N times with an exponential
``base_sleep`` that doubles, plus a per-item pace) and adds Spotify ``429
Retry-After`` handling. ``sleep`` is injectable so tests run instantly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence

SleepFn = Callable[[float], None]
RetryHook = Callable[[Exception, int, float], None]
Retryable = Callable[[Exception], bool]


def retry_after_seconds(exc: Exception) -> float | None:
    """Extract a ``Retry-After`` wait from a 429, if the exception carries one.

    spotipy raises ``SpotifyException`` with ``http_status`` and ``headers``.
    Returns the seconds to wait (plus a 1s cushion), or ``None`` if not a 429.
    """
    status = getattr(exc, "http_status", None) or getattr(exc, "code", None)
    if status != 429:
        return None
    headers = getattr(exc, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw) + 1.0
    except (TypeError, ValueError):
        return None


def with_retry[T](
    fn: Callable[[], T],
    *,
    tries: int = 6,
    base_sleep: float = 5.0,
    max_sleep: float = 60.0,
    sleep: SleepFn = time.sleep,
    on_retry: RetryHook | None = None,
    retryable: Retryable | None = None,
) -> T:
    """Call ``fn`` with exponential backoff. Honors ``Retry-After`` on 429s.

    Re-raises the last exception once ``tries`` is exhausted. ``retryable`` (when
    given) decides whether a given exception is worth retrying at all — a
    deterministic failure (bad auth, an unparseable "please sign in" page) just
    fails again, so we re-raise it immediately instead of sleeping through the
    whole backoff schedule. Default: retry everything.
    """
    delay = base_sleep
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — deliberately broad; we retry then re-raise
            if attempt >= tries or (retryable is not None and not retryable(exc)):
                raise
            wait = retry_after_seconds(exc)
            if wait is None:
                wait = min(delay, max_sleep)
                delay *= 2
            else:
                wait = min(wait, max_sleep)
            if on_retry is not None:
                on_retry(exc, attempt, wait)
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def interruptible_sleep(stop: threading.Event) -> SleepFn:
    """A :data:`SleepFn` that wakes the instant ``stop`` is set.

    ``Event.wait(timeout)`` returns immediately once another thread calls
    ``stop.set()``, so a long backoff/pacing sleep aborts the moment the UI asks
    the engine to stop (e.g. on quit) instead of sleeping out the full interval.
    """

    def _sleep(seconds: float) -> None:
        stop.wait(seconds)  # discard the bool return so this matches SleepFn

    return _sleep


class Pacer:
    """Sleeps a fixed interval between operations to stay under rate limits."""

    def __init__(self, seconds: float, *, sleep: SleepFn = time.sleep) -> None:
        self.seconds = max(0.0, seconds)
        self._sleep = sleep

    def wait(self) -> None:
        if self.seconds > 0:
            self._sleep(self.seconds)


def chunked[T](seq: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield ``seq`` in lists of at most ``size`` (Spotify caps: 100 / 50)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])
