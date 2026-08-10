"""Asset registry — content hashes and locks for the files git can't merge.

Two agents editing one .blend is the failure mode this module exists for. Text
merges; binary doesn't. A conflicted .tscn is an afternoon; a conflicted .blend
is a lost asset. So binaries LOCK, they never merge:

  * track()   — register a file under a content hash (sha256)
  * lock()    — claim a path for one seat; anyone else's lock attempt fails
  * verify()  — compare disk against the registry; catches silent clobbers
  * release() — free the lock, re-hash, record the new content

Text paths get the same treatment one notch softer: an ADVISORY path lease (see
the bottom of this module). Two agents in overlapping lanes editing one .gd is
last-write-wins with no warning — the same lost work as a conflicted .blend,
just quieter. A lease says who is in there right now.

The registry is advisory at this layer — enforcement (blocking a write tool on a
locked path) belongs to the seat/hook layer, same as Orbit's PreToolUse lanes.
But verify() makes violations VISIBLE even without enforcement: a changed hash
with no lock held names the file that was stomped and when.

Leases are COMPARED against the clock, not merely written. A lease that is
recorded and heartbeat but never checked is a gate that does not gate: a dead
agent's lock would hold the path forever.
"""
from __future__ import annotations

import hashlib
import os
import time
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
    # VIDEO, AND .ogv IS NOT .ogg. Ogg is a container: the same extension family
    # carries Vorbis audio and Theora video, and the engine treats them as
    # completely different resources — .ogg imports as an AudioStream, .ogv as a
    # VideoStream. Mapping .ogv to "audio" by family resemblance would file every
    # shipped cutscene as a sound effect in the registry, in the audio seat's
    # listings, and in every kind-filtered query downstream.
    #
    # .mp4 is here even though Godot cannot play one (see bgate_core.cinematic):
    # it is what every video model returns, so it is what a candidate revision is
    # before a human keeps it. A generated clip awaiting a decision is a real
    # tracked asset with a real hash; "unknown" would have hidden the whole
    # candidate pool from asset_verify.
    ".mp4": "video", ".ogv": "video", ".webm": "video", ".mov": "video",
}

_CHUNK = 1 << 20  # 1 MiB

# A lease should be as long as the work, not a flat guess. 300s expired under a
# Blender bake (the lock evaporated mid-edit) and outlived a 5-second import by
# five minutes (the next seat waited on nobody). Callers name the operation.
DEFAULT_LEASE_S = 300
OPERATION_LEASE_S = {
    "import": 120, "inspect": 120, "track": 120,
    "edit": 900, "paint": 900, "sprites": 900, "generate": 900,
    "export": 600, "convert": 600,
    "bake": 1800, "render": 1800, "simulate": 1800,
}
_MIN_LEASE_S = 30
_POLL_S = 0.25  # blocking-acquire poll interval


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _in(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=max(_MIN_LEASE_S, int(seconds)))
            ).strftime("%Y-%m-%d %H:%M:%S")


def lease_seconds(operation: str = "", lease_s: Optional[int] = None) -> int:
    """How long this claim should live. Explicit wins; else size it to the job."""
    if lease_s is not None:
        return max(_MIN_LEASE_S, int(lease_s))
    return OPERATION_LEASE_S.get((operation or "").strip().lower(), DEFAULT_LEASE_S)


def normalize_path(root: str | os.PathLike[str],
                   path: str | os.PathLike[str]) -> str:
    """Registry key: repo-root-relative, forward slashes — stable across OSes.

    SEPARATORS ARE NORMALISED BEFORE THE CONTAINMENT CHECK, NOT AFTER, and the
    order is a security property rather than tidiness.

    This used to resolve the path as given, verify it sat inside the project,
    and THEN rewrite backslashes to forward slashes on the way out. On Windows
    that is harmless — both characters are separators, so `resolve()` already
    saw the traversal. On POSIX a backslash is an ORDINARY FILENAME CHARACTER,
    so `..\\..\\outside\\secret.png` is one legal filename, resolves to a
    non-existent child of the project, passes containment — and is then handed
    back as `../../outside/secret.png`, which every caller joins to the root and
    follows straight out of the project.

    MEASURED against the cutscene pipeline, where the consequence is worst: a
    conditioning frame is uploaded to a third-party generation provider, so the
    escaped path is not read locally, it is POSTed off the machine. Nine
    traversal shapes were contained and this one was not.

    Rewriting first means containment is checked against the SAME
    interpretation the caller gets back, which is the only version of this
    function that can be reasoned about.
    """
    project = Path(root).resolve()
    supplied = Path(str(path).replace("\\", "/"))
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
    reap_expired(root)
    conn = db.connect(root)
    sql, params = "SELECT * FROM asset WHERE 1=1", []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if locked_only:
        sql += " AND lock_seat IS NOT NULL"
    return rows(conn.execute(sql + " ORDER BY path", params))


# ---------------------------------------------------------------------------
# Expiry — the clock comparison the audit found missing
# ---------------------------------------------------------------------------
def reap_expired(root: str | os.PathLike[str]) -> list[str]:
    """Drop every claim whose lease ran out. Agents die mid-flight; a lock that
    outlives its holder is indistinguishable from a permanently blocked path.

    Deliberately does NOT re-hash: nobody knows what the dead agent left behind,
    so verify() should keep reporting the file as drifted until a human looks.
    A lock with no lease at all (pre-0011 rows) is left alone — force_release is
    still the way to break those.
    """
    now = _now()
    try:
        with db.tx(root) as conn:
            expired = rows(conn.execute(
                "SELECT path, lock_seat, lock_owner FROM asset "
                "WHERE lock_seat IS NOT NULL AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?", (now,)))
            if expired:
                conn.execute(
                    "UPDATE asset SET lock_seat = NULL, lock_owner = '', "
                    "lock_actor = '', lock_at = NULL, work_item_id = NULL, "
                    "heartbeat_at = NULL, lease_expires_at = NULL "
                    "WHERE lock_seat IS NOT NULL AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at < ?", (now,))
            conn.execute(
                "DELETE FROM path_lease WHERE expires_at IS NOT NULL "
                "AND expires_at < ?", (now,))
    except Exception:
        return []  # a reaping failure must never break the caller's real work
    for row in expired:
        activity.log(root, "lease_expired",
                     f"lease on {row['path']} expired — lock released",
                     seat=row["lock_seat"] or "", ref=row["path"],
                     actor=row["lock_owner"] or "")
    return [row["path"] for row in expired]


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def lock_holder(root: str | os.PathLike[str],
                path: str | os.PathLike[str]) -> Optional[dict]:
    """The seat currently holding ``path``, or None. NEVER RAISES.

    Every writer that is not `lock()` itself needs this same question answered
    before it touches a file — the dashboard's code editor, its scene editor,
    and the scene tools on the MCP surface. They each used to answer it their
    own way or not at all, and "not at all" is how an agent mid-edit and a human
    at the keyboard end up writing the same .tscn a second apart.

    Not raising is the contract, not laziness: a lock lookup that fails is a
    reason not to CLAIM the file is free, never a reason to refuse the write.
    Callers treat None as "nothing known" and proceed; the file's own backup is
    the floor under both of them.
    """
    try:
        rel = _norm(root, path)
    except Exception:
        return None
    try:
        for row in list_assets(root, locked_only=True):
            if str(row.get("path") or "") == rel:
                return row
    except Exception:
        return None
    return None


def lock(root: str | os.PathLike[str], path: str | os.PathLike[str],
         seat: str, owner: str = "", work_item_id: Optional[int] = None,
         lease_s: Optional[int] = None, *, operation: str = "",
         wait_s: float = 0.0, actor: Optional[str] = None) -> dict:
    """Claim a path for one seat. Held locks fail loudly, not queue silently.

    Locking is idempotent for the same seat AND execution owner. A second work
    item in the same seat still conflicts: stable role identity must not let two
    art workers edit one binary concurrently.

    ``wait_s`` turns the failure into a wait: the caller is registered in
    asset_waiter (so 'who is blocked on this .blend' is answerable) and polls
    until the holder releases or its lease expires. ``operation`` sizes the
    lease to the job — 'bake' is not 'import'.
    """
    if not seat or not seat.strip():
        raise ValueError("a lock needs a seat name")
    seat = seat.strip()
    owner = owner.strip()
    rel = _norm(root, path)
    who = actor if actor is not None else activity.current_actor()
    lease = lease_seconds(operation, lease_s)
    if work_item_id is None and owner.startswith("item-") and owner[5:].isdigit():
        candidate_id = int(owner[5:])
        if db.connect(root).execute(
                "SELECT 1 FROM work_item WHERE id = ?", (candidate_id,)).fetchone():
            work_item_id = candidate_id

    deadline = time.monotonic() + max(0.0, float(wait_s))
    waiting = False
    try:
        while True:
            reap_expired(root)
            try:
                _claim(root, rel, seat, owner, who, work_item_id, lease)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                if not waiting:
                    _add_waiter(root, rel, seat, owner)
                    waiting = True
                time.sleep(_POLL_S)
    finally:
        if waiting:
            _drop_waiter(root, rel, seat, owner)
    activity.log(root, "lock", f"locked {rel}", seat=seat, ref=rel, actor=who)
    return get(root, rel)


def _claim(root: str | os.PathLike[str], rel: str, seat: str, owner: str,
           who: str, work_item_id: Optional[int], lease_s: int) -> None:
    """One acquisition attempt. Raises RuntimeError naming the holder."""
    stamp = _now()
    expires = _in(lease_s)
    with db.tx(root) as conn:
        row = conn.execute("SELECT * FROM asset WHERE path = ?", (rel,)).fetchone()
        if row is None:
            # Lock-before-create is the normal flow: claim the path, then write.
            conn.execute(
                "INSERT INTO asset "
                "(path, kind, lock_seat, lock_owner, lock_actor, lock_at, "
                "updated_at, work_item_id, heartbeat_at, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rel, kind_of(rel), seat, owner, who, stamp, stamp,
                 work_item_id, stamp, expires),
            )
            return
        holder = row["lock_seat"]
        held_owner = row["lock_owner"] or ""
        if holder and (holder != seat or (held_owner and held_owner != owner)):
            blocked = waiters(root, rel)
            raise RuntimeError(
                f"{rel} is locked by seat {holder!r}"
                + (f" ({held_owner})" if held_owner else "")
                + f" since {row['lock_at']}, lease until "
                f"{row['lease_expires_at'] or 'forever'} — "
                "binary assets don't merge; wait for release or re-plan"
                + (f" ({len(blocked)} already waiting)" if blocked else "")
            )
        conn.execute(
            "UPDATE asset SET lock_seat = ?, lock_owner = ?, lock_actor = ?, "
            "lock_at = ?, work_item_id = ?, heartbeat_at = ?, "
            "lease_expires_at = ? WHERE path = ?",
            (seat, owner, who, stamp, work_item_id, stamp, expires, rel),
        )


# ---------------------------------------------------------------------------
# Waiters — a blocked agent that nobody can see is a scheduling bug
# ---------------------------------------------------------------------------
def _waiter_key(seat: str, owner: str) -> str:
    return owner or f"seat:{seat}"


def _add_waiter(root: str | os.PathLike[str], rel: str, seat: str,
                owner: str) -> None:
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO asset_waiter (asset_path, seat, owner, since) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (asset_path, owner) "
                "DO UPDATE SET seat = excluded.seat",
                (rel, seat, _waiter_key(seat, owner), _now()))
    except Exception:
        pass  # visibility is a nicety; never fail the acquire over it


def _drop_waiter(root: str | os.PathLike[str], rel: str, seat: str,
                 owner: str) -> None:
    try:
        with db.tx(root) as conn:
            conn.execute(
                "DELETE FROM asset_waiter WHERE asset_path = ? AND owner = ?",
                (rel, _waiter_key(seat, owner)))
    except Exception:
        pass


def waiters(root: str | os.PathLike[str],
            path: Optional[str | os.PathLike[str]] = None) -> list[dict]:
    """Who is blocked, and on what."""
    conn = db.connect(root)
    if path is not None:
        return rows(conn.execute(
            "SELECT * FROM asset_waiter WHERE asset_path = ? ORDER BY since",
            (_norm(root, path),)))
    return rows(conn.execute("SELECT * FROM asset_waiter ORDER BY asset_path, since"))


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
            "UPDATE asset SET lock_seat = NULL, lock_owner = '', lock_actor = '', "
            "lock_at = NULL, hash = ?, "
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
            "UPDATE asset SET lock_seat = NULL, lock_owner = '', lock_actor = '', "
            "lock_at = NULL, updated_at = ?, work_item_id = NULL, "
            "heartbeat_at = NULL, lease_expires_at = NULL "
            "WHERE path = ?", (_now(), rel))
    activity.log(root, "force_release", f"lock on {rel} broken by hand", ref=rel)
    return get(root, rel)


def heartbeat(root: str | os.PathLike[str], owner: str,
              lease_s: Optional[int] = None, *, operation: str = "") -> dict:
    """Refresh every claim held by one dispatched execution — locks and leases.

    The path leases go up with the asset locks: a run that is still alive keeps
    everything it holds, and a run that dies loses all of it at once.
    """
    owner = owner.strip()
    if not owner:
        raise ValueError("heartbeat needs an execution owner")
    now = _now()
    expires = _in(lease_seconds(operation, lease_s))
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE asset SET heartbeat_at = ?, lease_expires_at = ? "
            "WHERE lock_owner = ? AND lock_seat IS NOT NULL",
            (now, expires, owner))
        leases = conn.execute(
            "UPDATE path_lease SET expires_at = ? WHERE owner = ?",
            (expires, owner))
    return {"owner": owner, "refreshed": cur.rowcount,
            "leases_refreshed": leases.rowcount,
            "heartbeat_at": now, "lease_expires_at": expires}


# ---------------------------------------------------------------------------
# Advisory path leases — the text-file half of the problem
# ---------------------------------------------------------------------------
def acquire_path_lease(root: str | os.PathLike[str],
                       path: str | os.PathLike[str], seat: str, owner: str,
                       *, lease_s: Optional[int] = None,
                       operation: str = "edit") -> dict:
    """Claim a text path for one execution for the length of its run.

    Unlike a binary lock this is best-effort and short: the point is not to make
    concurrent edits impossible, it is to make them VISIBLE and to name the item
    that got there first, instead of silently taking the last write.

    Raises RuntimeError naming the holding owner when someone else holds a
    lease that has not expired.
    """
    rel = _norm(root, path)
    seat, owner = seat.strip(), owner.strip()
    if not owner:
        raise ValueError("a path lease needs an execution owner")
    reap_expired(root)
    expires = _in(lease_seconds(operation, lease_s))
    with db.tx(root) as conn:
        row = conn.execute(
            "SELECT * FROM path_lease WHERE path = ?", (rel,)).fetchone()
        if row is not None and (row["owner"] or "") != owner:
            raise RuntimeError(
                f"{rel} is leased by {row['owner']} (seat {row['seat'] or '?'}) "
                f"since {row['acquired_at']} until {row['expires_at'] or 'forever'}"
            )
        conn.execute(
            "INSERT INTO path_lease (path, seat, owner, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (path) DO UPDATE SET "
            "seat = excluded.seat, owner = excluded.owner, "
            "expires_at = excluded.expires_at",
            (rel, seat, owner, _now(), expires))
        row = conn.execute(
            "SELECT * FROM path_lease WHERE path = ?", (rel,)).fetchone()
    return dict(row)


def path_lease_for(root: str | os.PathLike[str],
                   path: str | os.PathLike[str]) -> Optional[dict]:
    """The live lease on a path, or None. Expired leases are not live."""
    reap_expired(root)
    row = db.connect(root).execute(
        "SELECT * FROM path_lease WHERE path = ?", (_norm(root, path),)).fetchone()
    return dict(row) if row else None


def list_path_leases(root: str | os.PathLike[str]) -> list[dict]:
    reap_expired(root)
    return rows(db.connect(root).execute(
        "SELECT * FROM path_lease ORDER BY path"))


def release_path_leases(root: str | os.PathLike[str], owner: str) -> int:
    """Drop everything one execution leased — called when its run ends."""
    owner = owner.strip()
    if not owner:
        return 0
    with db.tx(root) as conn:
        cur = conn.execute("DELETE FROM path_lease WHERE owner = ?", (owner,))
    return int(cur.rowcount or 0)


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
                           "actor": entry["lock_actor"] or "",
                           "waiters": waiters(root, entry["path"]),
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
