-- s2y persistent state. Applied with CREATE ... IF NOT EXISTS so it is safe to
-- run on every connect. Bump PRAGMA user_version (set in db.py) on real changes.

-- The cross-service identity cache. Append-mostly: once a Spotify track is
-- resolved to a YouTube Music videoId we never re-match it.
--
-- spotify_uri is UNIQUE (a given Spotify track resolves to exactly one row).
-- yt_video_id is only indexed, NOT unique: two distinct Spotify tracks can
-- legitimately resolve to the same YouTube video (single vs album version, the
-- same recording on two releases), so uniqueness there would be wrong.
CREATE TABLE IF NOT EXISTS track_map (
    id               INTEGER PRIMARY KEY,
    isrc             TEXT,
    canon_title      TEXT NOT NULL,
    canon_artist     TEXT NOT NULL,
    duration_ms      INTEGER,
    spotify_uri      TEXT UNIQUE,
    yt_video_id      TEXT,
    match_method     TEXT NOT NULL,
    match_confidence REAL NOT NULL DEFAULT 0,
    verified         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_map_video ON track_map(yt_video_id);
CREATE INDEX IF NOT EXISTS idx_track_map_isrc  ON track_map(isrc);

-- Pairs a logical collection across services. Playlists key on
-- spotify_playlist_id; Liked Songs / Liked Albums are singletons keyed by kind.
CREATE TABLE IF NOT EXISTS playlist_map (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    kind                   TEXT NOT NULL,  -- playlist | liked_songs | liked_albums
    spotify_playlist_id    TEXT UNIQUE,
    spotify_snapshot_id    TEXT,
    yt_playlist_id         TEXT,
    yt_content_fingerprint TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
-- At most one liked_songs row and one liked_albums row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_map_singleton
    ON playlist_map(kind) WHERE kind != 'playlist';

-- Desired-state membership; covers playlists AND the liked pseudo-collections.
-- yt_set_video_id is captured at insert time and is required to *remove* a YT
-- playlist item later (Phase 3). The Liked Music collection has no setVideoId,
-- so un-likes there use rate_song(INDIFFERENT) instead.
CREATE TABLE IF NOT EXISTS playlist_membership (
    playlist_map_id  INTEGER NOT NULL REFERENCES playlist_map(id) ON DELETE CASCADE,
    track_map_id     INTEGER NOT NULL REFERENCES track_map(id)    ON DELETE CASCADE,
    present_spotify  INTEGER NOT NULL DEFAULT 0,
    present_yt       INTEGER NOT NULL DEFAULT 0,
    yt_set_video_id  TEXT,
    spotify_added_at TEXT,
    yt_added_at      TEXT,
    tombstoned_at    TEXT,
    PRIMARY KEY (playlist_map_id, track_map_id)
);

-- Per-side last-known state: the *base* a future run diffs against. Latest row
-- per (side, playlist_map_id) wins; older rows are kept as history.
CREATE TABLE IF NOT EXISTS snapshot (
    id              INTEGER PRIMARY KEY,
    side            TEXT NOT NULL,  -- spotify | yt
    playlist_map_id INTEGER NOT NULL REFERENCES playlist_map(id) ON DELETE CASCADE,
    taken_at        TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_lookup ON snapshot(playlist_map_id, side, id);

-- Unmatched / low-confidence tracks awaiting human review. First-class state:
-- a track sitting here must NEVER be interpreted as a deletion.
CREATE TABLE IF NOT EXISTS quarantine (
    id                      INTEGER PRIMARY KEY,
    source_side             TEXT NOT NULL,
    source_uri              TEXT,
    title                   TEXT NOT NULL,
    artist                  TEXT NOT NULL,
    album                   TEXT,
    reason                  TEXT NOT NULL,  -- no_match | low_conf | ambiguous
    best_candidate_json     TEXT,
    attempts                INTEGER NOT NULL DEFAULT 1,
    resolved                INTEGER NOT NULL DEFAULT 0,
    resolution_track_map_id INTEGER REFERENCES track_map(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(source_side, source_uri)
);

-- Deletes with a grace period (Phase 3, two-way). Defined now so the schema is
-- stable; unused by the v1 union policy.
CREATE TABLE IF NOT EXISTS tombstone (
    id                   INTEGER PRIMARY KEY,
    playlist_map_id      INTEGER NOT NULL REFERENCES playlist_map(id) ON DELETE CASCADE,
    track_map_id         INTEGER NOT NULL REFERENCES track_map(id)    ON DELETE CASCADE,
    side_removed         TEXT NOT NULL,
    removed_at           TEXT NOT NULL,
    grace_runs_remaining INTEGER NOT NULL DEFAULT 2
);

-- Run history / resumability.
CREATE TABLE IF NOT EXISTS sync_run (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,  -- import | sync
    dry_run     INTEGER NOT NULL DEFAULT 0,
    stats_json  TEXT,
    status      TEXT NOT NULL DEFAULT 'running'  -- running | ok | error | aborted
);
