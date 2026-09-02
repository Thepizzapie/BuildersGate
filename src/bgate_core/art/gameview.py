"""The project's VIEW — the one declaration that decides what "correct" means.

A prop is not right or wrong on its own. A barrel showing its lid and a sliver
of side is correct for a top-down game and wrong for a platformer; a barrel
showing two side faces is correct for an isometric game and wrong for both of
the others. The same is true of the tile geometry, of which prop mounts even
exist, and of what "the level is playable" is checked against.

NOTHING IN THE PROJECT DECLARED THIS, and the consequence was measured rather
than theorised: a prop generation asked for "a high 3/4 top-down game view",
which to an image model means the standard three-quarter product render — near
ISOMETRIC. Every prop came back showing two side faces, to sit on a floor
tileset drawn flat top-down. The prompt was the proximate cause; the real cause
is that the view lived in one scratch prompt instead of in the project, so every
agent re-derived it and drifted.

So the view is declared once, here, and read by everyone:

  * PROP AND SPRITE GENERATION take their camera clause from `camera_clause`,
    including its negations. "NOT isometric" is in there because the model's
    default reading of "game prop" is isometric, and a clause that only says
    what it wants gets the default for everything it forgot to forbid.
  * THE TILESET takes its shape and layout from `tile_geometry` — square for
    top-down and side-scrolling, a 2:1 diamond for isometric.
  * THE LEVEL GENERATOR takes its `reachability` rule from here, and that is
    not cosmetic: under gravity, "every floor cell is one connected region" is
    not the question. "Can you jump from this platform to that one" is.
  * PROP PLACEMENT takes its legal mounts from here. A wall torch is furniture
    in a top-down room and a background decoration in a platformer; a ceiling
    mount is meaningless top-down and ordinary side-on.

Pure data and pure functions. Nothing here generates, draws or writes.
"""
from __future__ import annotations

import os

from ..store import workspace as _ws

SEAT = "director"
DOC_KEY = "game_view"

#: The three 2D views this pipeline supports, and the only legal values.
VIEWS = ("top_down", "side_scroller", "isometric")

DEFAULT_VIEW = "top_down"


class ViewError(ValueError):
    """A view that is not one of `VIEWS`, or a request that contradicts it."""


# ---------------------------------------------------------------------------
# The camera, stated the way the failure taught
# ---------------------------------------------------------------------------
#
# Each clause names the angle, names the CONSEQUENCE ("you see mostly the top
# and a sliver of the front"), and forbids the wrong reading BY NAME. The last
# part is what a first attempt leaves out and what it costs: an image model
# renders a "game prop" isometric unless told not to, and a clause that only
# describes what it wants inherits the default for everything it did not
# forbid.
_TOP_DOWN_GROUND = (
    "CAMERA: almost directly overhead, looking DOWN at the object with only a "
    "slight forward lean, like a classic top-down 2D SNES action-RPG. You see "
    "mostly the TOP surface and only a thin sliver of the front side. "
    "NOT isometric. NOT axonometric. NOT a three-quarter product render. "
    "NEVER show a left face and a right face at the same time."
)
_FLAT_OVERHEAD = (
    "CAMERA: seen PERFECTLY DIRECTLY ABOVE, a flat plan view with zero tilt "
    "and zero height, as if painted onto the ground. NOT isometric, no "
    "perspective, no thickness."
)
_WALL_FACE = (
    "CAMERA: seen straight on from the front, a completely FLAT elevation "
    "view, as it would look mounted facing the player — like a decal on a "
    "flat surface. NOT isometric. No perspective, no vanishing point, no "
    "depth, no receding surfaces. If the object has an opening, that opening "
    "is a FLAT dark shape: do NOT draw a tunnel, do NOT show anything through "
    "it, do NOT show its interior receding away."
)
_SIDE_ELEVATION = (
    "CAMERA: strict side view, a pure flat profile from the side, the way a "
    "2D side-scrolling platformer is drawn. The object sits on the ground line "
    "at the bottom of the frame. NOT isometric, NOT top-down, no perspective, "
    "and never show the top surface."
)
_ISOMETRIC = (
    "CAMERA: angled isometric view on 2:1 tile geometry — you see the top "
    "face and TWO side faces, left and right, meeting at a near vertical edge "
    "in the middle. NEVER flat top-down and NEVER a straight-on side view."
)

#: What each view IS, for everyone downstream.
#:
#:   camera     the prompt clause per prop mount, and a `default` for the rest
#:   tile       (shape, layout) for tilemap.write_tileset
#:   mounts     the prop mounts that mean anything in this view
#:   gravity    which way down is, or "" for none
#:   reach      how "is this level playable" is checked
#:   sprite_view  the matching `spritecontract` view, so a character sheet and
#:                the level it walks through cannot disagree
SPECS: dict[str, dict] = {
    "top_down": {
        "label": "top-down 2D",
        "camera": {"default": _TOP_DOWN_GROUND,
                   "overlay": _FLAT_OVERHEAD,
                   "wall": _WALL_FACE, "corner": _WALL_FACE,
                   "door": _WALL_FACE},
        "tile": ("square", "stacked"),
        "mounts": ("wall", "corner", "floor", "pillar", "overlay", "centre",
                   "door", "portal"),
        "gravity": "",
        "reach": "connected",
        "sprite_view": "top_down_3q",
        "note": "walk any direction; a level is playable when its floor is one "
                "connected region",
    },
    "side_scroller": {
        "label": "side-scrolling 2D",
        "camera": {"default": _SIDE_ELEVATION,
                   # a decal on a platformer's ground is still seen side-on,
                   # because there is no ground plane facing the camera
                   "overlay": _SIDE_ELEVATION,
                   "wall": _SIDE_ELEVATION, "door": _SIDE_ELEVATION},
        "tile": ("square", "stacked"),
        # no `corner` or `pillar`: a corner mount needs a wall you look along,
        # and a colonnade reads as depth, which this view does not have
        "mounts": ("floor", "ceiling", "wall", "overlay", "door", "portal"),
        "gravity": "s",
        "reach": "jumpable",
        "sprite_view": "side",
        "note": "gravity pulls down, so connectivity is NOT the question — "
                "reachability by jump arc is",
    },
    "isometric": {
        "label": "isometric 2D",
        "camera": {"default": _ISOMETRIC, "overlay": _ISOMETRIC},
        "tile": ("isometric", "diamond_down"),
        "mounts": ("wall", "corner", "floor", "pillar", "overlay", "centre",
                   "door", "portal"),
        "gravity": "",
        "reach": "connected",
        "sprite_view": "isometric",
        "note": "walk any direction, but everything carries a height and a "
                "y-sort anchor",
    },
}


def normalise(view: str | None) -> str:
    """A legal view, or a refusal naming the three that exist."""
    name = str(view or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not name:
        return DEFAULT_VIEW
    aliases = {"topdown": "top_down", "top": "top_down",
               "side": "side_scroller", "sidescroller": "side_scroller",
               "platformer": "side_scroller", "iso": "isometric"}
    name = aliases.get(name, name)
    if name not in VIEWS:
        raise ViewError(
            f"{view!r} is not a view this pipeline supports; the three are "
            f"{list(VIEWS)}")
    return name


def spec(view: str | None = None) -> dict:
    """Everything downstream needs, for one view."""
    return SPECS[normalise(view)]


def camera_clause(view: str | None, mount: str = "") -> str:
    """The prompt clause for generating art of this kind, in this view.

    Per MOUNT, because one view holds several cameras: a top-down game draws
    its floor props from overhead, its wall props on the wall's face, and its
    decals perfectly flat, and asking for one camera across all three is how a
    torch comes back with a slab of masonry attached.
    """
    cams = spec(view)["camera"]
    return cams.get(str(mount or "").strip().lower()) or cams["default"]


def tile_geometry(view: str | None = None) -> tuple[str, str]:
    """``(shape, layout)`` names for `tilemap.write_tileset`."""
    return spec(view)["tile"]


def mounts(view: str | None = None) -> tuple:
    """The prop mounts that mean anything in this view."""
    return tuple(spec(view)["mounts"])


def supports(view: str | None, mount: str) -> bool:
    """Is this prop mount meaningful here? A ceiling mount is ordinary in a
    platformer and meaningless seen from above."""
    return str(mount or "").strip().lower() in mounts(view)


def load(root: str | os.PathLike[str]) -> str:
    """The project's declared view. Defaults rather than guessing per call."""
    doc = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    try:
        return normalise(doc.get("view"))
    except ViewError:
        # a project that stored something invalid gets the default and not a
        # crash on every unrelated tool call
        return DEFAULT_VIEW


def save(root: str | os.PathLike[str], view: str) -> dict:
    """Declare the project's view. Returns the full spec."""
    name = normalise(view)
    _ws.set(root, SEAT, DOC_KEY, {"view": name})
    return {"view": name, **SPECS[name]}


def describe(view: str | None = None) -> dict:
    """The view as a flat report, for a tool result or a seat brief."""
    name = normalise(view)
    s = SPECS[name]
    return {"view": name, "label": s["label"], "tile_shape": s["tile"][0],
            "tile_layout": s["tile"][1], "mounts": list(s["mounts"]),
            "gravity": s["gravity"], "reachability": s["reach"],
            "sprite_view": s["sprite_view"], "note": s["note"]}
