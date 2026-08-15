"""Handing a run back to the terminal.

WHAT THIS IS FOR. Every dispatched agent IS a Claude Code session, and Claude
Code keeps that session's transcript on disk. So a run this dashboard is drawing
as cards can also be picked up in a terminal exactly where it left off, with its
whole context - which is what somebody who would rather work in the CLI than in
a web page actually wants, and what "read the log" is a poor substitute for. The
id has been in the log the whole time; nothing was storing it.

IT IS A HANDOFF, NOT A LAUNCH. This returns the command and says whether it will
work; it does not run it. The dashboard is a long-lived server that may not share
a terminal with anybody, `claude --resume` is interactive, and a button that
silently spawned an interactive process somewhere the user cannot see is a worse
answer than a command they can paste. Resuming also costs tokens against their
account, which is a decision that belongs to the person, not to a web page.

WHETHER IT CAN BE RESUMED IS CHECKED, NOT ASSUMED. Claude Code stores transcripts
under ~/.claude/projects/<slugged-cwd>/<session>.jsonl and they do not live
forever. Offering a resume for a transcript that has been cleaned up is a command
that fails in the user's terminal with an error about a session id they have
never seen, so the answer says `resumable` and the UI can say why not.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter

from bgate_core import db
from bgate_ui import api, dispatch
from bgate_ui.deps import root

router = APIRouter()


def _project_slug(project_root: Path) -> str:
    """Claude Code's directory name for a project root.

    Its own scheme: the absolute path with every character that is not a letter,
    digit or dash replaced by a dash. Derived here rather than guessed at from
    one example - `C:\\Users\\adria\\Desktop\\bg-testbed` becomes
    `C--Users-adria-Desktop-bg-testbed`, which is drive letter, colon and both
    separators all collapsing to single dashes.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", str(project_root))


def _transcript(project_root: Path, session_id: str) -> Path | None:
    """Where Claude Code kept this session, if it still has it."""
    if not session_id:
        return None
    home = Path(os.path.expanduser("~")) / ".claude" / "projects"
    candidate = home / _project_slug(project_root) / f"{session_id}.jsonl"
    if candidate.is_file():
        return candidate
    # THE SLUG IS A GUESS ABOUT SOMEBODY ELSE'S SCHEME, so a miss falls back to
    # a search rather than reporting "not resumable" at a session that is right
    # there. Bounded to the project directories, and only on the miss.
    try:
        for found in home.glob(f"*/{session_id}.jsonl"):
            return found
    except OSError:
        pass
    return None


@router.get("/api/agents/{item_id}/session")
def agent_session(item_id: int) -> dict:
    """The Claude session behind one work item, and how to resume it."""
    r = root()
    conn = db.connect(r)
    row = conn.execute(
        "SELECT id, seat, title, status FROM work_item WHERE id = ?",
        (int(item_id),)).fetchone()
    if row is None:
        raise api.not_found(f"no work item #{item_id}", item_id=item_id)

    # limit=0 is the whole ring: the id is on the first line of the run, and a
    # windowed read of the last few steps can be past it.
    feed = dispatch.read_activity(str(r), int(item_id), limit=0)
    session_id = str(feed.get("session_id") or "")
    path = _transcript(Path(r), session_id)

    if not session_id:
        why = ("this run has no Claude session recorded - it either predates "
               "session capture or never started")
    elif path is None:
        why = ("Claude no longer has this session's transcript, so it cannot "
               "be resumed")
    else:
        why = ""

    return api.ok({
        "item_id": int(row["id"]),
        "seat": row["seat"],
        "title": row["title"],
        "status": row["status"],
        "session_id": session_id,
        "running": bool(feed.get("running")),
        "resumable": bool(session_id and path),
        "reason": why,
        # RUN IT FROM THE PROJECT ROOT. Claude Code scopes sessions per working
        # directory, so resuming from somewhere else does not find this one.
        "cwd": str(r),
        "command": f"claude --resume {session_id}" if session_id else "",
    })
