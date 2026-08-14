"""The scene viewport opens at the size the player sees, and says which size that is.

WHY THIS FILE EXISTS. "props are still not scaled right in atlas" was reported
three times against a viewport whose geometry was, measured against the engine's
own frame, exact to the pixel:

    frame-to-frame registration    scale 1.000, offset (-1, 0) px
    floor lattice, both captures   128 x 64 screen px  (64x32 world at x2)
    DeskCrtSE_028, both captures   93 x 83 px          ratio 1.0000

What was wrong was the ZOOM and the label on it. The panel opened at `fit` — an
arbitrary 14% on a floor plate — and its HUD called `view.z == 1` "100%", which
on a project that stretches x2 is half the size the player sees. Every art call
made in that panel was made at the wrong size, and the readout agreed with
neither the engine nor the Godot editor.

None of that throws, none of it shows up in a screenshot of the panel, and the
fix is four small things in one file that a later edit can undo one at a time —
so each one is pinned here as a contract on the source rather than left to be
rediscovered by a fourth bug report.

Source-text assertions, deliberately: there is no JS runtime in this suite, and
a test that re-implements the maths in Python would pass while the browser drew
something else. What can honestly be checked from here is that the pieces still
refer to each other.
"""
from __future__ import annotations

import re
from pathlib import Path

SCENEVIEW = (Path(__file__).resolve().parents[1]
             / "frontend" / "public" / "sceneview.js")
SRC = SCENEVIEW.read_text(encoding="utf-8")


LINES = SRC.splitlines()


def _body(name: str) -> str:
    """The source of one function in the module IIFE.

    Sliced on indentation rather than by matching braces: this file is one
    2600-line closure full of template literals and CSS-in-strings, and a brace
    counter walks straight into them. Every function in it is declared at two
    spaces and closed by a line that is exactly `  }`.
    """
    head = re.compile(rf"^  (?:async )?function {re.escape(name)}\(")
    start = next((i for i, ln in enumerate(LINES) if head.match(ln)), None)
    assert start is not None, f"{name}() is gone from sceneview.js"
    end = next((i for i in range(start + 1, len(LINES))
                if LINES[i] == "  }"), None)
    assert end is not None, f"{name}() never closes at the module's indent"
    return "\n".join(LINES[start:end + 1])


# ---------------------------------------------------------------------------
# The opening view
# ---------------------------------------------------------------------------
def test_the_panel_opens_at_the_game_scale_not_at_fit():
    """`fit` is a button, not the default. A scene judged at 14% has not been
    judged: "is this prop too big", "does this light read", "is this sprite
    muddy" all have different answers at 14% than at the size it ships at."""
    mount = _body("mount")
    assert "openingView()" in mount
    assert not re.search(r"\bfit\(\);", mount), \
        "mount() must not fall back to fit() — that is the bug being fixed"


def test_the_opening_view_is_the_games_scale_about_the_content():
    opening = _body("openingView")
    assert "gameScaleOf()" in opening, \
        "the opening zoom is the game's factor, not 1 and not a fit"
    assert "contentBounds()" in opening, (
        "at x2 only a sixth of a floor plate is on screen, so the opening view "
        "has to be centred on the level rather than parked at its corner")


def test_fit_and_the_opening_view_frame_the_same_rectangle():
    """Two copies of the bounds walk is two rectangles that drift apart, and
    the drift shows up as `fit` and the opening view disagreeing about where
    the level is."""
    assert "contentBounds()" in _body("fit")


def test_a_lights_halo_does_not_define_the_framing():
    """A PointLight2D's box is its cookie times texture_scale — 400px of
    falloff around a 30px fitting. Forty of them dragged floor_tut's bounds out
    to roughly 1883x1746 around a level that ends at 1493x1184, which put the
    opening centre in a room the plate does not have."""
    bounds = _body("contentBounds")
    assert 'kind === "light"' in bounds and "return" in bounds


def test_the_opening_view_waits_for_the_panel_to_have_a_size():
    """The Atlas surface mounts this while its section is still display:none.
    A view computed against a 0x0 stage is a view of nothing — fit() used to do
    exactly that and clamp the zoom to its 5% floor."""
    opening = _body("openingView")
    assert "r.width" in opening and "r.height" in opening
    assert "viewReady" in opening
    assert "viewReady" in _body("_paint"), \
        "nothing else retries, so the paint loop has to"


# ---------------------------------------------------------------------------
# Remembering it
# ---------------------------------------------------------------------------
def test_the_view_is_remembered_per_scene():
    assert 'VIEW_KEY = "bgate-sceneview-view"' in SRC
    remember, restore = _body("rememberView"), _body("restoreView")
    assert "all[scene]" in remember and "localStorage.setItem" in remember
    assert "viewStore()[scene]" in restore, (
        "one entry per scene — a single shared view lands the next scene "
        "wherever the last one was panned to")
    assert "isFinite" in restore, \
        "a corrupt or hand-edited entry must not brick the panel"


def test_switching_scene_takes_the_new_scenes_view():
    assert "openingView()" in _body("setScene")


def test_remembering_is_debounced_because_it_runs_in_the_paint_loop():
    """A drag is sixty view changes a second and localStorage is synchronous."""
    remember = _body("rememberView")
    assert "setTimeout" in remember and "clearTimeout" in remember


# ---------------------------------------------------------------------------
# Saying which 100% it is
# ---------------------------------------------------------------------------
def test_the_hud_names_both_percentages():
    """A bare "100%" is the ambiguity itself: on a stretched project it is true
    of two different sizes and the reader cannot tell which is on screen."""
    paint = _body("_paint")
    assert "game ${" in paint and "editor ${" in paint
    assert re.search(r"view\.z\s*/\s*gs", paint), \
        "the game percentage is view.z relative to the game's factor"


def test_the_one_to_one_button_says_what_it_goes_to():
    """"1:1" names the wrong thing on a stretched project — one WHAT to one
    what? It goes to the size the player sees."""
    paint = _body("_paint")
    assert '"game" : "1:1"' in paint


def test_a_missing_scale_from_the_api_is_one_not_a_crash():
    """An older server does not send `scale` at all. The panel then behaves as
    an unstretched project rather than dividing by undefined — which is exactly
    what a dashboard running yesterday's Python does."""
    assert "isFinite(s) && s > 0 ? s : 1" in _body("gameScaleOf")
