"""Static contract tests over the node-editor ENGINE (frontend/public/nodecanvas.js).

The canvas is browser JS with no build step, so nothing in Python executes it —
a regression here is found by a user whose half-typed prompt got eaten. These
tests read the source and assert the contract that the rest of the UI is built
on:

  * the public API surface every host (wf.js, flow_*.js, world.js, flows.js)
    constructs against is still there, with every option it passes,
  * the FOCUS INVARIANT is intact: a node is patched, never replaced, and its
    body is only rewritten when it changed AND nothing inside it has focus,
  * the canvas affordances (minimap, collapse, resize, notes, marquee) exist and
    none of them repaints a node body,
  * the minimap is throttled through requestAnimationFrame — an overview that
    redraws per mousemove is a frame-rate bug, not a feature,
  * fit() keeps a readable zoom floor.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC_PATH = Path(__file__).resolve().parents[2] / "frontend" / "public" / "nodecanvas.js"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Prose documents the invariants; only code may satisfy them."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


CODE = _strip_comments(SRC)


def _method(name: str) -> str:
    """The body of a class method, brace-matched."""
    m = re.search(r"^\s*" + re.escape(name) + r"\s*\([^)]*\)\s*\{", CODE, re.M)
    assert m, f"{name}() is gone from nodecanvas.js"
    i = CODE.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(CODE):
        if CODE[j] == "{":
            depth += 1
        elif CODE[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return CODE[i:j + 1]


# --------------------------------------------------------------------------- #
# public surface — every host constructs against this
# --------------------------------------------------------------------------- #
PUBLIC_METHODS = ["mount", "setNodes", "addNode", "addEdge", "removeNode",
                  "removeEdge", "refreshNode", "select", "fit"]
PUBLIC_STATICS = ["NodeCanvas.w", "NodeCanvas.typeColor",
                  "NodeCanvas.typesCompatible", "NodeCanvas.esc"]
# options the existing hosts pass; dropping one silently breaks a whole flow
OPTIONS = ["nodes", "edges", "renderBody", "onSelect", "onConnect", "onNodeMove",
           "onWidget", "onReject", "onAction", "onNodeRemove", "accent",
           "panX", "panY", "zoom"]
WIDGETS = ["text", "number", "slider", "select", "toggle", "seed", "image", "note", "tag"]


@pytest.mark.parametrize("name", PUBLIC_METHODS)
def test_public_methods_exist(name):
    assert re.search(r"^\s*" + name + r"\s*\(", CODE, re.M), f"{name}() disappeared"


@pytest.mark.parametrize("name", PUBLIC_STATICS)
def test_public_statics_exported(name):
    assert name + " =" in CODE, f"{name} is no longer exported"


def test_engine_is_on_window():
    assert "window.NodeCanvas = NodeCanvas" in CODE


@pytest.mark.parametrize("opt", OPTIONS)
def test_every_host_option_is_still_read(opt):
    assert f"o.{opt}" in CODE, f"option {opt!r} is no longer read by the engine"


@pytest.mark.parametrize("name", WIDGETS)
def test_widget_helpers_intact(name):
    assert re.search(r"^\s*" + name + r"\s*\(", CODE, re.M), f"NodeCanvas.w.{name} disappeared"


def test_widgets_carry_the_isolation_marker():
    """`.nc-w` is what stops the canvas stealing a widget's pointer/wheel/keys."""
    assert CODE.count("nc-w ") + CODE.count('"nc-w"') >= 5
    assert 'closest(".nc-w")' in CODE


# --------------------------------------------------------------------------- #
# the focus invariant — the reason this engine exists
# --------------------------------------------------------------------------- #
def test_no_outerhtml_assignment_anywhere():
    """`el.outerHTML = html` is the old engine's sin: it forbids live widgets."""
    assert not re.search(r"outerHTML\s*=", CODE), "a node is being replaced, not patched"


def test_body_repaint_is_guarded_by_focus_and_change():
    body = _method("_renderNode")
    assert "body.contains(document.activeElement)" in body, "the focus guard is gone"
    assert re.search(r"if\s*\(!focused\s*&&\s*this\._paint\.get\([^)]*\)\s*!==\s*html\)", body), \
        "the body is no longer gated on (not focused AND actually changed)"
    assert body.count("body.innerHTML") == 1, \
        "the body is written outside the focus guard"


def test_refresh_node_can_skip_the_body():
    assert "opts.body !== false" in _method("_renderNode")


def test_affordances_never_touch_a_node_body():
    """Collapse, resize and the minimap repaint — none may rewrite a body."""
    for name in ("toggleCollapse", "_startResize", "_drawMap", "_paintSel", "_paintMarquee"):
        m = _method(name)
        assert "innerHTML" not in m, f"{name}() rewrites HTML and can eat a focused widget"


def test_collapse_and_resize_are_style_and_class_only():
    col = _method("toggleCollapse")
    assert "classList.toggle" in col
    move = _method("_onMove")
    assert "el.style.width" in move, "resize must set a style, not re-render"


# --------------------------------------------------------------------------- #
# minimap
# --------------------------------------------------------------------------- #
def test_minimap_exists_and_is_opt_out_only():
    assert "_bindMinimap" in CODE and "nc-map" in CODE
    assert "o.minimap === false" in CODE, "the minimap must be suppressible by hosts"


def test_minimap_is_throttled_through_raf():
    sched = _method("_scheduleMap")
    assert "requestAnimationFrame" in sched
    assert "this._mraf" in sched, "the minimap needs its own rAF latch"
    assert re.search(r"if\s*\([^)]*this\._mraf\)\s*return", sched), \
        "a second schedule inside one frame must be dropped"


def test_mousemove_schedules_the_map_instead_of_drawing_it():
    move = _method("_onMove")
    assert "_drawMap" not in move, "the minimap must not redraw synchronously per mousemove"
    assert "_scheduleMap" in move


def test_minimap_is_a_canvas_not_dom_blocks():
    assert "nc-map-c" in CODE and 'getContext("2d")' in CODE


# --------------------------------------------------------------------------- #
# collapse / notes / resize / selection
# --------------------------------------------------------------------------- #
def test_collapsed_node_keeps_its_edges():
    pos = _method("_portPos")
    assert "collapsed" in pos, "collapsed edges are not rerouted — links would orphan"
    assert ".nc-head" in pos


def test_collapsed_flag_persists_through_a_callback():
    col = _method("toggleCollapse")
    assert "n.collapsed" in col and "_changed" in col
    ch = _method("_changed")
    assert "onNodeChange" in ch and "onNodeMove" in ch, \
        "hosts without onNodeChange must still hear about a structural edit"


def test_note_nodes_are_portless_and_editable():
    r = _method("_renderNode")
    assert 'n.kind === "note"' in r
    note = _method("_noteBody")
    assert "textarea" in note and 'data-w="text"' in note
    assert "NodeCanvas.noteNode" in CODE


def test_note_sits_behind_the_graph():
    assert re.search(r"\.nc-note-node\{[^}]*z-index:0", CODE)
    assert re.search(r"\.nc-world \.nc-node\{z-index:1", CODE)


def test_resize_writes_width_onto_the_node():
    move = _method("_onMove")
    assert "n.w = " in move and "n.h = " in move
    up = _method("_onUp")
    assert '_changed(n, "resize")' in up


def test_marquee_and_multiselect():
    md = _method("_bindCanvas")
    assert "_startMarquee" in md
    assert "e.shiftKey" in md, "shift-click must extend the selection"
    assert "e.button === 2" in md, "right-drag must box-select"
    assert "this._panning = {" in md, "plain drag must still pan"
    assert "selectMany" in CODE and "this.selection" in CODE


def test_delete_removes_the_whole_selection():
    assert re.search(r"ids\s*=\s*this\.selection\.size\s*\?", CODE)
    assert "INPUT|TEXTAREA|SELECT" in CODE, "delete must not fire while typing in a widget"


# --------------------------------------------------------------------------- #
# fit
# --------------------------------------------------------------------------- #
def test_fit_has_a_readable_floor_and_prefers_the_selection():
    fit = _method("fit")
    assert "this.selection.size" in fit, "fit() ignores the selection"
    m = re.search(r"floor\s*=\s*o\.min != null \? o\.min : \(this\.o\.fitMin != null \? this\.o\.fitMin : ([\d.]+)\)", fit)
    assert m, "fit() lost its configurable zoom floor"
    assert float(m.group(1)) >= 0.5, f"fit floor {m.group(1)} is still unreadable"
    assert "Math.max(floor," in fit


def test_fit_takes_no_required_arguments():
    """Every host calls nc.fit() bare."""
    assert re.search(r"^\s*fit\(opts\)\s*\{", CODE, re.M)


# --------------------------------------------------------------------------- #
# it has to parse
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")
def test_source_parses():
    r = subprocess.run([shutil.which("node"), "--check", str(SRC_PATH)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
