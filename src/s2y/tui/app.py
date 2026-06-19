"""The s2y Textual application: routing, config, and lazy engine construction."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from textual.app import App

from ..config import Config, Paths, default_paths
from ..core.engine import Engine
from ..core.models import Direction
from ..session import build_engine, direction_from_config, is_configured, open_repo
from ..store.repository import Repository

log = logging.getLogger(__name__)

EngineFactory = Callable[[], Engine]


class S2YApp(App):
    CSS_PATH = "s2y.tcss"
    TITLE = "s2y"
    SUB_TITLE = "Spotify ⇄ YouTube Music sync"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(
        self,
        *,
        config: Config | None = None,
        paths: Paths | None = None,
        engine_factory: EngineFactory | None = None,
        configured: bool | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths or default_paths()
        self.paths.ensure_dirs()
        self.config = config if config is not None else Config.load(self.paths.config_file)
        self.repo: Repository = open_repo(self.paths)
        # Cooperative-cancellation signal: set on quit / leaving the sync screen so
        # in-flight engine work (running on a daemon thread) winds down promptly.
        self.stop = threading.Event()
        # Which direction the next-built engine syncs. Picked per-run from the
        # dashboard; defaults to the configured direction.
        self._pending_direction: Direction = direction_from_config(self.config)
        self._engine_factory = engine_factory or (
            lambda: build_engine(
                self.config, self.paths, direction=self._pending_direction, stop=self.stop
            )
        )
        self._engine: Engine | None = None
        self._configured_override = configured

    # ----- engine lifecycle ------------------------------------------------- #
    def engine(self) -> Engine:
        """Build the engine on first use (this is where auth/network happens)."""
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine

    def make_engine(self, direction: Direction) -> Engine:
        """Build a fresh engine bound to ``direction`` (used by the sync/review screens).

        Rebuilds rather than reusing the cached one so a per-run direction choice (or
        a "both ways" run that needs two engines) always gets the right roles. Does NOT
        leave the per-run engine cached as the default, nor leave ``_pending_direction``
        flipped — so a later plain ``engine()`` (e.g. status) reflects config, not the
        last run.
        """
        self._pending_direction = direction
        self.reset_engine()
        engine = self.engine()
        self._pending_direction = direction_from_config(self.config)
        self._engine = None
        return engine

    def reset_engine(self) -> None:
        self._engine = None

    def is_set_up(self) -> bool:
        if self._configured_override is not None:
            return self._configured_override
        return is_configured(self.config, self.paths)

    def save_config(self) -> None:
        self.config.save(self.paths.config_file)
        # Keep the default-built engine in sync with a changed default direction.
        self._pending_direction = direction_from_config(self.config)
        self.reset_engine()  # config changed; rebuild clients next time

    # ----- shutdown --------------------------------------------------------- #
    async def action_quit(self) -> None:
        # Tell in-flight engine work to wind down, then cancel the awaiting
        # workers, so quit doesn't wait on a blocking spotipy/ytmusicapi call.
        self.stop.set()
        self.workers.cancel_all()
        self.exit()

    # ----- routing ---------------------------------------------------------- #
    def on_mount(self) -> None:
        # Imported here to avoid a circular import at module load.
        from .screens.dashboard import DashboardScreen
        from .screens.setup import SetupScreen

        self.push_screen(DashboardScreen() if self.is_set_up() else SetupScreen())

    def go_dashboard(self) -> None:
        from .screens.dashboard import DashboardScreen

        self.switch_screen(DashboardScreen())
