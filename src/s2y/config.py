"""Configuration and on-disk paths.

All user state lives under the OS config/data dirs (via ``platformdirs``), never
in the current working directory like the reference project did. Secrets are
written with ``0600`` perms and their directory with ``0700``.

Config is a small, fixed-shape TOML file. We read it with stdlib ``tomllib`` and
write it with a tiny purpose-built dumper (below) so we don't take a third-party
TOML-writer dependency for a handful of scalar fields.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "s2y"

_CONFIG_DIR = Path(platformdirs.user_config_dir(APP_NAME))
_DATA_DIR = Path(platformdirs.user_data_dir(APP_NAME))


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Paths:
    """Resolved on-disk locations. Override ``root_config``/``root_data`` in tests."""

    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def spotify_token(self) -> Path:
        return self.config_dir / "spotify-token.json"

    @property
    def ytmusic_browser(self) -> Path:
        return self.config_dir / "ytmusic-browser.json"

    @property
    def ytmusic_oauth(self) -> Path:
        return self.config_dir / "ytmusic-oauth.json"

    @property
    def db(self) -> Path:
        return self.data_dir / "s2y.db"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "s2y.log"

    def ensure_dirs(self) -> None:
        """Create config/data/log dirs with private (0700) permissions."""
        for d in (self.config_dir, self.data_dir, self.log_file.parent):
            d.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(d, 0o700)


def default_paths() -> Paths:
    return Paths(config_dir=_CONFIG_DIR, data_dir=_DATA_DIR)


def _chmod_quiet(path: Path, mode: int) -> None:
    """chmod that no-ops on platforms/filesystems that don't support it."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def secure_existing(path: Path) -> None:
    """Tighten perms on a file a third-party lib wrote (e.g. token caches)."""
    _chmod_quiet(path, 0o600)


def write_secret(path: Path, data: str) -> None:
    """Write a secret file with 0600 perms (created private from the start)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(path.parent, 0o700)
    # Open with restrictive mode so the file is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
    finally:
        _chmod_quiet(path, 0o600)


# --------------------------------------------------------------------------- #
# Config schema
# --------------------------------------------------------------------------- #
class AuthMode(str):
    BROWSER = "browser"
    OAUTH = "oauth"


@dataclass
class SpotifyConfig:
    client_id: str = ""
    redirect_port: int = 8888  # local port the callback catcher binds to
    # Explicit redirect URI to register with Spotify. Blank = the loopback default
    # below. Set this to an HTTPS URL (e.g. a Tailscale tunnel) when authorizing
    # from another device or when the dashboard won't accept a loopback URL.
    redirect_uri: str = ""

    @property
    def effective_redirect_uri(self) -> str:
        # Default uses the 127.0.0.1 literal (NOT "localhost", which Spotify now
        # rejects). Spotify still allows this loopback http URL; anything else
        # must be https.
        return self.redirect_uri or f"http://127.0.0.1:{self.redirect_port}/callback"


@dataclass
class YtMusicConfig:
    auth_mode: str = AuthMode.BROWSER  # "browser" | "oauth"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


@dataclass
class SyncConfig:
    conflict_policy: str = "union"  # v1 only supports "union" (adds-only)
    track_sleep: float = 0.1
    circuit_breaker_pct: int = 30
    dry_run: bool = False
    # Per-request network timeout (seconds) for the Spotify/YouTube Music clients,
    # so a stuck socket raises (and our retry layer surfaces it) instead of hanging.
    request_timeout: float = 15.0


@dataclass
class CollectionsConfig:
    sync_playlists: bool = True
    sync_liked_songs: bool = True
    sync_liked_albums: bool = True
    selected_playlists: list[str] = field(default_factory=list)  # [] => all


@dataclass
class ClassifyConfig:
    """Optional LLM-judge pass over the quarantine, via the ``claude`` CLI.

    Opt-in (``enabled`` defaults False). When on, the engine can ask
    ``claude -p`` to look at each quarantined track plus a fresh set of YouTube
    Music candidates and auto-resolve the ones it is confident about — the human
    then only reviews the remainder. Tracks are judged in batches (one model call
    per ``batch_size`` chunk). Everything here is plain scalars so the tiny TOML
    writer handles the section without changes.
    """

    enabled: bool = False  # master switch — the feature does nothing unless True
    claude_path: str = "claude"  # binary name (resolved via PATH) or absolute path
    model: str = "sonnet"  # --model alias ("sonnet"|"haiku"|"opus") or a full model id
    # Apply Claude's ACCEPT only at/above this confidence. Kept above the matcher's
    # AUTO_ACCEPT (0.82) so the judge clears a strictly higher bar than the scorer.
    auto_accept_confidence: float = 0.85
    timeout_s: float = 120.0  # per-call subprocess timeout (a batch is one call)
    max_candidates: int = 5  # how many candidates to show Claude per track (matcher's top-5)
    batch_size: int = 40  # tracks judged per claude -p call; huge quarantines split


@dataclass
class Config:
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)
    ytmusic: YtMusicConfig = field(default_factory=YtMusicConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    collections: CollectionsConfig = field(default_factory=CollectionsConfig)
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)

    # ----- (de)serialization ------------------------------------------------ #
    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        def section(klass, key):
            raw = data.get(key, {}) or {}
            allowed = {f for f in klass.__dataclass_fields__}
            return klass(**{k: v for k, v in raw.items() if k in allowed})

        return cls(
            spotify=section(SpotifyConfig, "spotify"),
            ytmusic=section(YtMusicConfig, "ytmusic"),
            sync=section(SyncConfig, "sync"),
            collections=section(CollectionsConfig, "collections"),
            classify=section(ClassifyConfig, "classify"),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_quiet(path.parent, 0o700)
        path.write_text(_dump_toml(self.to_dict()), encoding="utf-8")
        _chmod_quiet(path, 0o600)


# --------------------------------------------------------------------------- #
# Minimal TOML writer (sufficient for our fixed, flat-per-section schema)
# --------------------------------------------------------------------------- #
def _dump_toml(data: dict) -> str:
    """Serialize a dict of ``{section: {key: scalar|list[str]}}`` to TOML.

    Only the value types our config actually uses are supported: str, bool, int,
    float, and list[str]. Anything else raises, loudly, rather than emitting
    malformed TOML.
    """
    lines: list[str] = []
    for section, body in data.items():
        if not isinstance(body, dict):
            raise TypeError(f"config section {section!r} must be a table")
        lines.append(f"[{section}]")
        for key, value in body.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")  # blank line between tables
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value) -> str:
    if isinstance(value, bool):  # must precede int (bool is an int subclass)
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_string(str(v)) for v in value) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
