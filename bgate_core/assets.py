"""Asset registry — content hashes and locks for the files git can't merge.

Two agents editing one .blend is the failure mode this module exists for. Text
merges; binary doesn't. A conflicted .tscn is an afternoon; a conflicted .blend
is a lost asset. So binaries LOCK, they never merge:

  * track()   — register a file under a content hash (sha256)
  * lock()    — claim a path for one seat; anyone else's lock attempt fails
  * verify()  — compare disk against the registry; catches silent clobbers
  * release() — free the lock, re-hash, record the new content

The registry is advisory at this layer — enforcement (blocking a write tool on a
locked path) belongs to the seat/hook layer, same as Orbit's PreToolUse lanes.
But verify() makes violations VISIBLE even without enforcement: a changed hash
with no lock held names the file that was stomped and when.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import activity, db
from .util import rows

# Kinds are advisory labels for humans/agents, inferred from suffix.
_SUFFIX_KINDS = {
    ".blend": "blender", ".glb": "model", ".gltf": "model", ".fbx": "model",
    ".png": "texture", ".jpg": "texture", ".jpeg": "texture", ".webp": "texture",
    ".svg": "vector", ".wav": "audio", ".ogg": "audio", ".mp3": "audio",
    ".tscn": "scene", ".tres": "resource", ".gd": "script",
}

_CHUNK = 1 << 20  # 1 MiB


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_path(root: str | os.PathLike[str],
                   path: str | os.PathLike[str]) -> str:
    """Registry key: repo-root-relative, forward slashes — stable across OSes."""
    project = Path(root).resolve()
    supplied = Path(path)
    absolute = supplied.resolve() if supplied.is_absolute() else (project / supplied).resolve()
    try:
        relative = absolute.relative_to(project)
    except ValueError as exc:
        raise ValueError(f"{path} is outside the project root {root}") from exc
    return str(relative).replace("\\", "/")


_norm = normalize_path


def file_hash(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def kind_of(path: str | os.PathLike[str]) -> str:
    return _SUFFIX_KINDS.get(Path(path).suffix.lower(), "unknown")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def track(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> dict:
    """Register (or refresh) a file under its content hash."""
    rel = _norm(root, path)
    abspath = Path(root) / rel
    if not abspath.exists():
        raise FileNotFoundError(f"nothing on disk at {rel}")

    digest = file_hash(abspath)
    size = abspath.stat().st_size
    with db.tx(root) as conn:
        conn.execute(
            """
            INSERT INTO asset (path, kind, hash, bytes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (path) DO UPDATE SET
                kind = excluded.kind, hash = excluded.hash,
                bytes = excluded.bytes, updated_at = excluded.updated_at
            """,
            (rel, kind_of(rel), digest, size, _now()),
        )
    return get(root, rel)


def get(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> dict:
    rel = _norm(root, path)
    row = db.connect(root).execute(
        "SELECT * FROM asset WHERE path = ?", (rel,)).fetchone()
    if row is None:
        raise LookupError(f"asset not tracked: {rel}")
    return dict(row)


def list_assets(root: str | os.PathLike[str], kind: Optional[str] = None,
                locked_only: bool = False) -> list[dict]:
    conn = db.connect(root)
    sql, params = "SELECT * FROM asset WHERE 1=1", []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if locked_only:
        sql += " AND lock_seat IS NOT NULL"
    return rows(conn.execute(sql + " ORDER BY path", params))


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def lock(root: str | os.PathLike[str], path: str | os.PathLike[str],
         seat: str, owner: str = "", work_item_id: Optional[int] = None,
         lease_s: int = 300) -> dict:
    """Claim a path for one seat. Held locks fail loudly, not queue silently.

    Locking is idempotent for the same seat AND execution owner. A second work
    item in the same seat still conflicts: stable role identity must not let two
    art workers edit one binary concurrently.
    """
    if not seat or not seat.strip():
        raise ValueError("a lock needs a seat name")
    seat = seat.strip()
    owner = owner.strip()
    rel = _norm(root, path)
    heartbeat = _now()
    lease_expires = (
        datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_s)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    if work_item_id is None and owner.startswith("item-") and owner[5:].isdigit():
        candidate_id = int(owner[5:])
        if db.connect(root).execute(
                "SELECT 1 FROM work_item WHERE id = ?", (candidate_id,)).fetchone():
            work_item_id = candidate_id

    with db.tx(root) as conn:
        row = conn.execute("SELECT * FROM asset WHERE path = ?", (rel,)).fetchone()
        if row is None:
            # Lock-before-create is the normal flow: claim the path, then write.
            conn.execute(
                "INSERT INTO asset "
                "(path, kind, lock_seat, lock_owner, lock_at, updated_at, "
                "work_item_id, heartbeat_at, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rel, kind_of(rel), seat, owner, heartbeat, heartbeat,
                 work_item_id, heartbeat, lease_expires),
            )
        else:
            holder = row["lock_seat"]
            held_owner = row["lock_owner"] or ""
            if holder and (holder != seat or (held_owner and held_owner != owner)):
                raise RuntimeError(
                    f"{rel} is locked by seat {holder!r}"
                    + (f" ({held_owner})" if held_owner else "")
                    + f" since {row['lock_at']} — "
                    "binary assets don't merge; wait for release or re-plan"
                )
            conn.execute(
                "UPDATE asset SET lock_seat = ?, lock_owner = ?, lock_at = ?, "
                "work_item_id = ?, heartbeat_at = ?, lease_expires_at = ? "
                "WHERE path = ?",
                (seat, owner, heartbeat, work_item_id, heartbeat,
                 lease_expires, rel),
            )
    activity.log(root, "lock", f"locked {rel}", seat=seat, ref=rel)
    return get(root, rel)


def release(root: str | os.PathLike[str], path: str | os.PathLike[str],
            seat: str, owner: str = "") -> dict:
    """Free a lock and record what the file became.

    Only the holder can release. Re-hashing on release is the point: the registry
    entry always reflects the content as of the last legitimate edit, which is
    what verify() measures drift against.
    """
    rel = _norm(root, path)
    entry = get(root, rel)
    holder = entry["lock_seat"]
    if holder is None:
        return entry  # releasing an unlocked path is a no-op, not an error
    if holder != seat.strip():
        raise RuntimeError(
            f"{rel} is locked by seat {holder!r}; seat {seat!r} cannot release it")
    held_owner = entry["lock_owner"] or ""
    if held_owner and held_owner != owner.strip():
        raise RuntimeError(
            f"{rel} is owned by execution {held_owner!r}; "
            f"execution {owner.strip()!r} cannot release it")

    abspath = Path(root) / rel
    digest = file_hash(abspath) if abspath.exists() else ""
    size = abspath.stat().st_size if abspath.exists() else 0
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE asset SET lock_seat = NULL, lock_owner = '', lock_at = NULL, hash = ?, "
            "bytes = ?, updated_at = ?, work_item_id = NULL, heartbeat_at = NULL, "
            "lease_expires_at = NULL WHERE path = ?",
            (digest, size, _now(), rel),
        )
    activity.log(root, "release", f"released {rel} ({size:,} bytes)",
                 seat=seat.strip(), ref=rel)
    return get(root, rel)


def force_release(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> dict:
    """Break a lock regardless of holder — for dead agents. A human's call."""
    rel = _norm(root, path)
    get(root, rel)  # raise if untracked
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE asset SET lock_seat = NULL, lock_owner = '', lock_at = NULL, "
            "updated_at = ?, work_item_id = NULL, heartbeat_at = NULL, "
            "lease_expires_at = NULL "
            "WHERE path = ?", (_now(), rel))
    activity.log(root, "force_release", f"lock on {rel} broken by hand", ref=rel)
    return get(root, rel)


def heartbeat(root: str | os.PathLike[str], owner: str,
              lease_s: int = 300) -> dict:
    """Refresh every asset lease held by one dispatched execution."""
    owner = owner.strip()
    if not owner:
        raise ValueError("heartbeat needs an execution owner")
    now = _now()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_s)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE asset SET heartbeat_at = ?, lease_expires_at = ? "
            "WHERE lock_owner = ? AND lock_seat IS NOT NULL",
            (now, expires, owner))
    return {"owner": owner, "refreshed": cur.rowcount,
            "heartbeat_at": now, "lease_expires_at": expires}


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------
def verify(root: str | os.PathLike[str]) -> dict:
    """Compare every tracked asset against disk. Names what changed and how.

    States:
      clean      — hash matches the registry
      locked     — held by a seat; changes are expected, not drift
      modified   — content changed with NO lock held: someone stomped it
      missing    — tracked but gone from disk
      untracked_hash — registered by lock() but never written/released
    """
    clean, locked, modified, missing, pending = [], [], [], [], []
    for entry in list_assets(root):
        abspath = Path(root) / entry["path"]
        if entry["lock_seat"]:
            locked.append({"path": entry["path"], "seat": entry["lock_seat"],
                           "owner": entry["lock_owner"] or "",
                           "work_item_id": entry["work_item_id"],
                           "since": entry["lock_at"],
                           "heartbeat_at": entry["heartbeat_at"],
                           "lease_expires_at": entry["lease_expires_at"]})
            continue
        if not abspath.exists():
            missing.append(entry["path"])
            continue
        if not entry["hash"]:
            pending.append(entry["path"])
            continue
        if file_hash(abspath) == entry["hash"]:
            clean.append(entry["path"])
        else:
            modified.append({
                "path": entry["path"],
                "registered": entry["updated_at"],
                "detail": "content changed with no lock held — an unlocked write "
                          "or an outside edit; re-track if intentional",
            })

    return {
        "ok": not modified and not missing and not pending,
        "clean": clean,
        "locked": locked,
        "modified": modified,
        "missing": missing,
        "untracked_hash": pending,
        "counts": {"clean": len(clean), "locked": len(locked),
                   "modified": len(modified), "missing": len(missing),
                   "pending": len(pending)},
    }
