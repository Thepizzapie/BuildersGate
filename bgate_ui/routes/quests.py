"""Quests, over HTTP.

WHY THIS ONE WRITES AND routes/dialogue.py DOES NOT. That file refuses HTTP
writes on the grounds that ``dialogue_write`` is where canon_check runs, and an
HTTP write would be a second, quieter path to the same files with none of that
around it. The reasoning is right and the conclusion does not have to be
read-only: what matters is that the gate runs, not which door the caller used.

So the write path here RUNS canon_check ITSELF, on the quest's own prose, before
anything lands — and hands the flags back with the row. A conflict is refused
with the flag that caused it. A review-level flag is written and reported, which
is the same latitude ``canon_check`` gives an agent, because "the wizard is still
draft" is information and not an error.

The difference from dialogue is also material: a dialogue tree is a FILE in the
Godot project's lane, so writing one is a lane act that belongs to the seat that
holds the lane. A quest is a row in the project database, like a decision — and
/api/decisions has always accepted writes for exactly that reason.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from bgate_core import canon, quests as _quests
from fastapi import APIRouter, Body

from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _canon_text(title: str, premise: str, steps: list[dict]) -> str:
    """The prose of a quest, for canon to read.

    `done_when` is included deliberately: it is where the concrete nouns live
    ("the wizard's form is in the player's inventory"), so a completion
    condition that names a retired character is exactly the contradiction this
    check exists to catch, and it would be invisible if only the premise were
    checked.
    """
    parts = [title, premise]
    for step in steps or []:
        parts.append(str(step.get("text") or ""))
        parts.append(str(step.get("done_when") or ""))
    return "\n".join(p for p in parts if p)


@router.get("/api/quests")
def quests_index(state: str = "") -> dict:
    """Every quest with its steps and its verdict, plus the possible givers."""
    try:
        return api.ok(_quests.brief(root(), state))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.get("/api/quests/{ref}")
def quest_read(ref: str) -> dict:
    try:
        return api.ok(_quests.get(root(), ref))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.post("/api/quests")
def quest_add(body: dict = Body(...)) -> dict:
    """Write a quest, its steps, and the canon verdict on all of it.

    A `conflict` REFUSES. That is the one level canon_check calls hard — a
    retired entity appearing in new content, or a statement that contradicts a
    locked fact — and writing it anyway would put the contradiction in the
    database where the next agent reads it as settled.
    """
    steps = body.get("steps") or []
    verdict = canon.check(
        root(),
        _canon_text(str(body.get("title") or ""), str(body.get("premise") or ""),
                    steps),
    )
    if verdict["verdict"] == "conflict":
        raise api.bad_request(
            "canon_check refuses this quest — "
            + "; ".join(f["message"] for f in verdict["flags"]
                        if f["level"] == "conflict")
        )
    try:
        quest = _quests.add(
            root(), str(body.get("title") or ""), steps=steps,
            premise=str(body.get("premise") or ""),
            reward=str(body.get("reward") or ""),
            giver=body.get("giver") or None,
            state=str(body.get("state") or "draft"),
        )
    except ValueError as exc:
        raise api.bad_request(str(exc))
    return api.ok({"quest": quest, "canon": verdict})


@router.post("/api/quests/{ref}/steps")
def quest_add_step(ref: str, body: dict = Body(...)) -> dict:
    verdict = canon.check(
        root(),
        _canon_text("", "", [{"text": body.get("text"),
                              "done_when": body.get("done_when")}]),
    )
    if verdict["verdict"] == "conflict":
        raise api.bad_request(
            "canon_check refuses this step — "
            + "; ".join(f["message"] for f in verdict["flags"]
                        if f["level"] == "conflict")
        )
    try:
        quest = _quests.add_step(root(), ref, str(body.get("text") or ""),
                                 str(body.get("done_when") or ""),
                                 optional=bool(body.get("optional")))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except ValueError as exc:
        raise api.bad_request(str(exc))
    return api.ok({"quest": quest, "canon": verdict})


@router.delete("/api/quests/steps/{step_id}")
def quest_cut_step(step_id: int) -> dict:
    try:
        return api.ok(_quests.cut_step(root(), step_id))
    except LookupError as exc:
        raise api.not_found(str(exc))


@router.patch("/api/quests/{ref}")
def quest_update(ref: str, body: dict = Body(...)) -> dict:
    """Change a quest's fields — through the same gate the create path uses.

    THIS ROUTE WAS THE HOLE IN THIS FILE'S OWN ARGUMENT. The module docstring
    says HTTP writes are allowed here precisely BECAUSE the write path runs
    canon_check; POST /api/quests and POST .../steps both do and both refuse a
    conflict. This PATCH accepted `premise` and `reward` — narrative prose, the
    exact material canon_check reads — and called quests.update directly. So a
    premise naming a retired character was refused on create and accepted on
    edit, and the quieter path was the one that got past the gate.

    Checked against the quest's WHOLE prose, not just the changed field: canon
    conflicts are between sentences, and a new premise can contradict a step's
    done_when that was fine on its own.
    """
    quest_ref = ref
    prose = [k for k in ("premise", "reward") if body.get(k) is not None]
    verdict = None
    if prose:
        try:
            existing = _quests.get(root(), quest_ref)
        except LookupError as exc:
            raise api.not_found(str(exc))
        verdict = canon.check(root(), _canon_text(
            str(existing.get("title") or ""),
            str(body.get("premise") if body.get("premise") is not None
                else existing.get("premise") or ""),
            existing.get("steps") or [],
        ) + "\n" + str(body.get("reward") or ""))
        if verdict["verdict"] == "conflict":
            raise api.bad_request(
                "canon_check refuses this edit — "
                + "; ".join(f["message"] for f in verdict["flags"]
                            if f["level"] == "conflict"))
    try:
        out = _quests.update(
            root(), ref,
            premise=body.get("premise"), reward=body.get("reward"),
            state=body.get("state"), giver=body.get("giver"),
        )
        return api.ok({**out, "canon": verdict} if verdict else out)
    except LookupError as exc:
        raise api.not_found(str(exc))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.delete("/api/quests/{ref}")
def quest_delete(ref: str) -> dict:
    try:
        return api.ok(_quests.delete(root(), ref))
    except LookupError as exc:
        raise api.not_found(str(exc))
