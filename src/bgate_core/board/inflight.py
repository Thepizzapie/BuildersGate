"""What is running RIGHT NOW, so restarting the server cannot lose it silently.

THE FAILURE. A provider call takes eleven minutes. The director watches a tool
that has said nothing, concludes the MCP server is wedged, and restarts it. The
server dies; the worker thread dies with it; the provider charges anyway and
writes the sheet anyway; nobody is left holding the result. Measured on
night-shift three times in one evening, and each time the recovery was somebody
finding the file on disk hours later and guessing which job it belonged to.

Two halves of a fix, and the progress heartbeat in ``bgate_mcp.server`` is only
the first. The heartbeat stops a SLOW call from looking dead. It does nothing
about the restart that happens anyway — a crash, a machine sleeping, a client
reconnect, a person who is simply out of patience.

This is the second half: a call announces itself before it blocks and clears
itself when it lands, in a file that outlives the process. So

  * the next server start can SAY what the last one was holding when it died,
    by name, with how long it had been running — instead of the work being
    gone with no record that it existed;
  * anything that wants to warn before a restart has something true to read;
  * an orphaned call is a row somebody can act on rather than a file on disk
    with no provenance.

A FILE, NOT THE DATABASE. This is written at the start and end of every tool
call including the fast ones, and it has to survive the process being killed
between them — sqlite would put a write lock in front of every tool call in the
product to store something that is worthless the moment it is stale. One JSONL
per project under ``.bgate/inflight/<pid>.json``, rewritten whole; a dead pid's
file is the evidence.

STALENESS IS DECIDED BY THE PID, NOT BY A CLOCK. A call that has run for two
hours is a long call. A call whose process no longer exists is orphaned. Those
are different facts and a timeout cannot tell them apart, which is why the old
"is it stuck" heuristics in this product all eventually reported a working
Blender job as hung.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

DIRNAME = "inflight"

#: Calls under this many seconds are not worth mentioning in a restart warning.
#: A restart during a 0.4s queue_list orphans nothing anybody cares about.
NOTABLE_SECONDS = 15.0


def _dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / DIRNAME


def _file(root: str | os.PathLike[str], pid: Optional[int] = None) -> Path:
    return _dir(root) / f"{int(pid or os.getpid())}.json"


def _alive(pid: int) -> bool:
    """Is this pid a live process? Best effort, and biased toward 'yes'.

    A false 'dead' would report a running job as orphaned and send somebody to
    re-run work that is about to land, which is worse than a stale row.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
    except PermissionError:
        return True                      # exists, owned by someone else
    except Exception:                    # noqa: BLE001
        return False
    return True


def _read(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write(root: str | os.PathLike[str], doc: dict) -> None:
    """Never raises. A registry that can fail a tool call is a worse bug than
    the one it exists to record."""
    try:
        target = _file(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


def begin(root: str | os.PathLike[str], tool: str, *, seat: str = "",
          item_id: Any = None) -> str:
    """Announce a call. Returns a token for :func:`end`. Never raises."""
    token = f"{os.getpid()}-{time.time():.6f}"
    try:
        doc = _read(_file(root))
        calls = doc.get("calls")
        doc["calls"] = calls if isinstance(calls, dict) else {}
        doc["calls"][token] = {"tool": tool, "seat": seat,
                               "item_id": item_id, "started": time.time()}
        doc["pid"] = os.getpid()
        doc["seat"] = seat
        _write(root, doc)
    except Exception:                                             # noqa: BLE001
        return token
    return token


def end(root: str | os.PathLike[str], token: str) -> None:
    """Clear a call. Never raises."""
    try:
        doc = _read(_file(root))
        calls = doc.get("calls")
        if isinstance(calls, dict) and token in calls:
            calls.pop(token)
            doc["calls"] = calls
            _write(root, doc)
    except Exception:                                             # noqa: BLE001
        pass


def active(root: str | os.PathLike[str]) -> list[dict]:
    """Calls in flight in a LIVE process, newest first."""
    return [c for c in _scan(root) if c["alive"]]


def orphaned(root: str | os.PathLike[str]) -> list[dict]:
    """Calls whose process is gone — work that was paid for and lost.

    This is the restart's bill. A row here means a provider call, a Blender
    run or a Godot export was in progress when its server died: whatever it
    produced is on disk with nothing pointing at it, and whatever it cost was
    charged.
    """
    return [c for c in _scan(root) if not c["alive"]]


def _scan(root: str | os.PathLike[str]) -> list[dict]:
    out: list[dict] = []
    here = _dir(root)
    if not here.is_dir():
        return out
    now = time.time()
    for path in sorted(here.glob("*.json")):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        doc = _read(path)
        alive = _alive(pid)
        for token, call in (doc.get("calls") or {}).items():
            if not isinstance(call, dict):
                continue
            started = float(call.get("started") or 0)
            out.append({
                "token": token, "pid": pid, "alive": alive,
                "tool": str(call.get("tool") or ""),
                "seat": str(call.get("seat") or ""),
                "item_id": call.get("item_id"),
                "started": started,
                "seconds": round(max(0.0, now - started), 1),
            })
    out.sort(key=lambda c: c["started"], reverse=True)
    return out


def reap(root: str | os.PathLike[str]) -> list[dict]:
    """Take the orphan list and delete the dead processes' files.

    Called once at server start. Returning the rows before deleting them is
    the whole design: the ONLY moment this information exists is the first
    read after a restart, and a startup that quietly tidied up would erase the
    only record that the work happened.
    """
    lost = orphaned(root)
    here = _dir(root)
    if not here.is_dir():
        return lost
    for path in list(here.glob("*.json")):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        if not _alive(pid):
            try:
                path.unlink()
            except OSError:
                pass
    return lost


def restart_warning(root: str | os.PathLike[str]) -> str:
    """What restarting this server right now would cost, as one paragraph.

    Empty when nothing notable is running. This is the sentence a director
    should see before killing a server, and the reason the check exists at all
    is that "it has not printed anything for eight minutes" is indistinguishable
    from "it is wedged" without it.
    """
    running = [c for c in active(root) if c["seconds"] >= NOTABLE_SECONDS]
    if not running:
        return ""
    lines = [f"  - {c['tool']} ({c['seconds']:.0f}s"
             + (f", seat {c['seat']}" if c["seat"] else "")
             + (f", item #{c['item_id']}" if c["item_id"] else "") + ")"
             for c in running[:10]]
    more = f"\n  … and {len(running) - 10} more" if len(running) > 10 else ""
    return ("RESTARTING THE MCP SERVER NOW WOULD ORPHAN "
            f"{len(running)} CALL(S):\n" + "\n".join(lines) + more +
            "\nThese threads die with the process. Provider calls already "
            "placed are still charged and their files still land, with "
            "nothing left holding the result. Wait for them, or accept the "
            "loss deliberately.")


def startup_notice(root: str | os.PathLike[str]) -> str:
    """What the LAST server was holding when it died. Empty if it exited clean.

    Reaps as it reads — see :func:`reap`.
    """
    lost = [c for c in reap(root) if c["seconds"] >= NOTABLE_SECONDS]
    if not lost:
        return ""
    lines = [f"  - {c['tool']} (pid {c['pid']}, ran {c['seconds']:.0f}s"
             + (f", item #{c['item_id']}" if c["item_id"] else "") + ")"
             for c in lost[:10]]
    return (f"A PREVIOUS MCP SERVER DIED HOLDING {len(lost)} CALL(S):\n"
            + "\n".join(lines) +
            "\nAnything they produced is on disk with no provenance and "
            "anything they cost was charged. Check the artifact list before "
            "re-running that work.")
