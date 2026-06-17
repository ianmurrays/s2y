"""Settings: collection toggles, pacing, safety, and re-running setup."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static


class SettingsScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        cfg = self.app.config
        yield Header()
        with VerticalScroll(id="settings"):
            with Vertical(classes="card"):
                yield Label("[b]Collections[/b]")
                yield Checkbox("Playlists", value=cfg.collections.sync_playlists, id="c_pl")
                yield Checkbox("Liked Songs", value=cfg.collections.sync_liked_songs, id="c_ls")
                yield Checkbox("Liked Albums", value=cfg.collections.sync_liked_albums, id="c_la")

            with Vertical(classes="card"):
                yield Label("[b]Pacing & safety[/b]")
                yield Static("Seconds to pause between writes (avoids rate limits).",
                             classes="help")
                yield Input(value=str(cfg.sync.track_sleep), id="track_sleep")
                yield Static("Circuit breaker: abort if a diff would remove more than "
                             "this % of a collection (Phase 3).", classes="help")
                yield Input(value=str(cfg.sync.circuit_breaker_pct), id="cb_pct")

            with Horizontal(id="settings_controls"):
                yield Button("Save", id="save", variant="success")
                yield Button("Re-run setup", id="resetup")
                yield Button("Back", id="back")
            yield Static("", id="settings_status", classes="status")
        yield Footer()

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        cfg = self.app.config
        cfg.collections.sync_playlists = self.query_one("#c_pl", Checkbox).value
        cfg.collections.sync_liked_songs = self.query_one("#c_ls", Checkbox).value
        cfg.collections.sync_liked_albums = self.query_one("#c_la", Checkbox).value
        cfg.sync.track_sleep = _as_float(self.query_one("#track_sleep", Input).value, cfg.sync.track_sleep)
        cfg.sync.circuit_breaker_pct = _as_int(self.query_one("#cb_pct", Input).value, cfg.sync.circuit_breaker_pct)
        self.app.save_config()
        self.query_one("#settings_status", Static).update("[green]Saved.[/]")

    @on(Button.Pressed, "#resetup")
    def _resetup(self) -> None:
        from .setup import SetupScreen

        self.app.switch_screen(SetupScreen())

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()


def _as_float(value: str, fallback: float) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        return fallback


def _as_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except ValueError:
        return fallback
