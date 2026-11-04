# CLAUDE.md — s2y

A TUI to sync a music library **between Spotify and YouTube Music** (playlists,
Liked Songs, Liked Albums). Ships an idempotent, **adds-only sync in either
direction** (Spotify → YouTube Music and YouTube Music → Spotify, or "both ways")
on a persistent engine. Direction is picked per-run on the dashboard (`x` cycles)
or set as a default in config; deletes (true two-way) are a later phase.

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
    models.py      SongInfo (spotify_uri+yt_video_id), YtCandidate (opaque id), MatchResult,
                   SyncOp/SyncPlan, ClassifyItem/Verdict; Side + Direction(FORWARD/REVERSE)  (pure)
    ports.py       MusicSource / MusicTarget (both adapters implement both roles) / Classifier
                   Protocols. SpotifySource/YtMusicTarget kept as aliases.
    matching.py    normalization + scored cascade (ISRC-ready, duration-gated)  (pure; direction-agnostic)
    classify.py    LLM-judge batch prompt builder + robust JSON-array verdict parser  (pure; no I/O)
    resolve.py     resolve_quarantine_match(): the one shared accept path (TUI + judge), routes by source_side
    policy.py      ConflictPolicy (union=adds-only), CircuitBreaker  (pure)
    planner.py     build_plan(source, target, direction): reads + matches -> SyncPlan. NO writes.
    engine.py      Engine(spotify, ytmusic, direction=…): build_plan / apply_plan / classify_quarantine
    events.py      progress events; engine takes an EventSink callback  (pure)
  services/
    base.py        ServiceError, first_artist(); re-exports ports
    ratelimit.py   with_retry (backoff + 429 Retry-After), Pacer, chunked
    spotify.py     spotipy adapter: reads (source) + search/writes/present-reads (target); paginates via sp.next
    ytmusic.py     ytmusicapi adapter: writes/search/present-reads (target) + rich reads (source)
    claude_classify.py  ClaudeClassifier: shells out to `claude -p`; all failures -> UNCERTAIN
  auth/
    spotify_pkce.py  SpotifyPKCE (Auth Code + PKCE, no secret); we drive the flow
                     ourselves (get_authorize_url -> own loopback catcher on the local
                     port -> get_access_token) so a configurable HTTPS redirect (e.g.
                     Tailscale tunnel) works, not just loopback. CacheFileHandler in config dir
    ytmusic_auth.py  browser-headers (default) + OAuth; normalize_headers (Firefox & Chrome cURL)
  store/
    schema.sql     track_map (fwd cache), reverse_map (rev cache, keyed yt_video_id), playlist_map,
                   playlist_membership, snapshot, quarantine, tombstone, sync_run
    db.py          connect() — WAL, FKs on, applies schema every connect (new tables are additive); user_version=2
    repository.py  typed CRUD + transaction(); direction-aware helpers (get_cached_target, present_target_ids,
                   upsert_reverse, get_collection_by_yt_id); injectable clock for deterministic tests
  tui/
    app.py         S2YApp: routing, lazy engine(), make_engine(direction), app.repo for status (no auth)
    labels.py      direction/service display labels + the per-run choice list (UI-only)
    screens/       setup (wizard), dashboard (x cycles direction), sync (runs a directions list),
                   review (quarantine), settings (direction default), playlists (source-side picker)
    messages.py    Message bridge: worker -> UI via post_message
    s2y.tcss
```

## Key invariants / conventions

- **Direction:** a sync reads its **source** and writes its **target**;
  `core/models.Direction` (`FORWARD`/`REVERSE`) names the sides and the per-direction
  signature column. The engine binds `(spotify, ytmusic)` to source/target by direction;
  the planner/matcher are direction-agnostic. The reverse identity cache lives in its
  **own `reverse_map` table** (keyed `yt_video_id`) so a reverse match can never clobber a
  forward `track_map` row — reverse also links the pair into `track_map` (for membership)
  only when it won't overwrite an existing forward target. `YtCandidate.video_id` /
  `SyncOp.video_id` are opaque ids (a Spotify uri in reverse); the engine routes them by
  direction and never writes a foreign id into a service-specific column.
- **Idempotency:** a re-run is a no-op. Two mechanisms: (1) the identity cache
  (`track_map` forward, `reverse_map` reverse) records every source id → target id so we
  never re-match; (2) `playlist_map` stores a per-direction source **signature**
  (`spotify_snapshot_id` forward / `yt_content_fingerprint` reverse — `snapshot_id` for
  playlists, else a hash of the id set) for cheap change-detection. Reverse dedups target
  adds against persisted `present_spotify` membership ∪ a live read, and `add_playlist_items`
  filters internally (Spotify's add isn't natively idempotent). On partial failure we still
  persist the target playlist-id mapping but **withhold the signature** so the collection is
  reprocessed and failed adds retry. `invalidate_signatures()` clears both columns.
- **Content dedup:** before searching, the planner asks `matching.PresentIndex` whether the
  target collection already holds the same recording under another id (equal title after
  edition tags, both durations known and within tolerance, artist gate). A hit emits no op
  and is deliberately **not persisted**: the identity cache is global but this fact is per
  collection, so it is re-probed from the live read. An unmapped collection finds its target
  playlist by name first (apply would add into that playlist). If the live read fails, the
  collection is skipped for the run with its signature withheld, never planned blind.
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
- **union policy:** adds only — nothing is ever auto-deleted, in either direction.
  Running "both ways" converges the two libraries to their union; a round-trip
  (forward → reverse → forward) is provably damped (no echo / duplicate adds).
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
network. When adding a service method, add a fake-client parsing test. Forward and
reverse have parallel suites: `tests/test_engine.py` (Spotify→YT) and
`tests/test_engine_reverse.py` (YT→Spotify, incl. the no-clobber and round-trip checks).

## Phasing (see /Users/ianmurrays/.claude/plans/i-want-to-create-federated-lighthouse.md)

Phase 0+1 (done): one-way import on the durable substrate. **Phase 2 (done):
reverse read — adds-only sync in either direction (and "both ways"), parameterized
by `Direction`.** Phase 3: two-way deletes behind tombstones + circuit-breaker (the
`tombstone` table + `CircuitBreaker` already exist, unused) — also true incremental
diff. Phase 4: scheduling, keyring, richer conflict UI.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for `ianmurrays/s2y` (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root (created lazily). See `docs/agents/domain.md`.
