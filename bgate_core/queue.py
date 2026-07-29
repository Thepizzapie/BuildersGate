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
STATUSES = ("queued", "dispatched", "done", "failed", "cancelled")


def add(root: str | os.PathLike[str], seat: str, title: str, brief: str = "",
        priority: int = 0, source: str = "manual", source_ref: str = "",
        scope_tier_id: Optional[int] = None) -> dict:
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
    if not title.strip():
        raise ValueError("a work item needs a title")
    # The cut line only means something if work cannot be filed under it.
    # OutOfScope subclasses ValueError, so every caller that already maps
    # ValueError -> 400 reports this correctly without knowing about scope.
    _scope.enforce(root, scope_tier_id)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO work_item (seat, title, brief, priority, source, "
            "source_ref, scope_tier_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seat, title.strip(), brief, priority, source, source_ref,
             scope_tier_id),
        )
        item_id = int(cur.lastrowid)
    activity.log(root, "queue", f"queued for {seat}: {title.strip()[:80]}",
                 ref=str(item_id))
    return get(root, item_id)


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
    # not a result anyone is waiting on, so it sinks below done/failed.
    sql += " ORDER BY CASE status WHEN 'queued' THEN 0 WHEN 'dispatched' THEN 1 "
    sql += "WHEN 'cancelled' THEN 3 ELSE 2 END, priority DESC, id"
    return rows(conn.execute(sql, params))


def _notify(root: str | os.PathLike[str], item: dict) -> None:
    """Append a status-transition event to .bgate/notify.jsonl (best-effort).

    The durable completion signal: dispatched agents flip their item via
    queue_complete, the watcher/reap paths flip it on death — ALL of it lands
    here, so an orchestrator (or the UI) can tail/long-poll one file instead of
    sleep-polling the queue. Never raises — losing a ping must not break the
    status change itself.
    """
    try:
        import json as _json
        from datetime import datetime, timezone
        path = os.path.join(str(root), ".bgate", "notify.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "item_id": item["id"], "status": item["status"],
                "seat": item["seat"], "title": item["title"][:120],
            }) + "\n")
    except Exception:
        pass


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
            (status, result[:2000], item_id),
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
            {"status": status, "result": result[:2000], "seat": item["seat"]})
    return item


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
    stamp = ("\n\n--- REOPENED (attempt %d) ---\n" % (item["attempts"] + 2)) + reason
    update(root, item_id, brief=(item["brief"] or "") + stamp[:3000])
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
    """The highest-priority queued item for a seat — what an agent works next."""
    row = db.connect(root).execute(
        "SELECT * FROM work_item WHERE status = 'queued' AND seat = ? "
        "ORDER BY priority DESC, id LIMIT 1", (seat,)).fetchone()
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
