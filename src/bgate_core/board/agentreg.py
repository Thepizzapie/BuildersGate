"""The machine-wide registry of running agents, read off every project's
``agent_runs`` table.

WHY THIS EXISTS. ``dispatch._live`` is a module-level dict of Popen handles, so
it dies with the process that built it. A dashboard restart left every agent it
had spawned INVISIBLE (nothing listed it) and UNKILLABLE (nothing held its
handle), while the agent itself carried on editing files and billing; and two
dashboards on one machine each saw only what they had spawned themselves, so
neither could answer "what is running right now" for the machine.

A database row outlives the process that wrote it, which is the whole trick.
Each run is one row in its project's ``agent_runs`` table (migration 0044),
written at spawn and finished at exit; the fleet is the union of the open rows
across every registered project, so any dashboard, any CLI, any project can
read it. The per-run JSON files this module used to drop under
``~/.bgate/agents`` are gone - the table is the only record.

PID PLUS PROCESS START TIME, NEVER PID ALONE. This is the detail the rest of
the module is built around. Pids are recycled within minutes on Windows, and
this ledger is best-effort by construction - a hard crash leaves rows for
processes that died hours ago. A stale row that names nothing but a number
will eventually name SOMEBODY ELSE'S PROGRAM, and "stop that agent" would then
kill the user's editor. The creation time of a process is the one thing a
recycled pid cannot inherit, so it is recorded at spawn and compared on every
read; a row whose pid is alive but whose start time differs is a stranger and
is treated as gone, not as ours.

THREE ANSWERS, NOT TWO. :func:`_matches` returns True (this is our process),
False (definitively not) or None (this host would not say). Unknowable is its
own answer because both mistakes are expensive in opposite directions: calling
it live leaves a dead item stuck 'dispatched' forever, and calling it dead
fails a work item out from under an agent that is still generating art. Both
:func:`live` and :func:`reconcile` therefore act only on a definite answer and
leave the unknowable alone.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from ..store import db, project as _project

# Two starts recorded by different mechanisms (psutil, ctypes, /proc) round
# differently, so an exact float compare would call our own process a stranger.
# A second is far tighter than any realistic pid recycle on a live machine.
START_TOLERANCE_S = 1.0


# ---------------------------------------------------------------------------
# Asking the OS about a pid
# ---------------------------------------------------------------------------
# psutil answers both questions in one call and is used WHEN IT HAPPENS TO BE
# INSTALLED - it is deliberately not a declared dependency of this project and
# must never become one on account of this module, so every path below works
# without it.

_FILETIME_EPOCH_S = 11644473600.0   # 1601-01-01 -> 1970-01-01, in seconds
_STILL_ACTIVE = 259


def _psutil_probe(pid: int) -> Optional[tuple[Optional[bool], Optional[float]]]:
    """(exists, start) via psutil, or None when psutil is not installed."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(int(pid))
        return True, round(float(proc.create_time()), 3)
    except psutil.NoSuchProcess:
        return False, None
    except Exception:
        # Access denied and friends: the pid exists but this host will not
        # describe it, which is the unknowable answer rather than "gone".
        return None, None


def _windows_probe(pid: int) -> tuple[Optional[bool], Optional[float]]:
    """(exists, start) from kernel32 directly - no dependency, no subprocess.

    ctypes rather than `wmic`/PowerShell because both of those cost a process
    spawn per pid and wmic is gone from current Windows 11 images. OpenProcess
    with PROCESS_QUERY_LIMITED_INFORMATION is the least privilege that answers
    GetProcessTimes, and it works across sessions, which matters when the agent
    was spawned by a dashboard that is no longer running.
    """
    import ctypes
    from ctypes import wintypes

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None, None
    # Declared explicitly: without a restype the returned HANDLE comes back
    # through a 32-bit int and is truncated on 64-bit Windows, which turns a
    # perfectly good handle into a bogus one (and then a leaked one).
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not handle:
        # 87 ERROR_INVALID_PARAMETER is what a pid that does not exist answers.
        # Anything else (5 ERROR_ACCESS_DENIED, most often) means there IS a
        # process there and we simply may not look at it.
        return (False, None) if ctypes.get_last_error() == 87 else (None, None)
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and \
                code.value != _STILL_ACTIVE:
            # A handle can outlive the process it names, so an open handle is
            # not proof of life. (A process that genuinely exits with 259 reads
            # as alive here; that errs toward leaving things alone, which is
            # the direction this module always errs.)
            return False, None
        created = wintypes.FILETIME()
        rest = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        ok = k32.GetProcessTimes(handle, ctypes.byref(created),
                                 *(ctypes.byref(x) for x in rest))
        if not ok:
            return True, None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return True, round(ticks / 1e7 - _FILETIME_EPOCH_S, 3)
    except Exception:
        return None, None
    finally:
        k32.CloseHandle(handle)


def _posix_probe(pid: int) -> tuple[Optional[bool], Optional[float]]:
    """(exists, start) on Linux-ish hosts. Best-effort: this is not the
    supported platform, and a host that will not say gets the unknowable
    answer rather than a guess."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return None, None            # exists, not ours to inspect
    except OSError:
        return None, None
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        # The comm field is parenthesised and may itself contain spaces and
        # parens, so the fields are counted from the LAST ')'.
        fields = stat[stat.rindex(")") + 2:].split()
        ticks = float(fields[19])                     # field 22, 1-based
        hz = float(os.sysconf("SC_CLK_TCK"))
        boot = 0.0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
                break
        if not boot or not hz:
            return True, None
        return True, round(boot + ticks / hz, 3)
    except (OSError, ValueError, IndexError):
        return True, None


def probe(pid: int) -> tuple[Optional[bool], Optional[float]]:
    """``(exists, start_time)`` for a pid, as much as this host will say.

    ``exists`` is True/False/None (see the module docstring on why None is a
    real answer). ``start_time`` is epoch seconds, or None when unreadable.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, None
    if pid <= 0:
        return False, None
    answered = _psutil_probe(pid)
    if answered is not None:
        return answered
    if sys.platform == "win32":
        return _windows_probe(pid)
    return _posix_probe(pid)


def process_start(pid: int) -> Optional[float]:
    """The process creation time in epoch seconds, or None if unknowable."""
    return probe(pid)[1]


def _matches(entry: dict) -> Optional[bool]:
    """Is the pid in this entry still the process it was written for?

    True/False/None as above. An entry with no recorded start time cannot be
    proved either way once its pid is alive - that is an entry written by an
    older build or on a host that would not say, and it stays unknowable rather
    than being promoted to "ours" on the strength of a recycled number.
    """
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    exists, started = probe(pid)
    if exists is False:
        return False
    if exists is None:
        return None
    recorded = entry.get("proc_started")
    if recorded is None or started is None:
        return None
    return abs(float(started) - float(recorded)) < START_TOLERANCE_S


# ---------------------------------------------------------------------------
# The registry itself: the agent_runs table, one per project
# ---------------------------------------------------------------------------
COLUMNS = ("id", "project", "item_id", "pid", "proc_started", "proc_name",
           "seat", "runner", "log", "started_at", "ended_at", "status",
           "result_json", "cost_usd")


def _key(root) -> str:
    return os.path.normcase(os.path.normpath(str(Path(root).resolve())))


def _row(r) -> dict:
    out = dict(r)
    out["root"] = out.get("project") or ""
    try:
        out["result"] = json.loads(out.get("result_json") or "{}")
    except ValueError:
        out["result"] = {}
    return out


def _ensure_registered(root: str) -> None:
    """A project with a run on record is a project the fleet view must be
    able to find. Registered under its slug, or its folder name when the slug
    already names a different folder (a copied project keeps its slug)."""
    try:
        here = _key(root)
        known = _project.known_projects()
        if any(_key(r) == here for r in known.values()):
            return
        try:
            name = _project.get(root)["slug"]
        except Exception:
            name = Path(root).name
        if name in known:
            name = Path(root).name
        if name in known:
            name = f"{name}-{abs(hash(here)) % 10_000}"
        _project.register(root, name)
    except Exception:
        pass


def record(pid: int, *, item_id: int, seat: str = "", root: str = "",
           runner: str = "", log: str = "", name: str = "") -> Optional[int]:
    """Write this run's row; returns its id. Best-effort: a registry write
    must NEVER be the reason a dispatch fails, because the run it describes
    has already started by the time this is called.

    The start time is read here, at spawn, while the process is certainly
    alive and certainly still ours - reading it later would race the very
    recycling this whole module exists to survive.
    """
    if not root:
        return None
    try:
        started = process_start(pid)
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO agent_runs (project, item_id, pid, proc_started, "
                "proc_name, seat, runner, log, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(Path(root).resolve()), int(item_id), int(pid), started,
                 str(name or ""), str(seat or ""), str(runner or ""),
                 str(log or ""), time.time()))
            row_id = int(cur.lastrowid)
        _ensure_registered(root)
        return row_id
    except Exception:
        return None


def finish(root: str, item_id: int, pid: int = 0, *, status: str = "exited",
           result: Optional[dict] = None, cost_usd: Optional[float] = None,
           create: bool = False) -> bool:
    """Close the OPEN row for this run. True if one was open.

    Only an open row is touched: a run finished once is finished, and a
    later caller (the orphan sweep after a reap, the fleet view after a stop)
    must not rewrite the outcome the reaper banked. ``create`` inserts an
    already-closed row when none was open - the reaper's case for a run
    nothing recorded at spawn, whose result still deserves a row to be read
    from.
    """
    try:
        with db.tx(root) as conn:
            where = "item_id = ? AND ended_at IS NULL"
            args: list = [int(item_id)]
            if pid:
                where += " AND pid = ?"
                args.append(int(pid))
            sets = ["ended_at = ?", "status = ?"]
            vals: list = [time.time(), str(status)]
            if result is not None:
                sets.append("result_json = ?")
                vals.append(json.dumps(result, default=str))
            if cost_usd is not None:
                sets.append("cost_usd = ?")
                vals.append(float(cost_usd))
            cur = conn.execute(
                f"UPDATE agent_runs SET {', '.join(sets)} WHERE {where}",
                (*vals, *args))
            if cur.rowcount > 0:
                return True
            if not create:
                return False
            now = time.time()
            conn.execute(
                "INSERT INTO agent_runs (project, item_id, pid, started_at, "
                "ended_at, status, result_json, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(Path(root).resolve()), int(item_id), int(pid or 0), now,
                 now, str(status), json.dumps(result or {}, default=str),
                 float(cost_usd or 0)))
            return True
    except Exception:
        return False


def set_cost(root: str, item_id: int, pid: int, cost_usd: float) -> None:
    """The price, once the final result event has been read. Lands on the
    newest row for the run whether or not it has been closed yet."""
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE agent_runs SET cost_usd = ? WHERE id = (SELECT id FROM "
                "agent_runs WHERE item_id = ? AND pid = ? ORDER BY started_at "
                "DESC LIMIT 1)", (float(cost_usd), int(item_id), int(pid)))
    except Exception:
        pass


def open_runs(root: str) -> list[dict]:
    """Rows with no ended_at, newest first - every run somebody recorded
    against this project and nobody has finished."""
    try:
        rows = db.connect(root).execute(
            "SELECT * FROM agent_runs WHERE ended_at IS NULL "
            "ORDER BY started_at DESC").fetchall()
    except Exception:
        return []
    return [_row(r) for r in rows]


def finished_runs(root: str, *, limit: int = 20,
                  since_s: Optional[float] = None) -> list[dict]:
    """The newest finished runs, oldest first, inside a window: at most
    ``limit`` rows and nothing that ended more than ``since_s`` ago."""
    try:
        args: list = []
        sql = "SELECT * FROM agent_runs WHERE ended_at IS NOT NULL"
        if since_s is not None:
            sql += " AND ended_at >= ?"
            args.append(time.time() - float(since_s))
        sql += " ORDER BY ended_at DESC LIMIT ?"
        args.append(int(limit))
        rows = db.connect(root).execute(sql, args).fetchall()
    except Exception:
        return []
    return [_row(r) for r in reversed(rows)]


def last_run(root: str, item_id: int) -> dict:
    """The newest row for an item, open or not. ``{}`` if none."""
    try:
        r = db.connect(root).execute(
            "SELECT * FROM agent_runs WHERE item_id = ? ORDER BY started_at "
            "DESC LIMIT 1", (int(item_id),)).fetchone()
    except Exception:
        return {}
    return _row(r) if r else {}


def forget(pid: int, root: str = "") -> bool:
    """Close the open row for this pid wherever it is. True if there was one.

    The fleet view calls this after killing a process it did not spawn, with
    no project in hand; the sweep looks across every registered project.
    """
    roots = [root] if root else [e["root"] for e in entries()
                                 if int(e.get("pid") or 0) == int(pid)]
    done = False
    for where in roots:
        try:
            with db.tx(where) as conn:
                cur = conn.execute(
                    "UPDATE agent_runs SET ended_at = ?, status = 'gone' "
                    "WHERE pid = ? AND ended_at IS NULL",
                    (time.time(), int(pid)))
                done = done or cur.rowcount > 0
        except Exception:
            continue
    return done


def _roots() -> list[str]:
    """Every project the fleet can see: the registry, plus the one this
    process is pinned to, plus the active one."""
    seen: dict[str, str] = {}
    candidates = list(_project.known_projects().values())
    for extra in (os.environ.get("BGATE_ROOT", ""), _project.active_root()):
        if extra:
            candidates.append(str(extra))
    for root in candidates:
        try:
            if not (Path(root) / db.DB_DIRNAME / db.DB_FILENAME).exists():
                continue
            seen.setdefault(_key(root), str(root))
        except Exception:
            continue
    return list(seen.values())


def entries() -> list[dict]:
    """Every open run across every project this machine knows, newest first.

    A project whose database will not open is skipped rather than raising:
    an unplugged drive is a moment, not a reason to hide the rest of the fleet.
    """
    found: list[dict] = []
    for root in _roots():
        for row in open_runs(root):
            if row.get("pid"):
                found.append(row)
    found.sort(key=lambda e: float(e.get("started_at") or 0), reverse=True)
    return found


def live() -> list[dict]:
    """Every open run whose process is GENUINELY still the one recorded.

    Anything the host would not vouch for is left out: this list is what a
    cross-project view lists and what a stop button aims at, and a stranger
    wearing a recycled pid must never appear in either.
    """
    return [e for e in entries() if _matches(e) is True]


def stale() -> list[dict]:
    """Open runs whose process is definitively gone. The complement of live()
    minus the unknowable ones, which belong to neither list."""
    return [e for e in entries() if _matches(e) is False]


def reconcile() -> dict:
    """Settle the work items of agents that died without cleaning up.

    An open row means the run ended without anybody banking it - a dashboard
    killed, a machine rebooted, a crash. The item on the other side of that is
    sitting in 'dispatched', which is the status nothing recovers from on its
    own: autodeploy will not touch it, dispatch() refuses it as 'not queued',
    and the board shows work in flight that no process is doing.

    Only items still reading 'dispatched' are touched. If the agent got as far
    as queue_complete, its own result is the better record and this must not
    overwrite it.
    """
    failed, cleared = [], []
    for entry in entries():
        if _matches(entry) is not False:
            continue
        root = str(entry.get("root") or "")
        item_id = int(entry.get("item_id") or 0)
        if root and item_id:
            try:
                from . import queue as _queue

                if _queue.get(root, item_id)["status"] == "dispatched":
                    _queue.complete(
                        root, item_id, failed=True,
                        result="the agent process is gone and nothing banked "
                               "this run - the dashboard that spawned it did "
                               "not survive to reap it, so what it did or did "
                               "not finish was never reported")
                    failed.append({"item_id": item_id, "root": root,
                                   "pid": entry.get("pid")})
            except Exception:
                # A project that has moved, a DB that will not open, an item
                # that was deleted: none of those are a reason to keep a dead
                # row open, so the finish below still runs.
                pass
        finish(root, item_id, int(entry.get("pid") or 0), status="gone")
        cleared.append(int(entry["pid"]))
    return {"failed": failed, "cleared": cleared}
