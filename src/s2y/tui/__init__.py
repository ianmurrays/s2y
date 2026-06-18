"""Textual UI layer. Blocking engine/service calls run off the event loop on a
daemon thread (:func:`s2y.tui.threads.run_detached`) inside ``@work`` workers, so
quitting never waits on an in-flight network call; engine events reach the UI as
Textual messages (``post_message``), never by touching widgets from a thread.
"""

from .app import S2YApp

__all__ = ["S2YApp"]
