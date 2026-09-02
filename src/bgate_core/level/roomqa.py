"""Room composition, reviewed as a WHOLE ROOM.

WHAT THE OLD REVIEW LOOKED AT. Cropped evidence. A sprite on a transparent
background, a contact sheet, a screenshot framed on the thing that had just been
made. Every one of those is a picture of an ASSET, and a room is not a pile of
assets — it is where they are. Night Shift passed art QA on every prop in a
level whose rooms were an empty rectangle with the furniture shoved against the
walls, because nothing ever looked at the rectangle.

So this module grades the ROOM, from two sources that answer different halves:

MEASURED, from the scene tree. Where things actually are is a fact, and facts
are what a reviewer should be arguing with:

    empty_floor       the largest contiguous stretch of interior with nothing
                      in it, as a fraction of the room
    perimeter_hug     how much of the furniture is pinned to a wall
    scale_spread      how far apart the biggest and smallest placed things are
    focal             whether any one region dominates the eye
    lanes             the widths of the traversable gaps between obstacles —
                      a room with no lane narrower than the whole room is a
                      room with no combat geography

SEEN, from a FULL-ROOM screenshot. Required, and required to be full-room: the
verdict names the shot it was made against, and a shot that does not frame the
whole room is refused rather than accepted with a caveat.

WHAT THIS DOES NOT DO. It does not judge whether a room is beautiful. Every
finding here is a number with a threshold, and the thresholds are named
constants a project can argue with — because the alternative is a model saying
"the composition feels sparse", which is unfalsifiable and was, in practice,
ignored.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..board import activity
from ..store import workspace as _ws

SEAT = "director"
DOC_KEY = "room_reviews"

MAX_TEXT = 2000

#: Interior with nothing in it, as a fraction of room area, past which the room
#: reads as a floor with objects at the edges rather than a place.
MAX_EMPTY_FLOOR = 0.55

#: Fraction of placed furniture within one tile of a wall, past which the room
#: is a perimeter arrangement. Real rooms have things in the middle; test rooms
#: do not, which is why this number is what tells them apart.
MAX_PERIMETER_HUG = 0.75

#: A wall band, in fractions of the room's short side. Anything whose centre
#: falls in it counts as hugging.
WALL_BAND = 0.12

#: Ratio between the largest and smallest placed prop past which the room has a
#: scale problem rather than a variety.
MAX_SCALE_SPREAD = 12.0

#: A room with fewer placed things than this is a test room, whatever it is
#: called. Named separately from the emptiness measure because a room can be
#: numerically busy and still have four objects in it.
MIN_PLACED = 4

#: Node roles that count as things placed IN the room. UI and controllers are
#: neither furniture nor obstacle and would drown the measurement.
PLACED_ROLES = ("visual", "instance", "character", "prop", "collision")

_VEC = re.compile(r"Vector2i?\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")


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


# ── reading the room ────────────────────────────────────────────────────────

def _vec(raw: Any) -> Optional[tuple[float, float]]:
    found = _VEC.search(str(raw or ""))
    return (float(found.group(1)), float(found.group(2))) if found else None


def _scene_path(root: str | os.PathLike[str], scene: str) -> Optional[Path]:
    bare = scene[len("res://"):] if scene.startswith("res://") else scene
    bare = bare.replace("/", os.sep).lstrip(os.sep)
    for candidate in (Path(root) / bare, Path(root) / "game" / bare):
        if candidate.is_file():
            return candidate
    return None


def placements(root: str | os.PathLike[str], scene: str) -> list[dict]:
    """Every node in the scene that has a position, with its role and size.

    Nodes with no ``position`` are skipped rather than defaulted to the origin:
    a default would pile half the tree onto one point and every measurement
    downstream would be about that pile.
    """
    from . import scenewire

    path = _scene_path(root, scene)
    if path is None:
        raise FileNotFoundError(
            f"{scene!r} is not a scene file under this project")
    nodes = scenewire.outline(path.read_text(encoding="utf-8",
                                             errors="replace"))
    out: list[dict] = []
    for node in nodes:
        props = node.get("properties") or {}
        at = _vec(props.get("position"))
        if at is None:
            continue
        scale = _vec(props.get("scale")) or (1.0, 1.0)
        size = _vec(props.get("size")) or _vec(props.get("region_rect"))
        extent = max(abs(size[0] * scale[0]), abs(size[1] * scale[1])) \
            if size else max(abs(scale[0]), abs(scale[1]))
        out.append({"name": node["name"], "role": node.get("role") or "node",
                    "type": node.get("type") or "", "at": [at[0], at[1]],
                    "extent": float(extent)})
    return out


def measure(root: str | os.PathLike[str], scene: str,
            bounds: Optional[list[float]] = None) -> dict:
    """The room's composition as numbers. ``bounds`` is [x0, y0, x1, y1].

    Without ``bounds`` the room is taken as the bounding box of everything
    placed in it, which is an under-estimate on purpose: a room measured from
    its own contents can only ever look BUSIER than it is, so a finding raised
    against a derived box is a finding that would also hold against the real
    one.
    """
    placed = [p for p in placements(root, scene)
              if p["role"] in PLACED_ROLES]
    out: dict[str, Any] = {"scene": scene, "placed": len(placed),
                           "findings": [], "derived_bounds": bounds is None}
    if not placed:
        out["findings"].append(
            "nothing is placed in this room — it is an empty rectangle, which "
            "is what a test room is")
        return out
    xs = [p["at"][0] for p in placed]
    ys = [p["at"][1] for p in placed]
    if bounds and len(bounds) == 4:
        x0, y0, x1, y1 = (float(b) for b in bounds)
    else:
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    width, height = max(1.0, x1 - x0), max(1.0, y1 - y0)
    short = min(width, height)
    band = short * WALL_BAND

    hugging = sum(
        1 for p in placed
        if (p["at"][0] - x0 <= band or x1 - p["at"][0] <= band
            or p["at"][1] - y0 <= band or y1 - p["at"][1] <= band))
    hug = hugging / len(placed)

    # Emptiness on a coarse grid. A grid rather than a largest-empty-rectangle
    # solve because the answer wanted is "how much of this room is nothing",
    # and an exact rectangle would be a precise answer to a different question.
    cells = 8
    occupied = set()
    for p in placed:
        cx = min(cells - 1, max(0, int((p["at"][0] - x0) / width * cells)))
        cy = min(cells - 1, max(0, int((p["at"][1] - y0) / height * cells)))
        occupied.add((cx, cy))
    empty = 1.0 - (len(occupied) / float(cells * cells))

    extents = sorted(p["extent"] for p in placed if p["extent"] > 0)
    spread = (extents[-1] / extents[0]) if len(extents) > 1 and extents[0] else 1.0

    # The focal test: does any one cell hold a disproportionate share? A room
    # where every cell holds the same is a grid of stuff, and a room where one
    # cell holds everything is a pile. Both read as "no focal point"; the
    # finding says which.
    per_cell: dict[tuple[int, int], int] = {}
    for p in placed:
        cx = min(cells - 1, max(0, int((p["at"][0] - x0) / width * cells)))
        cy = min(cells - 1, max(0, int((p["at"][1] - y0) / height * cells)))
        per_cell[(cx, cy)] = per_cell.get((cx, cy), 0) + 1
    busiest = max(per_cell.values())
    focal = busiest / float(len(placed))

    # Lanes: gaps between occupied columns, in fractions of the room width. A
    # room whose only lane is the whole room has no geography to fight in.
    columns = sorted({c for c, _ in occupied})
    gaps = [(b - a - 1) / float(cells)
            for a, b in zip(columns, columns[1:]) if b - a > 1]
    lanes = sorted(gaps, reverse=True)

    out.update({
        "bounds": [x0, y0, x1, y1], "size": [width, height],
        "empty_floor": round(empty, 3),
        "perimeter_hug": round(hug, 3),
        "scale_spread": round(spread, 2),
        "focal": round(focal, 3),
        "lanes": [round(g, 3) for g in lanes],
    })

    findings = out["findings"]
    if len(placed) < MIN_PLACED:
        findings.append(
            f"{len(placed)} things placed in the whole room — under "
            f"{MIN_PLACED} this is a test room with a name")
    if empty > MAX_EMPTY_FLOOR:
        findings.append(
            f"{empty:.0%} of the room is empty floor (cap {MAX_EMPTY_FLOOR:.0%})"
            " — large empty areas read as unfinished, not as space")
    if hug > MAX_PERIMETER_HUG:
        findings.append(
            f"{hug:.0%} of what is placed is against a wall (cap "
            f"{MAX_PERIMETER_HUG:.0%}) — a perimeter arrangement is the single "
            "clearest tell of a room that was furnished by a list rather than "
            "composed")
    if spread > MAX_SCALE_SPREAD:
        findings.append(
            f"the largest placed thing is {spread:.0f}x the smallest (cap "
            f"{MAX_SCALE_SPREAD:.0f}x) — check these against the scale "
            "contract, not against each other")
    if focal > 0.6:
        findings.append(
            f"{focal:.0%} of everything sits in one eighth of the room — that "
            "is a pile, not a focal point")
    elif focal < (1.5 / (cells * cells)) and len(placed) > MIN_PLACED:
        findings.append(
            "no region of this room holds more than any other — evenly "
            "scattered furniture gives the eye nowhere to land")
    if not lanes and len(placed) >= MIN_PLACED:
        findings.append(
            "there are no gaps between the occupied columns — the room has no "
            "lanes, so every fight in it happens in the same undifferentiated "
            "space")
    return out


# ── the verdict ─────────────────────────────────────────────────────────────

def _full_room(root: str | os.PathLike[str], shot: str) -> tuple[bool, str]:
    """Is this screenshot plausibly of a WHOLE room?

    Two crude tests, both aimed at the same failure: an asset crop offered as
    room evidence. A shot with an alpha channel that is mostly transparent is a
    cutout, and a shot far off the project's own viewport aspect is a crop of
    one. Neither proves the framing is right; both catch what actually got
    submitted last time.
    """
    path = Path(shot)
    if not path.is_absolute():
        path = Path(root) / shot
    if not path.is_file():
        return False, f"{shot} is not a file under this project"
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.size
            transparent = 0.0
            if im.mode in ("RGBA", "LA"):
                alpha = im.convert("RGBA").getchannel("A")
                small = alpha.resize((64, 64))
                data = list(small.getdata())
                transparent = sum(1 for a in data if a < 8) / float(len(data))
    except Exception as exc:                                      # noqa: BLE001
        return False, f"{shot} could not be read as an image ({exc})"
    if transparent > 0.25:
        return False, (
            f"{shot} is {transparent:.0%} transparent — that is a cutout of an "
            "asset, not a screenshot of a room. Run the game and capture the "
            "room.")
    if width < 320 or height < 240:
        return False, (
            f"{shot} is {width}x{height} — too small to be a full-room capture")
    aspect = width / float(height)
    want = _viewport_aspect(root)
    if want:
        # The project's OWN viewport, when it declares one. A 4:3 game's
        # legitimate capture is 1.33 and a hardcoded 16:9 band would refuse
        # every screenshot it ever takes — a gate that is wrong about a whole
        # class of project is a gate that gets switched off.
        if not 0.85 <= aspect / want <= 1.18:
            return False, (
                f"{shot} is {width}x{height} (aspect {aspect:.2f}); this "
                f"project's viewport is {want:.2f}. A full-room capture comes "
                "out at the game's aspect — this is a crop of one.")
        return True, ""
    if aspect < 1.2:
        return False, (
            f"{shot} is {width}x{height} (aspect {aspect:.2f}) — square or "
            "taller than it is wide. No game viewport is that shape, so this "
            "is a crop.")
    return True, ""


def _viewport_aspect(root: str | os.PathLike[str]) -> float:
    """The game's declared viewport aspect, or 0.0 if it does not declare one."""
    for folder in ("game", ""):
        path = Path(root) / folder / "project.godot"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0.0
        found = {}
        for axis in ("width", "height"):
            match = re.search(rf"^window/size/viewport_{axis}\s*=\s*(\d+)",
                              text, re.MULTILINE)
            if match:
                found[axis] = int(match.group(1))
        if found.get("width") and found.get("height"):
            return found["width"] / float(found["height"])
        return 0.0
    return 0.0


def review(root: str | os.PathLike[str], scene: str, *, shot: str,
           verdict: str, notes: str, bounds: Optional[list[float]] = None,
           by: str = "") -> dict:
    """Record a full-room verdict. Refuses a cropped shot and a bare 'pass'.

    A pass is refused while MEASURED findings stand: the reviewer may disagree
    with a threshold, and saying so is what ``override`` is for — but they have
    to say it, per finding, which is the difference between a judgement and a
    click.
    """
    verdict = str(verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        raise ValueError("verdict is 'pass' or 'fail'")
    notes = " ".join(str(notes or "").split())[:MAX_TEXT]
    if len(notes) < 20:
        raise ValueError(
            "a room verdict costs a sentence — what is in this room, where the "
            "eye lands, and where a fight happens in it")
    ok, why = _full_room(root, shot)
    if not ok:
        raise ValueError(
            f"{why} A room-composition gate that accepts cropped evidence is "
            "the gate that passed Night Shift's empty rectangles.")
    measured = measure(root, scene, bounds)
    if verdict == "pass" and measured["findings"]:
        raise ValueError(
            "this room still has measured findings against it, so a pass has "
            "to answer them one by one — pass each with roomqa_override, or "
            "fail the room:\n- " + "\n- ".join(measured["findings"]))
    doc = _doc(root)
    rooms = doc.get("rooms")
    doc["rooms"] = rooms if isinstance(rooms, dict) else {}
    row = {"scene": scene, "shot": shot, "verdict": verdict, "notes": notes,
           "measured": measured, "by": by or activity.current_actor(),
           "at": _now(),
           "overrides": (doc["rooms"].get(scene) or {}).get("overrides") or {}}
    doc["rooms"][scene] = row
    _save(root, doc)
    activity.log(root, "roomqa", f"room {verdict}: {scene}", seat=SEAT,
                 ref=scene)
    return row


def override(root: str | os.PathLike[str], scene: str, finding: str,
             reason: str, by: str = "") -> dict:
    """Accept one measured finding on this room, with a reason on the record.

    Per finding, never per room: "the thresholds do not suit this game" would
    switch the gate off for everything, and a gate that can be switched off
    wholesale is the one that was.
    """
    reason = " ".join(str(reason or "").split())[:MAX_TEXT]
    if len(reason) < 20:
        raise ValueError(
            "an override costs a sentence naming why this room is right and "
            "the measurement is wrong")
    doc = _doc(root)
    rooms = doc.get("rooms")
    doc["rooms"] = rooms if isinstance(rooms, dict) else {}
    row = doc["rooms"].get(scene) or {"scene": scene, "overrides": {}}
    held = row.get("overrides")
    row["overrides"] = held if isinstance(held, dict) else {}
    row["overrides"][str(finding)[:400]] = {
        "reason": reason, "by": by or activity.current_actor(), "at": _now()}
    doc["rooms"][scene] = row
    _save(root, doc)
    activity.log(root, "roomqa", f"override on {scene}: {reason[:100]}",
                 seat=SEAT, ref=scene)
    return row


def reviews(root: str | os.PathLike[str]) -> dict:
    got = _doc(root).get("rooms")
    return got if isinstance(got, dict) else {}


def rooms(root: str | os.PathLike[str]) -> list[str]:
    """Every playable room scene this project has, newest listing each call.

    Derived rather than registered: a registry is a second list to keep in step
    with the filesystem, and the one that drifts is always the registry. A room
    is a ``.tscn`` under a levels/rooms/scenes directory that is not the
    graybox.
    """
    from ..design import greenlight as _gl

    base = Path(root)
    graybox = str((_gl.graybox(root) or {}).get("scene") or "")
    graybox_tail = graybox.rsplit("/", 1)[-1].lower()
    out: list[str] = []
    for folder in ("game/levels", "game/rooms", "game/scenes/levels",
                   "levels", "rooms"):
        here = base / folder
        if not here.is_dir():
            continue
        for path in sorted(here.rglob("*.tscn")):
            rel = str(path.relative_to(base)).replace("\\", "/")
            if graybox_tail and rel.lower().endswith(graybox_tail):
                continue
            out.append(rel)
    return out


def unreviewed(root: str | os.PathLike[str]) -> list[str]:
    """Rooms with no passing full-room verdict. The release gate's question."""
    done = reviews(root)
    out: list[str] = []
    for scene in rooms(root):
        row = done.get(scene)
        if row is None:
            # A room can also have been reviewed under its res:// name.
            row = done.get("res://" + scene.split("game/", 1)[-1])
        if row is None:
            out.append(
                f"{scene} has never had a full-room composition review — "
                f"roomqa_review('{scene}', shot=..., verdict=...) against a "
                "screenshot of the whole room")
            continue
        if row.get("verdict") != "pass":
            out.append(f"{scene} failed its room review: "
                       f"{str(row.get('notes') or '')[:160]}")
            continue
        standing = [f for f in (row.get("measured") or {}).get("findings") or []
                    if f not in (row.get("overrides") or {})]
        if standing:
            out.append(f"{scene} passed but has {len(standing)} measured "
                       f"finding(s) nobody answered: {standing[0]}")
    return out


def state(root: str | os.PathLike[str]) -> dict:
    return {"rooms": rooms(root), "reviews": reviews(root),
            "unreviewed": unreviewed(root),
            "thresholds": {"max_empty_floor": MAX_EMPTY_FLOOR,
                           "max_perimeter_hug": MAX_PERIMETER_HUG,
                           "max_scale_spread": MAX_SCALE_SPREAD,
                           "min_placed": MIN_PLACED}}
