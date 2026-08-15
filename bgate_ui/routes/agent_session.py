"""Handing a run back to the terminal.

WHAT THIS IS FOR. Every dispatched agent IS a Claude Code session, and Claude
Code keeps that session's transcript on disk. So a run this dashboard is drawing
as cards can also be picked up in a terminal exactly where it left off, with its
whole context - which is what somebody who would rather work in the CLI than in
a web page actually wants, and what "read the log" is a poor substitute for. The
id has been in the log the whole time; nothing was storing it.

IT OPENS A REAL TERMINAL, and that is the point rather than a convenience. A
copyable command was the first version and it is still there for anybody who
wants it, but `claude --resume` is an INTERACTIVE program: the useful thing is
a window with the session already in it, not a string to paste. This dashboard
binds to loopback, holds a token, and already spawns agent processes on this
machine - opening a terminal is inside the powers it already has, not a new one.

WHAT IT MAY LAUNCH IS FIXED, WHICH IS THE WHOLE SAFETY STORY. The only program
this can start is the `claude` binary that `runners.find_claude()` resolves, its
only argument is `--resume <id>` where the id has been matched against a UUID
pattern, and the argv is built as a LIST so no shell parses any of it. A request
cannot name a program, add a flag, or smuggle a metacharacter, because nothing
it sends is ever concatenated into a command line.

STARTING COSTS MONEY AND SAYS SO. Both buttons spend tokens on the user's
account once they type into the window, which is why they are buttons a person
presses rather than something that happens when a panel opens.

WHETHER IT CAN BE RESUMED IS CHECKED, NOT ASSUMED. Claude Code stores transcripts
under ~/.claude/projects/<slugged-cwd>/<session>.jsonl and they do not live
forever. Offering a resume for a transcript that has been cleaned up is a command
that fails in the user's terminal with an error about a session id they have
never seen, so the answer says `resumable` and the UI can say why not.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Body

from bgate_core import db, proc
from bgate_ui import api, dispatch, runners
from bgate_ui.deps import root

router = APIRouter()

# A Claude session id, and nothing else, ever reaches a command line. Matched
# rather than escaped: an id either looks like this or the request is refused,
# which is a smaller thing to get right than quoting.
_SESSION_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


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


def _terminal_argv(cwd: Path, inner: list[str]) -> list[str] | None:
    """A platform terminal that will run `inner` and STAY OPEN.

    Staying open is the requirement that rules most of these out. `claude` is
    interactive, so a launcher that runs it and closes on exit is fine, but one
    that closes the window the instant the program asks for input is not - which
    is why the Windows path goes through `cmd /k` rather than `cmd /c`.

    NOTHING FROM THE REQUEST IS IN HERE. `inner` is built by the caller from a
    resolved executable path and a pattern-matched session id, and every element
    stays a separate argv entry, so no shell ever parses any of it.
    """
    if sys.platform == "win32":
        # Windows Terminal when it is installed, because it is what a Windows 11
        # user actually has open; plain conhost otherwise.
        wt = shutil.which("wt.exe")
        if wt:
            return [wt, "-d", str(cwd), "cmd", "/k", *inner]
        cmd = shutil.which("cmd.exe") or "cmd.exe"
        return [cmd, "/c", "start", "", cmd, "/k", *inner]
    if sys.platform == "darwin":
        # `open -a Terminal` takes a FILE, not a command, so there is no way to
        # pass argv through it without writing a script - which is a temp file
        # holding a command, i.e. the shell injection this design avoids.
        return None
    for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                       ("konsole", "-e"), ("xterm", "-e")):
        found = shutil.which(term)
        if found:
            return [found, flag, *inner]
    return None


def _launch(project_root: Path, inner_args: list[str]) -> dict:
    """Open a terminal in `project_root` running claude with `inner_args`."""
    exe = runners.find_claude()
    if not exe:
        raise api.ApiError(503, "the claude CLI is not on PATH", detail={
            "fix": "install Claude Code, or open a terminal yourself and run "
                   "the command shown"})
    inner = [exe, *inner_args]
    argv = _terminal_argv(project_root, inner)
    if argv is None:
        raise api.ApiError(501, "no terminal this dashboard knows how to open "
                                "on this platform", detail={
            "fix": "run the command yourself",
            "command": " ".join(inner)})
    try:
        # THE TERMINAL IS NOT OUR CHILD TO SUPERVISE. It outlives this request by
        # design, so its streams are detached; inheriting them would tie an
        # interactive window's lifetime to a web request that ends immediately.
        proc.popen(argv, cwd=str(project_root), close_fds=True)
    except OSError as exc:
        raise api.ApiError(500, f"could not open a terminal: {exc}",
                           detail={"argv": argv[0]}) from exc
    return {"cwd": str(project_root), "opened": Path(argv[0]).name}


@router.post("/api/session/open")
def open_project_session() -> dict:
    """Open a terminal on the PROJECT, as a new Claude session.

    SEPARATE FROM THE PER-ITEM ROUTE BECAUSE STARTING NEEDS NO ITEM. Continuing
    is about one run and has to name it; starting is about the project, and
    requiring a run to exist first meant a console with nothing in it yet - the
    exact moment somebody wants a terminal - had no way to open one.
    """
    r = root()
    return api.ok({"mode": "start", "session_id": "", **_launch(Path(r), [])})


@router.post("/api/agents/{item_id}/session/open")
def open_session(item_id: int, body: dict = Body(default={})) -> dict:
    """Open a terminal on this run: `continue` resumes it, `start` opens a new
    session in the same project.

    Returns what it launched. On a platform with no terminal it can drive, it
    refuses and hands back the command instead - a refusal that names the
    command is more useful than a spinner that never resolves.
    """
    r = root()
    mode = str(body.get("mode") or "continue").strip().lower()
    if mode not in ("continue", "start"):
        raise api.ApiError(422, "mode must be 'continue' or 'start'",
                           detail={"mode": mode})

    inner: list[str] = []
    session_id = ""
    if mode == "continue":
        info = agent_session(item_id)["data"]
        session_id = str(info.get("session_id") or "")
        if not info.get("resumable"):
            raise api.ApiError(409, info.get("reason") or "this run cannot be resumed",
                               detail={"item_id": item_id})
        # BELT AND BRACES. The id came from our own log parse, not from the
        # request - this refuses anyway, because the one thing that must never
        # be true is an unvalidated string reaching a process launch.
        if not _SESSION_RE.match(session_id):
            raise api.ApiError(422, "that session id is not a session id",
                               detail={"session_id": session_id[:64]})
        inner = ["--resume", session_id]

    return api.ok({"mode": mode, "session_id": session_id,
                   **_launch(Path(r), inner)})
