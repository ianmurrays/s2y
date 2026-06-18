"""TUI tests via Textual's headless pilot, with a fake engine (no network)."""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from s2y.config import Config, Paths
from s2y.core.events import Advanced, CollectionStarted, OpApplied, PhaseStarted, TrackMatched
from s2y.core.models import (
    CollectionKind,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    SongInfo,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from s2y.tui import S2YApp
from s2y.tui.screens.dashboard import DashboardScreen
from s2y.tui.screens.review import QuarantineScreen
from s2y.tui.screens.setup import SetupScreen
from s2y.tui.screens.sync import SyncScreen


class FakeEngine:
    def __init__(self, repo):
        self.repo = repo
        src = SongInfo("Song", "Artist", spotify_uri="spotify:track:1")
        self.match = MatchResult(src, MatchOutcome.AUTO, YtCandidate("v1", "Song", "Artist"),
                                 MatchMethod.META, 0.95)
        self.op = SyncOp(OpKind.ADD_PLAYLIST_ITEM, "Chill", CollectionKind.PLAYLIST,
                         collection_key="sp1", source=src, video_id="v1")
        qsrc = SongInfo("Weird", "Nobody", spotify_uri="spotify:track:2")
        self.q = MatchResult(qsrc, MatchOutcome.QUARANTINE, YtCandidate("vq", "Weirdish", "Nob"),
                             MatchMethod.FUZZY, 0.4, reason="low_conf")

    def build_plan(self, on_event=lambda e: None):
        on_event(CollectionStarted("Chill", CollectionKind.PLAYLIST, 2))
        on_event(TrackMatched(self.match))
        on_event(TrackMatched(self.q))
        return SyncPlan(ops=[self.op], quarantine=[self.q], matches=[self.match])

    def apply_plan(self, plan, on_event=lambda e: None):
        on_event(PhaseStarted("apply", total=len(plan.ops)))
        for op in plan.ops:
            on_event(OpApplied(op, ok=True))
            on_event(Advanced(1))
        return {"applied": len(plan.ops), "failed": 0,
                "quarantined": len(plan.quarantine), "skipped": 0}


def make_app(*, configured=True):
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = Paths(config_dir=tmp / "c", data_dir=tmp / "d")
    app = S2YApp(config=Config(), paths=paths, configured=configured,
                 engine_factory=None)
    app._engine_factory = lambda: FakeEngine(app.repo)  # noqa: SLF001
    return app


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause()
    return False


@pytest.mark.asyncio
async def test_boots_to_setup_when_unconfigured():
    app = make_app(configured=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_setup_prefills_redirect_uri_and_shows_auth_url():
    from textual.widgets import Input

    from s2y.tui.messages import AuthUrl

    app = make_app(configured=False)
    app.config.spotify.redirect_uri = "https://host.tailnet.ts.net/callback"
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        # The redirect URI input is prefilled with the effective URI.
        assert app.screen.query_one("#sp_redirect", Input).value == (
            "https://host.tailnet.ts.net/callback"
        )
        # An authorize URL posted from the worker lands in a selectable field.
        app.screen.post_message(AuthUrl("https://accounts.spotify.com/authorize?x=1"))
        await _wait(pilot, lambda: app.screen.auth_url is not None)
        assert app.screen.auth_url == "https://accounts.spotify.com/authorize?x=1"
        assert app.screen.query_one("#sp_auth_url", Input).value == (
            "https://accounts.spotify.com/authorize?x=1"
        )


@pytest.mark.asyncio
async def test_dry_run_builds_plan_without_apply():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("d")  # dry run
        ok = await _wait(pilot, lambda: isinstance(app.screen, SyncScreen)
                         and app.screen._plan is not None)
        assert ok
        assert len(app.screen._plan.ops) == 1
        # Dry run never enables Apply: the action is a no-op.
        assert app.screen._apply_ready is False
        await pilot.press("a")  # no-op in dry run
        await pilot.pause()
        assert app.screen.apply_stats is None


@pytest.mark.asyncio
async def test_sync_screen_tracks_per_track_progress():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")  # dry run
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen)
                    and app.screen._plan is not None)
        # CollectionStarted set the total; each TrackMatched advanced the counter.
        assert app.screen._total == 2
        assert app.screen._matched == 2


@pytest.mark.asyncio
async def test_sync_table_shows_destination_and_action():
    from textual.widgets import DataTable

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen)
                    and app.screen._plan is not None)
        table = app.screen.query_one("#rows", DataTable)
        # Columns: Source, → YouTube Music, Where, Conf, Action
        first = table.get_row_at(0)
        assert first[2] == "Chill"            # Where = the collection it came from
        assert first[4] == "add to playlist"  # an AUTO match into a playlist
        actions = [table.get_row_at(r)[4] for r in range(table.row_count)]
        assert "review" in actions            # the quarantined track is flagged


@pytest.mark.asyncio
async def test_sync_screen_log_panel_toggles_and_receives_messages():
    from textual.widgets import RichLog

    from s2y.tui.messages import LogMessage

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen))
        rlog = app.screen.query_one("#log", RichLog)
        assert rlog.display is False  # hidden by default
        await pilot.press("l")  # toggle on
        await pilot.pause()
        assert rlog.display is True
        before = len(rlog.lines)
        app.screen.post_message(LogMessage("a streamed log line"))
        grew = await _wait(pilot, lambda: len(rlog.lines) > before)
        assert grew


@pytest.mark.asyncio
async def test_sync_applies_plan():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")  # sync now
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen)
                    and app.screen._plan is not None)
        assert app.screen._apply_ready is True
        await pilot.press("a")
        applied = await _wait(pilot, lambda: app.screen.apply_stats is not None)
        assert applied
        assert app.screen.apply_stats["applied"] == 1
        assert app.screen.apply_stats["failed"] == 0


@pytest.mark.asyncio
async def test_quit_signals_stop_and_exits():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("s")  # sync now → SyncScreen
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen))
        assert app.stop.is_set() is False
        await pilot.press("q")  # quit
        await pilot.pause()
    # action_quit told in-flight engine work to wind down before exiting.
    assert app.stop.is_set() is True


@pytest.mark.asyncio
async def test_leaving_sync_screen_signals_stop_then_rearms():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")  # dry run → SyncScreen
        await _wait(pilot, lambda: isinstance(app.screen, SyncScreen))
        await pilot.press("escape")  # back to the dashboard
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))
        # Unmount asked the worker to wind down…
        assert app.stop.is_set() is True
        # …and a fresh sync re-arms the signal so it isn't pre-cancelled.
        await pilot.press("d")
        rearmed = await _wait(
            pilot,
            lambda: isinstance(app.screen, SyncScreen) and not app.stop.is_set(),
        )
        assert rearmed


@pytest.mark.asyncio
async def test_quarantine_accept_writes_mapping():
    app = make_app()
    # Seed a quarantined track with a stored candidate.
    app.repo.add_quarantine(
        source_side="spotify", source_uri="spotify:track:2", title="Weird",
        artist="Nobody", album=None, reason="low_conf",
        best_candidate={"video_id": "vq", "title": "Weirdish", "confidence": 0.4},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")  # review quarantine
        await _wait(pilot, lambda: isinstance(app.screen, QuarantineScreen))
        await pilot.pause()
        await pilot.press("a")  # accept best
        resolved = await _wait(pilot, lambda: app.repo.count_unresolved_quarantine() == 0)
        assert resolved
        row = app.repo.get_track_by_spotify_uri("spotify:track:2")
        assert row is not None and row.yt_video_id == "vq" and row.verified is True
