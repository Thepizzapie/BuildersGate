"""Immutable generated-artifact revisions and their review state.

The asset registry answers "did this path drift?" Artifact revisions answer the
iteration questions: what produced this candidate, what was approved, and which
older candidate it superseded. Paths may be replaced; revision rows never are.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import activity, assets, db, iterations
from .util import rows

STATUSES = ("candidate", "approved", "rejected", "integrated", "superseded")


def register(root: str | os.PathLike[str], logical_name: str,
             path: str | os.PathLike[str], *, producer: str = "",
             model: str = "", prompt: str = "", refs: Optional[list[str]] = None,
             metadata: Optional[dict] = None,
             work_item_id: Optional[int] = None) -> dict:
    """Record a new immutable candidate revision for an existing output file."""
    name = logical_name.strip()
    if not name:
        raise ValueError("an artifact needs a logical name")
    rel = assets.normalize_path(root, path)
    absolute = Path(root) / rel
    if not absolute.is_file():
        raise FileNotFoundError(f"nothing on disk at {rel}")

    tracked = assets.track(root, rel)
    digest = tracked["hash"]
    size = tracked["bytes"]
    iteration_id = iterations.active_id(root)
    with db.tx(root) as conn:
        revision = int(conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM artifact_revision "
            "WHERE logical_name = ?", (name,)).fetchone()[0])
        cur = conn.execute(
            """
            INSERT INTO artifact_revision (
                logical_name, revision, path, kind, hash, bytes, producer,
                model, prompt, refs_json, metadata_json, work_item_id, iteration_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, revision, rel, assets.kind_of(rel), digest, size,
             producer.strip(), model.strip(), prompt,
             json.dumps(refs or []), json.dumps(metadata or {}),
             work_item_id, iteration_id),
        )
        artifact_id = int(cur.lastrowid)
    activity.log(root, "artifact",
                 f"candidate {name} r{revision} ({producer or 'unknown'})",
                 ref=str(artifact_id))
    if iteration_id:
        iterations.add_event(
            root, iteration_id, "asset_revision", "artifact", str(artifact_id),
            f"Created {name} r{revision}", {"path": rel, "producer": producer})
    return get(root, artifact_id)


def get(root: str | os.PathLike[str], artifact_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM artifact_revision WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise LookupError(f"no artifact revision {artifact_id}")
    return _decode(dict(row))


def list_revisions(root: str | os.PathLike[str], *,
                   logical_name: Optional[str] = None,
                   status: Optional[str] = None,
                   limit: int = 100) -> list[dict]:
    conn = db.connect(root)
    sql, params = "SELECT * FROM artifact_revision WHERE 1=1", []
    if logical_name:
        sql += " AND logical_name = ?"
        params.append(logical_name)
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [_decode(row) for row in rows(conn.execute(sql, params))]


def review(root: str | os.PathLike[str], artifact_id: int, status: str,
           note: str = "") -> dict:
    """Approve/reject/integrate a candidate and preserve the decision as case law."""
    if status not in STATUSES[1:]:
        raise ValueError(f"review status must be one of {STATUSES[1:]}")
    artifact = get(root, artifact_id)
    with db.tx(root) as conn:
        if status in ("approved", "integrated"):
            conn.execute(
                "UPDATE artifact_revision SET status = 'superseded', "
                "reviewed_at = COALESCE(reviewed_at, datetime('now')) "
                "WHERE logical_name = ? AND id <> ? "
                "AND status IN ('approved','integrated')",
                (artifact["logical_name"], artifact_id),
            )
        conn.execute(
            "UPDATE artifact_revision SET status = ?, review_note = ?, "
            "reviewed_at = datetime('now') WHERE id = ?",
            (status, note.strip()[:1000], artifact_id),
        )
    activity.log(root, "artifact_review",
                 f"{artifact['logical_name']} r{artifact['revision']} -> {status}",
                 ref=str(artifact_id))
    iteration_id = artifact.get("iteration_id") or iterations.active_id(root)
    if iteration_id:
        iterations.add_event(
            root, int(iteration_id), "asset_decision", "artifact", str(artifact_id),
            f"{artifact['logical_name']} r{artifact['revision']} -> {status}",
            {"status": status, "reason": note.strip()})
    return get(root, artifact_id)


def workspace(root: str | os.PathLike[str]) -> list[dict]:
    """Logical assets with every revision and the state needed to review them."""
    conn = db.connect(root)
    revisions = list_revisions(root, limit=500)
    tracked = {item["path"]: item for item in assets.list_assets(root)}
    latest_iteration = conn.execute(
        "SELECT active_artifact_ids_json FROM iteration ORDER BY id DESC LIMIT 1"
    ).fetchone()
    used_ids: set[int] = set()
    if latest_iteration:
        try:
            used_ids = {int(value) for value in
                        json.loads(latest_iteration["active_artifact_ids_json"])}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    groups: dict[str, dict] = {}
    for revision in revisions:
        asset = tracked.get(revision["path"], {})
        work = None
        if revision.get("work_item_id"):
            row = conn.execute(
                "SELECT id, seat, title, status, result, updated_at "
                "FROM work_item WHERE id = ?", (revision["work_item_id"],)).fetchone()
            work = dict(row) if row else None
        revision.update({
            "profile": revision["metadata"].get("profile", ""),
            "consistency": revision["metadata"].get("consistency", {}),
            "engine_import": revision["metadata"].get("engine_import", {}),
            "used_in_current_build": int(revision["id"]) in used_ids,
            "lock": {
                "seat": asset.get("lock_seat"),
                "owner": asset.get("lock_owner", ""),
                "work_item_id": asset.get("work_item_id"),
                "heartbeat_at": asset.get("heartbeat_at"),
                "lease_expires_at": asset.get("lease_expires_at"),
            } if asset else None,
            "work_item": work,
        })
        group = groups.setdefault(revision["logical_name"], {
            "logical_name": revision["logical_name"],
            "approved": None,
            "candidates": [],
            "revisions": [],
            "feedback": [],
        })
        group["revisions"].append(revision)
        if revision["status"] in ("approved", "integrated") and group["approved"] is None:
            group["approved"] = revision
        if revision["status"] == "candidate":
            group["candidates"].append(revision)

    for logical_name, group in groups.items():
        group["feedback"] = rows(conn.execute(
            "SELECT i.id, i.session_id, i.t, i.kind, i.text, i.seat, i.status, "
            "l.confidence FROM playtest_item_asset l "
            "JOIN playtest_item i ON i.id = l.item_id "
            "WHERE l.logical_name = ? ORDER BY i.id DESC",
            (logical_name,)))
    return sorted(groups.values(), key=lambda item: item["logical_name"].lower())


def regenerate(root: str | os.PathLike[str], artifact_id: int,
               reason: str = "") -> dict:
    """Queue a new revision using the exact provenance of an existing one."""
    from . import queue

    artifact = get(root, artifact_id)
    brief = (
        f"Regenerate {artifact['logical_name']} from revision "
        f"{artifact['revision']}. Original producer={artifact['producer'] or 'unknown'}, "
        f"model={artifact['model'] or 'unknown'}, prompt={artifact['prompt']!r}, "
        f"refs={artifact['refs']}. Review request: {reason or 'produce a stronger candidate'}. "
        "Register the result as a new immutable artifact revision."
    )
    return queue.add(
        root, "art", f"Regenerate {artifact['logical_name']}",
        brief=brief, priority=2, source="artifact",
        source_ref=str(artifact_id))


def link_feedback(root: str | os.PathLike[str], artifact_id: int,
                  item_id: int, confidence: float = 1.0) -> dict:
    artifact = get(root, artifact_id)
    with db.tx(root) as conn:
        exists = conn.execute(
            "SELECT 1 FROM playtest_item WHERE id = ?", (item_id,)).fetchone()
        if not exists:
            raise LookupError(f"no playtest item {item_id}")
        conn.execute(
            "INSERT INTO playtest_item_asset (item_id, logical_name, confidence) "
            "VALUES (?, ?, ?) ON CONFLICT(item_id, logical_name) DO UPDATE SET "
            "confidence = excluded.confidence",
            (item_id, artifact["logical_name"], max(0.0, min(float(confidence), 1.0))))
    return {"artifact_id": artifact_id, "logical_name": artifact["logical_name"],
            "item_id": item_id}


def record_check(root: str | os.PathLike[str], path: str | os.PathLike[str],
                 key: str, result: dict) -> Optional[dict]:
    """Attach consistency/import evidence to the newest revision for a path."""
    rel = assets.normalize_path(root, path)
    row = db.connect(root).execute(
        "SELECT * FROM artifact_revision WHERE path = ? "
        "ORDER BY revision DESC LIMIT 1", (rel,)).fetchone()
    if row is None:
        return None
    artifact = _decode(dict(row))
    metadata = artifact["metadata"]
    metadata[key] = result
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE artifact_revision SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), artifact["id"]))
    return get(root, int(artifact["id"]))


def _decode(row: dict) -> dict:
    for source, target, fallback in (
        ("refs_json", "refs", []),
        ("metadata_json", "metadata", {}),
    ):
        try:
            row[target] = json.loads(row.pop(source))
        except (TypeError, json.JSONDecodeError):
            row.pop(source, None)
            row[target] = fallback
    return row
