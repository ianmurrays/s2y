# s2y

A TUI to sync your music library **between Spotify and YouTube Music** — playlists,
Liked Songs, and Liked Albums.

Built for migrating away from Spotify while keeping a sync running both ways. v1
ships a polished, accurate, *idempotent* one-way Spotify → YouTube Music import
on top of a persistent sync engine; reverse-read and full two-way sync land in
later phases.

It is inspired by [`linsomniac/spotify_to_ytmusic`](https://github.com/linsomniac/spotify_to_ytmusic)
but is a fresh implementation: a pure, testable sync engine with a persistent
SQLite cross-service identity map, confidence-scored track matching with a
human-review quarantine, dry-run, and a [Textual](https://textual.textualize.io/)
UI.

## Status

Early development. See the implementation plan for the architecture and phasing.

## Install (dev)

```sh
uv sync --extra dev
uv run s2y
```

## How it works (short version)

- **Spotify** auth via `spotipy` Authorization Code + PKCE (you supply a Client
  ID from the Spotify Developer Dashboard — no secret needed).
- **YouTube Music** auth via `ytmusicapi` browser headers (default) or OAuth.
- The engine fetches full state, diffs against a stored snapshot, matches tracks
  across services (ISRC → normalized title+artist+duration → fuzzy→quarantine),
  and applies adds idempotently. Nothing is ever auto-deleted in v1.

## Safety

- Conflict policy is **adds-only (`union`)**: a track added on either side is
  added to both; nothing is removed automatically.
- **Dry-run** prints the full planned operation set without touching either
  service.
- Secrets live under your OS config dir with `0600` perms and are gitignored.
