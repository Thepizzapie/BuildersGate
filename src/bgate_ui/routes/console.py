"""The board behind the director's screen — one poll, everything it paints.

THE CONVERSATION IS NOT HERE. It used to be: a message was a work item with a
fenced brief, a reserved row and an archived cut line, and this module owned
all of it. A chat is a chat, so it moved to routes/director.py over
directorsession's own transcript, and what is left here is the BOARD.

  * ``GET /api/console/state`` — everything the view paints, in ONE request:
    the board, the delegation lineage, the running agents WITH their last few
    steps, the open approval gates, the auto-deploy switch, and a floor tally.
    The old view needed /api/queue + /api/agents + one /api/agent-activity per
    live agent every 3.5 seconds and still could not draw an edge between two
    items.

  * ``POST /api/console/autopilot`` — the auto-deploy switch (bgate_ui.agents.autodeploy),
    plus an immediate tick so flipping it on does something visible now rather
    than up to four seconds later.

Bounded on purpose: this is polled. The board window is capped, steps are the
last few per live agent, and briefs are previews.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from bgate_core.board import activity as _activity
from bgate_core.store import artifacts as _artifacts
from bgate_core.store import db
from bgate_core.board import queue as _queue
from bgate_core.board import steerbox as _steerbox
from bgate_core.store import workspace as _ws
from bgate_ui import api as _api
from bgate_ui.agents import autodeploy as _autodeploy
from bgate_ui.agents import dispatch as _dispatch
from bgate_ui.agents import phases as _phases
from bgate_ui.deps import root
from bgate_ui.routes.orchestrator import _lineage

router = APIRouter()

# Where the "a human has seen this claim" record lives. Same store as the
# auto-deploy switch: per project, survives a restart, no schema change.
SEAT = "director"
SIGNOFF_KEY = "signoffs"
ATTENTION_KEY = "dismissed_attention"
# How far back a finished item still asks for a sign-off. This is a LIVE
# instrument: work that landed while you were watching wants a decision, work
# from last Tuesday is history and belongs to the timeline. Without the window,
# opening the console on an old project drew a wall of approval nodes over runs
# nobody is thinking about — which is the exact failure the candidate gate had.
SIGNOFF_HOURS = 8


def _director_running(root_dir) -> dict:
    """{running: bool} - is the director session mid-reply right now."""
    try:
        from bgate_ui.agents import directorsession as _ds
        return {"running": bool(_ds.status(str(root_dir)).get("running"))}
    except Exception:
        return {"running": False}


def _signoff_hours(root_dir) -> float:
    """The sign-off window, from the registry (``signoff.hours``).

    Read per request rather than captured in the constant above: the constant is
    now only the fallback, and a project that raised the window because it is
    catching up on a weekend of work must not have to restart the dashboard for
    it. Without this the Settings panel offered the field and nothing read it —
    a switch that silently does nothing is worse than no switch.
    """
    try:
        from bgate_core.store import settings as _settings
        return max(0.25, float(_settings.get(root_dir, "signoff.hours")))
    except Exception:
        return float(SIGNOFF_HOURS)

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

# Columns a project's database may not have yet: the card must still build on
# one that predates the migration, so these are read defensively rather than
# named in _CARD_FIELDS. `exhausted_at`/`exhausted_why` arrived with 0043.
_CARD_OPTIONAL = ("exhausted_at", "exhausted_why", "auto_retries")


def _card(row) -> dict:
    item = {k: row[k] for k in _CARD_FIELDS}
    keys = row.keys()
    for name in _CARD_OPTIONAL:
        if name in keys:
            item[name] = row[name]
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
    """Stamp readiness onto the cards, in TWO queries for the whole board.

    A chained item that is not ready is indistinguishable from a plain queued
    one on the wire, so the console offered a deploy button whose only possible
    outcome was a refusal. Resolved here rather than per row because the
    console payload is already the expensive call on this page.

    THREE TRUTHS THE FIRST VERSION MISSED, each of which drew a dispatchable-
    looking card over an item nothing was ever going to run:
      * EXTRA PARENTS. Fan-in deps live in work_item_dep, and reading only the
        depends_on column rendered those cards `ready` with a deploy button
        whose one outcome was `blocked_on_dependency`.
      * `held`. A queued row whose source is in HELD_SOURCES (qa-gate
        escalations, chat) is skipped by every auto-dispatcher on purpose - a
        human takes it. On the wire it looked like any queued item.
      * `stuck`. A predecessor in `failed`/`cancelled` will never reach 'done'
        on its own, so the successor is not "waiting", it is parked until
        someone reopens the predecessor or cuts the dependency - and the card
        should say which act frees it.
    """
    from bgate_core.board.queue import HELD_SOURCES, SATISFIED

    ids = [int(it["id"]) for it in items]
    extra: dict[int, list[int]] = {}
    if ids:
        marks = ", ".join("?" * len(ids))
        for row in conn.execute(
                f"SELECT item_id, depends_on FROM work_item_dep "
                f"WHERE cut_at IS NULL AND item_id IN ({marks})",
                tuple(ids)):
            extra.setdefault(int(row["item_id"]), []).append(
                int(row["depends_on"]))
    need = {int(it["depends_on"]) for it in items if it.get("depends_on")}
    need.update(d for deps in extra.values() for d in deps)
    deps: dict[int, dict] = {}
    if need:
        marks = ", ".join("?" * len(need))
        deps = {int(row["id"]): dict(row) for row in conn.execute(
            f"SELECT id, seat, title, status FROM work_item "
            f"WHERE id IN ({marks})", tuple(sorted(need)))}
    for it in items:
        parents = ([int(it["depends_on"])] if it.get("depends_on") else []) \
            + extra.get(int(it["id"]), [])
        # A missing predecessor (deleted) unblocks rather than strands: an
        # item nobody can release is worse than one that ran a step early.
        unsatisfied = [deps[p] for p in parents
                       if p in deps and deps[p]["status"] not in SATISFIED]
        it["ready"] = not unsatisfied
        it["held"] = (it.get("status") == "queued"
                      and str(it.get("source") or "") in HELD_SOURCES)
        it["depends_on_all"] = sorted(parents)
        it["unresolved"] = sorted(int(d["id"]) for d in unsatisfied)
        it["stuck"] = any(d["status"] in ("failed", "cancelled")
                          for d in unsatisfied)
        if unsatisfied:
            it["waiting_on"] = unsatisfied[0]
            if len(unsatisfied) > 1:
                it["waiting_count"] = len(unsatisfied)
            # THE SENTENCE, NOT THE STATUS WORD. `#43 QUEUED` next to a running
            # #45 and a done #42 reads as a scheduler that skipped an item -
            # observed, and the dependency engine was correct throughout. Ids
            # are creation identifiers; #45 was inserted between #42 and #43
            # later because the route measurements needed real furniture
            # dimensions. Naming the blocker AND ITS TITLE is what stops a
            # correct insertion looking like a fault.
            it["waiting_line"] = (
                f"WAITING ON #{unsatisfied[0]['id']} {unsatisfied[0]['title']}"
                + (f" (and {len(unsatisfied) - 1} more)"
                   if len(unsatisfied) > 1 else "")
                # A DEAD PREDECESSOR IS NOT PATIENCE. Naming the two acts that
                # free it is the difference between a card that reads as the
                # board working and one that reads as a stall somebody owns.
                + (f" — that predecessor is {unsatisfied[0]['status']!r} and "
                   "will not reach 'done' on its own; reopen it or cut the "
                   "dependency" if it["stuck"] else ""))
        # ONE FIELD THE CARD CAN COLOUR BY, so a reader never has to derive the
        # difference between "the board is working" and "this needs a person"
        # from four booleans.
        it["execution_state"] = _execution_state(it, unsatisfied)
        # EXHAUSTED AND HELD OWE A SENTENCE TOO. Neither has an unmet
        # predecessor, so neither took the branch above — and both rendered as
        # the seat name, which is the least informative thing the row could
        # have said about why nothing is happening to it.
        if it["execution_state"] == "exhausted":
            it["waiting_line"] = (
                "EXHAUSTED — the harness stopped retrying this: "
                + str(it.get("exhausted_why") or "")
                + " Reopen it (with a changed brief or a fixed blocker) to "
                  "start it again.")
        elif it["execution_state"] == "held" and not it.get("waiting_line"):
            it["waiting_line"] = (
                f"held — source {it.get('source')!r} is never auto-dispatched; "
                "a human (or the director session) takes it")


def _liveness(items: list[dict], agents: list[dict]) -> None:
    """IS IT WORKING, OR IS IT WEDGED? Stamped onto the item, not just the agent.

    A dispatched row carried no signal of its own. `num_turns` and
    `total_cost_usd` are written at COMPLETION, so both sit at 0 for the whole
    of a run — the two numbers that look like progress are precisely the two
    that cannot report it while you need them. The only way to tell a working
    agent from a wedged one was to go and `stat` the log file by hand.

    The dispatcher already measures it (`_last_output_age_s`, which watches the
    log AND files written under .bgate_out / game assets, so a long atomic image
    batch is not mistaken for a corpse) and already puts it on the AGENT row.
    Items and agents are two lists in one payload and nothing joined them, so
    the number existed and the card that needed it did not have it.

    `progress` is the word a reader wants:
        working  output within the quiet threshold
        quiet    silent a while — often legitimate, an atomic call writes
                 nothing until it returns
        stalled  silent past the dispatcher's own stall ceiling; this is what
                 the watchdog would kill on
    """
    by_item = {int(a["item_id"]): a for a in (agents or [])
               if a.get("item_id") is not None}
    for it in items:
        agent = by_item.get(int(it["id"]))
        if agent is None:
            continue
        silent = agent.get("last_output_s")
        it["last_output_s"] = silent
        it["run_seconds"] = agent.get("seconds")
        it["runner"] = agent.get("runner") or ""
        # cost_usd from the live entry, NOT total_cost_usd from the row: the
        # column is written at completion and reads 0.00 for the whole run.
        it["live_cost_usd"] = agent.get("cost_usd")
        it["cost_tracked"] = agent.get("cost_tracked", True)
        if silent is None:
            it["progress"] = "unknown"
            continue
        it["progress"] = ("stalled" if silent >= _STALL_S
                          else "quiet" if silent >= _QUIET_S else "working")
        if it["progress"] != "working":
            it["progress_why"] = (
                f"no log line and no file written for {int(silent) // 60}m"
                + (" — past the stall ceiling; the watchdog kills at this "
                   "point unless an MCP call is in flight"
                   if it["progress"] == "stalled" else
                   ". Often legitimate: an atomic image or engine call writes "
                   "nothing until it returns"))


#: Mirrors the dispatcher's own threshold so the card and the watchdog cannot
#: disagree about what silence means. Imported lazily — this module is on the
#: three-second poll and dispatch drags the whole runner stack in.
def _stall_s() -> int:
    try:
        from bgate_ui.agents import dispatch as _d

        return int(getattr(_d, "STALL_S", 900))
    except Exception:
        return 900


_STALL_S = _stall_s()
_QUIET_S = max(60, _STALL_S // 4)


def _execution_state(item: dict, unsatisfied: list[dict]) -> str:
    """ready | running | waiting | blocked | held | exhausted | <status>.

    `waiting` means an ordinary predecessor has not landed yet: the board is
    working, and nobody should touch it. `blocked` means one never will on its
    own. `exhausted` means the harness stopped buying rounds for this item and
    a person now owns it - it used to be indistinguishable from fresh queued
    work, and the only tell was reading two counters off the row and comparing
    them to a setting by hand.
    """
    status = str(item.get("status") or "")
    if status == "dispatched":
        return "running"
    if status != "queued":
        return status
    if item.get("exhausted_at"):
        return "exhausted"
    if item.get("held"):
        return "held"
    if any(d["status"] in ("failed", "cancelled") for d in unsatisfied):
        return "blocked"
    if unsatisfied:
        return "waiting"
    return "ready"


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
    from bgate_core.board import gates as _gatemode

    try:
        mode = _gatemode.mode(root_dir)
    except Exception:
        # An unreadable mode must not blank the board. DEFAULT is 'agent', the
        # behaviour that shipped before the setting existed.
        mode = _gatemode.DEFAULT
    out: list[dict] = []
    # failure-escalation rows are in this list since 2026-08-19 - before
    # that they appeared on NO rail at all: held from every dispatcher AND
    # absent from the one view that lists what waits on a person, so a
    # failed-past-its-cap item simply vanished until somebody went digging.
    # They are auto-dispatchable now (a director agent may take one), so a
    # QUEUED one is awaiting a ruling and a DISPATCHED one is being handled -
    # `blocking` says which, and only 'qa-gate' rows are never a human's.
    for row in conn.execute(
            "SELECT id, seat, title, status, source, source_ref, created_at "
            "FROM work_item WHERE source IN ('qa-gate', 'qa-gate-escalation', "
            "'failure-escalation') "
            "AND status IN ('queued', 'dispatched') ORDER BY id DESC LIMIT 20"):
        ref = (row["source_ref"] or "").strip()
        out.append({
            "kind": "qa" if row["source"] == "qa-gate" else "escalation",
            "id": f"gate_item_{row['id']}",
            "item_id": int(row["id"]),
            "over_item_id": int(ref) if ref.isdigit() else None,
            "title": row["title"],
            "seat": row["seat"],
            "status": row["status"],
            "blocking": (row["source"] != "qa-gate"
                         and row["status"] == "queued"),
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
        cands: list[tuple[int, dict]] = []
        for art in _artifacts.list_revisions(root_dir, status="candidate")[:60]:
            item_id = art.get("work_item_id")
            if not item_id or int(item_id) not in active:
                continue
            cands.append((int(item_id), art))
        # THE SEAT IS WHOEVER PRODUCED THE CANDIDATE, NOT ALWAYS ART. This said
        # "art" for every candidate, while item_id named the real producing row:
        # cinematic.py, music.py and storyboard.py all call artifacts.register,
        # so a cinematic shot or a music cue raised a gate addressed to a seat
        # that had no work. Anything reading the gate BY SEAT then pointed at
        # the wrong room - the studio floor walked the art character to the
        # Director's door carrying a cinematic item's title, and the seat that
        # was actually blocked showed nothing.
        seat_of: dict[int, str] = {}
        if cands:
            ids = sorted({item_id for item_id, _ in cands})
            marks = ",".join("?" * len(ids))
            for row in conn.execute(
                    f"SELECT id, seat FROM work_item WHERE id IN ({marks})", ids):
                seat_of[int(row["id"])] = row["seat"] or ""
        for item_id, art in cands:
            out.append({
                "kind": "art",
                "id": f"gate_art_{art['id']}",
                "artifact_id": int(art["id"]),
                "item_id": item_id,
                "over_item_id": item_id,
                "title": art.get("logical_name") or f"candidate {art['id']}",
                # Falls back to 'art' only when the row is gone: a candidate
                # with no seat at all would be a gate no reader could place.
                "seat": seat_of.get(item_id) or "art",
                "status": "candidate",
                "blocking": True,
                "path": art.get("path") or "",
                "gate_mode": mode,
            })
    return out


def _questions(root_dir) -> list[dict]:
    """Open ``ask_human`` questions — what an agent is waiting on a human for.

    A question is an event rather than a work item (see bgate_core.board.steerbox): a
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
        from bgate_core.store import settings as _settings
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
        "WHEN 'integrating' THEN 1 WHEN 'review' THEN 2 WHEN 'queued' THEN 3 "
        "WHEN 'failed' THEN 4 "
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

    # The live agents, in detail: the card wants the last step, the graph wants
    # the phases, and both come out of one read of the feed.
    live_steps: dict[str, list] = {}
    live_phases: dict[str, list] = {}
    # WHICH CLAUDE SESSION EACH LIVE AGENT IS, keyed by item id. The board, the
    # graph and the floor all drill down to an item, and any of them can offer
    # to hand that run to a terminal.
    live_sessions: dict[str, str] = {}
    if steps:
        by_item = _artifacts_by_item(r, live_ids)
        for item_id in sorted(live_ids):
            feed = _dispatch.read_activity(str(r), item_id, limit=0)
            all_steps = feed.get("steps") or []
            live_steps[str(item_id)] = all_steps[-STEPS:]
            live_sessions[str(item_id)] = feed.get("session_id") or ""
            live_phases[str(item_id)] = _phases_for(
                r, item_id, feed, all_steps, by_item.get(item_id, []))

    counts = {row["status"]: int(row["n"]) for row in conn.execute(
        "SELECT status, count(*) AS n FROM work_item GROUP BY status")}

    # Work in flight — what a gate is allowed to hang off.
    active = {int(row["id"]) for row in conn.execute(
        "SELECT id FROM work_item WHERE status IN "
        "('queued', 'dispatched', 'integrating')")}
    active |= live_ids

    _chain_state(conn, items)
    _liveness(items, agents)

    # WHETHER EACH FAILURE HAS ALREADY BEEN ESCALATED, so the failed rail can
    # offer the button once and say "escalated" after - without this flag the
    # only signal was a refusal toast from the once-per-item cap.
    failed_ids = [str(it["id"]) for it in items if it.get("status") == "failed"]
    if failed_ids:
        from bgate_ui.agents import followup as _followup
        marks = ", ".join("?" * len(failed_ids))
        esc = {str(row["source_ref"]) for row in conn.execute(
            "SELECT source_ref FROM work_item WHERE source = ? "
            f"AND source_ref IN ({marks})",
            (_followup.FAIL_ESCALATION_SOURCE, *failed_ids))}
        for it in items:
            if it.get("status") == "failed":
                it["escalated"] = str(it["id"]) in esc

    questions = _questions(r)
    _question_reminders(r, questions)

    from bgate_core.board import gates as _gatemode
    return {
        "items": items,
        "agents": agents,
        "lineage": _lineage(r),
        "gates": _gates(r, conn, active),
        "questions": questions,
        "steps": live_steps,
        "phases": live_phases,
        "sessions_by_item": live_sessions,
        "collab": _collab(r, conn, active),
        "autopilot": _autodeploy.state(r),
        "dismissed_attention": list(
            (_ws.get(r, SEAT, ATTENTION_KEY, {}).get("keys") or {}).keys()),
        "gate": _gatemode.state(r),
        # WHETHER THE DIRECTOR IS MID-REPLY. Chat turns stopped being work
        # items, so floor.running no longer covers a streaming director
        # answer - and floorIsQuiet's whole job is 'one source of words on
        # screen'. This is the shared signal both the chat pane and the
        # lounge read, so they cannot disagree about whether the director is
        # talking. Best-effort: the lounge must not die if the session
        # module cannot answer.
        "director": _director_running(r),
        "floor": {
            "running": len(live_ids),
            "queued": counts.get("queued", 0),
            "dispatched": counts.get("dispatched", 0),
            "integrating": counts.get("integrating", 0),
            "review": counts.get("review", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
        },
    }


@router.post("/api/console/attention/dismiss")
def console_attention_dismiss(payload: dict) -> dict:
    """Hide one attention snapshot without changing the underlying work item."""
    key = str(payload.get("key") or "").strip()
    if (not key.startswith(("item:", "gate:", "question:"))
            or len(key) > 240 or any(ch in key for ch in "\r\n\0")):
        raise _api.bad_request("a valid attention key is required")
    dismissed = payload.get("dismissed") is not False
    now = datetime.now(timezone.utc).isoformat()

    def _change(doc: dict) -> dict:
        keys = dict(doc.get("keys") or {})
        if dismissed:
            keys[key] = now
        else:
            keys.pop(key, None)
        # This is a UI acknowledgement log, not history. Bound it so old
        # projects do not carry every dismissed transient forever.
        if len(keys) > 500:
            keys = dict(sorted(keys.items(), key=lambda row: row[1])[-500:])
        doc["keys"] = keys
        return doc

    _ws_update(root(), ATTENTION_KEY, _change)
    return {"ok": True, "key": key, "dismissed": dismissed}


@router.post("/api/console/escalate")
def console_escalate(payload: dict) -> dict:
    """Escalate one FAILED item to the director, by hand.

    The automatic escalation paths only see recent failures (the event batch
    and sweep_failed's 12-hour window), so anything that failed while the
    server was down aged out with no route to a decider. This is the human's
    button for exactly those. Same branch, same guards - one escalation per
    item ever, and an item that is itself an escalation refuses.
    """
    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        raise _api.bad_request("item_id (int) is required")
    reason = str(payload.get("reason") or "").strip()
    r = root()
    try:
        item = _queue.get(r, item_id)
    except LookupError:
        raise _api.not_found(f"no work item {item_id}")
    if item["status"] != "failed":
        raise _api.bad_request(
            f"item {item_id} is {item['status']} — only a failed item "
            "escalates")
    from bgate_ui.agents import followup as _followup
    out = _followup.escalate_failure(r, item_id, reason)
    if not out.get("ok"):
        raise _api.bad_request(str(out.get("why") or "refused"))
    return {"ok": True, "item_id": item_id,
            "escalation": out.get("escalation"),
            "session": bool(out.get("session"))}


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
                {"code": "tick_failed", "message": _api.safe_error(exc)}]}
    return {**_autodeploy.state(r), "tick": tick,
            "was": {"on": not on}, "now": state["on"]}
