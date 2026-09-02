"""WHICH RUN A CHANGED FILE BELONGS TO, when several were running at once.

THE MEASURED FAILURE. Auto-commit scoped itself to ``gitwork.touched(root,
base)`` - every path that changed since the closing run's boundary commit -
which on a board with one agent is exactly right and on a board with three is
``git add -A`` with extra steps. From the benchmark projects' own history:

    tactics 0c537c1  "bgate: item #1 [art]"   also carried nine .wav files and
                     nine .synth.json recipes the AUDIO seat delivered under
                     item #2, which was running at the same time.
    tactics 43c94fe  "bgate: item #6 [qa]"    also carried scripts/board_view.gd,
                     scripts/hud.gd and scenes/main.tscn - gameplay code the QA
                     item never touched.

That destroys the one thing a multi-agent board is FOR: knowing who changed
what and why. `git log` on those projects attributes an audio delivery to an
art item and a visual-effect implementation to a QA item.

WHY THE OBVIOUS FIX IS WRONG ON ITS OWN. "Commit what the hook observed" fails
in the other direction, and the same projects prove it: the hook is a
PreToolUse gate, so it sees ``Write``, ``Edit`` and paths it can read off a Bash
command line - and misses everything a program the agent RAN produced. The art
seat's writelog for platform item #1 lists ``art/gen_statics.py`` and not one of
the PNGs that script wrote. The audio seat's writelog for room-3 item #2 lists
seven ``.synth.json`` recipes and not one of the seven ``.wav`` files beside
them. Committing only observed writes would have dropped every deliverable in
both runs.

SO IT IS THREE SOURCES AND A LANE, and each covers the others' blind spot:

  1. the write log      what the hook saw this owner write (bgate_core.store.writelog)
  2. the artifact ledger  what a TOOL produced for this work item
                        (artifact_revision.work_item_id) - this is where those
                        missing .wav files actually are, correctly attributed
  3. engine sidecars    a .import / .uid Godot generates beside a file belongs
                        to whoever wrote the file, not to whoever closes first
  4. seat lanes         for what none of the above claims: a path inside ANOTHER
                        seat's write lane is not this run's to commit, and a
                        path nobody's lane covers is (which keeps the board
                        moving - see the deadlock note in dispatch._auto_commit)

Nothing here raises and nothing here writes. It answers a question; the caller
decides what to do with the answer.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from . import db

# What an engine writes BESIDE a file, which belongs to whoever wrote the file.
# Godot generates `x.png.import` and `x.gd.uid` on the next project scan, and
# that scan is usually triggered by somebody else's godot_check_project - so
# without this rule the sidecars of one seat's asset land in another seat's
# commit, which is one of the exact rows observed in the benchmark history.
SIDECAR_SUFFIXES = (".import", ".uid")


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _with_sidecars(paths: Iterable[str]) -> set[str]:
    """``paths``, plus the engine bookkeeping that hangs off each one."""
    out: set[str] = set()
    for one in paths:
        rel = _norm(one)
        if not rel:
            continue
        out.add(rel)
        for suffix in SIDECAR_SUFFIXES:
            out.add(rel + suffix)
    return out


def _is_sidecar_of(path: str, owned: set[str]) -> bool:
    for suffix in SIDECAR_SUFFIXES:
        if path.endswith(suffix) and path[: -len(suffix)] in owned:
            return True
    return False


def owner_ids(root: str | os.PathLike[str]) -> list[str]:
    """Every execution that has a write log in this project.

    ``item-<id>`` for a dispatched run and ``session-<id>`` for a top-level
    session - the director's own edits are in that second shape, which is why
    a director writing design docs while an agent finishes is not swept into
    the agent's commit.
    """
    from . import writelog

    folder = Path(root) / db.DB_DIRNAME / writelog.DIRNAME
    try:
        return sorted(p.stem for p in folder.glob("*.jsonl"))
    except OSError:
        return []


def _writelog_paths(root, owner: str, since: str = "") -> set[str]:
    """One owner's observed writes, optionally only those since ``since``.

    THE TIME FILTER IS NOT AN OPTIMISATION. Write logs accumulate across an
    item's whole life and a session log across the project's, so without it a
    file the director wrote last week would still read as "somebody else's"
    forever, and a run that legitimately rewrote it could never commit it - the
    tree stays dirty and the board stops, which is the deadlock auto-commit
    exists to prevent. Only work that overlapped THIS run competes with it.

    ``since`` is local wall-clock in writelog's own format, because that is what
    writelog stamps; comparing the two as strings is exact for that format.
    """
    from . import writelog

    out: set[str] = set()
    for entry in writelog.entries(root, owner):
        if since and str(entry.get("t") or "") < since:
            continue
        rel = _norm(entry.get("path"))
        if rel:
            out.add(rel)
    return out


def _artifact_paths(root, *, work_item_id: Optional[int] = None,
                    exclude_item: Optional[int] = None) -> set[str]:
    """Paths a TOOL produced, keyed on the work item it produced them for.

    No time filter here, and deliberately: an artifact row carries the work
    item that paid for it, so "another item's artifact" is unambiguous whenever
    it was made. (It is also stamped in UTC while the write log is stamped
    local, so a shared cutoff would be wrong by the machine's offset.)
    """
    sql = "SELECT path FROM artifact_revision WHERE 1=1"
    args: list = []
    if work_item_id is not None:
        sql += " AND work_item_id = ?"
        args.append(int(work_item_id))
    if exclude_item is not None:
        sql += " AND work_item_id IS NOT NULL AND work_item_id != ?"
        args.append(int(exclude_item))
    try:
        rows = db.connect(root).execute(sql, args).fetchall()
    except Exception:
        return set()
    return {_norm(r["path"]) for r in rows if _norm(r["path"])}


def _lane_owners(root, path: str) -> list[str]:
    from ..board import seats

    try:
        return list(seats.lane_owners(root, path))
    except Exception:
        return []


def attribute(root: str | os.PathLike[str], item_id: int, seat: str,
              candidates: Iterable[str], *, since: str = "") -> dict:
    """Split ``candidates`` into what this run may commit and what it may not.

    ``candidates`` is normally ``gitwork.touched(root, base)`` - everything the
    tree has changed since this run's boundary. Returns::

        {"mine": [...], "left": [{"path": ..., "why": ...}], "reason": ...}

    ``mine`` is safe to commit under this item's name. ``left`` stays
    uncommitted WITH A REASON, because a path silently dropped is the same
    class of lie as a path wrongly swept: the caller logs it, and the next
    dispatch's dirty-tree refusal names it.

    ``since`` is this run's start as local wall clock (writelog's format). Pass
    it whenever it is known; without it every historical write by another owner
    competes with this run and the answer is needlessly conservative.
    """
    scope = {_norm(p) for p in candidates if _norm(p)}
    if not scope:
        return {"mine": [], "left": [], "reason": "nothing changed"}

    mine_declared = _writelog_paths(root, f"item-{int(item_id)}")
    mine_declared |= _artifact_paths(root, work_item_id=int(item_id))
    mine_all = _with_sidecars(mine_declared)

    theirs_declared: set[str] = set()
    for owner in owner_ids(root):
        if owner == f"item-{int(item_id)}":
            continue
        theirs_declared |= _writelog_paths(root, owner, since=since)
    theirs_declared |= _artifact_paths(root, exclude_item=int(item_id))
    theirs_all = _with_sidecars(theirs_declared)

    mine: list[str] = []
    left: list[dict] = []
    for path in sorted(scope):
        claimed_by_me = path in mine_all
        claimed_by_them = path in theirs_all
        if claimed_by_me and not claimed_by_them:
            mine.append(path)
            continue
        if claimed_by_them and not claimed_by_me:
            left.append({"path": path,
                         "why": "another run wrote it while this one was open"})
            continue
        if claimed_by_me and claimed_by_them:
            # Two live writers in one file. The lock and lease gates exist to
            # stop this; when it happens anyway the honest move is to commit
            # neither half under one name.
            left.append({"path": path,
                         "why": "two runs both wrote it - needs a human"})
            continue
        # Nobody's record claims it. Most of these are real and ours: a file a
        # program the agent RAN produced, which no PreToolUse hook can see.
        # The lane is the tiebreak - and a path no lane covers goes to the
        # closing item, because leaving it would stop the whole board over a
        # file that has no other owner.
        if _is_sidecar_of(path, theirs_all):
            left.append({"path": path,
                         "why": "engine sidecar for another run's file"})
            continue
        owners = _lane_owners(root, path)
        # LANES OVERLAP AND THAT IS THE DESIGN: `game/scripts/**` belongs to
        # gameplay AND tech. So the test is membership, not exclusivity - a
        # closing seat that is one of the owners is entitled to its own lane,
        # and only a path it has no claim on at all goes to somebody else.
        if owners and (seat or "") not in owners:
            left.append({"path": path,
                         "why": f"in the {owners[0]!r} seat's lane"})
            continue
        mine.append(path)

    return {"mine": mine, "left": left,
            "reason": (f"{len(mine)} attributable, {len(left)} left for "
                       "another owner" if left else "")}
