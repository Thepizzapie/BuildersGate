"""The harness's own version, and why editing it under running agents hurts.

TWO FAILURE MODES, AND THEY POINT IN OPPOSITE DIRECTIONS. Both were hit while
this pipeline was being worked on with its own agents running.

    A FRESH SPAWN GETS A HALF-WRITTEN PACKAGE. An agent starting while a module
    is mid-write imports whatever bytes happen to be on disk at that instant. A
    Python file is not written atomically; a multi-second edit has a window
    where the module is syntactically broken, and an agent that boots into it
    dies with a SyntaxError in a file the agent never touched. Observed exactly
    this way: `bgate_adapters/godot.py` was rewritten while a test run was
    importing it, and the traceback pointed at a line number that no longer
    existed by the time anybody looked.

    A RUNNING AGENT KEEPS THE OLD IMPORT FOR ITS WHOLE LIFE. Python caches
    modules per process. A dispatched agent's MCP server imported the harness
    at spawn and will keep that copy until it exits — so a fix landing
    mid-run reaches the NEXT agent and not this one. The operator watches the
    same bug reproduce after fixing it and concludes the fix does not work.
    "MCP not connected" and "my fix did nothing" are both this, and neither is
    a configuration problem.

WHAT THIS MODULE DOES ABOUT IT. It cannot lock the source — the human editing
it is the point of the exercise. What it can do is make both facts VISIBLE:

  * :func:`fingerprint` is a cheap identity for the harness source as it is
    right now. Stamped on a run at spawn, it answers "is the agent I am
    watching running the code I am reading?" — a question that previously had
    no answer short of restarting things until the symptom changed.
  * :func:`recently_edited` names harness files written in the last few
    seconds, which is the window where a spawn imports a half-written module.
  * :func:`spawn_guard` is the one call the dispatcher makes.

DELIBERATELY ADVISORY BY DEFAULT. Refusing to dispatch because somebody saved
a file two seconds ago would make the board unusable exactly when a person is
working on it, which is most of the time this project is under development. The
default is a short WAIT (the edit window is seconds, not minutes) and then a
spawn that records what it started against. ``BGATE_HARNESS_GUARD=block``
refuses instead, for a machine running unattended where a broken spawn is
worse than a delayed one.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

#: The packages whose bytes a dispatched agent executes. `frontend/` is not
#: here: the bundle is served to a browser, not imported by an agent, and a
#: half-written .tsx cannot break a spawn.
PACKAGES = ("bgate_core", "bgate_adapters", "bgate_mcp", "bgate_ui",
            "bgate_cli")

#: How recently a write counts as "the edit may still be in flight". A file
#: save completes in milliseconds; this is generous enough to cover an editor
#: writing several files in sequence and short enough not to be a nuisance.
EDIT_WINDOW_S = 4.0

#: How long :func:`spawn_guard` will wait for the window to pass before giving
#: up and reporting. Bounded: a spawn that blocks forever on a person typing is
#: a worse failure than the one being prevented.
MAX_WAIT_S = 12.0

MODES = ("off", "warn", "block")
DEFAULT_MODE = "warn"

_ENV = "BGATE_HARNESS_GUARD"


def mode() -> str:
    """How hard to guard a spawn against a live edit. Never raises."""
    chosen = os.environ.get(_ENV, "").strip().lower()
    return chosen if chosen in MODES else DEFAULT_MODE


def _sources(repo: Path) -> list[Path]:
    out: list[Path] = []
    for package in PACKAGES:
        base = repo / package
        if not base.is_dir():
            continue
        try:
            out.extend(p for p in base.rglob("*.py")
                       if "__pycache__" not in p.parts)
        except OSError:
            continue
    return out


def repo_root() -> Optional[Path]:
    """The checkout this harness is running FROM, not the game it is pointed at.

    Derived from this module's own location, which is the only thing that is
    true regardless of which project is active.
    """
    here = Path(__file__).resolve().parents[2]
    return here if (here / "bgate_core").is_dir() else None


def fingerprint(repo: Optional[Path] = None) -> dict:
    """A cheap identity for the harness source right now.

    ``{digest, files, newest_at, newest}``. The digest folds each file's path
    and mtime — not its content, which would mean reading the whole tree on a
    hot path for a question that mtime already answers.
    """
    base = repo or repo_root()
    if base is None:
        return {"digest": "", "files": 0, "newest_at": 0.0, "newest": ""}
    newest_at, newest = 0.0, ""
    hasher = hashlib.sha256()
    files = _sources(base)
    for path in sorted(files):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        hasher.update(f"{path.relative_to(base).as_posix()}:{stamp:.3f}".encode())
        if stamp > newest_at:
            newest_at, newest = stamp, path.relative_to(base).as_posix()
    return {"digest": hasher.hexdigest()[:16], "files": len(files),
            "newest_at": round(newest_at, 3), "newest": newest}


def recently_edited(within_s: float = EDIT_WINDOW_S,
                    repo: Optional[Path] = None) -> list[str]:
    """Harness files written in the last ``within_s`` seconds, newest first."""
    base = repo or repo_root()
    if base is None:
        return []
    cutoff = time.time() - float(within_s)
    hits: list[tuple[float, str]] = []
    for path in _sources(base):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp >= cutoff:
            hits.append((stamp, path.relative_to(base).as_posix()))
    return [name for _stamp, name in sorted(hits, reverse=True)]


def spawn_guard(*, wait: bool = True) -> dict:
    """May an agent be spawned right now? ``{ok, waited_s, edited, why, mode}``.

    Called by the dispatcher immediately before Popen. A spawn during the edit
    window imports whatever bytes are on disk at that instant, and a Python
    module is not written atomically.
    """
    active = mode()
    if active == "off":
        return {"ok": True, "waited_s": 0.0, "edited": [], "why": "",
                "mode": active}
    started = time.monotonic()
    edited = recently_edited()
    while edited and wait and (time.monotonic() - started) < MAX_WAIT_S:
        time.sleep(0.5)
        edited = recently_edited()
    waited = round(time.monotonic() - started, 2)
    if not edited:
        return {"ok": True, "waited_s": waited, "edited": [], "mode": active,
                "why": (f"waited {waited:.0f}s for a harness edit to settle"
                        if waited >= 0.5 else "")}
    why = (f"the harness itself was written {len(edited)} time(s) in the last "
           f"{EDIT_WINDOW_S:g}s and is still changing: "
           + ", ".join(edited[:4])
           + ". A Python module is not written atomically, so an agent "
             "spawned now can import a half-written file and die with a "
             "SyntaxError in a file it never touched.")
    return {"ok": active != "block", "waited_s": waited, "edited": edited,
            "mode": active, "why": why}


def drift(stamped: str, repo: Optional[Path] = None) -> dict:
    """Has the harness changed since a run started? ``{drifted, why}``.

    THE SECOND FAILURE MODE, made answerable. A running agent's MCP server
    imported the harness at spawn and keeps that copy until it exits, so a fix
    landing mid-run is NOT in the process you are watching. Without this the
    operator sees the bug reproduce after fixing it and concludes the fix does
    not work — which is how an afternoon goes.
    """
    now = fingerprint(repo)
    if not stamped or not now["digest"]:
        return {"drifted": False, "why": "", "now": now["digest"],
                "started_with": stamped}
    if stamped == now["digest"]:
        return {"drifted": False, "why": "", "now": now["digest"],
                "started_with": stamped}
    return {
        "drifted": True, "now": now["digest"], "started_with": stamped,
        "why": ("the harness source has changed since this run started, and "
                "this run is STILL EXECUTING THE OLD COPY — Python caches "
                "modules per process, so a fix made since it spawned reaches "
                "the next agent and not this one. Restart the run to pick the "
                "change up; do not read its behaviour as a verdict on the "
                "fix."),
    }
