"""Run blocking work on a *daemon* thread, awaited like ``asyncio.to_thread``.

``asyncio.to_thread`` runs on the loop's default ``ThreadPoolExecutor``, whose
threads are joined at interpreter shutdown (and drained by ``asyncio.run``
cleanup). A blocking spotipy/ytmusicapi call in flight when the user quits would
therefore block the process from exiting — the classic ``q``-hang.

A raw ``threading.Thread(daemon=True)`` is joined by *neither* path, so it can be
abandoned freely: the result bridges back to the event loop while the app lives,
and at exit the daemon thread is simply dropped. Paired with the engine's
cooperative ``stop`` event, the abandoned thread also stops doing work promptly.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


async def run_detached[T](fn: Callable[[], T]) -> T:
    """Run ``fn()`` on a daemon thread and await its result.

    If the awaiting task is cancelled (e.g. on quit), the future is cancelled and
    the daemon thread is left to finish or be reclaimed at process exit — it can
    never block shutdown.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[T] = loop.create_future()

    def _resolve(setter: Callable[[], None]) -> None:
        if not fut.cancelled():  # the awaiter may have gone away (cancelled)
            setter()

    def runner() -> None:
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — relayed to the awaiter verbatim
            # Bind exc as a default arg so its value is captured now, on this
            # thread. The lambda runs later on the loop thread, by which point the
            # except block has implicitly `del`-ed the `exc` name — reading it then
            # would race into a NameError and the future would never resolve.
            loop.call_soon_threadsafe(_resolve, lambda exc=exc: fut.set_exception(exc))
        else:
            loop.call_soon_threadsafe(_resolve, lambda: fut.set_result(result))

    threading.Thread(target=runner, name="s2y-engine", daemon=True).start()
    return await fut
