"""The machine-wide registry of running agents - one small file per run.

WHY THIS EXISTS. ``dispatch._live`` is a module-level dict of Popen handles, so
it dies with the process that built it. A dashboard restart left every agent it
had spawned INVISIBLE (nothing listed it) and UNKILLABLE (nothing held its
handle), while the agent itself carried on editing files and billing; and two
dashboards on one machine each saw only what they had spawned themselves, so
neither could answer "what is running right now" for the machine.

A file outlives the process that wrote it, which is the whole trick. Each run
drops one JSON under ``~/.bgate/agents/`` at spawn and removes it on exit, so
any dashboard, any CLI, any project can read the fleet.

PID PLUS PROCESS START TIME, NEVER PID ALONE. This is the detail the rest of
the module is built around. Pids are recycled within minutes on Windows, and
this ledger is best-effort by construction - a hard crash leaves entries for
processes that died hours ago. A stale entry that names nothing but a number
will eventually name SOMEBODY ELSE'S PROGRAM, and "stop that agent" would then
kill the user's editor. The creation time of a process is the one thing a
recycled pid cannot inherit, so it is recorded at spawn and compared on every
read; an entry whose pid is alive but whose start time differs is a stranger
and is treated as gone, not as ours.

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

from ..store.project import user_dir

DIRNAME = "agents"

# Two starts recorded by different mechanisms (psutil, ctypes, /proc) round
# differently, so an exact float compare would call our own process a stranger.
# A second is far tighter than any realistic pid recycle on a live machine.
START_TOLERANCE_S = 1.0


def registry_dir() -> Path:
    """``~/.bgate/agents`` - through user_dir(), so BGATE_HOME moves it."""
    return user_dir() / DIRNAME


def _path(pid: int) -> Path:
    return registry_dir() / f"agent-{int(pid)}.json"


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
# The registry itself
# ---------------------------------------------------------------------------


def record(pid: int, *, item_id: int, seat: str = "", root: str = "",
           runner: str = "", log: str = "") -> Optional[Path]:
    """Write this run's entry. Best-effort: a registry write must NEVER be the
    reason a dispatch fails, because the run it is describing has already
    started by the time this is called.

    The start time is read here, at spawn, while the process is certainly alive
    and certainly still ours - reading it later would race the very recycling
    this whole module exists to survive.
    """
    try:
        path = _path(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": int(pid),
                   "proc_started": process_start(pid),
                   "item_id": int(item_id),
                   "seat": str(seat or ""),
                   "root": str(root or ""),
                   "runner": str(runner or ""),
                   "log": str(log or ""),
                   "started_at": time.time()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
    except Exception:
        return None


def forget(pid: int) -> bool:
    """Drop this run's entry. True if there was one to drop."""
    try:
        _path(pid).unlink()
        return True
    except OSError:
        return False


def entries() -> list[dict]:
    """Every recorded run, verified or not, newest first.

    A file that will not parse is skipped rather than raising: this directory is
    written by several processes at once and a half-written file is a moment,
    not a failure.
    """
    found: list[dict] = []
    try:
        paths = sorted(registry_dir().glob("agent-*.json"))
    except OSError:
        return found
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("pid"):
            data["path"] = str(path)
            found.append(data)
    found.sort(key=lambda e: float(e.get("started_at") or 0), reverse=True)
    return found


def live() -> list[dict]:
    """Every entry whose process is GENUINELY still the one that was recorded.

    Anything the host would not vouch for is left out: this list is what a
    cross-project view lists and what a stop button aims at, and a stranger
    wearing a recycled pid must never appear in either.
    """
    return [e for e in entries() if _matches(e) is True]


def stale() -> list[dict]:
    """Entries whose process is definitively gone. The complement of live()
    minus the unknowable ones, which belong to neither list."""
    return [e for e in entries() if _matches(e) is False]


def reconcile() -> dict:
    """Settle the work items of agents that died without cleaning up.

    An entry only exists between spawn and exit, so one left behind by a
    process that is gone means the run ended without anybody banking it - a
    dashboard killed, a machine rebooted, a crash. The item on the other side
    of that is sitting in 'dispatched', which is the status nothing recovers
    from on its own: autodeploy will not touch it, dispatch() refuses it as
    'not queued', and the board shows work in flight that no process is doing.

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
                # entry in the registry, so the forget below still runs.
                pass
        forget(int(entry["pid"]))
        cleared.append(int(entry["pid"]))
    return {"failed": failed, "cleared": cleared}
