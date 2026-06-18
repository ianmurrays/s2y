"""The logging→UI bridge: records become LogMessage posts for the live panel."""

from __future__ import annotations

import logging

from s2y.tui.log_bridge import TextualLogHandler, install, remove
from s2y.tui.messages import LogMessage


class FakeTarget:
    """Stands in for a Screen: collects whatever is post_message-d to it."""

    def __init__(self) -> None:
        self.messages: list[LogMessage] = []

    def post_message(self, message: LogMessage) -> None:
        self.messages.append(message)


def test_handler_forwards_records_as_log_messages():
    target = FakeTarget()
    handler = TextualLogHandler(target, level=logging.INFO)
    logger = logging.getLogger("s2y.test.bridge")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("hello %s", "world")
        logger.debug("below the handler level — dropped")
    finally:
        logger.removeHandler(handler)

    assert len(target.messages) == 1
    msg = target.messages[0]
    assert isinstance(msg, LogMessage)
    assert "hello world" in msg.text
    assert msg.level == "info"


def test_handler_survives_a_dead_target():
    class Boom:
        def post_message(self, _message):
            raise RuntimeError("screen is gone")

    handler = TextualLogHandler(Boom(), level=logging.INFO)
    record = logging.LogRecord("s2y.x", logging.INFO, __file__, 1, "msg", None, None)
    handler.emit(record)  # must not raise


def test_install_and_remove_toggle_the_handler_on_the_root_namespace():
    target = FakeTarget()
    handler = install(target, level=logging.INFO)
    try:
        logging.getLogger("s2y.services.somewhere").warning("deep retry line")
        assert any("deep retry line" in m.text for m in target.messages)
    finally:
        remove(handler)
    target.messages.clear()
    logging.getLogger("s2y.services.somewhere").warning("after removal")
    assert target.messages == []
