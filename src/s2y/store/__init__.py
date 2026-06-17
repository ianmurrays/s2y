"""Persistent SQLite state: the cross-service identity map and sync bookkeeping.

This is what makes s2y *incremental and idempotent* — the reference project keeps
no state and re-matches everything every run. See ``schema.sql`` for the data
model and ``repository.py`` for typed access.
"""

from .db import connect
from .repository import PlaylistMapRow, Repository, TrackMapRow

__all__ = ["connect", "Repository", "TrackMapRow", "PlaylistMapRow"]
