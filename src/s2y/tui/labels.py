"""Human-readable labels for sync directions (UI-only concern)."""

from __future__ import annotations

from ..core.models import FORWARD, REVERSE, Direction, Side

_SERVICE = {Side.SPOTIFY: "Spotify", Side.YT: "YouTube Music"}

# The dashboard cycles through these per-run choices (matches config.sync.direction).
CHOICES = ("spotify_to_yt", "yt_to_spotify", "both")


def service_name(side: Side) -> str:
    return _SERVICE[side]


def target_label(direction: Direction) -> str:
    return service_name(direction.target)


def direction_label(direction: Direction) -> str:
    return f"{service_name(direction.source)} → {service_name(direction.target)}"


def directions_for_choice(choice: str) -> list[Direction]:
    if choice == "yt_to_spotify":
        return [REVERSE]
    if choice == "both":
        return [FORWARD, REVERSE]
    return [FORWARD]


def choice_label(choice: str) -> str:
    if choice == "both":
        return "Both ways (Spotify → YouTube Music, then back)"
    return direction_label(directions_for_choice(choice)[0])
