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

# How long a writer waits for the lock before giving up. Agent runs are long and
# bursty; 10s comfortably covers the worst contention we see with 8 seats live.
BUSY_TIMEOUT_MS = 10_000

_local = threading.local()


def _work_item_rebuild(conn: sqlite3.Connection) -> None:
    """Rebuild work_item: widen the status CHECK to allow 'cancelled', and add
    the accountability/ceiling columns.

    A plain ALTER cannot touch a CHECK constraint, so this is SQLite's documented
    12-step table rebuild. Both pragmas matter: since 3.25 a RENAME rewrites the
    REFERENCES clauses in child tables to follow the new name, so without
    ``legacy_alter_table`` task_ref/asset/artifact_revision end up pointing at
    ``work_item_old`` and lose their parent when it is dropped.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE work_item RENAME TO work_item_old")
        conn.execute("""
            CREATE TABLE work_item (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seat        TEXT NOT NULL,
                title       TEXT NOT NULL,
                brief       TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'queued'
                                CHECK (status IN
                                    ('queued','dispatched','done','failed','cancelled')),
                priority    INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'manual',
                source_ref  TEXT NOT NULL DEFAULT '',
                result      TEXT NOT NULL DEFAULT '',
                -- 0011 additions
                actor         TEXT NOT NULL DEFAULT '',
                scope_tier_id INTEGER REFERENCES bible_section(id) ON DELETE SET NULL,
                total_cost_usd REAL NOT NULL DEFAULT 0,
                num_turns     INTEGER NOT NULL DEFAULT 0,
                max_cost_usd  REAL,
                max_runtime_s INTEGER,
                attempts      INTEGER NOT NULL DEFAULT 0,
                base_commit   TEXT NOT NULL DEFAULT '',
                branch        TEXT NOT NULL DEFAULT '',
                worktree      TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO work_item
                (id, seat, title, brief, status, priority, source, source_ref,
                 result, created_at, updated_at)
            SELECT id, seat, title, brief, status, priority, source, source_ref,
                   result, created_at, updated_at
            FROM work_item_old
        """)
        conn.execute("DROP TABLE work_item_old")
        conn.execute("CREATE INDEX idx_work_status ON work_item(status, priority DESC, id)")
        conn.commit()
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"work_item rebuild broke {len(broken)} references")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


# ---------------------------------------------------------------------------
# Schema. Forward-only: append, never rewrite.
#
# An entry is either a SQL script or a callable(conn) — callables exist for the
# table rebuilds SQLite cannot express as an ALTER.
# ---------------------------------------------------------------------------
_MIGRATIONS: list = [
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
    # 0004 — seats: per-project overrides + the coordination blackboard.
    #
    # Seats are STABLE identities (the agent-spam rule: never one per task).
    # Code defaults live in seats.py; this table only stores what a project
    # changes. Notes are the token-frugal channel seats leave for each other.
    """
    CREATE TABLE seat_config (
        role        TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 1,
        write_globs TEXT,
        mission     TEXT
    );

    CREATE TABLE seat_note (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        role        TEXT NOT NULL,
        topic       TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_note_topic ON seat_note(topic, id);
    CREATE INDEX idx_note_role ON seat_note(role, id);
    """,
    # 0005 — the activity ledger the dashboard watches.
    #
    # One row per meaningful event (lock taken, asset landed, render produced,
    # session recorded, note posted). `seat` is the adopted identity when known,
    # '' when the actor is anonymous. `ref` is a path / slug / id to link on.
    """
    CREATE TABLE activity (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        seat        TEXT NOT NULL DEFAULT '',
        kind        TEXT NOT NULL,
        summary     TEXT NOT NULL,
        ref         TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_activity_id ON activity(id DESC);
    """,
    # 0006 — pinned reference anchors.
    #
    # Approved visual references (character refs, style anchors, the user's
    # concept mocks) were living in scratch dirs that agents rediscovered by
    # path guesswork. A pin makes a reference canonical: named, durable (copied
    # into .bgate/refs/), described, and surfaced in every seat brief.
    """
    CREATE TABLE ref_pin (
        name        TEXT PRIMARY KEY,
        path        TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'style'
                        CHECK (kind IN ('character','style','ui','concept')),
        note        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 0007 — the work queue (Orbit's ticket->task pattern, game-dev shaped).
    #
    # Work flows in from the human (dashboard), from promoted playtest items,
    # and (optionally) from an Orbit import. Seats pull from it; the dashboard
    # dispatches real Claude sessions against it.
    """
    CREATE TABLE work_item (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        seat        TEXT NOT NULL,
        title       TEXT NOT NULL,
        brief       TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','dispatched','done','failed')),
        priority    INTEGER NOT NULL DEFAULT 0,
        source      TEXT NOT NULL DEFAULT 'manual',
        source_ref  TEXT NOT NULL DEFAULT '',
        result      TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_work_status ON work_item(status, priority DESC, id);
    """,
    # 0008 — durable playtest processing, execution-owned asset locks, and
    # immutable artifact revisions. These are the three pieces the cockpit
    # needs to survive restarts and explain which generated thing reached which
    # playable build.
    """
    ALTER TABLE playtest_session ADD COLUMN processing_stage TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_session ADD COLUMN processing_error TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_session ADD COLUMN audio_offset_s REAL NOT NULL DEFAULT 0;
    ALTER TABLE playtest_session ADD COLUMN video_offset_s REAL NOT NULL DEFAULT 0;

    ALTER TABLE asset ADD COLUMN lock_owner TEXT NOT NULL DEFAULT '';

    CREATE TABLE artifact_revision (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        logical_name  TEXT NOT NULL,
        revision      INTEGER NOT NULL,
        path          TEXT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'unknown',
        hash          TEXT NOT NULL DEFAULT '',
        bytes         INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'candidate'
                          CHECK (status IN
                              ('candidate','approved','rejected','integrated','superseded')),
        producer      TEXT NOT NULL DEFAULT '',
        model         TEXT NOT NULL DEFAULT '',
        prompt        TEXT NOT NULL DEFAULT '',
        refs_json     TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        work_item_id  INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        review_note   TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        reviewed_at   TEXT,
        UNIQUE (logical_name, revision)
    );
    CREATE INDEX idx_artifact_name ON artifact_revision(logical_name, revision DESC);
    CREATE INDEX idx_artifact_status ON artifact_revision(status, created_at DESC);
    """,
    # 0009 — first-class iterations and complete playtest/build snapshots.
    #
    # A session without its exact source, build, assets, tunables, checks, and
    # telemetry contract cannot be compared honestly to the next session.
    """
    CREATE TABLE iteration (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        goal                     TEXT NOT NULL,
        status                   TEXT NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active','complete','abandoned')),
        previous_id              INTEGER REFERENCES iteration(id) ON DELETE SET NULL,
        source_commit            TEXT NOT NULL DEFAULT '',
        dirty_fingerprint        TEXT NOT NULL DEFAULT '',
        source_fingerprint       TEXT NOT NULL DEFAULT '',
        export_hash              TEXT NOT NULL DEFAULT '',
        active_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
        tunables_json            TEXT NOT NULL DEFAULT '{}',
        tests_json               TEXT NOT NULL DEFAULT '{}',
        telemetry_schema_version INTEGER NOT NULL DEFAULT 1,
        outcome_json             TEXT NOT NULL DEFAULT '{}',
        created_at               TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at             TEXT
    );
    CREATE INDEX idx_iteration_created ON iteration(id DESC);

    CREATE TABLE iteration_event (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        iteration_id INTEGER NOT NULL REFERENCES iteration(id) ON DELETE CASCADE,
        stage        TEXT NOT NULL,
        ref_type     TEXT NOT NULL DEFAULT '',
        ref_id       TEXT NOT NULL DEFAULT '',
        summary      TEXT NOT NULL DEFAULT '',
        data_json    TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_iteration_event ON iteration_event(iteration_id, id);

    ALTER TABLE playtest_session ADD COLUMN iteration_id
        INTEGER REFERENCES iteration(id) ON DELETE SET NULL;
    ALTER TABLE playtest_item ADD COLUMN director_recommendation
        TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_item ADD COLUMN merged_into_id
        INTEGER REFERENCES playtest_item(id) ON DELETE SET NULL;

    ALTER TABLE artifact_revision ADD COLUMN iteration_id
        INTEGER REFERENCES iteration(id) ON DELETE SET NULL;

    ALTER TABLE asset ADD COLUMN work_item_id
        INTEGER REFERENCES work_item(id) ON DELETE SET NULL;
    ALTER TABLE asset ADD COLUMN heartbeat_at TEXT;
    ALTER TABLE asset ADD COLUMN lease_expires_at TEXT;

    CREATE TABLE playtest_item_asset (
        item_id      INTEGER NOT NULL REFERENCES playtest_item(id) ON DELETE CASCADE,
        logical_name TEXT NOT NULL,
        confidence   REAL NOT NULL DEFAULT 1,
        PRIMARY KEY (item_id, logical_name)
    );
    CREATE INDEX idx_feedback_asset ON playtest_item_asset(logical_name, item_id);
    """,
    # 0010 — per-task anchored references + a generic per-seat workspace store.
    #
    # task_ref: references a user anchors to ONE work item, layered on top of the
    #   global ref_pin set. `ref` holds a pin name or a project-relative path;
    #   resolution (task_refs.resolve_for_task) returns task refs first, then the
    #   global pins, so the task's anchors take priority for that task.
    # workspace_doc: a small JSON blob keyed by (seat, key) — backs the seat
    #   workspaces that need to persist free-form state (narrative storyboards,
    #   art flow maps, qa bot rosters, sound cue sheets) without a table each.
    """
    CREATE TABLE task_ref (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        work_item_id INTEGER NOT NULL REFERENCES work_item(id) ON DELETE CASCADE,
        ref          TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT 'style'
                         CHECK (kind IN ('character','style','ui','concept')),
        note         TEXT NOT NULL DEFAULT '',
        rank         INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (work_item_id, ref)
    );
    CREATE INDEX idx_task_ref_item ON task_ref(work_item_id, rank);

    CREATE TABLE workspace_doc (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        seat       TEXT NOT NULL DEFAULT '',
        key        TEXT NOT NULL,
        data_json  TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (seat, key)
    );
    """,
    # 0011 — accountability and ceilings.
    #
    # The QA audit's three converged blockers all need schema: nobody could answer
    # "who approved this", "what did tonight cost", or "which scope tier is this
    # work under". Everything here exists to make those answerable.
    #
    # actor: the human (or agent) responsible for a row. Agents stamp
    #   'agent:item-<id>'; a dashboard session stamps the OS/studio identity.
    #   `approved` now demands a non-agent actor — see artifacts.review.
    # spend_*: dispatch is the only thing in the product that can spend money
    #   unboundedly, so cost lands on the item and rolls up into a budget the
    #   dispatcher checks BEFORE spawning.
    # scope_tier_id: the cut line only has teeth if work points at a tier.
    # workflow_run / job: the two long-running-operation models the UI needs to
    #   render progress instead of blocking a request.
    _work_item_rebuild,
    """
    -- Accountability. Free-form so a studio can put an SSO subject here later.
    ALTER TABLE activity ADD COLUMN actor TEXT NOT NULL DEFAULT '';
    ALTER TABLE artifact_revision ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE asset ADD COLUMN lock_actor TEXT NOT NULL DEFAULT '';

    -- Repro steps. The QA seat had nowhere to type what the mic did not catch.
    ALTER TABLE playtest_item ADD COLUMN notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_item ADD COLUMN repro_steps TEXT NOT NULL DEFAULT '';

    -- Pinned references become versioned, like artifacts: the row points at the
    -- newest revision and every artifact records the hash it actually resolved.
    ALTER TABLE ref_pin ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE ref_pin ADD COLUMN hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE ref_pin ADD COLUMN updated_at TEXT;

    CREATE TABLE ref_pin_revision (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        revision    INTEGER NOT NULL,
        path        TEXT NOT NULL,
        hash        TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT '',
        actor       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (name, revision)
    );

    -- Money. One row per spend event, plus a budget the dispatcher enforces.
    CREATE TABLE spend_event (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'agent'
                         CHECK (kind IN ('agent','image','audio','other')),
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        logical_name TEXT NOT NULL DEFAULT '',
        usd          REAL NOT NULL DEFAULT 0,
        detail       TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_spend_created ON spend_event(created_at);
    CREATE INDEX idx_spend_item ON spend_event(work_item_id);

    CREATE TABLE spend_budget (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        per_item_usd  REAL NOT NULL DEFAULT 5,
        per_day_usd   REAL NOT NULL DEFAULT 25,
        per_project_usd REAL NOT NULL DEFAULT 250,
        max_runtime_s INTEGER NOT NULL DEFAULT 1800,
        max_concurrent INTEGER NOT NULL DEFAULT 4,
        enforced      INTEGER NOT NULL DEFAULT 1,
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT INTO spend_budget (id) VALUES (1);

    -- Workflow runs: the node canvas needs somewhere to paint status from.
    CREATE TABLE workflow_run (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL DEFAULT '',
        seat        TEXT NOT NULL DEFAULT '',
        graph_json  TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','passed','failed','cancelled')),
        actor       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE workflow_run_node (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       INTEGER NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
        node_id      TEXT NOT NULL,
        kind         TEXT NOT NULL DEFAULT '',
        label        TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','queued','running','passed','failed','skipped')),
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        detail       TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (run_id, node_id)
    );
    CREATE INDEX idx_wf_node_run ON workflow_run_node(run_id);

    -- Generic async job, so a 90-second Godot import stops holding an HTTP
    -- request open. The playtest processing worker is the pattern being reused.
    CREATE TABLE job (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed','cancelled')),
        progress    REAL NOT NULL DEFAULT 0,
        stage       TEXT NOT NULL DEFAULT '',
        request_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT NOT NULL DEFAULT '{}',
        error       TEXT NOT NULL DEFAULT '',
        actor       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_job_status ON job(status, id DESC);

    -- QA bot expectations + the last run kept as a baseline to diff against.
    CREATE TABLE qa_bot_run (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bot         TEXT NOT NULL,
        verdict     TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (verdict IN ('pass','fail','error','unknown')),
        expectations_json TEXT NOT NULL DEFAULT '[]',
        results_json TEXT NOT NULL DEFAULT '[]',
        samples_json TEXT NOT NULL DEFAULT '{}',
        build_ref   TEXT NOT NULL DEFAULT '',
        is_baseline INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_qa_bot_run ON qa_bot_run(bot, id DESC);

    -- Waiters on a locked asset, so 'who is blocked on this .blend' is visible.
    CREATE TABLE asset_waiter (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_path  TEXT NOT NULL,
        seat        TEXT NOT NULL,
        owner       TEXT NOT NULL DEFAULT '',
        since       TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (asset_path, owner)
    );

    -- Advisory locks on text paths. Binaries lock because they cannot merge;
    -- two agents in overlapping lanes editing one .gd is last-write-wins, which
    -- is the same problem with a friendlier failure mode.
    CREATE TABLE path_lease (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL UNIQUE,
        seat        TEXT NOT NULL DEFAULT '',
        owner       TEXT NOT NULL DEFAULT '',
        acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at  TEXT
    );
    CREATE INDEX idx_path_lease_owner ON path_lease(owner);
    """,
    # 0012 — when a complaint ENDED, not just when it started.
    #
    # feedback.extract already returned t_end and the INSERT dropped it, so the
    # telemetry join anchored its window to the first syllable of a fifteen
    # second complaint — it went looking for the bug several seconds before the
    # speaker got to describing it. playtest.py reconstructs the span from the
    # stored segments today; with the column it just reads it.
    #
    # Deliberately NOT backfilled: 0 means "never recorded", and playtest reads
    # that as a cue to reconstruct the span from the session's segments. Writing
    # t into it would look like a real answer and permanently freeze every
    # existing complaint to a zero-length span — losing the reconstruction the
    # code can still do.
    """
    ALTER TABLE playtest_item ADD COLUMN t_end REAL NOT NULL DEFAULT 0;
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
    # WAL gives us concurrent readers, not concurrent writers. The dashboard
    # threadpool, the qa_gate daemon, N completion watchers, the playtest worker
    # and every spawned agent's own MCP server all write this file — without a
    # busy timeout the loser of any race gets an instant "database is locked"
    # and an agent's finished work is dropped on the floor.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _migrate(conn)

    cache[key] = conn
    _local.conns = cache
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations under an exclusive lock.

    Every process that opens the DB runs this. Unguarded, two starting at once
    both read user_version=N and both replay migration N+1, and the second one
    dies on 'table already exists' — taking a dashboard or an agent's MCP server
    down at startup. The lock serialises them; the re-read inside it means the
    loser sees the winner's work and does nothing.
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= len(_MIGRATIONS):
        return  # fast path: no lock, no write, nothing pending

    conn.execute("BEGIN EXCLUSIVE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        pending = list(enumerate(_MIGRATIONS[version:], start=version + 1))
        conn.commit()  # callables manage their own transactions
    except Exception:
        conn.rollback()
        raise

    for i, step in pending:
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
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
