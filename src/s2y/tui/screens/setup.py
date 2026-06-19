"""First-run wizard: authorize Spotify, connect YouTube Music, pick collections.

Each network step runs in a worker (off the UI thread) and reports back via an
``AuthResult`` message. The two services are verified independently; ``Finish``
unlocks once both succeed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from ...auth import spotify_pkce, ytmusic_auth
from ...services.spotify import SpotifyClient
from ...services.ytmusic import YTMusicClient
from ..messages import AuthResult, AuthUrl
from ..threads import run_detached

if TYPE_CHECKING:
    from ..app import S2YApp

_YT_HELP = (
    "YouTube Music: open music.youtube.com (logged in) → DevTools → Network → "
    "filter '/browse' → click a POST → copy the request headers (Firefox) or "
    "'Copy as cURL' (Chrome) → paste below."
)
_SP_HELP = (
    "Spotify: create an app at developer.spotify.com → copy its Client ID → add the "
    "Redirect URI below to the app's allowlist. Spotify needs HTTPS unless you use "
    "the loopback http://127.0.0.1. To authorize from another device, use a tunnel: "
    "set the Redirect URI to your HTTPS URL (e.g. https://host.tailnet.ts.net/callback), "
    "set the Local callback port it forwards to, and run `tailscale serve --bg <port>`."
)


class SetupScreen(Screen):
    if TYPE_CHECKING:
        app: S2YApp  # type: ignore[assignment]  # narrow Textual's App to ours

    def __init__(self) -> None:
        super().__init__()
        self._sp_ok = False
        self._yt_ok = False
        self.auth_url: str | None = None  # last Spotify authorize URL shown

    def compose(self) -> ComposeResult:
        cfg = self.app.config
        yield Header()
        with VerticalScroll(id="setup"):
            yield Static("[b]Welcome to s2y[/b] — let's connect your accounts.", id="intro")

            with Vertical(classes="card"):
                yield Label("[b]1. Spotify[/b]")
                yield Static(_SP_HELP, classes="help")
                yield Input(
                    placeholder="Spotify Client ID", id="sp_client", value=cfg.spotify.client_id
                )
                yield Input(
                    placeholder="Redirect URI",
                    id="sp_redirect",
                    value=cfg.spotify.effective_redirect_uri,
                )
                yield Input(
                    placeholder="Local callback port (default 8888)",
                    id="sp_port",
                    value=str(cfg.spotify.redirect_port),
                )
                yield Button("Authorize Spotify", id="sp_auth", variant="primary")
                yield Static("", id="sp_status", classes="status")
                # The authorization URL lands here as a selectable field (focusing
                # selects it all) so it can be copied cleanly, without wrap line-breaks.
                yield Input(
                    placeholder="Authorization URL appears here — select & copy", id="sp_auth_url"
                )
                yield Button("Copy URL", id="sp_copy_url")

            with Vertical(classes="card"):
                yield Label("[b]2. YouTube Music[/b]")
                yield Static(_YT_HELP, classes="help")
                mode = (
                    cfg.ytmusic.auth_mode
                    if cfg.ytmusic.auth_mode in ("browser", "oauth")
                    else "browser"
                )
                yield Select(
                    [("Browser headers (recommended)", "browser"), ("OAuth", "oauth")],
                    value=mode,
                    id="yt_mode",
                    allow_blank=False,
                )
                yield TextArea(id="yt_headers")
                yield Input(
                    placeholder="OAuth Client ID (oauth mode)",
                    id="yt_cid",
                    value=cfg.ytmusic.oauth_client_id,
                )
                yield Input(
                    placeholder="OAuth Client Secret (oauth mode)",
                    id="yt_secret",
                    password=True,
                    value=cfg.ytmusic.oauth_client_secret,
                )
                yield Button("Connect YouTube Music", id="yt_auth", variant="primary")
                yield Static("", id="yt_status", classes="status")

            with Vertical(classes="card"):
                yield Label("[b]3. What to sync[/b]")
                yield Checkbox("Playlists", value=cfg.collections.sync_playlists, id="c_pl")
                yield Checkbox("Liked Songs", value=cfg.collections.sync_liked_songs, id="c_ls")
                yield Checkbox("Liked Albums", value=cfg.collections.sync_liked_albums, id="c_la")

            with Horizontal(id="setup_controls"):
                yield Button("Finish", id="finish", variant="success", disabled=True)
        yield Footer()

    # ----- Spotify ---------------------------------------------------------- #
    @on(Button.Pressed, "#sp_auth")
    def _sp_auth(self) -> None:
        self.query_one("#sp_status", Static).update("Starting authorization…")
        self.authorize_spotify()

    @work
    async def authorize_spotify(self) -> None:
        cid = self.query_one("#sp_client", Input).value.strip()
        redirect = self.query_one("#sp_redirect", Input).value.strip()
        port = self.query_one("#sp_port", Input).value.strip()
        if not cid:
            self.post_message(AuthResult("spotify", False, "Client ID is required"))
            return
        self.app.config.spotify.client_id = cid
        self.app.config.spotify.redirect_uri = redirect
        if port.isdigit():
            self.app.config.spotify.redirect_port = int(port)
        self.app.save_config()
        try:
            name = await run_detached(self._do_spotify_auth)
        except Exception as exc:  # noqa: BLE001
            self.post_message(AuthResult("spotify", False, str(exc)))
            return
        self.post_message(AuthResult("spotify", True, name))

    def _do_spotify_auth(self) -> str:
        client = spotify_pkce.authorize(
            self.app.config,
            self.app.paths,
            open_browser=False,  # user copies the URL from the screen themselves
            on_url=lambda url: self.post_message(AuthUrl(url)),
        )
        return SpotifyClient(client).verify()

    def on_auth_url(self, message: AuthUrl) -> None:
        self.auth_url = message.url
        field = self.query_one("#sp_auth_url", Input)
        field.value = message.url
        field.focus()  # select_on_focus selects the whole URL, ready to copy
        self.query_one("#sp_status", Static).update(
            "Copy the URL from the field below (or press Copy URL), open it in a "
            "browser, authorize, then come back here."
        )

    @on(Button.Pressed, "#sp_copy_url")
    def _copy_url(self) -> None:
        if not self.auth_url:
            return
        self.app.copy_to_clipboard(self.auth_url)
        self.notify("Authorization URL copied to clipboard.")

    # ----- YouTube Music ---------------------------------------------------- #
    @on(Button.Pressed, "#yt_auth")
    def _yt_auth(self) -> None:
        self.query_one("#yt_status", Static).update("Connecting…")
        self.connect_ytmusic()

    @work
    async def connect_ytmusic(self) -> None:
        # allow_blank=False on the Select guarantees a real value ("browser"/"oauth").
        mode = str(self.query_one("#yt_mode", Select).value)
        self.app.config.ytmusic.auth_mode = mode
        try:
            if mode == "browser":
                raw = self.query_one("#yt_headers", TextArea).text.strip()
                if not raw:
                    raise ValueError("Paste your YouTube Music request headers first")
                await run_detached(
                    lambda: ytmusic_auth.setup_browser(self.app.config, self.app.paths, raw)
                )
            else:
                self.app.config.ytmusic.oauth_client_id = self.query_one(
                    "#yt_cid", Input
                ).value.strip()
                self.app.config.ytmusic.oauth_client_secret = self.query_one(
                    "#yt_secret", Input
                ).value.strip()
                await run_detached(
                    lambda: ytmusic_auth.setup_oauth(self.app.config, self.app.paths)
                )
            self.app.save_config()
            name = await run_detached(self._verify_ytmusic)
        except Exception as exc:  # noqa: BLE001
            self.post_message(AuthResult("ytmusic", False, str(exc)))
            return
        self.post_message(AuthResult("ytmusic", True, name))

    def _verify_ytmusic(self) -> str:
        return YTMusicClient(ytmusic_auth.build_client(self.app.config, self.app.paths)).verify()

    # ----- results ---------------------------------------------------------- #
    def on_auth_result(self, message: AuthResult) -> None:
        if message.service == "spotify":
            self._sp_ok = message.ok
            self.query_one("#sp_status", Static).update(
                f"[green]Connected as {message.detail}[/]"
                if message.ok
                else f"[red]{message.detail}[/]"
            )
        else:
            self._yt_ok = message.ok
            self.query_one("#yt_status", Static).update(
                f"[green]Connected to {message.detail}[/]"
                if message.ok
                else f"[red]{message.detail}[/]"
            )
        self.query_one("#finish", Button).disabled = not (self._sp_ok and self._yt_ok)

    # ----- finish ----------------------------------------------------------- #
    @on(Button.Pressed, "#finish")
    def _finish(self) -> None:
        cols = self.app.config.collections
        cols.sync_playlists = self.query_one("#c_pl", Checkbox).value
        cols.sync_liked_songs = self.query_one("#c_ls", Checkbox).value
        cols.sync_liked_albums = self.query_one("#c_la", Checkbox).value
        self.app.save_config()
        self.app.go_dashboard()
