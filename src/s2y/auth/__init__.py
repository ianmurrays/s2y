"""Authentication for both services.

Spotify uses ``spotipy.SpotifyPKCE`` (Authorization Code + PKCE, no client
secret). YouTube Music uses ``ytmusicapi`` with browser-header auth by default
(more reliable in 2024-2026) or OAuth opt-in. Token/credential files live under
the config dir with 0600 perms.
"""
