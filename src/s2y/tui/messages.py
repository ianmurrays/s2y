"""Textual messages bridging engine/worker results to the UI thread."""

from __future__ import annotations

from textual.message import Message

from ..core.events import Event
from ..core.models import RemotePlaylist, SyncPlan


class EngineEvent(Message):
    """One progress event emitted by the engine during plan/apply."""

    def __init__(self, event: Event) -> None:
        super().__init__()
        self.event = event


class PlanReady(Message):
    def __init__(self, plan: SyncPlan) -> None:
        super().__init__()
        self.plan = plan


class ApplyDone(Message):
    def __init__(self, stats: dict) -> None:
        super().__init__()
        self.stats = stats


class ClassifyDone(Message):
    """The LLM judge finished a pass over the quarantine — carries the stats."""

    def __init__(self, stats: dict) -> None:
        super().__init__()
        self.stats = stats


class PlaylistsLoaded(Message):
    """The user's Spotify playlists, fetched off-thread for the picker screen."""

    def __init__(self, playlists: list[RemotePlaylist]) -> None:
        super().__init__()
        self.playlists = playlists


class WorkerFailed(Message):
    """A background worker raised — carries a user-facing message."""

    def __init__(self, where: str, detail: str) -> None:
        super().__init__()
        self.where = where
        self.detail = detail


class AuthResult(Message):
    def __init__(self, service: str, ok: bool, detail: str = "") -> None:
        super().__init__()
        self.service = service
        self.ok = ok
        self.detail = detail


class AuthUrl(Message):
    """The Spotify authorization URL — shown so the user can open it anywhere."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url


class LogMessage(Message):
    """One formatted log line forwarded from the ``logging`` system to the UI."""

    def __init__(self, text: str, level: str = "info") -> None:
        super().__init__()
        self.text = text
        self.level = level
