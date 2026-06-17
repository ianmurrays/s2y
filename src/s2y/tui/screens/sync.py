"""Run a sync: build the plan with live progress, then optionally apply it."""

from __future__ import annotations

import asyncio

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, ProgressBar, Static

from ...core.events import (
    Advanced,
    CollectionSkipped,
    CollectionStarted,
    OpApplied,
    PhaseStarted,
    TrackMatched,
)
from ...core.models import MatchOutcome
from ..messages import ApplyDone, EngineEvent, PlanReady, WorkerFailed

_ACTION = {
    MatchOutcome.AUTO: "add",
    MatchOutcome.QUARANTINE: "review",
    MatchOutcome.NO_MATCH: "no match",
}


class SyncScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, *, dry_run: bool) -> None:
        super().__init__()
        self.dry_run = dry_run
        self._plan = None
        self.apply_stats: dict | None = None  # set when an apply completes

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sync"):
            banner = "DRY RUN — nothing will be changed" if self.dry_run else "Sync"
            yield Static(banner, id="mode")
            yield Static("Building plan…", id="phase")
            yield ProgressBar(total=None, show_eta=False, id="bar")
            table = DataTable(id="rows")
            table.add_columns("Source", "→ YouTube Music", "Conf", "Action")
            yield table
            yield Static("", id="summary")
            with Horizontal(id="controls"):
                yield Button("Apply", id="apply", variant="success", disabled=True)
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.run_plan()

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
            self.query_one("#phase", Static).update(
                f"Planning: {event.name} ({event.source_count} tracks)"
            )
        elif isinstance(event, CollectionSkipped):
            self.query_one("#phase", Static).update(f"Unchanged: {event.name}")
        elif isinstance(event, TrackMatched):
            self._add_row(event.result)
        elif isinstance(event, PhaseStarted) and event.phase == "apply":
            self.query_one("#phase", Static).update("Applying…")
            self.query_one("#bar", ProgressBar).update(total=event.total or None, progress=0)
        elif isinstance(event, Advanced):
            self.query_one("#bar", ProgressBar).advance(event.amount)
        elif isinstance(event, OpApplied) and not event.ok:
            self.query_one("#phase", Static).update(f"[red]failed:[/] {event.op.describe()}")

    def _add_row(self, result) -> None:
        table = self.query_one("#rows", DataTable)
        matched = result.best.title if result.best else "—"
        table.add_row(
            result.source.title,
            matched,
            f"{result.confidence:.2f}",
            _ACTION.get(result.outcome, "?"),
        )

    # ----- result messages -------------------------------------------------- #
    def on_plan_ready(self, message: PlanReady) -> None:
        self._plan = message.plan
        plan = message.plan
        self.query_one("#phase", Static).update("Plan ready")
        self.query_one("#summary", Static).update(
            f"{len(plan.ops)} actions · {len(plan.quarantine)} need review · "
            f"{plan.skipped} unchanged"
        )
        if not self.dry_run and plan.ops:
            self.query_one("#apply", Button).disabled = False

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
    @on(Button.Pressed, "#apply")
    def _apply(self) -> None:
        if self._plan is None:
            return
        self.query_one("#apply", Button).disabled = True
        self.query_one("#phase", Static).update("Applying…")
        self.run_apply()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()
