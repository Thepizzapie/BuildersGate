"""A tiny per-seat JSON document store.

The seat workspaces that hold free-form user state — narrative storyboards, the
art flow map, the qa bot roster, sound cue sheets — each persist a single JSON
blob keyed by (seat, key). One table, no schema churn per feature.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import db
from .util import rows


def get(root: str | os.PathLike[str], seat: str, key: str,
        default: Optional[dict] = None) -> dict:
    row = db.connect(root).execute(
        "SELECT data_json FROM workspace_doc WHERE seat = ? AND key = ?",
        (seat, key)).fetchone()
    if row is None:
        return default if default is not None else {}
    try:
        return json.loads(row["data_json"])
    except Exception:
        return default if default is not None else {}


def set(root: str | os.PathLike[str], seat: str, key: str, data: dict) -> dict:
    payload = json.dumps(data)
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO workspace_doc (seat, key, data_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(seat, key) DO UPDATE SET "
            "data_json = excluded.data_json, updated_at = datetime('now')",
            (seat, key, payload))
    return {"ok": True, "seat": seat, "key": key}


def list_keys(root: str | os.PathLike[str], seat: str) -> list[dict]:
    return rows(db.connect(root).execute(
        "SELECT seat, key, updated_at FROM workspace_doc WHERE seat = ? "
        "ORDER BY updated_at DESC", (seat,)))
