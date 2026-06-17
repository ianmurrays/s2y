# CLAUDE.md — s2y

A TUI to sync a music library **between Spotify and YouTube Music** (playlists,
Liked Songs, Liked Albums). v1 ships a polished, idempotent **one-way
Spotify → YouTube Music** import on top of a persistent sync engine; reverse-read
and full two-way sync are planned for later phases.

Inspired by `linsomniac/spotify_to_ytmusic` (+ its unmerged PKCE fix, PR #249) but
a fresh implementation: persistent SQLite cross-service identity map,
confidence-scored matching with a human-review quarantine, dry-run, Textual UI.

## Commands

```sh
uv sync --extra dev        # install (Python 3.12, pinned in .python-version)
uv run pytest -q           # run the test suite (fast, no network)
uv run s2y                 # launch the TUI
uv run s2y --plan          # headless dry-run: print the plan, no UI, no changes
uv build --wheel           # build (data files schema.sql + s2y.tcss are bundled)
```

## Architecture (hexagonal; dependencies point inward)

`tui → core + services + store`; `services → core`; **`core/models|matching|policy|events` are pure** (no I/O).
The engine talks to services only through Protocols (`core/ports.py`), so it is
fully testable with in-memory fakes.

```
src/s2y/
  config.py        platformdirs paths + TOML config (custom tiny writer); 0600 secrets
  session.py       wiring: build_engine() / open_repo() / is_configured()
  cli.py           argparse entry; --plan headless; logging to file ONLY (never stdout)
  core/
    models.py      SongInfo, YtCandidate, MatchResult, SyncOp/SyncPlan, RemotePlaylist/Album  (pure)
    ports.py       SpotifySource / YtMusicTarget Protocols (the engine's seam)
    matching.py    normalization + scored cascade (ISRC-ready, duration-gated)  (pure)
    policy.py      ConflictPolicy (union=adds-only in v1), CircuitBreaker  (pure)
    planner.py     build_plan(): reads + matches -> SyncPlan. NO writes.
    engine.py      Engine.build_plan / apply_plan (+ persistence, idempotency)
    events.py      progress events; engine takes an EventSink callback  (pure)
  services/
    base.py        ServiceError, first_artist(); re-exports ports
    ratelimit.py   with_retry (backoff + 429 Retry-After), Pacer, chunked
    spotify.py     spotipy adapter (read-only in v1; paginates via sp.next)
    ytmusic.py     ytmusicapi adapter (videoId/setVideoId, rate_song/rate_playlist, search)
  auth/
    spotify_pkce.py  SpotifyPKCE (Auth Code + PKCE, no secret); we drive the flow
                     ourselves (get_authorize_url -> own loopback catcher on the local
                     port -> get_access_token) so a configurable HTTPS redirect (e.g.
                     Tailscale tunnel) works, not just loopback. CacheFileHandler in config dir
    ytmusic_auth.py  browser-headers (default) + OAuth; normalize_headers (Firefox & Chrome cURL)
  store/
    schema.sql     track_map, playlist_map, playlist_membership, snapshot, quarantine, tombstone, sync_run
    db.py          connect() — WAL, FKs on, check_same_thread=False (used across worker threads)
    repository.py  typed CRUD + transaction(); injectable clock for deterministic tests
  tui/
    app.py         S2YApp: routing, lazy engine(), app.repo for status (no auth)
    screens/       setup (wizard), dashboard, sync, review (quarantine), settings
    messages.py    Message bridge: worker -> UI via post_message
    s2y.tcss
```

## Key invariants / conventions

- **Idempotency:** a re-run is a no-op. Two mechanisms: (1) `track_map` caches
  every Spotify-URI → `videoId` so we never re-match; (2) `playlist_map` stores a
  source **signature** (Spotify `snapshot_id` for playlists; a hash of the id set
  for Liked Songs/Albums) for cheap change-detection. On partial failure we still
  persist the `yt_playlist_id` (so the next run dedupes against live YT) but
  **withhold the signature** so the collection is reprocessed and failed adds retry.
- **Matching:** auto-accept only ≥ `AUTO_ACCEPT` (0.82); everything else →
  **quarantine** (never guess). Duration-gated; normalization keeps meaningful
  qualifiers (Live/Remix) but strips edition tags (Remastered/feat./accents).
- **union policy (v1):** adds only — nothing is ever auto-deleted.
- **Threading:** `spotipy`/`ytmusicapi` are blocking. The TUI runs every engine
  call via `asyncio.to_thread` inside a `@work` worker; `self.app.engine()` is
  resolved on the UI thread first. Engine events reach the UI only via
  `post_message` — never touch widgets from a worker thread. Log to file only.
- **Secrets** live under the config dir (0600), gitignored; never in the DB.

## Testing

Pure layers tested directly; engine/planner via in-memory fakes + `:memory:`
store; TUI via Textual `App.run_test()` pilot with a fake engine. No test hits the
network. When adding a service method, add a fake-client parsing test.

## Phasing (see /Users/ianmurrays/.claude/plans/i-want-to-create-federated-lighthouse.md)

v1 = Phase 0+1 (done): one-way import on the durable substrate. Phase 2: reverse
read + true incremental diff (both ways, no deletes). Phase 3: two-way deletes
behind tombstones + circuit-breaker (the `tombstone` table + `CircuitBreaker`
already exist, unused). Phase 4: scheduling, keyring, richer conflict UI.
