"""A tiny per-seat JSON document store.

The seat workspaces that hold free-form user state — narrative storyboards, the
art flow map, the qa bot roster, sound cue sheets — each persist a single JSON
blob keyed by (seat, key). One table, no schema churn per feature.

WRITES ARE VERSIONED. These docs are whole-document read-modify-writes, so two
tabs (or a tab and an agent) each saved the copy they loaded and the second one
silently erased the first — an afternoon of storyboarding gone with no error
anywhere. Every doc now carries a version under the reserved key ``_version``:
``get`` puts it in, ``set`` takes it out and refuses the write if the stored
document has moved on (:class:`StaleWrite`).

The version rides INSIDE the document deliberately: every existing caller
already round-trips the whole blob, so the precondition travels end to end
without a new column, a new endpoint, or a client that knows about any of this.
A caller that strips ``_version`` gets the old last-write-wins behaviour — which
is why the field is injected on read rather than left to be remembered.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from . import db
from .util import rows

VERSION_KEY = "_version"


class StaleWrite(ValueError):
    """The document changed since the caller read it. Refuse, do not merge.

    Carries both versions so a UI can say "someone else saved at 15:04" and
    offer the choice, instead of the caller discovering the loss days later.
    """

    def __init__(self, seat: str, key: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{seat}/{key} changed since you loaded it (you had {expected or 'nothing'}, "
            f"stored is {actual or 'nothing'}) — reload and reapply your edit; "
            "saving would erase the other write")
        self.seat, self.key = seat, key
        self.expected, self.actual = expected, actual


def _version_of(payload: str) -> str:
    """Content hash. Cheap, needs no column, and two writes in the same second
    (which `updated_at`'s one-second resolution cannot tell apart) differ."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _stored(conn, seat: str, key: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT data_json, updated_at FROM workspace_doc WHERE seat = ? AND key = ?",
        (seat, key)).fetchone()
    if row is None:
        return "", ""
    return row["data_json"], row["updated_at"]


def get(root: str | os.PathLike[str], seat: str, key: str,
        default: Optional[dict] = None) -> dict:
    """The document, with its ``_version`` stamped in so a later set() can check."""
    payload, _updated = _stored(db.connect(root), seat, key)
    if not payload:
        return dict(default) if default is not None else {}
    try:
        data = json.loads(payload)
    except ValueError:
        return dict(default) if default is not None else {}
    if isinstance(data, dict):
        data[VERSION_KEY] = _version_of(payload)
    return data


def version(root: str | os.PathLike[str], seat: str, key: str) -> str:
    """The current version of a doc ('' when it does not exist yet)."""
    payload, _updated = _stored(db.connect(root), seat, key)
    return _version_of(payload) if payload else ""


def set(root: str | os.PathLike[str], seat: str, key: str, data: dict, *,
        if_version: Optional[str] = None) -> dict:
    """Store the document. Raises :class:`StaleWrite` on a lost update.

    The precondition comes from ``if_version`` or, failing that, from the
    reserved ``_version`` key inside ``data`` (what get() put there). It is
    stripped before storing — the version is metadata, never content.
    """
    payload_data = dict(data) if isinstance(data, dict) else data
    embedded = ""
    if isinstance(payload_data, dict):
        embedded = str(payload_data.pop(VERSION_KEY, "") or "")
    expected = if_version if if_version is not None else embedded
    payload = json.dumps(payload_data)

    with db.tx(root) as conn:
        # BEGIN IMMEDIATE takes the write lock before the compare, so two
        # concurrent savers serialise instead of both reading the same "before".
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        current, _updated = _stored(conn, seat, key)
        actual = _version_of(current) if current else ""
        if expected and expected != actual:
            raise StaleWrite(seat, key, expected, actual)
        conn.execute(
            "INSERT INTO workspace_doc (seat, key, data_json, updated_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(seat, key) DO UPDATE SET "
            "data_json = excluded.data_json, updated_at = datetime('now')",
            (seat, key, payload))
    return {"ok": True, "seat": seat, "key": key,
            VERSION_KEY: _version_of(payload)}


def list_keys(root: str | os.PathLike[str], seat: str) -> list[dict]:
    return rows(db.connect(root).execute(
        "SELECT seat, key, updated_at FROM workspace_doc WHERE seat = ? "
        "ORDER BY updated_at DESC", (seat,)))
