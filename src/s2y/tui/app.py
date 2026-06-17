"""The s2y Textual application: routing, config, and lazy engine construction."""

from __future__ import annotations

import logging
from collections.abc import Callable

from textual.app import App

from ..config import Config, Paths, default_paths
from ..core.engine import Engine
from ..session import build_engine, is_configured, open_repo
from ..store.repository import Repository

log = logging.getLogger(__name__)

EngineFactory = Callable[[], Engine]


class S2YApp(App):
    CSS_PATH = "s2y.tcss"
    TITLE = "s2y"
    SUB_TITLE = "Spotify → YouTube Music sync"
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
        self._engine_factory = engine_factory or (lambda: build_engine(self.config, self.paths))
        self._engine: Engine | None = None
        self._configured_override = configured

    # ----- engine lifecycle ------------------------------------------------- #
    def engine(self) -> Engine:
        """Build the engine on first use (this is where auth/network happens)."""
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine

    def reset_engine(self) -> None:
        self._engine = None

    def is_set_up(self) -> bool:
        if self._configured_override is not None:
            return self._configured_override
        return is_configured(self.config, self.paths)

    def save_config(self) -> None:
        self.config.save(self.paths.config_file)
        self.reset_engine()  # config changed; rebuild clients next time

    # ----- routing ---------------------------------------------------------- #
    def on_mount(self) -> None:
        # Imported here to avoid a circular import at module load.
        from .screens.dashboard import DashboardScreen
        from .screens.setup import SetupScreen

        self.push_screen(DashboardScreen() if self.is_set_up() else SetupScreen())

    def go_dashboard(self) -> None:
        from .screens.dashboard import DashboardScreen

        self.switch_screen(DashboardScreen())
