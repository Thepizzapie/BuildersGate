"""The event bus — every consequential transition, readable in id order.

WHAT WAS BROKEN. ``queue._notify`` has written every status transition to
``.bgate/notify.jsonl`` since the beginning, and its docstring promises a
tail/long-poll surface. Nothing ever read it, so a finished item told the
director nothing, told a chain nothing, and told the human nothing unless they
happened to have the console open on the right view. Three separate features
(notify me, debrief the director, badge the window) each need the same thing:
"what has happened since I last looked".

A TABLE, NOT THE FILE. The obvious move is to give notify.jsonl a cursor, and it
cannot be done correctly here. That file is a bare ``open(..., "a")`` executed by
whichever process flips the status — ``queue_complete`` runs in the MCP server
process (one per Claude session, several live at once), while the reaper and the
stranded-run settler run in the dashboard process. Multi-writer with no lock: a
monotonic sequence number in that file needs a cross-process lock that does not
exist, and interleaved partial lines are a real Windows failure mode. SQLite in
WAL mode with a busy timeout is the ONE thing in this repo that is already safe
against many writers, and ``INTEGER PRIMARY KEY AUTOINCREMENT`` hands us the
cursor for free — ids are never reused, so a cursor keeps its meaning even after
a prune.

``notify.jsonl`` keeps being written, unchanged. Its docstring advertises an
interface; repurposing it would break a documented surface for no gain. This
table is purely additive.

GAPS ARE REPORTED, NEVER SKIPPED. :func:`prune` deletes old rows, so a consumer
that was away long enough can hold a cursor pointing into a range that no longer
exists. Silently starting it at the oldest surviving row is how "eleven items
finished while you were away" becomes "nothing happened". :func:`since` says
``gap: True`` instead, and the consumer decides whether to collapse, warn, or
ignore.

BEST-EFFORT ON THE WRITE PATH, like :mod:`bgate_core.board.activity`. Every emit sits
inside a real operation — a completion, an approval, a chain being filed — and a
lost event is a cosmetic loss while a raised exception out of a status change is
work dropped on the floor. :func:`emit` therefore never raises; it returns 0 when
it could not record.
"""
from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

from ..board import activity
from . import db, workspace as _ws
from .util import rows

# The vocabulary. Kept small on purpose: every kind here is something a
# subscriber can be expected to have an opinion about, and a bus whose kinds are
# invented per call site cannot be filtered by a settings checkbox.
#
# Advisory rather than enforced — emit() accepts any dotted string, because the
# alternative is a write path that drops (silently, since it is best-effort) the
# one event a new feature cared about. Consumers render choices from this tuple.
KINDS = (
    "item.done",         # an agent finished and nothing is holding the work
    "item.review",       # finished, parked for a human under the builder's gate
    "item.failed",       # the agent (or the watchdog) says it could not finish
    "item.stopped",      # a HUMAN ended the run — banked as failed, not a crash
    "item.approved",     # the human released a held item
    "item.rejected",     # the human sent it back with a reason
    "item.aging",        # heartbeat: nobody has looked at a 'review' item
    # A MID-RUN CORRECTION, ON THE ITEM'S OWN RECORD. The steer inbox is a
    # spool that is consumed and deleted, so before this a correction existed
    # only until it was read - and a later reader could not tell which
    # corrections shaped a result, or that any had. The most influential input
    # to a run was the one input the run did not record.
    "item.steered",      # a running agent was corrected mid-run
    # A brief whose premise was MEASURED FALSE by the agent working it. The
    # most valuable thing agents did in the benchmark, and it survived only as
    # prose in a result note - see queue.premise_refuted.
    "item.premise_refuted",
    "artifact.candidate",# a generated candidate is waiting on a human decision
    "artifact.reviewed", # that decision was made — approved, rejected, integrated
    "chain.filed",       # a dependent group was queued as one ordered chain
    "chain.advanced",    # a link landed and the next one became ready
    "chain.stalled",     # heartbeat: the head of a chain has not moved
    "gate.mode",         # who signs off changed
    "settings.guard",    # a switch that widens a safety guard was changed
    "style.trained",     # a project trained a style from its pinned anchors
    "budget.refused",    # a dispatch was refused for spend
    # A FLOOR refusal — one that stops the WHOLE board rather than one
    # item. Today that is the dirty-tree gate, which was pull-only
    # (board_digest.blocked) and so went unnoticed for an hour at a time
    # while two seats idled. Same channel as budget.refused, for the same
    # reason: a board that stopped has to be as loud as a board that
    # failed.
    "dispatch.blocked",  # the board cannot dispatch at all, and why
    "director.question", # ask_human: the director wants a human answer
    "agent.spawned",
    "agent.exited",
    "file.edited",       # a human saved a game source file from the dashboard
)

# Where cursors live. A workspace doc per consumer under one seat, so the set of
# subscribers is discoverable (workspace.list_keys) instead of being spread
# across a column per feature.
CURSOR_SEAT = "consumer"

# A payload is context for a notification, not a place to park a diff. The cap
# exists because the alternative is one runaway result note making the events
# table the biggest thing in the database.
MAX_PAYLOAD = 4000
MAX_KIND = 40
MAX_REF = 200

# Guardrails on since(): a consumer asking for everything must not be able to
# pull the whole table into one JSON response.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _encode(payload: Optional[dict]) -> str:
    """Payload -> JSON text that is always storable.

    ``default=str`` and the fallback both matter: a caller passing a Path, a
    datetime or a sqlite3.Row is a bug that must not become a dropped event on
    the write path of a completion.
    """
    if not payload:
        return "{}"
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps({"_unserializable": repr(payload)[:200]})
    if len(text) > MAX_PAYLOAD:
        # Truncating the JSON would produce a row that cannot be parsed back, so
        # the oversized payload is replaced by an honest marker instead.
        text = json.dumps({"_truncated": True, "chars": len(text),
                           "head": text[:400]})
    return text


def _decode(text: str) -> dict:
    """Stored JSON -> dict. Always a dict, so consumers can index it blind."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    if isinstance(value, dict):
        return value
    return {"value": value}


def emit(root: str | os.PathLike[str], kind: str, ref: str = "",
         payload: Optional[dict] = None) -> int:
    """Record one event. Returns its id, or 0 if it could not be recorded.

    ``kind`` is a dotted string from :data:`KINDS`; ``ref`` is whatever the event
    is about (an item id, a chain id, a path) as text, so one column serves every
    kind. ``payload`` is stored as JSON text and comes back parsed.

    NEVER RAISES. Callers are status transitions — see the module docstring: an
    event lost to a locked database is a missing line in a drawer, while an
    exception escaping here is an agent's finished work not being marked done.
    The 0 return is the signal for anything that actually needs to know.
    """
    kind = str(kind or "").strip()[:MAX_KIND]
    if not kind:
        return 0
    try:
        actor = activity.current_actor()
    except Exception:
        actor = ""
    try:
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO event (kind, ref, actor, payload) VALUES (?, ?, ?, ?)",
                (kind, str(ref or "")[:MAX_REF], (actor or "")[:120],
                 _encode(payload)))
            return int(cur.lastrowid)
    except Exception:
        return 0


def head(root: str | os.PathLike[str]) -> int:
    """The newest event id (0 on an empty log).

    What a subscriber that deliberately skips the backlog starts from — the
    staleness policy: a router that was down for eight hours must be able to say
    "start at now" rather than replaying eight hours of debriefs.
    """
    try:
        row = db.connect(root).execute("SELECT MAX(id) AS m FROM event").fetchone()
    except Exception:
        return 0
    return int(row["m"] or 0) if row else 0


def since(root: str | os.PathLike[str], seq: int = 0,
          kinds: Iterable[str] = (), limit: int = DEFAULT_LIMIT) -> dict:
    """Events after ``seq``, oldest first, plus where the cursor should land.

    Returns ``{"events": [...], "seq": int, "gap": bool, "more": bool,
    "head": int}``:

      * ``events`` — dicts of ``id, kind, ref, actor, payload (parsed), created_at``
      * ``seq`` — the cursor to store: the last returned id, or the requested
        ``seq`` unchanged when nothing matched. Never the table head, because a
        ``kinds`` filter would then permanently swallow the events a consumer
        starts caring about after it widens the filter.
      * ``gap`` — the requested range has been pruned; see below.
      * ``more`` — the limit truncated the batch, so poll again immediately
        rather than waiting for the next tick. This is also what lets a
        notification collapse ("11 items finished while you were away") instead
        of firing eleven pings.
      * ``head`` — the newest id in the table, for a progress display.

    WHY ``gap`` EXISTS. :func:`prune` drops old rows, so a cursor can point below
    the oldest surviving id. Reporting that is the whole difference between "you
    missed 40 events" and a consumer quietly believing nothing happened. It is
    false for ``seq <= 0``: a consumer with no cursor has not LOST anything, it
    simply has no history, and treating a fresh subscriber as data loss would put
    a permanent "events were dropped" warning in the UI of every new project.
    """
    seq = max(0, int(seq or 0))
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    wanted = [str(k).strip() for k in (kinds or ()) if str(k).strip()]

    try:
        conn = db.connect(root)
        sql = "SELECT * FROM event WHERE id > ?"
        params: list = [seq]
        if wanted:
            sql += f" AND kind IN ({', '.join('?' for _ in wanted)})"
            params.extend(wanted)
        # limit + 1 detects truncation without a second COUNT query.
        sql += " ORDER BY id LIMIT ?"
        params.append(limit + 1)
        found = rows(conn.execute(sql, params))
        bounds = conn.execute(
            "SELECT MIN(id) AS lo, MAX(id) AS hi FROM event").fetchone()
    except Exception:
        # A subscriber must not take the dashboard down because the log could not
        # be read; an empty batch reads as "nothing new" and it polls again.
        return {"events": [], "seq": seq, "gap": False, "more": False, "head": 0}

    more = len(found) > limit
    found = found[:limit]
    out = [{"id": int(r["id"]), "kind": r["kind"], "ref": r["ref"] or "",
            "actor": r["actor"] or "", "payload": _decode(r["payload"]),
            "created_at": r["created_at"]} for r in found]

    lo = int(bounds["lo"] or 0) if bounds else 0
    hi = int(bounds["hi"] or 0) if bounds else 0
    # lo > seq + 1 rather than lo > seq: a cursor sitting exactly on the oldest
    # surviving row lost nothing, and reporting a gap there would fire on every
    # poll of a healthy pruned log.
    gap = bool(seq > 0 and lo and lo > seq + 1)
    return {"events": out, "seq": (out[-1]["id"] if out else seq),
            "gap": gap, "more": more, "head": hi}


def _cursor_key(consumer: str) -> str:
    key = str(consumer or "").strip()[:60]
    if not key:
        raise ValueError("a cursor needs a consumer name")
    return key


def cursor_get(root: str | os.PathLike[str], consumer: str) -> int:
    """Where ``consumer`` last got to. 0 when it has never run.

    0 means "the whole retained log", which is the right default for a drawer and
    the wrong one for anything that spends money — a subscriber with a leash
    starts from :func:`head` instead. Never raises: a missing or corrupt cursor
    doc reads as 0, because refusing to run is worse than replaying.
    """
    try:
        doc = _ws.get(root, CURSOR_SEAT, _cursor_key(consumer), {})
        return max(0, int(doc.get("seq") or 0))
    except Exception:
        return 0


def cursor_set(root: str | os.PathLike[str], consumer: str, seq: int) -> None:
    """Store ``consumer``'s position. Best-effort, and deliberately not versioned.

    A subscriber that acts and then dies before this lands will act again on
    restart — delivery is at-least-once, which is why every subscriber carries
    its own idempotency guard. Writing the cursor is therefore not worth failing
    a tick over, and it is last-write-wins on purpose: a deliberate replay (set
    it backwards) must be allowed, and there is one process per consumer name.
    """
    try:
        _ws.set(root, CURSOR_SEAT, _cursor_key(consumer),
                {"seq": max(0, int(seq or 0)),
                 "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())})
    except Exception:
        pass


def cursors(root: str | os.PathLike[str]) -> list[dict]:
    """Every known subscriber and its position — what makes a stuck consumer
    visible instead of being inferred from events that stopped arriving."""
    out: list[dict] = []
    try:
        keys = _ws.list_keys(root, CURSOR_SEAT)
    except Exception:
        return out
    for row in keys:
        out.append({"consumer": row["key"], "seq": cursor_get(root, row["key"]),
                    "updated_at": row.get("updated_at") or ""})
    return out


def prune(root: str | os.PathLike[str], keep_days: int = 14) -> int:
    """Delete events older than ``keep_days``. Returns how many went.

    The log is a notification substrate, not the audit trail — `activity` is the
    ledger that keeps history — so it is bounded by time rather than kept
    forever. Cursors are NOT consulted: a subscriber that has been down for two
    weeks is not going to be caught up by holding rows for it, and :func:`since`
    already reports the gap honestly. ``keep_days`` is clamped to at least 1 so a
    settings field arriving as 0 or "" cannot silently empty the log.
    """
    days = max(1, int(keep_days or 0))
    try:
        with db.tx(root) as conn:
            cur = conn.execute(
                f"DELETE FROM event WHERE created_at < datetime('now', '-{days} days')")
            return int(cur.rowcount or 0)
    except Exception:
        return 0  # a failed prune costs disk, not correctness
