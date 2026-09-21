"""Gate the EXPORT, not the editor — item #17.

EXIT 67's whole QA pass ran against the loose project directory. Every gate
answered "boots fine" because the editor never once loaded the thing that
actually ships: the exported pck. ``godot_export_verify`` (server.py,
``bgate_adapters.godot_audit.export_verify``) already diffs a scene loaded
from the project against the SAME scene loaded from a pck — but it was an
opt-in tool nobody was required to run, and running it once at the start of
a project proves nothing about the build three days and forty commits later.

So this module makes it a STANDING FACT the release gate (and the playtest
gate) can read: :func:`record` is called by ``godot_export_verify`` on every
run, and :func:`unmet` compares the LAST recorded verification's timestamp
against the newest ``.gd``/``.tscn`` mtime under the project — a source file
touched after the last export check invalidates it, exactly the way a stale
cache should.
"""
from __future__ import annotations

import os
import time as _time
from datetime import datetime, timezone
from typing import Optional

from ..store import workspace as _ws

SEAT = "qa"
DOC_KEY = "export_verify"

#: Extensions whose change invalidates a recorded export verification. The
#: same two the exported pck's own diff walks (nodes from .tscn, behaviour
#: from .gd) — a changed .png or .tres does not reshape the scene graph the
#: verify diffed, so it is deliberately not in this list.
SOURCE_SUFFIXES = (".gd", ".tscn")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(root: str | os.PathLike[str], *, godot_project: str, scene: str,
          pck: str, ok: bool, diffs: int = 0, by: str = "") -> dict:
    """Called by godot_export_verify after every run — the standing fact.

    Keyed by ``godot_project`` because a project can hold more than one
    Godot project (rare, but the sceneproof/assets sections already have to
    handle it); a verification for one project says nothing about another.
    """
    doc = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    by_project = doc.get("by_project")
    if not isinstance(by_project, dict):
        by_project = {}
    key = os.path.normpath(os.path.abspath(str(godot_project))).replace("\\", "/")
    by_project[key] = {
        # "at" is the human-readable timestamp; "at_ts" is a full-precision
        # epoch float for comparisons. Comparing "at" alone (second
        # precision) let a verify recorded in the same wall-clock second as
        # a source edit read as stale or fresh at random depending on
        # sub-second ordering — measured while writing this module's own
        # tests.
        "at": _now(), "at_ts": _time.time(), "scene": scene or "", "pck": pck,
        "ok": bool(ok), "diffs": int(diffs), "by": by or "",
    }
    doc["by_project"] = by_project
    clean = {k: v for k, v in doc.items() if k != _ws.VERSION_KEY}
    _ws.set(root, SEAT, DOC_KEY, clean)
    return by_project[key]


def last(root: str | os.PathLike[str], godot_project: str) -> Optional[dict]:
    """The most recent recorded verification for this project, or None."""
    doc = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    if not isinstance(doc, dict):
        return None
    by_project = doc.get("by_project")
    if not isinstance(by_project, dict):
        return None
    key = os.path.normpath(os.path.abspath(str(godot_project))).replace("\\", "/")
    got = by_project.get(key)
    return got if isinstance(got, dict) else None


def _newest_source_mtime(godot_project: str) -> tuple[float, str]:
    """The newest mtime among this project's .gd/.tscn, and which file it was.

    Skips ``.godot`` — the engine's own import cache holds generated .gd
    stubs for some resource types, and their mtime is a fact about when the
    cache was last rebuilt, not about a source edit.
    """
    newest = 0.0
    newest_path = ""
    for dirpath, dirnames, filenames in os.walk(godot_project):
        dirnames[:] = [d for d in dirnames if d != ".godot"]
        for name in filenames:
            if not name.endswith(SOURCE_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime > newest:
                newest, newest_path = mtime, full
    return newest, newest_path


def unmet(root: str | os.PathLike[str], godot_project: str) -> list[dict]:
    """The release/playtest gate's rows for this project's export verification.

    Every row names ``godot_export_verify`` as the clearing action, because
    that is the only tool that can produce the fact this gate is missing.
    """
    from ..board import findings as _findings

    if not godot_project or not os.path.isfile(
            os.path.join(godot_project, "project.godot")):
        return []

    got = last(root, godot_project)
    if got is None:
        return [_findings.make(
            gate="export", key=f"export:{godot_project}",
            kind=_findings.BLOCKING,
            claim=(f"{godot_project} has never had its export verified — "
                   "every gate that ran against it ran in the EDITOR, "
                   "against the loose project, and the exported pck could "
                   "be silently empty (measured: EXIT 67)"),
            tool="qa.exportgate.unmet",
            inputs={"godot_project": godot_project},
            measured={"recorded": False},
            clears_by=(f"godot_export_verify('{godot_project}', '<pck>') "
                       "after exporting a pck with --export-pack"))]

    newest, newest_path = _newest_source_mtime(godot_project)
    verified_at = got.get("at_ts")
    if not isinstance(verified_at, (int, float)):
        # Older records (or a hand-written test doc) may only have "at" —
        # fall back to second precision rather than treating them as never
        # verified.
        try:
            verified_at = datetime.fromisoformat(str(got.get("at") or "")).timestamp()
        except ValueError:
            verified_at = 0.0

    rows: list[dict] = []
    if newest > verified_at:
        rows.append(_findings.make(
            gate="export", key=f"export:{godot_project}:stale",
            kind=_findings.BLOCKING,
            claim=(f"{godot_project} was verified at {got.get('at')} but "
                   f"{newest_path} changed after that "
                   f"({datetime.fromtimestamp(newest, tz=timezone.utc).isoformat(timespec='seconds')}) "
                   "— the last export_verify no longer speaks for this "
                   "source"),
            tool="qa.exportgate.unmet",
            inputs={"godot_project": godot_project},
            measured={"verified_at": got.get("at"), "newest_source": newest_path,
                     "newest_mtime": newest},
            clears_by=(f"re-export and re-run godot_export_verify('"
                       f"{godot_project}', '<pck>')")))
    if not got.get("ok"):
        rows.append(_findings.make(
            gate="export", key=f"export:{godot_project}:failed",
            kind=_findings.BLOCKING,
            claim=(f"the last godot_export_verify on {godot_project} found "
                   f"{got.get('diffs', 0)} diff(s) between the project and "
                   "the exported pck — the exported build disagrees with "
                   "what was evidenced"),
            tool="qa.exportgate.unmet",
            inputs={"godot_project": godot_project},
            measured={"diffs": got.get("diffs", 0), "at": got.get("at")},
            clears_by=(f"fix the diffs godot_export_verify reported, "
                       f"re-export, and re-run it against the new pck")))
    return rows
