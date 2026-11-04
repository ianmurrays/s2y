"""The home screen: status at a glance and the main actions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from ..labels import CHOICES, choice_label, directions_for_choice

if TYPE_CHECKING:
    from ..app import S2YApp


class DashboardScreen(Screen):
    if TYPE_CHECKING:
        app: S2YApp  # type: ignore[assignment]  # narrow Textual's App to ours

    BINDINGS = [
        ("d", "dry", "Dry run"),
        ("s", "sync", "Sync now"),
        ("x", "cycle_direction", "Direction"),
        ("p", "playlists", "Playlists"),
        ("r", "review", "Review"),
        ("g", "settings", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="dashboard"):
            yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        # Per-run direction choice, seeded from the saved default.
        choice = self.app.config.sync.direction
        self._choice = choice if choice in CHOICES else "spotify_to_yt"
        self._refresh()

    def on_screen_resume(self) -> None:
        # Settings (or another screen) may have changed the default direction while we
        # were away — re-sync the per-run choice from config so the header and s/d match.
        choice = self.app.config.sync.direction
        if choice in CHOICES:
            self._choice = choice
        self._refresh()

    def _refresh(self) -> None:
        repo = self.app.repo
        last = repo.last_run()
        quarantined = repo.count_unresolved_quarantine()
        collections = repo.list_collections()
        if last is None:
            when = "never"
        elif last["status"] in ("running", "aborted", "error"):
            # A previous sync didn't finish cleanly. Re-running picks up where it
            # left off (cached matches + dedup against what already landed).
            when = f"{last['started_at']} — interrupted; run Sync to resume"
        else:
            when = f"{last['started_at']} ({last['status']})"
        lines = [
            "[b]s2y[/b] — Spotify ⇄ YouTube Music",
            "",
            f"Direction:    [b]{choice_label(self._choice)}[/b]  (press x to change)",
            f"Last sync:    {when}",
            f"Collections:  {len(collections)} mapped",
            f"Quarantine:   {quarantined} awaiting review",
            "",
            "Keys:  [b]d[/b] dry-run · [b]s[/b] sync · [b]x[/b] direction · "
            "[b]p[/b] playlists · [b]r[/b] review · [b]g[/b] settings · [b]q[/b] quit",
        ]
        self.query_one("#status", Static).update("\n".join(lines))

    # ----- actions ---------------------------------------------------------- #
    def action_cycle_direction(self) -> None:
        nxt = CHOICES[(CHOICES.index(self._choice) + 1) % len(CHOICES)]
        self._choice = nxt
        # Persist as the new default so the choice sticks across runs.
        self.app.config.sync.direction = nxt
        self.app.save_config()
        self._refresh()

    def action_dry(self) -> None:
        self._open_sync(dry_run=True)

    def action_sync(self) -> None:
        self._open_sync(dry_run=False)

    def action_playlists(self) -> None:
        from .playlists import PlaylistsScreen

        self.app.push_screen(PlaylistsScreen())

    def action_review(self) -> None:
        from .review import QuarantineScreen

        self.app.push_screen(QuarantineScreen())

    def action_settings(self) -> None:
        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen())

    def _open_sync(self, *, dry_run: bool) -> None:
        from .sync import SyncScreen

        self.app.push_screen(
            SyncScreen(dry_run=dry_run, directions=directions_for_choice(self._choice))
        )
