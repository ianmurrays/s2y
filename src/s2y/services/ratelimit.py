"""Retry, backoff, pacing, and batching for flaky/throttled service calls.

Ports the reference project's approach (retry up to N times with an exponential
``base_sleep`` that doubles, plus a per-item pace) and adds Spotify ``429
Retry-After`` handling. ``sleep`` is injectable so tests run instantly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")

SleepFn = Callable[[float], None]
RetryHook = Callable[[Exception, int, float], None]


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


def with_retry(
    fn: Callable[[], T],
    *,
    tries: int = 10,
    base_sleep: float = 5.0,
    max_sleep: float = 300.0,
    sleep: SleepFn = time.sleep,
    on_retry: RetryHook | None = None,
) -> T:
    """Call ``fn`` with exponential backoff. Honors ``Retry-After`` on 429s.

    Re-raises the last exception once ``tries`` is exhausted.
    """
    delay = base_sleep
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — deliberately broad; we retry then re-raise
            if attempt >= tries:
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


class Pacer:
    """Sleeps a fixed interval between operations to stay under rate limits."""

    def __init__(self, seconds: float, *, sleep: SleepFn = time.sleep) -> None:
        self.seconds = max(0.0, seconds)
        self._sleep = sleep

    def wait(self) -> None:
        if self.seconds > 0:
            self._sleep(self.seconds)


def chunked(seq: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield ``seq`` in lists of at most ``size`` (Spotify caps: 100 / 50)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for i in range(0, len(seq), size):
        yield list(seq[i : i + size])
