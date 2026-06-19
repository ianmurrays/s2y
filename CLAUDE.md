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
uv run s2y --classify      # headless: claude -p LLM judge over the quarantine ([classify] enabled)
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
    models.py      SongInfo, YtCandidate, MatchResult, SyncOp/SyncPlan, ClassifyItem/Verdict, …  (pure)
    ports.py       SpotifySource / YtMusicTarget / Classifier (classify_batch) Protocols (the engine's seams)
    matching.py    normalization + scored cascade (ISRC-ready, duration-gated)  (pure)
    classify.py    LLM-judge batch prompt builder + robust JSON-array verdict parser  (pure; no I/O)
    resolve.py     resolve_quarantine_match(): the one shared accept path (TUI + judge)
    policy.py      ConflictPolicy (union=adds-only in v1), CircuitBreaker  (pure)
    planner.py     build_plan(): reads + matches -> SyncPlan. NO writes.
    engine.py      Engine.build_plan / apply_plan / classify_quarantine (+ persistence)
    events.py      progress events; engine takes an EventSink callback  (pure)
  services/
    base.py        ServiceError, first_artist(); re-exports ports
    ratelimit.py   with_retry (backoff + 429 Retry-After), Pacer, chunked
    spotify.py     spotipy adapter (read-only in v1; paginates via sp.next)
    ytmusic.py     ytmusicapi adapter (videoId/setVideoId, rate_song/rate_playlist, search)
    claude_classify.py  ClaudeClassifier: shells out to `claude -p`; all failures -> UNCERTAIN
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
- **Classifier (opt-in):** `[classify] enabled` turns on an LLM judge that re-searches
  YT per quarantined track and asks `claude -p` to pick a match. Tracks are judged in
  **one batched `claude -p` call per `batch_size` chunk** (default 40), not one call
  per track — so a big backlog costs a handful of calls; the reply is a JSON array
  mapped back per-track by index. It auto-resolves only ACCEPTs at ≥
  `auto_accept_confidence` (0.85, above the matcher's bar) whose `video_id` is one
  actually shown *for that track* (the parser rejects hallucinated/misindexed ids);
  every failure → UNCERTAIN → stays quarantined. Both the TUI accept and the judge
  resolve through the one `core/resolve.py` helper (so neither forgets
  `invalidate_signatures`). Uses the user's existing `claude` login (no `--bare`, no
  API key needed).
- **union policy (v1):** adds only — nothing is ever auto-deleted.
- **Threading:** `spotipy`/`ytmusicapi` are blocking. The TUI runs every engine
  call on a **daemon** thread (`tui/threads.py:run_detached`) inside a `@work`
  worker — daemon so a wedged network call can never block interpreter exit. Quit
  (`App.action_quit`) sets `app.stop` (a `threading.Event`) and cancels workers;
  the engine/planner loops and the injected `interruptible_sleep` (via
  `build_engine(stop=...)`) watch it, so in-flight work winds down promptly and
  partial applies withhold their signature for retry. Engine events reach the UI
  only via `post_message` — never touch widgets from a worker thread. Log to file only.
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
