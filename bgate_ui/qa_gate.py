"""Auto-QA gate — every agent-completed deliverable gets a nit-picky review.

Why this exists: an entire HUD shipped "done" with bare fills, black boxes,
colliding elements, and a character sprite where an icon belonged — because
nothing between the working seat and "done" ever COMPARED the result against
the concept refs. The QA seat persona (bgate_core.seats, "qa") knows how to be
that gate; this module makes it AUTOMATIC: when a maker seat's item transitions
to done, a QA work item is created and dispatched to verify it, and a FAIL
reopens the original with the nitpick list.

THE LOOP MOVED. This module used to own a daemon thread that scanned for
completed items every ten seconds, and the cutoff that thread carried was a real
hole: it reviewed "only transitions after the server started", so every
completion that happened while the dashboard was down was never reviewed and
nothing said so. ``bgate_ui.followup`` now drives the gate from the event bus'
cursor, which is a row id rather than a wall clock — a dashboard that was off for
an hour resumes exactly where it stopped. What is left here is the part that was
always worth having on its own: WHAT a QA round is, when one is owed, and the
brief the reviewer reads.

Deliberately NOT in bgate_core: core stays pure data/logic; spawning agents is a
UI-server concern.

Contract:
- Maker seats gated: art, gameplay, audio, narrative. QA/director items are never
  gated (no recursion); items created by the gate itself (source='qa-gate') are
  never gated either.
- One open QA follow-up per original item at a time; a re-done original (the
  fix round) gets a fresh QA round once the prior one closed.
- BOUNDED. fail -> reopen -> re-dispatch -> fail is a money pump on a subjective
  deliverable, and an uncompromising reviewer will ride it forever. After
  ``qa.max_rounds`` reviewed rounds (work_item.attempts, which queue.reopen
  increments, is the round counter) the gate stops reviewing that item and files
  ONE escalation for the director — queued, never dispatched, because the whole
  point is that a human now decides whether the thing is good enough.
- Disable with BGATE_QA_GATE=0, which is the legacy kill switch and now reads
  through ``bgate_core.settings`` as a coercion of ``gate.mode`` to "none".
"""
from __future__ import annotations

import os

from bgate_core import activity, db, gates as _gates, queue as _queue, \
    settings as _settings

# THE DEFAULT, NOT THE POLICY. This was the whole policy: a studio that wanted
# QA on art alone had to edit this tuple in the harness source, which changed it
# for every project on the machine and needed a dashboard restart to take effect,
# because Python had already cached the module. The live value is the registry's
# ``qa.gated_seats``; this stays as the default and as the fallback for an
# unreadable settings doc, on the same reasoning as MAX_ROUNDS below — a gate
# that cannot read its own configuration keeps the coverage it always had rather
# than silently reviewing nothing.
GATED_SEATS = ("art", "gameplay", "audio", "narrative", "tech", "cinematic")

# Rounds of QA an item may go through before a human is asked to arbitrate.
# 3 = the original attempt plus two fix rounds; past that the disagreement is
# about taste, not correctness, and another agent will not settle it.
#
# The live value is the registry's ``qa.max_rounds``; this stays as the default
# and as the fallback for an unreadable settings doc, because a gate that cannot
# read its cap must keep the cap it always had rather than run uncapped.
MAX_ROUNDS = 3

ESCALATION_SOURCE = "qa-gate-escalation"

# How far back the backstop sweep looks. The sweep exists because
# ``events.emit`` is best-effort — a completion that hit a locked database writes
# no event, so the cursor-driven path never sees it — and it is WINDOWED because
# an unbounded sweep on first run is the startup QA-bomb of the whole historical
# queue that the old cutoff existed to prevent.
SWEEP_WINDOW_MIN = 120


def gated_seats(root: str | os.PathLike[str]) -> tuple:
    """Which maker seats get an automatic reviewer, for THIS project.

    Read per call, not captured at import. director and qa are never in it —
    gating the gate is recursion, not review — and that is enforced here rather
    than trusted to the setting, because a list is a thing a human can typo.
    """
    try:
        chosen = _settings.get(root, "qa.gated_seats")
    except Exception:
        return GATED_SEATS
    seats = tuple(str(s).strip() for s in (chosen or ()) if str(s).strip())
    seats = tuple(s for s in seats if s not in ("qa", "director"))
    return seats or ()


def max_rounds(root: str | os.PathLike[str]) -> int:
    """The round cap for this project. Read per call, not captured at startup.

    A studio that raises the cap because it is watching a specific fight must not
    have to restart the dashboard for it to apply.
    """
    try:
        return max(1, int(_settings.get(root, "qa.max_rounds") or MAX_ROUNDS))
    except Exception:
        return MAX_ROUNDS


def _brief_for(item: dict, cap: int = MAX_ROUNDS) -> str:
    rounds = int(item.get("attempts") or 0) + 1
    last_round = (
        f"\n\nTHIS IS ROUND {rounds} OF {cap} — the last automatic one. "
        "A FAIL here does not buy another agent: the item is escalated to the "
        "director for a human call. So make this verdict count: if it fails, "
        "the nitpick list must be the complete, ranked set, each item naming the "
        "exact problem and the exact fix.\n"
    ) if rounds >= cap else f"\n\nRound {rounds} of at most {cap}.\n"
    return (
        f"AUTO-QA GATE for work item #{item['id']} ({item['seat']}): "
        f"\"{item['title']}\" — the {item['seat']} seat reports it DONE. "
        "Verify that claim like the picky owner.\n\n"
        "THE VERDICT STANDARD IS THE ITEM'S OWN PROMISE — title, brief, "
        "result note — not your seat's whole checklist. The checklist "
        "(seat_brief) is your LENS for how to check what the item touched; "
        "it is not a list of extra demands. Failing this item for something "
        "its brief never asked for does not improve the work, it buys a "
        "bounce round about scope — put genuinely-noticed unrelated problems "
        "in one line of your result instead, for the director to price.\n\n"
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
        "   - PASS: queue_complete THIS item (done) with 'VERDICT: PASS' + the "
        "evidence (screenshot paths, test counts, the specific checks that "
        "held). The marker is not optional on the pass path either: the "
        "dashboard reads only 'VERDICT: PASS'/'VERDICT: FAIL', so a clean "
        "review that omits it is filed as UNKNOWN — decided nothing.\n"
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
    """When the last CONCLUDED review round for this item was filed.

    Concluded means the reviewer actually delivered: status 'done' AND a
    VERDICT marker in the result. It used to be max(created_at) over every
    gate row, which made a DEAD reviewer count as a review — a QA agent that
    crashed (reaped 'failed', or banked 'done' off a bare exit with no
    verdict text) closed the round, the original item's updated_at was then
    <= that timestamp, and the item passed the gate WITHOUT ANYONE EVER
    LOOKING AT IT. Silence is the one verdict a gate must not accept: a
    round with no verdict now simply does not count, so the sweep files a
    fresh reviewer (bounded below — see _scan_once's runaway guard)."""
    row = db.connect(root).execute(
        "SELECT max(created_at) AS c FROM work_item "
        "WHERE source = 'qa-gate' AND source_ref = ? AND status = 'done' "
        "AND (result LIKE '%VERDICT: PASS%' OR result LIKE '%VERDICT: FAIL%')",
        (ref,)).fetchone()
    return (row["c"] if row and row["c"] else "")


def _gate_rows_filed(root: str, ref: str) -> int:
    """Every QA round ever filed for this item, delivered or not."""
    row = db.connect(root).execute(
        "SELECT count(*) AS n FROM work_item "
        "WHERE source = 'qa-gate' AND source_ref = ?", (ref,)).fetchone()
    return int(row["n"] if row else 0)


def escalated(root: str | os.PathLike[str], ref: str) -> bool:
    """Has this item already been escalated to the director, ever?

    The one-per-item rule is what makes the cap a cap: without this an item at
    the round limit files a fresh escalation on every scan, and the director's
    queue fills with the same argument. Exposed because the follow-up router
    checks it before deciding, not only when applying.
    """
    try:
        row = db.connect(root).execute(
            "SELECT 1 FROM work_item WHERE source = ? AND source_ref = ? LIMIT 1",
            (ESCALATION_SOURCE, str(ref))).fetchone()
    except Exception:
        # Unreadable board: claim it IS escalated. The direction that errs
        # towards silence is right here — the other one files duplicates.
        return True
    return row is not None


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
    if escalated(root, ref):
        return
    _queue.add(root, "director",
               f"QA loop: #{item['id']} failed {rounds} rounds — {item['title'][:60]}",
               brief=_escalation_brief(item, rounds), priority=9,
               source=ESCALATION_SOURCE, source_ref=ref)
    activity.log(root, "qa-gate",
                 f"item {item['id']} hit the QA round cap ({rounds}) — escalated "
                 "to the director instead of re-dispatching",
                 seat="qa", ref=ref)


def open_round(root: str | os.PathLike[str], item: dict) -> dict:
    """File one QA round for a completed item. Returns ``{ok, gate, why}``.

    The FILING only — the caller dispatches, because the two callers want
    different things from a refusal (the router records it as an action result,
    the sweep just moves on) and because a function that both creates a row and
    spawns a process cannot be used by either without doing the other.

    Guarded here as well as by the caller: this is the last point before a row
    exists, and delivery of the event that triggered it is at-least-once.
    """
    ref = str(item["id"])
    if _open_gate_exists(root, ref):
        return {"ok": False, "gate": 0,
                "why": "a QA round for this item is already open"}
    cap = max_rounds(root)
    gate = _queue.add(root, "qa",
                      f"QA gate: verify #{item['id']} — {item['title'][:70]}",
                      brief=_brief_for(item, cap), priority=8,
                      source="qa-gate", source_ref=ref)
    return {"ok": True, "gate": int(gate["id"]), "why": ""}


def _scan_once(root: str, cutoff_utc: str) -> None:
    """The backstop sweep: review anything completed since ``cutoff_utc`` that
    has no QA round.

    NOT the normal path any more — ``bgate_ui.followup`` routes completions off
    the event bus, which is what fixed the startup cutoff. This survives as the
    recovery path for a completion whose EVENT was never written: ``events.emit``
    is best-effort by design and returns 0 rather than raising when the database
    is locked, so without a sweep a lost event means an unreviewed deliverable
    with nothing anywhere saying so. Every action it takes runs the same
    idempotency guards as the event path, so the two cannot double-review.
    """
    from bgate_ui import dispatch as _dispatch

    # WHICH GATE IS THIS PROJECT USING. Read every scan, not once at startup:
    # the mode is a live setting and a studio that switches to the builder's gate
    # mid-session must not keep paying for QA agents it stopped wanting. Under
    # 'builders' a finished item never reaches 'done' unreviewed anyway — it sits
    # in 'review' — so this loop would find nothing to do regardless; the early
    # return is what makes that explicit rather than incidental.
    if not _gates.wants_qa_agent(root):
        return
    conn = db.connect(root)
    cap = max_rounds(root)
    # Placeholders derived from GATED_SEATS: a hardcoded "?, ?, ?" fell out of
    # sync when narrative was added as a 4th gated seat — the binding mismatch
    # raised on EVERY scan and the fail-safe swallowed it, so the gate silently
    # never reviewed anything.
    seats = gated_seats(root)
    if not seats:
        return                      # every seat opted out; nothing to review
    marks = ", ".join("?" * len(seats))
    # gate_skip: an item a HUMAN closed by hand. The gate reviews state at
    # close, so reviewing one of those dispatches a reviewer against a result
    # note rather than against work an agent just did — see queue.complete.
    # COALESCE, because a project whose database predates the column still has
    # to be readable by a dashboard that knows about it.
    rows = conn.execute(
        f"SELECT * FROM work_item WHERE status = 'done' AND seat IN ({marks}) "
        f"AND source NOT IN ('qa-gate', '{ESCALATION_SOURCE}') "
        "AND COALESCE(gate_skip, 0) = 0 "
        "AND updated_at >= ? ORDER BY updated_at",
        (*seats, cutoff_utc)).fetchall()
    for row in rows:
        item = dict(row)
        ref = str(item["id"])
        if _open_gate_exists(root, ref):
            continue
        # attempts counts the reopens; the first pass is attempt 1.
        rounds = int(item.get("attempts") or 0) + 1
        if rounds > cap:
            _escalate(root, item, ref, rounds - 1)
            continue
        # RUNAWAY GUARD for the no-verdict re-review path. A round whose
        # reviewer died no longer counts as a review (_latest_gate_created),
        # which means this sweep will file another - and a reviewer that dies
        # EVERY time (broken CLI, poisoned brief) must not buy an agent per
        # sweep forever. Twice the round cap in total filings is generous for
        # honest crashes and cheap as a ceiling; past it, a human decides.
        if _gate_rows_filed(root, ref) >= cap * 2:
            _escalate(root, item, ref, rounds)
            continue
        # A closed gate exists and the original hasn't moved since -> already
        # reviewed this round. (updated_at bumps on the re-done fix round.)
        last = _latest_gate_created(root, ref)
        if last and item["updated_at"] <= last:
            continue
        opened = open_round(root, item)
        if opened.get("ok"):
            _dispatch.dispatch(root, int(opened["gate"]))


def sweep(root: str | os.PathLike[str], minutes: int = SWEEP_WINDOW_MIN) -> None:
    """Run the backstop over the last ``minutes`` of completions.

    Windowed, because the failure it recovers from is recent by definition (a
    locked database during one completion) while an unbounded version would
    review the entire history of a project the first time it ran — which is the
    startup QA-bomb the old thread's cutoff existed to prevent. Never raises: the
    router calls this from inside its tick.
    """
    try:
        row = db.connect(root).execute(
            "SELECT datetime('now', ?) AS c", (f"-{int(minutes)} minutes",)
        ).fetchone()
        cutoff = str(row["c"]) if row else ""
        if not cutoff:
            return
        _scan_once(str(root), cutoff)
    except Exception:
        pass
