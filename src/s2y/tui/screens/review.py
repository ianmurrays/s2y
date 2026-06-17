"""Quarantine review: resolve low-confidence / unmatched tracks by hand.

Accepting the stored best candidate writes a *verified* mapping into the
``track_map`` (so the next sync picks it up and never asks again). Skipping marks
it resolved with no mapping.
"""

from __future__ import annotations

import json

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static

from ...core.matching import normalize_artist, normalize_title


class QuarantineScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("a", "accept", "Accept best"),
        ("s", "skip", "Skip"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="quarantine"):
            yield Static("Quarantine — your call on these tracks", id="q_title")
            table = DataTable(id="q_table")
            table.cursor_type = "row"
            table.add_columns("Track", "Artist", "Best YT match", "Conf")
            yield table
            with Horizontal(id="q_controls"):
                yield Button("Accept best", id="accept", variant="success")
                yield Button("Skip", id="skip")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self._rows: list = []
        self._load()

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
        row = self._selected()
        if row is None:
            return
        best = _best(row)
        if not best or not best.get("video_id"):
            self.notify("No candidate to accept — skip it instead.", severity="warning")
            return
        repo = self.app.repo
        with repo.transaction():
            track = repo.upsert_track(
                spotify_uri=row["source_uri"],
                yt_video_id=best["video_id"],
                canon_title=normalize_title(row["title"]),
                canon_artist=normalize_artist(row["artist"]),
                match_method="manual",
                match_confidence=1.0,
                verified=True,
            )
            repo.resolve_quarantine(row["id"], track.id)
            # The resolved track is now cached, but collections may be skipped by
            # change-detection next run — force a re-examine so it actually syncs.
            repo.invalidate_signatures()
        self.notify(f"Accepted: {row['title']}")
        self._load()

    def action_skip(self) -> None:
        row = self._selected()
        if row is None:
            return
        self.app.repo.skip_quarantine(row["id"])
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#accept")
    def _on_accept(self) -> None:
        self.action_accept()

    @on(Button.Pressed, "#skip")
    def _on_skip(self) -> None:
        self.action_skip()

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.action_back()


def _best(row) -> dict | None:
    raw = row["best_candidate_json"]
    return json.loads(raw) if raw else None
