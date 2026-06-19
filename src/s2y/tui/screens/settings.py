"""Settings: collection toggles, pacing, safety, and re-running setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

if TYPE_CHECKING:
    from ..app import S2YApp


class SettingsScreen(Screen):
    if TYPE_CHECKING:
        app: S2YApp  # type: ignore[assignment]  # narrow Textual's App to ours

    BINDINGS = [("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        cfg = self.app.config
        yield Header()
        with VerticalScroll(id="settings"):
            with Vertical(classes="card"):
                yield Label("[b]Collections[/b]")
                # The picker (PlaylistsScreen) is the single owner of which
                # playlists sync — here we only show a summary and a way in.
                yield Static(_playlists_summary(cfg.collections), id="pl_summary")
                yield Button("Choose playlists…", id="choose_pl")
                yield Checkbox("Liked Songs", value=cfg.collections.sync_liked_songs, id="c_ls")
                yield Checkbox("Liked Albums", value=cfg.collections.sync_liked_albums, id="c_la")

            with Vertical(classes="card"):
                yield Label("[b]Pacing & safety[/b]")
                yield Static(
                    "Seconds to pause between writes (avoids rate limits).", classes="help"
                )
                yield Input(value=str(cfg.sync.track_sleep), id="track_sleep")
                yield Static(
                    "Circuit breaker: abort if a diff would remove more than "
                    "this % of a collection (Phase 3).",
                    classes="help",
                )
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
        # sync_playlists / selected_playlists are owned by the playlist picker;
        # Settings never writes them. Liked Songs/Albums stay all-or-nothing here.
        cfg.collections.sync_liked_songs = self.query_one("#c_ls", Checkbox).value
        cfg.collections.sync_liked_albums = self.query_one("#c_la", Checkbox).value
        cfg.sync.track_sleep = _as_float(
            self.query_one("#track_sleep", Input).value, cfg.sync.track_sleep
        )
        cfg.sync.circuit_breaker_pct = _as_int(
            self.query_one("#cb_pct", Input).value, cfg.sync.circuit_breaker_pct
        )
        self.app.save_config()
        self.query_one("#settings_status", Static).update("[green]Saved.[/]")

    @on(Button.Pressed, "#choose_pl")
    def _choose_pl(self) -> None:
        from .playlists import PlaylistsScreen

        self.app.push_screen(PlaylistsScreen())

    def on_screen_resume(self) -> None:
        # Returning from the picker: reflect the new selection in the summary.
        self.query_one("#pl_summary", Static).update(
            _playlists_summary(self.app.config.collections)
        )

    @on(Button.Pressed, "#resetup")
    def _resetup(self) -> None:
        from .setup import SetupScreen

        self.app.switch_screen(SetupScreen())

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()


def _playlists_summary(cols) -> str:
    if not cols.sync_playlists:
        state = "none"
    elif not cols.selected_playlists:
        state = "all"
    else:
        n = len(cols.selected_playlists)
        state = f"{n} selected"
    return f"Playlists: [b]{state}[/b]"


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
