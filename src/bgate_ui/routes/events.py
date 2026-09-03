"""The event log over HTTP — what happened, and what you have not seen yet.

``bgate_core.store.events`` records every consequential transition in id order, and
before this router nothing could read it from the browser: the console surfaced a
finished item only while the right view happened to be open, and only for
``signoff.hours``. Two endpoints close that — one read that answers "what has
happened since I last looked", and one write that records that a human has now
looked.

THE UNREAD COUNT IS NOT DERIVED FROM THE POLL. The console polls with
``since=<the seq it last rendered>``, which walks forward on every tick; the bell
has to count against a cursor that only moves when a human dismisses it. So the
read position is its own stored cursor (consumer ``ui``) and the count is a COUNT
against it, independent of whatever window this request asked for. Deriving the
badge from the polled batch is how a bell reads 0 because the poller already
consumed the events.

WHY THIS FILE HAS SQL IN IT. ``events.py`` exposes a forward cursor read, which
is the right shape for a subscriber and the wrong shape for two things a drawer
needs: an exact unread COUNT (pulling up to 1000 rows per poll to call ``len()``
on them is worse), and the NEWEST n events for a cold open. A cold drawer asking
``since=0`` on a fortnight of history gets the two-hundred OLDEST events, i.e.
the least interesting screenful in the database. Both queries live here rather
than in core because core belongs to another change in flight; they are guarded
the same way core's are, and they should move into ``events.py`` when it is next
touched.

Every read fails soft. A project migrated before the ``event`` table existed, or
one whose DB is locked by four MCP servers, must render an empty drawer rather
than a blank panel with a 500 behind it.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from bgate_core.store import db as _db
from bgate_core.store import events as _events
from bgate_core.store import settings as _settings
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# The bell's read position. One cursor per project, not per person — see the
# plan's non-goals: this is a single-operator tool and a per-user read state
# would need accounts to hang off.
UI_CONSUMER = "ui"


def _payload(text: str) -> dict:
    """Stored JSON -> dict, always. Mirrors ``events._decode`` rather than
    importing it: a private name across a module boundary breaks quietly, and a
    consumer that gets a string where it indexed a dict throws in the template."""
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _wanted(kinds: str) -> list[str]:
    """Parse the ``kinds`` query parameter (comma-separated, order preserved).

    Comma-separated rather than repeated ``?kinds=`` params because that is what
    a checkbox list serialises to in one string and what ``settings`` already
    accepts for ``notify.kinds`` — one spelling for the same list everywhere.
    """
    out = [part.strip() for part in str(kinds or "").split(",")]
    return list(dict.fromkeys([part for part in out if part]))


def _notify_kinds(project) -> list[str]:
    """Which kinds are worth ringing about, per ``notify.kinds``.

    AN EMPTY LIST MEANS NOTHING RINGS, and it has to, because
    ``bgate_ui.agents.followup`` reads the same setting and sends no notice for a kind
    that is not on it. "Empty means everything" here would have made unchecking
    every box light the badge for every event while the webhook stayed silent —
    one value, two opposite behaviours. The bell's own mute is
    ``notify.in_app``; this list is which kinds count.

    A read that FAILS falls back to the shipped default rather than to either
    extreme: a bell that goes quiet looks identical to nothing having happened,
    and a bell that rings for everything gets muted. The default is what the
    code would have used anyway.
    """
    try:
        value = _settings.get(project, "notify.kinds")
    except Exception:
        try:
            value = _settings.BY_KEY["notify.kinds"].default
        except Exception:
            return []
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value if str(part).strip()]
    return _wanted(str(value))


def _in_app(project) -> bool:
    try:
        return bool(_settings.get(project, "notify.in_app"))
    except Exception:
        return True


def _unread(project, read_seq: int, notify_kinds: list[str]) -> dict:
    """Counts of events newer than ``read_seq``: total, ringing, and per kind.

    One GROUP BY instead of a row fetch, so the badge is exact at any log size
    and costs the same on every poll. Returns zeros rather than raising — a
    dashboard that cannot count is still a dashboard.
    """
    per_kind: dict[str, int] = {}
    try:
        rows = _db.connect(project).execute(
            "SELECT kind, COUNT(*) AS n FROM event WHERE id > ? GROUP BY kind",
            (max(0, int(read_seq or 0)),)).fetchall()
        per_kind = {r["kind"]: int(r["n"]) for r in rows}
    except Exception:
        per_kind = {}
    total = sum(per_kind.values())
    # No "empty means everything" branch: see _notify_kinds. An empty list rings
    # for nothing, which is the same thing the follow-up router does with it.
    ringing = sum(n for kind, n in per_kind.items() if kind in notify_kinds)
    return {"unread": ringing, "unread_total": total,
            "unread_by_kind": per_kind}


def _tail(project, kinds: list[str], limit: int) -> dict:
    """The newest ``limit`` events, oldest-first, shaped like ``events.since``.

    What a drawer opening cold needs. ``more`` is deliberately False and
    ``older`` carries the truncation instead: the batch was cut at the OLD end,
    so a consumer that re-polled on ``more`` would be asking for events newer
    than the newest one it just received and would spin for nothing.
    """
    params: list = []
    sql = "SELECT * FROM event"
    if kinds:
        sql += f" WHERE kind IN ({', '.join('?' for _ in kinds)})"
        params.extend(kinds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)  # one extra row detects history behind the window
    try:
        conn = _db.connect(project)
        found = list(conn.execute(sql, params).fetchall())
        head_row = conn.execute("SELECT MAX(id) AS hi FROM event").fetchone()
    except Exception:
        return {"events": [], "seq": 0, "gap": False, "more": False, "head": 0,
                "older": False}
    older = len(found) > limit
    found = list(reversed(found[:limit]))
    out = [{"id": int(r["id"]), "kind": r["kind"], "ref": r["ref"] or "",
            "actor": r["actor"] or "", "payload": _payload(r["payload"]),
            "created_at": r["created_at"]} for r in found]
    head = int((head_row["hi"] if head_row else 0) or 0)
    return {"events": out, "seq": (out[-1]["id"] if out else 0), "gap": False,
            "more": False, "head": head, "older": older}


# ---------------------------------------------------------------------------
# The push channel
# ---------------------------------------------------------------------------
# ONE SSE STREAM INSTEAD OF FORTY POLLS. Every panel that only wanted to notice
# that something had changed was asking the server every 1-5 seconds whether it
# had; the event table already records every consequential transition in id
# order, so the browser can hold one connection and refetch on arrival instead.
#
# The wire format is plain SSE: ``id:`` is the event row id, so a browser that
# drops and reconnects sends ``Last-Event-ID`` and resumes exactly where it was;
# ``event:`` is the kind, so a listener can subscribe per kind. A comment line
# every PING_S keeps the connection from being idled out by a proxy or the
# WebView2 host. The first frame is ``hello``, carrying the head id (which also
# becomes the browser's Last-Event-ID) and the kinds the client should listen
# for. A kind seen mid-stream that was not announced is announced first
# (``vocabulary``) - EventSource only dispatches named events to a listener
# registered for that name, and the vocabulary in core is advisory.
PING_S = 15.0
TICK_S = 0.5
STREAM_BATCH = 200


def _sse(event: str, data: dict, event_id: Optional[int] = None) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    head = f"event: {event}\n"
    if event_id is not None:
        head += f"id: {event_id}\n"
    return head + "data: " + text + "\n\n"


def _known_kinds(project) -> list[str]:
    """The vocabulary plus every kind the log has actually recorded."""
    out = list(_events.KINDS)
    try:
        rows = _db.connect(project).execute(
            "SELECT DISTINCT kind FROM event").fetchall()
        for r in rows:
            if r["kind"] and r["kind"] not in out:
                out.append(r["kind"])
    except Exception:
        pass
    return out


def _resume_from(request: Request, after: Optional[int]) -> Optional[int]:
    """Where a subscriber wants to start: the header wins over the query."""
    raw = request.headers.get("last-event-id")
    if raw is None or not str(raw).strip():
        return after
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return after


async def _stream(request: Request, project, start: Optional[int],
                  wanted: list[str], limit: int) -> AsyncIterator[str]:
    """Rows after ``start`` as they land, forever, or until the client goes.

    ``start`` None means "from now": the backlog is not replayed to a fresh
    tab - that is what the JSON read is for. ``limit`` > 0 ends the stream
    after that many rows, which is what makes it testable and curl-able.
    """
    head = await asyncio.to_thread(_events.head, project)
    seq = head if start is None else min(start, head)
    known = await asyncio.to_thread(_known_kinds, project)
    gap = False
    if start is not None:
        probe = await asyncio.to_thread(_events.since, project, seq, wanted, 1)
        gap = bool(probe.get("gap"))
    yield _sse("hello", {"head": head, "seq": seq, "kinds": known, "gap": gap,
                         "ping_s": PING_S}, event_id=seq)
    sent = 0
    last_ping = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        batch = await asyncio.to_thread(
            _events.since, project, seq, wanted, STREAM_BATCH)
        for ev in batch.get("events") or []:
            kind = str(ev.get("kind") or "")
            if kind not in known:
                known.append(kind)
                yield _sse("vocabulary", {"kinds": [kind]})
            yield _sse(kind, ev, event_id=int(ev["id"]))
            seq = int(ev["id"])
            sent += 1
            if limit and sent >= limit:
                return
        if batch.get("more"):
            continue
        now = time.monotonic()
        if now - last_ping >= PING_S:
            last_ping = now
            yield ": ping\n\n"
        await asyncio.sleep(TICK_S)


def _stream_response(request: Request, after: Optional[int], kinds: str,
                     limit: int) -> StreamingResponse:
    project = root()
    gen = _stream(request, project, _resume_from(request, after),
                  _wanted(kinds), max(0, int(limit or 0)))
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


def _wants_stream(request: Request) -> bool:
    return "text/event-stream" in (request.headers.get("accept") or "").lower()


@router.get("/api/events/stream")
def events_stream(request: Request,
                  after: Optional[int] = Query(None, ge=0),
                  kinds: str = Query(""),
                  limit: int = Query(0, ge=0)):
    """The push channel, at its own path. GET, same-origin, no token: it is a
    read, and the guard exempts reads on purpose (a viewer may look).

    ONLY FOR A CLIENT THAT ASKED FOR A STREAM. A plain GET - a browser
    address bar, a route smoke test - would otherwise hang on a response that
    never ends; it gets a 406 that says how to ask instead.
    """
    if not _wants_stream(request):
        raise api.ApiError(406, "send Accept: text/event-stream to subscribe; "
                                "GET /api/events is the JSON read",
                           code="not_acceptable")
    return _stream_response(request, after, kinds, limit)


@router.get("/api/events")
def events_feed(request: Request,
                      since: Optional[int] = Query(None, ge=0),
                      kinds: str = Query(""),
                      limit: int = Query(_events.DEFAULT_LIMIT, ge=1,
                                         le=_events.MAX_LIMIT)):
    """Events for a drawer or a subscriber, plus the unread count for the bell.

    ``Accept: text/event-stream`` - what EventSource sends - gets the push
    channel above at this path; ``since`` is then the resume point. Everything
    else is the JSON read described below.

    ``since`` OMITTED means the most recent ``limit`` events (``tail: true``) —
    what a panel wants the first time it opens. ``since=<n>``, including 0, is
    the cursor read: everything after n, oldest first, so a poller walks forward
    and never re-renders what it already showed. Those are different questions
    and conflating them is why a cold drawer would otherwise show the oldest
    fortnight-old rows in the table.

    ``kinds`` is a comma-separated filter over the event vocabulary; unknown
    names are reported in ``unknown_kinds`` rather than refused, because the
    vocabulary is advisory in core and a filter that 400s would break a UI
    against a newer log.
    """
    if _wants_stream(request):
        return _stream_response(request, since, kinds, 0)
    project = root()
    wanted = _wanted(kinds)
    if since is None:
        batch = _tail(project, wanted, limit)
        batch["tail"] = True
    else:
        batch = _events.since(project, seq=int(since), kinds=wanted, limit=limit)
        batch["tail"] = False
        # A forward read has no window behind it to offer; keep the key present
        # so the UI does not have to branch on which mode produced the payload.
        batch["older"] = False

    notify_kinds = _notify_kinds(project)
    read_seq = _events.cursor_get(project, UI_CONSUMER)
    batch.update(_unread(project, read_seq, notify_kinds))
    batch["read_seq"] = read_seq
    batch["notify_kinds"] = notify_kinds
    batch["in_app"] = _in_app(project)
    batch["vocabulary"] = list(_events.KINDS)
    batch["filter"] = wanted
    batch["unknown_kinds"] = [k for k in wanted if k not in _events.KINDS]
    return api.ok(batch)


@router.post("/api/events/read")
def events_read(payload: Optional[dict] = None) -> dict:
    """Mark the bell read up to ``seq``, and report what is left.

    ``seq`` omitted means "everything currently in the log" — the dismiss-all a
    bell needs, resolved server-side against the head so a client cannot clear
    events it never received. Moving the cursor BACKWARDS is allowed on purpose:
    "mark unread" is a real thing a human does, and the cursor is last-write-wins
    by design in core.

    Without this the drawer would have no read state at all and the badge would
    either count from zero forever or be silently consumed by the poller.
    """
    project = root()
    payload = payload or {}
    head = _events.head(project)
    raw = payload.get("seq")
    if raw is None:
        seq = head
    else:
        try:
            seq = int(raw)
        except (TypeError, ValueError):
            raise api.bad_request(f"seq must be an integer, got {raw!r}")
        if seq < 0:
            raise api.bad_request("seq cannot be negative")
        # Clamped, not refused: a client that read the head a tick before an
        # emit landed is not wrong, and a 400 there would leave the bell stuck.
        seq = min(seq, head)
    _events.cursor_set(project, UI_CONSUMER, seq)
    stored = _events.cursor_get(project, UI_CONSUMER)
    out = {"consumer": UI_CONSUMER, "read_seq": stored, "head": head}
    out.update(_unread(project, stored, _notify_kinds(project)))
    return api.ok(out)
