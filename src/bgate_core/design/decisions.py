"""The decision register, and the list of things this project is NOT building.

WHY THIS MODULE EXISTS AT ALL. The director seat's mission, shipped in every
brief this product has ever printed, contains two sentences that had nowhere to
land:

    "Every settled decision names its acceptance test and what it deliberately
     leaves dark."
    "Say plainly what the project is not building, because an unsaid no gets
     built anyway."

Neither was recordable. The seat's central panel read both lists out of the
generic per-seat document store (``/api/workspace/director/<key>``) — a JSON
blob with no schema, no state, no author and no timestamp, which nothing can
append one row to without rewriting the whole document. So the panel was empty
on every project that has ever existed, and the two most expensive facts a
project owns lived in chat scrollback.

THE TWO LISTS ARE DIFFERENT SHAPES AND THAT IS THE DESIGN.

    decision      a call that was MADE. It has an acceptance test (how anyone
                  checks the call was honoured) and a left-dark (what the call
                  deliberately does not cover). Both are mandatory.
    not_building  a call that was REFUSED. There is nothing to verify, which is
                  the point of refusing it, so it has a reason instead of a
                  test.

Folding the second into the first as ``state='refused'`` would leave two
required columns permanently blank on half the rows, and a reader could no
longer tell "settled, but nobody named a test" — a defect — from "refused, no
test applies" — correct.

WHY BLANK IS AN ERROR AND NOT A DEFAULT. ``add`` refuses an empty acceptance
test or an empty left-dark. That is the whole feature: a decision with no test
is an opinion, and a deferral nobody labelled gets "fixed" as a bug by the next
agent that finds it. Letting either through as '' would produce a register that
is technically full and answers nothing, which is worse than an empty one
because it looks answered. The caller gets a ValueError naming which field, at
the moment of writing, rather than a discovery six weeks later.

PROPOSE VERSUS SETTLE. ``state`` is 'open' for a proposal and 'settled' for a
ruling. This module does not police who may do which — identity lives one layer
up, in ``bgate_ui.api.require_human`` and the MCP server's ``_caller_is_agent``,
which are the two places that know who is calling. What this module guarantees
is that the distinction is STORABLE, so those layers have something to enforce
against, and that ``actor`` records who did it either way.

NOTHING ELSE MAY TOUCH THESE TABLES. Every read and write in the product goes
through this file — no SQL against ``decision`` or ``not_building`` anywhere
else — so the mandatory-fields rule cannot be routed around by a caller that
would rather not fill them in.
"""
from __future__ import annotations

import os
from typing import Optional

from ..board import activity
from ..store import db
from ..store.util import rows

# 'settled' is a ruling; 'open' is a proposal awaiting one; 'superseded' is a
# ruling that was replaced. There is deliberately no 'rejected' — a proposal
# that is turned down is a refusal, and refusals live in not_building where
# somebody can read them before rebuilding the thing.
STATES = ("settled", "open", "superseded")

# Bounds, not opinions. A register is read at a glance in a rail 340px wide, and
# an agent that pastes a design document into `title` makes the panel unusable
# for every other row. Truncation is silent on purpose: refusing a too-long
# title would mean a decision that does not get recorded, which is the failure
# this module exists to end.
MAX_TITLE = 200
MAX_TEXT = 2000
MAX_TAG = 40


def _actor(given: str = "") -> str:
    """Who is responsible for this row.

    Same identity the activity ledger and the approval gate use, so "who
    settled this" and "who approved that" are the same kind of string and can be
    compared. An explicit argument wins for the HTTP layer, which knows the
    caller better than the environment does.
    """
    return (str(given).strip() or activity.current_actor())[:120]


def _required(value, field: str) -> str:
    """A mandatory prose field, or a ValueError that names it.

    The error text is written for the agent that will read it: it says what the
    field is FOR, because "acceptance is required" tells a caller to type
    something and this tells it what to type.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError({
            "title": "a decision needs a title — the call itself, in one line",
            "acceptance": (
                "a decision needs an acceptance test — how anyone checks the "
                "call was honoured. Without one it is an opinion, not a "
                "decision, and nobody can tell later whether it held"),
            "leaves_dark": (
                "a decision needs what it deliberately leaves dark — the part "
                "it does NOT cover. A deferral nobody labelled gets 'fixed' as "
                "a bug by the next agent that finds it"),
            "text": "a refusal needs to name the thing being refused",
            "reason": (
                "a refusal needs a reason — an unexplained no is re-proposed "
                "every few weeks by somebody who cannot see why it was a no"),
        }.get(field, f"{field} is required"))
    return text[:MAX_TEXT]


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def add(root: str | os.PathLike[str], title: str, acceptance: str,
        leaves_dark: str, *, state: str = "settled",
        work_item_id: Optional[int] = None, session_id: Optional[int] = None,
        actor: str = "") -> dict:
    """File a decision. Title, acceptance test and left-dark are all mandatory.

    ``state='open'`` files it as a PROPOSAL — the shape an agent is allowed to
    write. It still has to name a test and a left-dark, because a proposal
    without them cannot be ruled on: the human would have to invent both before
    settling it, which is the work the proposal was supposed to have done.
    """
    state = str(state or "settled").strip().lower()
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    title = _required(title, "title")[:MAX_TITLE]
    acceptance = _required(acceptance, "acceptance")
    leaves_dark = _required(leaves_dark, "leaves_dark")
    who = _actor(actor)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO decision (title, acceptance, leaves_dark, state, "
            "actor, work_item_id, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, acceptance, leaves_dark, state, who,
             _int_or_none(work_item_id), _int_or_none(session_id)),
        )
        did = int(cur.lastrowid)
    activity.log(root, "decision", f"{state} {title[:80]!r}", ref=f"decision:{did}")
    return get(root, did)


def settle(root: str | os.PathLike[str], decision_id: int, *,
           actor: str = "") -> dict:
    """Turn a proposal into a ruling.

    Re-stamps ``actor``: the person who settles a decision is the one
    accountable for it, not whichever agent happened to draft the proposal. The
    draft's authorship is not lost — the activity ledger holds the 'open' entry
    with its own actor on it.
    """
    row = get(root, decision_id)
    if row["state"] == "settled":
        return row
    if row["state"] == "superseded":
        raise ValueError(
            f"decision {decision_id} was superseded by "
            f"{row.get('superseded_by') or 'another decision'}; settle that one "
            "or file a new decision rather than reviving this")
    who = _actor(actor)
    with db.tx(root) as conn:
        conn.execute("UPDATE decision SET state = 'settled', actor = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (who, decision_id))
    activity.log(root, "decision", f"settled {row['title'][:80]!r}",
                 ref=f"decision:{decision_id}")
    return get(root, decision_id)


def supersede(root: str | os.PathLike[str], decision_id: int, by_id: int, *,
              actor: str = "") -> dict:
    """Mark a decision replaced by another, keeping both.

    NOT A DELETE, and that is the whole reason the verb exists. "We decided X,
    then we decided Y instead" is the most useful pair of rows a register holds:
    without the first one, the next person to propose X has no way to learn it
    was already tried. Deleting the loser turns the register into a snapshot of
    current opinion, which the bible already is.
    """
    row = get(root, decision_id)
    replacement = get(root, by_id)          # raises if it does not exist
    if int(by_id) == int(decision_id):
        raise ValueError("a decision cannot supersede itself")
    with db.tx(root) as conn:
        conn.execute("UPDATE decision SET state = 'superseded', "
                     "superseded_by = ?, updated_at = datetime('now') "
                     "WHERE id = ?", (int(by_id), int(decision_id)))
    activity.log(root, "decision",
                 f"{row['title'][:60]!r} superseded by "
                 f"{replacement['title'][:60]!r}",
                 ref=f"decision:{decision_id}", actor=_actor(actor))
    return get(root, decision_id)


def get(root: str | os.PathLike[str], decision_id: int) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM decision WHERE id = ?",
                       (int(decision_id),)).fetchone()
    if row is None:
        raise LookupError(f"no decision {decision_id}")
    return dict(row)


def list_decisions(root: str | os.PathLike[str], state: str = "",
                   work_item_id: Optional[int] = None) -> list[dict]:
    """The register, newest first.

    NEWEST FIRST, unlike the bible, which is ranked into a reading order. This
    is a log of rulings and the question a reader arrives with is "what has been
    decided lately"; a chronological document would put the founding decisions
    at the top of a panel forever and bury this week's.
    """
    state = str(state or "").strip().lower()
    if state and state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    sql = "SELECT * FROM decision"
    where, params = [], []
    if state:
        where.append("state = ?")
        params.append(state)
    if work_item_id is not None:
        where.append("work_item_id = ?")
        params.append(int(work_item_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return rows(db.connect(root).execute(sql, params))


# ---------------------------------------------------------------------------
# The no-list
# ---------------------------------------------------------------------------

def refuse(root: str | os.PathLike[str], text: str, reason: str, *,
           tag: str = "", decision_id: Optional[int] = None,
           actor: str = "") -> dict:
    """Write down something this project is not building.

    ``reason`` is mandatory for the same reason the acceptance test is: an
    unexplained no is re-proposed every few weeks by somebody who cannot see
    what was wrong with it, and each re-proposal costs the argument again.

    ``tag`` is free-form and optional — 'scope', 'engine', 'v2', whatever this
    project groups its refusals by. No CHECK constraint and no vocabulary in
    code, because a fixed list of reasons-to-refuse is a list somebody has to
    extend before they can record the refusal, and at that moment they will
    simply not record it.
    """
    text = _required(text, "text")
    reason = _required(reason, "reason")
    who = _actor(actor)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO not_building (text, reason, tag, actor, decision_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, reason, str(tag or "").strip()[:MAX_TAG], who,
             _int_or_none(decision_id)),
        )
        nid = int(cur.lastrowid)
    activity.log(root, "decision", f"not building {text[:80]!r}",
                 ref=f"not_building:{nid}")
    return get_refusal(root, nid)


def unrefuse(root: str | os.PathLike[str], not_building_id: int, *,
             actor: str = "") -> dict:
    """Take a line off the no-list, because the answer changed.

    A real delete, unlike :func:`supersede`. The asymmetry is deliberate: a
    superseded DECISION still explains why the project is where it is, but a
    refusal that no longer holds is actively harmful — every agent that reads
    the list treats it as binding, so a stale no silently prevents work that is
    now wanted. The activity ledger keeps the record that it existed and who
    lifted it.
    """
    row = get_refusal(root, not_building_id)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM not_building WHERE id = ?",
                     (int(not_building_id),))
    activity.log(root, "decision", f"no longer refusing {row['text'][:80]!r}",
                 ref=f"not_building:{not_building_id}", actor=_actor(actor))
    return {"deleted": row}


def get_refusal(root: str | os.PathLike[str], not_building_id: int) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM not_building WHERE id = ?",
                       (int(not_building_id),)).fetchone()
    if row is None:
        raise LookupError(f"no not_building entry {not_building_id}")
    return dict(row)


def list_not_building(root: str | os.PathLike[str], tag: str = "") -> list[dict]:
    """The no-list, newest first. Read this BEFORE building anything."""
    sql = "SELECT * FROM not_building"
    params: list = []
    if str(tag or "").strip():
        sql += " WHERE tag = ?"
        params.append(str(tag).strip()[:MAX_TAG])
    sql += " ORDER BY id DESC"
    return rows(db.connect(root).execute(sql, params))


# ---------------------------------------------------------------------------
# The pair, read together
# ---------------------------------------------------------------------------

def overview(root: str | os.PathLike[str]) -> dict:
    """Both lists plus the open count, for the one panel that shows all three.

    ONE CALL BECAUSE THE PANEL IS ONE PANEL. The director workspace draws the
    settled register, the no-rail and the awaiting-a-ruling count in a single
    layout; three endpoints would mean three round trips and three separate
    loading states for one screen that is meaningless in pieces.
    """
    everything = list_decisions(root)
    return {
        "decisions": [d for d in everything if d["state"] == "settled"],
        "open": [d for d in everything if d["state"] == "open"],
        "superseded": [d for d in everything if d["state"] == "superseded"],
        "not_building": list_not_building(root),
    }


def _int_or_none(value) -> Optional[int]:
    """A link, or nothing. 0 is nothing too — it is what an empty number field
    posts, and a foreign key onto work item 0 is a row that will never resolve."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n or None
