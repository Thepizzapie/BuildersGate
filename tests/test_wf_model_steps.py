"""Static contract tests over the MODEL-COMPARISON steps (browser JS).

`bgate_ui/static/wf_steps_model.js` is where the user drives the schedule: fan
one prompt into several models, run just those cards, look at the candidates,
pick one. None of that is reachable from Python at runtime — the step registry
lives in the browser — so the contract is asserted against the source:

  * the model card selects a TIER (draft/standard/hero) and a task kind; it does
    NOT ship a model catalogue. A second copy of the ladder in the browser drifts
    from the one the server charges from, and the whole point of tiers is that a
    user never has to learn model names;
  * no price literal appears in the JS. Prices come from the server (the tier
    ladder / the imagegen table via /api/node/media); a number typed here is a
    number that can lie about what a run will cost;
  * the ports are typed the way the graph is wired — a PROMPT wire out of the
    prompt writer into every model card, a REF anchor in, an IMAGE out;
  * per-node run travels the canvas's existing [data-wact] channel (nodecanvas
    routes those to onAction) rather than inventing an event path;
  * the pick node actually renders candidates and can reject all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bgate_core import tiers

STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
MODEL_FILE = STATIC / "wf_steps_model.js"
WF_FILE = STATIC / "wf.js"

SRC = MODEL_FILE.read_text(encoding="utf-8")
WF_SRC = WF_FILE.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


CODE = _strip_comments(SRC)
WF_CODE = _strip_comments(WF_SRC)


def _blocks(src: str, call: str) -> list[str]:
    """Every `WF.<call>({ ... })` argument object, brace-matched."""
    out = []
    for m in re.finditer(re.escape("WF." + call) + r"\(\s*\{", src):
        i = src.index("{", m.end() - 1)
        depth, j = 0, i
        while j < len(src):
            ch = src[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i:j + 1])
    return out


def _ports(block: str) -> dict[str, dict[str, str]]:
    tail = block[block.index("ports"):]
    sides: dict[str, dict[str, str]] = {"in": {}, "out": {}}
    for side in ("in", "out"):
        m = re.search(r"\b" + side + r":\s*\[([^\]]*)\]", tail)
        if not m:
            continue
        for pm in re.finditer(r"\{([^}]*)\}", m.group(1)):
            body = pm.group(1)
            pid = re.search(r'id:\s*"(\w+)"', body)
            ptype = re.search(r'type:\s*"([\w*]+)"', body)
            if pid:
                sides[side][pid.group(1)] = ptype.group(1) if ptype else ""
    return sides


def _section(block: str, start: str, stop: str) -> str:
    i = block.find(start)
    if i < 0:
        return ""
    j = block.find(stop, i + len(start))
    return block[i:j if j > 0 else len(block)]


STEPS = {}
for _block in _blocks(CODE, "registerStep"):
    _m = re.search(r'type:\s*"([\w.]+)"', _block)
    assert _m, "registerStep with no type in wf_steps_model.js"
    STEPS[_m.group(1)] = {"src": _block, "ports": _ports(_block)}

TEMPLATES = []
for _block in _blocks(CODE, "registerTemplate"):
    _tid = re.search(r'id:\s*"([\w.]+)"', _block)
    TEMPLATES.append({
        "id": _tid.group(1) if _tid else "?",
        "nodes": dict(re.findall(r'\{\s*id:\s*"(\w+)",\s*type:\s*"([\w.]+)"', _block)),
        "edges": re.findall(
            r'from:\s*\[\s*"(\w+)"\s*,\s*"(\w+)"\s*\]\s*,\s*to:\s*\[\s*"(\w+)"\s*,\s*"(\w+)"\s*\]',
            _block),
    })

# base steps the template also wires to live in wf.js
BASE_PORTS = {}
for _block in _blocks(WF_CODE, "registerStep"):
    _m = re.search(r'type:\s*"([\w.]+)"', _block)
    if _m:
        BASE_PORTS[_m.group(1)] = _ports(_block)


# --------------------------------------------------------------------------- #
# the three nodes exist
# --------------------------------------------------------------------------- #
def test_the_registered_steps():
    """`llm.prompt` was removed on purpose: input.task already carries shared
    text to every card on the same wire, and each card has its own prompt
    field, so a node whose only job was to fill in a text box added a wire and
    a run lifecycle to a solved problem.

    input.bible / input.lore earn their place differently: they carry world
    context that exists nowhere on the canvas, resolved at run time."""
    assert set(STEPS) == {"model.image", "control.pick",
                          "input.bible", "input.lore"}, sorted(STEPS)


@pytest.mark.parametrize("step", ["input.bible", "input.lore"])
def test_world_context_rides_the_prompt_wire(step):
    """No new plumbing: anything that accepts a prompt accepts world context."""
    assert STEPS[step]["ports"]["out"]["o"] == "prompt"
    assert STEPS[step]["ports"].get("in", {}) == {}, "a context source has no inputs"


def test_context_nodes_do_not_bake_a_copy_of_the_bible():
    """They name what they want; the engine resolves it when the run happens,
    so editing the bible changes the next run."""
    for step in ("input.bible", "input.lore"):
        src = STEPS[step]["src"]
        assert "fetch(" not in src, f"{step} reads the bible in the browser"


# --------------------------------------------------------------------------- #
# tiers, not a model catalogue
# --------------------------------------------------------------------------- #
def test_model_node_selects_a_tier_and_a_kind():
    body = _section(STEPS["model.image"]["src"], "body", "config")
    assert 'w.select(n, "tier"' in body, "the model card has no tier selector"
    assert 'w.select(n, "task_kind"' in body, "the model card cannot say what it is making"
    defaults = re.search(r"defaults:\s*\{([^}]*)\}", STEPS["model.image"]["src"])
    assert defaults, "the model card declares no defaults"
    keys = set(re.findall(r"(\w+)\s*:", defaults.group(1)))
    # exactly the names bgate_core.generate.plan() reads — a key that drifts
    # here is a knob the engine never sees
    assert {"task_kind", "tier", "prompt", "count", "seed", "model", "provider"} <= keys, keys


def test_tier_options_come_from_the_server_not_this_file():
    """The rungs are read back from WF (the tier endpoint), never enumerated."""
    for fn in ("WF.tierLadder(", "WF.tierResolve(", "WF.tierKinds("):
        assert fn in CODE, f"{fn} is how the ladder is read; the model card must use it"
    for literal in tiers.TIERS:
        assert f'"{literal}"' not in CODE, (
            f'the tier name "{literal}" is written into the JS — the ladder is the '
            "server's, and a rung list copied here drifts from the one that runs")


@pytest.mark.parametrize("model", sorted(
    {m for ladder in tiers.LADDERS.values() for (_p, m) in ladder.values()}))
def test_no_model_name_is_hardcoded(model):
    assert model not in SRC, (
        f"{model!r} is named in wf_steps_model.js — the user must never have to know "
        "model names, and a catalogue in the browser goes stale silently")


@pytest.mark.parametrize("provider", sorted(
    {p for ladder in tiers.LADDERS.values() for (p, _m) in ladder.values()}))
def test_no_provider_name_is_hardcoded(provider):
    assert not re.search(r'["\']' + re.escape(provider) + r'["\']', SRC), (
        f"provider {provider!r} is written into the JS as a literal")


def test_no_price_literals_in_the_js():
    """Every number a card shows about money comes from the backend."""
    assert not re.search(r"\$\s*\d", CODE), "a dollar amount is written into the JS"
    decimals = re.findall(r"(?<![\w.])\d+\.\d+(?![\w.])", CODE)
    assert not decimals, f"numeric literals that could be prices: {decimals}"
    assert "WF.fmtUsd(" in CODE, "money must be formatted through WF, from server figures"


def test_flat_rungs_are_marked_not_sold_as_upgrades():
    """A tier resolving to the model below it must be labelled, and must not be
    offered as a distinct card by the compare fan-out."""
    assert ".flat" in CODE, "flat rungs are never consulted"
    assert re.search(r"filter\(\s*r\s*=>\s*!r\.flat", CODE), \
        "the compare fan-out would clone a card that calls the same model"


def test_the_ladder_being_unavailable_degrades_readably():
    assert "WF.tiersReady()" in CODE and "WF.tiersError()" in CODE, \
        "a missing tier endpoint must be reported on the card, not left blank"


# --------------------------------------------------------------------------- #
# ports
# --------------------------------------------------------------------------- #
EXPECTED_TYPES = {
    ("model.image", "in", "prompt"): "prompt",
    ("model.image", "in", "ref"): "ref",
    ("model.image", "out", "image"): "image",
    ("control.pick", "in", "candidates"): "image",
    ("control.pick", "out", "chosen"): "image",
}


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_TYPES.items()))
def test_port_types(key, expected):
    step, side, port = key
    ports = STEPS[step]["ports"][side]
    assert port in ports, f"{step} lost its {side} port {port!r} (has {sorted(ports)})"
    assert ports[port] == expected, \
        f"{step}.{side}:{port} is typed {ports[port]!r}, expected {expected!r}"


def test_one_task_feeds_every_model_card():
    """The whole pattern: ONE prompt reaching several models unchanged. Different
    words would compare the words, not the models."""
    compare = next(t for t in TEMPLATES if t["id"] == "tpl.compare")
    fed = [e for e in compare["edges"] if e[3] == "prompt"]
    assert len(fed) >= 2, "a comparison needs the prompt on more than one card"
    assert len({e[0] for e in fed}) == 1,         "every model card must be fed from the SAME source"


def test_the_prompt_can_be_improved_in_place():
    """The useful half of the deleted node, as a button on the field it edits."""
    assert 'data-wact="improve"' in STEPS["model.image"]["src"]
    assert "/api/prompt/expand" in SRC, "the improve button calls no endpoint"


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda t: t["id"])
def test_template_wiring_typechecks(tpl):
    assert tpl["nodes"] and tpl["edges"], f"template {tpl['id']} parsed nothing"
    ports = dict(BASE_PORTS)
    ports.update({k: v["ports"] for k, v in STEPS.items()})
    for src_node, src_port, dst_node, dst_port in tpl["edges"]:
        for alias in (src_node, dst_node):
            assert alias in tpl["nodes"], f"{tpl['id']}: unknown node {alias!r}"
        src_type, dst_type = tpl["nodes"][src_node], tpl["nodes"][dst_node]
        assert src_type in ports, f"{tpl['id']}: unknown step {src_type}"
        assert dst_type in ports, f"{tpl['id']}: unknown step {dst_type}"
        out_ports, in_ports = ports[src_type]["out"], ports[dst_type]["in"]
        assert src_port in out_ports, f"{tpl['id']}: {src_type} has no out {src_port!r}"
        assert dst_port in in_ports, f"{tpl['id']}: {dst_type} has no in {dst_port!r}"
        a, b = out_ports[src_port], in_ports[dst_port]
        assert not a or not b or a.lower() == b.lower(), (
            f"{tpl['id']}: {src_type}.{src_port} ({a}) cannot connect to "
            f"{dst_type}.{dst_port} ({b}) — nodecanvas refuses this edge")


# --------------------------------------------------------------------------- #
# per-node run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("step", ["model.image"])
def test_per_node_run_uses_the_canvas_action_channel(step):
    src = STEPS[step]["src"]
    assert 'data-wact="run"' in src or "runRow(" in src, \
        f"{step} has no per-node run affordance"
    assert "WF.runNode(" in src, f"{step} does not run itself through WF.runNode"
    assert "addEventListener" not in src, \
        f"{step} invents its own event path instead of using [data-wact]"


def test_run_button_markup_exists_once_in_the_shared_row():
    assert 'data-wact="run"' in CODE
    assert 'data-wact="compare"' in CODE, "the compare fan-out has no affordance"


def test_wf_routes_unknown_actions_to_the_step():
    """nodecanvas already routes [data-wact]; wf.js must hand the ones it does
    not own (run / pick / compare) to the step definition."""
    action = _section(WF_CODE, "_nodeAction(n, action, field)", "_toCanvasNode")
    assert "def.onAction" in action, \
        "wf.js swallows every action that is not a stepper — per-node run cannot arrive"


def test_wf_calls_the_documented_run_and_pick_endpoints():
    assert "/nodes/${encodeURIComponent(nodeId)}/run" in WF_CODE
    assert "/nodes/${encodeURIComponent(nodeId)}/pick" in WF_CODE
    # a route this build does not have must read as a missing route
    assert "__status === 404" in WF_CODE


def test_running_is_reflected_in_node_state():
    assert 'status = "running"' in WF_CODE, \
        "a node that is running must say so on the card"


# --------------------------------------------------------------------------- #
# picking
# --------------------------------------------------------------------------- #
def test_pick_node_renders_candidates():
    body = _section(STEPS["control.pick"]["src"], "body", "config")
    assert "candidates(n)" in body, "the pick node renders no candidate strip"
    assert 'data-wact="pick"' in body, "candidates are not clickable"
    assert "<img src=" in body, "candidates are not shown as pictures"
    assert "WF.candidatesFor(" in CODE, \
        "candidates must come from the run's own list — the only one a pick accepts"


def test_pick_node_can_reject_everything():
    src = STEPS["control.pick"]["src"]
    assert 'data-wact="reject"' in src, "there is no way to reject every candidate"
    assert "WF.pickCandidate(n.id, \"\")" in src, \
        "rejecting everything must reach the server as an empty pick"


def test_pick_node_shows_the_winner():
    body = _section(STEPS["control.pick"]["src"], "body", "config")
    assert "won" in body and "chosen:" in body, "the pick node never says which candidate won"


def test_pick_node_is_a_pick_not_a_bare_gate():
    """Approving says a human was happy; picking says WHICH — only the second
    hands the next step a value to consume."""
    assert 'kind: "pick"' in STEPS["control.pick"]["src"], \
        "a picker that resolves to nothing is a decoration"


def test_candidates_fall_back_to_upstream_model_nodes():
    """Before a run exists there is no server list; the node still shows what
    its upstream cards hold rather than rendering an empty box."""
    assert "upstreamNodes(" in CODE and "WF.nodeArtifacts(" in CODE, \
        "candidates must fall back to the upstream model cards' own output"


def test_model_card_is_a_generate_step():
    """The engine calls the provider for this node itself — that is what makes
    'run just this card' quick enough to compare with, and why the card carries
    no seat and no brief (a brief would be handed over AS the prompt)."""
    src = STEPS["model.image"]["src"]
    assert 'kind: "generate"' in src, "the model card is not a generate step"
    assert "agentSeat" not in src, "a generate node must not also claim a seat"
    assert "toBrief" not in src, \
        "a brief on a generate node would reach the provider as the prompt"
