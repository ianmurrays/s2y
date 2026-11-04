"""TUI tests via Textual's headless pilot, with a fake engine (no network)."""

from __future__ import annotations

import pathlib
import tempfile

import pytest
from textual.widgets import Button, SelectionList, Static

from s2y.config import Config, Paths
from s2y.core.events import (
    Advanced,
    ClassifyVerdictEvent,
    CollectionStarted,
    LogLine,
    OpApplied,
    PhaseFinished,
    PhaseStarted,
    TrackMatched,
)
from s2y.core.models import (
    ClassifyDecision,
    ClassifyVerdict,
    CollectionKind,
    MatchMethod,
    MatchOutcome,
    MatchResult,
    OpKind,
    RemotePlaylist,
    SongInfo,
    SyncOp,
    SyncPlan,
    YtCandidate,
)
from s2y.tui import S2YApp
from s2y.tui.screens.dashboard import DashboardScreen
from s2y.tui.screens.playlists import PlaylistsScreen
from s2y.tui.screens.review import QuarantineScreen
from s2y.tui.screens.settings import SettingsScreen
from s2y.tui.screens.setup import SetupScreen
from s2y.tui.screens.sync import SyncScreen


class _FakeSpotify:
    """Stub Spotify source for the playlist picker — no network."""

    def __init__(self, playlists=None):
        self._playlists = (
            playlists
            if playlists is not None
            else [
                RemotePlaylist("sp1", "Chill", 2, "s1"),
                RemotePlaylist("sp2", "Focus", 5, "s2"),
            ]
        )

    def list_playlists(self):
        return list(self._playlists)


class _RaisingSpotify:
    def list_playlists(self):
        raise RuntimeError("boom")


class FakeEngine:
    def __init__(self, repo, spotify=None):
        self.repo = repo
        self.spotify = spotify if spotify is not None else _FakeSpotify()
        # The playlist picker lists the source service via engine().source.
        self.source = self.spotify
        src = SongInfo("Song", "Artist", spotify_uri="spotify:track:1")
        self.match = MatchResult(
            src, MatchOutcome.AUTO, YtCandidate("v1", "Song", "Artist"), MatchMethod.META, 0.95
        )
        self.op = SyncOp(
            OpKind.ADD_PLAYLIST_ITEM,
            "Chill",
            CollectionKind.PLAYLIST,
            collection_key="sp1",
            source=src,
            video_id="v1",
        )
        qsrc = SongInfo("Weird", "Nobody", spotify_uri="spotify:track:2")
        self.q = MatchResult(
            qsrc,
            MatchOutcome.QUARANTINE,
            YtCandidate("vq", "Weirdish", "Nob"),
            MatchMethod.FUZZY,
            0.4,
            reason="low_conf",
        )

    def build_plan(self, on_event=lambda e: None, *, persist_matches=False):
        on_event(CollectionStarted("Chill", CollectionKind.PLAYLIST, 2))
        on_event(TrackMatched(self.match))
        on_event(TrackMatched(self.q))
        return SyncPlan(ops=[self.op], quarantine=[self.q], matches=[self.match])

    def apply_plan(self, plan, on_event=lambda e: None):
        on_event(PhaseStarted("apply", total=len(plan.ops)))
        for op in plan.ops:
            on_event(OpApplied(op, ok=True))
            on_event(Advanced(1))
        return {
            "applied": len(plan.ops),
            "failed": 0,
            "quarantined": len(plan.quarantine),
            "skipped": 0,
        }

    def classify_quarantine(self, on_event=lambda e: None, *, apply=True):
        # Mirror the engine: emit the same progress events (so the screen's bar /
        # status / counters update) and resolve each row with a stored candidate via
        # the shared helper (so the screen reload reflects real DB state).
        import json

        from s2y.core.resolve import resolve_quarantine_match

        rows = list(self.repo.list_unresolved_quarantine())
        on_event(PhaseStarted("classify", total=len(rows)))
        on_event(LogLine(f"Fetching YouTube candidates — tracks 1–{len(rows)} of {len(rows)}…"))
        on_event(LogLine(f"Asking Claude about {len(rows)} track(s)… (batch 1/1)"))
        applied = 0
        for row in rows:
            raw = row["best_candidate_json"]
            best = json.loads(raw) if raw else None
            ok = bool(apply and best and best.get("video_id"))
            if ok:
                resolve_quarantine_match(
                    self.repo,
                    quarantine_id=row["id"],
                    source_side=row["source_side"],
                    source_uri=row["source_uri"],
                    target_id=best["video_id"],
                    title=row["title"],
                    artist=row["artist"],
                    match_method="claude",
                    match_confidence=0.95,
                )
                applied += 1
            verdict = ClassifyVerdict(
                ClassifyDecision.ACCEPT if ok else ClassifyDecision.UNCERTAIN,
                best["video_id"] if ok else None,
                0.95 if ok else 0.0,
            )
            on_event(ClassifyVerdictEvent(row["title"], row["artist"], verdict, ok))
            on_event(Advanced(1))
        on_event(PhaseFinished("classify"))
        return {
            "considered": len(rows),
            "accepted": applied,
            "rejected": 0,
            "uncertain": 0,
            "applied": applied,
        }


def make_app(*, configured=True):
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = Paths(config_dir=tmp / "c", data_dir=tmp / "d")
    app = S2YApp(config=Config(), paths=paths, configured=configured, engine_factory=None)
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
        ok = await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
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
        await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
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
        await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
        table = app.screen.query_one("#rows", DataTable)
        # Columns: Source, → YouTube Music, Where, Conf, Action
        first = table.get_row_at(0)
        assert first[2] == "Chill"  # Where = the collection it came from
        assert first[4] == "add to playlist"  # an AUTO match into a playlist
        actions = [table.get_row_at(r)[4] for r in range(table.row_count)]
        assert "review" in actions  # the quarantined track is flagged


@pytest.mark.asyncio
async def test_sync_table_and_summary_report_present_and_skipped_tracks():
    from textual.widgets import DataTable, Static

    class PresentEngine(FakeEngine):
        def build_plan(self, on_event=lambda e: None, *, persist_matches=False):
            on_event(CollectionStarted("Chill", CollectionKind.PLAYLIST, 1))
            hit = MatchResult(
                self.match.source,
                MatchOutcome.AUTO,
                YtCandidate("alt", "Song", "Artist"),
                MatchMethod.PRESENT,
                1.0,
            )
            on_event(TrackMatched(hit, present=True))
            return SyncPlan(present=1, degraded=["Focus"])

    app = make_app()
    app._engine_factory = lambda: PresentEngine(app.repo)  # noqa: SLF001
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
        table = app.screen.query_one("#rows", DataTable)
        assert table.get_row_at(0)[4] == "already there"
        summary = str(app.screen.query_one("#summary", Static).render())
        assert "1 already there" in summary
        assert "1 skipped (target unreadable)" in summary


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
        await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
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
        source_side="spotify",
        source_uri="spotify:track:2",
        title="Weird",
        artist="Nobody",
        album=None,
        reason="low_conf",
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


@pytest.mark.asyncio
async def test_quarantine_classify_resolves_via_engine():
    app = make_app()
    app.config.classify.enabled = True  # opt in
    app.repo.add_quarantine(
        source_side="spotify",
        source_uri="spotify:track:3",
        title="Fuzzy",
        artist="Band",
        album=None,
        reason="low_conf",
        best_candidate={"video_id": "vc", "title": "Fuzzy", "confidence": 0.6},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await _wait(pilot, lambda: isinstance(app.screen, QuarantineScreen))
        await pilot.pause()
        await pilot.press("c")  # classify with AI (runs off the daemon thread)
        done = await _wait(pilot, lambda: app.repo.count_unresolved_quarantine() == 0)
        assert done
        row = app.repo.get_track_by_spotify_uri("spotify:track:3")
        assert row is not None and row.yt_video_id == "vc" and row.match_method == "claude"


@pytest.mark.asyncio
async def test_quarantine_classify_shows_progress():
    from textual.widgets import ProgressBar

    app = make_app()
    app.config.classify.enabled = True
    for i in range(3):
        app.repo.add_quarantine(
            source_side="spotify",
            source_uri=f"spotify:track:p{i}",
            title=f"Track {i}",
            artist="Band",
            album=None,
            reason="low_conf",
            best_candidate={"video_id": f"vp{i}", "title": f"Track {i}", "confidence": 0.6},
        )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await _wait(pilot, lambda: isinstance(app.screen, QuarantineScreen))
        await pilot.pause()
        await pilot.press("c")
        await _wait(pilot, lambda: app.repo.count_unresolved_quarantine() == 0)
        await pilot.pause()
        screen = app.screen
        bar = screen.query_one("#q_bar", ProgressBar)
        # Bar filled to total, the tally reflects every verdict, and we finished.
        assert bar.progress == 3
        summary = str(screen.query_one("#q_summary", Static).render())
        assert "3 resolved" in summary
        phase = str(screen.query_one("#q_phase", Static).render())
        assert "complete" in phase.lower()


@pytest.mark.asyncio
async def test_quarantine_classify_disabled_is_a_noop():
    app = make_app()  # classify disabled by default
    app.repo.add_quarantine(
        source_side="spotify",
        source_uri="spotify:track:4",
        title="Keep",
        artist="Me",
        album=None,
        reason="low_conf",
        best_candidate={"video_id": "vk"},
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await _wait(pilot, lambda: isinstance(app.screen, QuarantineScreen))
        await pilot.pause()
        await pilot.press("c")  # disabled → notifies, resolves nothing
        await pilot.pause()
        assert app.repo.count_unresolved_quarantine() == 1


# --------------------------------------------------------------------------- #
# Playlist picker
# --------------------------------------------------------------------------- #
async def _open_picker(pilot, app):
    """Press 'p' from the dashboard and wait for the picker to finish loading."""
    await pilot.press("p")
    loaded = await _wait(
        pilot, lambda: isinstance(app.screen, PlaylistsScreen) and app.screen._loaded
    )
    assert loaded
    return app.screen.query_one("#pl_list", SelectionList)


@pytest.mark.asyncio
async def test_dashboard_p_opens_and_loads_playlists():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        assert sel.option_count == 2


@pytest.mark.asyncio
async def test_prechecks_all_when_selection_empty():
    # Default Config(): selected_playlists == [] and sync_playlists True => "all".
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        assert set(sel.selected) == {"sp1", "sp2"}


@pytest.mark.asyncio
async def test_prechecks_subset_when_selection_nonempty():
    app = make_app()
    app.config.collections.selected_playlists = ["sp2"]
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        assert set(sel.selected) == {"sp2"}


@pytest.mark.asyncio
async def test_prechecks_none_when_playlists_disabled():
    # sync_playlists False with empty selection must NOT pre-check all.
    app = make_app()
    app.config.collections.sync_playlists = False
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        assert set(sel.selected) == set()


@pytest.mark.asyncio
async def test_save_subset_writes_explicit_list_and_enables():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        sel.deselect_all()
        sel.select("sp1")
        await pilot.pause()
        app.screen.query_one("#pl_save", Button).press()
        await pilot.pause()
        cols = app.config.collections
        assert cols.sync_playlists is True
        assert cols.selected_playlists == ["sp1"]
        # Persisted to TOML and round-trips through the custom writer.
        reloaded = Config.load(app.paths.config_file)
        assert reloaded.collections.selected_playlists == ["sp1"]
        assert reloaded.collections.sync_playlists is True


@pytest.mark.asyncio
async def test_save_none_checked_disables_playlists():
    # The [] == all trap: zero checked must flip sync_playlists False, not mean "all".
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_picker(pilot, app)
        app.screen.query_one("#pl_none", Button).press()
        await pilot.pause()
        app.screen.query_one("#pl_save", Button).press()
        await pilot.pause()
        cols = app.config.collections
        assert cols.sync_playlists is False
        assert cols.selected_playlists == []
        reloaded = Config.load(app.paths.config_file)
        assert reloaded.collections.sync_playlists is False


@pytest.mark.asyncio
async def test_select_all_button_checks_everything():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        sel = await _open_picker(pilot, app)
        app.screen.query_one("#pl_none", Button).press()
        await pilot.pause()
        assert set(sel.selected) == set()
        app.screen.query_one("#pl_all", Button).press()
        await pilot.pause()
        assert set(sel.selected) == {"sp1", "sp2"}


@pytest.mark.asyncio
async def test_settings_button_opens_picker():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")  # settings
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        app.screen.query_one("#choose_pl", Button).press()
        opened = await _wait(pilot, lambda: isinstance(app.screen, PlaylistsScreen))
        assert opened


@pytest.mark.asyncio
async def test_load_error_keeps_save_disabled():
    app = make_app()
    app._engine_factory = lambda: FakeEngine(app.repo, spotify=_RaisingSpotify())  # noqa: SLF001
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await _wait(pilot, lambda: isinstance(app.screen, PlaylistsScreen))
        failed = await _wait(
            pilot,
            lambda: (
                "Couldn't load" in str(app.screen.query_one("#playlists_status", Static).render())
            ),
        )
        assert failed
        assert app.screen._loaded is False
        assert app.screen.query_one("#pl_save", Button).disabled is True


def test_playlists_summary_states():
    from s2y.config import CollectionsConfig
    from s2y.tui.screens.settings import _playlists_summary

    assert "all" in _playlists_summary(
        CollectionsConfig(sync_playlists=True, selected_playlists=[])
    )
    assert "none" in _playlists_summary(
        CollectionsConfig(sync_playlists=False, selected_playlists=[])
    )
    assert "2 selected" in _playlists_summary(
        CollectionsConfig(sync_playlists=True, selected_playlists=["a", "b"])
    )


@pytest.mark.asyncio
async def test_settings_summary_refreshes_after_picker():
    # The summary in Settings must reflect a selection saved in the picker — i.e.
    # on_screen_resume re-renders #pl_summary when the picker is popped.
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")  # settings
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))

        def summary() -> str:
            return str(app.screen.query_one("#pl_summary", Static).render())

        assert "all" in summary()  # default: [] + enabled

        # Into the picker: uncheck everything, save, back out.
        app.screen.query_one("#choose_pl", Button).press()
        await _wait(pilot, lambda: isinstance(app.screen, PlaylistsScreen) and app.screen._loaded)
        app.screen.query_one("#pl_none", Button).press()
        await pilot.pause()
        app.screen.query_one("#pl_save", Button).press()
        await pilot.pause()
        app.screen.query_one("#pl_back", Button).press()

        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        refreshed = await _wait(pilot, lambda: "none" in summary())
        assert refreshed


@pytest.mark.asyncio
async def test_dashboard_cycles_direction_and_persists():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert app.screen._choice == "spotify_to_yt"
        await pilot.press("x")  # cycle
        await pilot.pause()
        assert app.screen._choice == "yt_to_spotify"
        assert app.config.sync.direction == "yt_to_spotify"  # persisted via save_config


@pytest.mark.asyncio
async def test_both_ways_dry_run_builds_two_direction_plan():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("x")  # -> yt_to_spotify
        await pilot.press("x")  # -> both
        await pilot.pause()
        assert app.screen._choice == "both"
        await pilot.press("d")  # dry run, both ways
        ok = await _wait(
            pilot, lambda: isinstance(app.screen, SyncScreen) and app.screen._plan is not None
        )
        assert ok
        assert len(app.screen._directions) == 2  # forward + reverse
        # The merged plan combines both directions (FakeEngine emits one op each).
        assert len(app.screen._plan.ops) == 2


@pytest.mark.asyncio
async def test_settings_saves_direction():
    from textual.widgets import Button, Select

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")  # settings
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        app.screen.query_one("#direction", Select).value = "yt_to_spotify"
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        assert app.config.sync.direction == "yt_to_spotify"


@pytest.mark.asyncio
async def test_dashboard_resyncs_direction_after_settings_change():
    from textual.widgets import Button, Select

    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen._choice == "spotify_to_yt"
        await pilot.press("g")  # settings
        await _wait(pilot, lambda: isinstance(app.screen, SettingsScreen))
        app.screen.query_one("#direction", Select).value = "yt_to_spotify"
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        app.screen.query_one("#back", Button).press()  # back to the dashboard
        await _wait(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.pause()
        # The dashboard re-syncs its per-run choice from config on resume, so 'd'/'s'
        # and the header reflect the just-saved direction instead of the stale one.
        assert app.screen._choice == "yt_to_spotify"


def test_make_engine_restores_default_direction_and_uncaches():
    from s2y.core.models import FORWARD, REVERSE

    app = make_app()
    eng = app.make_engine(REVERSE)
    assert eng is not None
    # make_engine must not leave the per-run direction (or engine) as the default,
    # or a later plain engine()/classify would silently inherit the last run's side.
    assert app._pending_direction == FORWARD  # restored to config default  # noqa: SLF001
    assert app._engine is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_setup_rerun_unlocks_finish_with_saved_creds(monkeypatch):
    from textual.widgets import Button

    from s2y.tui.screens import setup

    monkeypatch.setattr(setup.spotify_pkce, "has_cached_token", lambda *a: True)
    monkeypatch.setattr(setup.ytmusic_auth, "has_credentials", lambda *a: True)
    app = make_app(configured=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.query_one("#finish", Button).disabled is False
