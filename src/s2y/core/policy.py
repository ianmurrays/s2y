"""Conflict-resolution policy.

v1 implements **union (adds-only)**: a track present on the source is added to the
destination; nothing is ever removed. The other policies and the circuit breaker
are defined now (so the shape is stable) but only exercised from Phase 2/3.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from enum import Enum
from typing import TypeVar

H = TypeVar("H", bound=Hashable)


class ConflictPolicy(str, Enum):
    UNION = "union"  # adds-only, never delete (v1 default)
    MIRROR = "mirror"  # destination becomes an exact copy of source
    TWO_WAY = "two_way"  # propagate adds AND tombstone-backed deletes (Phase 3)


def additions(desired_ordered: Iterable[H], present: set[H]) -> list[H]:
    """Items in ``desired_ordered`` not already ``present``, order-preserving + deduped."""
    seen: set[H] = set()
    out: list[H] = []
    for item in desired_ordered:
        if item in present or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


class CircuitBreaker:
    """Aborts a run if a single diff proposes removing too much of a collection.

    A partial/failed fetch looks identical to "the user deleted everything", so a
    percentage cap is a last-line guard before destructive ops (Phase 3).
    """

    def __init__(self, pct: float) -> None:
        self.pct = pct

    def trips(self, removing: int, total: int) -> bool:
        if total <= 0 or self.pct <= 0:
            return False
        return (removing / total) * 100.0 > self.pct
