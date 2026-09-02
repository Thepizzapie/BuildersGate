"""Quests — the third noun in the narrative seat's mission.

WHY THIS MODULE EXISTS. The seat's brief has always read "own the lore graph,
quests, and dialogue". Two of those three had a home: entities and facts in
lore.py, trees in dialogue.py. Quests had none, so the seat's Quests tab drew a
sentence explaining that quests are not modelled, on every project, forever.

WHAT A QUEST IS HERE. A title, who hands it out, a premise, and an ORDERED list
of steps. What makes it a quest rather than a note is that it can be finished,
and that is a property of the steps: each one names the observable that closes
it.

    quest.title      the thing the player is asked to do
    quest.giver      a lore entity, so the quest is part of the graph and not a
                     document beside it
    step.text        what the player does
    step.done_when   how anything — the engine, a playtest, a reader — knows
                     that step is finished
    step.optional    a step that does not gate completion

``done_when`` IS MANDATORY AND THAT IS THE FEATURE. It is the same column as
``decision.acceptance``: a step reading "talk to the accounting wizard" cannot
be finished by anybody, because nothing says what counts as having talked to
them. Writing it as a required field means the refusal lands at the moment of
writing, with a sentence saying what to type, instead of surfacing when somebody
tries to implement the quest and finds there is nothing to implement.

THE THREE SHAPES A QUEST FAILS IN, which :func:`validate` names, deliberately
mirroring dialogue.py's three:

    no steps            nothing to do. A premise is not a quest.
    all optional        no step gates completion, so the quest can never be
                        finished — the quest-shaped version of dialogue's node
                        with no way out.
    broken order        ``ord`` values that skip or repeat, so "step 3" means
                        different things to the panel and to whoever implements
                        it.

A fourth, the dangling giver, is prevented rather than reported: the foreign key
means a giver either resolves to an entity or is null.

VALIDATE REPORTS, WRITE REFUSES. Same split as dialogue: :func:`validate` is
data, so a panel can draw a broken quest and say what is wrong with it, and the
write path raises. A module that could only raise would make the broken rows
unviewable, which is the state they most need to be viewed in.

NOTHING ELSE TOUCHES THESE TABLES. Every read and write goes through this file,
so the mandatory-``done_when`` rule cannot be routed around.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

from ..board import activity
from ..store import db
from . import lore
from ..store.util import rows, slugify

# 'draft' is being written, 'active' is in the game, 'done' is finished content,
# 'cut' is kept-but-not-shipped. There is no 'deleted': a quest that was cut is
# the most useful row for the next person who proposes it, exactly as with a
# superseded decision.
STATES = ("draft", "active", "done", "cut")

MAX_TITLE = 200
MAX_TEXT = 2000


def _actor(given: str = "") -> str:
    return (str(given).strip() or activity.current_actor())[:120]


def _required(value, field: str) -> str:
    """A mandatory prose field, or a ValueError that says what to type."""
    text = str(value or "").strip()
    if not text:
        raise ValueError({
            "title": "a quest needs a title — what the player is asked to do",
            "text": "a step needs to say what the player does",
            "done_when": (
                "a step needs a done_when — the observable that closes it. "
                "Without one nothing can finish the step: not the engine, "
                "which has nothing to test, and not the player, who cannot "
                "tell what counted. 'the wizard's form is in the player's "
                "inventory', not 'talk to the wizard'"),
        }.get(field, f"{field} is required"))
    return text[:MAX_TEXT]


def _int_or_none(value) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n or None


def _giver_id(root, giver) -> Optional[int]:
    """Resolve a giver to an entity id, or refuse by name.

    A quest handed out by nobody is fine — a notice board, a letter, the world
    itself. A quest handed out by a name that is not in the lore graph is not:
    it is either a typo or a character somebody forgot to write down, and both
    are worth stopping for, because the whole point of the giver column is that
    the quest hangs off the graph.
    """
    if giver in (None, "", 0):
        return None
    try:
        return int(lore.get_entity(root, giver)["id"])
    except LookupError:
        raise ValueError(
            f"no lore entity {giver!r} to give this quest — lore_add writes "
            "one, or leave the giver empty if the quest comes from the world "
            "rather than from somebody"
        ) from None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def add(root: str | os.PathLike[str], title: str, *,
        steps: Iterable[dict] | None = None, premise: str = "",
        reward: str = "", giver: str | int | None = None,
        state: str = "draft", actor: str = "") -> dict:
    """Write a quest and its steps in one transaction.

    ONE CALL, NOT A CREATE-THEN-APPEND. A quest with no steps is one of the
    three things :func:`validate` refuses on, so a two-call API would make the
    invalid state the NORMAL first state of every quest, and any reader between
    the two calls would see a broken row. Steps may still be added later; they
    just cannot be the only way to get a usable quest.

    ``steps`` is a list of ``{text, done_when, optional?}``. Order is the order
    given — ``ord`` is assigned here rather than taken from the caller, because
    a caller that numbers its own steps is a caller that can produce gaps.
    """
    state = str(state or "draft").strip().lower()
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    title = _required(title, "title")[:MAX_TITLE]
    gid = _giver_id(root, giver)
    clean = [_clean_step(s) for s in (steps or [])]
    slug = slugify(title)
    who = _actor(actor)

    with db.tx(root) as conn:
        if conn.execute("SELECT id FROM quest WHERE slug = ?", (slug,)).fetchone():
            raise ValueError(
                f"quest {slug!r} already exists — update it, or give this one a "
                "title of its own")
        cur = conn.execute(
            "INSERT INTO quest (slug, title, premise, reward, state, giver_id, "
            "actor) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, title, str(premise or "").strip()[:MAX_TEXT],
             str(reward or "").strip()[:MAX_TEXT], state, gid, who),
        )
        qid = int(cur.lastrowid)
        for i, step in enumerate(clean):
            conn.execute(
                "INSERT INTO quest_step (quest_id, ord, text, done_when, "
                "optional) VALUES (?, ?, ?, ?, ?)",
                (qid, i, step["text"], step["done_when"], step["optional"]),
            )
    activity.log(root, "narrative", f"quest {title[:80]!r}", ref=f"quest:{qid}")
    return get(root, qid)


def _clean_step(step: dict) -> dict:
    return {
        "text": _required((step or {}).get("text"), "text"),
        "done_when": _required((step or {}).get("done_when"), "done_when"),
        "optional": 1 if (step or {}).get("optional") else 0,
    }


def add_step(root: str | os.PathLike[str], ref: str | int, text: str,
             done_when: str, *, optional: bool = False,
             actor: str = "") -> dict:
    """Append one step. ``ord`` continues the existing sequence."""
    quest = get(root, ref)
    step = _clean_step({"text": text, "done_when": done_when,
                        "optional": optional})
    with db.tx(root) as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) + 1 AS n FROM quest_step "
            "WHERE quest_id = ?", (quest["id"],)).fetchone()["n"]
        conn.execute(
            "INSERT INTO quest_step (quest_id, ord, text, done_when, optional) "
            "VALUES (?, ?, ?, ?, ?)",
            (quest["id"], int(nxt), step["text"], step["done_when"],
             step["optional"]),
        )
        conn.execute("UPDATE quest SET updated_at = datetime('now') WHERE id = ?",
                     (quest["id"],))
    activity.log(root, "narrative", f"step on {quest['title'][:60]!r}",
                 ref=f"quest:{quest['id']}", actor=_actor(actor))
    return get(root, quest["id"])


def cut_step(root: str | os.PathLike[str], step_id: int, *,
             actor: str = "") -> dict:
    """Remove a step and CLOSE THE GAP IT LEAVES.

    Renumbering matters: `ord` is what "step 3" means, and a sequence with a
    hole in it makes the panel, the agent and whoever implements the quest
    disagree about which step that is. Deleting without renumbering is how the
    broken-order problem gets created by the tool that is supposed to prevent
    it.
    """
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM quest_step WHERE id = ?",
                       (int(step_id),)).fetchone()
    if row is None:
        raise LookupError(f"no quest step {step_id}")
    qid = int(row["quest_id"])
    with db.tx(root) as conn:
        conn.execute("DELETE FROM quest_step WHERE id = ?", (int(step_id),))
        # Rewritten in ord order so the UNIQUE (quest_id, ord) index never sees
        # two rows claiming the same slot mid-update.
        remaining = rows(conn.execute(
            "SELECT id FROM quest_step WHERE quest_id = ? ORDER BY ord", (qid,)))
        for i, step in enumerate(remaining):
            conn.execute("UPDATE quest_step SET ord = ? WHERE id = ?",
                         (i, step["id"]))
        conn.execute("UPDATE quest SET updated_at = datetime('now') WHERE id = ?",
                     (qid,))
    activity.log(root, "narrative", "cut a quest step", ref=f"quest:{qid}",
                 actor=_actor(actor))
    return get(root, qid)


def update(root: str | os.PathLike[str], ref: str | int, *,
           premise: str | None = None, reward: str | None = None,
           state: str | None = None, giver: str | int | None = None,
           actor: str = "") -> dict:
    """Change the quest's own fields. Steps have their own verbs."""
    quest = get(root, ref)
    if state is not None:
        state = str(state).strip().lower()
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}, got {state!r}")
    gid = quest["giver_id"] if giver is None else _giver_id(root, giver)
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE quest SET premise = ?, reward = ?, state = ?, giver_id = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (quest["premise"] if premise is None else str(premise).strip()[:MAX_TEXT],
             quest["reward"] if reward is None else str(reward).strip()[:MAX_TEXT],
             quest["state"] if state is None else state,
             gid, quest["id"]),
        )
    activity.log(root, "narrative", f"quest {quest['title'][:60]!r} updated",
                 ref=f"quest:{quest['id']}", actor=_actor(actor))
    return get(root, quest["id"])


def delete(root: str | os.PathLike[str], ref: str | int, *,
           actor: str = "") -> dict:
    """Delete a quest and its steps.

    A real delete, and the reason it is offered at all is that `state='cut'`
    covers the useful case — "we are not shipping this, and here is what it
    was". Delete is for the row that was a mistake to write, not for content
    that was decided against.
    """
    quest = get(root, ref)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM quest WHERE id = ?", (quest["id"],))
    activity.log(root, "narrative", f"deleted quest {quest['title'][:60]!r}",
                 ref=f"quest:{quest['id']}", actor=_actor(actor))
    return {"deleted": quest}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get(root: str | os.PathLike[str], ref: str | int) -> dict:
    """One quest, whole: its fields, its giver resolved, and its steps."""
    conn = db.connect(root)
    if isinstance(ref, int):
        row = conn.execute("SELECT * FROM quest WHERE id = ?", (ref,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM quest WHERE slug = ? OR title = ?",
                           (ref, ref)).fetchone()
    if row is None:
        raise LookupError(f"no quest {ref!r}")
    quest = dict(row)
    quest["steps"] = rows(conn.execute(
        "SELECT * FROM quest_step WHERE quest_id = ? ORDER BY ord",
        (quest["id"],)))
    quest["giver"] = None
    if quest["giver_id"]:
        giver = conn.execute(
            "SELECT slug, name, kind, status FROM lore_entity WHERE id = ?",
            (quest["giver_id"],)).fetchone()
        quest["giver"] = dict(giver) if giver else None
    quest.update(validate(quest))
    return quest


def list_quests(root: str | os.PathLike[str], state: str = "") -> list[dict]:
    """Every quest, with its step count and its verdict.

    THE VERDICT IS IN THE LISTING because the listing is where a broken quest is
    cheapest to notice — the same reason dialogue's listing carries `ok`. A rail
    that shows eight titles and makes you open each one to find the two that do
    not hold together is a rail that gets read once.
    """
    state = str(state or "").strip().lower()
    if state and state not in STATES:
        raise ValueError(f"state must be one of {STATES}, got {state!r}")
    conn = db.connect(root)
    sql = ("SELECT q.*, e.slug AS giver_slug, e.name AS giver_name, "
           "e.kind AS giver_kind, e.status AS giver_status "
           "FROM quest q LEFT JOIN lore_entity e ON e.id = q.giver_id")
    params: list = []
    if state:
        sql += " WHERE q.state = ?"
        params.append(state)
    out = []
    for quest in rows(conn.execute(sql + " ORDER BY q.id DESC", params)):
        quest["steps"] = rows(conn.execute(
            "SELECT * FROM quest_step WHERE quest_id = ? ORDER BY ord",
            (quest["id"],)))
        # THE SAME `giver` SHAPE `get` RETURNS. A listing and a single read that
        # disagree about the name of a field is a reader that works on one and
        # silently renders nothing on the other — which is precisely what
        # happened: the quests panel reads the listing, found no `giver`, and
        # drew every quest as coming from nobody while the rows had a giver.
        quest["giver"] = {
            "slug": quest["giver_slug"], "name": quest["giver_name"],
            "kind": quest["giver_kind"], "status": quest["giver_status"],
        } if quest.get("giver_slug") else None
        quest.update(validate(quest))
        out.append(quest)
    return out


def validate(quest: dict) -> dict:
    """The three shapes a quest fails in, as data.

    Returns ``{ok, problems}`` where each problem names the step it is about, in
    the same style dialogue.py's refusals do — "step 3: ..." is actionable and
    "invalid quest" is not.

    THIS IS NOT A WRITE GATE. ``done_when`` being present is enforced by the
    writers; what is left are the properties that only exist across the whole
    step list, which is exactly the set a panel needs to draw and a caller
    cannot check field by field.
    """
    steps = quest.get("steps") or []
    problems: list[dict] = []

    if not steps:
        problems.append({
            "kind": "no-steps",
            "step": None,
            "text": "this quest has no steps — a premise is not something the "
                    "player can do, and nothing can finish it",
        })
    elif all(int(s.get("optional") or 0) for s in steps):
        problems.append({
            "kind": "all-optional",
            "step": None,
            "text": "every step is optional, so nothing gates completion — this "
                    "quest can never be finished",
        })

    for i, step in enumerate(steps):
        if int(step.get("ord", i)) != i:
            problems.append({
                "kind": "broken-order",
                "step": step.get("ord"),
                "text": f"step {step.get('ord')} sits in position {i} — the "
                        "numbering skips or repeats, so 'step N' means two "
                        "different things",
            })

    return {"ok": not problems, "problems": problems}


def brief(root: str | os.PathLike[str], state: str = "") -> dict:
    """Every quest plus the givers available, for the one panel that shows both.

    ONE CALL for the same reason decisions.overview is: the panel draws the
    list, the detail and the giver picker together and is meaningless in pieces.
    """
    return {
        "quests": list_quests(root, state),
        "givers": [
            {"slug": e["slug"], "name": e["name"], "kind": e["kind"],
             "status": e["status"]}
            for e in lore.list_entities(root)
            if e["kind"] in ("character", "faction")
        ],
        "states": list(STATES),
    }
