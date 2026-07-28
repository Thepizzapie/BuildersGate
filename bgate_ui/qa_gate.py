"""Auto-QA gate — every agent-completed deliverable gets a nit-picky review.

Why this exists: an entire HUD shipped "done" with bare fills, black boxes,
colliding elements, and a character sprite where an icon belonged — because
nothing between the working seat and "done" ever COMPARED the result against
the concept refs. The QA seat persona (bgate_core.seats, "qa") knows how to be
that gate; this module makes it AUTOMATIC: when a maker seat's item transitions
to done, a QA work item is created and dispatched to verify it, and a FAIL
reopens the original with the nitpick list.

Runs as a daemon thread inside the dashboard server (the process that already
owns dispatch). Deliberately NOT in bgate_core: core stays pure data/logic;
spawning agents is a UI-server concern.

Contract:
- Only reviews items completed AFTER the server started (no startup QA-bomb of
  the whole historical queue).
- Maker seats gated: art, gameplay, audio. QA/director items are never gated
  (no recursion); items created by the gate itself (source='qa-gate') are never
  gated either.
- One open QA follow-up per original item at a time; a re-done original (the
  fix round) gets a fresh QA round once the prior one closed.
- BOUNDED. fail -> reopen -> re-dispatch -> fail is a money pump on a subjective
  deliverable, and an uncompromising reviewer will ride it forever. After
  MAX_ROUNDS reviewed rounds (work_item.attempts, which queue.reopen increments,
  is the round counter) the gate stops reviewing that item and files ONE
  escalation for the director — queued, never dispatched, because the whole
  point is that a human now decides whether the thing is good enough.
- Disable with BGATE_QA_GATE=0. Fail-safe: the watcher never raises.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from bgate_core import activity, db, queue as _queue

GATED_SEATS = ("art", "gameplay", "audio", "narrative")
POLL_S = 10.0

# Rounds of QA an item may go through before a human is asked to arbitrate.
# 3 = the original attempt plus two fix rounds; past that the disagreement is
# about taste, not correctness, and another agent will not settle it.
MAX_ROUNDS = 3

ESCALATION_SOURCE = "qa-gate-escalation"

_started = threading.Event()


def _brief_for(item: dict) -> str:
    rounds = int(item.get("attempts") or 0) + 1
    last_round = (
        f"\n\nTHIS IS ROUND {rounds} OF {MAX_ROUNDS} — the last automatic one. "
        "A FAIL here does not buy another agent: the item is escalated to the "
        "director for a human call. So make this verdict count: if it fails, "
        "the nitpick list must be the complete, ranked set, each item naming the "
        "exact problem and the exact fix.\n"
    ) if rounds >= MAX_ROUNDS else f"\n\nRound {rounds} of at most {MAX_ROUNDS}.\n"
    return (
        f"AUTO-QA GATE for work item #{item['id']} ({item['seat']}): "
        f"\"{item['title']}\" — the {item['seat']} seat reports it DONE. "
        "Verify that claim like the picky owner, per your seat workflow "
        "(seat_brief has the full checklist).\n\n"
        f"THE CLAIM (its result note): {(item.get('result') or '(none)')[:1200]}\n\n"
        "Protocol:\n"
        "1. Establish what the item promised (title + brief + result note + its "
        f".bgate/progress/item-{item['id']}.jsonl trail).\n"
        "2. Verify it ACTUALLY happened: for anything visual, render the real "
        "in-game result (godot_screenshot 640x360) and put it SIDE-BY-SIDE with "
        "the pinned concept/character refs — a mock or the seat's own preview "
        "does not count. For gameplay, run the test suite headlessly and check "
        "the change behaves in a real scene. For audio, verify the files load "
        "and are wired where the item claims.\n"
        "3. Verdict:\n"
        "   - PASS: queue_complete THIS item (done) with the evidence "
        "(screenshot paths, test counts, the specific checks that held).\n"
        f"   - FAIL: queue_reopen item_id={item['id']} with your ranked nitpick "
        "list as the reason (it lands in the item's brief for the fix round — "
        "every nitpick names the exact problem AND the fix). Then "
        "queue_complete THIS item (done) with 'VERDICT: FAIL' + the list in "
        "the result. 'Almost' is a FAIL.\n"
        "4. seat_post_note (topic 'qa-gate') with the verdict + evidence paths."
        + last_round
    )


def _open_gate_exists(root: str, ref: str) -> bool:
    row = db.connect(root).execute(
        "SELECT 1 FROM work_item WHERE source = 'qa-gate' AND source_ref = ? "
        "AND status IN ('queued', 'dispatched') LIMIT 1", (ref,)).fetchone()
    return row is not None


def _latest_gate_created(root: str, ref: str) -> str:
    row = db.connect(root).execute(
        "SELECT max(created_at) AS c FROM work_item "
        "WHERE source = 'qa-gate' AND source_ref = ?", (ref,)).fetchone()
    return (row["c"] if row and row["c"] else "")


def _escalation_brief(item: dict, rounds: int) -> str:
    return (
        f"QA LOOP BROKEN — work item #{item['id']} ({item['seat']}): "
        f"\"{item['title']}\" has been through {rounds} QA rounds and the "
        "reviewer is still failing it. The auto-QA gate has STOPPED reviewing "
        "this item; no further agent has been dispatched against it.\n\n"
        f"Its latest result note: {(item.get('result') or '(none)')[:1200]}\n\n"
        "A human decides from here. Read the item's brief (every reopen appended "
        "its nitpick list) and the QA verdicts, then do ONE of:\n"
        "  - accept it as-is (queue_update the item, close this escalation "
        "saying what you accepted and why the remaining nitpicks are fine);\n"
        "  - re-scope it: rewrite the brief so the disagreement is decidable "
        "(name the exact reference and the exact acceptance test), then "
        "queue_reopen it once more;\n"
        "  - cut it (queue_update to cancel/park) — three failed rounds on a "
        "subjective deliverable usually means the ask, not the work, is wrong.\n\n"
        "This item was deliberately NOT dispatched to an agent: spending more "
        "money on the same argument is exactly what the cap exists to stop."
    )


def _escalate(root: str, item: dict, ref: str, rounds: int) -> None:
    """File one escalation per item, ever. Queued, never dispatched."""
    seen = db.connect(root).execute(
        "SELECT 1 FROM work_item WHERE source = ? AND source_ref = ? LIMIT 1",
        (ESCALATION_SOURCE, ref)).fetchone()
    if seen is not None:
        return
    _queue.add(root, "director",
               f"QA loop: #{item['id']} failed {rounds} rounds — {item['title'][:60]}",
               brief=_escalation_brief(item, rounds), priority=9,
               source=ESCALATION_SOURCE, source_ref=ref)
    activity.log(root, "qa-gate",
                 f"item {item['id']} hit the QA round cap ({rounds}) — escalated "
                 "to the director instead of re-dispatching",
                 seat="qa", ref=ref)


def _scan_once(root: str, cutoff_utc: str) -> None:
    from bgate_ui import dispatch as _dispatch
    conn = db.connect(root)
    # Placeholders derived from GATED_SEATS: a hardcoded "?, ?, ?" fell out of
    # sync when narrative was added as a 4th gated seat — the binding mismatch
    # raised on EVERY scan and the fail-safe swallowed it, so the gate silently
    # never reviewed anything.
    marks = ", ".join("?" * len(GATED_SEATS))
    rows = conn.execute(
        f"SELECT * FROM work_item WHERE status = 'done' AND seat IN ({marks}) "
        f"AND source NOT IN ('qa-gate', '{ESCALATION_SOURCE}') "
        "AND updated_at >= ? ORDER BY updated_at",
        (*GATED_SEATS, cutoff_utc)).fetchall()
    for row in rows:
        item = dict(row)
        ref = str(item["id"])
        if _open_gate_exists(root, ref):
            continue
        # attempts counts the reopens; the first pass is attempt 1.
        rounds = int(item.get("attempts") or 0) + 1
        if rounds > MAX_ROUNDS:
            _escalate(root, item, ref, rounds - 1)
            continue
        # A closed gate exists and the original hasn't moved since -> already
        # reviewed this round. (updated_at bumps on the re-done fix round.)
        last = _latest_gate_created(root, ref)
        if last and item["updated_at"] <= last:
            continue
        qa = _queue.add(root, "qa",
                        f"QA gate: verify #{item['id']} — {item['title'][:70]}",
                        brief=_brief_for(item), priority=8,
                        source="qa-gate", source_ref=ref)
        _dispatch.dispatch(root, qa["id"])


def _run(root: str) -> None:
    # Only gate transitions that happen while we're alive — SQLite stores
    # datetime('now') (UTC, second resolution), so compare in the same format.
    cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    while True:
        time.sleep(POLL_S)
        try:
            _scan_once(root, cutoff)
        except Exception:
            pass  # fail-safe: the gate must never take the dashboard down


def start(root: str) -> bool:
    """Idempotently start the watcher for this server process."""
    if os.environ.get("BGATE_QA_GATE", "1").strip() in ("0", "false", "off"):
        return False
    if _started.is_set():
        return True
    _started.set()
    threading.Thread(target=_run, args=(str(root),), daemon=True,
                     name="bgate-qa-gate").start()
    return True
