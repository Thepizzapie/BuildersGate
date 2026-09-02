"""The project's thread across sessions — in-flight state, nothing else.

WHAT THIS IS FOR. The board records work that was DISPATCHED. The bible records
decisions that were SETTLED. Between those two sits everything a top-level
session knows and nothing writes down: what it was halfway through, what it
decided and why, what it deliberately did not do. A session ends and that
evaporates, so the next one re-derives it or, worse, re-opens a question that was
already answered. `seat_brief`'s own rules make this argument for spawned workers
-- "a successor must resume from ONE file read, never from archaeology" -- and
then apply it only to seat workers, scoped to one work item. This is the same
rule for the participant that had no version of it.

APPEND AS YOU GO; DO NOT GENERATE AT THE END. The obvious design is a session-end
hook that writes a summary, and it is wrong for the sessions that matter most: a
killed process, a crash, or a closed window fires nothing, and those are exactly
the sessions worth resuming. `.bgate/progress/<item>.jsonl` already reached this
conclusion for the same reason -- "agents die mid-flight constantly (interrupts
are normal usage)". So this is an append-only trail: one line per note, flushed
on write, useful after any death at any point.

ONE THREAD PER PROJECT, NOT ONE PER SESSION. The first draft filed a trail per
session id, and that quietly required the MCP server and the PreToolUse/
SessionStart hooks to agree on what "this session" is. They cannot: they are
separate processes, the harness hands a session_id to HOOKS only, and the server
has no way to learn it. Every workaround (a pointer file, a ppid, an env var
nobody sets) either breaks under the concurrent agents this system is built for,
or silently writes one session's notes into another's file.

An append-only line log needs no such agreement -- concurrent writers interleave
safely by construction -- and with several agents working at once "the last
twelve things anyone recorded about this project" is a better answer than "the
previous session's file" anyway. Each line stamps its own `actor` and, when the
writer happens to know it, `session`, so who-said-what survives without the
filename having to carry it.

WHAT IT MUST NOT BECOME: a third decision store. The bible IS the settled-decision
record -- the director's own mission says every settled decision names its
acceptance test and what it deliberately leaves dark -- and the queue IS the work
list. A handoff that grows its own copies of those competes with them and they
drift. So the line is drawn at IN-FLIGHT:

    bible   settled canon. Cite it from here; never restate it here.
    queue   dispatched work. Reference item ids; never duplicate briefs.
    handoff what is unfinished, undecided, or deliberately deferred RIGHT NOW.

A `decision` note whose content belongs in the bible is a note that should have
been a bible_add, and `refs` is how it says so.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from ..store import db

DIRNAME = "handoff"
FILENAME = "thread.jsonl"

# Deliberately few, and each earns its place by being something a NEXT session
# has to know and cannot read off the board.
#
#   state     where things stand; what is half-done.
#   decision  a call made, with the reason. `refs` cites the bible when settled.
#   deferred  chosen NOT to do, and why. The director's mission is explicit that
#             "a deferral nobody labelled gets 'fixed' as a bug", and an
#             unlabelled deferral is the most expensive thing to lose between
#             sessions — the next agent finds it and "fixes" it.
#   blocker   what is in the way, and who owns it.
#   next      the very next action. Same field the progress trail carries.
KINDS = ("state", "decision", "deferred", "blocker", "next")

MAX_TEXT = 2000
MAX_REFS = 12

# What a resuming session is shown. Enough to pick the thread up, bounded because
# this is injected into EVERY session and an unbounded paste is how a useful
# block becomes one people stop reading. handoff_read returns more on request.
DIGEST_NOTES = 12


def path_for(root: str | os.PathLike[str]) -> Path:
    return Path(root) / db.DB_DIRNAME / DIRNAME / FILENAME


def note(root: str | os.PathLike[str], kind: str, text: str,
         refs: Optional[list] = None, actor: str = "",
         session: str = "") -> dict:
    """Append one line. Returns the record as written.

    Raises on a bad kind or empty text: this is called by an agent, and a note
    silently dropped for being malformed is worse than an error it can read.
    """
    kind = str(kind).strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; kinds are {KINDS}")
    text = str(text).strip()
    if not text:
        raise ValueError("an empty handoff note helps nobody")
    record = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "text": text[:MAX_TEXT],
        "refs": [str(r)[:200] for r in (refs or [])][:MAX_REFS],
        # BGATE_ACTOR is what dispatch.py stamps on a spawned agent
        # (agent:item-<id>), so a seat worker's notes are attributable without
        # the caller passing anything.
        "actor": str(actor or os.environ.get("BGATE_ACTOR", "") or "director")[:80],
        "session": str(session or "")[:64],
    }
    target = path_for(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Append + flush per line. A trail whose last entry is lost to a buffer is a
    # trail that fails in exactly the case it was built for. Line-buffered append
    # is also what makes concurrent writers safe without a lock.
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
    return record


def read(root: str | os.PathLike[str], limit: int = 0,
         kind: str = "") -> list[dict]:
    """The thread, oldest first. `limit` takes the most recent N. Never raises.

    A half-written final line (the process died mid-write) is skipped rather
    than fatal — losing one note must not cost the other forty.
    """
    out: list[dict] = []
    try:
        raw = path_for(root).read_text(encoding="utf-8")
    except OSError:
        return out
    want = str(kind).strip().lower()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if want and rec.get("kind") != want:
            continue
        out.append(rec)
    return out[-limit:] if limit and limit > 0 else out


def digest(root: str | os.PathLike[str], limit: int = DIGEST_NOTES) -> list[str]:
    """The tail of the thread as lines for a SessionStart block.

    Chronological rather than grouped by kind: the thread is a narrative, and
    re-ordering it into buckets loses the one thing a narrative has — which
    decision came before which deferral.
    """
    trail = read(root, limit=limit)
    if not trail:
        return []
    total = len(read(root))
    head = f"THREAD   last {len(trail)} of {total} note(s)"
    if total > len(trail):
        head += "  (handoff_read for the rest)"
    lines = [head]
    for rec in trail:
        who = str(rec.get("actor") or "?")
        who = "" if who == "director" else f" <{who}>"
        ref = f"  [{', '.join(rec['refs'])}]" if rec.get("refs") else ""
        lines.append(f"  {str(rec.get('kind', '?')):9s}"
                     f"{str(rec.get('text', ''))[:150]}{who}{ref}")
    return lines
