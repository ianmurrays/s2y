# s2y

[![CI](https://github.com/ianmurrays/s2y/actions/workflows/ci.yml/badge.svg)](https://github.com/ianmurrays/s2y/actions/workflows/ci.yml)

A terminal UI (TUI) for syncing your music library **between Spotify and YouTube
Music** — playlists, Liked Songs, and Liked Albums.

Built for migrating away from Spotify while keeping a sync running. **v1 ships a
polished, accurate, _idempotent_ one-way Spotify → YouTube Music import** on top
of a persistent sync engine; reverse-read and full two-way sync are planned for
later phases.

It is a fresh implementation: a pure, testable sync engine with a persistent
SQLite cross-service identity map, confidence-scored track matching with a
human-review quarantine, dry-run, an optional LLM judge, and a
[Textual](https://textual.textualize.io/) UI.

> **Status:** early development (`0.1.0`). The Spotify side is read-only in v1;
> nothing is ever deleted from either service.

## Inspiration & attribution

s2y is inspired by [`linsomniac/spotify_to_ytmusic`](https://github.com/linsomniac/spotify_to_ytmusic)
and its unmerged **Auth-Code + PKCE** fix
([PR #249](https://github.com/linsomniac/spotify_to_ytmusic/pull/249)). It is **not
a fork** — it's a ground-up rewrite around a durable, idempotent sync substrate.
What it borrows from the reference project: the retry/backoff/pacing approach for
flaky service calls, and the PKCE idea (so Spotify auth needs only a public Client
ID, no client secret). The YouTube Music browser-header parser accepts both
Firefox "Copy Request Headers" and Chrome "Copy as cURL", an idea from the same PR.

## What it does

- **Syncs three kinds of collection:** playlists, Liked Songs, and Liked Albums.
- **Confidence-scored matching** with a human-review **quarantine** — it never
  guesses. High-confidence matches auto-apply; anything ambiguous waits for you.
- **Idempotent:** a re-run is a no-op. Every match is cached, and each collection
  carries a change signature, so unchanged collections are skipped and an
  interrupted run resumes cleanly instead of starting over.
- **Dry-run / `--plan`:** see the full planned operation set without touching
  either service.
- **Optional LLM judge:** an opt-in `claude -p` pass re-searches YouTube Music for
  each quarantined track and auto-resolves only the matches it's confident about.
- **Adds-only (`union`) policy:** a track present on the source is added to the
  destination; **nothing is ever removed automatically.**

## Install

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev   # install (dev extra adds the test + lint tooling)
uv run s2y            # launch the TUI
```

## First-run setup

Launching `s2y` for the first time opens a setup wizard with three steps:

1. **Authorize Spotify (Auth Code + PKCE).**
   Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
   and copy its **Client ID** — no client secret is needed. Add the redirect URI
   to the app's allowlist. The wizard shows an authorization URL; open it, approve,
   and the token is cached and refreshed silently from then on.
   - Default redirect is the loopback `http://127.0.0.1:8888/callback`.
   - To authorize from another device, set the redirect to an HTTPS URL behind a
     tunnel (e.g. `tailscale serve`) and set the local port it forwards to; s2y
     runs its own loopback catcher so this works, not just plain loopback.

2. **Connect YouTube Music.** Two modes:
   - **Browser headers (default):** in `music.youtube.com` (logged in) → DevTools →
     Network → filter `/browse` → click a POST → copy the request headers (Firefox)
     or "Copy as cURL" (Chrome) → paste. s2y normalizes either format.
   - **OAuth (opt-in):** use Google Cloud "TVs and Limited Input devices"
     credentials (Client ID + Secret); supports the device-code flow.

3. **Pick collections:** Playlists, Liked Songs, Liked Albums. The playlist picker
   (also reachable later from the dashboard) chooses exactly which playlists sync.

From the dashboard you can run a dry-run, run a sync, open the playlist picker,
review the quarantine, or open settings. It's keyboard-first — the footer shows the
keys.

## Usage

```sh
s2y                    # launch the TUI (setup wizard on first run, else dashboard)
s2y --plan             # headless dry-run: print the sync plan and exit (no changes)
s2y --classify         # headless: run the LLM judge over the quarantine, auto-resolve
s2y --classify-dry-run # like --classify but print verdicts only; resolve nothing
s2y -v / --verbose     # DEBUG logging to the log file
s2y --version
```

`--classify` / `--classify-dry-run` require `[classify] enabled = true` in your
config (see [Configuration](#configuration)). All headless modes require that
setup has already been completed in the TUI.

## How it works

The engine fetches the full source state, matches each track to a YouTube Music
`videoId`, builds a plan, and applies adds idempotently.

**Matching** is a scored cascade, duration-gated:

- **ISRC-ready** exact identity, then **normalized title + artist** similarity.
  Normalization casefolds, folds accents, and strips edition tags (Remastered,
  Deluxe, `feat.` …) but **keeps meaningful qualifiers** (Live, Remix, Acoustic) so
  a remix never matches the original at high confidence.
- A **duration gate** scales the score down as the candidate's length drifts from
  the source — the key accuracy improvement over the reference project.
- **Auto-accept only at ≥ 0.82.** Everything below that goes to the **quarantine**
  (with the best candidate shown) rather than being guessed. In the **review**
  screen you accept, skip, or search for a better match; accepting writes a
  _verified_ mapping so the next sync never asks again.

**Optional LLM judge (`claude -p`).** With `[classify] enabled = true`, the review
screen's `c` action (or `s2y --classify`) re-searches YouTube Music per quarantined
track and asks Claude to pick a match. Tracks are judged in **one batched call per
`batch_size` chunk** (default 40), so a large backlog costs a handful of calls. It
**auto-resolves only ACCEPTs at ≥ `auto_accept_confidence`** (0.85 — above the
matcher's bar) whose chosen `video_id` was actually shown for that track; the parser
rejects hallucinated or misindexed ids, and every failure (timeout, bad output,
non-zero exit) becomes `UNCERTAIN` and stays quarantined. It uses your existing
`claude` login — no API key required.

**Idempotency & resume.** A `track_map` caches every Spotify-URI → `videoId` so
matches are never recomputed; each collection stores a **signature** (Spotify's
`snapshot_id` for playlists, a hash of the id set for Liked Songs/Albums) for cheap
change-detection. On partial failure the YouTube playlist id is still persisted (so
the next run dedupes against what already landed) but the signature is **withheld**,
so the collection is reprocessed and failed adds retry.

## Configuration

State lives under your OS config/data directories (via `platformdirs`), never in
the working directory. On Linux that's `~/.config/s2y/` and `~/.local/share/s2y/`;
macOS uses `~/Library/Application Support/s2y/`. Secret files are written with
`0600` perms (their directory `0700`) and are gitignored.

| Path | What |
| --- | --- |
| `<config>/config.toml` | non-secret settings (below) |
| `<config>/spotify-token.json` | cached Spotify OAuth token |
| `<config>/ytmusic-browser.json` *or* `ytmusic-oauth.json` | YouTube Music credentials |
| `<data>/s2y.db` | SQLite identity map + state |
| `<data>/logs/s2y.log` | rotating log (1 MB × 3; logging is file-only) |

`config.toml` is written by the setup wizard, but you can edit it. Notable keys:

```toml
[spotify]
client_id = "…"                 # from developer.spotify.com (public; no secret)
redirect_port = 8888            # local callback port
redirect_uri = ""               # blank = loopback; set for a tunnel/HTTPS redirect

[ytmusic]
auth_mode = "browser"           # "browser" | "oauth"

[sync]
conflict_policy = "union"       # v1 supports "union" (adds-only) only
track_sleep = 0.1               # per-track pacing (seconds)
circuit_breaker_pct = 30        # reserved for the two-way phase
request_timeout = 15.0

[collections]
sync_playlists = true
sync_liked_songs = true
sync_liked_albums = true
selected_playlists = []         # [] = all; otherwise an allow-list of Spotify ids

[classify]                      # the opt-in LLM judge (off by default)
enabled = false
model = "sonnet"                # claude --model alias or a full model id
auto_accept_confidence = 0.85   # min confidence to auto-resolve an ACCEPT
batch_size = 40                 # tracks per claude -p call
max_candidates = 5              # candidates shown to the model per track
timeout_s = 120.0               # per-call subprocess timeout
```

`S2Y_LOG_LEVEL=DEBUG` (or `-v`) raises log verbosity.

## Architecture

s2y is **hexagonal** — dependencies point inward, and the engine talks to services
only through Protocols, so it's fully testable with in-memory fakes:

```
tui → core + services + store        services → core
core/{models,matching,classify,policy,events} are pure (no I/O)
```

- **`core/`** — pure value types, the scored matcher, the LLM-judge prompt/parser,
  the planner (`build_plan` — reads + matches, no writes), and the engine
  (`build_plan` / `apply_plan` / `classify_quarantine`, plus persistence).
- **`services/`** — `spotipy` and `ytmusicapi` adapters, with retry/backoff/pacing.
- **`auth/`** — Spotify PKCE flow and YouTube Music browser-header / OAuth setup.
- **`store/`** — SQLite schema + typed repository (WAL, foreign keys on).
- **`tui/`** — the Textual app and screens. Blocking service calls run on **daemon
  threads** so a wedged network call can never block quit; engine progress reaches
  the UI only via thread-safe `post_message`.

See [`CLAUDE.md`](CLAUDE.md) for the full module map and invariants.

## Development

```sh
uv run pytest -q              # fast test suite — no test ever hits the network
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
uv run mypy                   # type-check
uv build --wheel              # build (bundles schema.sql + s2y.tcss)
```

CI runs all of the above on Python 3.12 and 3.13, plus a
[gitleaks](https://github.com/gitleaks/gitleaks) secret scan, on every push and PR
(`.github/workflows/ci.yml`).

## Security & secrets

Tokens, cookies, and headers never live in the repo or the database — they're
written to the OS config dir with `0600` perms and are gitignored. The test
fixtures use obviously-fake values, and a gitleaks job scans every commit to keep
it that way.

## License

MIT © Ian Murray
