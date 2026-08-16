"""Per-task anchored references, layered on top of the global project refs.

The global set (bgate_core.refs / ref_pin) is the project's identity anchors.
task_ref lets the user pin extra references to ONE work item — a specific pose,
a scene-specific palette, the exact frame a variant must match. When the art
seat works that item, resolve_for_task returns the task's anchors FIRST, then
the global pins, so the task-specific refs take priority without discarding the
project identity.
"""
from __future__ import annotations

import os
from typing import Optional

from . import activity, db, refs
from .util import rows

KINDS = refs.KINDS  # ('character','style','ui','concept')


def add(root: str | os.PathLike[str], work_item_id: int, ref: str, *,
        kind: str = "style", note: str = "", rank: int = 0) -> dict:
    """Anchor a reference (a global pin name OR a project-relative path) to a
    work item. Validates that it resolves to a real image now, so a task never
    carries a dangling anchor."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    refs.resolve(root, ref)  # raises LookupError if it points at nothing
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO task_ref (work_item_id, ref, kind, note, rank) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(work_item_id, ref) DO UPDATE SET "
            "kind=excluded.kind, note=excluded.note, rank=excluded.rank",
            (work_item_id, ref, kind, note, rank))
    activity.log(root, "ref", f"anchored '{ref}' to work item {work_item_id}",
                 ref=str(work_item_id))
    return {"ok": True, "work_item_id": work_item_id, "ref": ref, "kind": kind}


def remove(root: str | os.PathLike[str], work_item_id: int, ref: str) -> dict:
    with db.tx(root) as conn:
        cur = conn.execute(
            "DELETE FROM task_ref WHERE work_item_id = ? AND ref = ?",
            (work_item_id, ref))
        removed = cur.rowcount
    return {"ok": True, "removed": removed, "ref": ref}


def list_for_task(root: str | os.PathLike[str], work_item_id: int) -> list[dict]:
    """The task's anchored refs, each with its resolved path + existence flag."""
    out = rows(db.connect(root).execute(
        "SELECT id, work_item_id, ref, kind, note, rank, created_at "
        "FROM task_ref WHERE work_item_id = ? ORDER BY rank, id",
        (work_item_id,)))
    for r in out:
        try:
            r["resolved_path"] = refs.resolve(root, r["ref"])
            r["exists"] = os.path.isfile(r["resolved_path"])
        except LookupError:
            r["resolved_path"] = None
            r["exists"] = False
    return out


def resolve_for_task(root: str | os.PathLike[str], work_item_id: Optional[int],
                     *, kind: Optional[str] = None) -> list[dict]:
    """The LAYERED reference set an art task should condition on: the task's own
    anchors first (highest priority), then the global pins that still belong.
    Each entry: {ref, kind, note, path, scope: 'task'|'global'}.

    A TASK WITH ANCHORS DOES NOT INHERIT EVERY PIN. It used to: task anchors
    first, then EVERY global pin not already covered — so a UI-bar task
    conditioned on every character, concept and second-character pin in the
    project, and the irrelevant identities bled into the output. When the task
    has anchors, they ARE its identity set, and the only globals still added
    are the STYLE pins — the project's look applies to everything. A task with
    no anchors keeps the old behaviour (the global pins are all there is), and
    an explicit ``kind`` was always the caller narrowing for itself.
    """
    seen: set[str] = set()
    layered: list[dict] = []
    if work_item_id is not None:
        for r in list_for_task(root, work_item_id):
            if kind and r["kind"] != kind:
                continue
            if not r.get("resolved_path"):
                continue
            seen.add(r["resolved_path"])
            layered.append({"ref": r["ref"], "kind": r["kind"], "note": r["note"],
                            "path": r["resolved_path"], "scope": "task"})
    anchored = bool(layered)
    for g in refs.list_refs(root, kind=kind):
        if anchored and not kind and g.get("kind") != "style":
            continue
        path = g.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        layered.append({"ref": g["name"], "kind": g["kind"], "note": g.get("note", ""),
                        "path": path, "scope": "global"})
    return layered
