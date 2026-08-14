"""The Agents console — one conversation, one live graph, one poll.

The Agents view used to be a composer over four kanban lanes: you typed a task,
picked the seat yourself, and then watched cards move. That is a to-do list with
a dispatch button. What the floor actually does is a CONVERSATION — you say what
you want, the director decides who does it, and work hands off between seats —
and none of that shape was visible.

So this module backs a different reading of the same data:

  * ``POST /api/console/say`` — a message to the director. It becomes a work
    item (``source='chat'``) and is dispatched immediately. The brief tells the
    director to answer the human AND to delegate the pieces, stamping each child
    with the same ``DELEGATED-FROM: #id`` line the delegate endpoint uses — so
    the children of a sentence are recoverable from the database after a reload,
    not held in a JS variable.

  * ``GET /api/console/state`` — everything the view paints, in ONE request:
    the conversation turns with their live replies, the board, the delegation
    lineage, the running agents WITH their last few steps, the open approval
    gates, the auto-deploy switch, and a floor tally. The old view needed
    /api/queue + /api/agents + one /api/agent-activity per live agent every
    3.5 seconds and still could not draw an edge between two items.

  * ``POST /api/console/autopilot`` — the auto-deploy switch (bgate_ui.autodeploy),
    plus an immediate tick so flipping it on does something visible now rather
    than up to four seconds later.

Bounded on purpose: this is polled. The board window is capped, steps are the
last few per live agent, and briefs are previews.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from bgate_core import activity as _activity
from bgate_core import artifacts as _artifacts
from bgate_core import db
from bgate_core import queue as _queue
from bgate_core import steerbox as _steerbox
from bgate_core import workspace as _ws
from bgate_ui import api as _api
from bgate_ui import autodeploy as _autodeploy
from bgate_ui import dispatch as _dispatch
from bgate_ui import phases as _phases
from bgate_ui.deps import root
from bgate_ui.routes.orchestrator import DELEGATED_FROM, _lineage

router = APIRouter()

CHAT_SOURCE = "chat"
# Where the "a human has seen this claim" record lives. Same store as the
# auto-deploy switch: per project, survives a restart, no schema change.
SEAT = "director"
SIGNOFF_KEY = "signoffs"
# Where the conversation's cut line and its archived sessions live. Clearing
# the console must not delete anything: a turn is a work item with a log on
# disk, and "clear" meaning "destroy the record of what was asked and what the
# agent did" would be the worst possible reading of the word.
SESSION_KEY = "console"
# How far back a finished item still asks for a sign-off. This is a LIVE
# instrument: work that landed while you were watching wants a decision, work
# from last Tuesday is history and belongs to the timeline. Without the window,
# opening the console on an old project drew a wall of approval nodes over runs
# nobody is thinking about — which is the exact failure the candidate gate had.
SIGNOFF_HOURS = 8


def _signoff_hours(root_dir) -> float:
    """The sign-off window, from the registry (``signoff.hours``).

    Read per request rather than captured in the constant above: the constant is
    now only the fallback, and a project that raised the window because it is
    catching up on a weekend of work must not have to restart the dashboard for
    it. Without this the Settings panel offered the field and nothing read it —
    a switch that silently does nothing is worse than no switch.
    """
    try:
        from bgate_core import settings as _settings
        return max(0.25, float(_settings.get(root_dir, "signoff.hours")))
    except Exception:
        return float(SIGNOFF_HOURS)

# The human's message, verbatim, inside the brief. See _chat_brief.
SAID_OPEN = "<<<SAID"
SAID_CLOSE = "SAID>>>"

# The conversation window. A turn is one work item, so this is also how far
# back the graph's "roots" go.
TURNS = 24
# Board window. Open work first — a project with 400 done items must not ship
# all of them to a view that draws maybe forty nodes.
BOARD = 80
# …AND THE MOST RECENTLY CLOSED WORK, WHICH THE WINDOW ABOVE CANNOT REACH.
#
# That ordering ranks `failed` third and everything genuinely finished fourth,
# so on a board carrying 89 failures the whole 80-row window was failures: the
# console's "last closed" list could only ever show things that went wrong, and
# a studio where plenty had landed looked like one where nothing had. This
# second, small window is by TIME and by nothing else, unioned in below.
RECENT_CLOSED = 12
# Steps shown per live agent inside the state payload. The full feed is still
# one /api/agent-activity call away when a node is opened.
STEPS = 6

_CARD_FIELDS = ("id", "seat", "title", "status", "priority", "source",
                "source_ref", "attempts", "total_cost_usd", "result",
                "chain_id", "chain_pos", "depends_on", "approved_by",
                "created_at", "updated_at")


def _card(row) -> dict:
    item = {k: row[k] for k in _CARD_FIELDS}
    brief = row["brief"] or ""
    item["brief_preview"] = brief[:240]
    item["brief_len"] = len(brief)
    item["result"] = (item["result"] or "")[:600]
    return item


# How much of a phase's step feed goes over the wire. MEASURED on a live board:
# 20 phases for one art agent shipped 106KB of a 162KB payload, two thirds of it
# raw step text repeated inside each phase — and the client only ever renders the
# newest phase's tail on the task rail plus one open phase's feed. Every poll of
# every open tab paid for all twenty.
STEP_PHASES = 3      # phases that keep their steps (the ones a rail actually opens)
PHASE_STEPS = 6      # steps kept per phase; the rail renders a tail, not a history

# Rebuilding phases is the expensive half of this endpoint: split() walks the
# whole ring and look() stats every path it finds on disk, per live agent, per
# poll, per tab. Keyed on the run's own step count, so a poll that brings no new
# steps costs a dict lookup and a poll that does rebuilds exactly once for every
# tab watching.
_PHASE_CACHE: dict[tuple, tuple] = {}
_PHASE_LOCK = threading.Lock()
_PHASE_CACHE_MAX = 64


def _trim_phases(phases: list[dict]) -> list[dict]:
    """Drop step text the client cannot show, and SAY it was dropped.

    An older phase shipping `steps: []` silently would render as "nothing
    recorded in this pocket", which is a lie about a run that did work. The count
    rides along so the rail can point at the full log instead.
    """
    out = []
    for i, phase in enumerate(phases or []):
        keep = i >= len(phases) - STEP_PHASES
        held = list(phase.get("steps") or [])
        trimmed = dict(phase)
        trimmed["steps"] = held[-PHASE_STEPS:] if keep else []
        trimmed["steps_dropped"] = len(held) - len(trimmed["steps"])
        out.append(trimmed)
    return out


def _phases_for(root, item_id: int, feed: dict, all_steps: list,
                artifacts: list) -> list[dict]:
    key = (str(root), int(item_id))
    stamp = (int(feed.get("step_count") or 0), len(all_steps), len(artifacts),
             bool(feed.get("running")))
    with _PHASE_LOCK:
        hit = _PHASE_CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    built = _trim_phases(_phases.look(root, _phases.attach(
        _phases.split(all_steps, running=bool(feed.get("running"))),
        artifacts)))
    with _PHASE_LOCK:
        if len(_PHASE_CACHE) >= _PHASE_CACHE_MAX:
            _PHASE_CACHE.clear()      # a finished run never comes back
        _PHASE_CACHE[key] = (stamp, built)
    return built


def _chain_state(conn, items: list[dict]) -> None:
    """Stamp readiness onto the cards, in ONE query for the whole board.

    A chained item that is not ready is indistinguishable from a plain queued one
    on the wire, so the console offered a deploy button whose only possible
    outcome was a refusal. Resolved here rather than per row because the console
    payload is already the expensive call on this page.
    """
    need = {int(it["depends_on"]) for it in items if it.get("depends_on")}
    if not need:
        for it in items:
            it["ready"] = True
        return
    marks = ", ".join("?" * len(need))
    deps = {int(row["id"]): dict(row) for row in conn.execute(
        f"SELECT id, seat, title, status FROM work_item WHERE id IN ({marks})",
        tuple(sorted(need)))}
    for it in items:
        dep = deps.get(int(it["depends_on"] or 0))
        # A missing predecessor (deleted) unblocks rather than strands: an item
        # nobody can release is worse than one that ran a step early.
        it["ready"] = not dep or dep["status"] == "done"
        if not it["ready"]:
            it["waiting_on"] = dep


def _chat_brief(text: str, turn_id: int) -> str:
    """What the director is actually told when a human types a sentence.

    Two jobs, in this order, and the order matters: ANSWER, then delegate. A
    director that silently queues five items and says nothing reads as a
    hung page — the reply is the only thing the human sees immediately.

    The human's own words are fenced. A work item's title is capped at 80
    characters and a paragraph typed into the console is routinely longer, so
    without a fence the transcript could only ever redisplay a truncated first
    line — the message the human actually sent would exist nowhere. The fence
    is also what lets the transcript survive a reload without a second store.
    """
    return (
        "You are the DIRECTOR of this game project, talking to the human who "
        "owns it. They said this to you in the console:\n\n"
        f"{SAID_OPEN}\n{text}\n{SAID_CLOSE}\n\n"
        "YOU ARE A SWITCHBOARD, NOT A RESEARCHER. Answer and route. The seat "
        "you hand a piece to reads its own brief, its own bible and its own "
        "notes when it starts — you do not need any of that to decide WHO does "
        "it, and gathering it is how a five-second routing decision turns into "
        "a minute of tool calls and a bill. Specifically:\n"
        "  · do NOT call seat_brief — that is the working agent's first call, "
        "not yours;\n"
        "  · call queue_list(status='queued') or queue_list(status='dispatched') "
        "ONLY if you need to avoid duplicating work already on the board, and "
        "read the titles, not the briefs;\n"
        "  · read the bible only if the ask itself is about canon.\n"
        "Two or three tool calls is a good turn. Ten is a bug.\n\n"
        "Do these, in order:\n"
        "1. WORK OUT WHAT THEY MEAN, from the message itself. If it is "
        "ambiguous in a way that changes who should do it, ask them — one "
        "question beats a wrong delegation.\n"
        "2. If the ask is about work ALREADY RUNNING — a correction, a change of "
        "mind, 'not like that' — do not queue a new item for it. Call "
        "queue_list(status='dispatched') to see who is live and "
        "agent_steer(item_id, text) to say it to that agent mid-run. Steering "
        "is cheaper and faster than letting a wrong run finish and re-queueing "
        "it, and it is the difference between a director and a dispatcher.\n"
        "3. If — and only if — the ask is NEW work that should be done, delegate "
        "it. For each piece call queue_add(seat=..., title=<short imperative>, "
        "brief=<a self-contained brief the working agent can act on with no "
        "other context>). Do not implement anything yourself, and do not split "
        "a single coherent task into fragments.\n"
        f"   EVERY brief you write MUST START with this exact line, verbatim:\n"
        f"     {DELEGATED_FROM}{turn_id}\n"
        "   That line is the only durable record that this piece came from "
        "this message — the console graph reads it to draw the edge. Write it "
        "first, then a blank line, then the real brief.\n"
        "4. Finish with queue_complete for THIS item "
        f"(work item id={turn_id}) and make the summary your ANSWER TO THE "
        "HUMAN: what you understood, what you queued and to which seats, who "
        "you steered and what you told them (or why you did neither), and "
        "anything you need them to decide. Plain sentences, no preamble — it is "
        "rendered straight into the chat.\n"
    )


def said(brief: str) -> str:
    """The human's own words back out of a turn's brief."""
    text = brief or ""
    if SAID_OPEN not in text or SAID_CLOSE not in text:
        return ""
    body = text.split(SAID_OPEN, 1)[1].split(SAID_CLOSE, 1)[0]
    return body.strip()


def _ws_update(root_dir, key: str, change) -> dict:
    """Read-modify-write a workspace doc, retrying once on a lost update.

    Each of these docs is READ on the three-second poll and WRITTEN by a button,
    so two tabs — or one tab and the autopilot toggle — collide often enough to
    matter. Unhandled, workspace.StaleWrite surfaced as a 500 with a version
    string in it; one retry against the fresh document is what a lost update
    actually calls for, and a second failure is reported as the conflict it is.
    """
    for attempt in (0, 1):
        doc = _ws.get(root_dir, SEAT, key, {})
        try:
            _ws.set(root_dir, SEAT, key, change(doc))
            return doc
        except _ws.StaleWrite as exc:
            if attempt:
                raise _api.conflict(
                    "another tab changed this first — reload and try again",
                    expected=exc.expected, actual=exc.actual)
    return {}


def _session_doc(root_dir) -> dict:
    doc = _ws.get(root_dir, SEAT, SESSION_KEY, {})
    doc.setdefault("cleared_before", 0)
    doc.setdefault("sessions", [])
    return doc


def _turn_rows(conn, limit: int, *, after: int = 0, span: tuple = ()) -> list[dict]:
    """The conversation. ``after`` is the live console's cut line; ``span`` is
    an archived session's (from_id, to_id) — the two are exclusive."""
    if span:
        rows = conn.execute(
            "SELECT * FROM work_item WHERE source = ? AND id >= ? AND id <= ? "
            "ORDER BY id DESC LIMIT ?",
            (CHAT_SOURCE, span[0], span[1], limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM work_item WHERE source = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?", (CHAT_SOURCE, after, limit)).fetchall()
    turns = []
    for row in reversed(rows):
        card = _card(row)
        # The verbatim message, not the 80-character title it was cut down to.
        card["said"] = said(row["brief"]) or card["title"]
        turns.append(card)
    return turns


def _reply(root_dir, item: dict) -> dict:
    """The director's side of one turn: its final summary if it has finished,
    its last words if it has not, and whether it is still talking."""
    feed = _dispatch.read_activity(str(root_dir), int(item["id"]), limit=STEPS)
    final = feed.get("final") or {}
    text = ""
    if item["status"] == "done" and item.get("result"):
        text = item["result"]
    elif final.get("text"):
        text = str(final["text"])
    said = [s.get("text", "") for s in feed.get("steps") or []
            if s.get("kind") == "say" and s.get("text")]
    return {
        # NOT 2000. This is the director's answer to the human — the deliverable
        # of a chat turn, not a status note — and clipping it here reintroduced
        # the exact mid-word cut queue.clip_result was fixed to stop. The store
        # already bounds it (queue.MAX_RESULT) and says so when it bites, so a
        # second, smaller, silent cap on the way out is pure loss.
        "text": text[:_queue.MAX_RESULT],
        "thinking": said[-1][:400] if said and not text else "",
        "running": bool(feed.get("running")),
        "steps": feed.get("steps") or [],
        "step_count": feed.get("step_count") or 0,
        "cost": final.get("cost"),
    }


def _gates(root_dir, conn, active: set[int]) -> list[dict]:
    """Everything the work IN FLIGHT is currently waiting on.

    Two kinds, because the floor has two: a QA gate item (an agent reviewing
    another agent's claim) and a generated candidate that only a human may
    approve. They are one list here because on the graph they are one shape —
    a node the work cannot get past on its own.

    ``active`` is the gate: a candidate belongs here only while the item that
    produced it is still queued or running. Every candidate ever generated is a
    'candidate' until somebody dispositions it, so without that filter a project
    with a used art library grows a permanent wall of approval nodes hanging off
    work that finished weeks ago — which is a review backlog, not a gate, and
    the review queue in Assets is where it belongs.

    THE MODE SELECTOR REACHES THIS LIST. It did not, and that is the bug this
    paragraph exists for: a project with the gate set to ``none`` kept drawing
    SIGN-OFF cards over every finished item and APPROVAL cards over every
    generated candidate, each one telling the human that only they could decide.
    Reported from the field across seats — art and director both — after the
    human had explicitly switched the asking off. Setting a gate to ``none`` is
    a sentence: *do not stop to ask me*. Asking anyway either stalls work behind
    a card nobody knew to click or trains the human to rubber-stamp, which
    destroys the gate for the runs where it IS on. So the mode is read here, once
    per poll, and it decides which human-facing cards exist at all.

    What each mode draws, stated so the next reader does not have to infer it:

        none      QA-gate items only — those are real work items somebody filed,
                  and hiding a queued agent is a different lie. No sign-off, no
                  candidate approvals (``artifacts._auto_approve`` clears those
                  at registration under this mode, so nothing is left stuck).
        agent     the above plus candidate approvals: an agent records a verdict
                  with ``art_qa_verdict`` and the revision STAYS a candidate, so
                  a human is still the only one who can promote it.
        builders  everything, including sign-off — and sign-off covers items
                  parked in ``review`` as well as recently-finished ones. Under
                  this mode ``queue.complete`` parks a finished item in 'review'
                  rather than 'done', and this list queried 'done' only, so the
                  one gate the mode actually mandates had no card on the graph.
    """
    from bgate_core import gates as _gatemode

    try:
        mode = _gatemode.mode(root_dir)
    except Exception:
        # An unreadable mode must not blank the board. DEFAULT is 'agent', the
        # behaviour that shipped before the setting existed.
        mode = _gatemode.DEFAULT
    out: list[dict] = []
    for row in conn.execute(
            "SELECT id, seat, title, status, source, source_ref, created_at "
            "FROM work_item WHERE source IN ('qa-gate', 'qa-gate-escalation') "
            "AND status IN ('queued', 'dispatched') ORDER BY id DESC LIMIT 20"):
        ref = (row["source_ref"] or "").strip()
        out.append({
            "kind": "escalation" if row["source"] == "qa-gate-escalation" else "qa",
            "id": f"gate_item_{row['id']}",
            "item_id": int(row["id"]),
            "over_item_id": int(ref) if ref.isdigit() else None,
            "title": row["title"],
            "seat": row["seat"],
            "status": row["status"],
            "blocking": row["source"] == "qa-gate-escalation",
            "created_at": row["created_at"],
        })
    # Sign-off: an agent says a thing is done, and until a human agrees it is
    # only a claim. The gate appears the moment the item lands and disappears
    # the moment it is acted on, which is what makes it a gate rather than a
    # backlog — see POST /api/console/signoff.
    #
    # BUILDER'S GATE ONLY. Under 'agent' the QA seat is the reviewer and a second
    # human ask buys nothing the QA verdict has not already bought; under 'none'
    # the human has said not to ask at all.
    if mode == _gatemode.BUILDERS:
        acked = set((_ws.get(root_dir, SEAT, SIGNOFF_KEY, {}).get("acked") or {}))
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=_signoff_hours(root_dir))
                  ).strftime("%Y-%m-%d %H:%M:%S")
        # 'review' has no time window and never gets one: it is not a claim
        # somebody may want to glance at, it is a chain stopped dead waiting for
        # this exact decision. Ageing it out of the list would hide the block and
        # leave the work behind it queued forever with nothing on screen saying
        # why. 'done' keeps the window — see SIGNOFF_HOURS.
        for row in conn.execute(
                "SELECT id, seat, title, result, status, source, updated_at "
                "FROM work_item "
                "WHERE source NOT IN ('chat', 'qa-gate') "
                "AND (status = 'review' OR (status = 'done' AND updated_at >= ?)) "
                "ORDER BY CASE status WHEN 'review' THEN 0 ELSE 1 END, "
                "updated_at DESC, id DESC LIMIT 14",
                (cutoff,)):
            if str(row["id"]) in acked:
                continue
            parked = row["status"] == "review"
            out.append({
                "kind": "signoff",
                "id": f"gate_done_{row['id']}",
                "item_id": int(row["id"]),
                "over_item_id": int(row["id"]),
                "title": row["title"],
                "seat": row["seat"],
                "status": row["status"],
                # The one field that separates "glance at this" from "this is
                # stopped": a parked item releases its chain only on approve.
                "parked": parked,
                "result": (row["result"] or "")[:400],
                "blocking": True,
                "created_at": row["updated_at"],
                "gate_mode": mode,
            })

    # A candidate is human-only to promote, so under 'none' there is nobody left
    # to ask — artifacts.register auto-approves at registration under that mode
    # rather than leaving a wall of candidates behind a suppressed card.
    if mode != _gatemode.NONE:
        for art in _artifacts.list_revisions(root_dir, status="candidate")[:60]:
            item_id = art.get("work_item_id")
            if not item_id or int(item_id) not in active:
                continue
            out.append({
                "kind": "art",
                "id": f"gate_art_{art['id']}",
                "artifact_id": int(art["id"]),
                "item_id": int(item_id) if item_id else None,
                "over_item_id": int(item_id) if item_id else None,
                "title": art.get("logical_name") or f"candidate {art['id']}",
                "seat": "art",
                "status": "candidate",
                "blocking": True,
                "path": art.get("path") or "",
                "gate_mode": mode,
            })
    return out


def _questions(root_dir) -> list[dict]:
    """Open ``ask_human`` questions — what an agent is waiting on a human for.

    A question is an event rather than a work item (see bgate_core.steerbox): a
    queued row is a row somebody has to dispatch in order to read it, which turns
    "ask the human" into "spawn an agent to ask the human". So it arrives here,
    on the payload the console already polls, instead of in the board list.

    Guarded and read whole rather than off the notification cursor: a question
    stays open until it is answered, so a cursor that has already passed the
    event would show it once and then never again.
    """
    try:
        return _steerbox.open_questions(root_dir)
    except Exception:
        return []


def _question_reminders(root_dir, questions: list[dict]) -> None:
    """Fire the ONE stale-question reminder, on the poll that is already here.

    The bus is transition-driven and an unanswered question makes no transitions,
    so without this the new routing keeps the quiet failure mode it exists to fix.
    It rides this endpoint rather than a fourth daemon thread, and the decision to
    do any work at all is made from the list already in hand — so the ordinary
    poll costs a string comparison, not a query. Fully guarded: a reminder must
    never be the reason the console stops painting.
    """
    if not questions:
        return
    try:
        from bgate_core import settings as _settings
        hours = float(_settings.get(root_dir, "notify.question_stale_h") or 12.0)
    except Exception:
        hours = 12.0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0.25, hours))
              ).strftime("%Y-%m-%d %H:%M:%S")
    if not any((q.get("asked_at") or "") < cutoff and not q.get("reminded_at")
               for q in questions):
        return
    try:
        _steerbox.remind_stale(root_dir, hours=hours)
    except Exception:
        pass


def _artifacts_by_item(root_dir, item_ids: set[int]) -> dict[int, list[dict]]:
    """Everything the live items have produced, grouped, oldest first — the
    input to the phase/artifact match."""
    out: dict[int, list[dict]] = {}
    if not item_ids:
        return out
    for art in _artifacts.list_revisions(root_dir, limit=400):
        item_id = art.get("work_item_id")
        if item_id and int(item_id) in item_ids:
            out.setdefault(int(item_id), []).append(art)
    for rows in out.values():
        rows.sort(key=lambda a: (a.get("created_at") or "", a.get("id") or 0))
    return out


def _collab(root_dir, conn, active: set[int]) -> list[dict]:
    """Where two agents are on the same thing at the same time.

    Delegation is already an edge (lineage). This is the other kind — the
    sideways one nothing in the product has ever drawn, even though the whole
    floor is built to make it happen:

      * ASSET — two live items producing revisions of the same logical asset.
        That is either a collaboration or a collision, and both are worth
        seeing before the second one lands.
      * BLOCKED — one item is waiting on a path another one holds a lease over
        (asset_waiter + path_lease). A stalled agent otherwise looks identical
        to a slow one.
      * STEER — one agent sent a message into another's run (the ledger rows
        the steer pump writes).

    Every edge names both ends and why, because an undirected line between two
    boxes is decoration.
    """
    if len(active) < 1:
        return []
    out: list[dict] = []
    # Bounded: this list becomes an IN (…) clause and SQLite before 3.32 caps a
    # statement at 999 bind parameters, so a big open backlog would turn every
    # poll into a hard error.
    ids = sorted(active)[:200]
    marks = ", ".join("?" * len(ids)) or "NULL"

    # ASSET — same logical name, two different live items.
    if ids:
        rows = conn.execute(
            f"SELECT logical_name, group_concat(DISTINCT work_item_id) AS items "
            f"FROM artifact_revision WHERE work_item_id IN ({marks}) "
            "GROUP BY logical_name HAVING count(DISTINCT work_item_id) > 1", ids)
        for row in rows:
            members = sorted({int(x) for x in str(row["items"]).split(",") if x})
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    out.append({"a": a, "b": b, "kind": "asset",
                                "label": f"both on {row['logical_name']}"})

    # BLOCKED — waiting on a path somebody else holds.
    try:
        rows = conn.execute(
            "SELECT w.asset_path AS path, w.owner AS waiter, l.owner AS holder "
            "FROM asset_waiter w JOIN path_lease l ON l.path = w.asset_path")
        for row in rows:
            a = _owner_item(row["waiter"])
            b = _owner_item(row["holder"])
            if a and b and a != b:
                out.append({"a": a, "b": b, "kind": "blocked",
                            "label": f"#{a} is waiting on {row['path']}"})
    except Exception:
        pass  # the lease tables are advisory; a missing one is not an error

    # STEER — an agent talking into another agent's run.
    for row in _activity.recent(root_dir, limit=60):
        if row.get("kind") != "steer":
            continue
        target = str(row.get("ref") or "")
        actor = _owner_item(row.get("actor") or "")
        if target.isdigit() and actor and actor != int(target):
            out.append({"a": actor, "b": int(target), "kind": "steer",
                        "label": row.get("summary", "")[:90]})

    seen, unique = set(), []
    for edge in out:
        key = (edge["a"], edge["b"], edge["kind"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique[:24]


def _owner_item(owner: str) -> int:
    """``item-41`` / ``agent:item-41`` -> 41. Anything else -> 0."""
    text = str(owner or "")
    marker = "item-"
    if marker not in text:
        return 0
    tail = text.split(marker, 1)[1]
    digits = ""
    for ch in tail:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else 0


@router.get("/api/console/state")
def console_state(steps: bool = True) -> dict:
    r = root()
    conn = db.connect(r)

    agents = _dispatch.status(str(r))
    live_ids = {int(a["item_id"]) for a in agents
                if a.get("item_id") is not None and a.get("state") == "running"}

    rows = conn.execute(
        "SELECT * FROM work_item ORDER BY CASE status WHEN 'dispatched' THEN 0 "
        "WHEN 'review' THEN 1 WHEN 'queued' THEN 2 WHEN 'failed' THEN 3 "
        "ELSE 4 END, priority DESC, id DESC LIMIT ?", (BOARD,)).fetchall()
    items = [_card(row) for row in rows]
    seen = {it["id"] for it in items}
    # The newest finished work, whatever it finished as. Unioned rather than
    # merged into the query above, because that ordering is what the board and
    # the graph want and this is a different question — "what just closed" is
    # never answered by "what is most urgent".
    closed = conn.execute(
        "SELECT * FROM work_item WHERE status IN "
        "('done','cancelled','approved','rejected') "
        "ORDER BY updated_at DESC, id DESC LIMIT ?", (RECENT_CLOSED,)).fetchall()
    for row in closed:
        if row["id"] not in seen:
            items.append(_card(row))
            seen.add(row["id"])
    # A running agent's item is always in the payload even if the window cut it
    # — a node that exists on the graph must have something to render.
    missing = [i for i in live_ids if i not in seen]
    if missing:
        marks = ", ".join("?" * len(missing))
        items += [_card(row) for row in conn.execute(
            f"SELECT * FROM work_item WHERE id IN ({marks})", missing)]

    doc = _session_doc(r)
    turns = _turn_rows(conn, TURNS, after=int(doc.get("cleared_before") or 0))
    for turn in turns:
        turn["reply"] = _reply(r, turn)

    # The live agents, in detail: the card wants the last step, the graph wants
    # the phases, and both come out of one read of the feed.
    live_steps: dict[str, list] = {}
    live_phases: dict[str, list] = {}
    if steps:
        by_item = _artifacts_by_item(r, live_ids)
        for item_id in sorted(live_ids):
            feed = _dispatch.read_activity(str(r), item_id, limit=0)
            all_steps = feed.get("steps") or []
            live_steps[str(item_id)] = all_steps[-STEPS:]
            live_phases[str(item_id)] = _phases_for(
                r, item_id, feed, all_steps, by_item.get(item_id, []))

    counts = {row["status"]: int(row["n"]) for row in conn.execute(
        "SELECT status, count(*) AS n FROM work_item GROUP BY status")}

    # Work in flight — what a gate is allowed to hang off.
    active = {int(row["id"]) for row in conn.execute(
        "SELECT id FROM work_item WHERE status IN ('queued', 'dispatched')")}
    active |= live_ids

    _chain_state(conn, items)

    questions = _questions(r)
    _question_reminders(r, questions)

    from bgate_core import gates as _gatemode
    return {
        "turns": turns,
        "items": items,
        "agents": agents,
        "lineage": _lineage(r),
        "gates": _gates(r, conn, active),
        "questions": questions,
        "steps": live_steps,
        "phases": live_phases,
        "collab": _collab(r, conn, active),
        "sessions": list(reversed(doc.get("sessions") or []))[:20],
        "autopilot": _autodeploy.state(r),
        "gate": _gatemode.state(r),
        "floor": {
            "running": len(live_ids),
            "queued": counts.get("queued", 0),
            "dispatched": counts.get("dispatched", 0),
            "review": counts.get("review", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
        },
    }


@router.post("/api/console/say")
def console_say(payload: dict) -> dict:
    """One message to the director — or straight to a seat you named.

    `seat` ADDRESSES THE WORK INSTEAD OF THE DIRECTOR. Untagged, this is what it
    has always been: a turn for the director, which answers and delegates. With
    a seat, the item is filed for that seat and dispatched to it, because
    "@narrative — write the dialogue tree" typed into the director got a polite
    paragraph about what it would delegate, and the work still had to go round
    the houses to arrive where the human had already pointed it.

    The director is still the default, and still the right answer when you do
    not know whose job it is. This is for when you do.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise _api.bad_request("say what?  an empty message has nothing to act on")
    if len(text) > 4000:
        raise _api.bad_request("that message is too long for one turn — "
                               "4000 characters is the cap", length=len(text))
    r = root()
    seat = str(payload.get("seat") or "").strip().lower() or "director"
    if seat != "director":
        from bgate_core import seats as _seats
        table = _seats.roles_for(r)
        if seat not in table:
            raise _api.bad_request(
                f"{seat!r} is not a seat on this project — "
                f"known: {', '.join(sorted(table))}", seat=seat)
    title = text.splitlines()[0][:80] or text[:80]
    try:
        turn = _queue.add(r, seat, title=title,
                          brief="(preparing)", source=CHAT_SOURCE)
    except ValueError as exc:
        # Out-of-scope is a ValueError subclass and reads as a real sentence.
        raise _api.bad_request(str(exc))
    turn_id = int(turn["id"])
    # The brief names the turn's own id (children stamp it), so it can only be
    # written once the row exists.
    #
    # A SEAT GETS THE SENTENCE, NOT THE DIRECTOR'S BRIEF. _chat_brief tells its
    # reader to answer and delegate, which is the director's job and nobody
    # else's — handing it to the art seat would have art filing work for other
    # seats. An addressed turn is the human's own words plus where they came
    # from.
    _queue.update(r, turn_id, brief=(
        _chat_brief(text, turn_id) if seat == "director"
        else (text + "\n\n---\n"
              + f"Addressed to the {seat} seat from the director's console. "
              "This is the human's own wording, not a brief written for you; "
              "if it is not yours to do, say so rather than doing it anyway.")))

    result = _dispatch.dispatch(str(r), turn_id, actor="console")
    if not result.get("ok"):
        # The turn stays on the board and can be dispatched by hand or by
        # auto-deploy later; the refusal is the answer for now.
        return {"ok": True, "turn_id": turn_id, "dispatched": False,
                "refusal": {"code": result.get("code") or "refused",
                            "message": result.get("error") or "dispatch refused"}}
    return {"ok": True, "turn_id": turn_id, "dispatched": True,
            "seat": seat, "pid": result.get("pid")}


@router.post("/api/console/clear")
def console_clear() -> dict:
    """Close the current conversation and start a fresh one.

    NOTHING IS DELETED. The turns stay work items, their logs stay on disk, and
    the range is filed as a session you can open again — clearing a console that
    destroyed the record of what was asked and what the agent did would be the
    worst possible reading of the word. All this moves is the cut line the live
    transcript reads from.
    """
    r = root()
    conn = db.connect(r)
    doc = _session_doc(r)
    before = int(doc.get("cleared_before") or 0)
    row = conn.execute(
        "SELECT min(id) AS lo, max(id) AS hi, count(*) AS n FROM work_item "
        "WHERE source = ? AND id > ?", (CHAT_SOURCE, before)).fetchone()
    if not row or not row["n"]:
        return {"ok": True, "cleared": 0, "sessions": doc.get("sessions") or []}

    first = conn.execute(
        "SELECT brief, title FROM work_item WHERE id = ?", (row["lo"],)).fetchone()
    title = (said(first["brief"]) or first["title"] or "session") if first else "session"
    session = {
        "id": len(doc["sessions"]) + 1,
        "from_id": int(row["lo"]), "to_id": int(row["hi"]), "turns": int(row["n"]),
        "title": title[:90],
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    def _file(fresh: dict) -> dict:
        fresh["sessions"] = (fresh.get("sessions") or []) + [session]
        fresh["cleared_before"] = int(row["hi"])
        return fresh

    _ws_update(r, SESSION_KEY, _file)
    _activity.log(r, "console",
                  f"archived console session {session['id']} "
                  f"({session['turns']} turn(s), #{session['from_id']}–#{session['to_id']})",
                  seat="director")
    return {"ok": True, "cleared": session["turns"], "session": session,
            "sessions": list(reversed(doc["sessions"]))[:20]}


@router.get("/api/console/session/{session_id}")
def console_session(session_id: int) -> dict:
    """One archived conversation, replies and all — read it, and open any turn's
    log from it. The logs never went anywhere; only the cut line moved."""
    r = root()
    doc = _session_doc(r)
    match = [s for s in (doc.get("sessions") or []) if int(s.get("id")) == session_id]
    if not match:
        raise _api.not_found(f"no archived session {session_id}")
    session = match[0]
    conn = db.connect(r)
    turns = _turn_rows(conn, 200,
                       span=(int(session["from_id"]), int(session["to_id"])))
    for turn in turns:
        turn["reply"] = _reply(r, turn)
    return {"session": session, "turns": turns}


@router.post("/api/console/signoff")
def console_signoff(payload: dict) -> dict:
    """Act on an agent's claim that a work item is done.

    ``accept`` records that a human has seen it and the gate goes away.
    ``reopen`` sends the item back to the queue with the reason appended to its
    brief — the same path the QA gate's FAIL takes, so the next agent on it
    reads exactly what was wrong.

    The acknowledgement is stored rather than inferred. 'Done' already means
    'the agent finished'; without a second, separate record there is no way to
    express 'and a human has looked at it', which is the entire difference
    between a claim and an approval.

    TWO DIFFERENT ITEMS ARRIVE HERE AND ONLY ONE OF THEM IS A CLAIM. Under the
    builder's gate a completion parks in 'review' and the chain behind it does
    not advance — the decision is not a glance, it is the thing releasing the
    work. Acking that in the workspace doc would clear the CARD and leave the
    CHAIN stopped, with the one surface that said so now hidden: the same
    off-at-the-drawing-not-at-the-decision mistake the gate selector made. So a
    parked item routes to ``queue.approve``/``queue.reject``, which move the
    status, stamp who signed and emit; only a genuinely finished item takes the
    acknowledgement path.
    """
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        raise _api.bad_request("item_id (int) is required")
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in ("accept", "reopen"):
        raise _api.bad_request("verdict must be 'accept' or 'reopen'")
    reason = str(payload.get("reason") or "").strip()
    r = root()
    try:
        item = _queue.get(r, item_id)
    except LookupError:
        raise _api.not_found(f"no work item {item_id}")

    parked = item["status"] == "review"

    if verdict == "reopen":
        if not reason:
            raise _api.bad_request(
                "say what is wrong — a reopen with no reason spends another "
                "agent on the same guess")
        if parked:
            # queue.reject parks it as failed and reuses reopen, so a rejected
            # item is indistinguishable from a QA-failed one downstream: one fix
            # path, one round counter, one place the next agent looks.
            _queue.reject(r, item_id, reason)
            _activity.log(r, "signoff", f"rejected #{item_id}: {reason[:120]}",
                          seat=item["seat"], ref=str(item_id))
            return {"ok": True, "item_id": item_id, "verdict": verdict,
                    "status": "queued", "released": False}
        if item["status"] not in ("done", "failed"):
            raise _api.bad_request(
                f"item {item_id} is {item['status']} — only a finished item "
                "can be sent back")
        # queue.reopen APPENDS the reason to the brief itself and stamps the
        # round. Appending it here as well doubled the text on every sign-off,
        # and the brief grew without bound across rounds.
        _queue.reopen(r, item_id, reason)
        _activity.log(r, "signoff", f"sent #{item_id} back: {reason[:120]}",
                      seat=item["seat"], ref=str(item_id))
        return {"ok": True, "item_id": item_id, "verdict": verdict,
                "status": "queued"}

    if parked:
        # 'review' -> 'done', which is what frees the chain. approve() records
        # WHO signed, because an approval nobody signed is not an approval, and
        # emits item.approved rather than a second item.done — the completion was
        # already announced when it parked.
        _queue.approve(r, item_id, note=reason)
        _activity.log(r, "signoff", f"approved #{item_id}: {item['title'][:80]}",
                      seat=item["seat"], ref=str(item_id))
        return {"ok": True, "item_id": item_id, "verdict": verdict,
                "status": "done", "released": True}

    def _ack(doc: dict) -> dict:
        acked = dict(doc.get("acked") or {})
        acked[str(item_id)] = {"verdict": "accept", "by": _activity.current_actor(),
                               "at": item["updated_at"], "note": reason}
        doc["acked"] = acked
        return doc

    _ws_update(r, SIGNOFF_KEY, _ack)
    _activity.log(r, "signoff", f"accepted #{item_id}: {item['title'][:80]}",
                  seat=item["seat"], ref=str(item_id))
    return {"ok": True, "item_id": item_id, "verdict": verdict, "status": "done",
            "released": False}


@router.post("/api/console/answer")
def console_answer(payload: dict) -> dict:
    """Answer a director's question, and send the answer where it will be read.

    ``{"seq": <the question's event id>, "answer": "..."}``. Where it lands
    depends on whether the asker is still alive, which is the whole reason this is
    an endpoint and not a text field on a work item: a live agent gets it as a
    steer mid-run, a finished one leaves behind a handoff `decision` note. Either
    way it is attached to the question event, so the drawer, the next session and
    the next debrief all see it without this tab being open.

    409 on a second answer: the first one has already been delivered, and a
    silent overwrite would contradict a message that is already gone.
    """
    try:
        seq = int(payload.get("seq"))
    except (TypeError, ValueError):
        raise _api.bad_request("seq (the question's event id) is required")
    text = str(payload.get("answer") or "").strip()
    r = root()
    try:
        result = _steerbox.answer(r, seq, text, by=_activity.current_actor())
    except _steerbox.AlreadyAnswered as exc:
        raise _api.conflict(str(exc), seq=seq, answer=exc.existing)
    except LookupError as exc:
        raise _api.not_found(str(exc))
    except ValueError as exc:
        raise _api.bad_request(str(exc))
    try:
        _activity.log(r, "question",
                      f"answered question {seq} ({result['route']}): {text[:120]}",
                      seat=SEAT, ref=str(result.get("item_id") or seq))
    except Exception:
        pass  # the answer is already recorded and delivered; the ledger is not
    return result


@router.post("/api/console/killswitch")
def console_killswitch(payload: dict | None = None) -> dict:
    """Stop every agent on this project and turn auto-deploy off.

    Deliberately one call with no arguments to get right: in the moment you
    need this you are not going to enumerate item ids. See dispatch.kill_all
    for the order it does things in and why that order matters.
    """
    reason = str((payload or {}).get("reason") or "").strip()
    r = root()
    return _dispatch.kill_all(str(r), reason=reason or "stopped from the console",
                              actor=_activity.current_actor())


@router.get("/api/console/autopilot")
def autopilot_get() -> dict:
    return _autodeploy.state(root())


@router.post("/api/console/autopilot")
def autopilot_set(payload: dict) -> dict:
    on = payload.get("on")
    if not isinstance(on, bool):
        raise _api.bad_request("on must be true or false")
    r = root()
    state = _autodeploy.set_enabled(r, on)
    tick = {"dispatched": [], "refused": []}
    if on:
        # Immediate, so the switch does something visible now instead of up to
        # one poll interval later. Failure here is not the toggle's failure.
        try:
            tick = _autodeploy.tick(r)
        except Exception as exc:  # pragma: no cover — belt and braces
            tick = {"dispatched": [], "refused": [
                {"code": "tick_failed", "message": f"{type(exc).__name__}: {exc}"}]}
    return {**_autodeploy.state(r), "tick": tick,
            "was": {"on": not on}, "now": state["on"]}
