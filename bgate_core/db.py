"""SQLite store — one file per game project, no daemon.

The database lives at ``<project_root>/.bgate/game.db`` so it travels with the
game repo. Schema is applied forward-only via ``PRAGMA user_version``; add a new
entry to ``_MIGRATIONS`` and never edit a shipped one.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
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


def _work_item_chain_rebuild(conn: sqlite3.Connection) -> None:
    """Rebuild work_item again: chains, and a 'review' status to hold work at.

    Two things the board could not express, both of which a human was doing by
    hand every session.

    CHAINS. Dependent work had exactly one way to say "this goes after that":
    priority. That is an ordering, not a dependency — auto-deploy would dispatch
    a scene-wiring item and its prerequisite at the same moment, and the second
    agent would write against a file the first had not created yet. So an item
    can now name the item it waits for (``depends_on``) and the group it belongs
    to (``chain_id`` + ``chain_pos``), and nothing dispatches ahead of its
    predecessor.

    'review'. An agent that has finished but not yet been approved is in a state
    the old CHECK had no word for, and both available lies were expensive:
    'done' advances the chain before anyone has looked at the work, and
    'dispatched' claims an agent is still burning money. A CHECK cannot be
    ALTERed, hence the second full rebuild rather than two ALTERs.

    Same 12-step dance and the same two pragmas as _work_item_rebuild — see its
    docstring for why ``legacy_alter_table`` is not optional here.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("BEGIN")
        conn.execute("DROP INDEX IF EXISTS idx_work_status")
        conn.execute("ALTER TABLE work_item RENAME TO work_item_old")
        conn.execute("""
            CREATE TABLE work_item (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seat        TEXT NOT NULL,
                title       TEXT NOT NULL,
                brief       TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'queued'
                                CHECK (status IN
                                    ('queued','dispatched','review','done',
                                     'failed','cancelled')),
                priority    INTEGER NOT NULL DEFAULT 0,
                source      TEXT NOT NULL DEFAULT 'manual',
                source_ref  TEXT NOT NULL DEFAULT '',
                result      TEXT NOT NULL DEFAULT '',
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
                -- 0014 additions. depends_on is SET NULL rather than CASCADE: a
                -- deleted predecessor must not delete the work that followed it,
                -- and an unblocked orphan is the safe failure — it becomes
                -- dispatchable, which a human can see, instead of vanishing.
                chain_id      TEXT NOT NULL DEFAULT '',
                chain_pos     INTEGER NOT NULL DEFAULT 0,
                depends_on    INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
                approved_by   TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO work_item
                (id, seat, title, brief, status, priority, source, source_ref,
                 result, actor, scope_tier_id, total_cost_usd, num_turns,
                 max_cost_usd, max_runtime_s, attempts, base_commit, branch,
                 worktree, created_at, updated_at)
            SELECT id, seat, title, brief, status, priority, source, source_ref,
                   result, actor, scope_tier_id, total_cost_usd, num_turns,
                   max_cost_usd, max_runtime_s, attempts, base_commit, branch,
                   worktree, created_at, updated_at
            FROM work_item_old
        """)
        conn.execute("DROP TABLE work_item_old")
        conn.execute("CREATE INDEX idx_work_status ON work_item(status, priority DESC, id)")
        conn.execute("CREATE INDEX idx_work_chain ON work_item(chain_id, chain_pos)")
        conn.execute("CREATE INDEX idx_work_depends ON work_item(depends_on)")
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
    # 0013 — what a workflow node PRODUCED, not just whether it finished.
    #
    # Only status was ever persisted, so a node could not hand anything to the
    # next one: an LLM step that writes a prompt had nowhere to put the prompt,
    # and a human picking between candidates had nowhere to put the choice. The
    # value a node produced is small and heterogeneous (a string, a list of
    # artifact ids, one chosen id), so it lands as one JSON blob rather than a
    # column per shape.
    """
    ALTER TABLE workflow_run_node ADD COLUMN output_json TEXT NOT NULL DEFAULT '{}';
    """,
    # 0014 — work chains, and a place to hold finished work before it counts.
    # See _work_item_chain_rebuild for why this is a rebuild and not two ALTERs.
    _work_item_chain_rebuild,
    # 0016 — the event log, so a finished item can tell somebody.
    #
    # Numbered 16 because that is the user_version this entry lands on: the block
    # labelled 0011 is TWO entries (the rebuild callable and its ALTER script), so
    # every label after it reads one behind the pragma. The pragma is what a human
    # debugging a half-migrated database actually looks at, so it wins.
    #
    # Every status transition has always been appended to .bgate/notify.jsonl and
    # nothing has ever read it: the director is not told when its work lands, a
    # chain advances silently, and a human with the console closed learns nothing.
    # The three features that fix that (notify, debrief, badge) all need the same
    # primitive — "what happened since I last looked" — and it cannot be that
    # file. notify.jsonl is written by whichever process flips the status, and
    # queue_complete runs in the per-session MCP server while the reaper runs in
    # the dashboard: multi-writer, no lock, so a monotonic sequence number in it
    # would need a cross-process lock that does not exist, and torn lines are a
    # real Windows failure. This table is the one many-writer-safe store here, and
    # AUTOINCREMENT is the cursor for free — ids are never reused, so a consumer's
    # position still means something after a prune (see bgate_core.events).
    #
    # notify.jsonl keeps being written, unchanged. Its docstring advertises a
    # tail/long-poll surface; this is additive, not a replacement.
    """
    CREATE TABLE event (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,
        ref        TEXT NOT NULL DEFAULT '',
        actor      TEXT NOT NULL DEFAULT '',
        payload    TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- (kind, id) rather than (kind): every read is "events of these kinds AFTER
    -- id N", so the id has to be in the index or a filtered subscriber scans the
    -- whole table on every poll.
    --
    -- NOT idx_event_kind, which the plan asked for and which 0002 already used
    -- for playtest_event(session_id, kind). Index names share ONE namespace per
    -- database regardless of table, so that name fails with 'index already
    -- exists' — on a fresh project, at first connect, taking the dashboard and
    -- every MCP server down before anything else runs.
    CREATE INDEX idx_event_kind_id ON event(kind, id);
    """,
    # 0017 — who closed an item, so the QA gate stops reviewing hand-closes.
    #
    # The gate reviews STATE AT CLOSE. When a human closes an item by hand —
    # which is what a killed-but-successful run leaves you doing — the gate
    # files a reviewer against whatever the result note says. MEASURED: an
    # agent was dispatched, and paid for, to verify procedurally block-modelled
    # vehicles that had already been superseded by a switch to image-to-3D.
    # Nobody intended to ship the thing under review.
    #
    # A column rather than a marker in `result`, because `result` is prose a
    # human reads and a sentinel in it is a parser waiting to be written.
    """
    ALTER TABLE work_item ADD COLUMN closed_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE work_item ADD COLUMN gate_skip INTEGER NOT NULL DEFAULT 0;
    """,
    # 0018 — pairwise art-tournament matches.
    #
    # art_qa_verdict answers "is this candidate on-model against its
    # reference" — a drift check, not a quality judgement, and the project's
    # own research (docs/visual-taste-research.md) found the VLM-as-judge
    # literature unusually consistent on one point: judges asked for a
    # pairwise "which is better" agree with human raters far better than
    # judges asked for an absolute 1-10 score, which they get wrong even
    # when the ranking they'd imply is right. So this does not add a score
    # column to artifact_revision — it adds a MATCH log, one row per
    # head-to-head decision, and a rating is derived from the log rather
    # than stored, the same split flex_verdict uses for rig thresholds:
    # the match outcome is a fact, Elo over it is a policy a caller can
    # recompute without re-judging anything.
    #
    # shown_first is recorded, not just randomised at dispatch time, because
    # position bias is a documented failure mode of this exact judging
    # pattern (MT-Bench, MLLM-as-Judge) — keeping which side a reviewer saw
    # first is what would let a later audit check whether THIS project's
    # reviewer sessions carry that bias too, rather than assuming the
    # literature's finding and never checking it against real verdicts.
    """
    CREATE TABLE art_match (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        logical_name    TEXT NOT NULL,
        candidate_a_id  INTEGER NOT NULL,
        candidate_b_id  INTEGER NOT NULL,
        shown_first     TEXT NOT NULL DEFAULT 'a'
                            CHECK (shown_first IN ('a','b')),
        winner_id       INTEGER,
        reasons         TEXT NOT NULL DEFAULT '',
        reviewer        TEXT NOT NULL DEFAULT '',
        tournament_ref  TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        decided_at      TEXT
    );
    CREATE INDEX idx_art_match_logical ON art_match(logical_name, id DESC);
    CREATE INDEX idx_art_match_tournament ON art_match(tournament_ref);
    """,

    # 0019 — a stop is not a crash, and prose is not a field.
    #
    # A human ending a run banks the item as `status='failed'` with the only
    # evidence in the result note: "stopped by Sam — this run was ended by
    # hand, it did not die on its own". Honest, and unreadable to anything that
    # is not a person. Measured: three items across three seats flipped to
    # 'failed' in the same second and read as three separate bugs until someone
    # opened the notes; the same-second multi-seat signature is what a systemic
    # event looks like, and no tool could see it.
    #
    # NOT A SIXTH STATUS, deliberately. 'failed' is load-bearing in ~85 places —
    # reopen()'s guard, the QA gate's query, the chain interlock, the console's
    # lanes — and a new status is a silent behaviour change in every one of them
    # that filters by name. What was actually missing is the CAUSE of the
    # failure, which is a different question from the status and belongs in its
    # own column. A stopped item stays failed: it did not finish, it IS worth
    # reopening, and every recovery path keeps working untouched.
    #
    # stopped_at is separate from updated_at because a stopped item that is later
    # reopened and re-run keeps its history; the pair is what tells "this run was
    # killed" from "this item was killed once, three rounds ago".
    """
    ALTER TABLE work_item ADD COLUMN stopped_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE work_item ADD COLUMN stopped_at TEXT NOT NULL DEFAULT '';
    """,

    # 00NN — TOKENS ON THE SPEND LEDGER, AND WHICH BILL A ROW LANDS ON.
    #
    # The ledger held one number, `usd`, and summed every kind into it. Two
    # different bills were being added together:
    #
    #   image / mesh / audio   real money, invoiced by OpenAI or Krea, and the
    #                          only rows where a dollar is a dollar.
    #   agent                  total_cost_usd off the Claude CLI, which on a
    #                          subscription is what the run WOULD have cost on
    #                          the API. Nobody is billed it. Adding it to the
    #                          image rows produced a project total that matched
    #                          no statement anywhere.
    #
    # `billing` is which of those a row is, so the two can be reported apart and
    # the daily ceiling can stop refusing image generation because agents had a
    # busy afternoon of spend that was never charged.
    #
    # The token columns are the other half. What actually runs out on a
    # subscription is a rolling usage window, and that window is driven by
    # TOKENS — 1.19 billion input-side tokens in eight hours emptied a 5-hour
    # allowance in three and a half while the dollar ledger showed a number
    # nobody pays. A ledger that cannot see tokens cannot see the limit that
    # actually bites, so it records them and the model that spent them.
    """
    ALTER TABLE spend_event ADD COLUMN billing TEXT NOT NULL DEFAULT 'api';
    ALTER TABLE spend_event ADD COLUMN model TEXT NOT NULL DEFAULT '';
    ALTER TABLE spend_event ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE spend_event ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE spend_event ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE spend_event ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;
    UPDATE spend_event SET billing = 'subscription' WHERE kind = 'agent';
    CREATE INDEX IF NOT EXISTS idx_spend_billing ON spend_event(billing, created_at);
    """,
    # 0021 — brainstorm sessions: the room before the board.
    #
    # Every sentence typed at the console becomes a work item and is dispatched
    # to a real Claude Code session, which is right for "do this" and ruinous
    # for "what if" — thinking out loud there bills a spawned agent per
    # half-thought and leaves a board full of items nobody meant to file. There
    # was nowhere in the product for the twenty minutes BEFORE you know what you
    # want built, so people used the console for it and paid for the mismatch.
    #
    # NOT workspace_doc, which is where a small per-seat JSON blob would
    # normally go. A brainstorm has an unbounded message list that is appended
    # to from one end and read as a window from the other; in a single JSON
    # document every append is a full read-modify-write of the entire
    # conversation, and two tabs on the same session lose messages to the
    # optimistic-version retry that workspace.set exists to catch.
    #
    # drawing_json is the pad's ELEMENTS, not a picture, and that is the point:
    # a flattened PNG is something a model can only look at, and only with
    # vision — it cannot tell you the box in the corner is called "shrine", and
    # it cannot add one. drawing_png rides alongside for previews and is never
    # the source of truth.
    #
    # deploys_json is what the session put on the board. The work items also
    # carry source='brainstorm' + source_ref=<session id>, so the link exists in
    # both directions on purpose: an item can name the conversation it came
    # from, and a session reopened next month can say what it produced without
    # scanning a queue those items may since have been deleted from.
    """
    CREATE TABLE brainstorm_session (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        seat        TEXT NOT NULL DEFAULT 'director'
                        CHECK (seat IN ('director','narrative')),
        title       TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','deployed','archived')),
        notes         TEXT NOT NULL DEFAULT '',
        drawing_json  TEXT NOT NULL DEFAULT '{}',
        drawing_png   TEXT NOT NULL DEFAULT '',
        deploys_json  TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_brainstorm_seat ON brainstorm_session(seat, status, id DESC);

    -- 'user' / 'assistant' are the model API's own role names, kept verbatim so
    -- the transcript maps 1:1 onto a messages array. A translation table here is
    -- where the off-by-one that puts the model's words in the human's mouth
    -- lives.
    CREATE TABLE brainstorm_message (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL
                        REFERENCES brainstorm_session(id) ON DELETE CASCADE,
        role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
        text        TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_brainstorm_message ON brainstorm_message(session_id, id);
    """,
    # 0022 — where a line of playtest evidence CAME FROM.
    #
    # Everything in playtest_segment/playtest_item has until now been whisper's
    # output, so provenance was implicit and nothing needed to record it. The
    # notepad breaks that: a note the player TYPES mid-play is the same kind of
    # object as a spoken remark — same clock, same triage, same report — but it
    # was not heard, and two things go wrong without a column saying so.
    #
    # 1. IT GETS DELETED. transcribe_session runs `DELETE FROM playtest_segment
    #    WHERE session_id = ?` and `DELETE FROM playtest_item WHERE session_id =
    #    ? AND status = 'new'` before it writes the transcript, so it can be
    #    re-run without duplicating anything. A typed note lives in exactly
    #    those two tables and is 'new' by definition, so stopping the session —
    #    the very next thing you do after taking notes — silently destroyed
    #    every one of them. The DELETEs now exclude source='typed'; the column
    #    is what makes that expressible.
    # 2. A READER CANNOT TELL. "the boss hitbox is wrong" carries different
    #    weight depending on whether the human typed it deliberately or whisper
    #    guessed it out of a noisy mic, and the review UI, the bug report and
    #    the QA queue all show the text with no way to say which.
    #
    # Default '' rather than 'transcribed': every existing row predates the
    # notepad and IS transcribed, but '' means "nobody recorded this", which is
    # the honest statement about a row written before the question was asked.
    # Readers treat anything that is not 'typed' as speech, so the default is
    # also the correct behaviour.
    """
    ALTER TABLE playtest_segment ADD COLUMN source TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_item ADD COLUMN source TEXT NOT NULL DEFAULT '';
    """,
    # 0023 — the spend ledger has been SILENTLY DROPPING a whole category.
    #
    # spend_event's CHECK was written in 0001 as ('agent','image','audio','other')
    # and never widened. spend.KINDS grew 'mesh' when image-to-3D landed, and
    # spend.record swallows every exception by design — "losing the ledger must
    # not lose the work that produced it" — so each mesh row raised an
    # IntegrityError, was caught, and vanished. A textured generation is ~$0.30,
    # an order of magnitude over an image, and NONE of it has ever reached the
    # ledger or the daily ceiling. The constraint that was meant to catch typos
    # was quietly deleting the most expensive rows in the table.
    #
    # 'video' is added at the same time rather than repeating the mistake one
    # capability later: kie's own docs put a clip at 100-500 credits against an
    # image's 10-50, so it is now the dearest thing this product can buy.
    #
    # A CHECK cannot be ALTERed in SQLite, so this is the 12-step rebuild. It is
    # written to be replay-safe (IF NOT EXISTS / IF EXISTS throughout) because
    # _apply_sql_step's repair path replays a step whose objects already exist.
    # No other table references spend_event, so there is nothing to re-point.
    """
    CREATE TABLE IF NOT EXISTS spend_event_new (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'agent'
                         CHECK (kind IN ('agent','image','audio','video',
                                         'mesh','other')),
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        logical_name TEXT NOT NULL DEFAULT '',
        usd          REAL NOT NULL DEFAULT 0,
        detail       TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        billing      TEXT NOT NULL DEFAULT 'api',
        model        TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO spend_event_new
        (id, kind, work_item_id, logical_name, usd, detail, created_at,
         billing, model, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens)
    SELECT id, kind, work_item_id, logical_name, usd, detail, created_at,
           billing, model, input_tokens, output_tokens, cache_read_tokens,
           cache_write_tokens
    FROM spend_event;
    DROP TABLE spend_event;
    ALTER TABLE spend_event_new RENAME TO spend_event;
    CREATE INDEX IF NOT EXISTS idx_spend_created ON spend_event(created_at);
    CREATE INDEX IF NOT EXISTS idx_spend_item ON spend_event(work_item_id);
    CREATE INDEX IF NOT EXISTS idx_spend_billing
        ON spend_event(billing, created_at);
    """,
    # 0024 — 'speech' joins the ledger, and it is deliberately NOT 'audio'.
    #
    # Deepgram bills the human talking to the brainstorm agent by the minute
    # ($0.0048) and the agent talking back by the character ($0.030/1k). Filed
    # under 'audio' those rows would sum into the same bucket as a generated
    # music track, which is the exact complaint 0023 makes about 'mesh' landing
    # in 'other': the bucket stops answering the question an author reads it to
    # ask. They are also different rate drivers — one scales with how long
    # somebody spoke, the other with how much the model wrote — so a single
    # number over both explains nothing when the month looks wrong.
    #
    # Without this step spend.record coerces 'speech' to 'other' (KINDS guards
    # the insert before SQLite sees it), so the rows are not lost either way.
    # This is what makes them findable.
    #
    # Same 12-step CHECK rebuild as 0023, replay-safe for the same reason.
    """
    CREATE TABLE IF NOT EXISTS spend_event_new (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL DEFAULT 'agent'
                         CHECK (kind IN ('agent','image','audio','video',
                                         'mesh','speech','other')),
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        logical_name TEXT NOT NULL DEFAULT '',
        usd          REAL NOT NULL DEFAULT 0,
        detail       TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        billing      TEXT NOT NULL DEFAULT 'api',
        model        TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO spend_event_new
        (id, kind, work_item_id, logical_name, usd, detail, created_at,
         billing, model, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens)
    SELECT id, kind, work_item_id, logical_name, usd, detail, created_at,
           billing, model, input_tokens, output_tokens, cache_read_tokens,
           cache_write_tokens
    FROM spend_event;
    DROP TABLE spend_event;
    ALTER TABLE spend_event_new RENAME TO spend_event;
    CREATE INDEX IF NOT EXISTS idx_spend_created ON spend_event(created_at);
    CREATE INDEX IF NOT EXISTS idx_spend_item ON spend_event(work_item_id);
    CREATE INDEX IF NOT EXISTS idx_spend_billing
        ON spend_event(billing, created_at);
    """,
    # 0025 — community feedback sessions: what live chat said, while it was asked.
    #
    # WHY NOT playtest_item, WHICH IS THE SAME SHAPE. It very nearly fits — the
    # kind/seat/status vocabulary here is playtest_item's, copied deliberately so
    # bgate_core.feedback's classify() and route() do the work for both — but
    # playtest_item.session_id is NOT NULL against playtest_session, and a chat
    # session is not a playtest: there is no recording, no transcript, no clock
    # to hang `t` off and no device. Filing chat under a fabricated playtest
    # session would put rows in front of transcribe_session's DELETE and into
    # the playtest report, both of which describe something that never happened.
    # Same vocabulary, own table.
    #
    # AUTHORS ARE STORED BY THE PLATFORM'S OWN ID AND DISPLAY NAME AND NOTHING
    # ELSE. That is what a moderator's delete and the per-author rate limit need,
    # and it is the entire extent of what this product knows about a viewer.
    #
    # `flags` records what the sanitiser removed on the way in (an injection
    # attempt, a link, a truncation). The hostile form of a message is never
    # stored — chatlink.sanitise runs at capture — so this is the only trace
    # that somebody tried, which is a thing a dev should be able to count after
    # a stream rather than a thing that vanishes silently.
    #
    # `brainstorm_id` is the ONE link to the rest of the product, and it points
    # at a room that cannot file work either. There is deliberately no
    # work_item_id here: the path from chat to the board runs through a plan a
    # human read and confirmed, and a column would be an invitation to shorten
    # it.
    """
    CREATE TABLE chat_feedback_session (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        platform      TEXT NOT NULL DEFAULT 'twitch',
        channel       TEXT NOT NULL DEFAULT '',
        title         TEXT NOT NULL DEFAULT '',
        prompt        TEXT NOT NULL DEFAULT '',
        capture       TEXT NOT NULL DEFAULT 'all'
                          CHECK (capture IN ('all','marked')),
        status        TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','closed')),
        fence         TEXT NOT NULL DEFAULT '',
        brainstorm_id INTEGER,
        seen          INTEGER NOT NULL DEFAULT 0,
        dropped       INTEGER NOT NULL DEFAULT 0,
        started_at    TEXT NOT NULL DEFAULT (datetime('now')),
        stopped_at    TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_chatfb_status
        ON chat_feedback_session(status, id DESC);

    CREATE TABLE chat_feedback_item (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL
                        REFERENCES chat_feedback_session(id) ON DELETE CASCADE,
        msg_id      TEXT NOT NULL DEFAULT '',
        user_id     TEXT NOT NULL DEFAULT '',
        author      TEXT NOT NULL DEFAULT 'viewer',
        kind        TEXT NOT NULL DEFAULT 'note'
                        CHECK (kind IN ('like','fix','add','change',
                                        'question','note')),
        text        TEXT NOT NULL,
        seat        TEXT NOT NULL DEFAULT 'unassigned',
        marked      INTEGER NOT NULL DEFAULT 0,
        flags       TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new','promoted','dismissed',
                                          'retracted')),
        at          REAL NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_chatfb_item ON chat_feedback_item(session_id, id);
    CREATE INDEX idx_chatfb_item_msg ON chat_feedback_item(msg_id);
    CREATE INDEX idx_chatfb_item_user ON chat_feedback_item(session_id, user_id);
    """,
    # 0026 — WHO SAID IT, once a playtest note can come from somebody who is not
    # in the room.
    #
    # Migration 0022 added `source` so a note the player TYPED could be told
    # apart from a sentence whisper guessed. That was enough while every note in
    # the session came from one person: the player. A viewer's note is a third
    # case — deliberate like a typed note, but NOT the dev's own observation —
    # and "source" cannot carry it, because source answers "how did this arrive"
    # and this question is "whose opinion is this".
    #
    # It has to be answerable at a glance, and it has to be answerable
    # downstream. A dev reading their notepad must be able to weigh "the boss
    # feels unfair" differently depending on whether they wrote it or a stranger
    # did, and the bug report and the work-item brief built from that note carry
    # the same distinction into the hands of an agent. Encoding the author into
    # `source` (source='chat:someviewer') was the alternative and it is the
    # worse one: every existing filter compares source for equality, so each of
    # them would have had to become a prefix match, and a prefix match is how
    # 'typed' starts matching 'typedbychat' two years from now.
    #
    # Empty means the project owner, which is what every row written before this
    # column existed was. That default is not a placeholder — it is the correct
    # statement about those rows, so nothing needs backfilling.
    #
    # THERE IS DELIBERATELY NO COLUMN HERE JOINING A FEEDBACK SESSION TO A
    # RECORDING. They are two separate features with two separate triggers and
    # two separate destinations — a chat note belongs to the recording it was
    # stamped against, a feedback session belongs to the question the dev asked
    # — and a foreign key between them would be the first step towards a message
    # that exists in both. The rule that keeps that impossible is a routing rule
    # rather than a schema one (chatfeedback.owner: an open feedback session owns
    # chat capture, otherwise a live recording does, and never both), because the
    # thing being prevented is a message being WRITTEN TWICE, and only the writer
    # can prevent that.
    """
    ALTER TABLE playtest_segment ADD COLUMN author TEXT NOT NULL DEFAULT '';
    ALTER TABLE playtest_item ADD COLUMN author TEXT NOT NULL DEFAULT '';
    """,
    # 0027 — THE SHOT LIST, because a cutscene is a SEQUENCE and every video
    # model on the market generates one shot at a time.
    #
    # WHY THIS IS A TABLE AND MUSIC NEEDED NONE. A Suno request returns the whole
    # deliverable: one prompt, one track, and the logical name plus the artifact
    # metadata carry everything there is to know. A cutscene is not like that.
    # Every model wired here caps a generation at 15 seconds (kie MODELS,
    # seedance-2: duration 4..15), so a 90-second scene is EIGHT separate paid
    # generations that have to be kept in order, and the ORDER is not derivable
    # from anything on disk — .bgate_out/cinematic/foo_shot3.mp4 sorts before
    # _shot10 lexically and after it in the cut.
    #
    # It is also the state that survives death worst without a home. An agent
    # that has generated five of eight shots and is killed has spent real money;
    # the plan for the remaining three lived in its context and nowhere else, and
    # a successor reading the artifact rows can see WHAT was made and not WHAT
    # WAS NEXT. That is exactly the kill-tax the WORK MANIFEST rule exists to
    # stop, and a jsonl checkpoint is the wrong shape for it because the shot
    # list is edited (a shot is re-generated, re-ordered, cut) rather than
    # appended to.
    #
    # THE PROMPT IS SPLIT INTO action/camera/dialogue RATHER THAN STORED WHOLE.
    # Not decoration: these are the three things a shot list has always carried,
    # they are edited independently — re-framing a shot changes the camera and
    # nothing else — and a re-generation that has to re-derive "the camera part"
    # out of a paragraph by string surgery is how a re-frame silently rewrites
    # the action. cinematic.prompt_for() is the one place they are joined.
    #
    # frames ARE PATHS, NOT URLS. A conditioning frame is a still this project
    # generated and a human approved; it lives in the repo, it is versioned, and
    # it is uploaded to the provider at generation time for a URL that dies in
    # three days (kie.UPLOAD_TTL_DAYS). Storing the minted URL here instead would
    # persist a dead link as if it were the anchor.
    """
    CREATE TABLE cine_sequence (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL UNIQUE,
        logline      TEXT NOT NULL DEFAULT '',
        -- STYLE IS THREE COLUMNS BECAUSE IT IS THREE DIFFERENT THINGS, and the
        -- first draft of this table had one called `style` that nothing ever
        -- read — a sequence could be given a look and every shot was generated
        -- without it.
        --   style      the preset KEY ('anime', 'noir', ...) or '' for free prose
        --   style_note the project's own wording, always applied
        --   style_refs images that carry the look, which beat any prose
        -- They are separate because they are edited separately and because the
        -- refs are paths that have to survive a re-plan, and folded into one
        -- string none of that is recoverable.
        style        TEXT NOT NULL DEFAULT '',
        style_note   TEXT NOT NULL DEFAULT '',
        style_refs_json TEXT NOT NULL DEFAULT '[]',
        -- WHICH MODEL THIS SEQUENCE IS BOUGHT FROM. On the sequence rather than
        -- passed per call, because a cutscene generated half on one model and
        -- half on another does not cut together — the grain, the motion and the
        -- colour response are all different, and the seam lands mid-scene.
        model        TEXT NOT NULL DEFAULT '',
        aspect_ratio TEXT NOT NULL DEFAULT '16:9',
        resolution   TEXT NOT NULL DEFAULT '720p',
        status       TEXT NOT NULL DEFAULT 'planned'
                         CHECK (status IN ('planned','generating','assembled',
                                           'abandoned')),
        assembled_artifact_id INTEGER
                         REFERENCES artifact_revision(id) ON DELETE SET NULL,
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE cine_shot (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        sequence_id  INTEGER NOT NULL
                         REFERENCES cine_sequence(id) ON DELETE CASCADE,
        idx          INTEGER NOT NULL,
        slug         TEXT NOT NULL DEFAULT '',
        action       TEXT NOT NULL,
        camera       TEXT NOT NULL DEFAULT '',
        dialogue     TEXT NOT NULL DEFAULT '',
        duration     INTEGER NOT NULL DEFAULT 5,
        first_frame  TEXT NOT NULL DEFAULT '',
        last_frame   TEXT NOT NULL DEFAULT '',
        refs_json    TEXT NOT NULL DEFAULT '[]',
        artifact_id  INTEGER REFERENCES artifact_revision(id) ON DELETE SET NULL,
        task_id      TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'planned'
                         CHECK (status IN ('planned','generating','generated',
                                           'kept','failed','cut')),
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (sequence_id, idx)
    );
    CREATE INDEX idx_cine_shot_seq ON cine_shot(sequence_id, idx);
    """,
    # 0028 — THE POST-PRODUCTION HALF. 0027 could plan and buy shots; everything
    # that turns a folder of clips into a CUTSCENE was missing, and the gap was
    # not visible from inside the module because each piece looked like somebody
    # else's job.
    #
    # SOUND IS THE ONE THAT WAS ACTIVELY MIS-DESCRIBED. cinematic.py's docstring,
    # the seat brief and the research note all said generated audio is off
    # because "the audio seat scores the cutscene over the top". No path existed
    # for that to happen — no bed, no mix, no mux — so every assembled cut
    # shipped SILENT while three documents described a mechanism. A sentence that
    # reads as a design decision and is actually an unbuilt feature is worse than
    # an admitted gap, because nobody goes looking for it.
    #
    # `audio_track` is a project path, not an artifact id, on purpose: the bed
    # may be a kept Suno track (music.py installs those under
    # game/assets/audio/music/) or a hand-made .wav the audio seat mixed, and
    # those have no id in common. Per-shot `vo` is separate from the bed because
    # dialogue is cut against the picture and music is laid under the whole
    # thing; one column would make it impossible to re-time one without the
    # other.
    #
    # TRANSITIONS ARE PER SHOT because a transition belongs to the JOIN, and the
    # join is a property of the shot that starts it. Stored on the incoming shot
    # (shot N's `transition` is how shot N-1 becomes shot N) so that re-ordering
    # a shot carries its handle with it. Default 'cut' keeps the fast concat path
    # for a sequence that wants none — nothing pays for a filter graph it does
    # not use.
    #
    # THERE IS NO `captions` COLUMN. Caption timing is DERIVED from each shot's
    # duration and the transitions between them, so storing it would be storing
    # a second answer to a question the shot list already answers — and the two
    # would disagree the first time anyone changed a duration.
    """
    ALTER TABLE cine_sequence ADD COLUMN audio_track TEXT NOT NULL DEFAULT '';
    ALTER TABLE cine_sequence ADD COLUMN audio_gain_db REAL NOT NULL DEFAULT 0;
    ALTER TABLE cine_sequence ADD COLUMN fade_in REAL NOT NULL DEFAULT 0;
    ALTER TABLE cine_sequence ADD COLUMN fade_out REAL NOT NULL DEFAULT 0;
    ALTER TABLE cine_shot ADD COLUMN transition TEXT NOT NULL DEFAULT 'cut';
    ALTER TABLE cine_shot ADD COLUMN transition_s REAL NOT NULL DEFAULT 0.5;
    ALTER TABLE cine_shot ADD COLUMN vo TEXT NOT NULL DEFAULT '';
    """,
    # 0029 — THE STORYBOARD. 0027 gave the cinematic seat a shot list and 0028
    # gave it a cut; both start at the point where somebody already knows what
    # the scene IS. Nothing modelled the part before that, so the only place to
    # work out a scene was the shot list itself — where every wrong idea is one
    # click from a paid generation and a re-think means editing rows that a
    # provider task id is already attached to.
    #
    # A BOARD IS SEPARATE FROM A SEQUENCE BECAUSE PLANNING IS FREE AND SHOTS ARE
    # NOT. That is the whole reason for two tables rather than more columns on
    # cine_sequence. A board is the place you are allowed to be wrong: reorder
    # it, throw half of it out, generate six versions of frame 3, and none of it
    # bills. Promotion to a cine_sequence is the moment that stops being true,
    # and it should be a moment you can point at rather than a gradient.
    #
    # `sequence_id` IS THE PROMOTION RECORD, nullable until then. It is not a
    # parent link — a board is not owned by a sequence, it is what the sequence
    # was argued out of — and it is kept afterwards so that a shot which looks
    # wrong on screen can be read back against the frame it was approved from.
    # ON DELETE SET NULL: abandoning a sequence must not delete the thinking.
    #
    # `cast_refs_json` IS THE COHERENCE LEVER and the reason this table earns
    # its place. It holds ref_pin NAMES (not paths) for the characters and style
    # anchors in this scene, resolved at generation time, so every frame on the
    # board is conditioned on the same cast. Names rather than paths because
    # ref_pin.pin() versions a re-pin into a new file and moves the pointer —
    # storing the path here would silently keep boarding against revision 1 of a
    # character that art has since redrawn twice.
    #
    # `script_json` HOLDS THE PROSE THE BEATS WERE DERIVED FROM. Written once by
    # a model or a human, then read by a human — it is a document, not a query
    # target, and the beats that matter are already normalised into story_frame
    # rows. Storing it whole keeps the reasoning next to the result; parsing it
    # back out on every read would make the prose authoritative over the rows
    # and re-open exactly the string-surgery hole that split cine_shot into
    # action/camera/dialogue in the first place.
    """
    CREATE TABLE story_board (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL UNIQUE,
        premise      TEXT NOT NULL DEFAULT '',
        logline      TEXT NOT NULL DEFAULT '',
        -- Same three-part split as cine_sequence, and for the same reason: the
        -- preset key, the project's own wording, and the images that outvote
        -- both. Carried onto the sequence verbatim at promotion so the look a
        -- board was approved under is the look that gets bought.
        style        TEXT NOT NULL DEFAULT '',
        style_note   TEXT NOT NULL DEFAULT '',
        style_refs_json TEXT NOT NULL DEFAULT '[]',
        cast_refs_json  TEXT NOT NULL DEFAULT '[]',
        script_json  TEXT NOT NULL DEFAULT '{}',
        aspect_ratio TEXT NOT NULL DEFAULT '16:9',
        status       TEXT NOT NULL DEFAULT 'drafting'
                         CHECK (status IN ('drafting','boarded','promoted',
                                           'abandoned')),
        sequence_id  INTEGER REFERENCES cine_sequence(id) ON DELETE SET NULL,
        work_item_id INTEGER REFERENCES work_item(id) ON DELETE SET NULL,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE story_frame (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        board_id     INTEGER NOT NULL
                         REFERENCES story_board(id) ON DELETE CASCADE,
        idx          INTEGER NOT NULL,
        slug         TEXT NOT NULL DEFAULT '',
        -- The beat is what HAPPENS in story terms; action/camera/dialogue are
        -- the shot-list columns it will become at promotion. Kept apart because
        -- a beat survives being re-shot three different ways, and collapsing
        -- them means re-framing a shot edits the story.
        beat         TEXT NOT NULL DEFAULT '',
        action       TEXT NOT NULL DEFAULT '',
        camera       TEXT NOT NULL DEFAULT '',
        dialogue     TEXT NOT NULL DEFAULT '',
        duration     INTEGER NOT NULL DEFAULT 5,
        -- The board image, project-relative, same contract as cine_shot frames:
        -- a path that survives a re-plan, never a provider URL that expires.
        image_path   TEXT NOT NULL DEFAULT '',
        artifact_id  INTEGER REFERENCES artifact_revision(id) ON DELETE SET NULL,
        -- HOW THIS FRAME GOT HERE, and it is not bookkeeping. A frame a human
        -- drew or chose is evidence; a frame the model guessed from the premise
        -- is a suggestion. Approving a paid shot off the second one while
        -- believing it was the first is the mistake this column exists to make
        -- impossible to make quietly.
        source       TEXT NOT NULL DEFAULT 'none'
                         CHECK (source IN ('none','generated','uploaded',
                                           'pinned')),
        refs_json    TEXT NOT NULL DEFAULT '[]',
        prompt       TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'empty'
                         CHECK (status IN ('empty','generating','drafted',
                                           'approved','cut')),
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (board_id, idx)
    );
    CREATE INDEX idx_story_frame_board ON story_frame(board_id, idx);
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


_ALREADY_EXISTS = re.compile(r"already exists", re.I)

# CREATE TABLE / INDEX / VIEW that does not already say IF NOT EXISTS. The
# trailing group(0) ends in whitespace, so the replacement re-attaches cleanly.
_CREATE_STMT = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(?:TABLE|INDEX|VIEW)\s+"
    r"(?!IF\s+NOT\s+EXISTS)",
    re.I,
)


def _apply_sql_step(conn: sqlite3.Connection, sql: str, version: int) -> None:
    """Apply one SQL migration and its version bump AS ONE COMMIT.

    THE BUG THIS EXISTS FOR, found in this repository's own .bgate/game.db:
    user_version said 15 while the `event` table migration 16 creates was
    already there. Every connect() therefore replayed 16, hit 'table event
    already exists', and took the dashboard and every MCP server down — for
    good, since nothing about that state improves on its own. /api/state
    answered 500 on a database that was, in substance, fully migrated.

    It was reachable because ``executescript()`` issues a COMMIT before it runs:
    the CREATE TABLEs landed in their own transaction and ``PRAGMA
    user_version`` was a separate write afterwards. Anything at all in that gap
    — a crash, `bgate panic`, a taskkill, the loser of the concurrent-startup
    race the caller documents — wedged the file permanently.

    SQLite DDL is transactional and so is user_version (it lives in the database
    header and rolls back with everything else), so the two belong in one
    transaction. An interrupted migration now rolls back whole and is simply
    retried on the next connect.

    The retry with IF NOT EXISTS is the repair path for databases already wedged
    by the old code, and it is what makes a lost startup race harmless rather
    than fatal: replaying a step whose objects exist becomes a no-op that
    finishes by recording the version that was missing.
    """
    # ADD COLUMN HAS NO `IF NOT EXISTS`, so the healing below cannot reach it:
    # replaying one raises "duplicate column name", which is not the "already
    # exists" this used to look for, and the whole repair path fell through to a
    # raise. Every ALTER-based migration was therefore replay-UNSAFE while the
    # CREATE-based ones were safe — a difference nothing declared and nothing
    # tested until a migration that only added columns was written.
    #
    # Filtering by the live schema rather than by matching an error string,
    # because the check is exact and it also keeps the statement out of the
    # transaction entirely instead of relying on rollback.
    sql = _skip_existing_columns(conn, sql)
    script = f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        conn.executescript(script)
        return
    except sqlite3.OperationalError as exc:
        if not _ALREADY_EXISTS.search(str(exc)):
            raise
        conn.rollback()
        collided = exc

    healed = _CREATE_STMT.sub(lambda m: m.group(0) + "IF NOT EXISTS ", sql)
    if healed == sql:
        # 'already exists' from something this does not know how to skip — a
        # TRIGGER, say. Raise the original rather than guessing: a bare `raise`
        # out here is past the handler and would surface as RuntimeError, which
        # would bury the one line saying what actually collided.
        raise collided
    conn.executescript(
        f"BEGIN;\n{healed}\nPRAGMA user_version = {version};\nCOMMIT;")
    print(f"bgate: migration {version} was already applied but unrecorded — "
          f"user_version repaired", file=sys.stderr)


_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+(?:COLUMN\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\b", re.I)


def _skip_existing_columns(conn: sqlite3.Connection, sql: str) -> str:
    """Drop ADD COLUMN statements whose column is already on the table.

    The ADD COLUMN equivalent of the `IF NOT EXISTS` rewrite below — SQLite has
    no such clause for it, so the idempotence has to be established by looking.
    Anything that is not an ADD COLUMN passes through untouched.
    """
    out = []
    for statement in sql.split(";"):
        match = _ADD_COLUMN.match(statement)
        if not match:
            out.append(statement)
            continue
        table, column = match.group(1), match.group(2)
        try:
            existing = {str(r[1]) for r in
                        conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            out.append(statement)          # unknown table: let it raise properly
            continue
        if column not in existing:
            out.append(statement)
    return ";".join(out)


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations.

    Every process that opens the DB runs this. Two starting at once both read
    user_version=N and both replay migration N+1 — the BEGIN EXCLUSIVE below
    does NOT prevent that, because the pending list has to be committed before
    the loop runs (the callable steps manage their own transactions, and the
    12-step table rebuild needs PRAGMA foreign_keys OFF, which SQLite refuses
    inside a transaction). What makes the race survivable is that each SQL step
    is now atomic and replay-safe: see _apply_sql_step.
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
            # A callable cannot be folded into one commit for the reason above,
            # so those are written to be re-runnable instead.
            step(conn)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.commit()
        else:
            _apply_sql_step(conn, step, i)


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
