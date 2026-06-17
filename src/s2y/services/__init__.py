"""Concrete service adapters over spotipy and ytmusicapi.

These are the *only* modules that import the third-party API clients. They
implement the ports in ``s2y.core.ports`` so the engine never sees a raw API
object. Network resilience (retry/backoff/pacing) lives in ``ratelimit``.
"""
