"""Wiring: build a ready :class:`Engine` from config + auth.

Shared by the CLI and the TUI so neither duplicates the adapter/auth plumbing.
"""

from __future__ import annotations

import time

from .auth import spotify_pkce, ytmusic_auth
from .config import Config, Paths
from .core.engine import Engine
from .services.ratelimit import SleepFn
from .services.spotify import SpotifyClient
from .services.ytmusic import YTMusicClient
from .store import connect
from .store.repository import Repository


def is_configured(config: Config, paths: Paths) -> bool:
    """True when both services have usable credentials (skip the wizard)."""
    return (
        bool(config.spotify.client_id)
        and spotify_pkce.has_cached_token(config, paths)
        and ytmusic_auth.has_credentials(config, paths)
    )


def open_repo(paths: Paths) -> Repository:
    """Open just the local store (no network/auth) — for dashboard status, etc."""
    paths.ensure_dirs()
    return Repository(connect(paths.db))


def build_engine(config: Config, paths: Paths, *, sleep: SleepFn = time.sleep) -> Engine:
    paths.ensure_dirs()
    spotify = SpotifyClient(spotify_pkce.build_client(config, paths), sleep=sleep)
    ytmusic = YTMusicClient(ytmusic_auth.build_client(config, paths), sleep=sleep)
    repo = Repository(connect(paths.db))
    return Engine(spotify, ytmusic, repo, config, sleep=sleep)
