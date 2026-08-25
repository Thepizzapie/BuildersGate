"""One engine process per project at a time — enforced, not remembered.

TWO CONCURRENT GODOT PROCESSES DEADLOCK ON THE SHARED ``.godot`` CACHE on
Windows, and the symptom is indistinguishable from a hang: both processes sit
there, neither prints, and the timeout that eventually kills them reports
"the game did not exit within 120s". Three agents were killed after 25 minutes
of total silence having written nothing, and part of that silence was this.

Before this module the rule existed — as a sentence in a project's seat note,
which means every operator and every agent had to remember it, and a rule that
depends on being remembered by a fresh process with no memory is not a rule.
The harness knows perfectly well when it is about to spawn an engine. It should
be the thing that serialises.

WHY A LOCK FILE AND NOT A MUTEX. The contenders are in different PROCESSES —
the MCP server, the dashboard's dispatcher, a worker agent's own tool call, a
`bgate` CLI invocation — and often on different Python interpreters. A file is
the only channel all of them already share. It lives beside the cache it is
protecting.

STALENESS IS NOT OPTIONAL. A killed engine cannot release anything, so a lock
with no expiry converts one crash into a project that can never run the engine
again. The holder stamps a deadline; a lock past its deadline is broken by the
next caller, loudly, and the break is reported so "your capture waited because
a dead run was still holding the engine" is a sentence somebody can read.

CONTENTION IS REPORTED, NOT HIDDEN. :func:`hold` yields a dict saying whether
it waited and for how long and who it waited on. A tool that took 40 seconds
because another agent was mid-export should say so rather than look slow.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

LOCK_NAME = "engine.lock"

#: How long a holder's claim is honoured before the next caller breaks it.
#: Above the longest legitimate engine call this harness makes (an export at
#: 600s) with room to spare, and far below "forever".
DEFAULT_TTL_S = 900

#: How long to wait for the lock before giving up. A caller that would rather
#: fail than queue passes 0.
DEFAULT_WAIT_S = 300

_POLL_S = 0.25


class EngineBusy(RuntimeError):
    """Another engine process holds this project and did not release in time.

    An explicit failure rather than a silent parallel spawn: the parallel spawn
    is the deadlock, and a deadlock reported as a timeout sends the reader to
    the wrong file.
    """


def lock_path(project_dir: str | os.PathLike[str]) -> Path:
    """Beside the cache it protects, so it moves with the project."""
    base = Path(project_dir)
    return base / ".godot" / LOCK_NAME


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def holder(project_dir: str | os.PathLike[str]) -> dict:
    """Who holds this project's engine right now, or {}. Never raises."""
    got = _read(lock_path(project_dir))
    if not isinstance(got, dict) or not got:
        return {}
    expires = float(got.get("expires_at") or 0)
    return {**got, "expired": expires and expires < time.time()}


def _claim(path: Path, what: str, ttl: float) -> bool:
    """Atomically create the lock. False if somebody else already has it."""
    payload = json.dumps({
        "pid": os.getpid(),
        "what": str(what or "")[:200],
        "actor": os.environ.get("BGATE_LOCK_OWNER", "")[:120],
        "at": time.time(),
        "expires_at": time.time() + float(ttl),
    })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        # An unwritable .godot must not stop the engine running — the lock is a
        # serialiser, not a permission. Report as taken-by-nobody and proceed.
        return True
    try:
        os.write(handle, payload.encode("utf-8"))
    finally:
        os.close(handle)
    return True


@contextmanager
def hold(project_dir: str | os.PathLike[str], what: str = "",
         *, wait_s: float = DEFAULT_WAIT_S,
         ttl_s: float = DEFAULT_TTL_S) -> Iterator[dict]:
    """Serialise one engine invocation for this project.

    Yields ``{waited_s, contended, waited_on, broke_stale}``. The caller is
    expected to surface ``contended`` in its own result: a screenshot that took
    a minute because an export was running is not a slow screenshot.
    """
    path = lock_path(project_dir)
    started = time.monotonic()
    deadline = started + max(0.0, float(wait_s))
    waited_on: dict = {}
    broke_stale = False
    mine = False

    while True:
        if _claim(path, what, ttl_s):
            mine = True
            break
        current = _read(path)
        waited_on = current or waited_on
        expires = float((current or {}).get("expires_at") or 0)
        if expires and expires < time.time():
            # The holder is dead or hung past its own deadline. Break it, and
            # say so — a broken lock is evidence about a previous run, not
            # routine housekeeping.
            try:
                path.unlink(missing_ok=True)
                broke_stale = True
            except OSError:
                pass
            continue
        if time.monotonic() >= deadline:
            raise EngineBusy(
                "another Godot/Blender process is already running against "
                f"{project_dir} (pid {(current or {}).get('pid', '?')}, "
                f"{(current or {}).get('what', 'unknown work')!r}) and did not "
                f"finish within {wait_s:.0f}s. TWO ENGINE PROCESSES SHARING "
                "ONE .godot CACHE DEADLOCK ON WINDOWS and the symptom is "
                "identical to a hang, so this refuses rather than spawning. "
                "Wait for that run, or stop it.")
        time.sleep(_POLL_S)

    waited = round(time.monotonic() - started, 2)
    try:
        yield {
            "waited_s": waited,
            "contended": waited >= _POLL_S,
            "waited_on": {k: waited_on.get(k) for k in ("pid", "what", "actor")}
                         if waited >= _POLL_S else {},
            "broke_stale": broke_stale,
        }
    finally:
        if mine:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def clear(project_dir: str | os.PathLike[str]) -> dict:
    """Drop a lock left by a killed run. Reports what it removed."""
    path = lock_path(project_dir)
    was = _read(path)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "held": was}
    return {"ok": True, "removed": bool(was), "held": was}
