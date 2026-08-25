"""Gate findings that carry their own provenance, and can be RETRACTED.

WHAT WENT WRONG WITHOUT IT. The presentation gate refused a release candidate
on a row that read:

    game/assets/cat_turnaround.png is in the game with no scale measurement —
    scale_check('...', klass) at game scale.

Two things were wrong with that sentence and neither was visible from it. The
tool that would clear it measures the OPAQUE PIXEL BOX OF A PNG, and the asset
was a 3D character's turnaround render, so the number it would produce is a
fact about a render canvas and about nothing in the game. And the vocabulary it
offers — ``prop|furniture|door|ui|enemy`` — has no ``player`` in it, so for a
player character there was no argument that could be passed at all. The row
claimed it cleared by being done. Nothing anybody could do would have done it.

A row like that is worse than no row: it teaches an operator to route around
the gate, which is the exact failure the gate exists to prevent.

So a finding here is not a sentence. It is a record:

    what       the claim, in the words a person reads
    tool       WHICH TOOL SAID SO, and with what inputs
    measured   what it actually measured — the number, the path, the unit
    clears_by  a runnable action, or a declared judgement, or nothing
    kind       blocking | judgement | unfinished | impossible

and it can be SUPERSEDED by a later, better measurement. Superseding does not
delete: the old row stays readable with the id of what replaced it and the
reason, because "this gate blocked on a false measurement for a day" is
precisely the sentence a post-mortem needs and precisely the one a delete
destroys.

STORAGE IS A JSONL FILE, on the same reasoning as ``enginetests``: appending a
line is the one write that cannot lose earlier lines to a crash, and an audit
trail whose earlier entries can vanish is not one. It lives beside the other
``.bgate/`` logs.

THE THREE NON-BLOCKING KINDS, and why they are not one:

    judgement    a human has to look — "these audio cues have never been heard
                 in context". Correct, expected, and NOT a bug. It blocks, and
                 it is labelled so nobody tries to automate it away.
    unfinished   work exists and nobody has done it yet. Ordinary backlog.
    impossible   no valid action clears it. THIS IS A HARNESS BUG, never
                 operator backlog, and it is reported as one.

:func:`actionable` is what enforces that distinction, and it is the check the
gate runs over its own rows before it refuses anything.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

LOG_FILE = "gate-findings.jsonl"

#: A finding that BLOCKS and names a runnable action.
BLOCKING = "blocking"
#: A finding that blocks and needs a person's eyes — correct, not a defect.
JUDGEMENT = "judgement"
#: Work nobody has done yet. Blocks; ordinary backlog.
UNFINISHED = "unfinished"
#: No valid action clears it. A HARNESS BUG. Blocks loudly and says so.
IMPOSSIBLE = "impossible"

KINDS = (BLOCKING, JUDGEMENT, UNFINISHED, IMPOSSIBLE)

#: Kinds that stop a release. All of them — the distinction is about who acts,
#: not about whether the gate holds.
BLOCKS = KINDS

MAX_TEXT = 2000


def _log_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / LOG_FILE


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clip(text: Any, cap: int = MAX_TEXT) -> str:
    return str(text or "")[:cap]


def make(*, gate: str, key: str, claim: str, tool: str = "",
         inputs: Optional[dict] = None, measured: Optional[dict] = None,
         clears_by: str = "", kind: str = BLOCKING,
         dimension: str = "") -> dict:
    """Build one finding. Pure — nothing is written until :func:`record`.

    ``key`` is the STABLE IDENTITY of the claim (usually the path or scene it
    is about, prefixed by the gate section). Supersession matches on it, so a
    better measurement of the same subject can retract the earlier one without
    anybody having to quote its id.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return {
        "id": uuid.uuid4().hex[:12],
        "at": _now(),
        "gate": _clip(gate, 40),
        "key": _clip(key, 300),
        "kind": kind,
        "claim": _clip(claim),
        "tool": _clip(tool, 120),
        "inputs": dict(inputs or {}),
        "measured": dict(measured or {}),
        "clears_by": _clip(clears_by, 600),
        "dimension": _clip(dimension, 12),
        "superseded_by": "",
        "superseded_why": "",
    }


def record(root: str | os.PathLike[str], finding: dict) -> dict:
    """Append one finding. Best-effort — never raises at a caller.

    A gate that cannot write its own ledger still has to be able to refuse.
    """
    row = dict(finding)
    row.setdefault("id", uuid.uuid4().hex[:12])
    row.setdefault("at", _now())
    try:
        path = _log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def _read(root: str | os.PathLike[str]) -> list[dict]:
    try:
        lines = _log_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def ledger(root: str | os.PathLike[str], *, gate: str = "",
           include_superseded: bool = True) -> list[dict]:
    """Every recorded finding, oldest first, with supersession folded in.

    A finding appears ONCE, carrying whatever retracted it. The retraction rows
    themselves are not returned as findings — they are the audit trail, and
    :func:`supersessions` is where they are read as events.
    """
    rows = _read(root)
    findings: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        if row.get("_retracts"):
            continue
        fid = str(row.get("id") or "")
        if not fid:
            continue
        if fid not in findings:
            order.append(fid)
        findings[fid] = row
    for row in rows:
        target = str(row.get("_retracts") or "")
        if not target or target not in findings:
            continue
        findings[target] = {
            **findings[target],
            "superseded_by": str(row.get("id") or ""),
            "superseded_why": _clip(row.get("why"), 600),
            "superseded_at": row.get("at") or "",
            "superseded_by_tool": _clip(row.get("tool"), 120),
            "superseded_measured": dict(row.get("measured") or {}),
        }
    out = [findings[fid] for fid in order]
    if gate:
        out = [f for f in out if str(f.get("gate") or "") == gate]
    if not include_superseded:
        out = [f for f in out if not f.get("superseded_by")]
    return out


def standing(root: str | os.PathLike[str], *, gate: str = "") -> list[dict]:
    """Findings that still block: everything not superseded."""
    return ledger(root, gate=gate, include_superseded=False)


def supersessions(root: str | os.PathLike[str]) -> list[dict]:
    """The retraction events themselves, oldest first — the audit trail."""
    return [r for r in _read(root) if r.get("_retracts")]


def supersede(root: str | os.PathLike[str], finding_id: str, *, why: str,
              tool: str = "", measured: Optional[dict] = None,
              by: str = "") -> dict:
    """Retract one finding with a LATER, BETTER measurement.

    The retraction is itself a row, so the ledger reads as a history rather
    than as a current state somebody edited. ``why`` is required and is the
    whole value of the operation: "superseded" with no antecedent is the same
    unaccountable erasure as a delete.

    Raises LookupError for an id that is not in the ledger — a retraction of a
    finding that does not exist is a typo, and silently appending it would put
    an orphan in the audit trail forever.
    """
    why = " ".join(str(why or "").split())
    if len(why) < 10:
        raise ValueError(
            "a retraction costs a sentence: say what was measured instead and "
            "why it outranks the finding being withdrawn")
    known = {str(f.get("id")): f for f in ledger(root)}
    if finding_id not in known:
        raise LookupError(
            f"no finding {finding_id!r} in this project's gate ledger — "
            "greenlight_status(section='findings') lists what is there")
    row = {
        "id": uuid.uuid4().hex[:12],
        "at": _now(),
        "_retracts": str(finding_id),
        "why": _clip(why, 600),
        "tool": _clip(tool, 120),
        "measured": dict(measured or {}),
        "by": _clip(by, 120),
        "gate": known[finding_id].get("gate", ""),
        "key": known[finding_id].get("key", ""),
    }
    record(root, row)
    return row


def supersede_key(root: str | os.PathLike[str], gate: str, key: str, *,
                  why: str, tool: str = "", measured: Optional[dict] = None,
                  by: str = "") -> list[dict]:
    """Retract every standing finding about one subject. Returns what it hit.

    The form a TOOL uses when it re-measures something authoritatively: a 3D
    AABB measurement of a character supersedes whatever a 2D pixel scan said
    about the same character, and the tool that took it should not have to know
    the id of the row it is correcting.
    """
    hit = [f for f in standing(root, gate=gate)
           if str(f.get("key") or "") == str(key)]
    return [supersede(root, str(f["id"]), why=why, tool=tool,
                      measured=measured, by=by) for f in hit]


# ── the row that no action can clear ────────────────────────────────────────

def actionable(finding: dict) -> dict:
    """Can a correct action clear this row? {ok, kind, why}.

    THE CHECK THE GATE RUNS OVER ITSELF. A blocking row must name something
    runnable; a judgement row must name the person and the artefact they are to
    look at. A row that names neither is IMPOSSIBLE, and impossible is a
    harness bug — it is reported as one rather than left on an operator's pile
    where it looks like work.
    """
    kind = str(finding.get("kind") or BLOCKING)
    clears = str(finding.get("clears_by") or "").strip()
    if kind == IMPOSSIBLE:
        return {"ok": False, "kind": IMPOSSIBLE,
                "why": "no valid action clears this row — it is a harness bug, "
                       "not operator backlog"}
    if kind == JUDGEMENT:
        if not clears:
            return {"ok": False, "kind": IMPOSSIBLE,
                    "why": "a judgement row must name WHO looks at WHAT; this "
                           "one names neither"}
        return {"ok": True, "kind": JUDGEMENT,
                "why": "a person has to look — this is correct, not unfinished"}
    if not clears:
        return {"ok": False, "kind": IMPOSSIBLE,
                "why": "the row names no action that would clear it"}
    return {"ok": True, "kind": kind, "why": ""}


# ── the label that named the wrong thing ────────────────────────────────────

#: Anything that looks like a path, a res:// reference or a node path. Crude on
#: purpose: what it has to catch is a sentence naming one artefact while the
#: measurement is about another, and that shape is always a bare identifier.
_NAMED = re.compile(
    r"(?:res://[\w./-]+|[\w./-]+\.(?:tscn|gd|glb|gltf|png|jpg|wav|ogg|tres)"
    r"|/[A-Za-z]\w*(?:/\w+)+)")


def label_check(finding: dict) -> dict:
    """Does this row's PROSE name the thing its MEASUREMENT is about?

    THE ASSERTION THAT SENT THREE PIECES OF WORK AT NOTHING. A test emitted

        owner CAN guard bedroom (dresser) within 2m

    while measuring a task marker 2.75 m from the dresser. That sentence was
    relayed into a director report, into a filed work item, and into a
    dispatched agent's brief before anybody checked the coordinate. Nobody
    lied; the label had simply stopped being about what the code measured, and
    prose does not go stale visibly.

    This cannot police a project's own GDScript assertions — it can police
    every label the HARNESS emits, which is where the bad one entered the
    ledger. A row whose claim names artefacts and whose measurement is about a
    different one is reported, not silently trusted.

    Returns ``{ok, why, named, key}``. ``ok`` is True when the claim names
    nothing (a general statement is not a mislabel) or names the key.
    """
    claim = str(finding.get("claim") or "")
    key = str(finding.get("key") or "")
    measured = " ".join(str(v) for v in (finding.get("measured") or {}).values())
    inputs = " ".join(str(v) for v in (finding.get("inputs") or {}).values())
    named = _NAMED.findall(claim)
    if not named:
        return {"ok": True, "why": "", "named": [], "key": key}
    haystack = f"{key} {measured} {inputs}"
    hit = [n for n in named if n in haystack or n.split("/")[-1] in haystack]
    if hit:
        return {"ok": True, "why": "", "named": named, "key": key}
    return {
        "ok": False, "named": named, "key": key,
        "why": (f"this row's text names {', '.join(named[:3])}, and its "
                f"measurement is about {key!r}. One of the two is stale. A "
                "label that has stopped being about what was measured becomes "
                "trusted evidence faster than anything else here — the "
                "benchmark relayed one into a director report, a work item and "
                "an agent's brief before anybody checked the coordinate."),
    }


# ── what a gate does NOT measure ────────────────────────────────────────────
#
# EVERY GATE MUST DECLARE ITS BLIND SPOT. A green check is read as a statement
# about the thing; it is only ever a statement about what was measured, and the
# gap between those two is where every false green in this benchmark lived:
#
#   the scale gate measured a PNG's opaque pixels and was read as "the cat is
#   the right size"; the traversal tests measured vertical rise and were read
#   as "the route is playable"; the named-scene evidence measured a scene
#   somebody chose and was read as "the game looks like this"; `has_collider`
#   counted shapes and was read as "the collider is right".
#
# In every case the check was CORRECT about its own subject. What was missing
# was the sentence saying what its subject was not. A gate that cannot say what
# it does not cover invites the reader to assume it covers everything, and the
# reader is not being careless — they have nothing else to go on.
#
# So a gate section declares its limits here, once, and every result carries
# them. Absent from this table is itself a finding: see :func:`blind_spots`.
BLIND_SPOTS: dict[str, str] = {
    "default_scene":
        "proves the project BOOTS into the intended scene and that somebody "
        "described the frame. It does not measure anything past the capture "
        "instant — nothing about whether the game is playable, whether the "
        "loop works, or what happens on frame two.",
    "assets":
        "a STATIC scan of references and import freshness. It cannot follow a "
        "resource path built at run time (see `dynamic_load_sites`), it does "
        "not open any asset to see whether the CONTENT is right, and a wired "
        "asset can still be the wrong picture.",
    "rooms":
        "a human's judgement of one room's composition against a screenshot. "
        "It is not a measurement, it does not cover rooms nobody submitted, "
        "and a passing room can still be unplayable — nothing here drives the "
        "player.",
    "scale":
        "measures one asset against the declared player height. On a 2D "
        "project that is opaque pixels; on a 3D one it must be engine "
        "geometry (see scalecontract.dimension_guard). It says nothing about "
        "where the asset SITS, whether it collides correctly, or how it reads "
        "next to anything else in the room.",
    "audio":
        "whether a cue has been heard in context by a person. It measures no "
        "levels, no mix, and nothing about whether the cue fires at the right "
        "moment — only that somebody listened.",
    "traversal":
        "proves ONE route, with ONE input program, from ONE launch surface. A "
        "pass says that route is completable, not that the level is; and it "
        "says nothing about what the player does when they miss.",
}


def blind_spots(gates: Iterable[str]) -> dict:
    """What each of these gates does not measure, and which ones will not say.

    A gate absent from :data:`BLIND_SPOTS` is reported as `undeclared` rather
    than skipped: a gate that has never been asked what it misses is exactly
    the one whose green will be over-read.
    """
    named, undeclared = {}, []
    for gate in sorted(set(str(g) for g in gates if g)):
        if gate in BLIND_SPOTS:
            named[gate] = BLIND_SPOTS[gate]
        else:
            undeclared.append(gate)
    return {
        "declared": named,
        "undeclared": undeclared,
        "why": ("a green row is a statement about WHAT WAS MEASURED, never "
                "about the thing. These are the gaps each gate is known to "
                "have; read them before treating a pass as coverage."
                + (f" {len(undeclared)} gate(s) declare no limits at all "
                   f"({', '.join(undeclared)}) — that is a gap in the gate, "
                   "not a guarantee." if undeclared else "")),
    }


def audit(rows: Iterable[dict]) -> dict:
    """Grade a whole gate's rows for satisfiability.

    Returns ``{ok, impossible, judgement, blocking, unfinished}``. ``ok`` is
    False when ANY row is unclearable — the gate is then reporting a defect in
    itself and should say so at the top rather than in a footnote.
    """
    buckets: dict[str, list[dict]] = {k: [] for k in KINDS}
    impossible: list[dict] = []
    mislabelled: list[dict] = []
    for row in rows:
        label = label_check(row)
        if not label["ok"]:
            mislabelled.append({**row, "why_mislabelled": label["why"]})
        verdict = actionable(row)
        if verdict["kind"] == IMPOSSIBLE and not verdict["ok"]:
            impossible.append({**row, "why_impossible": verdict["why"]})
            buckets[IMPOSSIBLE].append(row)
            continue
        buckets[verdict["kind"]].append(row)
    return {
        "ok": not impossible,
        "impossible": impossible,
        "mislabelled": mislabelled,
        "counts": {k: len(v) for k, v in buckets.items()},
        "why": ("" if not impossible else
                f"{len(impossible)} gate row(s) cannot be cleared by any valid "
                "action. That is a HARNESS BUG, not work — a row no correct "
                "action clears teaches operators to route around the gate. "
                "Fix the tool that produced it, then supersede the row "
                "(greenlight_supersede)."),
    }
