"""Auth tests — the pure header normalizer and Spotify scope/redirect wiring."""

from __future__ import annotations

from s2y.auth import spotify_pkce
from s2y.auth.ytmusic_auth import normalize_headers
from s2y.config import Config

FIREFOX_RAW = """Host: music.youtube.com
Cookie: VISITOR_INFO1=abc; SID=xyz
Authorization: SAPISIDHASH 1234_hash
X-Goog-AuthUser: 0"""

CHROME_CURL = r"""curl 'https://music.youtube.com/youtubei/v1/browse' \
  -H 'authority: music.youtube.com' \
  -H 'cookie: VISITOR_INFO1=abc; SID=xyz' \
  -H 'authorization: SAPISIDHASH 1234_hash' \
  -b 'extra=cookie' \
  --compressed"""


def test_firefox_headers_pass_through():
    out = normalize_headers(FIREFOX_RAW)
    assert out == FIREFOX_RAW.strip()
    assert "Cookie: VISITOR_INFO1=abc" in out


def test_chrome_curl_is_parsed():
    out = normalize_headers(CHROME_CURL)
    lines = out.splitlines()
    assert "authority: music.youtube.com" in lines
    assert "cookie: VISITOR_INFO1=abc; SID=xyz" in lines
    assert "authorization: SAPISIDHASH 1234_hash" in lines


def test_chrome_curl_without_explicit_cookie_header_uses_b_flag():
    curl = r"""curl 'https://x' -H 'authorization: AB' -b 'SID=only_cookie' --compressed"""
    out = normalize_headers(curl)
    assert "Cookie: SID=only_cookie" in out
    assert "authorization: AB" in out


def test_spotify_scopes_cover_read_and_write():
    scopes = set(spotify_pkce.SCOPES)
    assert {"playlist-modify-private", "user-library-modify",
            "user-library-read", "playlist-read-private"} <= scopes


def test_effective_redirect_defaults_to_loopback_literal():
    cfg = Config()
    cfg.spotify.redirect_port = 9090
    # 127.0.0.1 literal, not "localhost" (which Spotify rejects).
    assert cfg.spotify.effective_redirect_uri == "http://127.0.0.1:9090/callback"


def test_effective_redirect_honors_explicit_override():
    cfg = Config()
    cfg.spotify.redirect_uri = "https://host.tailnet.ts.net/callback"
    assert cfg.spotify.effective_redirect_uri == "https://host.tailnet.ts.net/callback"


def test_redirect_uri_round_trips_through_config(tmp_path):
    cfg = Config()
    cfg.spotify.redirect_uri = "https://host.tailnet.ts.net/callback"
    cfg.spotify.redirect_port = 8899
    path = tmp_path / "config.toml"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.spotify.redirect_uri == "https://host.tailnet.ts.net/callback"
    assert loaded.spotify.redirect_port == 8899


def test_build_auth_manager_uses_effective_uri(tmp_path):
    from s2y.config import Paths

    cfg = Config()
    cfg.spotify.client_id = "abc"
    cfg.spotify.redirect_uri = "https://host.tailnet.ts.net/callback"
    paths = Paths(config_dir=tmp_path / "c", data_dir=tmp_path / "d")
    auth = spotify_pkce.build_auth_manager(cfg, paths)
    assert auth.redirect_uri == "https://host.tailnet.ts.net/callback"


def test_spotify_build_client_sets_timeout_and_disables_lib_retries(tmp_path):
    from s2y.config import Paths

    cfg = Config()
    cfg.spotify.client_id = "abc"
    cfg.sync.request_timeout = 12.0
    paths = Paths(config_dir=tmp_path / "c", data_dir=tmp_path / "d")
    client = spotify_pkce.build_client(cfg, paths)
    # A finite timeout, and retries handed entirely to our with_retry layer.
    assert client.requests_timeout == 12.0
    assert client.retries == 0
    assert client.status_retries == 0


def test_ytmusic_build_client_passes_timeout_session(tmp_path, monkeypatch):
    from s2y.auth import ytmusic_auth
    from s2y.config import Paths
    from s2y.services.base import TimeoutSession

    captured: dict = {}

    def fake_ytmusic(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    # ytmusic_auth imports YTMusic into its own namespace, so patch it there.
    monkeypatch.setattr(ytmusic_auth, "YTMusic", fake_ytmusic)
    cfg = Config()
    cfg.sync.request_timeout = 8.0
    paths = Paths(config_dir=tmp_path / "c", data_dir=tmp_path / "d")
    ytmusic_auth.build_client(cfg, paths)
    session = captured["kwargs"]["requests_session"]
    assert isinstance(session, TimeoutSession)
    assert session.timeout == 8.0


def test_request_timeout_round_trips_through_config(tmp_path):
    cfg = Config()
    cfg.sync.request_timeout = 22.5
    path = tmp_path / "config.toml"
    cfg.save(path)
    assert Config.load(path).sync.request_timeout == 22.5


def test_classify_config_round_trips(tmp_path):
    cfg = Config()
    cfg.classify.enabled = True
    cfg.classify.model = "haiku"
    cfg.classify.claude_path = "/usr/local/bin/claude"
    cfg.classify.auto_accept_confidence = 0.91
    cfg.classify.timeout_s = 30.0
    cfg.classify.max_candidates = 4
    cfg.classify.batch_size = 12
    path = tmp_path / "config.toml"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.classify.enabled is True
    assert loaded.classify.model == "haiku"
    assert loaded.classify.claude_path == "/usr/local/bin/claude"
    assert loaded.classify.auto_accept_confidence == 0.91
    assert loaded.classify.timeout_s == 30.0
    assert loaded.classify.max_candidates == 4
    assert loaded.classify.batch_size == 12


def test_classify_config_defaults_when_section_missing():
    cfg = Config.from_dict({})
    assert cfg.classify.enabled is False  # opt-in
    assert cfg.classify.model == "sonnet"
    assert cfg.classify.batch_size == 40
    assert cfg.classify.timeout_s == 120.0  # a batch is one (longer) call


def test_capture_code_returns_code_from_redirect():
    import http.client
    import socket
    from concurrent.futures import ThreadPoolExecutor

    from s2y.auth.spotify_pkce import _capture_code

    # Find a free port, then hand it to the catcher.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_capture_code, port, timeout=10.0)
        # Retry until the catcher's server is bound, then send the redirect.
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/callback?code=THE_CODE&state=x")
                conn.getresponse().read()
                conn.close()
                break
            except OSError:
                import time

                time.sleep(0.05)
        assert future.result(timeout=10) == "THE_CODE"


def test_capture_code_raises_on_oauth_error():
    import http.client
    import socket
    import time
    from concurrent.futures import ThreadPoolExecutor

    import pytest

    from s2y.auth.spotify_pkce import _capture_code

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_capture_code, port, timeout=10.0)
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/callback?error=access_denied")
                conn.getresponse().read()
                conn.close()
                break
            except OSError:
                time.sleep(0.05)
        with pytest.raises(RuntimeError, match="access_denied"):
            future.result(timeout=10)


def test_bind_error_messages_are_actionable():
    import errno as _errno

    from s2y.auth.spotify_pkce import _bind_error

    eacces = _bind_error(443, OSError(_errno.EACCES, "Permission denied"))
    assert "Permission denied" in str(eacces) and "443" in str(eacces)

    inuse = _bind_error(8888, OSError(_errno.EADDRINUSE, "in use"))
    assert "already in use" in str(inuse)

    other = _bind_error(8888, OSError(_errno.EINVAL, "bad"))
    assert "Could not open" in str(other)


def test_capture_code_times_out_cleanly():
    import socket

    import pytest

    from s2y.auth.spotify_pkce import _capture_code

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    with pytest.raises(RuntimeError, match="Timed out"):
        _capture_code(port, timeout=0.5)
