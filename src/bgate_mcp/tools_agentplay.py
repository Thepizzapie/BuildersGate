"""Decision-model tools: `decide` for any seat, and bots that play the game.

The model is Jev (TypeSafe AI) through :mod:`bgate_adapters.jev`. It writes
nothing; it returns a typed answer - a choice, a score, a probability - over a
state. That makes it the right engine for a bot holding a controller, and a
cheap second opinion for a seat facing a yes/no it would otherwise guess.
"""
import json as _json

from bgate_core.board import queue as _queue
from bgate_core.qa import agentplay as _agentplay
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
    (`bgate key set typesafe --global`)."""
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
def playtest_agent_install() -> dict:
    """Install or refresh the BGateAgent autoload (addons/bgate/bgate_agent.gd)
    in this Godot project. Inert unless a playtest_agent_run launches the
    game. Additive: the game's own autoloads are kept."""
    try:
        return {"ok": True, **_agentplay.install(_root())}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
