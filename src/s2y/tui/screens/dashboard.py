"""The home screen: status at a glance and the main actions."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class DashboardScreen(Screen):
    BINDINGS = [
        ("d", "dry", "Dry run"),
        ("s", "sync", "Sync now"),
        ("r", "review", "Review"),
        ("g", "settings", "Settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="dashboard"):
            yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def on_screen_resume(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        repo = self.app.repo
        last = repo.last_run()
        quarantined = repo.count_unresolved_quarantine()
        collections = repo.list_collections()
        when = "never" if last is None else f"{last['started_at']} ({last['status']})"
        lines = [
            "[b]s2y[/b] — Spotify → YouTube Music",
            "",
            f"Last sync:    {when}",
            f"Collections:  {len(collections)} mapped",
            f"Quarantine:   {quarantined} awaiting review",
            "",
            "Keys:  [b]d[/b] dry-run · [b]s[/b] sync · [b]r[/b] review · [b]g[/b] settings · [b]q[/b] quit",
        ]
        self.query_one("#status", Static).update("\n".join(lines))

    # ----- actions ---------------------------------------------------------- #
    def action_dry(self) -> None:
        self._open_sync(dry_run=True)

    def action_sync(self) -> None:
        self._open_sync(dry_run=False)

    def action_review(self) -> None:
        from .review import QuarantineScreen

        self.app.push_screen(QuarantineScreen())

    def action_settings(self) -> None:
        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen())

    def _open_sync(self, *, dry_run: bool) -> None:
        from .sync import SyncScreen

        self.app.push_screen(SyncScreen(dry_run=dry_run))
