"""Textual UI layer. Blocking engine/service calls run off the event loop via
``asyncio.to_thread`` inside ``@work`` workers; engine events reach the UI as
Textual messages (``post_message``), never by touching widgets from a thread.
"""

from .app import S2YApp

__all__ = ["S2YApp"]
