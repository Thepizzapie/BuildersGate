"""Static contract tests over the workflow STEP DEFINITIONS (browser JS).

The step registry lives in the browser (frontend/public/wf_steps_*.js) and the
run compiles from it, so a broken step definition is not caught by any Python
test — it is caught by a user whose workflow silently runs against nothing.
These tests read the JS as source and assert the contract:

  * every registered step declares ports,
  * the parameters a step's brief reads are actually settable (a node widget or
    a default) — a brief key that drifted off the UI is a dead knob,
  * no step still renders the old inspector row helpers inside its node body
    (the node is the instrument now; those helpers are inspector-only),
  * and — the one that matters — every starter TEMPLATE's wiring still connects:
    each edge names ports that exist and whose declared types are compatible.
    Typed ports are enforced at connect time in nodecanvas.js, so a template
    that ships an incompatible edge ships a graph a user cannot rebuild.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
STEP_FILES = [
    STATIC / "wf_steps_asset.js",
    STATIC / "wf_steps_agent.js",
    STATIC / "wf_steps_world.js",
    STATIC / "wf_steps_3d.js",
]
# the base steps (input.task / input.reference / control.gate) that templates wire to
BASE_FILE = STATIC / "wf.js"

# Ports the graph is built around. A step may leave a port untyped (anything
# connects), but where a type IS declared it has to be this one, or two steps
# that are meant to meet stop meeting.
EXPECTED_TYPES = {
    ("art.concept", "in", "i"): "task",
    ("art.concept", "out", "o"): "image",
    ("art.anchor", "in", "task"): "task",
    ("art.anchor", "in", "ref"): "ref",
    ("art.anchor", "out", "o"): "image",
    ("art.animation", "in", "anchor"): "image",
    ("art.animation", "out", "o"): "frames",
    ("art.edit", "in", "frame"): "image",
    ("art.edit", "out", "o"): "image",
    ("output.sheet", "in", "frames"): "frames",
    ("output.sheet", "out", "o"): "sheet",
    ("agent.director", "in", "i"): "task",
    ("agent.director", "out", "o"): "task",
    ("agent.art", "in", "i"): "task",
    ("agent.art", "out", "o"): "image",
    ("agent.gameplay", "in", "i"): "task",
    ("agent.narrative", "out", "o"): "text",
    ("agent.audio", "out", "o"): "audio",
    ("world.background", "out", "o"): "image",
    ("world.parallax", "in", "i"): "image",
    ("world.parallax", "out", "o"): "image",
    ("3d.concept", "in", "i"): "task",
    ("3d.concept", "out", "o"): "image",
    ("3d.model", "in", "i"): "image",
    ("3d.model", "out", "o"): "model",
    ("3d.gltf", "in", "i"): "model",
    ("3d.gltf", "out", "o"): "gltf",
    ("3d.sprites", "in", "i"): "model",
    ("3d.sprites", "out", "o"): "sheet",
    ("3d.import", "in", "i"): "gltf",
    ("3d.import", "out", "o"): "asset",
    ("3d.verify", "in", "i"): "asset",
}

# The type vocabulary. A typo ("frame" for "frames") is a silently unwireable
# graph, so the set is closed.
VOCABULARY = {"task", "text", "prompt", "ref", "image", "sheet", "frames",
              "asset", "model", "gltf", "audio"}

# inspector-only row builders — none of these may appear in a node body
OLD_ROW_HELPERS = ("numRow", "textRow", "selRow", "boolRow", "rowNum", "rowText", "rowSel", "rowBool")

WIDGET_RE = re.compile(r'w\.(?:text|number|slider|select|toggle|seed)\(\s*n\s*,\s*"(\w+)"')
CFG_RE = re.compile(r'c(?:fg|v)\(\s*n\s*,\s*"(\w+)"')


# --------------------------------------------------------------------------- #
# tiny source scanner (no JS engine — these are assertions about the text)
# --------------------------------------------------------------------------- #
def _strip_comments(src: str) -> str:
    """Drop block comments and whole-line // comments.

    wf.js documents the step + template contract in a block comment that
    contains a literal `WF.registerStep({ type:"art.animation" ... })` — parsed
    naively that phantom overwrites the real step. Comments are prose here, so
    remove them before reading structure.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


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


def _step_type(block: str) -> str:
    m = re.search(r'type:\s*"([\w.]+)"', block)
    return m.group(1) if m else ""


def _ports(block: str) -> dict[str, dict[str, str]]:
    """{'in': {portId: type}, 'out': {...}} for one step block.

    A step with no ports() gets the engine default (wf.js _toCanvasNode): one
    untyped in "i" and one untyped out "o".
    """
    pm = re.search(r'ports(?:\(\))?\s*:?\s*(?:function\s*\(\)\s*)?(?:\(\))?\s*(?:=>)?\s*[\({]', block)
    if "ports" not in block:
        return {"in": {"i": ""}, "out": {"o": ""}}
    # the ports body runs from "ports" to the end of its returned object literal;
    # the in:[...] / out:[...] arrays hold no nested arrays, so slice them out.
    tail = block[block.index("ports"):]
    sides: dict[str, dict[str, str]] = {"in": {}, "out": {}}
    for side in ("in", "out"):
        m = re.search(r"\b" + side + r":\s*\[([^\]]*)\]", tail)
        if not m:
            continue
        for pm2 in re.finditer(r"\{([^}]*)\}", m.group(1)):
            body = pm2.group(1)
            pid = re.search(r'id:\s*"([\w]+)"', body)
            ptype = re.search(r'type:\s*"([\w*]+)"', body)
            if pid:
                sides[side][pid.group(1)] = ptype.group(1) if ptype else ""
    assert pm is not None
    return sides


def _section(block: str, start: str, stop: str) -> str:
    i = block.find(start)
    if i < 0:
        return ""
    j = block.find(stop, i + len(start))
    return block[i:j if j > 0 else len(block)]


def _defaults(block: str) -> set[str]:
    m = re.search(r"defaults:\s*\{([^}]*)\}", block)
    if not m:
        return set()
    return set(re.findall(r"(\w+)\s*:", m.group(1)))


def _load() -> tuple[dict[str, dict], list[dict]]:
    """(steps by type, templates) across the step files plus wf.js's base steps."""
    steps: dict[str, dict] = {}
    templates: list[dict] = []
    for path in STEP_FILES + [BASE_FILE]:
        src = _strip_comments(path.read_text(encoding="utf-8"))
        for block in _blocks(src, "registerStep"):
            t = _step_type(block)
            assert t, f"registerStep with no type in {path.name}"
            steps[t] = {"file": path.name, "src": block, "ports": _ports(block)}
        for block in _blocks(src, "registerTemplate"):
            tid = re.search(r'id:\s*"([\w.]+)"', block)
            nodes = dict(re.findall(r'\{\s*id:\s*"(\w+)",\s*type:\s*"([\w.]+)"', block))
            edges = re.findall(
                r'from:\s*\[\s*"(\w+)"\s*,\s*"(\w+)"\s*\]\s*,\s*to:\s*\[\s*"(\w+)"\s*,\s*"(\w+)"\s*\]',
                block)
            templates.append({"file": path.name, "id": tid.group(1) if tid else "?",
                              "nodes": nodes, "edges": edges})
    return steps, templates


STEPS, TEMPLATES = _load()


def _compatible(a: str, b: str) -> bool:
    """Mirror of nodecanvas.js typesCompatible()."""
    if not a or not b or a == "*" or b == "*":
        return True
    return a.lower() == b.lower()


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_step_files_all_parsed():
    assert len(STEPS) >= 27, f"only found {len(STEPS)} steps: {sorted(STEPS)}"
    for base in ("input.task", "input.reference", "control.gate"):
        assert base in STEPS


@pytest.mark.parametrize("step_type", sorted(STEPS))
def test_every_step_declares_ports(step_type):
    ports = STEPS[step_type]["ports"]
    assert ports["in"] or ports["out"], f"{step_type} declares no ports at all"


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_TYPES.items()))
def test_expected_port_types(key, expected):
    step_type, side, port_id = key
    assert step_type in STEPS, f"{step_type} is no longer registered"
    ports = STEPS[step_type]["ports"][side]
    assert port_id in ports, f"{step_type} lost its {side} port {port_id!r}"
    assert ports[port_id] == expected, (
        f"{step_type}.{side}:{port_id} is typed {ports[port_id]!r}, expected {expected!r}")


@pytest.mark.parametrize("step_type", sorted(STEPS))
def test_port_types_come_from_the_vocabulary(step_type):
    for side, ports in STEPS[step_type]["ports"].items():
        for pid, ptype in ports.items():
            if ptype and ptype != "*":
                assert ptype in VOCABULARY, (
                    f"{step_type}.{side}:{pid} declares unknown type {ptype!r}")


@pytest.mark.parametrize("step_type", sorted(t for t in STEPS if t.split(".")[0] in
                                             ("art", "agent", "world", "3d", "output", "control")))
def test_node_body_uses_widgets_not_inspector_rows(step_type):
    body = _section(STEPS[step_type]["src"], "body", "config")
    for helper in OLD_ROW_HELPERS:
        assert helper + "(" not in body, (
            f"{step_type} still renders the inspector helper {helper}() inside body()")


@pytest.mark.parametrize("step_type", sorted(STEPS))
def test_brief_config_keys_are_settable(step_type):
    """Every key toBrief() reads must be a node widget or a step default.

    toBrief is what the run actually dispatches; a key it reads that nothing can
    set is a parameter the user cannot reach.
    """
    block = STEPS[step_type]["src"]
    brief = _section(block, "toBrief", "\0")
    if not brief:
        return
    settable = set(WIDGET_RE.findall(block)) | _defaults(block)
    for key in set(CFG_RE.findall(brief)):
        assert key in settable, (
            f"{step_type}.toBrief reads config {key!r}, which is neither a node "
            f"widget nor a default (settable: {sorted(settable)})")


def test_templates_were_parsed():
    assert len(TEMPLATES) >= 7, [t["id"] for t in TEMPLATES]
    for tpl in TEMPLATES:
        assert tpl["nodes"], f"template {tpl['id']} parsed no nodes"
        assert tpl["edges"], f"template {tpl['id']} parsed no edges"


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda t: t["id"])
def test_template_wiring_typechecks(tpl):
    """Every template edge names real ports whose types can actually connect."""
    for src_node, src_port, dst_node, dst_port in tpl["edges"]:
        for alias in (src_node, dst_node):
            assert alias in tpl["nodes"], f"{tpl['id']}: edge names unknown node {alias!r}"
        src_type, dst_type = tpl["nodes"][src_node], tpl["nodes"][dst_node]
        assert src_type in STEPS, f"{tpl['id']}: unknown step type {src_type}"
        assert dst_type in STEPS, f"{tpl['id']}: unknown step type {dst_type}"
        out_ports = STEPS[src_type]["ports"]["out"]
        in_ports = STEPS[dst_type]["ports"]["in"]
        assert src_port in out_ports, (
            f"{tpl['id']}: {src_type} has no out port {src_port!r} (has {sorted(out_ports)})")
        assert dst_port in in_ports, (
            f"{tpl['id']}: {dst_type} has no in port {dst_port!r} (has {sorted(in_ports)})")
        a, b = out_ports[src_port], in_ports[dst_port]
        assert _compatible(a, b), (
            f"{tpl['id']}: {src_type}.{src_port} ({a or 'any'}) cannot connect to "
            f"{dst_type}.{dst_port} ({b or 'any'}) — nodecanvas refuses this edge")
