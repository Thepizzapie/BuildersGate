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

from . import activity, db, iterations, seats as _seats
from .util import rows

STATUSES = ("queued", "dispatched", "done", "failed")


def add(root: str | os.PathLike[str], seat: str, title: str, brief: str = "",
        priority: int = 0, source: str = "manual", source_ref: str = "") -> dict:
    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}; seats are {tuple(_seats.DEFAULT_SEATS)}")
    if not title.strip():
        raise ValueError("a work item needs a title")
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO work_item (seat, title, brief, priority, source, source_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seat, title.strip(), brief, priority, source, source_ref),
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
           seat: Optional[str] = None, priority: Optional[int] = None) -> dict:
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
    sql += " ORDER BY CASE status WHEN 'queued' THEN 0 WHEN 'dispatched' THEN 1 "
    sql += "ELSE 2 END, priority DESC, id"
    return rows(conn.execute(sql, params))


def set_status(root: str | os.PathLike[str], item_id: int, status: str,
               result: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    get(root, item_id)
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE work_item SET status = ?, result = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, result[:2000], item_id),
        )
    item = get(root, item_id)
    activity.log(root, "queue", f"item {item_id} -> {status}: {item['title'][:60]}",
                 seat=item["seat"], ref=str(item_id))
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
