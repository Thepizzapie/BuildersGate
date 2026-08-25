"""The production stage machine — what a project is ALLOWED to be doing yet.

WHAT WENT WRONG WITHOUT IT. A premise was talked into a plan and the plan fanned
out to every seat in the same tick. Art painted props, audio cut stingers and
narrative wrote barks for a game whose core loop nobody had ever played, because
nothing in the pipeline could say "not yet". NIGHT SHIFT: FLOOR 13 is the
measured case — it ran a full production pass against a loop that reduced to
attack + dodge + hold-interact, and the first moment anyone could tell was after
the assets existed. By then the cost of saying no was every asset already made.

The fix is a stage the whole board reads:

    thesis      Nobody produces anything. The director owes ONE SENTENCE naming
                the decision the player repeatedly makes, plus what makes that
                decision non-obvious. A feature list does not pass.
    graybox     Gameplay (and tech) prove the loop in one ugly test room. No art
                seat, no audio seat, no cinematic seat — those are HELD, by the
                readiness rule itself, not by a convention agents can forget.
    production  The specialist fan-out the old pipeline started with.
    release     Production plus a presentation gate a release candidate cannot
                close around.

WHY THE STAGE HOLDS SEATS RATHER THAN WARNING ABOUT THEM. The advisory version
of this already existed: a line in the director's brief saying prove the loop
first. It was read and ignored, in the ordinary way an instruction competing
with a queue full of dispatchable work gets ignored. Holding is enforced in
:func:`bgate_core.queue.ready`, THE one copy of the readiness rule, so a held
item is not dispatchable by either dispatcher and no third path exists.

WHAT IS AND IS NOT BYPASSABLE. The graybox hold takes a director WAIVER, because
a human who has already played the thing should not have to satisfy a checklist
about it — the waiver is per-seat, recorded with its reason, and shows up in
``state()``. The RELEASE gate takes no waiver of that kind: see
:func:`presentation_check`. The distinction is deliberate and it is the whole
lesson from Night Shift, where a blocked presentation gate was routed around and
the build shipped anyway.

STORAGE. One workspace doc, ``director/greenlight``, like the sign-off gate and
the sprite contract. Read fresh on every check, so advancing a stage takes
effect on the next dispatch rather than the next restart.
"""
from __future__ import annotations

import os
import time
from typing import Any

from . import activity, events as _events, workspace as _ws

SEAT = "director"
DOC_KEY = "greenlight"

THESIS = "thesis"
GRAYBOX = "graybox"
PRODUCTION = "production"
RELEASE = "release"
STAGES = (THESIS, GRAYBOX, PRODUCTION, RELEASE)

#: What a project is at before anybody says otherwise. Starting at ``thesis``
#: rather than ``production`` is the change: an unconfigured project used to be
#: a project with the brakes off, which is precisely the shipped failure.
DEFAULT = THESIS

STAGE_LABELS = {
    THESIS: "thesis — name the repeated decision before anything is built",
    GRAYBOX: "graybox — prove the loop in one ugly room",
    PRODUCTION: "production — specialist fan-out is open",
    RELEASE: "release — presentation QA binds on close",
}

#: Which seats may be dispatched at each stage. A seat absent from the tuple is
#: HELD: its queued items stay queued and say why in ``queue_list``.
#:
#: ``qa`` runs at every stage on purpose — the gate that verifies the graybox is
#: itself a seat, and holding it would deadlock the stage it is meant to clear.
#: An empty tuple means every seat, which is what production and release are.
STAGE_SEATS: dict[str, tuple[str, ...]] = {
    THESIS: ("director", "narrative", "qa"),
    GRAYBOX: ("director", "narrative", "gameplay", "tech", "qa"),
    PRODUCTION: (),
    RELEASE: (),
}

#: The minimum a thesis sentence can be and still be a sentence about a
#: decision. Below this it is a title.
MIN_SENTENCE = 40

#: A sentence claiming to name a decision has to contain the act of deciding.
#: This is a crude test and it is meant to be: what it catches is the pitch
#: sentence ("a tense horror game about surviving the night shift"), which is
#: the exact shape Night Shift's design opened with and never grew past.
DECISION_WORDS = (
    "choose", "choosing", "choice", "decide", "deciding", "decision",
    "trade", "trading", "trade-off", "tradeoff", "pick", "picking",
    "commit", "committing", "spend", "spending", "risk", "risking",
    "weigh", "weighing", "whether", "which", "when to", "where to",
    "how much", "give up", "sacrifice", "abandon",
)

MAX_TEXT = 2000


class StageRefused(PermissionError):
    """Something was attempted that this stage does not allow.

    A PermissionError rather than a ValueError: the input was fine, the project
    is not there yet. Callers that map exceptions to HTTP get 403 for free, and
    the two failures read differently in a log — which matters when the whole
    point is that somebody skipped a step.
    """


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _doc(root: str | os.PathLike[str]) -> dict:
    try:
        got = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _save(root: str | os.PathLike[str], doc: dict) -> dict:
    clean = {k: v for k, v in doc.items() if k != _ws.VERSION_KEY}
    _ws.set(root, SEAT, DOC_KEY, clean)
    return clean


def stage(root: str | os.PathLike[str]) -> str:
    """The stage this project is at.

    A project that has never stored one is read from its BOARD, and the two
    answers are different on purpose:

    * a board with work already past 'queued' is a project that was building
      before this gate existed. It is at ``production``. Holding it
      retroactively would be the harness inventing a refusal for work already
      under way — the same silent behaviour change on upgrade that
      :mod:`bgate_core.gates` refuses to make about sign-off, and for the same
      reason.
    * a board with nothing dispatched is a new project, and a new project
      starts at ``thesis``. That is the change: an unconfigured project used
      to be a project with the brakes off.

    Derived, never written. Storing it on first read would freeze whichever
    answer the first caller happened to get, and the answer legitimately moves
    the moment the first item lands.
    """
    got = str(_doc(root).get("stage") or "").strip()
    if got in STAGES:
        return got
    return PRODUCTION if _already_building(root) else DEFAULT


def _already_building(root: str | os.PathLike[str]) -> bool:
    """Has anything on this board ever left 'queued'?

    Cheap and best-effort: an unreadable board reads as a NEW project, which
    is the strict answer, because the alternative is an unreadable board
    silently opening the gate.
    """
    from . import db

    try:
        row = db.connect(root).execute(
            "SELECT 1 FROM work_item WHERE status <> 'queued' LIMIT 1"
        ).fetchone()
    except Exception:                                             # noqa: BLE001
        return False
    return row is not None


def waivers(root: str | os.PathLike[str]) -> dict:
    """Seat -> {reason, by, at} for every seat a director let through early."""
    got = _doc(root).get("waivers")
    return got if isinstance(got, dict) else {}


def allows(root: str | os.PathLike[str], seat: str) -> tuple[bool, str]:
    """May this seat be dispatched right now? ``(ok, reason_if_not)``.

    The reason is a sentence written for whoever is staring at a queued item
    that will not start — that reader is the entire justification for this
    returning prose instead of a bool.
    """
    at = stage(root)
    permitted = STAGE_SEATS.get(at, ())
    if not permitted or seat in permitted:
        return True, ""
    held = waivers(root).get(seat)
    if isinstance(held, dict) and held.get("reason"):
        return True, ""
    if at == THESIS:
        return False, (
            f"held: the project is at the {at!r} stage — no mechanical thesis "
            f"has been settled, so there is nothing for the {seat} seat to be "
            "producing against. Settle one with greenlight_thesis_set, then "
            "greenlight_advance('graybox').")
    return False, (
        f"held: the project is at the {at!r} stage — gameplay has not yet "
        f"proved the core loop in a test room, so {seat} work would be "
        "producing assets for a loop nobody has played. Submit the graybox "
        "(greenlight_graybox_submit), have the director rule on it "
        "(greenlight_graybox_verdict), then greenlight_advance('production'). "
        f"If this particular {seat} item genuinely has to run first, "
        f"greenlight_waive('{seat}', reason) says so on the record.")


def held_seats(root: str | os.PathLike[str]) -> tuple[str, ...]:
    """Every seat the current stage is holding, waivers already applied."""
    from . import seats as _seats

    return tuple(s for s in _seats.DEFAULT_SEATS if not allows(root, s)[0])


# ── the mechanical thesis ───────────────────────────────────────────────────

def validate_thesis(raw: Any) -> dict:
    """A thesis, or a ValueError naming what is missing.

    STRICT, AND THE STRICTNESS IS THE FEATURE. Every field exists because a
    shipped game reached production without it:

    ``sentence``   the one-liner. Must name an act of deciding — the pitch
                   sentence is what this rejects.
    ``options``    at least two things the player is deciding BETWEEN. One
                   option is a button, not a decision.
    ``stakes``     what the wrong pick costs. Without it every option is
                   equally fine and the decision is cosmetic.
    ``tension``    why the answer is not the same every time. This is the field
                   Night Shift could not have filled in, and filling it in is
                   what would have stopped the run.
    ``dominant_strategy``
                   the play that would collapse the decision, named on purpose.
                   The director seat already owes "what we are not building";
                   this is the same discipline pointed at the loop.
    ``cadence``    how often the decision comes round. A decision made once is
                   a plot point.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            "a mechanical thesis is an object with sentence, options, stakes, "
            "tension, dominant_strategy and cadence")
    sentence = " ".join(str(raw.get("sentence") or "").split())[:MAX_TEXT]
    if len(sentence) < MIN_SENTENCE:
        raise ValueError(
            f"the thesis sentence is {len(sentence)} characters; it has to be "
            f"at least {MIN_SENTENCE} and it has to be a sentence — 'what "
            "decision is the player repeatedly making that makes this game "
            "interesting?'")
    low = sentence.lower()
    if not any(word in low for word in DECISION_WORDS):
        raise ValueError(
            "that sentence describes the game, not a decision. It has to name "
            "what the player is choosing between — words like choose, trade, "
            "risk, commit, whether, which, how much. A premise ('a tense "
            "horror game about surviving the night shift') is what this "
            "refuses, because it is what shipped last time.")
    options = [" ".join(str(o).split())[:200]
               for o in (raw.get("options") or []) if str(o).strip()]
    if len(options) < 2:
        raise ValueError(
            f"a decision needs at least two options; got {len(options)}. One "
            "option is a button the player presses, and a loop built out of "
            "buttons is the 'attack + dodge + hold interact' outcome.")
    if len({o.lower() for o in options}) < 2:
        raise ValueError("the options are the same option written twice")
    fields = {}
    for key, why in (
        ("stakes", "what the wrong pick costs — without it every option is "
                   "equally fine and the decision is cosmetic"),
        ("tension", "why the answer is not the same every time — this is the "
                    "field a game with nouns and systems but no decision "
                    "structure cannot fill in"),
        ("dominant_strategy", "the play that would collapse this decision, "
                              "named on purpose so QA can go looking for it"),
        ("cadence", "how often the decision comes round — per room, per "
                    "minute, per encounter"),
    ):
        value = " ".join(str(raw.get(key) or "").split())[:MAX_TEXT]
        if len(value) < 10:
            raise ValueError(f"the thesis has no {key}: {why}")
        fields[key] = value
    return {"sentence": sentence, "options": options, **fields}


def thesis(root: str | os.PathLike[str]) -> dict:
    """The settled thesis, or ``{}``."""
    got = _doc(root).get("thesis")
    return got if isinstance(got, dict) else {}


def set_thesis(root: str | os.PathLike[str], raw: Any, by: str = "") -> dict:
    """Settle the mechanical thesis. Validated, logged, and stage-neutral.

    Deliberately does NOT advance the stage. Writing the sentence and agreeing
    it is worth building are two acts, and collapsing them would mean the
    director could not draft one without opening the gate behind it.
    """
    clean = validate_thesis(raw)
    clean["by"] = by or activity.current_actor()
    clean["at"] = _now()
    doc = _doc(root)
    doc["thesis"] = clean
    _save(root, doc)
    activity.log(root, "greenlight",
                 f"mechanical thesis settled: {clean['sentence'][:120]}",
                 seat=SEAT)
    return clean


# ── the graybox ─────────────────────────────────────────────────────────────

def graybox(root: str | os.PathLike[str]) -> dict:
    got = _doc(root).get("graybox")
    return got if isinstance(got, dict) else {}


def _under_project(root: str | os.PathLike[str], scene: str) -> bool:
    """Does ``scene`` name a real file in this project?

    Accepts a Godot ``res://`` path, a project-relative path, or one already
    prefixed with the ``game/`` subdirectory, because all three are what the
    seats actually type.
    """
    base = os.fspath(root)
    bare = scene[len("res://"):] if scene.startswith("res://") else scene
    bare = bare.replace("/", os.sep).lstrip(os.sep)
    return any(os.path.exists(os.path.join(base, part))
               for part in (bare, os.path.join("game", bare)))


def graybox_submit(root: str | os.PathLike[str], *, scene: str,
                   evidence: list[str] | None = None, notes: str = "",
                   by: str = "") -> dict:
    """Gameplay says the loop is playable in an ugly room. Evidence required.

    ``scene`` must exist on disk under the project. That check is the point:
    the failure this replaces is a seat reporting a loop as proven with nothing
    runnable behind the claim, which the sign-off gate could not catch because
    what it reviewed was prose.
    """
    scene = str(scene or "").strip()
    if not scene:
        raise ValueError("a graybox submission names the scene that holds it")
    if not _under_project(root, scene):
        raise ValueError(
            f"{scene!r} is not a file under this project — a graybox that "
            "cannot be opened is a claim, and this gate exists because claims "
            "were what got reviewed last time")
    shots = [str(e).strip()[:400] for e in (evidence or []) if str(e).strip()]
    if not shots:
        raise ValueError(
            "a graybox submission needs evidence: a playtest recording, a "
            "screenshot, or telemetry from an actual run. The director is "
            "being asked whether the interaction is interesting, and cannot "
            "answer that from a scene path.")
    doc = _doc(root)
    got = {
        "scene": scene, "evidence": shots,
        "notes": str(notes or "").strip()[:MAX_TEXT],
        "by": by or activity.current_actor(), "at": _now(),
        "verdict": "", "verdict_notes": "", "verdict_by": "", "verdict_at": "",
    }
    doc["graybox"] = got
    _save(root, doc)
    activity.log(root, "greenlight", f"graybox submitted from {scene}",
                 seat="gameplay")
    _events.emit(root, "greenlight.graybox", ref=scene,
                 payload={"stage": stage(root), "evidence": len(shots)})
    return got


def graybox_verdict(root: str | os.PathLike[str], *, verdict: str,
                    interesting: bool, why: str, by: str = "") -> dict:
    """The director rules on whether the interaction is actually interesting.

    ``why`` is mandatory in BOTH directions. A pass with no reason is the
    rubber stamp this gate exists to stop; a fail with no reason sends gameplay
    back with nothing to change.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        raise ValueError("verdict is 'pass' or 'fail'")
    why = " ".join(str(why or "").split())[:MAX_TEXT]
    if len(why) < 20:
        raise ValueError(
            "say why in a sentence. A pass with no reason is the rubber stamp "
            "this gate exists to stop, and a fail with no reason sends "
            "gameplay back with nothing to change.")
    if verdict == "pass" and not interesting:
        raise ValueError(
            "a pass that says the interaction is not interesting is the "
            "contradiction this gate is for — fail it, or say what makes it "
            "interesting")
    doc = _doc(root)
    got = doc.get("graybox")
    if not isinstance(got, dict) or not got.get("scene"):
        raise StageRefused(
            "there is no graybox to rule on — greenlight_graybox_submit first")
    got.update({"verdict": verdict, "interesting": bool(interesting),
                "verdict_notes": why,
                "verdict_by": by or activity.current_actor(),
                "verdict_at": _now()})
    doc["graybox"] = got
    _save(root, doc)
    activity.log(root, "greenlight", f"graybox {verdict}: {why[:120]}",
                 seat=SEAT)
    _events.emit(root, "greenlight.graybox", ref=str(got.get("scene") or ""),
                 payload={"verdict": verdict, "interesting": bool(interesting)})
    return got


# ── advancing ───────────────────────────────────────────────────────────────

def blockers(root: str | os.PathLike[str], to: str) -> list[str]:
    """What stands between this project and ``to``. Empty means go.

    Split out from :func:`advance` so the dashboard and the director's brief
    can SHOW the list without attempting the move — "why can I not advance"
    was a question the first cut of this could only answer by failing.
    """
    to = str(to or "").strip().lower()
    if to not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    at = stage(root)
    if STAGES.index(to) <= STAGES.index(at):
        return []                        # going back, or standing still
    out: list[str] = []
    if STAGES.index(to) >= STAGES.index(GRAYBOX) and not thesis(root):
        out.append(
            "no mechanical thesis is settled — greenlight_thesis_set. One "
            "sentence: what decision is the player repeatedly making that "
            "makes this game interesting?")
    if STAGES.index(to) >= STAGES.index(PRODUCTION):
        got = graybox(root)
        if not got.get("scene"):
            out.append(
                "no graybox has been submitted — gameplay proves the loop in "
                "one ugly test room first (greenlight_graybox_submit)")
        elif got.get("verdict") != "pass":
            ruled = got.get("verdict") or "unruled"
            out.append(
                f"the graybox verdict is {ruled!r}, not 'pass' — the director "
                "rules on whether the interaction is actually interesting "
                "(greenlight_graybox_verdict)")
        from . import encounter as _enc

        out.extend(_enc.production_blockers(root))
    if STAGES.index(to) >= STAGES.index(RELEASE):
        unmet = presentation_check(root)["unmet"]
        out.extend(f"presentation QA: {row}" for row in unmet)
        if unmet:
            # SAID HERE TOO, and not only in release_guard. A director looking
            # at a refusal reaches for the tool that got them past the last
            # one, and the last one took a waiver. This one does not, and the
            # refusal has to be the thing that says so.
            out.append(
                "there is no waiver for the presentation gate — "
                "greenlight_waive releases a seat from a STAGE hold and does "
                "nothing here. The rows above clear by being done.")
    return out


def advance(root: str | os.PathLike[str], to: str, by: str = "") -> dict:
    """Move the project to ``to``, or raise :class:`StageRefused` saying why not.

    Moving BACKWARD is always allowed and is not an error: a project that
    discovers in production that its loop does not hold should be able to drop
    to graybox without arguing with a state machine.
    """
    to = str(to or "").strip().lower()
    if to not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    at = stage(root)
    held = blockers(root, to)
    if held:
        raise StageRefused(
            f"cannot advance from {at!r} to {to!r}:\n- " + "\n- ".join(held))
    doc = _doc(root)
    doc["stage"] = to
    doc["stage_since"] = _now()
    doc["stage_by"] = by or activity.current_actor()
    _save(root, doc)
    activity.log(root, "greenlight", f"stage {at} -> {to}", seat=SEAT)
    _events.emit(root, "greenlight.stage", ref=to, payload={"from": at, "to": to})
    return state(root)


def waive(root: str | os.PathLike[str], seat: str, reason: str,
          by: str = "") -> dict:
    """Let one seat through the stage hold, on the record.

    NOT AVAILABLE FOR THE RELEASE GATE, which takes no waiver — see
    :func:`presentation_check`. This is the escape hatch for the true case (a
    tech seat building the graybox's own tooling, an art seat making the
    graybox's placeholder blocks) and it costs a sentence, which is the price
    that keeps it from becoming the default route.
    """
    from . import seats as _seats

    if seat not in _seats.DEFAULT_SEATS:
        raise ValueError(f"unknown seat {seat!r}")
    reason = " ".join(str(reason or "").split())[:MAX_TEXT]
    if len(reason) < 15:
        raise ValueError(
            "a waiver costs a sentence. Say what this seat has to do before "
            "the loop is proven and why it cannot wait.")
    doc = _doc(root)
    held = doc.get("waivers")
    doc["waivers"] = held if isinstance(held, dict) else {}
    doc["waivers"][seat] = {"reason": reason,
                            "by": by or activity.current_actor(), "at": _now()}
    _save(root, doc)
    activity.log(root, "greenlight",
                 f"waived the stage hold on {seat}: {reason[:120]}", seat=SEAT)
    return doc["waivers"][seat]


def unwaive(root: str | os.PathLike[str], seat: str) -> dict:
    doc = _doc(root)
    held = doc.get("waivers")
    if isinstance(held, dict) and seat in held:
        held.pop(seat)
        doc["waivers"] = held
        _save(root, doc)
        activity.log(root, "greenlight", f"the {seat} waiver was withdrawn",
                     seat=SEAT)
    return waivers(root)


# ── the presentation gate ───────────────────────────────────────────────────

#: The sections a release candidate is reconciled against, and the module that
#: owns each judgement. Order is the order a reader wants them in: the runtime
#: the player boots first, then whether the assets are actually IN it, then
#: what they look and sound like.
#:
#: THE FIRST TWO ARE NEW AND THEY ARE THE TWO THAT CAUGHT THE EXPENSIVE ONES.
#: A 3D benchmark shipped with `run/main_scene` still pointing at the scaffold
#: demo while every named-scene test passed, and it shipped delivered assets
#: nothing loaded while `asset_verify` — which already answers exactly that,
#: with `delivered_but_unwired` — was never run by anybody.
_SECTIONS = ("default_scene", "assets", "rooms", "scale", "audio")


def presentation_check(root: str | os.PathLike[str]) -> dict:
    """What a release candidate still owes. THIS ONE TAKES NO WAIVER.

    Night Shift's presentation gate was blocked and the build shipped anyway,
    which is only possible if the gate is something a caller CONSULTS rather
    than something the close path RUNS. So this is called by the close path
    (:func:`release_guard`) and an unmet row refuses the close outright. The
    only thing that clears a row is doing the work the row names.

    Rows come from the modules that own each judgement, so this stays a
    reconciliation and never a second opinion:

    * default scene — the project boots into the GAME, that frame has been
      captured with no scene override, and somebody has said what is in it
      (:mod:`bgate_core.sceneproof`)
    * assets — delivered is not integrated: dangling references, stale
      imports, delivered-but-unwired orphans (:mod:`bgate_core.assets`)
    * room composition — every playable room has a full-room verdict
      (:mod:`bgate_core.roomqa`)
    * asset scale — every delivered art asset measured against the scale
      contract, BY A TOOL COMPETENT FOR THIS PROJECT'S DIMENSION
      (:mod:`bgate_core.scalecontract`)
    * audio — cues reviewed against a gameplay capture, not just file metrics

    EVERY ROW IS AUDITED FOR SATISFIABILITY before it is published. A row that
    no valid action clears is reported as a HARNESS BUG (``impossible``) rather
    than left on an operator's pile looking like work — see
    :mod:`bgate_core.findings`. Rows are recorded to the findings ledger with
    the tool and measurement behind them, so a later, better measurement can
    supersede one with an audit trail instead of it blocking forever.
    """
    from . import findings as _findings

    rows: list[dict] = []
    met: list[str] = []
    for name, fn in (("default_scene", _default_scene_unmet),
                     ("assets", _assets_unmet),
                     ("rooms", _rooms_unmet), ("scale", _scale_unmet),
                     ("audio", _audio_unmet)):
        try:
            found = list(fn(root) or [])
        except Exception as exc:                                  # noqa: BLE001
            # A check that will not run is NOT a pass. The failure this gate
            # exists for is a blocked check being treated as an absent one.
            rows.append(_findings.make(
                gate=name, key=f"{name}:unrunnable",
                kind=_findings.BLOCKING,
                claim=(f"the {name} check could not run "
                       f"({type(exc).__name__}: {exc}) — an unrunnable "
                       "presentation check is a fail, not a skip"),
                tool=f"greenlight._{name}_unmet",
                clears_by="fix the check itself; a gate that cannot ask its "
                          "own question cannot pass"))
            continue
        if found:
            # A section may still answer in sentences — several did before
            # findings existed, and a test or a plug-in check may hand back a
            # bare list of strings. Promote rather than crash: a gate that
            # falls over on the shape of a refusal is a gate that passes.
            rows.extend(row if isinstance(row, dict) else _findings.make(
                gate=name, key=f"{name}:{i}", kind=_findings.BLOCKING,
                claim=str(row), tool=f"greenlight._{name}_unmet",
                clears_by="do what the row says")
                for i, row in enumerate(found))
        else:
            met.append(f"{name}: clear")

    # A finding already retracted by a better measurement stops blocking but
    # stays readable. This is what makes a false blocker survivable.
    try:
        retracted = {str(f.get("key")): f
                     for f in _findings.ledger(root)
                     if f.get("superseded_by")}
    except Exception:                                             # noqa: BLE001
        retracted = {}
    standing, superseded = [], []
    for row in rows:
        was = retracted.get(str(row.get("key")))
        if was and str(was.get("gate")) == str(row.get("gate")):
            superseded.append({**row, "superseded_by": was["superseded_by"],
                               "superseded_why": was.get("superseded_why", "")})
        else:
            standing.append(row)

    for row in standing:
        try:
            _findings.record(root, row)
        except Exception:                                         # noqa: BLE001
            pass

    audit = _findings.audit(standing)
    unmet = [_row_sentence(r) for r in standing]
    return {
        "ok": not standing,
        "unmet": unmet,
        "rows": standing,
        "met": met,
        "stage": stage(root),
        # THE GATE GRADING ITSELF. `harness_bug` being non-empty means this
        # gate is publishing a row nobody can clear, which is a defect here and
        # not backlog there.
        "satisfiable": audit["ok"],
        "harness_bug": audit["impossible"],
        "harness_bug_why": audit["why"],
        "by_kind": audit["counts"],
        "superseded": superseded,
        # WHAT A PASS HERE DOES NOT COVER. `met: ["scale: clear"]` was read as
        # "the assets are the right size"; it means "every delivered asset has
        # a recorded measurement inside its band", which is a much smaller
        # claim. Every false green in this benchmark lived in that gap, and the
        # reader was not being careless — nothing told them where the edge was.
        "blind_spots": _findings.blind_spots(_SECTIONS),
        "note": ("rows carry the tool, inputs and measurement behind them. A "
                 "row proved wrong by a better measurement is retracted with "
                 "greenlight_supersede — it stops blocking and stays visible. "
                 "READ `blind_spots` BEFORE TREATING A PASS AS COVERAGE: a "
                 "green row is a statement about what was measured, never "
                 "about the thing."),
    }


def _row_sentence(row: dict) -> str:
    """One gate row as the line a person reads, action included.

    The action is not decoration: the benchmark's false blocker read as a
    complete instruction and was not one, and the only way to tell was to go
    and check the tool's parameter list.
    """
    claim = str(row.get("claim") or "")
    clears = str(row.get("clears_by") or "")
    kind = str(row.get("kind") or "")
    lead = {"judgement": "[needs a person] ",
            "impossible": "[HARNESS BUG - no action clears this] "}.get(kind, "")
    tail = f" -> {clears}" if clears else ""
    return f"{lead}{claim}{tail}"


def _default_scene_unmet(root) -> list[dict]:
    from . import sceneproof as _proof

    return _proof.unproven(root)


def _assets_unmet(root) -> list[dict]:
    """DELIVERED IS NOT INTEGRATED — the row nobody was running.

    ``asset_verify`` has answered this since it was written: ``dangling``
    references, ``delivered_but_unwired`` orphans joined against the artifact
    ledger, and ``freshness`` (whether the ENGINE is serving the bytes on
    disk). It was never part of any gate, so the answer existed and nobody
    read it.

    DYNAMIC LOADING IS PRESERVED AS A CAVEAT, NOT ERASED. A project that
    builds resource paths at run time cannot be scanned statically, and
    ``dynamic_load_sites`` counts exactly those places. Where they exist an
    unwired asset is reported as a CANDIDATE — it still blocks, because
    somebody has to look, but the row says plainly that a static scan cannot
    settle it.
    """
    from . import assets as _assets, findings as _findings

    got = _assets.verify(root)
    wiring = got.get("integration") or {}
    dynamic = int(wiring.get("dynamic_load_sites") or 0)
    hedge = ("" if not dynamic else
             f" CANDIDATE, not a verdict: this project builds resource paths "
             f"at run time in {dynamic} place(s), which no static scan "
             f"follows — confirm before deleting anything.")
    out: list[dict] = []

    for row in (wiring.get("dangling") or [])[:40]:
        path = row.get("path") if isinstance(row, dict) else row
        out.append(_findings.make(
            gate="assets", key=f"dangling:{path}", kind=_findings.BLOCKING,
            claim=(f"{path} is referenced by the project and is not there — "
                   "the game loads a resource that does not exist"),
            tool="asset_verify", inputs={"scan": "integration"},
            measured={"kind": "dangling", "path": str(path)},
            clears_by=("deliver the missing file, or unwire the reference "
                       "(scene_unwire / scene_swap_resource)")))

    for row in (wiring.get("delivered_but_unwired") or [])[:40]:
        path = row.get("path") if isinstance(row, dict) else row
        item = (row or {}).get("work_item_id") if isinstance(row, dict) else ""
        out.append(_findings.make(
            gate="assets", key=f"unwired:{path}", kind=_findings.BLOCKING,
            claim=(f"{path} was DELIVERED and nothing in the game consumes it"
                   + (f" (produced by item #{item})" if item else "")
                   + ". Delivered is not integrated: the file being at the "
                     "right path is the one thing that was never in doubt."
                   + hedge),
            tool="asset_verify", inputs={"scan": "delivered_but_unwired"},
            measured={"kind": "delivered_but_unwired", "path": str(path),
                      "dynamic_load_sites": dynamic},
            clears_by=("wire it (scene_wire / scene_swap_resource), or mark "
                       "the artifact rejected if it is not going in")))

    freshness = wiring.get("freshness") or {}
    for row in (freshness.get("stale") or [])[:20]:
        path = row.get("path") if isinstance(row, dict) else row
        out.append(_findings.make(
            gate="assets", key=f"stale:{path}", kind=_findings.BLOCKING,
            claim=(f"the engine is serving an OLDER build of {path} than the "
                   "bytes on disk — every structural check passes and the "
                   "running game draws the previous version"),
            tool="asset_verify", inputs={"scan": "freshness"},
            measured={"kind": "stale",
                      "on_disk_md5": (row or {}).get("on_disk_md5"),
                      "imported_md5": (row or {}).get("imported_md5")},
            clears_by="godot_check_project to reimport, then asset_verify again"))

    for row in (got.get("missing") or [])[:20]:
        out.append(_findings.make(
            gate="assets", key=f"missing:{row}", kind=_findings.BLOCKING,
            claim=f"{row} is tracked by this project and is gone from disk",
            tool="asset_verify", inputs={"scan": "intact"},
            measured={"kind": "missing", "path": str(row)},
            clears_by="restore the file, or asset_release it from tracking"))

    return out


def _rooms_unmet(root) -> list[dict]:
    from . import findings as _findings, roomqa as _roomqa

    return [_findings.make(
        gate="rooms", key=f"room:{i}", kind=_findings.JUDGEMENT,
        claim=sentence, tool="roomqa.unreviewed",
        clears_by=("room_review(scene, shot=..., verdict=...) against a "
                   "screenshot of the WHOLE room — a person looks at the "
                   "composition; no measurement substitutes"))
        for i, sentence in enumerate(_roomqa.unreviewed(root))]


def _scale_unmet(root) -> list[dict]:
    from . import scalecontract as _scale

    return _scale.unmeasured_findings(root)


def _audio_unmet(root) -> list[dict]:
    from . import audiohooks as _hooks, findings as _findings

    check = getattr(_hooks, "in_game_unreviewed", None)
    if check is None:
        return []
    return [_findings.make(
        gate="audio", key=f"audio:{i}", kind=_findings.JUDGEMENT,
        claim=sentence, tool="audiohooks.in_game_unreviewed",
        clears_by=("audio_listen_record(...) after HEARING the cue in a "
                   "gameplay capture — a file metric is not a listen. This "
                   "row needs a person and that is correct, not backlog"))
        for i, sentence in enumerate(check(root) or [])]


def release_guard(root: str | os.PathLike[str]) -> None:
    """Raise :class:`StageRefused` if a release candidate may not close.

    The one call a close path needs. Kept separate from ``presentation_check``
    so the check can be READ anywhere without a caller accidentally treating a
    truthy dict as permission — which is how the last gate got bypassed.
    """
    if stage(root) != RELEASE:
        return
    got = presentation_check(root)
    if got["ok"]:
        return
    _events.emit(root, "greenlight.release_refused", ref="",
                 payload={"unmet": got["unmet"][:12],
                          "satisfiable": got.get("satisfiable", True)})
    # A ROW NOBODY CAN CLEAR IS NAMED AS OURS. The alternative is what shipped:
    # a false blocker written by a tool answering outside its competence, in a
    # list of real work, indistinguishable from it, permanent.
    bug = ""
    if not got.get("satisfiable", True):
        bug = ("\n\n" + got.get("harness_bug_why", "")
               + "\nUnclearable row(s): "
               + "; ".join(str(r.get("claim", ""))[:160]
                           for r in got.get("harness_bug") or []))
    raise StageRefused(
        "this build is a release candidate and presentation QA is not "
        "complete. It does not close until these are done — there is no "
        "waiver for this gate:\n- " + "\n- ".join(got["unmet"]) + bug)


def state(root: str | os.PathLike[str]) -> dict:
    """Everything a panel, a brief or a digest needs in one read."""
    at = stage(root)
    ahead = STAGES.index(at) + 1
    nxt = STAGES[ahead] if ahead < len(STAGES) else ""
    return {
        "stage": at,
        "label": STAGE_LABELS[at],
        "stages": list(STAGES),
        "thesis": thesis(root),
        "graybox": graybox(root),
        "waivers": waivers(root),
        "held_seats": list(held_seats(root)),
        "next": nxt,
        "blockers": blockers(root, nxt) if nxt else [],
        "since": str(_doc(root).get("stage_since") or ""),
    }
