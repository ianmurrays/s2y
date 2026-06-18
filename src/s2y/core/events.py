"""Progress events emitted by the engine during planning and applying.

The engine is pure and UI-agnostic: it takes an ``on_event`` callback and calls
it with these plain dataclasses. The CLI prints them; the Textual UI translates
them into ``Message`` objects via ``post_message`` (never touching widgets from a
worker thread). Keeping events as plain data keeps the engine decoupled from both.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import CollectionKind, MatchResult, SyncOp


@dataclass(frozen=True)
class PhaseStarted:
    """A top-level phase began (e.g. "plan", "apply")."""

    phase: str
    total: int | None = None  # total units of work, if known


@dataclass(frozen=True)
class PhaseFinished:
    phase: str


@dataclass(frozen=True)
class CollectionStarted:
    name: str
    kind: CollectionKind
    source_count: int


@dataclass(frozen=True)
class CollectionSkipped:
    """A collection was unchanged since the last run (cheap change-detect)."""

    name: str
    kind: CollectionKind
    reason: str = "unchanged"


@dataclass(frozen=True)
class DedupDegraded:
    """A live YouTube dedup read failed (e.g. session signed out); planning fell
    back to the locally-persisted membership ledger instead of aborting the run."""

    name: str
    detail: str = ""


@dataclass(frozen=True)
class TrackMatched:
    """One source track was resolved (or quarantined) during planning."""

    result: MatchResult


@dataclass(frozen=True)
class OpApplied:
    op: SyncOp
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class Advanced:
    """Generic progress tick for the active progress bar."""

    amount: int = 1


@dataclass(frozen=True)
class LogLine:
    message: str
    level: str = "info"  # info | warning | error


# Any event the engine may emit.
Event = (
    PhaseStarted
    | PhaseFinished
    | CollectionStarted
    | CollectionSkipped
    | DedupDegraded
    | TrackMatched
    | OpApplied
    | Advanced
    | LogLine
)

# The callback the engine accepts. A no-op default keeps callers simple.
EventSink = Callable[[Event], None]


def null_sink(_event: Event) -> None:
    """An :data:`EventSink` that discards everything (useful in tests)."""
