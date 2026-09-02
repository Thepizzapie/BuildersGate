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

import json
import os
from typing import Optional

from . import activity, iterations, seats as _seats
from ..store import db
from ..store.util import rows

# 'cancelled' is a human calling work off — distinct from 'failed', which is an
# agent (or the watchdog) reporting it could not finish. Only the second is
# worth reopening; the audit needs to tell them apart.
#
# 'review' is finished-but-not-counted: the agent is gone, the work is on disk,
# and under the builder's gate (bgate_core.board.gates) a human has not yet said yes.
# It is deliberately NOT 'done' — a chain must not advance on unapproved work —
# and deliberately not 'dispatched', which would claim an agent is still running.
STATUSES = ("queued", "dispatched", "review", "done", "failed", "cancelled")

# Statuses a dependent item is allowed to start on top of. 'review' is not one
# of them: the whole point of the hold is that the next link waits.
SATISFIED = ("done",)

# Sources no automatic dispatcher may touch. Defined HERE because there are two
# such dispatchers now — the dashboard's autodeploy loop and a worker's
# claim_next — in different processes, and two copies of this tuple is how one
# of them ends up grabbing an escalation a human was supposed to read.
#   qa-gate-escalation — two agents could not agree and a human has to decide.
#   chat — a message to the director; the console dispatches those itself.
#
# failure-escalation is NOT held any more (2026-08-19). It was, on the theory
# that a failure past its retry cap needed a person — and the observed result
# was the board's worst dead end: the escalation sat queued forever, the
# failed item sat failed forever, and the human's actual job became clearing
# and re-dispatching by hand. The console's director session still gets first
# claim on a fresh escalation (followup._hand_to_director_session reserves
# it); when no such session exists, autodeploy now spawns a director-seat
# agent whose brief is to DIAGNOSE AND ACT — read the failure, fix the brief,
# queue_reopen or route the real blocker. Spend is bounded the same way it
# always was: ONE escalation per item, ever (followup.fail_escalated).
HELD_SOURCES = ("qa-gate-escalation", "chat")

# The source stamped on that escalation. Named here rather than in the router
# that files them because the hold above and the filing must never drift apart:
# a rename in one place and not the other silently makes escalations
# auto-dispatchable again, which is the exact failure the tuple exists to stop.
FAILURE_ESCALATION_SOURCE = "failure-escalation"
# A row created in two statements — INSERT with a placeholder, then UPDATE with
# the real text — is briefly dispatchable with nothing in it.
PLACEHOLDER_BRIEF = "(preparing%"

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


#: How much of a reopen reason rides in the result note. The brief gets the
#: whole thing; this is the summary line a listing shows.
MAX_REOPEN_REASON = 1900


def clip_reason(text: str) -> str:
    """Bound a reopen reason WITHOUT cutting mid-sentence, and say it was cut.

    ``reason[:1900]`` was a silent mid-sentence truncation, and a reopen reason
    is the one thing the next agent reads to learn what to change — a
    half-sentence there is worse than a short one, because it reads as
    complete. Cuts at the last sentence or line break before the ceiling and
    marks the cut, so the reader knows to open the brief for the rest (which
    carries the full text either way).
    """
    text = str(text or "")
    if len(text) <= MAX_REOPEN_REASON:
        return text
    head = text[:MAX_REOPEN_REASON]
    cut = max(head.rfind("\n"), head.rfind(". "), head.rfind("? "),
              head.rfind("! "))
    if cut > MAX_REOPEN_REASON - 600:
        head = head[:cut + 1]
    return head.rstrip() + (
        f" […reason clipped at {MAX_REOPEN_REASON} chars for this listing; "
        "the whole of it is appended to the item's brief]")


def add(root: str | os.PathLike[str], seat: str, title: str, brief: str = "",
        priority: int = 0, source: str = "manual", source_ref: str = "",
        chain_id: str = "", chain_pos: int = 0,
        depends_on: Optional[int] = None, chain_self: bool = False) -> dict:
    # A `scope_tier_id` used to be filed here and run through scope.enforce
    # first — the cut line's one gate. It never refused an item in the product's
    # life: untiered work was deliberately allowed through, and nothing was ever
    # tiered, so the gate was a no-op with a paragraph of documentation. Both
    # the parameter and the column are gone (migration 0030).
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
    if not title.strip():
        raise ValueError("a work item needs a title")
    if depends_on is not None:
        get(root, int(depends_on))          # LookupError if the link is a fiction
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO work_item (seat, title, brief, priority, source, "
            "source_ref, chain_id, chain_pos, depends_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seat, title.strip(), brief, priority, source, source_ref,
             chain_id.strip(), int(chain_pos),
             int(depends_on) if depends_on is not None else None),
        )
        item_id = int(cur.lastrowid)
        if chain_self:
            # The first link of a chain names the chain after its OWN row id,
            # inside the same transaction — see _chain_id_from for the race
            # the old select-max mint had.
            conn.execute("UPDATE work_item SET chain_id = ? WHERE id = ?",
                         (_chain_id_from(item_id), item_id))
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

    chain_id = (chain_id or "").strip()
    made: list[dict] = []
    previous: Optional[int] = None
    for pos, link in enumerate(links, start=1):
        item = add(root, str(link["seat"]), str(link["title"]),
                   brief=str(link.get("brief") or ""),
                   priority=int(link.get("priority") or 0),
                   source=str(link.get("source") or source),
                   source_ref=str(link.get("source_ref") or source_ref),
                   chain_id=chain_id, chain_pos=pos, depends_on=previous,
                   chain_self=not chain_id and pos == 1)
        if not chain_id:
            chain_id = str(item["chain_id"])
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


def _chain_id_from(first_item_id: int) -> str:
    """A short, human-sayable chain id: ``c<first link's own item id>``.

    It USED to be sequential (SELECT the highest c-number, add one) — which is
    a select-then-insert with no reservation, and there are two writers in two
    processes (the MCP server's queue_add_chain and the dashboard). Two
    concurrent chains minted the same ``cN`` and merged: debriefs, stall
    detection and "what is blocked behind this" all key on chain_id. The first
    link's item id is allocated by SQLite atomically, so it costs nothing and
    cannot collide; the numbers are merely no longer consecutive, and nothing
    ever parsed them as a sequence."""
    return f"c{int(first_item_id)}"


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


def _rotate_notify(path: str, cap_bytes: int = 5 * 1024 * 1024) -> None:
    """Roll notify.jsonl aside when it outgrows the cap. Best-effort.

    The stream was append-only for the life of a project with no rotation and
    no lock — migration 0016's own comment names torn multi-process lines on
    Windows as a real failure of exactly this file, and it also simply grew
    forever. One rolled generation (.1) keeps a consumer's recent history; a
    tailer that sees the file shrink re-reads from zero, which every cursor
    reader in this repo (events, feeds) already survives.
    """
    try:
        if os.path.getsize(path) >= cap_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


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
        _rotate_notify(path)
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
    """Put one event on the bus (bgate_core.store.events), never at the cost of the
    transition that caused it.

    The event log is a notification substrate: subscribers read it to debrief the
    director, ring the bell and fire a webhook. All of that is worth less than the
    status change itself, so a locked database — or an events module that will not
    even import — loses the line and nothing else. events.emit already swallows
    its own failures; this guards the import as well, because the transition path
    must not depend on any of it being present.
    """
    try:
        from ..store import events as _events

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


def emit_terminal(root, item_id: int) -> None:
    """Announce an item's terminal status for a transition that BYPASSED complete().

    The watchdog's ceiling kills (runtime, stall, cost, terminal CLI error) write
    'failed' through set_status and then _reap skips complete() because the item
    is no longer 'dispatched' — so the most expensive failures this system has
    were the only ones that never reached the bus: no bell, no webhook, no
    auto-reopen. This is their announcement path.

    NOT wired into set_status itself, and complete()'s docstring says why:
    set_status also moves items for reopen and for reject's parking step, and
    emitting there would tell the router an agent crashed when a human simply
    said no. This is called explicitly, by the one caller that knows its
    transition is a genuine terminal outcome.
    """
    try:
        item = get(root, item_id)
    except LookupError:
        return
    kind = _COMPLETION_KINDS.get(item["status"])
    if kind:
        _emit(root, kind, ref=str(item_id), payload=_item_event_payload(item))


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
        from ..store import writelog
        observed = writelog.summary(root, f"item-{item_id}")
    except Exception:
        return result          # bookkeeping must never fail a completion
    if not observed:
        # THE ABSENCE IS EVIDENCE TOO — for a MACHINE's 'done' on a maker
        # seat. "I finished the sprite work" over a run the hook watched
        # write nothing is exactly the claim-versus-record gap this function
        # exists to surface, and silence here let it read as consistent.
        # Still attached, still not enforced: some legitimate items write
        # nothing (a review, an answer), and the reviewer — not a regex — is
        # who weighs it. Human closes are not stamped; a human hand-closing
        # someone else's run is not the claimant.
        try:
            from . import activity as _act
            row = get(root, item_id)
            if (status == "done" and _act.is_machine(_act.current_actor())
                    and (row.get("seat") or "") not in ("director", "qa")
                    and (row.get("source") or "") != "chat"):
                return (result.rstrip() + "\n\n" if result.strip() else "") + \
                    ("HARNESS NOTE: the hook observed NO file writes from "
                     "this item's runs. If this item was supposed to change "
                     "the project, the claim above is unbacked.")
        except Exception:
            pass
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


def parents(root: str | os.PathLike[str], item_id: int) -> list[int]:
    """Every predecessor of this item — the single column AND the extra table.

    A game is a DAG: a scene can need the sprite, the sound and the script.
    `depends_on` holds one parent (and every chain is built on it); the rest
    live in work_item_dep. Cut dependencies are excluded — that is what
    cutting means.
    """
    item = get(root, item_id)
    out = [int(item["depends_on"])] if item.get("depends_on") else []
    try:
        extra = rows(db.connect(root).execute(
            "SELECT depends_on FROM work_item_dep WHERE item_id = ? "
            "AND cut_at IS NULL ORDER BY depends_on", (int(item_id),)))
    except Exception:
        extra = []             # pre-migration project: one parent is all there is
    out.extend(int(r["depends_on"]) for r in extra
               if int(r["depends_on"]) not in out)
    return out


def add_dependency(root: str | os.PathLike[str], item_id: int,
                   depends_on: int) -> dict:
    """Make this item wait for one MORE predecessor.

    The multi-parent half of queue_add's depends_on. Refuses a dependency on
    a fiction (an item silently waiting on nothing dispatches immediately —
    the exact failure the wait was for) and refuses a self-loop.
    """
    get(root, item_id)
    get(root, int(depends_on))
    if int(depends_on) == int(item_id):
        raise ValueError(f"item {item_id} cannot wait for itself")
    with db.tx(root) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO work_item_dep (item_id, depends_on) "
            "VALUES (?, ?)", (int(item_id), int(depends_on)))
    activity.log(root, "queue",
                 f"item {item_id} now also waits for #{int(depends_on)}",
                 ref=str(item_id))
    return {"item": int(item_id), "parents": parents(root, item_id)}


def cut_dependency(root: str | os.PathLike[str], item_id: int,
                   depends_on: int, by: str = "") -> dict:
    """THE REPAIR VERB. Release an item from a predecessor that will never land.

    A cancelled predecessor satisfies nothing (SATISFIED is 'done' alone), so
    before this the successors of a cut item waited forever with no operation
    that could free them — the board's one unreachable state. Cutting is
    recorded rather than deleted: "#13 waited on #12, which was cancelled, and
    a person released it" is exactly the sentence an audit needs later.

    The single `depends_on` column is cleared in place; an extra parent is
    marked cut. Either way the item becomes dispatchable if nothing else holds
    it, and it is the CALLER's job to have decided that is right.
    """
    item = get(root, item_id)
    actor = (by or activity.current_actor() or "the dashboard")[:120]
    dep = int(depends_on)
    if item.get("depends_on") and int(item["depends_on"]) == dep:
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET depends_on = NULL WHERE id = ?",
                         (int(item_id),))
    else:
        with db.tx(root) as conn:
            cur = conn.execute(
                "UPDATE work_item_dep SET cut_by = ?, cut_at = datetime('now') "
                "WHERE item_id = ? AND depends_on = ? AND cut_at IS NULL",
                (actor, int(item_id), dep))
            if cur.rowcount != 1:
                raise LookupError(
                    f"item {item_id} does not wait for #{dep} (already cut?)")
    activity.log(root, "queue",
                 f"item {item_id} released from #{dep} by {actor}",
                 seat=item["seat"], ref=str(item_id))
    remaining = parents(root, item_id)
    return {"item": int(item_id), "cut": dep, "by": actor,
            "still_waiting_on": remaining,
            "ready": not remaining and item["status"] == "queued"}


def blocker(root: str | os.PathLike[str], item_id: int) -> Optional[dict]:
    """The predecessor this item is still waiting on, or None if it can run.

    Returns the blocking row (id/seat/title/status) rather than a bool because
    every caller that refuses a dispatch has to SAY what it is waiting for —
    "blocked" with no antecedent is the least actionable refusal there is.
    With several parents it reports the FIRST unsatisfied one and counts the
    rest, so a fan-in does not have to be discovered one dispatch at a time.
    """
    unsatisfied = []
    for parent in parents(root, item_id):
        try:
            dep = get(root, int(parent))
        except LookupError:
            continue           # deleted predecessor: unblock rather than strand
        if dep["status"] not in SATISFIED:
            unsatisfied.append(dep)
    if not unsatisfied:
        return None
    dep = unsatisfied[0]
    out = {"id": dep["id"], "seat": dep["seat"], "title": dep["title"],
           "status": dep["status"]}
    if len(unsatisfied) > 1:
        out["also_waiting_on"] = [int(d["id"]) for d in unsatisfied[1:]]
    return out


def chain(root: str | os.PathLike[str], chain_id: str) -> list[dict]:
    """Every link of one chain, in running order."""
    return rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE chain_id = ? ORDER BY chain_pos, id",
        (str(chain_id),)))


def successors(root: str | os.PathLike[str], item_id: int) -> list[dict]:
    """Items waiting directly on this one — BOTH mechanisms, one answer.

    It used to read the `depends_on` column alone, which is half the graph:
    the extra parents added by `queue_add_dependency` live in `work_item_dep`,
    and an item waiting through that table was invisible to every caller
    asking "what unblocks when this lands". `parents()` has always merged the
    two; this is the same merge pointed the other way, and the asymmetry was a
    graph that could be walked in one direction only.

    A user should never have to know which of the two stores holds a link. See
    :func:`graph`.
    """
    direct = rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE depends_on = ? ORDER BY chain_pos, id",
        (int(item_id),)))
    seen = {int(r["id"]) for r in direct}
    try:
        extra = rows(db.connect(root).execute(
            "SELECT i.* FROM work_item i JOIN work_item_dep d ON d.item_id = i.id "
            "WHERE d.depends_on = ? AND d.cut_at IS NULL ORDER BY i.chain_pos, i.id",
            (int(item_id),)))
    except Exception:
        extra = []             # pre-migration project: one parent is all there is
    return direct + [r for r in extra if int(r["id"]) not in seen]


# ---------------------------------------------------------------------------
# DEPENDENCY ORDER, READ BY A HUMAN
# ---------------------------------------------------------------------------
#
# WORK-ITEM IDs ARE CREATION IDENTIFIERS, NOT EXECUTION ORDER, and the board
# presented them as if they were. Observed:
#
#     #42 enlarge rooms       done
#     #45 swap in furniture   running
#     #43 rebuild routes      queued
#
# which reads as a scheduler that skipped #43. It did not. #43 was filed after
# #42; #45 was inserted between them later, because the route measurements had
# to wait for real furniture dimensions. The dependency engine was correct
# throughout. The PRESENTATION made it look broken, and an operator who
# believes the scheduler is broken starts working around it.
#
# THE FIX IS NOT RENUMBERING. Stable ids stay stable — they are in briefs, in
# git commit messages, in result notes and in the human's head. What changes is
# that the human-facing surfaces state the execution order explicitly instead
# of leaving it to be inferred from an ordering that never meant that.
#
#     #43 Rebuild routes
#     WAITING ON #45 Swap in furniture
#
# beats `#43 QUEUED`, and it beats it precisely because it does not require the
# reader to already know the answer.

#: A status that satisfies a dependency. Only 'done' — see cut_dependency.
_TERMINAL = ("done", "cancelled")


def waiting_line(root: str | os.PathLike[str], item_id: int) -> str:
    """"WAITING ON #45 Swap in furniture" — or '' when nothing holds it.

    The one sentence the board was missing. Names the blocker AND ITS TITLE:
    an id alone sends the reader off to look it up, and the whole defect here
    is that people were not looking things up.
    """
    blk = blocker(root, item_id)
    if blk is None:
        return ""
    also = blk.get("also_waiting_on") or []
    tail = (f" (and {len(also)} more: "
            + ", ".join(f"#{i}" for i in also[:4]) + ")") if also else ""
    return f"WAITING ON #{blk['id']} {blk['title']}{tail}"


def graph(root: str | os.PathLike[str], *, limit: int = 400) -> dict:
    """The whole dependency graph, normalised, with a topological READ ORDER.

    ``order`` is a display sequence, not a renumbering: predecessors before
    successors, and within a tier the existing priority/id ordering, so two
    unrelated items keep the order the board already showed them in. Items in a
    cycle (which the add paths refuse to create, but a hand-edited database
    could) come last and are named in ``cycles`` rather than silently dropped.

    Every node carries ``execution_state``, which is the question the operator
    was actually asking:

        running | ready | waiting | blocked | held | done | failed

    ``waiting`` means an ordinary predecessor has not landed yet; ``blocked``
    means one never will on its own (a cancelled or failed parent), which is
    the state that needs a person and used to look identical to ``waiting``.
    """
    items = {int(r["id"]): dict(r)
             for r in list_items(root)[:max(1, int(limit))]}
    parents_of = {i: [p for p in parents(root, i) if p in items] for i in items}
    children_of: dict[int, list[int]] = {i: [] for i in items}
    for child, ups in parents_of.items():
        for up in ups:
            children_of.setdefault(up, []).append(child)

    try:
        held_seats = set(__import__(
            "bgate_core.design.greenlight", fromlist=["x"]).held_seats(root))
    except Exception:
        held_seats = set()

    # Kahn, seeded and tie-broken by the board's own ordering so the display is
    # stable between calls.
    def rank(i: int) -> tuple:
        row = items[i]
        return (-int(row.get("priority") or 0), i)

    pending = {i: len(parents_of[i]) for i in items}
    frontier = sorted([i for i, n in pending.items() if n == 0], key=rank)
    order: list[int] = []
    while frontier:
        node = frontier.pop(0)
        order.append(node)
        for child in children_of.get(node, []):
            pending[child] -= 1
            if pending[child] == 0:
                frontier.append(child)
        frontier.sort(key=rank)
    cycles = sorted(i for i in items if i not in order)
    order.extend(cycles)

    nodes = []
    for position, i in enumerate(order):
        row = items[i]
        status = str(row.get("status") or "")
        unresolved = [p for p in parents_of[i]
                      if str(items[p].get("status")) != "done"]
        dead = [p for p in unresolved
                if str(items[p].get("status")) in _TERMINAL + ("failed",)]
        if status == "dispatched":
            state = "running"
        elif status in ("done", "failed", "cancelled", "review"):
            state = status
        elif str(row.get("seat")) in held_seats:
            state = "held"
        elif str(row.get("source") or "") in HELD_SOURCES:
            state = "held"
        elif dead:
            state = "blocked"
        elif unresolved:
            state = "waiting"
        else:
            state = "ready"
        nodes.append({
            "id": i,
            "title": row.get("title", ""),
            "seat": row.get("seat", ""),
            "status": status,
            "execution_state": state,
            "execution_position": position,
            "depends_on": parents_of[i],
            "unresolved": unresolved,
            "blocking_now": (unresolved[0] if unresolved else None),
            "unblocks": sorted(children_of.get(i, [])),
            "waiting_line": (
                f"WAITING ON #{unresolved[0]} {items[unresolved[0]]['title']}"
                if unresolved else ""),
            "in_cycle": i in cycles,
        })
    return {
        "nodes": nodes,
        "order": order,
        "cycles": cycles,
        "note": ("`order` is a DISPLAY order derived from the dependency "
                 "graph. Ids are creation identifiers and are never "
                 "renumbered — read `execution_position`, not `id`, for what "
                 "runs when."),
        "sources": ("work_item.depends_on and work_item_dep are one graph "
                    "here; which table holds a link is not a question anybody "
                    "should have to answer."),
    }


def execution_path(root: str | os.PathLike[str], item_id: int) -> list[dict]:
    """The chain that has to happen before this item, in the order it happens.

    What ``#42 -> #45 -> #43`` looks like as data. Multiple parents are
    followed depth-first through the currently-blocking one, with the rest
    named on each node, because "which of my three parents is holding me right
    now" is the question and the other two are context.
    """
    seen: set[int] = set()
    out: list[dict] = []

    def walk(node: int) -> None:
        if node in seen:
            return
        seen.add(node)
        try:
            row = get(root, node)
        except LookupError:
            return
        for parent in parents(root, node):
            walk(parent)
        out.append({"id": node, "title": row["title"], "seat": row["seat"],
                    "status": row["status"],
                    "depends_on": parents(root, node)})

    walk(int(item_id))
    return out


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
    # NOT droppable telemetry, retried and then SHOUTED. gate_skip is policy:
    # losing it means the QA gate pays a reviewer for a hand-close — the exact
    # failure migration 0017 exists to prevent — with zero trace the stamp was
    # lost. The completion itself must still never be lost to this, so a
    # second failure logs the loss loudly instead of raising.
    for attempt in (0, 1):
        try:
            with db.tx(root) as conn:
                conn.execute(
                    "UPDATE work_item SET closed_by = ?, gate_skip = ? "
                    "WHERE id = ?",
                    (closer[:120], 1 if skip_gate else 0, item_id))
            item = get(root, item_id)
            break
        except Exception as exc:
            if attempt:
                try:
                    activity.log(root, "queue",
                                 f"LOST STAMP on #{item_id}: closed_by/"
                                 f"gate_skip could not be written "
                                 f"({type(exc).__name__}) — the QA gate may "
                                 "review a hand-close", ref=str(item_id))
                except Exception:
                    pass
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
    # stopped_by is policy, not telemetry: was_stopped() reads it to keep the
    # follow-up router from auto-retrying a run a human deliberately ended.
    # Same retry-then-shout shape as the gate_skip stamp in complete().
    for attempt in (0, 1):
        try:
            with db.tx(root) as conn:
                conn.execute(
                    "UPDATE work_item SET stopped_by = ?, "
                    "stopped_at = datetime('now') WHERE id = ?",
                    (actor, item_id))
            item = get(root, item_id)
            break
        except Exception as exc:
            if attempt:
                try:
                    activity.log(root, "queue",
                                 f"LOST STAMP on #{item_id}: stopped_by could "
                                 f"not be written ({type(exc).__name__}) — "
                                 "the router may auto-retry a human stop",
                                 ref=str(item_id))
                except Exception:
                    pass
    # ON THE HEARTBEAT AS A STOP, not just as another 'failed'. set_status above
    # already appended its line, but it ran before the column was stamped and it
    # says exactly what a crash says. A watcher tailing the stream during a STOP
    # ALL would otherwise see a burst of identical failures and have to open the
    # database to learn a person caused them.
    _notify(root, {**item, "status": "stopped"})
    _emit(root, "item.stopped", ref=str(item_id),
          payload={**_item_event_payload(item), "by": actor})
    return item


def note_auto_retry(root: str | os.PathLike[str], item_id: int) -> int:
    """Record that the HARNESS bought this item another round. Returns the total.

    Written next to the other item writes, and in the same transaction style,
    because the number is a spend control: the follow-up router's cap on
    automatic re-dispatch is enforced against this column and nothing else. A
    counter kept in the router's memory resets when the dashboard restarts, and
    a cap that a restart clears is a cap that a structurally-broken item (a
    missing key, a credit block) escapes by simply failing long enough.

    Deliberately separate from ``attempts``: that counts every round the item
    has had, including a human's reopen and a QA rejection. Charging a person's
    own retry against the automatic budget would deny the one free attempt to
    the items somebody is already working on.

    Best-effort on a project whose database predates the column (migration
    0041) — a bookkeeping write must not lose the reopen it is describing. It
    returns 0 in that case, which reads as 'no automatic retries recorded', and
    the caller's cap is then enforced by ``attempts`` alone.
    """
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET auto_retries = COALESCE(auto_retries, 0) + 1 "
                "WHERE id = ?", (int(item_id),))
        return int(get(root, item_id).get("auto_retries") or 0)
    except Exception:
        return 0


MAX_PREMISE = 1200


def premise_refuted(root: str | os.PathLike[str], item_id: int, *,
                    claim: str, measured: str, did_instead: str,
                    by: str = "") -> dict:
    """THE BRIEF WAS WRONG, AND HERE IS THE MEASUREMENT. A real outcome.

    THE MOST VALUABLE THING AGENTS DID IN THE BENCHMARK, and until now it
    survived only as prose in a result note — where it dies with the item.
    Three times an agent was handed a brief containing a false MEASURED
    premise, twice authored by the director:

      * an item filed on a "0.14 m guard clearance" that was in fact a task
        marker 2.75 m from the real target. The agent measured, refused to move
        furniture that was fine, and fixed the mislabelled assertion instead —
        adding the missing half of the gate and a deliberately-wrong control.
      * a briefed "blind spot" in a vision cone. The agent traced the
        implementation, found the cone used the flattened horizontal bearing so
        the claimed blind spot could not exist, said so, and built the correct
        fix anyway.
      * an inherited PASS from a previous attempt. The agent distrusted it,
        found the driver bug behind it, and fixed that instead.

    Each of those prevented a wrong fix from shipping. None of them was
    visible on the board, none was searchable, and none reached the author of
    the brief in a form that would stop them writing the next one the same way.

    THREE FIELDS, ALL REQUIRED, and the middle one is the point: a refutation
    without a measurement is a disagreement, and a disagreement is not evidence.
    Recording one does NOT close the item — an agent that refuted a premise
    usually went on to do the right work, and the outcome of that is a separate
    question from this.
    """
    item = get(root, item_id)
    parts = {"claim": claim, "measured": measured, "did_instead": did_instead}
    for name, value in parts.items():
        cleaned = " ".join(str(value or "").split())
        if len(cleaned) < 10:
            raise ValueError(
                f"{name} is required and has to be a sentence. A refutation is "
                "worth something because it carries the claim as stated, the "
                "measurement that contradicts it, and what you did instead — "
                "drop any one of those and it is an opinion.")
        parts[name] = cleaned[:MAX_PREMISE]
    actor = (by or activity.current_actor() or "")[:120]
    row = {"id": int(item_id), "seat": item["seat"], "by": actor, **parts}
    _emit(root, "item.premise_refuted", ref=str(item_id), payload=row)
    activity.log(root, "queue",
                 f"PREMISE REFUTED on #{item_id}: {parts['claim'][:100]} — "
                 f"measured: {parts['measured'][:100]}",
                 seat=item["seat"], ref=str(item_id))
    note = ("\n\nPREMISE REFUTED — this brief contained a measured claim that "
            "is not true.\n"
            f"  CLAIMED:  {parts['claim']}\n"
            f"  MEASURED: {parts['measured']}\n"
            f"  INSTEAD:  {parts['did_instead']}")
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET result = ? WHERE id = ?",
                (clip_result((item.get("result") or "") + note), int(item_id)))
    except Exception:                                             # noqa: BLE001
        pass
    return {**row, "recorded": True,
            "note": "the item is NOT closed by this — report its own outcome "
                    "separately"}


def refutations(root: str | os.PathLike[str], limit: int = 50) -> list[dict]:
    """Every refuted premise on this board, newest first. For the digest."""
    try:
        rows_ = db.connect(root).execute(
            "SELECT id, ref, payload, created_at FROM event WHERE kind = ? "
            "ORDER BY id DESC LIMIT ?",
            ("item.premise_refuted", max(1, int(limit)))).fetchall()
    except Exception:
        return []
    out = []
    for row in rows_:
        try:
            got = json.loads(row["payload"] or "{}")
        except Exception:
            got = {}
        out.append({"at": row["created_at"], "item": row["ref"], **got})
    return out


def mark_exhausted(root: str | os.PathLike[str], item_id: int,
                   why: str) -> dict:
    """The harness has stopped buying rounds for this item. Say so ON THE ROW.

    Called when the follow-up router escalates instead of retrying. Before
    this the fact lived only in the router's decision and in a director item
    filed elsewhere; the item itself carried two counters and left every
    reader to do the arithmetic. Two readers did it differently.

    Exhausted work does not dispatch (see :func:`ready`) and is not offered by
    :func:`next_for`. Only a reopen clears it, which is the explicit human or
    director action the state exists to require.
    """
    said = " ".join(str(why or "").split())[:600] or (
        "the automatic retry budget is spent")
    try:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE work_item SET exhausted_at = datetime('now'), "
                "exhausted_why = ? WHERE id = ?", (said, int(item_id)))
    except Exception:
        # A project whose database predates migration 0043. The escalation
        # itself already landed; losing the stamp must not lose that.
        return get(root, item_id)
    activity.log(root, "queue",
                 f"item {item_id} is exhausted: {said[:120]}", ref=str(item_id))
    _emit(root, "item.failed", ref=str(item_id),
          payload={"id": int(item_id), "exhausted": True, "why": said})
    return get(root, item_id)


def is_exhausted(item: dict) -> bool:
    """Has the harness stopped retrying this item? Never raises."""
    return bool((item or {}).get("exhausted_at"))


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
        from ..store import writelog
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
    # A REOPEN IS THE HUMAN ACTION THAT CLEARS EXHAUSTION. Whoever reopens has
    # decided this is worth another round; leaving the stamp on would mean the
    # dispatcher still refused it and the reopen did nothing visible.
    try:
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET exhausted_at = NULL, "
                         "exhausted_why = '' WHERE id = ?", (item_id,))
    except Exception:
        pass                              # pre-0043 database: nothing to clear
    return set_status(root, item_id, "queued",
                      result="reopened: " + clip_reason(reason))


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

    DELEGATES TO ``ready()``, WHICH IS WHAT ITS DOCSTRING ALREADY CLAIMED TO BE.
    This function kept its own SQL, and the two copies had drifted exactly the
    way ``ready``'s own comment warns about: this one did not filter
    HELD_SOURCES, did not filter placeholder briefs, and did not apply the
    production-stage seat holds. So ``queue_next('director')`` reported a
    qa-gate escalation — a row NO auto-dispatcher will ever take, filed
    precisely because a human has to decide — as the next thing to work on.
    Observed on a board where the only way to tell "waiting for a director"
    from "ready to run" was to read the retry counters by hand.

    Ready excludes a link whose predecessor has not landed. Handing an agent
    blocked work is worse than handing it nothing: it cannot tell the difference,
    so it starts, finds the file it was promised missing, and either stalls or
    invents one.
    """
    found = ready(root, seat=seat, limit=20)
    return dict(found[0]) if found else None


def stalled(root: str | os.PathLike[str], seat: str = "") -> list[dict]:
    """Queued work that NO dispatcher will take, and why. The other half of ready().

    ``ready`` answers "what may start". Nothing answered "what is sitting here
    that never will", and the difference between those two lists is the whole
    of an operator's morning. An item whose automatic retries are spent, whose
    source is human-held, or whose seat the production stage is holding looks
    identical to fresh work in every listing — the only tell was the retry
    counters, read by hand, on the row.

    Each row carries ``stalled_because`` and ``needs``, where ``needs`` names
    the human or director action that would release it.
    """
    from ..design import greenlight as _greenlight

    try:
        held = set(_greenlight.held_seats(root))
    except Exception:
        held = set()
    dispatchable = {int(r["id"]) for r in ready(root, seat=seat, limit=500)}
    out: list[dict] = []
    for row in list_items(root, status="queued", seat=seat or None):
        item = dict(row)
        item_id = int(item["id"])
        if item_id in dispatchable:
            continue
        source = str(item.get("source") or "")
        auto = int(item.get("auto_retries") or 0)
        if source in HELD_SOURCES:
            because = (f"source {source!r} is never auto-dispatched — it "
                       "exists because a person has to decide")
            needs = "a human (or the director session) takes it by hand"
        elif str(item.get("seat")) in held:
            because = "the production stage is holding this seat"
            needs = "greenlight_advance, or a per-seat waiver"
        elif str(item.get("brief") or "").startswith("(preparing"):
            because = "the brief is still a placeholder"
            needs = "whatever is filing this item finishes writing it"
        elif blocker(root, item_id) is not None:
            blk = blocker(root, item_id)
            because = waiting_line(root, item_id)
            needs = ("nothing — this is the board working" if
                     blk["status"] in ("queued", "dispatched", "review") else
                     f"#{blk['id']} is {blk['status']!r} and will not reach "
                     "'done' on its own: queue_reopen it, or "
                     "queue_cut_dependency to release this")
        else:
            continue
        item["stalled_because"] = because
        item["needs"] = needs
        item["auto_retries"] = auto
        out.append(item)
    return out


def reserve(root: str | os.PathLike[str], item_id: int) -> bool:
    """Atomically take a queued item for dispatch: queued -> dispatched.

    There are two dispatchers now — the dashboard (autodeploy, buttons) and a
    worker's claim_next — in different processes. Both used to be able to read
    'queued' and proceed, which is two agents spawned against one item with
    nothing to say so. The WHERE clause is the whole point: exactly one caller
    wins the row, and the loser finds out here rather than two items later.

    Deliberately touches ONLY status and updated_at. set_status also rewrites
    the result column, and a reservation that wiped a reopened item's
    "reopened: ..." note would destroy the one line explaining the round.
    """
    get(root, item_id)                       # LookupError if the item is fiction
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE work_item SET status = 'dispatched', "
            "updated_at = datetime('now') WHERE id = ? AND status = 'queued'",
            (item_id,))
        taken = cur.rowcount == 1
    if taken:
        item = get(root, item_id)
        activity.log(root, "queue",
                     f"item {item_id} -> dispatched: {item['title'][:60]}",
                     seat=item["seat"], ref=str(item_id))
        _notify(root, item)
    return taken


def release(root: str | os.PathLike[str], item_id: int) -> bool:
    """Undo reserve(): dispatched -> queued, touching nothing else.

    For a dispatch refused AFTER the reservation (dirty tree, missing runner,
    failed worktree) — the item was never run, so it goes back exactly as it
    was rather than through set_status, which would blank its result note.
    """
    with db.tx(root) as conn:
        cur = conn.execute(
            "UPDATE work_item SET status = 'queued', "
            "updated_at = datetime('now') WHERE id = ? AND status = 'dispatched'",
            (item_id,))
        undone = cur.rowcount == 1
    if undone:
        activity.log(root, "queue",
                     f"item {item_id} -> queued (dispatch refused after reserve)",
                     ref=str(item_id))
    return undone


def ready(root: str | os.PathLike[str], seat: str = "",
          limit: int = 120) -> list[dict]:
    """Queued items an auto-dispatcher may start, most deserving first.

    THE ONE COPY OF THE READINESS RULE. Both dispatchers - the dashboard's
    autodeploy loop and a worker's claim_next - used to carry their own SQL
    for "queued, not held, brief real, parents landed", and the two copies
    had already drifted once (autodeploy joined the single-column parent,
    claim_next re-derived it) before this function existed. A candidate that
    one dispatcher considers ready and the other does not is a race with a
    personality; readiness is now a fact about the ROW, answered here, and
    the dispatchers differ only in what they do with the list.

    Multi-parent deps (work_item_dep) are checked via blocker() in Python -
    the SQL join sees only the depends_on column - which is also why the
    LIMIT matters: it bounds the per-call blocker() sweep. Items filtered
    out here produce NO refusal by design; a blocked successor is the board
    working as intended, and the row's own `waiting_on` (queue_list) is
    where the reason surfaces.

    THE PRODUCTION STAGE IS APPLIED HERE, and here is the only place it could
    have been. greenlight holds whole SEATS - art, audio and cinematic do not
    dispatch until gameplay has proved the loop in a graybox - and the advisory
    version of that rule (a line in the director's brief) lost every time it
    competed with a queue full of dispatchable work. A seat held by the stage
    is filtered out exactly like a blocked successor, for the same reason: this
    is the one function both dispatchers ask.
    """
    from ..design import greenlight as _greenlight

    marks = ", ".join("?" * len(HELD_SOURCES))
    seat_clause = "AND i.seat = ? " if seat else ""
    params = (*( (seat,) if seat else () ), *HELD_SOURCES, PLACEHOLDER_BRIEF)
    candidates = rows(db.connect(root).execute(
        f"SELECT i.* FROM work_item i "
        f"LEFT JOIN work_item d ON d.id = i.depends_on "
        f"WHERE i.status = 'queued' {seat_clause}"
        f"AND i.source NOT IN ({marks}) AND i.brief NOT LIKE ? "
        "AND (i.depends_on IS NULL OR d.id IS NULL OR d.status = 'done') "
        f"ORDER BY i.priority DESC, i.id LIMIT {max(1, int(limit))}",
        params))
    try:
        held = set(_greenlight.held_seats(root))
    except Exception:
        # A greenlight doc that will not read must not stop the board. The
        # failure it guards against is expensive; a deadlocked queue is worse,
        # and state() surfaces the unreadable doc to anyone looking.
        held = set()
    # EXHAUSTED WORK IS NOT CLAIMABLE WORK. An item the harness has stopped
    # retrying (see mark_exhausted) is waiting on a decision, not on a slot,
    # and offering it to a dispatcher is how it gets re-run for free while a
    # director item about it sits unread. Cleared by reopen(), which is the
    # explicit action.
    return [c for c in candidates
            if c["seat"] not in held
            and not c.get("exhausted_at")
            and blocker(root, int(c["id"])) is None]


def claim_next(root: str | os.PathLike[str], seat: str,
               actor: str) -> Optional[dict]:
    """Atomically claim the next READY item for a seat — the worker pickup loop.

    A finished worker used to have exactly one move: exit, and let autodeploy
    pay a fresh agent's whole briefing to start the next item. This is the
    other move: the same session claims the next item and keeps going, with
    its context already paid for.

    Readiness is ``ready()`` — sources a human must touch, placeholder briefs,
    stage-held seats, spent retry budgets and unlanded parents, all of it. A
    pickup loop that could grab an escalation would be an agent dispatching the
    item that exists because agents disagreed. ``next_for`` used to keep a
    SECOND copy of that rule and had already drifted from this one; it
    delegates now, so the read an agent does and the claim it then makes cannot
    disagree about what is available.

    The claim itself is reserve(), so racing the dashboard is safe: whoever
    loses the UPDATE simply tries the next candidate.

    ``actor`` is the claiming EXECUTION (agent:item-<original id>). It is
    stamped on the claimed row, which is what lets the dashboard keep the
    session alive past its original item and requeue claims if the run dies.
    """
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
    if not str(actor or "").strip():
        raise ValueError("claim_next needs the claiming execution's identity")
    for row in ready(root, seat=seat, limit=10):
        if not reserve(root, int(row["id"])):
            continue                      # lost the race — next candidate
        item = set_run_fields(root, int(row["id"]), actor=str(actor).strip())
        activity.log(root, "queue",
                     f"item {item['id']} claimed by {actor}",
                     seat=seat, ref=str(item["id"]))
        return item
    return None


def claimed_open(root: str | os.PathLike[str], actor: str) -> list[dict]:
    """Dispatched items owned by this execution — the claims a run still holds.

    The dashboard's watchdog asks this before closing a finished worker's
    stdin, and _reap asks it to requeue whatever a dead run claimed and never
    settled. Matches on the actor stamp claim_next wrote; an item dispatched
    the ordinary way carries the dispatcher's actor, not the run's, so it
    never appears here.
    """
    return rows(db.connect(root).execute(
        "SELECT * FROM work_item WHERE status = 'dispatched' AND actor = ? "
        "ORDER BY id", (str(actor or ""),)))


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
