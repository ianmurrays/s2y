"""Quarantine review: resolve low-confidence / unmatched tracks by hand or AI.

Accepting the stored best candidate writes a *verified* mapping into the
``track_map`` (so the next sync picks it up and never asks again). Skipping marks
it resolved with no mapping. Pressing ``c`` runs the optional ``claude -p`` judge
over the whole list (when ``[classify] enabled``), auto-resolving the matches it
is confident about so only the genuinely ambiguous ones are left for you.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, ProgressBar, Static

from ...core.events import Advanced, ClassifyVerdictEvent, LogLine, PhaseStarted
from ...core.models import ClassifyDecision
from ...core.resolve import resolve_quarantine_match

if TYPE_CHECKING:
    from ..app import S2YApp
from ..messages import ClassifyDone, EngineEvent, WorkerFailed
from ..threads import run_detached


class QuarantineScreen(Screen):
    if TYPE_CHECKING:
        app: S2YApp  # type: ignore[assignment]  # narrow Textual's App to ours

    BINDINGS = [
        ("escape", "back", "Back"),
        ("a", "accept", "Accept best"),
        ("s", "skip", "Skip"),
        ("c", "classify", "Classify (AI)"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="quarantine"):
            yield Static("Quarantine — your call on these tracks", id="q_title")
            yield Static(
                "[b]a[/b] accept · [b]s[/b] skip · [b]c[/b] classify (AI) · [b]esc[/b] back",
                classes="help",
            )
            # Classify progress — hidden until a run starts (see action_classify).
            yield Static("", id="q_phase")
            yield ProgressBar(total=None, show_eta=False, id="q_bar")
            yield Static("", id="q_summary")
            table: DataTable[Any] = DataTable(id="q_table")
            table.cursor_type = "row"
            table.add_columns("Track", "Artist", "Best YT match", "Conf")
            yield table
        yield Footer()

    def on_mount(self) -> None:
        self._rows: list = []
        self._classifying = False  # guards manual actions while a batch runs
        self._reset_counts()
        # Progress widgets only appear during a classify run.
        self.query_one("#q_bar", ProgressBar).display = False
        self.query_one("#q_summary", Static).display = False
        self.app.stop.clear()  # re-arm: a prior screen's unmount may have set it
        self._load()

    def _reset_counts(self) -> None:
        self._counts = {"resolved": 0, "kept": 0, "rejected": 0, "unsure": 0}

    def on_unmount(self) -> None:
        # Leaving the screen winds down an in-flight classify pass (daemon thread).
        self.app.stop.set()

    def _load(self) -> None:
        table = self.query_one("#q_table", DataTable)
        table.clear()
        self._rows = list(self.app.repo.list_unresolved_quarantine())
        for row in self._rows:
            best = _best(row)
            table.add_row(
                row["title"],
                row["artist"],
                best.get("title", "—") if best else "—",
                "" if best is None else f"{(best.get('confidence') or 0):.2f}",
            )
        if not self._rows:
            self.query_one("#q_title", Static).update("Quarantine is empty — nothing to review.")
        else:
            self.query_one("#q_title", Static).update(
                f"Quarantine — {len(self._rows)} track(s) need your call"
            )

    def _selected(self):
        table = self.query_one("#q_table", DataTable)
        if not self._rows or table.cursor_row is None:
            return None
        return self._rows[table.cursor_row]

    # ----- actions ---------------------------------------------------------- #
    def action_accept(self) -> None:
        if self._classifying:
            return
        row = self._selected()
        if row is None:
            return
        best = _best(row)
        if not best or not best.get("video_id"):
            self.notify("No candidate to accept — skip it instead.", severity="warning")
            return
        resolve_quarantine_match(
            self.app.repo,
            quarantine_id=row["id"],
            spotify_uri=row["source_uri"],
            yt_video_id=best["video_id"],
            title=row["title"],
            artist=row["artist"],
            match_method="manual",
            match_confidence=1.0,
        )
        self.notify(f"Accepted: {row['title']}")
        self._load()

    def action_skip(self) -> None:
        if self._classifying:
            return
        row = self._selected()
        if row is None:
            return
        self.app.repo.skip_quarantine(row["id"])
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ----- classify (AI) ---------------------------------------------------- #
    def action_classify(self) -> None:
        if self._classifying:
            return
        if not self.app.config.classify.enabled:
            self.notify(
                "Classification is disabled — set [classify] enabled = true in your config.",
                severity="warning",
            )
            return
        if not self._rows:
            return
        self._classifying = True
        self._reset_counts()
        self.app.stop.clear()
        self.query_one("#q_title", Static).update("Classifying with Claude…")
        self.query_one("#q_phase", Static).update("Starting…")
        bar = self.query_one("#q_bar", ProgressBar)
        bar.display = True
        bar.update(total=len(self._rows), progress=0)
        summary = self.query_one("#q_summary", Static)
        summary.display = True
        summary.update("")
        self.run_classify()

    def _emit(self, event) -> None:
        # Called from the worker thread; post_message is thread-safe.
        self.post_message(EngineEvent(event))

    @work
    async def run_classify(self) -> None:
        try:
            # Build/resolve the engine INSIDE the thread (it opens the DB + clients).
            stats = await run_detached(lambda: self.app.engine().classify_quarantine(self._emit))
        except Exception as exc:  # noqa: BLE001
            self.post_message(WorkerFailed("classify", str(exc)))
            return
        self.post_message(ClassifyDone(stats))

    def on_engine_event(self, message: EngineEvent) -> None:
        event = message.event
        if isinstance(event, PhaseStarted) and event.phase == "classify":
            self._reset_counts()
            bar = self.query_one("#q_bar", ProgressBar)
            bar.display = True
            bar.update(total=event.total or None, progress=0)
            self.query_one("#q_summary", Static).display = True
            self.query_one("#q_phase", Static).update(f"Classifying {event.total} track(s)…")
        elif isinstance(event, LogLine):
            self.query_one("#q_phase", Static).update(event.message)
        elif isinstance(event, Advanced):
            self.query_one("#q_bar", ProgressBar).advance(event.amount)
        elif isinstance(event, ClassifyVerdictEvent):
            self._tally(event)

    def _tally(self, event: ClassifyVerdictEvent) -> None:
        if event.applied:
            self._counts["resolved"] += 1
        elif event.verdict.decision is ClassifyDecision.ACCEPT:
            self._counts["kept"] += 1  # accepted but below the auto-resolve threshold
        elif event.verdict.decision is ClassifyDecision.REJECT:
            self._counts["rejected"] += 1
        else:
            self._counts["unsure"] += 1
        c = self._counts
        self.query_one("#q_summary", Static).update(
            f"✓ {c['resolved']} resolved · {c['kept']} kept · "
            f"{c['rejected']} rejected · {c['unsure']} unsure"
        )

    def on_classify_done(self, message: ClassifyDone) -> None:
        self._classifying = False
        self.query_one("#q_bar", ProgressBar).display = False
        self.query_one("#q_phase", Static).update("Classification complete.")
        s = message.stats
        self.notify(
            f"Claude: {s['applied']} resolved · {s['rejected']} rejected · {s['uncertain']} unsure"
        )
        self._load()

    def on_worker_failed(self, message: WorkerFailed) -> None:
        self._classifying = False
        self.query_one("#q_bar", ProgressBar).display = False
        self.query_one("#q_phase", Static).update(f"[red]Classify failed:[/] {message.detail}")
        self._load()


def _best(row) -> dict | None:
    raw = row["best_candidate_json"]
    return json.loads(raw) if raw else None
