"""Every agent running on this machine, and the only button that stops one.

WHY THIS IS NOT /api/agents WITH A WIDER QUERY. That endpoint answers out of
``dispatch._live``, a module-level dict of Popen handles, for the ONE project
the dashboard currently has open. Both halves of that are lossy in the same
direction: an agent spawned before a dashboard restart, an agent spawned by a
second dashboard, and an agent working a game you are not looking at right now
all appeared in nothing at all -- while still editing files and still billing.
``bgate_core.board.agentreg`` writes a row per run into the project's
``agent_runs`` table and reads the open rows across every registered project;
a row outlives the process that wrote it, which is the whole reason a machine-
wide view can exist.

THESE ARE NOT MCP TOOLS AND MUST NEVER BECOME ONE. An agent that can enumerate
and stop agents can stop the QA agent that is checking its own work, or the
peer holding the lock it wants, or every witness to a bad run at once -- and it
would do it through the one control whose entire meaning is that a person
decided. This surface is human-only for the same reason the key-writing panel
in Settings is: capability that ends or funds a run belongs to the operator,
not to the fleet. ``api.require_human`` on both writes is what enforces it,
because BGATE_ACTOR marks a spawned session and nothing else would notice.

WHAT A STOP IS ALLOWED TO ASSUME. Only that a pid was recorded. The process may
belong to this dashboard (then dispatch.stop owns it -- it holds the stdin, the
log handle and the run's own bookkeeping), or to a dashboard that is gone (then
there is a number and nothing else, and proc.kill_pid_tree is the whole tool).
Both paths kill the TREE, never the parent alone: a runner spawns MCP servers
of its own and terminating the top of that leaves them holding pipes.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request

from bgate_core.board import agentreg as _agentreg
from bgate_core.runtime import proc as _proc
from bgate_core.store import project as _project
from bgate_core.board import queue as _queue
from bgate_ui import api
from bgate_ui.agents import dispatch as _dispatch

router = APIRouter()

_log = logging.getLogger(__name__)


def _pkey(root: str) -> str:
    """One comparable key per project directory.

    normcase + normpath because Windows is the supported platform, where
    C:\\Games\\Ember and c:/games/ember are one folder and a string compare says
    they are two -- which would list one project twice and let a stop aimed at
    one of them miss.
    """
    try:
        return os.path.normcase(os.path.normpath(str(root)))
    except (TypeError, ValueError):
        return str(root)


def _names() -> dict[str, str]:
    """{project key: display name} for every registered project.

    known_projects() is {name: root} and its VALUES repeat -- renaming a project
    registers the new name without retiring the old one -- so this inverts it
    with first-registration-wins, matching what the project switcher shows.
    """
    out: dict[str, str] = {}
    try:
        for name, root in _project.known_projects().items():
            out.setdefault(_pkey(root), name)
    except Exception:
        pass
    return out


def _item_of(root: str, item_id: int) -> dict:
    """What the board says about this agent's work item, best-effort.

    A fleet row that says only "item 14" is not actionable, and the title is the
    thing an operator recognises. Never raises: the project may have moved, its
    database may be locked by the very agent being listed, or the row may have
    been deleted -- none of which is a reason to omit a RUNNING agent from the
    view whose entire job is to show running agents.
    """
    try:
        row = _queue.get(root, int(item_id))
    except Exception:
        return {}
    return {"title": str(row.get("title") or "")[:200],
            "status": str(row.get("status") or ""),
            "seat": str(row.get("seat") or "")}


def _rows() -> list[dict]:
    """One row per genuinely-live agent, machine-wide.

    agentreg.live() is already the strict list -- pid AND recorded process start
    time, so a stale entry wearing a recycled pid is excluded rather than
    offered with a stop button pointed at somebody's editor.
    """
    now = time.time()
    rows: list[dict] = []
    for entry in _agentreg.live():
        root = str(entry.get("root") or "")
        item_id = int(entry.get("item_id") or 0)
        started = float(entry.get("started_at") or 0.0)
        item = _item_of(root, item_id) if root and item_id else {}
        rows.append({
            "pid": int(entry.get("pid") or 0),
            "item_id": item_id,
            # The seat off the registry entry, falling back to the board row:
            # the entry is written at spawn and is the fact about THIS run,
            # while the row can have been edited since.
            "seat": str(entry.get("seat") or item.get("seat") or ""),
            "root": root,
            "runner": str(entry.get("runner") or ""),
            "log": str(entry.get("log") or ""),
            "started_at": started,
            # Wall clock, not monotonic: this entry may have been written by a
            # process that is gone, so there is no shared monotonic origin to
            # subtract from. 0 when unrecorded rather than the epoch, which
            # would report the run as 56 years long.
            "seconds": int(max(0.0, now - started)) if started else 0,
            "item_title": item.get("title", ""),
            "item_status": item.get("status", ""),
        })
    return rows


@router.get("/api/agents/all")
def agents_all() -> dict:
    """Every live agent on this machine, grouped by the project it is pinned to.

    Grouped rather than flat because the project is the unit an operator acts
    on: "stop everything on the game I just left" is the actual question, and a
    flat list makes the reader do the grouping by eye across two games with
    similarly-numbered items.
    """
    try:
        active = str(_project.active_root() or "")
    except Exception:
        active = ""
    names = _names()
    groups: dict[str, dict] = {}
    for row in _rows():
        key = _pkey(row["root"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "root": row["root"],
                # An UNREGISTERED root is possible and must still be shown: the
                # agent is running either way, and hiding it behind a missing
                # registry entry is exactly the blind spot this file exists to
                # close. The folder name is a better label than nothing.
                "name": names.get(key) or (Path(row["root"]).name
                                           if row["root"] else "unknown"),
                "active": bool(active) and key == _pkey(active),
                "agents": [],
            }
        group["agents"].append(row)
    # Oldest agent first inside a project (the one that has been running longest
    # is the one you are most likely looking for), active project first overall.
    projects = sorted(groups.values(),
                      key=lambda g: (not g["active"], g["name"].lower()))
    for group in projects:
        group["agents"].sort(key=lambda r: (-r["started_at"], r["item_id"]))
    return api.ok({"projects": projects,
                   "total": sum(len(g["agents"]) for g in projects),
                   "active_root": active})


def _stop_one(row: dict, actor: str) -> dict:
    """Stop one recorded agent, by whichever route this process actually has.

    THE IN-PROCESS PATH IS TRIED FIRST AND IT IS NOT AN OPTIMISATION. When this
    dashboard spawned the agent it holds the stdin, the log handle and the run
    entry; dispatch.stop closes all three, banks the item with the operator's
    name on it, and stops the watchdog from later reporting the same run as
    having mysteriously died. Killing the pid behind its back would leave every
    one of those dangling. ``root`` is passed so a same-numbered item in another
    game cannot be hit by mistake -- see dispatch.stop.
    """
    root, item_id, pid = row["root"], int(row["item_id"]), int(row["pid"])
    mine = _dispatch.stop(item_id, actor=actor, root=root)
    if mine.get("ok"):
        return {"ok": True, "item_id": item_id, "pid": pid, "root": root,
                "how": "dispatch"}

    # No handle here: another dashboard spawned it, or the one that did is gone.
    # The pid and the recorded start time are the whole inheritance, and
    # agentreg.live() already proved they still name this run.
    killed = _proc.kill_pid_tree(pid)
    _agentreg.forget(pid)
    reason = (f"stopped by {actor} from the fleet view - this run was ended by "
              "hand from a dashboard that did not spawn it, so it reported "
              "nothing about what it had finished")
    banked = ""
    if root and item_id:
        try:
            # Only while it still reads 'dispatched'. If the agent got as far as
            # queue_complete its own result is the better record, and the same
            # rule agentreg.reconcile follows applies here.
            if _queue.get(root, item_id)["status"] == "dispatched":
                _queue.stop(root, item_id, by=actor, reason=reason)
                banked = "failed"
        except Exception as exc:                                  # noqa: BLE001
            # THE TYPE, NOT THE MESSAGE. An exception's text here can carry the
            # sqlite statement and the absolute paths around it, and this value
            # is returned to a browser - CodeQL calls that stack-trace exposure
            # and it is right, even on a loopback surface: the detail belongs in
            # the log the operator can read, not in a JSON body a page renders.
            # The type is enough to tell "the row was gone" from "the database
            # was locked", which is the only distinction this line has to make.
            _log.warning("stop-all could not bank item %s in %s", item_id, root,
                         exc_info=True)
            banked = f"could not bank the item ({type(exc).__name__})"
    return {"ok": bool(killed), "item_id": item_id, "pid": pid, "root": root,
            "how": "pid", "banked": banked,
            "error": "" if killed else f"pid {pid} could not be aimed at"}


@router.post("/api/agents/stop")
def agents_stop(request: Request, payload: dict) -> dict:
    """Stop ONE agent, named by its work item.

    ``root`` is optional and only needed to disambiguate: item ids are per
    project, so two games can each have a live #14. Rather than guessing (and
    killing the wrong game's run, which is unrecoverable in the sense that
    matters -- the work is gone and the operator was not told), an ambiguous id
    is a 400 that lists the candidates.
    """
    api.require_human(api.current_actor(request), "stop an agent")
    payload = payload or {}
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        raise api.bad_request("item_id is required")
    want = str(payload.get("root") or "").strip()

    matches = [r for r in _rows() if r["item_id"] == item_id
               and (not want or _pkey(r["root"]) == _pkey(want))]
    if not matches:
        raise api.not_found(
            f"no live agent for item {item_id}"
            + (f" in {want}" if want else " on this machine"),
            item_id=item_id, root=want)
    roots = {_pkey(r["root"]) for r in matches}
    if len(roots) > 1:
        raise api.bad_request(
            f"item {item_id} names a live agent in {len(roots)} projects - "
            "pass root to say which",
            item_id=item_id, roots=sorted(r["root"] for r in matches))
    return api.ok(_stop_one(matches[0], api.current_actor(request)))


@router.post("/api/agents/stop-all")
def agents_stop_all(request: Request, payload: Optional[dict] = None) -> dict:
    """Stop every live agent, or every one pinned to ``root``.

    Deliberately reports PER AGENT, refusals included. A broadcast stop that
    half landed and answered "ok" is worse than one that failed outright,
    because the operator stops watching - the same reasoning the steer-all
    endpoint is written to.

    This does NOT touch auto-deploy. dispatch.kill_all is the per-project panic
    button and turns the loop off first, precisely so nothing is re-dispatched
    into the gap; this is the machine-wide view's stop, and a fleet view has no
    business flipping a switch in a project the operator is not standing in.
    The UI says which one it is offering.
    """
    api.require_human(api.current_actor(request), "stop agents")
    want = str((payload or {}).get("root") or "").strip()
    actor = api.current_actor(request)
    rows = [r for r in _rows() if not want or _pkey(r["root"]) == _pkey(want)]
    results = [_stop_one(row, actor) for row in rows]
    return api.ok({"root": want, "attempted": len(results),
                   "stopped": sum(1 for r in results if r.get("ok")),
                   "results": results})
