"""Decision-model tools: `decide` for any seat, and bots that play the game.

The model is Jev (TypeSafe AI) through :mod:`bgate_adapters.jev`. It writes
nothing; it returns a typed answer - a choice, a score, a probability - over a
state. That makes it the right engine for a bot holding a controller, and a
cheap second opinion for a seat facing a yes/no it would otherwise guess.
"""
import json as _json

from bgate_core.board import queue as _queue
from bgate_core.qa import agentplay as _agentplay
from bgate_core.qa import feel as _feel
from bgate_mcp.server import _root, _tool, _work_item_id  # noqa: F401


def _budget() -> dict:
    """Count one paid call against the dispatched item's ceiling (the same
    seat-wide seam the image and music tools use), so a seat looping on
    `decide` or re-running bots stops at the item's budget. The human's own
    session has no item and is not counted."""
    return _queue.spend_paid_call(_root(), _work_item_id())


@_tool
def decide(state: str, questions: str, model: str = "") -> dict:
    """Ask the decision model typed questions about a state; get typed answers.

    `questions` is a JSON object {name: {type, instructions, criteria}}:
    type `noul` (yes/no, answers a probability), `choice` (criteria = list of
    names or {name: description}, up to 255; answers the pick, a probability
    per option and a confidence) or `score` (criteria = 2-10 ordered levels).
    About 250 ms and a hundredth of a cent a call. Use it for classification
    and judgement over text you already have - it does not write, plan or
    read files. Needs TYPESAFE_API_KEY or AI_GATEWAY_API_KEY
    (`bgate key set typesafe --global`), or BGATE_JEV_OLLAMA_MODEL naming a
    local Ollama decision model (laya, nimble, tev1) for a keyless, weaker
    route on the same endpoint; the result's `route` says which answered."""
    from bgate_adapters import jev

    spent = _budget()
    if not spent.get("ok"):
        return spent
    try:
        parsed = _json.loads(questions) if isinstance(questions, str) \
            else questions
    except ValueError as exc:
        return {"ok": False, "error": f"questions is not JSON: {exc}"}
    try:
        return jev.decide(state, parsed, root=_root(), model=model)
    except jev.JevError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def playtest_agent_run(goal: str, seconds: float = 60.0,
                       decide_every: int = 6, lockstep: bool = True,
                       policy: str = "jev", scene: str = "",
                       headless: bool = False, model: str = "",
                       max_usd: float = 0.10) -> dict:
    """A decision model PLAYS the game for `seconds` of game time; returns
    what it chose and what the game reported.

    Godot only. Installs addons/bgate/bgate_agent.gd + the BGateAgent autoload
    if missing (off unless the run sets BGATE_AGENT_URL). Every `decide_every`
    physics frames the game sends its state and options to a loopback relay;
    the relay asks Jev and the game holds the pick. `lockstep` pauses the game
    while a decision is in flight. `policy="random"` needs no key and proves
    the wiring. What the bot SEES is the game's `bgate_observe()` on a node in
    group "bgate_observer" ({state, options, goal}) - without one it sees a
    generic dump of the player and nearby enemy/boss/hazard/parryable nodes
    and is offered every non-ui input action, which plays badly. A good
    observer is the difference between a bot that plays and one that mashes.
    Output under .bgate/agentplay/<stamp>/: decisions.jsonl, telemetry.jsonl,
    game.log, summary.json. Capped: `max_usd` per run (default $0.10; a 45 s
    run measured $0.015, hard ceiling $1) and 1.5x the decisions `seconds`
    can need; a cap kills the game and `capped` says which."""
    spent = _budget()
    if not spent.get("ok"):
        return spent
    cap = float(max_usd) if float(max_usd) > 0 else 0.10
    try:
        return _agentplay.run(_root(), goal=goal, seconds=seconds,
                              decide_every=decide_every, lockstep=lockstep,
                              policy=policy, model=model, headless=headless,
                              scene=scene,
                              max_usd=min(cap, 1.0))
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def playtest_feel_check(scene: str, goal: str, seconds: float = 90.0) -> dict:
    """Is this scene FUN or only clearable? Races a Jev bot against a bot that
    holds the Jev bot's most-used option the whole run.

    FAILS (any of): ONE_BUTTON (one option > 60% of decisions), FLAT (the
    hold-one-option bot reaches >= 85% of the Jev bot's progress), NO_PRESSURE
    (the hold-one-option bot is threatened on < 20% of decisions), TRIVIAL
    (neither bot dies in a run that reached an end; a run that neither died
    nor ended inside `seconds` is a `notes` entry, not a fail), UNFAIR (3+
    deaths in one 5% of the scene), UNMEASURED (the game's bgate_observe()
    reports no `progress` 0..1 or `danger` bool). ONE_BUTTON counts only
    frames that offered more than one option. Leaving the slice and the
    release gate both require every checked scene to pass. A FAIL files a
    director item that WAITS FOR A PERSON; nothing dispatches it. Built after
    HARDCOVER passed every clearability gate and the human called it "a flat
    just hold click game". Two runs of `seconds` game time; about $0.03. For
    a turn-based game set `seconds` to a whole match or TRIVIAL is unjudged."""
    spent = _budget()
    if not spent.get("ok"):
        return spent
    try:
        return _feel.check(_root(), scene=scene, goal=goal, seconds=seconds)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def playtest_agent_install(refresh: bool = False) -> dict:
    """Install the BGateAgent autoload (addons/bgate/bgate_agent.gd) in this
    Godot project. Inert unless a playtest_agent_run launches the game.
    Additive: the game's own autoloads are kept, and so is the game's own
    copy of the script when it differs from the template (the result says
    so under `note`); `refresh=True` replaces it with the template."""
    try:
        return {"ok": True, **_agentplay.install(_root(), refresh=refresh)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def copy_check(lines: list = None, paths: list = None, context: str = "") -> dict:
    """Is this game text CORNY? Flags lines that are trying to be funny -
    zingers, twist endings ('X, not Y'), winks, taglines, accusatory tags -
    instead of sounding like the real thing the game depicts.

    Pass `lines` to check text you are about to write, or nothing to scan the
    project's own text (string literals that read as prose in src/ and
    scripts/, text values in content/ and dialogue/ JSON; `paths` are globs to
    scan instead). `context` names who is speaking ('an AI coding assistant's
    chat replies'). Lexical tells always; the decision model's 'trying to be
    funny' score (>= 0.6 flags) when a Jev key is set. A project passes with at
    most 5% of its lines flagged. Built after the human called agent-written
    game text "so corny". The rules every writing seat follows are in the
    result's `rules`."""
    from bgate_core.design import voice as _voice

    spent = _budget()
    if not spent.get("ok"):
        return spent
    found = list(lines or []) or _voice.scan(_root(), paths or ())
    return _voice.check(found, context=context, root=_root())
