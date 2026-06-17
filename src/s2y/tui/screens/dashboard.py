"""The home screen: status at a glance and the main actions."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static


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
            with Horizontal(id="actions"):
                yield Button("Dry run", id="dry", variant="primary")
                yield Button("Sync now", id="sync", variant="success")
                yield Button("Review quarantine", id="review")
                yield Button("Settings", id="settings")
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
            "Dry run shows the plan without changing anything. Sync applies it.",
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

    @on(Button.Pressed, "#dry")
    def _on_dry(self) -> None:
        self.action_dry()

    @on(Button.Pressed, "#sync")
    def _on_sync(self) -> None:
        self.action_sync()

    @on(Button.Pressed, "#review")
    def _on_review(self) -> None:
        self.action_review()

    @on(Button.Pressed, "#settings")
    def _on_settings(self) -> None:
        self.action_settings()
