"""The sprite editor's hot wheel: Ctrl+right-click puts the rail at the cursor.

These are source assertions, not behaviour tests — there is no JS runner in this
suite. They exist because every one of them guards a property that is invisible
until a human is mid-stroke and it is already wrong:

  * the angles are the whole feature. A wheel whose contents move is a wheel you
    have to read, which is a slower sidebar.
  * right-drag has panned this canvas since it shipped. The wheel is a SECOND
    gesture on an already-bound button, and the ordering of two `if`s in one
    pointerdown handler is the only thing keeping the pan alive.
  * a panel floating over pixel art has to be opaque to be legible, and this
    codebase has reached for --surface-N by mistake three times.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "frontend" / "public"
SE = STATIC / "spriteedit.js"


@pytest.fixture(scope="module")
def js() -> str:
    return SE.read_text(encoding="utf-8")


def _wheel_table(js: str) -> list[tuple[int, str, str]]:
    """The `const WHEEL = [...]` table, as (angle, kind, id) in source order."""
    block = re.search(r"const WHEEL = \[(.*?)\n  \];", js, re.S)
    assert block, "the WHEEL table is gone or was reshaped"
    rows = re.findall(
        r'\{a:\s*(\d+),\s*kind:"(\w+)",\s*id:"(\w+)"', block.group(1))
    return [(int(a), k, i) for a, k, i in rows]


# ---------------------------------------------------------------------------
# The angles ARE the feature
# ---------------------------------------------------------------------------
def test_twelve_slots_thirty_degrees_apart_and_never_renumbered(js):
    rows = _wheel_table(js)
    assert len(rows) == 12, "the wheel is twelve fixed slots"
    assert [a for a, _, _ in rows] == list(range(0, 360, 30)), (
        "slots must sit at 0,30,...,330 in that order — the render walks this "
        "array and the aim maths rounds an angle to an index, so a reorder "
        "here silently moves every entry under the user's hand")
    assert "WHEEL_SLOTS = 12" in js and "WHEEL_STEP  = 360 / WHEEL_SLOTS" in js


def test_the_tools_keep_the_left_rails_own_order(js):
    """A hand that knows the rail already knows the wheel. That only holds while
    the wheel's first seven slots are the rail top-to-bottom."""
    rail = re.findall(r'\{id:"(\w+)",\s+i:"', js)[:7]
    assert rail == ["pencil", "eraser", "bucket", "picker", "line", "rect",
                    "anchor"], "the TOOLS table changed; re-read this test"
    wheel_tools = [i for _, k, i in _wheel_table(js) if k == "tool"]
    assert wheel_tools == rail
    assert [a for a, k, _ in _wheel_table(js) if k == "tool"] == [
        0, 30, 60, 90, 120, 150, 180], "tools own the right half, 12 to 6"


def test_the_five_options_are_the_mid_stroke_ones_and_own_the_left_half(js):
    acts = [(a, i) for a, k, i in _wheel_table(js) if k == "act"]
    assert acts == [(210, "undo"), (240, "brush"), (270, "colour"),
                    (300, "grid"), (330, "onion")]


def test_setup_only_controls_stayed_out_of_the_wheel(js):
    """detect / de-halo / cell sizing are things you do once per sheet. A wheel
    that carries everything is a sidebar you have to aim at."""
    block = re.search(r"const WHEEL = \[(.*?)\n  \];", js, re.S).group(1)
    for banned in ("detectGrid", "dehalo", "applyGrid", "applyCells",
                   "exportFrames", "saveRig", "regenerate", "spreadLabels"):
        assert banned not in block, f"{banned} is setup, not a mid-stroke reach"


def test_an_unavailable_entry_dims_in_place_and_never_closes_the_gap(js):
    """Removing a slot renumbers every slot after it. Dimming does not."""
    assert ".se-wi.off{opacity:" in js
    assert "o.off = !S.undo.length" in js
    assert js.count("o.off = !multi") == 2, "grid and onion both need one frame"
    # wheelItems() maps the whole table, so the count is structurally fixed.
    assert "return WHEEL.map(s => wheelFace(s));" in js
    assert "return WHEEL.map((s, i) =>" in js, "the colour page keeps 12 slots"


# ---------------------------------------------------------------------------
# The pan this is bolted onto
# ---------------------------------------------------------------------------
def test_ctrl_right_is_tested_before_the_pan_branch(js):
    """Right-drag pans. If the pan branch runs first, Ctrl+right pans too and
    the wheel never opens; if setPointerCapture runs first, the wheel loses the
    move/up stream it aims with. Both are ordering, not logic."""
    down = js.index('v.addEventListener("pointerdown"')
    wheel = js.index("wheelOpen(ev.clientX, ev.clientY)", down)
    capture = js.index("v.setPointerCapture(ev.pointerId)", down)
    pan = js.index("S.drag = {pan:true", down)
    assert down < wheel < capture < pan


def test_right_without_ctrl_still_reaches_the_pan_untouched(js):
    assert "if (ev.button === 2 && (ev.ctrlKey || ev.metaKey)){" in js
    assert ("if (ev.button === 1 || ev.button === 2 || ev.altKey || S.space){"
            in js), "middle, plain right, alt and space must still pan"


# ---------------------------------------------------------------------------
# Getting out again
# ---------------------------------------------------------------------------
def test_escape_closes_the_wheel_before_it_closes_the_editor(js):
    """Escape in this editor means close(). With a wheel up it must mean close
    the wheel — and it has to be tested above the INPUT guard, because the
    wheel can be open while focus is still in a sidebar field."""
    key = js.index("function onKey(ev){")
    wheel_esc = js.index('if (S.wheel && ev.key === "Escape")', key)
    input_guard = js.index('t.tagName === "INPUT"', key)
    editor_esc = js.index('if (ev.key === "Escape"){ ev.preventDefault(); close()', key)
    assert key < wheel_esc < input_guard < editor_esc


def test_the_wheel_is_torn_down_with_the_editor(js):
    """Its listeners live on window, not on the DOM close() removes."""
    close = js.index("async function close(silent){")
    assert js.index("wheelClose();", close) < js.index(
        'const back = document.getElementById("se-back");', close)


def test_every_listener_the_wheel_adds_is_removed(js):
    added = set(re.findall(r'window\.addEventListener\("(\w+)", (onWheel\w+)', js))
    removed = set(re.findall(r'window\.removeEventListener\("(\w+)", (onWheel\w+)', js))
    assert added and added == removed


def test_cancel_is_reachable_three_ways(js):
    assert "if (i < 0) wheelClose(); else wheelPick(i);" in js          # hub release
    assert "> w.R + w.btn) return wheelClose();" in js                  # clicked away
    assert 'if (S.wheel && ev.key === "Escape")' in js                  # escape


def test_a_drag_out_and_back_is_a_cancel_not_a_tap(js):
    """Measuring only the final delta reads "changed my mind" as "show me the
    wheel" and leaves it up. The gesture's furthest point is what decides."""
    assert "w.moved = Math.max(w.moved," in js
    assert "if (w.moved < 6 && Date.now() - w.t0 < 320)" in js


# ---------------------------------------------------------------------------
# Orbit: over pixel art, anything with text is opaque
# ---------------------------------------------------------------------------
def test_the_wheel_carries_text_on_solid_not_surface(js):
    for rule in (".se-whub{", ".se-wlab{", ".se-wi{"):
        i = js.index(rule)
        chunk = js[i:js.index("}", i)]
        assert "var(--solid-" in chunk, f"{rule} is translucent over pixel art"
        assert "var(--surface-" not in chunk, (
            f"{rule} uses --surface-N; over a sprite sheet that is unreadable")


def test_the_wheel_uses_only_custom_properties_for_colour(js):
    block = js[js.index('".se-wheelwrap{'):js.index('".se-wsw{')]
    for lit in re.findall(r"#[0-9a-fA-F]{3,8}\b", block):
        pytest.fail(f"hard-coded colour {lit} in the wheel's CSS")


def test_the_wheel_ships_in_spriteedits_own_style_block(js):
    """spriteedit.js injects its own <style>; several agents are live in
    app.css and index.html and the wheel must not need either."""
    assert '".se-wheelwrap{' in js
    assert '"se-wheel"' in js
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "se-wheel" not in css and "se-wheel" not in html


# ---------------------------------------------------------------------------
# It is a second path, never the only one
# ---------------------------------------------------------------------------
def test_nothing_on_the_wheel_is_reachable_only_from_the_wheel(js):
    for _, _, entry in _wheel_table(js):
        if entry in ("pencil", "eraser", "bucket", "picker", "line", "rect",
                     "anchor"):
            assert f'id:"{entry}"' in js, "the rail still carries every tool"
    assert 'onclick="SpriteEdit.undo()"' in js                    # title bar
    assert 'oninput="SpriteEdit.setBrush(this.value)"' in js      # sidebar
    assert 'oninput="SpriteEdit.setColor(this.value)"' in js      # sidebar
    assert "SpriteEdit.toggle('showGrid',this.checked)" in js     # sidebar
    assert 'onchange="SpriteEdit.setOnion(this.value)"' in js     # sidebar
    assert 'if (tool && !ev.ctrlKey && !ev.metaKey){ setTool(tool.id); }' in js


def test_the_wheel_never_escapes_an_embedded_host(js):
    """The editor also mounts inside the art seat. The overlay is parented to
    .se-back, which in that mode is the seat's clipped box."""
    assert "$.back.appendChild(wrap);" in js
    assert '".se-wheelwrap{position:absolute;inset:0' in js
    assert "Math.min(r.right, window.innerWidth || r.right)" in js
    assert "Math.min(r.bottom, window.innerHeight || r.bottom)" in js


def test_aim_is_angle_only_so_a_shrunk_wheel_keeps_its_directions(js):
    """In a narrow seat pane the radius drops to the floor. Muscle memory is
    direction, so the hit test must not read distance for anything but the
    cancel zone."""
    fn = js[js.index("function wheelAim(x, y){"):]
    fn = fn[:fn.index("\n  }")]
    assert "Math.round(th / WHEEL_STEP) % WHEEL_SLOTS" in fn
    assert fn.count("Math.hypot") == 1, "distance decides cancel, nothing else"
    assert "WHEEL_R_MAX = 108, WHEEL_R_MIN = 68" in js


def test_everything_reaching_innerhtml_is_escaped(js):
    build = js[js.index("function wheelRender(){"):js.index("function wheelHot(")]
    for interp in re.findall(r"\$\{([^}]+)\}", build):
        if "aria-label=" in build[:build.index(interp)][-24:] or "it.lab" in interp:
            assert interp.strip().startswith("E("), f"unescaped: {interp}"
    face = js[js.index("function wheelFace(s){"):js.index("function wheelOpen(")]
    assert "background:${E(S.color)}" in face
    items = js[js.index("function wheelItems(){"):js.index("function wheelFace(")]
    assert "background:${E(c)}" in items
