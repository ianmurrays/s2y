"""Tests for retry/backoff/pacing/batching."""

from __future__ import annotations

import threading
import time

import pytest

from s2y.services.ratelimit import (
    Pacer,
    chunked,
    interruptible_sleep,
    retry_after_seconds,
    with_retry,
)


class Fake429(Exception):
    def __init__(self, retry_after="2"):
        super().__init__("rate limited")
        self.http_status = 429
        self.headers = {"Retry-After": retry_after}


def test_with_retry_returns_immediately_on_success():
    calls = []
    out = with_retry(lambda: (calls.append(1), "ok")[1], sleep=lambda _: None)
    assert out == "ok"
    assert len(calls) == 1


def test_with_retry_recovers_after_failures():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("transient")
        return "done"

    slept: list[float] = []
    out = with_retry(flaky, base_sleep=1.0, sleep=slept.append)
    assert out == "done"
    assert state["n"] == 3
    assert slept == [1.0, 2.0]  # exponential backoff


def test_with_retry_reraises_after_exhaustion():
    with pytest.raises(RuntimeError):
        with_retry(lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                   tries=3, sleep=lambda _: None)


def test_with_retry_honors_retry_after_on_429():
    slept: list[float] = []
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise Fake429("2")
        return "ok"

    with_retry(flaky, sleep=slept.append)
    assert slept == [3.0]  # 2s Retry-After + 1s cushion, not exponential


def test_retry_after_seconds_ignores_non_429():
    assert retry_after_seconds(RuntimeError("x")) is None


def test_with_retry_invokes_on_retry_hook_each_attempt():
    events: list[tuple[int, float]] = []
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    with_retry(
        flaky, base_sleep=1.0, sleep=lambda _: None,
        on_retry=lambda exc, attempt, wait: events.append((attempt, wait)),
    )
    assert events == [(1, 1.0), (2, 2.0)]  # attempt index + the backoff it waited


def test_with_retry_on_retry_hook_reports_429_wait():
    events: list[tuple[int, float]] = []
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise Fake429("2")
        return "ok"

    with_retry(
        flaky, sleep=lambda _: None,
        on_retry=lambda exc, attempt, wait: events.append((attempt, wait)),
    )
    assert events == [(1, 3.0)]  # 2s Retry-After + 1s cushion, surfaced to the hook


def test_with_retry_default_tries_is_bounded():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        with_retry(always_fail, sleep=lambda _: None)
    assert calls["n"] == 6  # the (lowered) interactive default


def test_chunked():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        list(chunked([1], 0))


def test_pacer():
    slept: list[float] = []
    Pacer(0.5, sleep=slept.append).wait()
    Pacer(0.0, sleep=slept.append).wait()  # no sleep when zero
    assert slept == [0.5]


def test_interruptible_sleep_wakes_immediately_when_already_set():
    stop = threading.Event()
    stop.set()
    isleep = interruptible_sleep(stop)
    t0 = time.monotonic()
    isleep(30)  # a plain time.sleep would block for 30s here
    assert time.monotonic() - t0 < 1.0


def test_interruptible_sleep_wakes_when_set_from_another_thread():
    stop = threading.Event()
    isleep = interruptible_sleep(stop)
    threading.Timer(0.05, stop.set).start()
    t0 = time.monotonic()
    isleep(30)
    assert time.monotonic() - t0 < 1.0


def test_interruptible_sleep_blocks_until_timeout_when_never_set():
    stop = threading.Event()
    isleep = interruptible_sleep(stop)
    t0 = time.monotonic()
    isleep(0.05)
    assert time.monotonic() - t0 >= 0.05
