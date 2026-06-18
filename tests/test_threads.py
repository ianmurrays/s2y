"""Tests for the daemon-thread runner that backs the TUI workers."""

from __future__ import annotations

import asyncio
import threading

import pytest

from s2y.tui.threads import run_detached


@pytest.mark.asyncio
async def test_run_detached_returns_result():
    assert await run_detached(lambda: 21 * 2) == 42


@pytest.mark.asyncio
async def test_run_detached_propagates_exception():
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await run_detached(boom)


@pytest.mark.asyncio
async def test_run_detached_runs_on_a_daemon_thread():
    info = await run_detached(lambda: threading.current_thread().daemon)
    assert info is True


@pytest.mark.asyncio
async def test_run_detached_cancel_drops_result_safely():
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "done"

    task = asyncio.ensure_future(run_detached(slow))
    await asyncio.to_thread(started.wait, 5)  # daemon thread is now running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Let the abandoned thread finish; resolving onto the cancelled future must
    # not raise (InvalidStateError) — the guard drops the result.
    release.set()
    await asyncio.sleep(0.1)  # give the threadsafe callback a tick to run
