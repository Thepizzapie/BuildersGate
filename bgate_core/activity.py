"""The activity ledger — what the dashboard's ticker reads.

log() follows the fail-safe rule: it is called from inside other operations and
must NEVER let a telemetry failure break the real work. Any exception is
swallowed; a missing ledger entry is a cosmetic loss, a failed lock is not.
"""
from __future__ import annotations

import os
from typing import Optional

from . import db
from .util import rows


def log(root: str | os.PathLike[str], kind: str, summary: str, *,
        seat: str = "", ref: str = "") -> None:
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO activity (seat, kind, summary, ref) VALUES (?, ?, ?, ?)",
                (seat or "", kind, summary[:400], ref[:200]),
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
    return rows(conn.execute(sql, params))
