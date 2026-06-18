"""Run a sync: build the plan with live progress, then optionally apply it."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ProgressBar, RichLog, Static

from ...core.events import (
    Advanced,
    CollectionSkipped,
    CollectionStarted,
    OpApplied,
    PhaseStarted,
    TrackMatched,
)
from ...core.models import CollectionKind, MatchOutcome, OpKind
from .. import log_bridge
from ..messages import ApplyDone, EngineEvent, LogMessage, PlanReady, WorkerFailed

# How an AUTO match will be applied, by the collection it came from.
_ADD_VERB = {
    CollectionKind.PLAYLIST: "add to playlist",
    CollectionKind.LIKED_SONGS: "like",
    CollectionKind.LIKED_ALBUMS: "save album",
}


class SyncScreen(Screen):
    BINDINGS = [
        ("a", "apply", "Apply"),
        ("escape", "back", "Back"),
        ("l", "toggle_log", "Logs"),
    ]

    def __init__(self, *, dry_run: bool) -> None:
        super().__init__()
        self.dry_run = dry_run
        self._plan = None
        self._apply_ready = False  # gates the Apply action (and its footer hint)
        self.apply_stats: dict | None = None  # set when an apply completes
        self._log_handler = None
        self._coll_name = ""  # active collection (for the per-track counter + "Where")
        self._coll_kind: CollectionKind | None = None  # active collection kind
        self._total = 0       # tracks in the active collection
        self._matched = 0     # tracks resolved so far in it

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sync"):
            banner = "DRY RUN — nothing will be changed" if self.dry_run else "Sync"
            yield Static(banner, id="mode")
            yield Static("Building plan…", id="phase")
            yield ProgressBar(total=None, show_eta=False, id="bar")
            table = DataTable(id="rows")
            table.add_columns("Source", "→ YouTube Music", "Where", "Conf", "Action")
            yield table
            yield Static("", id="summary")
            # Live log panel — hidden until toggled with `l`. Streams the same
            # breadcrumbs that go to the log file (which track is matching, big
            # fetches, rate-limit waits, errors) so a slow/stuck run is legible.
            yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        panel = self.query_one("#log", RichLog)
        panel.display = False
        panel.write(f"Full log: {self.app.paths.log_file}")
        panel.write("Press 'l' to hide this panel.")
        # Forward s2y log records into the panel for the life of this screen.
        self._log_handler = log_bridge.install(self)
        self.run_plan()

    def on_unmount(self) -> None:
        log_bridge.remove(self._log_handler)
        self._log_handler = None

    def _emit(self, event) -> None:
        # Called from a worker thread; post_message is thread-safe.
        self.post_message(EngineEvent(event))

    # ----- workers ---------------------------------------------------------- #
    @work
    async def run_plan(self) -> None:
        try:
            # Resolve (and lazily build) the engine INSIDE the thread — building it
            # opens the DB and the API clients, which would block the UI thread.
            plan = await asyncio.to_thread(lambda: self.app.engine().build_plan(self._emit))
        except Exception as exc:  # noqa: BLE001
            self.post_message(WorkerFailed("plan", str(exc)))
            return
        self.post_message(PlanReady(plan))

    @work
    async def run_apply(self) -> None:
        try:
            stats = await asyncio.to_thread(
                lambda: self.app.engine().apply_plan(self._plan, self._emit)
            )
        except Exception as exc:  # noqa: BLE001
            self.post_message(WorkerFailed("apply", str(exc)))
            return
        self.post_message(ApplyDone(stats))

    # ----- engine events ---------------------------------------------------- #
    def on_engine_event(self, message: EngineEvent) -> None:
        event = message.event
        if isinstance(event, CollectionStarted):
            self._coll_name = event.name
            self._coll_kind = event.kind
            self._total = event.source_count
            self._matched = 0
            self.query_one("#phase", Static).update(
                f"Planning: {event.name} ({event.source_count} tracks)"
            )
        elif isinstance(event, CollectionSkipped):
            self.query_one("#phase", Static).update(f"Unchanged: {event.name}")
        elif isinstance(event, TrackMatched):
            self._matched += 1
            self.query_one("#phase", Static).update(
                f"Planning: {self._coll_name} ({self._matched}/{self._total})"
            )
            self._add_row(event.result)
        elif isinstance(event, PhaseStarted) and event.phase == "apply":
            self.query_one("#phase", Static).update("Applying…")
            self.query_one("#bar", ProgressBar).update(total=event.total or None, progress=0)
        elif isinstance(event, Advanced):
            self.query_one("#bar", ProgressBar).advance(event.amount)
        elif isinstance(event, OpApplied) and not event.ok:
            self.query_one("#phase", Static).update(f"[red]failed:[/] {event.op.describe()}")

    def on_log_message(self, message: LogMessage) -> None:
        self.query_one("#log", RichLog).write(message.text)

    def _add_row(self, result) -> None:
        table = self.query_one("#rows", DataTable)
        matched = result.best.title if result.best else "—"
        where = self._coll_name or "—"
        table.add_row(
            result.source.title,
            matched,
            where,
            f"{result.confidence:.2f}",
            self._action_label(result.outcome),
        )

    def _action_label(self, outcome: MatchOutcome) -> str:
        """What will happen to this track, by outcome and destination collection."""
        if outcome is MatchOutcome.QUARANTINE:
            return "review"
        if outcome is MatchOutcome.NO_MATCH:
            return "no match"
        return _ADD_VERB.get(self._coll_kind, "add")  # AUTO

    # ----- result messages -------------------------------------------------- #
    def on_plan_ready(self, message: PlanReady) -> None:
        self._plan = message.plan
        plan = message.plan
        self.query_one("#phase", Static).update("Plan ready")
        creates = sum(1 for op in plan.ops if op.kind is OpKind.CREATE_PLAYLIST)
        adds = len(plan.ops) - creates
        parts = [f"{adds} adds"]
        if creates:
            parts.append(f"{creates} new playlist{'s' if creates != 1 else ''}")
        parts.append(f"{len(plan.quarantine)} need review")
        parts.append(f"{plan.skipped} unchanged")
        self.query_one("#summary", Static).update(" · ".join(parts))
        self._apply_ready = not self.dry_run and bool(plan.ops)
        self.refresh_bindings()  # re-evaluate check_action so the footer shows/hides "Apply"

    def on_apply_done(self, message: ApplyDone) -> None:
        s = message.stats
        self.apply_stats = s
        self.query_one("#phase", Static).update("Done")
        self.query_one("#summary", Static).update(
            f"Applied {s['applied']} · failed {s['failed']} · "
            f"{s['quarantined']} in review"
        )

    def on_worker_failed(self, message: WorkerFailed) -> None:
        self.query_one("#phase", Static).update(
            f"[red]Error in {message.where}:[/] {message.detail}"
        )

    # ----- controls --------------------------------------------------------- #
    def action_apply(self) -> None:
        if not self._apply_ready or self._plan is None:
            return
        self._apply_ready = False
        self.refresh_bindings()
        self.query_one("#phase", Static).update("Applying…")
        self.run_apply()

    def check_action(self, action: str, parameters):
        # Only advertise/allow Apply once a non-dry-run plan with ops is ready.
        if action == "apply":
            return self._apply_ready
        return True

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_toggle_log(self) -> None:
        panel = self.query_one("#log", RichLog)
        panel.display = not panel.display
