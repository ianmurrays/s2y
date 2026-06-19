"""Bridge the ``logging`` system into the Textual UI as ``LogMessage`` events.

A :class:`TextualLogHandler` is attached to the ``s2y`` logger while a sync runs;
each record is formatted and ``post_message``-d to the active screen (thread-safe,
so it works from the engine worker thread). The same records still reach the file
handler configured in :mod:`s2y.cli`, so we get a live panel *and* a log file from
one set of ``log.*`` calls.
"""

from __future__ import annotations

import contextlib
import logging

from .messages import LogMessage

# Root of our logging namespace; a handler here sees every ``s2y.*`` record via
# propagation, including the deep service/ratelimit retry lines.
_ROOT = "s2y"


class TextualLogHandler(logging.Handler):
    """A logging handler that forwards records to a Textual message target."""

    def __init__(self, target, *, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._target = target
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 — never let logging crash the run
            return
        # target torn down (app exiting) — just drop the line
        with contextlib.suppress(Exception):
            self._target.post_message(LogMessage(text, record.levelname.lower()))


def install(target, *, level: int = logging.INFO) -> TextualLogHandler:
    """Attach a forwarding handler to the ``s2y`` logger for ``target``."""
    handler = TextualLogHandler(target, level=level)
    logger = logging.getLogger(_ROOT)
    logger.addHandler(handler)
    # Make sure records at our level actually flow (the logger may default to
    # WARNING when no file logging was configured, e.g. in tests).
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    return handler


def remove(handler: TextualLogHandler | None) -> None:
    """Detach a previously installed handler (safe to call with ``None``)."""
    if handler is not None:
        logging.getLogger(_ROOT).removeHandler(handler)
