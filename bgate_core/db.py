"""SQLite store — one file per game project, no daemon.

The database lives at ``<project_root>/.bgate/game.db`` so it travels with the
game repo. Schema is applied forward-only via ``PRAGMA user_version``; add a new
entry to ``_MIGRATIONS`` and never edit a shipped one.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

DB_DIRNAME = ".bgate"
DB_FILENAME = "game.db"

_local = threading.local()


# ---------------------------------------------------------------------------
# Schema. Forward-only: append, never rewrite.
# ---------------------------------------------------------------------------
_MIGRATIONS: list[str] = [
    # 0001 — project identity, design bible, lore graph, canon facts, assets.
    """
    CREATE TABLE project (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        name        TEXT NOT NULL,
        slug        TEXT NOT NULL,
        pitch       TEXT NOT NULL DEFAULT '',
        engine      TEXT NOT NULL DEFAULT 'godot',
        dimension   TEXT NOT NULL DEFAULT '2d',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- The design bible. Sections are typed so the Director seat can reason about
    -- scope (tiers + cut_line) without parsing prose.
    CREATE TABLE bible_section (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL CHECK (kind IN
                        ('pillar','loop','scope_tier','cut_line','constraint','reference')),
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        rank        INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_bible_kind ON bible_section(kind, rank);

    -- Lore entities: the nouns of the world.
    CREATE TABLE lore_entity (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL CHECK (kind IN
                        ('faction','character','place','event','item','concept','species')),
        name        TEXT NOT NULL,
        slug        TEXT NOT NULL UNIQUE,
        summary     TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','canon','retired')),
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_entity_kind ON lore_entity(kind);
    CREATE INDEX idx_entity_status ON lore_entity(status);

    -- Typed edges between entities. rel is free-form ('allied_with', 'rules',
    -- 'born_in') — the graph is descriptive, not a fixed ontology.
    CREATE TABLE lore_link (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        src_id      INTEGER NOT NULL REFERENCES lore_entity(id) ON DELETE CASCADE,
        dst_id      INTEGER NOT NULL REFERENCES lore_entity(id) ON DELETE CASCADE,
        rel         TEXT NOT NULL,
        note        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (src_id, dst_id, rel)
    );
    CREATE INDEX idx_link_src ON lore_link(src_id);
    CREATE INDEX idx_link_dst ON lore_link(dst_id);

    -- Atomic canon assertions. canon_check reads these; prose in lore_entity.body
    -- is for humans, facts are for machines.
    CREATE TABLE canon_fact (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id   INTEGER REFERENCES lore_entity(id) ON DELETE CASCADE,
        statement   TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT '',
        locked      INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_fact_entity ON canon_fact(entity_id);

    -- Full-text over the bible + lore + facts. Populated by search.py; content
    -- is denormalized on purpose so recall is one query with no joins.
    CREATE VIRTUAL TABLE search_idx USING fts5(
        ref, kind, title, text
    );

    -- Binary asset registry. Assets are content-hashed and LOCKED, never merged:
    -- two agents editing one .blend is the failure mode this table exists for.
    CREATE TABLE asset (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL UNIQUE,
        kind        TEXT NOT NULL DEFAULT 'unknown',
        hash        TEXT NOT NULL DEFAULT '',
        bytes       INTEGER NOT NULL DEFAULT 0,
        lock_seat   TEXT,
        lock_at     TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_asset_lock ON asset(lock_seat);
    """,
    # 0002 — playtest sessions: recording, transcript, telemetry, feedback.
    """
    -- One play session. The VIDEO is for the human; the agent-facing artifact is
    -- the aligned transcript + frames + telemetry (agents cannot watch video).
    -- All t_* columns are SECONDS FROM SESSION START — the one clock everything
    -- joins on.
    CREATE TABLE playtest_session (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        name           TEXT NOT NULL,
        slug           TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'recording'
                           CHECK (status IN ('recording','processing','ready','failed')),
        started_at     TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at       TEXT,
        duration_s     REAL NOT NULL DEFAULT 0,
        video_path     TEXT,
        audio_path     TEXT,
        telemetry_path TEXT,
        frames_dir     TEXT,
        game_cmd       TEXT NOT NULL DEFAULT '',
        build_ref      TEXT NOT NULL DEFAULT '',
        error          TEXT,
        notes          TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_session_status ON playtest_session(status);

    -- Transcript segments, timestamped against session start.
    CREATE TABLE playtest_segment (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES playtest_session(id) ON DELETE CASCADE,
        t_start     REAL NOT NULL,
        t_end       REAL NOT NULL,
        text        TEXT NOT NULL,
        confidence  REAL
    );
    CREATE INDEX idx_segment_session ON playtest_segment(session_id, t_start);

    -- Feedback items lifted from the transcript. status stays 'new' until the
    -- human promotes it: thinking out loud mid-play must not become backlog.
    CREATE TABLE playtest_item (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER NOT NULL REFERENCES playtest_session(id) ON DELETE CASCADE,
        segment_id   INTEGER REFERENCES playtest_segment(id) ON DELETE SET NULL,
        t            REAL NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'note'
                         CHECK (kind IN ('like','fix','add','change','question','note')),
        text         TEXT NOT NULL,
        seat         TEXT NOT NULL DEFAULT 'unassigned',
        frame_path   TEXT,
        status       TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new','promoted','dismissed')),
        promoted_ref TEXT
    );
    CREATE INDEX idx_item_session ON playtest_item(session_id, t);
    CREATE INDEX idx_item_status ON playtest_item(status);

    -- Game-emitted events (JSONL), indexed on the same clock as the transcript.
    -- This is what turns "the jump feels floaty" into a number an agent can act on.
    CREATE TABLE playtest_event (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL REFERENCES playtest_session(id) ON DELETE CASCADE,
        t           REAL NOT NULL,
        kind        TEXT NOT NULL,
        data        TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_event_session ON playtest_event(session_id, t);
    CREATE INDEX idx_event_kind ON playtest_event(session_id, kind);
    """,
    # 0003 — wall-clock anchor for the session.
    #
    # The game's clock and the recorder's clock are unrelated: the game may have
    # launched long before recording started, or after. Telemetry therefore
    # carries a UNIX timestamp, and this column is the anchor that converts it
    # onto the session clock. Without it, every telemetry join is silently off by
    # however long the game had been running.
    """
    ALTER TABLE playtest_session ADD COLUMN started_epoch REAL;
    """,
]


def db_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / DB_DIRNAME / DB_FILENAME


def resolve_root(start: Optional[str | os.PathLike[str]] = None) -> Optional[Path]:
    """Walk up from ``start`` looking for a ``.bgate`` dir. None if unfound."""
    cur = Path(start or os.getcwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / DB_DIRNAME / DB_FILENAME).exists():
            return candidate
    return None


def connect(root: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open (and migrate) the project database. Cached per thread + path."""
    path = db_path(root)
    cache: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    key = str(path)
    conn = cache.get(key)
    if conn is not None:
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(key, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _migrate(conn)

    cache[key] = conn
    _local.conns = cache
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()


@contextmanager
def tx(root: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    """Transaction scope. Commits on clean exit, rolls back on raise."""
    conn = connect(root)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_all() -> None:
    """Drop this thread's cached connections (tests, or after moving a project)."""
    for conn in (getattr(_local, "conns", None) or {}).values():
        try:
            conn.close()
        except Exception:
            pass
    _local.conns = {}
