"""Wiring: build a ready :class:`Engine` from config + auth.

Shared by the CLI and the TUI so neither duplicates the adapter/auth plumbing.
"""

from __future__ import annotations

import threading
import time

from .auth import spotify_pkce, ytmusic_auth
from .config import Config, Paths
from .core.engine import Engine
from .core.ports import Classifier
from .services.ratelimit import SleepFn, interruptible_sleep
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


def build_classifier(config: Config) -> Classifier | None:
    """Build the LLM quarantine judge, or ``None`` when it's disabled in config.

    Constructing it is cheap and side-effect-free — it only shells out to
    ``claude`` when ``classify_quarantine`` is actually run — so it's safe to wire
    into every engine when ``[classify] enabled`` is set.
    """
    if not config.classify.enabled:
        return None
    from .services.claude_classify import ClaudeClassifier

    return ClaudeClassifier(
        claude_path=config.classify.claude_path,
        model=config.classify.model,
        timeout_s=config.classify.timeout_s,
    )


def build_engine(
    config: Config,
    paths: Paths,
    *,
    sleep: SleepFn | None = None,
    stop: threading.Event | None = None,
    classifier: Classifier | None = None,
) -> Engine:
    """Build a ready engine.

    When a ``stop`` event is given (the TUI passes the app's), backoff/pacing
    sleeps become interruptible and the engine/planner loops bail out the moment
    it is set — so quitting mid-sync doesn't leave a thread grinding to completion.
    An explicit ``sleep`` (tests) overrides this. The quarantine classifier is
    auto-wired from config (when enabled) unless one is passed explicitly.
    """
    paths.ensure_dirs()
    if sleep is None:
        sleep = interruptible_sleep(stop) if stop is not None else time.sleep
    if classifier is None:
        classifier = build_classifier(config)
    spotify = SpotifyClient(spotify_pkce.build_client(config, paths), sleep=sleep)
    ytmusic = YTMusicClient(ytmusic_auth.build_client(config, paths), sleep=sleep)
    repo = Repository(connect(paths.db))
    return Engine(
        spotify, ytmusic, repo, config, sleep=sleep, stop=stop, classifier=classifier
    )
