"""The work queue — where intent becomes dispatchable seat work.

Modeled on Orbit's ticket->task pattern: items carry a seat, a title, a brief,
and a lifecycle (queued -> dispatched -> done/failed). Three inflows:

  * the human, via the dashboard's add form
  * promoted playtest items (sync_promoted — feedback the user blessed becomes
    queued work automatically, keeping its telemetry-joined provenance)
  * optionally, Orbit tickets tagged for the game (import_orbit)

Seats interact through MCP tools (queue_next / queue_complete); the dashboard
dispatches real Claude sessions against items.
"""
from __future__ import annotations

import os
from typing import Optional

from . import activity, db, iterations, scope as _scope, seats as _seats
from .util import rows

# 'cancelled' is a human calling work off — distinct from 'failed', which is an
# agent (or the watchdog) reporting it could not finish. Only the second is
# worth reopening; the audit needs to tell them apart.
#
# 'review' is finished-but-not-counted: the agent is gone, the work is on disk,
# and under the builder's gate (bgate_core.gates) a human has not yet said yes.
# It is deliberately NOT 'done' — a chain must not advance on unapproved work —
# and deliberately not 'dispatched', which would claim an agent is still running.
STATUSES = ("queued", "dispatched", "review", "done", "failed", "cancelled")

# Statuses a dependent item is allowed to start on top of. 'review' is not one
# of them: the whole point of the hold is that the next link waits.
SATISFIED = ("done",)

# HOW MUCH OF A RESULT SURVIVES THE WRITE.
#
# This was 2000 characters and it was SILENTLY EATING THE DELIVERABLE. A chat
# turn's result IS the director's answer to you — the whole point of the turn —
# and a long one arrived in the console cut off mid-word with nothing saying so.
# Measured on a real board: two answers stored at exactly 2000 chars, one ending
# "...as you asked — just sequen", the other "...rather than how it is coloured
# — s". A reader cannot tell that from an agent that stopped talking.
#
# 20k is not a guess about answers, it is a bound on runaway: it is far past any
# real result note (the longest observed was 1634) while still refusing to let a
# loop dump a log into a column every list query reads. Downstream consumers cap
# for their own contexts already — event payloads at 400, queue_list previews at
# 240 — so nothing else has to change for this to be safe.
MAX_RESULT = 20000
# What is appended when the ceiling is genuinely hit. The truncation being
# VISIBLE is the actual fix here; the higher ceiling only makes it rare.
CLIPPED = "\n\n…[result truncated at %d characters — the run's full output is in "
CLIPPED += "its log]"


def clip_result(text: str) -> str:
    """Bound a result note, and SAY SO when it is bounded.

    Cuts at a line break where one is near the ceiling, so the marker does not
    land mid-sentence on top of the mid-word cut it exists to explain.
    """
    text = str(text or "")
    if len(text) <= MAX_RESULT:
        return text
    head = text[:MAX_RESULT]
    cut = head.rfind("\n", MAX_RESULT - 500)
    if cut > 0:
        head = head[:cut]
    return head + (CLIPPED % MAX_RESULT)


def add(root: str | os.PathLike[str], seat: str, title: str, brief: str = "",
        priority: int = 0, source: str = "manual", source_ref: str = "",
        scope_tier_id: Optional[int] = None, chain_id: str = "",
        chain_pos: int = 0, depends_on: Optional[int] = None) -> dict:
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
    if not title.strip():
        raise ValueError("a work item needs a title")
    # The cut line only means something if work cannot be filed under it.
    # OutOfScope subclasses ValueError, so every caller that already maps
    # ValueError -> 400 reports this correctly without knowing about scope.
    _scope.enforce(root, scope_tier_id)
    if depends_on is not None:
        get(root, int(depends_on))          # LookupError if the link is a fiction
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO work_item (seat, title, brief, priority, source, "
            "source_ref, scope_tier_id, chain_id, chain_pos, depends_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seat, title.strip(), brief, priority, source, source_ref,
             scope_tier_id, chain_id.strip(), int(chain_pos),
             int(depends_on) if depends_on is not None else None),
        )
        item_id = int(cur.lastrowid)
    waits = f" (waits for #{int(depends_on)})" if depends_on is not None else ""
    activity.log(root, "queue",
                 f"queued for {seat}: {title.strip()[:80]}{waits}",
                 ref=str(item_id))
    return get(root, item_id)


def add_chain(root: str | os.PathLike[str], links: list[dict],
              chain_id: str = "", source: str = "manual",
              source_ref: str = "") -> list[dict]:
    """File dependent work as ONE ordered group, each link waiting on the last.

    THE GAP THIS CLOSES. Splitting an ask across seats produced N independent
    rows whose only relationship was priority, and priority is an ordering, not
    a dependency: auto-deploy dispatches everything it can reach, so the item
    that needed a scene and the item that CREATES that scene started in the same
    tick. The second agent then wrote against a file that did not exist yet,
    reported done, and the failure surfaced two items later as a mystery.

    Each link is a dict of the same fields ``add`` takes (seat + title are
    required). The chain is strictly linear — link N waits for link N-1 — because
    a DAG needs a graph editor to be legible and every real case so far has been
    a line. Priority still decides which READY item goes first; the chain only
    decides what is ready.

    Returns the created items in order. Raises before writing anything if a link
    is malformed, so a bad chain does not half-land.
    """
    if not links:
        raise ValueError("a chain needs at least one link")
    for i, link in enumerate(links):
        if not str(link.get("seat") or "").strip():
            raise ValueError(f"chain link {i + 1} has no seat")
        if str(link.get("seat")) not in _seats.DEFAULT_SEATS:
            raise ValueError(f"chain link {i + 1}: unknown seat "
                             f"{link.get('seat')!r}; seats are "
                             f"{tuple(_seats.DEFAULT_SEATS)}")
        if not str(link.get("title") or "").strip():
            raise ValueError(f"chain link {i + 1} has no title")
    if len(links) == 1:
        raise ValueError("a one-link chain is just an item — use queue_add")

    chain_id = (chain_id or "").strip() or _new_chain_id(root)
    made: list[dict] = []
    previous: Optional[int] = None
    for pos, link in enumerate(links, start=1):
        item = add(root, str(link["seat"]), str(link["title"]),
                   brief=str(link.get("brief") or ""),
                   priority=int(link.get("priority") or 0),
                   source=str(link.get("source") or source),
                   source_ref=str(link.get("source_ref") or source_ref),
                   scope_tier_id=link.get("scope_tier_id"),
                   chain_id=chain_id, chain_pos=pos, depends_on=previous)
        previous = int(item["id"])
        made.append(item)
    activity.log(root, "queue",
                 f"chain {chain_id}: {len(made)} linked items — "
                 + " -> ".join(f"#{m['id']}[{m['seat']}]" for m in made),
                 ref=chain_id)
    # ref is the chain id, not an item id: everything downstream that reasons
    # about a chain (one debrief per chain, the stall reminder, "what is blocked
    # behind this") keys on the chain, and a subscriber that had to infer the
    # group from N separate item events would guess at the boundary.
    _emit(root, "chain.filed", ref=chain_id,
          payload={"chain_id": chain_id, "count": len(made),
                   "links": [{"item": int(m["id"]), "seat": m["seat"],
                              "title": str(m["title"])[:200],
                              "chain_pos": int(m.get("chain_pos") or 0)}
                             for m in made]})
    return made


def _new_chain_id(root: str | os.PathLike[str]) -> str:
    """A short, human-sayable chain id. Sequential per project rather than a
    uuid: these get read aloud and typed into briefs ("chain c3"), and a uuid is
    neither."""
    row = db.connect(root).execute(
        "SELECT chain_id FROM work_item WHERE chain_id LIKE 'c%' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    last = 0
    if row and str(row["chain_id"] or "")[1:].isdigit():
        last = int(str(row["chain_id"])[1:])
    return f"c{last + 1}"


def get(root: str | os.PathLike[str], item_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM work_item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise LookupError(f"no work item {item_id}")
    return dict(row)


def update(root: str | os.PathLike[str], item_id: int, *,
           title: Optional[str] = None, brief: Optional[str] = None,
           seat: Optional[str] = None, priority: Optional[int] = None,
           max_cost_usd: Optional[float] = None,
           max_runtime_s: Optional[int] = None) -> dict:
    """Edit an existing item in place, without changing its status/lineage.

    This is how a reviewer enriches a ticket: e.g. the video-watching director
    rewriting a transcript-era brief to add the frames/timestamps/telemetry it
    saw. Only the passed fields change; the rest (status, source, source_ref)
    are untouched."""
    get(root, item_id)  # 404 if missing
    sets, params = [], []
    if title is not None:
        if not title.strip():
            raise ValueError("title cannot be blank")
        sets.append("title = ?"); params.append(title.strip())
    if brief is not None:
        sets.append("brief = ?"); params.append(brief)
    if seat is not None:
        if seat not in _seats.DEFAULT_SEATS:
            raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
        sets.append("seat = ?"); params.append(seat)
    if priority is not None:
        sets.append("priority = ?"); params.append(int(priority))
    # Per-item ceilings override the project budget for one expensive item —
    # editable here so raising them is a deliberate edit, not a dispatch flag
    # nobody sees again.
    if max_cost_usd is not None:
        if float(max_cost_usd) <= 0:
            raise ValueError("max_cost_usd must be positive")
        sets.append("max_cost_usd = ?"); params.append(float(max_cost_usd))
    if max_runtime_s is not None:
        if int(max_runtime_s) <= 0:
            raise ValueError("max_runtime_s must be positive")
        sets.append("max_runtime_s = ?"); params.append(int(max_runtime_s))
    if not sets:
        return get(root, item_id)
    params.append(item_id)
    with db.tx(root) as conn:
        conn.execute(
            f"UPDATE work_item SET {', '.join(sets)}, updated_at = datetime('now') "
            "WHERE id = ?", params)
    item = get(root, item_id)
    activity.log(root, "queue", f"item {item_id} edited: {item['title'][:60]}",
                 seat=item["seat"], ref=str(item_id))
    return item


def list_items(root: str | os.PathLike[str], status: Optional[str] = None,
               seat: Optional[str] = None) -> list[dict]:
    conn = db.connect(root)
    sql, params = "SELECT * FROM work_item WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if seat:
        sql += " AND seat = ?"
        params.append(seat)
    # Live work first, then finished, then the abandoned — a cancelled item is
    # not a result anyone is waiting on, so it sinks below done/failed. 'review'
    # sits with the live work because it IS live: it is holding up its chain and
    # the only thing that moves it is somebody looking at it.
    sql += " ORDER BY CASE status WHEN 'queued' THEN 0 WHEN 'dispatched' THEN 1 "
    sql += "WHEN 'review' THEN 2 WHEN 'cancelled' THEN 4 ELSE 3 END, priority DESC, id"
    return rows(conn.execute(sql, params))


def _notify(root: str | os.PathLike[str], item: dict) -> None:
    """Append a status-transition event to .bgate/notify.jsonl (best-effort).

    The durable completion signal: dispatched agents flip their item via
    queue_complete, the watcher/reap paths flip it on death — ALL of it lands
    here, so an orchestrator (or the UI) can tail/long-poll one file instead of
    sleep-polling the queue. Never raises — losing a ping must not break the
    status change itself.

    ``kind`` says WHICH CLASS OF EVENT a line is, and it is here because this
    file is no longer only work items: ``artifacts._notify_line`` puts pending
    human decisions on the same stream (they had no signal at all, so a batch of
    candidates waiting on a human produced silence indistinguishable from an
    agent still working). Purely additive — every line this function writes is a
    work-item transition and still carries the same five fields, so a consumer
    reading ``status`` sees exactly what it saw before.
    """
    try:
        import json as _json
        from datetime import datetime, timezone
        path = os.path.join(str(root), ".bgate", "notify.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": "item.status",
                "item_id": item["id"], "status": item["status"],
                "seat": item["seat"], "title": item["title"][:120],
            }) + "\n")
    except Exception:
        pass


# Terminal statuses -> the event kind a subscriber filters on. Only completions
# map here: a rejection parks an item as 'failed' on its way back to 'queued'
# (see reject), and emitting item.failed for that would tell the router an agent
# crashed when a human simply said no.
_COMPLETION_KINDS = {"done": "item.done", "review": "item.review",
                     "failed": "item.failed"}


def _emit(root, kind: str, ref: str = "", payload: Optional[dict] = None) -> None:
    """Put one event on the bus (bgate_core.events), never at the cost of the
    transition that caused it.

    The event log is a notification substrate: subscribers read it to debrief the
    director, ring the bell and fire a webhook. All of that is worth less than the
    status change itself, so a locked database — or an events module that will not
    even import — loses the line and nothing else. events.emit already swallows
    its own failures; this guards the import as well, because the transition path
    must not depend on any of it being present.
    """
    try:
        from . import events as _events

        _events.emit(root, kind, ref=ref, payload=payload)
    except Exception:
        pass


def _item_event_payload(item: dict) -> dict:
    """The context a subscriber needs without re-reading the row.

    Deliberately includes the chain fields: "done and nothing follows" versus
    "done and link 3 of 4 just became ready" are different notifications, and a
    consumer that has to query the board to tell them apart is a consumer that
    races the next transition. The result note is TRIMMED — the full text stays on
    the item, and a payload is context for a ping, not a place to store a diff.
    """
    return {
        "item": int(item["id"]),
        "seat": item["seat"],
        "title": str(item["title"])[:200],
        "status": item["status"],
        "source": item.get("source") or "",
        "source_ref": str(item.get("source_ref") or ""),
        "chain_id": item.get("chain_id") or "",
        "chain_pos": int(item.get("chain_pos") or 0),
        "attempts": int(item.get("attempts") or 0),
        "result": str(item.get("result") or "")[:400],
    }


def _with_observed_writes(root, item_id: int, status: str, result: str) -> str:
    """Append what the harness SAW this item write to what the agent CLAIMS.

    A QA agent closed a gate reporting "no files were touched" while having
    written its own checkpoint file. The report was not dishonest -- it answered
    about the project's files and its own harness file was invisible to it --
    but nothing in the system could contradict it, which in the one seat whose
    job is refusing claims at face value is the actual defect.

    ATTACHED, NOT ENFORCED. The obvious alternative is a required disclosure
    field that queue_complete refuses without, and it fixes the wrong half: an
    agent that did not realise it wrote a file will not declare it either, so
    the field catches omission and never inaccuracy, while breaking every
    existing caller and anything already in flight. The hook already observes
    every write. Appending its record costs no caller anything, cannot be
    forgotten, and puts the claim and the evidence in one place -- which is
    where the QA gate reads from, so a future gate can compare them.

    Terminal statuses only: a queued or dispatched item has not finished writing,
    so a list taken then would be a snapshot presented as a total. `cancelled`
    IS on the list, and is arguably the case that needs it most -- a run someone
    killed is precisely the one where "what did it leave behind" has no other
    answer.

    The record accumulates across ROUNDS, because the lock owner a dispatch
    stamps is `item-<id>` and a reopened item keeps its id. That is the intended
    reading: the list is the item's total footprint, not one attempt's, and a QA
    gate looking at round two should see what round one wrote.
    """
    if status not in ("done", "failed", "cancelled"):
        return result
    try:
        from . import writelog
        observed = writelog.summary(root, f"item-{item_id}")
    except Exception:
        return result          # bookkeeping must never fail a completion
    if not observed:
        return result
    return (result.rstrip() + "\n\n" + observed) if result.strip() else observed


def set_status(root: str | os.PathLike[str], item_id: int, status: str,
               result: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    get(root, item_id)
    result = _with_observed_writes(root, item_id, status, result)
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE work_item SET status = ?, result = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, clip_result(result), item_id),
        )
    item = get(root, item_id)
    activity.log(root, "queue", f"item {item_id} -> {status}: {item['title'][:60]}",
                 seat=item["seat"], ref=str(item_id))
    _notify(root, item)
    iteration_id = None
    conn = db.connect(root)
    if item["source"] == "playtest" and item["source_ref"].isdigit():
        linked = conn.execute(
            "SELECT s.iteration_id FROM playtest_item i "
            "JOIN playtest_session s ON s.id = i.session_id WHERE i.id = ?",
            (int(item["source_ref"]),)).fetchone()
        iteration_id = int(linked["iteration_id"]) if linked and linked["iteration_id"] else None
    elif item["source"] == "artifact" and item["source_ref"].isdigit():
        linked = conn.execute(
            "SELECT iteration_id FROM artifact_revision WHERE id = ?",
            (int(item["source_ref"]),)).fetchone()
        iteration_id = int(linked["iteration_id"]) if linked and linked["iteration_id"] else None
    if iteration_id:
        iterations.add_event(
            root, iteration_id,
            "resulting_change" if status in ("done", "failed") else "queued_change",
            "work_item", str(item_id), f"Work item {item_id} -> {status}",
            {"status": status, "result": clip_result(result), "seat": item["seat"]})
    return item


def blocker(root: str | os.PathLike[str], item_id: int) -> Optional[dict]:
    """The predecessor this item is still waiting on, or None if it can run.

    Returns the blocking row (id/seat/title/status) rather than a bool because
    every caller that refuses a dispatch has to SAY what it is waiting for —
    "blocked" with no antecedent is the least actionable refusal there is.
    """
    item = get(root, item_id)
    if not item.get("depends_on"):
        return None
    try:
        dep = get(root, int(item["depends_on"]))
    except LookupError:
        return None            # deleted predecessor: unblock rather than strand
    if dep["status"] in SATISFIED:
        return None
    return {"id": dep["id"], "seat": dep["seat"], "title": dep["title"],
            "status": dep["status"]}


def chain(root: str | os.PathLike[str], chain_id: str) -> list[dict]:
    """Every link of one chain, in running order."""
    return rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE chain_id = ? ORDER BY chain_pos, id",
        (str(chain_id),)))


def successors(root: str | os.PathLike[str], item_id: int) -> list[dict]:
    """Items waiting directly on this one."""
    return rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE depends_on = ? ORDER BY chain_pos, id",
        (int(item_id),)))


def complete(root: str | os.PathLike[str], item_id: int, result: str = "",
             failed: bool = False, skip_gate: Optional[bool] = None) -> dict:
    """An agent reporting the end of its own run — THROUGH the approval gate.

    Every completion path funnels here (the MCP queue_complete tool, the
    dispatcher's exit handler) so the gate is one decision in one place. Before
    this existed, "done" was written directly by three callers, which is exactly
    how a setting like this ends up honoured in two of them.

    A failure is always a failure: the builder's gate holds work for approval, it
    does not ask anyone to bless a crash.

    THIS is where the completion event is emitted, not set_status: set_status is
    also how a reopen, a reject's parking step and the reaper's bookkeeping move
    an item, and emitting there would put transitions on the bus that no
    subscriber can act on — a router seeing item.failed for the failed half of a
    rejection would try to auto-reopen work a human is already sending back.
    """
    from . import gates as _gates

    # WHO CLOSED IT, AND WHETHER A REVIEWER IS OWED. The QA gate reviews state
    # at close, so a HAND-CLOSE — which is what a killed-but-successful run
    # leaves a human doing — files a reviewer against a result note describing
    # work that may already have been superseded. Measured: an agent was
    # dispatched, and paid for, to verify block-modelled vehicles that had been
    # replaced by image-to-3D generation days earlier.
    #
    # The default is not "always skip a human's close": a human closing an
    # agent's finished work is exactly the case worth reviewing. It is
    # "skip when a human closes work the agent did not report itself", which is
    # what `skip_gate=None` resolves to below — the caller may still say either
    # way explicitly.
    closer = activity.current_actor()
    by_machine = activity.is_machine(closer)
    if skip_gate is None:
        skip_gate = not by_machine and not failed
    if failed:
        item = set_status(root, item_id, "failed", result=result)
    elif _gates.holds_for_human(root):
        item = set_status(root, item_id, "review", result=result)
    else:
        item = set_status(root, item_id, "done", result=result)
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET closed_by = ?, gate_skip = ? WHERE id = ?",
                (closer[:120], 1 if skip_gate else 0, item_id))
        item = get(root, item_id)
    except Exception:
        pass            # bookkeeping must never lose a completion
    kind = _COMPLETION_KINDS.get(item["status"])
    if kind:
        _emit(root, kind, ref=str(item_id), payload=_item_event_payload(item))
    return item


def approve(root: str | os.PathLike[str], item_id: int, note: str = "",
            by: str = "") -> dict:
    """The human saying yes: 'review' -> 'done', which releases the chain.

    ``by`` is recorded because an approval nobody signed is not an approval —
    and because an agent must not be able to approve its own work, the caller
    that stamps this is the dashboard (a human session), not a seat tool.
    """
    item = get(root, item_id)
    if item["status"] != "review":
        raise ValueError(f"item {item_id} is {item['status']!r} — only an item "
                         "waiting in review can be approved")
    actor = by or activity.current_actor()
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET approved_by = ? WHERE id = ?",
                     (actor[:120], item_id))
    note = (note or "").strip()
    tail = f"\n\nAPPROVED by {actor}" + (f": {note[:400]}" if note else "")
    released = set_status(root, item_id, "done",
                         result=(item.get("result") or "") + tail)
    # item.approved rather than item.done: the completion was already announced
    # when the item parked in 'review', and a second item.done would make every
    # subscriber act twice on one piece of work. What is new here is that a human
    # signed it and the chain behind it is now free.
    _emit(root, "item.approved", ref=str(item_id),
          payload={**_item_event_payload(released), "by": actor[:120],
                   "note": note[:400]})
    return released


def reject(root: str | os.PathLike[str], item_id: int, reason: str,
           by: str = "") -> dict:
    """The human saying no: back to 'queued' with the reason in the brief.

    Same motion as ``reopen`` (and it reuses it) so a rejected item is
    indistinguishable from a QA-failed one downstream — one fix path, one round
    counter, one place the next agent looks for what to change.
    """
    item = get(root, item_id)
    if item["status"] != "review":
        raise ValueError(f"item {item_id} is {item['status']!r} — only an item "
                         "waiting in review can be rejected")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a rejection needs a reason — say exactly what to fix")
    actor = by or activity.current_actor()
    # reopen() guards on done/failed/cancelled, which 'review' is not: it is not
    # a finished state, it is a held one. Park it as failed first so the one
    # reopen path (and its round counter) stays the only way work comes back.
    set_status(root, item_id, "failed", result=f"rejected by {actor}")
    sent_back = reopen(root, item_id, f"REJECTED by {actor}: {reason}")
    # After the reopen, so the payload's status is 'queued' — what a subscriber
    # would see if it went and looked. Emitting between the two writes would
    # publish the 'failed' half, which is bookkeeping, not what happened.
    _emit(root, "item.rejected", ref=str(item_id),
          payload={**_item_event_payload(sent_back), "by": actor[:120],
                   "reason": reason[:400]})
    return sent_back


def stop(root: str | os.PathLike[str], item_id: int, by: str = "",
         reason: str = "") -> dict:
    """A HUMAN ended this run. Bank it as failed, and say so in a column.

    ``status`` stays 'failed' and that is deliberate — the item did not finish,
    it is worth reopening, and 'failed' is what reopen(), the QA gate query, the
    chain interlock and the console's lanes all key on. Inventing a sixth status
    would change behaviour in every one of those by omission.

    What was missing is the CAUSE, which is a different question from the status
    and now has its own field. Before this the only evidence was English in the
    result note, so ``status: failed`` covered both "the agent crashed" and "a
    person pressed stop" with no way to tell them apart except by reading. Three
    items across three seats flipped to failed in the same second during a STOP
    ALL and read as three separate bugs; a same-second multi-seat failure is the
    signature of a systemic event, and nothing could see it.

    Two things worth remembering about a stopped run, because both surprised
    somebody: a stop often lands AFTER the valuable work, so check what survived
    before assuming loss — and ``stopped_at`` is kept separate from
    ``updated_at`` so a stopped item that is later reopened and re-run still says
    it was stopped once, three rounds ago, rather than claiming it just now.
    """
    actor = (by or activity.current_actor() or "the dashboard")[:120]
    said = (reason or "").strip() or (
        f"stopped by {actor} — this run was ended by hand, "
        "it did not die on its own")
    item = set_status(root, item_id, "failed", result=said)
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET stopped_by = ?, "
                "stopped_at = datetime('now') WHERE id = ?", (actor, item_id))
        item = get(root, item_id)
    except Exception:
        pass            # bookkeeping must never lose the stop itself
    # ON THE HEARTBEAT AS A STOP, not just as another 'failed'. set_status above
    # already appended its line, but it ran before the column was stamped and it
    # says exactly what a crash says. A watcher tailing the stream during a STOP
    # ALL would otherwise see a burst of identical failures and have to open the
    # database to learn a person caused them.
    _notify(root, {**item, "status": "stopped"})
    _emit(root, "item.stopped", ref=str(item_id),
          payload={**_item_event_payload(item), "by": actor})
    return item


def was_stopped(item: dict) -> bool:
    """Did a human end this run, as opposed to it dying?

    A helper rather than an inline truth test because callers kept reaching for
    the prose. ``stopped_by`` is absent on a project that has not migrated and
    empty on every crash, so this is the one place that has to be careful about
    the difference between "no" and "cannot tell".
    """
    return bool((item or {}).get("stopped_by"))


def awaiting_review(root: str | os.PathLike[str]) -> list[dict]:
    """What the human owes an answer on, oldest first — a drain list."""
    return rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE status = 'review' "
        "ORDER BY updated_at, id"))


def reopen(root: str | os.PathLike[str], item_id: int, reason: str) -> dict:
    """Send a done/failed item back to 'queued' for another round.

    Retrying failed work is the most common motion in an agent runner, and the
    reason is the whole payload: it is APPENDED to the brief so the next agent
    reads exactly what to fix rather than repeating the run that failed.
    ``attempts`` counts the rounds, which is what makes a loop visible.
    """
    item = get(root, item_id)
    if item["status"] not in ("done", "failed", "cancelled"):
        raise ValueError(f"item {item_id} is {item['status']!r} — only "
                         "done/failed/cancelled items can be reopened")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("reason is required — say exactly what to fix")
    # WHAT IS ALREADY ON DISK RIDES INTO THE NEXT ROUND. A reopen used to hand
    # the agent nothing but the reason, so a run that was stopped by a ceiling
    # or stranded by a dashboard restart was repeated from scratch — paying
    # twice for files that were already sitting there correct. The harness
    # observed every one of those writes; not passing them on was the waste.
    already = ""
    try:
        from . import writelog
        observed = writelog.summary(root, f"item-{item_id}")
        if observed:
            already = ("\n\nALREADY ON DISK from the previous attempt — the "
                       "harness observed these writes itself, so this is not a "
                       "claim by the agent that made them. READ THEM BEFORE "
                       "WRITING ANYTHING: continue from what is there, and "
                       "regenerate only what is actually wrong.\n" + observed)
    except Exception:
        already = ""
    stamp = (("\n\n--- REOPENED (attempt %d) ---\n" % (item["attempts"] + 2))
             + reason + already)
    update(root, item_id, brief=(item["brief"] or "") + stamp[:6000])
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET attempts = attempts + 1 WHERE id = ?",
                     (item_id,))
    return set_status(root, item_id, "queued", result=f"reopened: {reason[:1900]}")


# Columns dispatch owns: they describe the RUN, not the request, so they are
# deliberately out of update()'s reach — a reviewer editing a brief must not be
# able to rewrite what a run cost or where it branched from.
_RUN_FIELDS = ("actor", "base_commit", "branch", "worktree", "num_turns",
               "total_cost_usd", "max_cost_usd", "max_runtime_s")


def set_run_fields(root: str | os.PathLike[str], item_id: int, **fields) -> dict:
    """Stamp dispatch/completion facts onto an item. Unknown keys are ignored."""
    sets = {k: v for k, v in fields.items() if k in _RUN_FIELDS and v is not None}
    if not sets:
        return get(root, item_id)
    assignments = ", ".join(f"{k} = ?" for k in sets)
    with db.tx(root) as conn:
        conn.execute(f"UPDATE work_item SET {assignments} WHERE id = ?",
                     [*sets.values(), item_id])
    return get(root, item_id)


def next_for(root: str | os.PathLike[str], seat: str) -> Optional[dict]:
    """The highest-priority READY item for a seat — what an agent works next.

    Ready excludes a link whose predecessor has not landed. Handing an agent
    blocked work is worse than handing it nothing: it cannot tell the difference,
    so it starts, finds the file it was promised missing, and either stalls or
    invents one.
    """
    row = db.connect(root).execute(
        "SELECT i.* FROM work_item i "
        "LEFT JOIN work_item d ON d.id = i.depends_on "
        "WHERE i.status = 'queued' AND i.seat = ? "
        "  AND (i.depends_on IS NULL OR d.id IS NULL OR d.status = 'done') "
        "ORDER BY i.priority DESC, i.id LIMIT 1", (seat,)).fetchone()
    return dict(row) if row else None


def sync_promoted(root: str | os.PathLike[str]) -> dict:
    """Promoted playtest items the user blessed become queued work, once each.

    Provenance rides along (source_ref = playtest item id) so the working agent
    can pull the frame + telemetry via playtest_brief.
    """
    conn = db.connect(root)
    promoted = rows(conn.execute(
        """
        SELECT i.id, i.seat, i.kind, i.text FROM playtest_item i
        WHERE i.status = 'promoted'
          AND NOT EXISTS (SELECT 1 FROM work_item w
                          WHERE w.source = 'playtest' AND w.source_ref = CAST(i.id AS TEXT))
        """))
    created = []
    for item in promoted:
        seat = item["seat"] if item["seat"] in _seats.DEFAULT_SEATS else "gameplay"
        created.append(add(
            root, seat,
            title=f"[{item['kind']}] {item['text'][:70]}",
            brief=f"Promoted playtest feedback (playtest item {item['id']}): "
                  f"\"{item['text']}\". Pull playtest_brief for the frame and "
                  "the telemetry around this moment before acting.",
            source="playtest", source_ref=str(item["id"])))
    return {"created": len(created), "items": created}


def import_orbit(root: str | os.PathLike[str], api_url: str = "http://127.0.0.1:8077",
                 tag: str = "bgate") -> dict:
    """Optional: pull Orbit tickets tagged for this game into the queue.

    Best-effort by design — Orbit may not be running; a queue import must never
    take the dashboard down with it.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{api_url}/tickets?tag={tag}", timeout=5) as resp:
            tickets = json.loads(resp.read().decode())
    except Exception as exc:
        return {"created": 0, "error": f"orbit unreachable: {type(exc).__name__}: {exc}"}

    created = []
    existing = {r["source_ref"] for r in list_items(root) if r["source"] == "orbit"}
    for ticket in tickets if isinstance(tickets, list) else tickets.get("tickets", []):
        key = str(ticket.get("key") or ticket.get("id"))
        if key in existing:
            continue
        created.append(add(root, "gameplay",
                           title=f"[orbit {key}] {ticket.get('title', '')[:70]}",
                           brief=ticket.get("description", "") or "",
                           source="orbit", source_ref=key))
    return {"created": len(created)}
