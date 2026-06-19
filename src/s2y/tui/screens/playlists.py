"""Choose which Spotify playlists to sync.

The backend already filters on ``config.collections.selected_playlists`` (empty
list = sync all; non-empty = an allow-list of Spotify playlist ids). This screen
is the UI for editing that list: it loads the live playlists off the UI thread
(same worker + cancellation pattern as :class:`SyncScreen`), shows them in a
``SelectionList`` pre-checked from the current config, and on Save maps the
checked set to the ``sync_playlists`` master toggle + ``selected_playlists``.

Save semantics are *always-explicit*: whatever is checked is saved verbatim, so a
playlist created on Spotify later won't sync until the user checks it here.

The ``[] == all`` trap: the planner reads an empty ``selected_playlists`` as "sync
ALL". So the picker never persists ``[]`` to mean "the user chose none" — zero
checked flips ``sync_playlists`` to ``False`` instead (the planner then skips
playlists entirely, never reaching the ``[]==all`` branch).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from ..messages import PlaylistsLoaded, WorkerFailed
from ..threads import run_detached

if TYPE_CHECKING:
    from ..app import S2YApp

_HELP = (
    "Checked playlists sync. New Spotify playlists don't sync until you check them. "
    "Uncheck all to stop syncing playlists entirely."
)


class PlaylistsScreen(Screen):
    if TYPE_CHECKING:
        app: S2YApp  # type: ignore[assignment]  # narrow Textual's App to ours

    BINDINGS = [("escape", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._loaded = False  # gates Save: never write a selection before data arrives

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="playlists"):
            with Vertical(classes="card"):
                yield Label("[b]Choose playlists to sync[/b]")
                yield Static(_HELP, classes="help")
                yield SelectionList(id="pl_list")  # populated by the worker
            with Horizontal(id="playlists_controls"):
                yield Button("Select all", id="pl_all", disabled=True)
                yield Button("Select none", id="pl_none", disabled=True)
                yield Button("Save", id="pl_save", variant="success", disabled=True)
                yield Button("Back", id="pl_back")
            yield Static("", id="playlists_status", classes="status")
        yield Footer()

    # ----- lifecycle (mirrors SyncScreen) ----------------------------------- #
    def on_mount(self) -> None:
        self.query_one("#playlists_status", Static).update("Loading playlists…")
        self.app.stop.clear()  # re-arm: a prior screen's unmount may have set it
        self.load_playlists()

    def on_unmount(self) -> None:
        # Leaving the screen (Esc/quit) winds down an in-flight load.
        self.app.stop.set()

    # ----- worker ----------------------------------------------------------- #
    @work
    async def load_playlists(self) -> None:
        try:
            # Resolve (and lazily build) the engine INSIDE the thread — building it
            # opens the API clients, which would block the UI thread.
            pls = await run_detached(lambda: self.app.engine().spotify.list_playlists())
        except Exception as exc:  # noqa: BLE001
            self.post_message(WorkerFailed("playlists", str(exc)))
            return
        self.post_message(PlaylistsLoaded(pls))

    # ----- result messages -------------------------------------------------- #
    def on_playlists_loaded(self, message: PlaylistsLoaded) -> None:
        if not self.is_mounted:  # a late post to an unmounted screen
            return
        cols = self.app.config.collections
        selected = set(cols.selected_playlists)
        # [] sentinel means "all" — but only while playlists are actually enabled.
        check_all = (not selected) and cols.sync_playlists
        sel = self.query_one("#pl_list", SelectionList)
        sel.clear_options()
        for pl in message.playlists:
            initial = (pl.id in selected) if selected else check_all
            # The Spotify id goes in `value` only — ids often start with a digit,
            # which is an invalid Textual DOM id.
            sel.add_option(Selection(f"{pl.name}  ({pl.count})", pl.id, initial))
        self._loaded = True
        for bid in ("#pl_save", "#pl_all", "#pl_none"):
            self.query_one(bid, Button).disabled = False
        n = len(message.playlists)
        self.query_one("#playlists_status", Static).update(
            f"{n} playlist{'s' if n != 1 else ''}. Checked ones will sync."
            if n
            else "No playlists found on Spotify."
        )

    def on_worker_failed(self, message: WorkerFailed) -> None:
        if message.where != "playlists":
            return
        # Save stays disabled: never overwrite a saved selection from a failed load.
        self.query_one("#playlists_status", Static).update(
            f"[red]Couldn't load playlists: {message.detail}[/]"
        )

    # ----- controls --------------------------------------------------------- #
    @on(Button.Pressed, "#pl_all")
    def _select_all(self) -> None:
        self.query_one("#pl_list", SelectionList).select_all()

    @on(Button.Pressed, "#pl_none")
    def _select_none(self) -> None:
        self.query_one("#pl_list", SelectionList).deselect_all()

    @on(Button.Pressed, "#pl_save")
    def _save(self) -> None:
        if not self._loaded:
            return
        ids = sorted(self.query_one("#pl_list", SelectionList).selected)
        cols = self.app.config.collections
        # >=1 checked -> enable + explicit allow-list; 0 checked -> disable playlists.
        # This makes []+sync_playlists=True (the "sync ALL" state) unreachable here.
        cols.sync_playlists = bool(ids)
        cols.selected_playlists = ids
        self.app.save_config()
        n = len(ids)
        self.query_one("#playlists_status", Static).update(
            f"[green]Saved — {n} playlist{'s' if n != 1 else ''} will sync.[/]"
            if n
            else "[green]Saved — playlists will not sync.[/]"
        )

    @on(Button.Pressed, "#pl_back")
    def _back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()
