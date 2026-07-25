"""The activity ledger — what the dashboard's ticker reads — and WHO acted.

log() follows the fail-safe rule: it is called from inside other operations and
must NEVER let a telemetry failure break the real work. Any exception is
swallowed; a missing ledger entry is a cosmetic loss, a failed lock is not.

The actor helpers live here rather than in bgate_ui.api because the core has to
answer "is this an agent?" with no web layer loaded (MCP tools, the hook, and
the CLI all ask). bgate_ui.api is still the authority when it is importable, so
the dashboard and the core never disagree about who is calling.
"""
from __future__ import annotations

import os
from typing import Optional

from . import db
from .util import rows

AGENT_PREFIX = "agent:"


def current_actor() -> str:
    """Who is responsible for this call.

    A dispatched agent carries BGATE_ACTOR=agent:item-<id> in its environment;
    anything else is the human at the machine. This is what makes 'approved'
    mean something — see :func:`bgate_core.artifacts.review`.
    """
    try:  # the dashboard's identity, when the web layer is available
        from bgate_ui import api

        return api.current_actor()
    except Exception:
        pass
    env = os.environ.get("BGATE_ACTOR", "").strip()
    if env:
        return env[:120]
    return _local_identity()


def _local_identity() -> str:
    """Fallback for a core-only process (MCP, hook, CLI) — mirrors
    bgate_ui.api.local_identity so both name the same human."""
    configured = os.environ.get("BGATE_STUDIO_USER", "").strip()
    if configured:
        return configured[:120]
    import getpass
    import socket

    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    try:
        host = socket.gethostname()
    except Exception:
        host = "local"
    return f"{user}@{host}"[:120]


def is_agent(actor: str) -> bool:
    return bool(actor) and actor.startswith(AGENT_PREFIX)


def is_human(actor: str) -> bool:
    """An agent may propose; only a human may approve."""
    return bool(actor) and not actor.startswith(AGENT_PREFIX)


def log(root: str | os.PathLike[str], kind: str, summary: str, *,
        seat: str = "", ref: str = "", actor: Optional[str] = None) -> None:
    try:
        who = actor if actor is not None else current_actor()
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO activity (seat, kind, summary, ref, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (seat or "", kind, summary[:400], ref[:200], (who or "")[:120]),
            )
    except Exception:
        pass  # see module docstring


def recent(root: str | os.PathLike[str], limit: int = 50,
           seat: Optional[str] = None, after_id: int = 0) -> list[dict]:
    conn = db.connect(root)
    sql, params = "SELECT * FROM activity WHERE id > ?", [after_id]
    if seat:
        sql += " AND seat = ?"
        params.append(seat)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    # Rows written before 0011 have no actor; the ticker still expects the key.
    return [{**row, "actor": row.get("actor") or ""}
            for row in rows(conn.execute(sql, params))]
