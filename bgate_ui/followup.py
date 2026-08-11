"""The follow-up router — one subscriber that decides what happens after a finish.

WHAT WAS BROKEN. An agent finished and nobody was told. Under the agent gate a
QA reviewer was spawned; under the builder's gate the item parked in ``review``;
in both cases the DIRECTOR — the seat that owns what happens next — heard
nothing, the chain advanced silently, and a human who was not staring at the
right console view found out later or not at all. Three features were needed to
fix that (spawn the reviewer, debrief the director, ring something), and the
established shape in this tree would have made each of them a daemon thread with
its own cutoff, its own cooldown and its own copy of "recent". ``qa_gate`` is the
cost of that shape: it reviewed only transitions after the server started, so
every completion that happened while the dashboard was down was never reviewed
and nothing said so.

So this is ONE subscriber on ONE cursor over ``bgate_core.events``, and it
absorbs ``qa_gate``'s loop rather than sitting next to it. Being cursor-driven is
what closes the startup hole: the cursor is a row id, so a dashboard that was off
for an hour resumes exactly where it stopped instead of pretending the hour did
not happen.

THE FIVE BRANCHES, in the order the plan states them:

    1. item.failed        reopen it (if followup.auto_reopen_failures) or leave it
    2. gate mode 'agent'  today's QA spawn, behaviour unchanged
    3. gate mode 'builders'  the item is held in review; say so and do nothing
                          else — the chain staying blocked is that mode's point
    4. a chain successor became ready  emit chain.advanced so the handoff is
                          legible while it happens rather than inferred after
    5. done and nothing follows  the director debrief (opt-in, leashed)

PURE CORE, THREADED EDGE. :func:`decide` is a function of (events, settings,
board) returning a list of action dicts and does NO I/O — no database, no clock,
no settings read. :func:`snapshot` gathers the facts, :func:`apply_action`
performs them, :func:`tick` joins the three. ``qa_gate``'s tests have to call
``_scan_once`` directly *because* that module cannot be split that way, and the
bug that made those tests necessary (a placeholder/parameter mismatch) lived
inside the un-decomposable part for months.

DELIVERY IS AT-LEAST-ONCE, SO EVERY ACTION CARRIES A GUARD. A subscriber that
acts and then dies before its cursor lands acts again on restart: double QA
spawn, double debrief, double webhook, double reopen. ``events.cursor_set`` is
deliberately unversioned and best-effort for exactly that reason, which pushes
the responsibility here. Every action dict has a ``guard`` string and
:func:`apply_action` re-runs that guard as one query immediately before acting —
the same trick ``qa_gate._open_gate_exists`` has always used.

CATCHING UP IS NOT ALWAYS DESIRABLE. Eight hours down must not fire eight hours
of pings and debriefs. Notifications COLLAPSE on resume ("11 items finished while
you were away") and a debrief is SKIPPED past ``followup.max_age_min`` with one
notice instead of a spawn. A first run with no cursor starts at the head of the
log, not the beginning of history.

THE WEBHOOK IS THE ONLY THING HERE THAT LEAVES THE MACHINE, and it ships off. A
loopback service POSTing a user-supplied URL is an SSRF and an exfiltration path,
so: https only, no private or link-local addresses, one attempt, short timeout,
failures to the activity ledger. See :func:`webhook_target`.
"""
from __future__ import annotations

import calendar
import json
import os
import threading
import time
from typing import Iterable, Optional

from bgate_core import (
    activity, db, events as _events, gates as _gates, queue as _queue,
    settings as _settings, spend as _spend, writelog,
)
from bgate_ui import qa_gate as _qa_gate
from bgate_ui.pump import Pump

# The router's own cursors. Two, not one: the ROUTING cursor advances as soon as
# a batch has been acted on, while the NOTICE cursor only advances when a notice
# was actually deliverable. Sharing one cursor means a quiet-hours window either
# blocks the routing (a QA agent that does not get spawned until morning) or
# silently eats the notices it was supposed to hold and collapse.
CONSUMER = "followup"
NOTIFY_CONSUMER = "followup.notify"

# Slower than autodeploy's 4s: nothing here is latency-critical (the QA spawn was
# on a 10s loop before this), and every tick is a handful of queries.
POLL_S = 6.0

# Events read per tick. `more` on the batch means the next tick runs immediately,
# so this bounds one pass rather than the backlog.
BATCH = 100

# More than this many notice-worthy events in one batch collapse into a single
# line. Three is the point where a list stops being read as individual facts.
COLLAPSE_AT = 3

# The source stamped on a debrief item, and the sources that never GET one.
# A completion loop that debriefs its own debriefs is the money pump
# qa.max_rounds exists to stop; 'chat' is a message to the director (the console
# dispatches those itself) and the escalation exists precisely because a human
# has to decide.
DEBRIEF_SOURCE = "completion"
NEVER_DEBRIEF_SOURCES = ("qa-gate", "qa-gate-escalation", "completion", "chat")

# Below the QA gate (8) and the escalation (9): a debrief is a decision about
# work that already landed, so it must not outrank verifying that it landed.
DEBRIEF_PRIORITY = 7

# The ledger kind every notice is written under, and the marker that makes a
# notice idempotent. The marker is inside the summary because `activity` has no
# spare column and adding one for this would be a migration for a dedupe key.
LEDGER_KIND = "followup"

# One attempt, short timeout. A webhook that retries is a webhook that can be
# turned into an amplifier, and a slow endpoint must not hold the router's tick.
WEBHOOK_TIMEOUT_S = 5.0
WEBHOOK_MAX_BODY = 8000

# How long the event log keeps rows, and how often the tick trims it. The drawer
# tells the human "the log keeps 14 days" when it reports a gap, so somebody has
# to actually be deleting: without a caller, events.prune() is dead code and the
# table grows for the life of the project while the UI promises otherwise. Once
# every ~500 ticks is roughly hourly at POLL_S, which is far more often than a
# fortnight-deep window needs and still one query.
KEEP_DAYS = 14
PRUNE_EVERY = 500

# How often the tick also runs qa_gate's backstop sweep. events.emit is
# best-effort by design (it returns 0 rather than raising when the database is
# locked), so a completion whose event was never recorded would never be
# reviewed by a purely cursor-driven router. The sweep is idempotency-guarded by
# the same queries the event path uses, so running both costs nothing but finds
# the lost ones.
SWEEP_EVERY = 10

# Kinds that reach a branch at all. Anything else is notice-only.
_ROUTED = ("item.failed", "item.review", "item.done", "item.approved")

_lock = threading.Lock()
_ticks: dict[str, int] = {}
# The daemon loop itself is built at the bottom of this module, next to the
# catch-up function it wraps.


# ---------------------------------------------------------------------------
# Settings — read once per tick, passed into decide() as data
# ---------------------------------------------------------------------------
def load_settings(root: str | os.PathLike[str]) -> dict:
    """The registry values this router reads, as one plain dict.

    Gathered here so :func:`decide` can be called from a literal dict in a test
    and so one tick cannot see two different gate modes half-way through. Every
    read falls back to the registry default rather than raising: a router that
    refuses to run because a settings doc will not parse is a board that stops
    routing for a reason nobody can see.
    """
    def _get(key: str, fallback):
        try:
            return _settings.get(root, key)
        except Exception:
            return fallback

    try:
        mode = _gates.mode(root)
    except Exception:
        mode = _gates.DEFAULT
    return {
        "gate_mode": mode,
        "director_debrief": bool(_get("followup.director_debrief", False)),
        "max_per_hour": int(_get("followup.max_per_hour", 4) or 4),
        "max_age_min": int(_get("followup.max_age_min", 30) or 30),
        "auto_reopen_failures": bool(_get("followup.auto_reopen_failures", False)),
        "max_rounds": int(_get("qa.max_rounds", _qa_gate.MAX_ROUNDS)
                          or _qa_gate.MAX_ROUNDS),
        # Per project, not per machine. Falls back to the module default rather
        # than to an empty list: an unreadable settings doc must not silently
        # mean "review nothing".
        "gated_seats": tuple(_get("qa.gated_seats", _qa_gate.GATED_SEATS)
                             or _qa_gate.GATED_SEATS),
        "notify_kinds": list(_get("notify.kinds", ()) or ()),
        "in_app": bool(_get("notify.in_app", True)),
        "webhook": str(_get("notify.webhook", "") or ""),
        "quiet_hours": str(_get("notify.quiet_hours", "") or ""),
    }


def in_quiet_hours(window: str, when: Optional[time.struct_time] = None) -> bool:
    """Is now inside a ``23:00-07:00`` style window? Empty window means never.

    LOCAL time, deliberately: the window exists because a human is asleep, and
    they are asleep on their own clock rather than on the UTC the database
    stores. Wraparound is the normal case, which is why this is not a naive
    ``lo <= t <= hi``.
    """
    text = str(window or "").strip()
    if not text:
        return False
    try:
        lo_text, hi_text = text.split("-", 1)
        lo_h, lo_m = (int(part) for part in lo_text.strip().split(":"))
        hi_h, hi_m = (int(part) for part in hi_text.strip().split(":"))
    except Exception:
        return False              # a malformed window delivers, it does not mute
    now = when or time.localtime()
    minute = now.tm_hour * 60 + now.tm_min
    lo, hi = lo_h * 60 + lo_m, hi_h * 60 + hi_m
    if lo == hi:
        return False
    if lo < hi:
        return lo <= minute < hi
    return minute >= lo or minute < hi   # the window crosses midnight


# ---------------------------------------------------------------------------
# Reading the bus
# ---------------------------------------------------------------------------
def _age_min(created_at: str, now_s: float) -> float:
    """Minutes since an event was recorded. 0.0 when the stamp is unreadable.

    created_at is SQLite's ``datetime('now')`` — UTC, second resolution — so it
    is parsed as UTC rather than through ``time.mktime``, which would silently
    shift every age by the machine's offset and make the staleness rule wrong by
    hours for most of the world. An unparseable stamp reads as FRESH: it can only
    come from a corrupt row, and refusing to act on a completion because of one
    is a worse failure than acting on one that was late.
    """
    try:
        stamp = calendar.timegm(time.strptime(str(created_at)[:19],
                                              "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0
    return max(0.0, (now_s - stamp) / 60.0)


def _event_item(ev: dict) -> int:
    """The item id an event is about, from its payload, else its ref."""
    payload = ev.get("payload") or {}
    try:
        return int(payload.get("item") or ev.get("ref") or 0)
    except (TypeError, ValueError):
        return 0


def summarize_pending(batch: dict) -> dict:
    """Turn a filtered ``events.since`` batch into the notice decision's input.

    ``{"count", "more", "gap", "seq", "head", "by_kind", "samples"}``. It is a
    summary rather than the whole list because the only decisions it feeds are
    "one line or a collapsed line" and "what does that line say" — handing
    :func:`decide` two hundred events to count would put the collapse rule and
    the batch size in two places.
    """
    found = list(batch.get("events") or [])
    by_kind: dict[str, int] = {}
    for ev in found:
        kind = str(ev.get("kind") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"count": len(found), "more": bool(batch.get("more")),
            "gap": bool(batch.get("gap")), "seq": int(batch.get("seq") or 0),
            "head": int(batch.get("head") or 0), "by_kind": by_kind,
            "samples": found[:COLLAPSE_AT + 1]}


def snapshot(root: str | os.PathLike[str], batch: Iterable[dict], *,
             main_seq: int = 0, notify_seq: int = 0,
             pending: Optional[dict] = None,
             settings: Optional[dict] = None) -> dict:
    """Every fact :func:`decide` needs, gathered in one place.

    This is the I/O half. It exists so the decision can be a pure function: the
    item rows, the successors, the idempotency guards ("is a QA round already
    open", "has this chain been debriefed"), the rate-cap count, whether a
    dispatcher is even reachable, and the clock all come from here. Without the
    split, every rule in this file could only be tested by seeding a database and
    running a thread — which is the situation that let ``qa_gate`` be dead in
    production with tests passing.
    """
    events = list(batch or [])
    settings = settings or {}
    now_s = time.time()
    board: dict = {
        "now_s": now_s,
        "main_seq": int(main_seq or 0),
        "notify_seq": int(notify_seq or 0),
        "pending": pending or summarize_pending({}),
        "quiet": in_quiet_hours(str(settings.get("quiet_hours") or "")),
        "dispatcher": dispatcher_live(root),
        "items": {},
        "qa": {},
        "successors": {},
        "debrief_open": {},
        "debriefs_last_hour": _debriefs_since(root, minutes=60),
        "advanced": {},
        "age_min": {},
    }
    wanted: set[int] = set()
    for ev in events:
        board["age_min"][int(ev.get("id") or 0)] = _age_min(
            ev.get("created_at") or "", now_s)
        item_id = _event_item(ev)
        if item_id:
            wanted.add(item_id)

    # The QA verdict's own completion is the event that releases the ORIGINAL
    # item for a debrief (see _debrief_subject), so its source_ref has to be
    # loaded too or the redirect has nothing to redirect to.
    for item_id in list(wanted):
        item = _item(root, item_id)
        if item is None:
            continue
        board["items"][item_id] = item
        ref = str(item.get("source_ref") or "")
        if item.get("source") == "qa-gate" and ref.isdigit():
            wanted.add(int(ref))
    for item_id in wanted - set(board["items"]):
        item = _item(root, item_id)
        if item is not None:
            board["items"][item_id] = item

    for item_id, item in board["items"].items():
        ref = str(item_id)
        board["qa"][item_id] = {
            "open": _qa_gate._open_gate_exists(root, ref),
            "last": _qa_gate._latest_gate_created(root, ref),
            "escalated": _qa_gate.escalated(root, ref),
        }
        board["successors"][item_id] = [
            {"id": int(s["id"]), "seat": s.get("seat") or "",
             "title": str(s.get("title") or "")[:200],
             "status": s.get("status") or "",
             "chain_pos": int(s.get("chain_pos") or 0)}
            for s in _successors(root, item_id)]
        guard_ref = debrief_ref(item)
        board["debrief_open"][guard_ref] = _debrief_exists(root, guard_ref)
        chain_id = str(item.get("chain_id") or "")
        board["advanced"][f"{chain_id or 'item-' + ref}/{item_id}"] = \
            _advanced_seen(root, chain_id, item_id)
    return board


def _item(root, item_id: int) -> Optional[dict]:
    try:
        return _queue.get(root, int(item_id))
    except Exception:
        return None      # a deleted item is not an error, it is nothing to do


def _successors(root, item_id: int) -> list[dict]:
    try:
        return _queue.successors(root, int(item_id))
    except Exception:
        return []


def debrief_ref(item: dict) -> str:
    """The key one debrief is filed against: the chain, else the item.

    ONE PER CHAIN, not one per link — a five-link chain that debriefed every
    landing would buy five director agents to narrate one piece of work. Chain
    ids are ``c<n>`` and item refs are digits, so the two namespaces cannot
    collide.
    """
    chain_id = str((item or {}).get("chain_id") or "").strip()
    return chain_id or str((item or {}).get("id") or "")


def _debrief_exists(root, guard_ref: str) -> bool:
    """Has a debrief already been filed for this chain/item, in any status?

    ANY status, including done and cancelled: the question is "has the director
    already been asked about this", and a second ask after the first was answered
    is the duplicate this guard exists to stop.
    """
    try:
        row = db.connect(root).execute(
            "SELECT 1 FROM work_item WHERE source = ? AND source_ref = ? LIMIT 1",
            (DEBRIEF_SOURCE, str(guard_ref))).fetchone()
    except Exception:
        # An unreadable board must not be read as "no debrief yet" — that is the
        # direction that spends money. Claim one exists and skip this tick.
        return True
    return row is not None


def _debriefs_since(root, minutes: int = 60) -> int:
    """How many debriefs were filed in the last ``minutes`` — the rate cap's input."""
    try:
        row = db.connect(root).execute(
            "SELECT COUNT(*) AS n FROM work_item WHERE source = ? "
            f"AND created_at >= datetime('now', '-{int(minutes)} minutes')",
            (DEBRIEF_SOURCE,)).fetchone()
    except Exception:
        return 10 ** 6          # unreadable: behave as capped, never as unlimited
    return int(row["n"] or 0) if row else 0


def _advanced_seen(root, chain_id: str, from_item: int) -> bool:
    """Has chain.advanced already been recorded for this handoff?

    The payload is matched with LIKE because ``event`` has one ``ref`` column and
    the ref is the chain (which is what a drawer groups by), so the FROM link
    lives in the payload. That is fragile against a change of key order — and it
    is the right trade here precisely because the cost of a miss is one duplicate
    line in a drawer. The debrief guard, where a miss costs a dispatched agent,
    is an exact query on an indexed column instead.
    """
    try:
        row = db.connect(root).execute(
            "SELECT 1 FROM event WHERE kind = 'chain.advanced' AND ref = ? "
            "AND payload LIKE ? LIMIT 1",
            (str(chain_id or f"item-{from_item}"),
             f'%"from": {int(from_item)},%')).fetchone()
    except Exception:
        return True
    return row is not None


def _noticed(root, event_id: int) -> bool:
    """Has a notice already been written for this event id?

    The ledger is the dedupe store because it is the thing the notice writes
    anyway: one marker, one query, no new table for a key.
    """
    try:
        row = db.connect(root).execute(
            "SELECT 1 FROM activity WHERE kind = ? AND summary LIKE ? LIMIT 1",
            (LEDGER_KIND, f"[e{int(event_id)}] %")).fetchone()
    except Exception:
        return True
    return row is not None


def dispatcher_live(root: str | os.PathLike[str]) -> bool:
    """Can a debrief actually be run from this process?

    A queued row on a dead board looks exactly like delegated work and is not —
    the trap the director protocol already warns about. The router files a
    debrief only when it can see a live dispatcher, and says so in the
    notification when it cannot. In practice that means the claude CLI is
    resolvable: this runs inside the dashboard, which is the process that owns
    dispatch, so the remaining way for a debrief to be undispatchable is a
    missing CLI.
    """
    try:
        from bgate_ui import dispatch as _dispatch

        return bool(_dispatch.find_claude())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The decision — pure, and the whole point of the file
# ---------------------------------------------------------------------------
def _action(kind: str, branch: int, event: dict, guard: str, why: str,
            **extra) -> dict:
    """One action dict. Keys are fixed because other code reads them.

    ``kind`` is what to do, ``branch`` is which rule decided it (1-5, 0 for a
    notice), ``event`` is the event id that caused it, ``guard`` is the
    idempotency key :func:`apply_action` re-checks, ``why`` is the sentence a
    human reads in the ledger when it happens or does not.
    """
    return {"kind": kind, "branch": branch, "event": int(event.get("id") or 0),
            "guard": guard, "why": why, **extra}


def decide(events: Iterable[dict], settings: dict, board: dict) -> list[dict]:
    """What to do about a batch of events. NO side effects, no I/O, no clock.

    Returns a list of action dicts — ``reopen``, ``qa_spawn``, ``qa_escalate``,
    ``emit``, ``debrief``, ``notify``, and ``skip`` for a decision that chose to
    do nothing (returned rather than dropped, so "why did nothing happen" is
    answerable). Feed it a literal list of events and a literal board and it is
    fully testable; that separation is why this router does not repeat
    ``qa_gate``'s history of being dead in production with green tests.
    """
    actions: list[dict] = []
    items = board.get("items") or {}
    main_seq = int(board.get("main_seq") or 0)
    filed = int(board.get("debriefs_last_hour") or 0)
    cap_per_hour = int(settings.get("max_per_hour") or 4)

    for ev in events:
        event_id = int(ev.get("id") or 0)
        if event_id <= main_seq:
            continue        # already routed; only the notice path replays these
        kind = str(ev.get("kind") or "")
        if kind not in _ROUTED:
            continue
        item_id = _event_item(ev)
        item = items.get(item_id)
        if item is None:
            actions.append(_action("skip", 0, ev, f"item:{item_id}",
                                   "the item this event names no longer exists"))
            continue

        # -- branch 1: failed ------------------------------------------------
        if kind == "item.failed":
            actions.extend(_branch_failed(ev, item, settings))
            continue

        # -- branch 3: the builder's gate is holding it ----------------------
        if kind == "item.review":
            actions.append(_action(
                "skip", 3, ev, f"item:{item_id}",
                "held in review for your approval — the chain behind it stays "
                "blocked, which is what the builder's gate is for"))
            continue

        # -- branch 2: the agent gate's QA spawn -----------------------------
        owned = False
        qa_actions = _branch_qa(ev, item, settings, board)
        if qa_actions:
            actions.extend(qa_actions)
            owned = any(a["kind"] != "skip" for a in qa_actions)

        # -- branch 4: a successor became ready ------------------------------
        advanced = _branch_chain(ev, item, board)
        actions.extend(advanced)
        if any(a["kind"] == "emit" for a in advanced) or \
                board.get("successors", {}).get(item_id):
            # Something follows, so branch 5 does not apply: the debrief is for
            # the END of a piece of work, and autodeploy picks the successor up
            # on its own now that it is ready.
            continue

        # -- branch 5: done, nothing follows -> the director debrief ---------
        if owned:
            # A QA round was just opened against this item. Debriefing now would
            # narrate work a reviewer is about to reopen; the debrief follows the
            # verdict instead (see _debrief_subject).
            actions.append(_action("skip", 5, ev, f"item:{item_id}",
                                   "a QA round is in flight — the debrief waits "
                                   "for the verdict"))
            continue
        made, filed = _branch_debrief(ev, item, settings, board, filed,
                                      cap_per_hour)
        actions.extend(made)

    actions.extend(_notice_actions(settings, board))
    return actions


def _branch_failed(ev: dict, item: dict, settings: dict) -> list[dict]:
    """Branch 1. Reopen the failure, or leave it and say why it was left."""
    item_id = int(item["id"])
    attempts = int(item.get("attempts") or 0)
    cap = int(settings.get("max_rounds") or _qa_gate.MAX_ROUNDS)
    rounds = attempts + 1
    if not settings.get("auto_reopen_failures"):
        # No notice of its own: item.failed is in the default notify.kinds, so
        # the notice path already tells the human. A second line for the same
        # failure is how a channel earns its mute.
        return [_action("skip", 1, ev, f"item:{item_id}",
                        "followup.auto_reopen_failures is off — the failure is "
                        "left for a human, which is the default")]
    if rounds >= cap:
        return [_action(
            "notify", 1, ev, f"activity:e{int(ev.get('id') or 0)}",
            f"#{item_id} has failed {rounds} time(s) and will not be reopened "
            "automatically again",
            cause="failure_capped", count=1, refs=[str(item_id)],
            kinds=["item.failed"],
            summary=f"#{item_id} [{item.get('seat') or ''}] failed on attempt "
                    f"{rounds} of {cap} — the retry cap is reached, so it is "
                    "waiting for a human",
            detail=str(item.get("result") or "")[:600])]
    return [_action(
        "reopen", 1, ev, f"item:{item_id}:failed",
        f"auto-reopening #{item_id} for attempt {rounds + 1} of {cap}",
        item=item_id, attempts=attempts,
        reason=("AUTO-REOPENED by the follow-up router — the previous attempt "
                f"reported FAILED (attempt {rounds} of {cap}). What it said:\n\n"
                + (str(item.get("result") or "(no result note)")[:1200])))]


def _branch_qa(ev: dict, item: dict, settings: dict, board: dict) -> list[dict]:
    """Branch 2. Today's QA spawn, unchanged, now driven by the cursor.

    Empty list means this branch has no opinion (wrong gate mode, ungated seat,
    or the gate's own item) and the later branches get the event.
    """
    if str(ev.get("kind")) not in ("item.done", "item.approved"):
        return []
    if str(settings.get("gate_mode")) != _gates.AGENT:
        return []
    seat = str(item.get("seat") or "")
    source = str(item.get("source") or "")
    if seat not in (settings.get("gated_seats") or _qa_gate.GATED_SEATS):
        return []
    if source in ("qa-gate", _qa_gate.ESCALATION_SOURCE):
        return []          # never gate the gate
    if int(item.get("gate_skip") or 0):
        return []          # a human closed this by hand; see queue.complete
    item_id = int(item["id"])
    qa = (board.get("qa") or {}).get(item_id) or {}
    cap = int(settings.get("max_rounds") or _qa_gate.MAX_ROUNDS)
    rounds = int(item.get("attempts") or 0) + 1
    if rounds > cap:
        if qa.get("escalated"):
            return [_action("skip", 2, ev, f"escalation:{item_id}",
                            "already escalated to the director once")]
        return [_action("qa_escalate", 2, ev, f"escalation:{item_id}",
                        f"#{item_id} has been through {rounds - 1} QA rounds — a "
                        "human decides from here",
                        item=item_id, rounds=rounds - 1)]
    if qa.get("open"):
        return [_action("skip", 2, ev, f"qa-gate:{item_id}",
                        "a QA round for this item is already open")]
    last = str(qa.get("last") or "")
    if last and str(item.get("updated_at") or "") <= last:
        return [_action("skip", 2, ev, f"qa-gate:{item_id}",
                        "already reviewed this round — the item has not moved "
                        "since the last gate was filed")]
    return [_action("qa_spawn", 2, ev, f"qa-gate:{item_id}",
                    f"QA round {rounds} of {cap} for #{item_id}",
                    item=item_id, rounds=rounds, cap=cap)]


def _branch_chain(ev: dict, item: dict, board: dict) -> list[dict]:
    """Branch 4. Emit chain.advanced when the next link just became runnable.

    Nothing is DISPATCHED here — autodeploy picks the successor up now that its
    predecessor is done, and a second dispatcher would race it. What is missing
    without this is the narration: link 2 starting because link 1 landed was only
    ever inferrable after the fact, from two unrelated rows.
    """
    if str(ev.get("kind")) not in ("item.done", "item.approved"):
        return []
    if str(item.get("status") or "") != "done":
        return []          # a held item releases nothing
    item_id = int(item["id"])
    following = (board.get("successors") or {}).get(item_id) or []
    if not following:
        return []
    chain_id = str(item.get("chain_id") or "")
    key = f"{chain_id or 'item-' + str(item_id)}/{item_id}"
    if (board.get("advanced") or {}).get(key):
        return [_action("skip", 4, ev, f"advanced:{key}",
                        "this handoff has already been announced")]
    nxt = following[0]
    return [_action(
        "emit", 4, ev, f"advanced:{key}",
        f"#{item_id} landed — #{nxt['id']} [{nxt['seat']}] is now ready",
        emit_kind="chain.advanced", ref=chain_id or f"item-{item_id}",
        # "from" is first so the LIKE guard in _advanced_seen can find it.
        payload={"from": item_id, "chain_id": chain_id,
                 "from_seat": item.get("seat") or "",
                 "from_title": str(item.get("title") or "")[:200],
                 "to": int(nxt["id"]), "to_seat": nxt["seat"],
                 "to_title": nxt["title"], "to_pos": nxt["chain_pos"],
                 "to_status": nxt["status"],
                 "waiting": len(following)})]


def _debrief_subject(ev: dict, item: dict, board: dict) -> Optional[dict]:
    """Which item a debrief would be ABOUT — not always the event's own item.

    Under the agent gate the maker item reaches ``done`` and a QA round opens
    immediately, so the completion worth debriefing is the one the VERDICT
    closes. Without this redirect the debrief would be dead under the gate mode
    that ships by default: the maker item is busy being reviewed, and the QA
    item's own source is on the never-debrief list.
    """
    if str(item.get("source") or "") == "qa-gate":
        ref = str(item.get("source_ref") or "")
        if not ref.isdigit():
            return None
        original = (board.get("items") or {}).get(int(ref))
        if original is None:
            return None
        if str(original.get("status") or "") != "done":
            # The verdict was a FAIL and the original is back in the queue —
            # there is nothing finished to debrief.
            return None
        return original
    return item


def _branch_debrief(ev: dict, item: dict, settings: dict, board: dict,
                    filed: int, cap_per_hour: int) -> tuple[list[dict], int]:
    """Branch 5. The director debrief, behind every guard the plan asks for.

    Returns (actions, running debrief count) — the count is threaded through the
    loop so a single batch containing twenty completions cannot file twenty
    debriefs by each one reading the same pre-batch total.
    """
    event_id = int(ev.get("id") or 0)
    if not settings.get("director_debrief"):
        # No notice: a feature that is off must not announce itself on every
        # completion. It is off by default and stays off on upgrade.
        return ([_action("skip", 5, ev, "setting:followup.director_debrief",
                         "followup.director_debrief is off")], filed)
    subject = _debrief_subject(ev, item, board)
    if subject is None:
        return ([_action("skip", 5, ev, f"item:{int(item['id'])}",
                         "nothing finished here to debrief")], filed)
    subject_id = int(subject["id"])
    source = str(subject.get("source") or "")
    if source in NEVER_DEBRIEF_SOURCES:
        return ([_action("skip", 5, ev, f"source:{source}",
                         f"source {source!r} never gets a debrief — a completion "
                         "loop that debriefs its own debriefs is a money pump")],
                filed)
    if (board.get("successors") or {}).get(subject_id):
        return ([_action("skip", 5, ev, f"item:{subject_id}",
                         "something follows this — branch 4 owns it")], filed)
    guard_ref = debrief_ref(subject)
    if (board.get("debrief_open") or {}).get(guard_ref):
        return ([_action("skip", 5, ev, f"debrief:{guard_ref}",
                         "a debrief for this chain has already been filed")],
                filed)
    age = float((board.get("age_min") or {}).get(event_id) or 0.0)
    max_age = int(settings.get("max_age_min") or 30)
    if age > max_age:
        return ([_action(
            "notify", 5, ev, f"activity:e{event_id}",
            "the completion is too old to debrief",
            cause="debrief_stale", count=1, refs=[str(subject_id)],
            kinds=[str(ev.get("kind") or "")],
            summary=(f"#{subject_id} [{subject.get('seat') or ''}] finished "
                     f"{int(age)} min ago — past followup.max_age_min "
                     f"({max_age}), so no debrief was filed"),
            detail=("The board has moved on since. Read the item if it still "
                    "matters; nothing was dispatched."))], filed)
    if filed >= cap_per_hour:
        return ([_action(
            "notify", 5, ev, f"activity:e{event_id}",
            "the debrief rate cap is reached",
            cause="debrief_capped", count=1, refs=[str(subject_id)],
            kinds=[str(ev.get("kind") or "")],
            summary=(f"#{subject_id} finished but followup.max_per_hour "
                     f"({cap_per_hour}) is reached — no debrief was filed"),
            detail="Raise followup.max_per_hour if a busy board should buy more "
                   "director agents than this.")], filed)
    if not board.get("dispatcher"):
        return ([_action(
            "notify", 5, ev, f"activity:e{event_id}",
            "no live dispatcher, so no debrief was filed",
            cause="no_dispatcher", count=1, refs=[str(subject_id)],
            kinds=[str(ev.get("kind") or "")],
            summary=(f"#{subject_id} [{subject.get('seat') or ''}] finished and "
                     "nothing follows it, but there is no live dispatcher — no "
                     "debrief was filed"),
            detail=("A queued row on a dead board looks like delegated work and "
                    "is not, so none was created. Start `bgate serve` (and check "
                    "the claude CLI is on PATH) and the next completion will "
                    "debrief."))], filed)
    return ([_action(
        "debrief", 5, ev, f"debrief:{guard_ref}",
        f"debriefing the director on #{subject_id}",
        item=subject_id, chain_id=str(subject.get("chain_id") or ""),
        guard_ref=guard_ref, cap=cap_per_hour)], filed + 1)


def _notice_actions(settings: dict, board: dict) -> list[dict]:
    """The human-facing half: one line per event, or one collapsed line.

    COLLAPSE IS THE STALENESS POLICY. A dashboard that was down for eight hours
    resumes holding hundreds of events, and firing one notice each is the
    behaviour that makes people turn notifications off — so past
    :data:`COLLAPSE_AT`, or whenever the batch was truncated or the log was
    pruned under the cursor, it becomes a single "11 items finished while you
    were away".
    """
    if board.get("quiet"):
        # Events accumulate and collapse afterwards: the notice cursor is not
        # advanced during a quiet window (see tick), which is what makes the
        # window a delay rather than a deletion.
        return []
    if not settings.get("in_app") and not settings.get("webhook"):
        return []           # no channel is configured; nothing to say it to
    pending = board.get("pending") or {}
    found = list(pending.get("samples") or [])
    count = int(pending.get("count") or 0)
    if not count:
        return []
    truncated = bool(pending.get("more")) or bool(pending.get("gap"))
    if count <= COLLAPSE_AT and not truncated:
        return [_action(
            "notify", 0, ev, f"activity:e{int(ev.get('id') or 0)}",
            "an event worth telling you about", cause="event", count=1,
            refs=[str(ev.get("ref") or "")], kinds=[str(ev.get("kind") or "")],
            summary=_line(ev), detail="") for ev in found]
    last = found[-1] if found else {"id": pending.get("seq") or 0}
    parts = ", ".join(f"{n}x {kind}" for kind, n in
                      sorted((pending.get("by_kind") or {}).items()))
    at_least = "at least " if truncated else ""
    detail = "\n".join(_line(ev) for ev in found[:COLLAPSE_AT])
    if pending.get("gap"):
        detail += ("\n(older events were pruned before they could be delivered — "
                   "the count is what survived)")
    return [_action(
        "notify", 0, last, f"activity:e{int(last.get('id') or 0)}",
        "collapsed notice", cause="collapsed", count=count,
        refs=[str(ev.get("ref") or "") for ev in found],
        kinds=sorted((pending.get("by_kind") or {}).keys()),
        summary=f"{at_least}{count} events while you were away — {parts}",
        detail=detail)]


def _line(ev: dict) -> str:
    """One sentence for one event — what a bell or a webhook shows.

    Written from the payload rather than by re-reading the board: a notice that
    queries the item races the next transition and can describe a state that no
    longer exists by the time it is read.
    """
    payload = ev.get("payload") or {}
    kind = str(ev.get("kind") or "")
    seat = str(payload.get("seat") or "")
    title = str(payload.get("title") or "")[:70]
    item = payload.get("item") or ev.get("ref") or "?"
    if kind == "item.done":
        return f"#{item} [{seat}] finished: {title}"
    if kind == "item.review":
        return f"#{item} [{seat}] is waiting for your approval: {title}"
    if kind == "item.failed":
        return f"#{item} [{seat}] FAILED: {title}"
    if kind == "item.approved":
        return f"#{item} [{seat}] approved by {payload.get('by') or 'you'}: {title}"
    if kind == "item.rejected":
        return f"#{item} [{seat}] sent back: {payload.get('reason') or title}"
    if kind == "item.aging":
        return (f"#{item} [{seat}] has waited {payload.get('idle_min') or '?'} min "
                f"for approval: {title}")
    if kind == "chain.advanced":
        # `item` is deliberately not used here: this payload has no "item" key and
        # the ref is the CHAIN id, so the shared fallback resolves it to "c3" and
        # concatenating it with `from` printed "#c312 landed" for item 12.
        return (f"chain {payload.get('chain_id') or ev.get('ref')}: "
                f"#{payload.get('from') or '?'} landed, #{payload.get('to')} "
                f"[{payload.get('to_seat')}] is next")
    if kind == "chain.stalled":
        # Two producers, two payload shapes. heartbeat sends a chain and its head;
        # steerbox's stale-question reminder sends question_seq and no head, and
        # reading that with the chain wording produces "chain None has not moved —
        # #None [None]", i.e. a reminder that names nothing.
        if payload.get("question_seq"):
            return ("still waiting on your answer: "
                    f"{str(payload.get('question') or '')[:120]}")
        head = payload.get("head") or {}
        return (f"chain {payload.get('chain_id') or ev.get('ref')} has not moved "
                f"for {payload.get('idle_min') or '?'} min — #{head.get('item')} "
                f"[{head.get('seat')}] {payload.get('reason') or ''}")
    if kind == "chain.filed":
        return (f"chain {payload.get('chain_id') or ev.get('ref')} filed with "
                f"{payload.get('count')} linked items")
    if kind == "director.question":
        return f"the director is asking you: {str(payload.get('question') or '')[:120]}"
    if kind == "budget.refused":
        return f"a dispatch was refused for spend: {str(payload.get('reason') or '')[:120]}"
    if kind == "gate.mode":
        return f"the approval gate is now {payload.get('mode') or '?'}"
    return f"{kind} {ev.get('ref') or ''}".strip()


# ---------------------------------------------------------------------------
# Applying — the I/O half, one guard query per action
# ---------------------------------------------------------------------------
def apply_action(root: str | os.PathLike[str], action: dict) -> dict:
    """Perform one action from :func:`decide`, re-checking its guard first.

    Returns ``{"ok": bool, "kind": str, "event": int, "why": str, ...}``. The
    guard is re-run HERE rather than trusted from the snapshot because the two
    are separated by the rest of the batch: a QA gate filed for item 12 earlier
    in the same pass, or by a restart replaying the same events, must not be
    filed twice. Never raises — one impossible action must not cost the tick.
    """
    kind = str(action.get("kind") or "")
    out = {"ok": False, "kind": kind, "event": int(action.get("event") or 0),
           "guard": str(action.get("guard") or ""), "why": ""}
    if kind == "skip":
        out["why"] = str(action.get("why") or "")
        return out
    try:
        if kind == "reopen":
            return {**out, **_do_reopen(root, action)}
        if kind == "qa_spawn":
            return {**out, **_do_qa_spawn(root, action)}
        if kind == "qa_escalate":
            return {**out, **_do_qa_escalate(root, action)}
        if kind == "emit":
            return {**out, **_do_emit(root, action)}
        if kind == "debrief":
            return {**out, **_do_debrief(root, action)}
        if kind == "notify":
            return {**out, **_do_notify(root, action)}
    except Exception as exc:
        # Fail-safe, the same rule the reactors around this one follow: a router
        # that raises takes the dashboard's tick with it, and the next agent to
        # finish is then routed by nothing at all.
        return {**out, "why": f"{type(exc).__name__}: {exc}"}
    return {**out, "why": f"unknown action {kind!r}"}


def _do_reopen(root, action: dict) -> dict:
    item_id = int(action.get("item") or 0)
    item = _item(root, item_id)
    if item is None or str(item.get("status")) != "failed":
        return {"why": "the item is no longer failed — already reopened"}
    if int(item.get("attempts") or 0) != int(action.get("attempts") or 0):
        return {"why": "the round counter moved — somebody already retried it"}
    _queue.reopen(root, item_id, str(action.get("reason") or "reopened"))
    activity.log(root, LEDGER_KIND,
                 f"auto-reopened #{item_id} after a failure — attempt "
                 f"{int(item.get('attempts') or 0) + 2}",
                 seat=item.get("seat") or "", ref=str(item_id))
    return {"ok": True, "item": item_id, "why": str(action.get("why") or "")}


def _do_qa_spawn(root, action: dict) -> dict:
    from bgate_ui import dispatch as _dispatch

    item_id = int(action.get("item") or 0)
    item = _item(root, item_id)
    if item is None:
        return {"why": "the item is gone"}
    if _qa_gate._open_gate_exists(root, str(item_id)):
        return {"why": "a QA round is already open for this item"}
    gate = _qa_gate.open_round(root, item)
    if not gate.get("ok"):
        return {"why": str(gate.get("why") or "the gate could not be filed")}
    sent = _dispatch.dispatch(root, int(gate["gate"]), actor=LEDGER_KIND)
    return {"ok": True, "gate": int(gate["gate"]),
            "dispatched": bool(sent.get("ok")),
            "why": str(sent.get("error") or action.get("why") or "")}


def _do_qa_escalate(root, action: dict) -> dict:
    item_id = int(action.get("item") or 0)
    item = _item(root, item_id)
    if item is None:
        return {"why": "the item is gone"}
    if _qa_gate.escalated(root, str(item_id)):
        return {"why": "already escalated once, which is the whole cap"}
    _qa_gate._escalate(root, item, str(item_id), int(action.get("rounds") or 0))
    return {"ok": True, "item": item_id, "why": str(action.get("why") or "")}


def _do_emit(root, action: dict) -> dict:
    payload = action.get("payload") or {}
    if str(action.get("emit_kind")) == "chain.advanced":
        if _advanced_seen(root, str(action.get("ref") or ""),
                          int(payload.get("from") or 0)):
            return {"why": "this handoff has already been announced"}
    event_id = _events.emit(root, str(action.get("emit_kind") or ""),
                            ref=str(action.get("ref") or ""), payload=payload)
    return {"ok": bool(event_id), "emitted": event_id,
            "why": str(action.get("why") or "")}


def _do_debrief(root, action: dict) -> dict:
    """File and dispatch the director debrief. See :func:`debrief_brief`."""
    from bgate_ui import dispatch as _dispatch

    guard_ref = str(action.get("guard_ref") or "")
    if _debrief_exists(root, guard_ref):
        return {"why": "a debrief for this chain already exists"}
    item_id = int(action.get("item") or 0)
    item = _item(root, item_id)
    if item is None:
        return {"why": "the item is gone"}
    # The rate cap, re-counted at the moment of spending rather than trusted
    # from the snapshot: a batch of twenty completions is decided in one pass and
    # the count that mattered when the first one was decided is stale by the
    # twentieth.
    cap = int(action.get("cap") or 0)
    already = _debriefs_since(root, minutes=60)
    if already >= 10 ** 5:
        return {"why": "the board could not be read"}
    if cap and already >= cap:
        return {"why": f"the debrief rate cap ({cap}/h) is reached"}
    # The ordinary spend gate. A debrief IS a dispatch, so it queues behind the
    # same ceiling as any other agent rather than getting a private allowance.
    try:
        verdict = _spend.check(root, projected_usd=_spend.item_ceiling(root, item))
    except Exception:
        verdict = {"allowed": True}
    if not verdict.get("allowed"):
        _events.emit(root, "budget.refused", ref=str(item_id),
                     payload={"what": "director debrief", "item": item_id,
                              "reason": str(verdict.get("reason") or "")})
        return {"why": f"budget: {verdict.get('reason')}"}
    row = _queue.add(
        root, "director",
        f"Debrief #{item_id}: {str(item.get('title') or '')[:60]} — decide the "
        "follow-up",
        brief=debrief_brief(root, item), priority=DEBRIEF_PRIORITY,
        source=DEBRIEF_SOURCE, source_ref=guard_ref)
    # allow_dirty=True DELIBERATELY. dispatch refuses on a dirty tree with no
    # exemption by source, and an agent that just finished writing files leaves
    # the tree dirty BY DEFINITION — that is the exact state this reacts to.
    # Without it every debrief would be filed, refused, cooled down by autodeploy
    # and look like it silently never happened. The brief says the tree is dirty,
    # because a director that assumes a clean one draws the wrong conclusion from
    # git status. The debrief reads and decides; it does not edit.
    sent = _dispatch.dispatch(root, int(row["id"]), allow_dirty=True,
                              actor=LEDGER_KIND)
    if not sent.get("ok"):
        # A queued 'completion' row left on the board is the trap this feature is
        # supposed to avoid AND a hazard to autodeploy, which would retry it
        # without allow_dirty and take a dirty-tree floor cooldown for the whole
        # board. So the row is closed out rather than left looking delegated.
        try:
            _queue.set_status(root, int(row["id"]), "cancelled",
                              result="the follow-up router could not dispatch "
                                     "this debrief: "
                                     + str(sent.get("error") or "refused"))
        except Exception:
            pass
        activity.log(root, LEDGER_KIND,
                     f"debrief for #{item_id} was refused and withdrawn — "
                     f"{str(sent.get('error') or 'refused')[:160]}",
                     seat="director", ref=str(item_id))
        return {"why": f"dispatch refused: {sent.get('error')}",
                "withdrawn": int(row["id"])}
    activity.log(root, LEDGER_KIND,
                 f"debriefed the director on #{item_id} "
                 f"({guard_ref}) — {str(item.get('title') or '')[:60]}",
                 seat="director", ref=str(item_id))
    return {"ok": True, "debrief": int(row["id"]), "item": item_id,
            "why": str(action.get("why") or "")}


def _do_notify(root, action: dict) -> dict:
    """Write one notice to the ledger and, if configured, POST it.

    The ledger line carries an ``[e<id>]`` marker, which is both the record and
    the dedupe key: a restart that replays the same events finds its own marker
    and stays quiet.
    """
    event_id = int(action.get("event") or 0)
    if event_id and _noticed(root, event_id):
        return {"why": "already noticed"}
    summary = str(action.get("summary") or action.get("why") or "")
    activity.log(root, LEDGER_KIND, f"[e{event_id}] {summary}"[:400],
                 ref=(action.get("refs") or [""])[0])
    fired = fire_webhook(root, {
        "event": event_id, "cause": str(action.get("cause") or "event"),
        "kinds": list(action.get("kinds") or []),
        "count": int(action.get("count") or 1),
        "refs": [str(r) for r in (action.get("refs") or [])],
        "summary": summary, "detail": str(action.get("detail") or ""),
    })
    return {"ok": True, "why": summary, "webhook": fired}


# ---------------------------------------------------------------------------
# The debrief brief
# ---------------------------------------------------------------------------
def debrief_brief(root: str | os.PathLike[str], item: dict) -> str:
    """The brief a director debrief carries. Everything it needs to decide once.

    Six things, and each is here because deciding without it produces a wrong
    decision rather than a slower one: what finished and its result note; the
    HARNESS-OBSERVED file list (``writelog``, not the agent's self-report, which
    has already been wrong in the one seat whose job is disbelieving claims); the
    chain and what is behind it; the active gate mode; the budget left; and the
    fact that the tree is DIRTY, because a director that assumes a clean tree
    reads ``git status`` and concludes somebody else has work in progress.

    Then exactly three legal moves, and the standing rule that it may not do seat
    work itself — restated because a debrief holding a fresh diff is the most
    tempting place in the system to break it.
    """
    item_id = int(item.get("id") or 0)
    seat = str(item.get("seat") or "")
    lines: list[str] = []
    lines.append(
        f"DEBRIEF — work item #{item_id} [{seat}] finished and nothing is "
        "queued behind it.\n")
    lines.append(
        "You hold the director seat. This is a REPORT-AND-DECIDE item: read what "
        "landed, then take exactly ONE of the three moves at the bottom and "
        "close this item saying which you took and why. You may NOT do the seat "
        "work yourself — no editing files, no generating art, no writing scenes. "
        "Work done here is unlaned, unlogged, unbudgeted and ungated, and this "
        "is the most tempting place in the system to forget that, because the "
        "diff is right in front of you.\n")

    lines.append("WHAT FINISHED")
    lines.append(f"  #{item_id} [{seat}] \"{str(item.get('title') or '')[:120]}\" "
                 f"— status {item.get('status')}, "
                 f"attempt {int(item.get('attempts') or 0) + 1}, "
                 f"${float(item.get('total_cost_usd') or 0):.2f} spent.")
    result = str(item.get("result") or "").strip()
    lines.append("  Its result note, in the agent's own words:")
    lines.append("  " + (result[:1400].replace("\n", "\n  ") if result
                         else "(none — it closed the item without saying anything)"))
    lines.append("")

    lines.append("WHAT THE HARNESS OBSERVED IT WRITE (not the agent's own list)")
    observed = ""
    try:
        observed = writelog.summary(root, f"item-{item_id}")
    except Exception:
        observed = ""
    if observed:
        lines.append("  " + observed.replace("\n", "\n  "))
    else:
        lines.append("  Nothing was recorded. Either it wrote no files, or the "
                     "write hook is not installed in this project — "
                     "`bgate hook-status` answers which, and until it does the "
                     "absence is not evidence.")
    lines.append("")

    lines.append("THE TREE IS DIRTY, AND THAT IS EXPECTED")
    lines.append("  These changes are UNCOMMITTED — the agent that just finished "
                 "left them in the working tree, which is why this item was "
                 "dispatched with allow_dirty. Do not read `git status` as "
                 "somebody else's work in progress, and do not conclude the "
                 "change is missing because it is not in the log.")
    lines.append("")

    lines.append(_chain_block(root, item))
    lines.append(_gate_block(root))
    lines.append(_budget_block(root))

    lines.append("YOUR THREE LEGAL MOVES — pick exactly one")
    lines.append(
        "  1. DISPATCH THE FOLLOW-UP. queue_add(seat, title, brief) for the next "
        "piece of work, or queue_add_chain([...]) if the pieces have an ORDER "
        "(they usually do — anything that needs the file, scene or schema "
        "another seat is about to make). Name the acceptance test in the brief.")
    lines.append(
        "  2. ASK THE HUMAN ONE QUESTION. ask_human(question, refs) when the "
        "next step is a judgement you do not own — a pillar, a priority call, or "
        "spending more money. If that tool is not in your list, put the question "
        "in your result note instead and stop there; do not guess.")
    lines.append(
        "  3. CLOSE IT OUT. Say nothing further is needed and why what landed is "
        "enough. This is a real answer and the cheapest one — take it when it is "
        "true, and do not invent follow-up work to look busy.")
    lines.append("")
    lines.append("Do not re-file what just landed. Then queue_complete THIS "
                 "item (done) with the move you took.")
    return "\n".join(lines)


def _chain_block(root, item: dict) -> str:
    chain_id = str(item.get("chain_id") or "").strip()
    if not chain_id:
        return ("THE CHAIN\n  Not part of a chain — this item stood alone.\n")
    try:
        links = _queue.chain(root, chain_id)
    except Exception:
        links = []
    lines = [f"THE CHAIN\n  chain {chain_id}, "
             f"link {int(item.get('chain_pos') or 0)} of {len(links)}."]
    for link in links:
        mark = "  <- this one" if int(link["id"]) == int(item["id"]) else ""
        lines.append(f"    #{link['id']} [{link['seat']}] "
                     f"\"{str(link['title'])[:60]}\" {link['status']}{mark}")
    open_links = [row for row in links
                  if str(row["status"]) not in ("done", "cancelled")]
    if open_links:
        lines.append("  Still open in this chain: "
                     + ", ".join(f"#{row['id']} ({row['status']})"
                                 for row in open_links))
    else:
        lines.append("  Every link has landed; nothing is queued or blocked "
                     "behind this one.")
    return "\n".join(lines) + "\n"


def _gate_block(root) -> str:
    try:
        return "THE GATE\n  " + _gates.describe(root) + "\n"
    except Exception:
        return ""


def _budget_block(root) -> str:
    """What is left to spend, so a follow-up is priced before it is filed."""
    try:
        totals = _spend.totals(root)
        budget = totals.get("budget") or {}
        day_cap = float(budget.get("per_day_usd") or 0)
        project_cap = float(budget.get("per_project_usd") or 0)
        today = float(totals.get("today_usd") or 0)
        life = float(totals.get("project_usd") or 0)
        left = f"${max(0.0, day_cap - today):.2f} left today" if day_cap else \
            "no daily ceiling set"
        enforced = "enforced" if budget.get("enforced") else \
            "NOT enforced — the numbers are a report, not a limit"
        return (f"BUDGET\n  ${today:.2f} spent today"
                + (f" of ${day_cap:.2f}" if day_cap else "")
                + f" ({left}); ${life:.2f} on this project"
                + (f" of ${project_cap:.2f}" if project_cap else "")
                + f". Ceilings are {enforced}.\n")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# The webhook — the only path off this machine, and it ships off
# ---------------------------------------------------------------------------
def webhook_target(url: str) -> tuple[str, str]:
    """Validate a webhook URL. Returns ``(url, "")`` or ``("", reason)``.

    A loopback service that POSTs a user-supplied URL is an SSRF: pointed at
    ``http://169.254.169.254`` or ``https://127.0.0.1:9200`` it becomes a way to
    reach things only this machine can reach, and it carries what the agents are
    doing as the payload. So: https only (a plaintext webhook puts the project's
    work on the wire), and every address the host resolves to must be a public
    one.

    Resolution happens here and the connection is made afterwards, so a hostile
    DNS server could in principle answer differently the second time. That is
    accepted rather than hidden: the alternative is pinning the socket and
    forging a Host header, and this is an opt-in, off-by-default switch on a
    single-operator loopback tool.
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    text = str(url or "").strip()
    if not text:
        return "", "no webhook configured"
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        return "", "webhook must be https"
    host = parts.hostname or ""
    if not host:
        return "", "webhook has no host"
    try:
        infos = socket.getaddrinfo(host, parts.port or 443,
                                   proto=socket.IPPROTO_TCP)
    except Exception as exc:
        return "", f"webhook host does not resolve ({type(exc).__name__})"
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return "", f"webhook host resolved to something unreadable ({raw})"
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return "", (f"webhook host resolves to {raw}, which is not a public "
                        "address — refusing to POST to this machine's own network")
    return text, ""


def fire_webhook(root: str | os.PathLike[str], body: dict,
                 url: Optional[str] = None) -> dict:
    """POST one notice. Off unless ``notify.webhook`` is set. Never raises.

    Returns ``{"sent": bool, "why": str, "status": int}``. ONE attempt with a
    short timeout: a retry loop against a dead endpoint is a hot loop inside the
    dashboard's tick, and a webhook that retries can be turned into an
    amplifier. Failures go to the activity ledger, which is where somebody
    asking "why did my Slack bridge stay quiet" is already looking.
    """
    out = {"sent": False, "why": "", "status": 0}
    try:
        if url is None:
            url = str(_settings.get(root, "notify.webhook") or "")
    except Exception:
        url = ""
    if not str(url or "").strip():
        return {**out, "why": "off"}
    target, refused = webhook_target(url)
    if refused:
        activity.log(root, LEDGER_KIND, f"webhook not sent — {refused}")
        return {**out, "why": refused}
    import urllib.error
    import urllib.request

    payload = {"project": os.path.basename(str(root).rstrip("\\/")),
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               **(body or {})}
    try:
        raw = json.dumps(payload, default=str)[:WEBHOOK_MAX_BODY].encode("utf-8")
        request = urllib.request.Request(
            target, data=raw, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "builders-gate/followup"})
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as resp:
            return {"sent": True, "why": "", "status": int(resp.status or 0)}
    except urllib.error.HTTPError as exc:
        why = f"webhook returned HTTP {exc.code}"
        status = int(exc.code or 0)
    except Exception as exc:
        why = f"webhook failed: {type(exc).__name__}"
        status = 0
    activity.log(root, LEDGER_KIND, why)
    return {"sent": False, "why": why, "status": status}


# ---------------------------------------------------------------------------
# The tick, and the thread around it
# ---------------------------------------------------------------------------
def tick(root: str | os.PathLike[str]) -> dict:
    """One pass: read the bus, decide, apply, advance the cursors.

    Returns ``{"seq", "notify_seq", "actions", "applied", "more", "pending",
    "heartbeat"}``. Safe to call directly — that is how it is tested and how a
    route can make the routing feel immediate instead of up to POLL_S late.

    Cursor discipline is the whole correctness story: the routing cursor advances
    after the batch is applied (so a crash mid-batch replays it, which every
    guard is built for), and the notice cursor advances only when notices were
    deliverable (so a quiet-hours window delays them instead of eating them).
    """
    root = str(root)
    settings = load_settings(root)
    main_seq = _bootstrap(root)
    notify_seq = _events.cursor_get(root, NOTIFY_CONSUMER)

    batch = _events.since(root, main_seq, limit=BATCH)
    kinds = list(settings.get("notify_kinds") or ())
    pending = summarize_pending(
        _events.since(root, notify_seq, kinds=kinds, limit=BATCH) if kinds
        else {})

    board = snapshot(root, batch.get("events") or [], main_seq=main_seq,
                     notify_seq=notify_seq, pending=pending, settings=settings)
    actions = decide(batch.get("events") or [], settings, board)
    applied = [apply_action(root, action) for action in actions
               if action.get("kind") != "skip"]

    _events.cursor_set(root, CONSUMER, int(batch.get("seq") or main_seq))
    if not board.get("quiet"):
        _events.cursor_set(root, NOTIFY_CONSUMER,
                           int(pending.get("seq") or notify_seq))

    beat = {}
    count = _bump(root)
    try:
        from bgate_ui import heartbeat as _heartbeat

        beat = _heartbeat.tick(root)
    except Exception:
        beat = {}
    if count % PRUNE_EVERY == 0:
        # Cursors are deliberately NOT consulted (see events.prune): a subscriber
        # that has been down for a fortnight is not caught up by holding rows for
        # it, and since() reports the gap honestly instead.
        _events.prune(root, keep_days=KEEP_DAYS)
    if count % SWEEP_EVERY == 0:
        # The backstop for an event that was never written: events.emit is
        # best-effort, so a completion that hit a locked database is invisible to
        # the cursor forever. The sweep's guards are the same ones the event path
        # uses, so finding nothing costs one query.
        try:
            _qa_gate.sweep(root)
        except Exception:
            pass
        # THE SLICE CHECK — the only thing in the harness that reviews the GAME.
        # On the sweep rather than a branch because it is not about any one
        # event: it fires when the LAST slice row lands, whichever item that
        # was, and its own due-check is idempotent (one open at a time, and
        # never twice for an unchanged slice). A project with no game plan
        # answers "not due" off one query.
        try:
            from bgate_core import gameplan as _gameplan

            if _gameplan.slice_check_due(root).get("due"):
                _gameplan.open_slice_check(root)
        except Exception:
            pass
    return {"seq": int(batch.get("seq") or main_seq),
            "notify_seq": int(pending.get("seq") or notify_seq),
            "actions": actions, "applied": applied,
            "more": bool(batch.get("more")), "gap": bool(batch.get("gap")),
            "pending": pending.get("count") or 0, "heartbeat": beat}


def _bump(root: str) -> int:
    with _lock:
        _ticks[root] = _ticks.get(root, 0) + 1
        return _ticks[root]


def _bootstrap(root: str) -> int:
    """The routing cursor, starting at the HEAD of the log on a first run.

    A project with a month of history must not have that month routed the first
    time the router starts: that is eight hours of pings and a QA agent per
    historical completion. 0 means "never run" (see ``events.cursor_get``), and
    for a subscriber that spends money the right reading of that is "start now",
    not "replay everything".
    """
    seq = _events.cursor_get(root, CONSUMER)
    if seq:
        return seq
    head = _events.head(root)
    if head:
        _events.cursor_set(root, CONSUMER, head)
        _events.cursor_set(root, NOTIFY_CONSUMER, head)
        activity.log(root, LEDGER_KIND,
                     f"follow-up router started at event {head} — the backlog "
                     "before it is deliberately not replayed")
    return head


# Catch-up passes one wake-up may take. A truncated batch means the router is
# behind, and waiting POLL_S per BATCH events makes an overnight backlog slower
# to drain than it was to create — but the loop is BOUNDED, because cursor_set is
# best-effort: a cursor that will not persist replays the same batch forever, and
# an unbounded `while more` there is a hot loop inside the dashboard.
CATCHUP_MAX = 20


def _catchup(root: str) -> None:
    """One wake-up: a tick, then up to CATCHUP_MAX more while the batch is full."""
    result = tick(root)
    for _ in range(CATCHUP_MAX):
        if not result.get("more"):
            break
        result = tick(root)


_pump = Pump("bgate-followup", lambda: POLL_S, _catchup,
             env_var="BGATE_FOLLOWUP")


def start(root: str | os.PathLike[str]) -> bool:
    """Idempotently start the router for this project in this process.

    Returns False when ``BGATE_FOLLOWUP=0`` disables it. The thread runs whatever
    the individual switches say — every branch reads its setting on each tick, so
    turning the debrief on in the browser must not need a restart. Per project
    rather than a single flag, for the reason autodeploy is: the active project
    can change under a long-lived server, and a latched flag keeps routing the
    project the user already left.
    """
    return _pump.start(root)


def reset(root: Optional[str | os.PathLike[str]] = None) -> None:
    """Forget that the router started here. Tests use this; nothing else should."""
    with _lock:
        if root is None:
            _ticks.clear()
        else:
            _ticks.pop(str(root), None)
    _pump.reset(root)
