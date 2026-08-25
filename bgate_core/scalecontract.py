"""Reference scale — how big everything is, declared once and MEASURED.

WHAT EXISTED AND WHY IT WAS NOT ENOUGH. The sprite contract already fixed player
height and tile size, so characters agreed with the floor. Nothing fixed a
DOOR, a desk, a filing cabinet, a health pip or an enemy, so every prop was
sized against whatever the generator felt like and reviewed on a contact sheet —
a grid where every asset is drawn at the same box size, which is exactly the
presentation that cannot show a scale error. Night Shift shipped a mug the
height of a chair and a door a player could not have walked through, both
approved, both approved *on a contact sheet*.

So this module declares a scale for every CLASS of thing, in the one unit that
survives a re-export: pixels of the player's height.

    prop        the small stuff you put on a surface
    furniture   the things a room is furnished with
    door        the openings — graded hardest, because a door is the one prop
                whose size the player reads as a promise about their own body
    ui          HUD and icons, which are screen-space and get their own band
    enemy       measured against the player deliberately: an enemy's size IS a
                threat statement, and one that lies is a design bug

and then it MEASURES, from the file, at game scale — the opaque bounding box in
pixels, divided by the declared player height. A contact sheet cannot be the
evidence for a check whose whole subject is relative size.

WHAT COUNTS AS DELIVERED. :func:`unmeasured` reports art that reached the game
without a measurement recorded, which is what the release gate refuses on. The
measurement rides on the artifact revision (``metadata.scale``) via
``artifacts.record_check``, so it is per-revision and a regenerated asset is
unmeasured again — which is the correct answer, and was not the old one.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from . import activity, workspace as _ws

SEAT = "director"
DOC_KEY = "scale_contract"

#: The metadata key a measurement is recorded under, and what the release gate
#: looks for. One key, so a check and its reader cannot drift.
CHECK_KEY = "scale"

#: Bands as multiples of PLAYER HEIGHT, low to high inclusive. These are
#: defaults for a human-scale game and every one of them is overridable; what
#: is not overridable is that a class HAS a band, because "no expectation" is
#: how a mug ends up chair-sized.
DEFAULT_CLASSES: dict[str, dict] = {
    "prop": {"low": 0.04, "high": 0.45,
             "note": "hand props and surface clutter — a mug, a folder, a "
                     "radio. Above 0.45 it is furniture, whatever it is "
                     "called."},
    "furniture": {"low": 0.35, "high": 1.6,
                  "note": "desks, cabinets, shelving. The top of the band is "
                          "a tall cabinet; anything past it reads as "
                          "architecture."},
    "door": {"low": 1.05, "high": 1.5,
             "note": "a door the player believes they fit through. The floor "
                     "is above 1.0 on purpose — a door the exact height of "
                     "the player reads as a hatch."},
    "ui": {"low": 0.05, "high": 1.0,
           "note": "screen-space elements, measured against the player only "
                   "so one number governs the whole project; the band is wide "
                   "because a health bar and an icon are both UI."},
    "enemy": {"low": 0.5, "high": 2.5,
              "note": "an enemy's size is a threat statement. Outside this "
                      "band it is either invisible in a room or it is a boss, "
                      "and the design should say which."},
    # THE CLASS THAT WAS MISSING, AND THE ROW NOBODY COULD CLEAR.
    #
    # The contract's own unit is `player_height_px`, and until now the class
    # vocabulary was prop|furniture|door|ui|enemy — no `player`. The release
    # gate's row said, verbatim, `scale_check(path, klass)`, and for the
    # PLAYER CHARACTER there was no klass that could be passed. The row
    # claimed it cleared by being done and it could never be done.
    #
    # A row no correct action can clear is worse than no row: it teaches
    # operators to route around the gate. See findings.actionable, which now
    # refuses to let a gate publish one.
    #
    # The band is tight around 1.0 on purpose. The player is the ruler; a
    # player sprite that is not one player-height tall means the ruler and the
    # thing being measured disagree, which invalidates every other row.
    "player": {"low": 0.85, "high": 1.15,
               "note": "the player character itself, measured against the "
                       "declared player height. The band is tight because "
                       "this asset IS the unit — if it is off, every other "
                       "measurement in the project is off by the same amount "
                       "and nothing else can catch it."},
}

CLASSES = tuple(DEFAULT_CLASSES)

#: Classes whose meaning is SCREEN-SPACE and therefore survives a 3D project.
#: A HUD icon is pixels whether or not the world behind it has three axes.
SCREEN_SPACE_CLASSES = ("ui",)

DEFAULTS: dict[str, Any] = {
    "player_height_px": 0,       # 0 = not declared; every check refuses
    "tile_px": 0,
    "classes": {},               # per-class {low, high} overrides
    "overrides": {},             # per-path {class, low, high}
}

#: Alpha at or below this is background. Matches artdirection's own threshold so
#: two modules measuring the same sheet cannot disagree about where it ends.
ALPHA_FLOOR = 8


class NotDeclared(ValueError):
    """No player height, so nothing can be measured against anything."""


class WrongDimension(ValueError):
    """This measurement is not a fact about this project's kind of game.

    MEASURED, and it reached a release gate. ``scale_check`` measures the
    OPAQUE PIXEL BOX OF A PNG. Pointed at a 3D character's turnaround render it
    reported "30.00 player-heights tall" for a cat 0.24 m long — a perfectly
    arithmetic statement about a render canvas and about nothing in the game.
    That number then became a blocking row in the presentation gate, written by
    a tool answering outside its competence, with no way to retract it.

    So a 2D pixel measurement is REFUSED on a 3D asset rather than returned.
    An explicit refusal is the only honest answer a tool has when the question
    is not one it can answer; returning ``ok: true`` with a number is how a
    false blocker gets born.
    """


def project_dimension(root: str | os.PathLike[str]) -> str:
    """``2d`` | ``3d`` | ``2d+3d``, or '' when it cannot be read."""
    try:
        from . import project as _project

        return str((_project.get(root) or {}).get("dimension") or "")
    except Exception:
        return ""


#: Suffixes whose scale is a fact about geometry, not about pixels.
MESH_SUFFIXES = (".glb", ".gltf", ".obj", ".fbx", ".blend")


def dimension_guard(root: str | os.PathLike[str], path: str,
                    klass: str) -> dict:
    """May a PIXEL measurement speak about this asset? {ok, why, measure_with}.

    Two independent refusals, and they are different questions:

      * the FILE is a mesh — pixels are not its unit under any circumstances
      * the PROJECT is 3D and the class is a world-space one — the PNG may
        well be a turnaround, a texture or a sprite sheet for a 3D thing, and
        its opaque box is a fact about the canvas
    """
    lowered = str(path).lower()
    if lowered.endswith(MESH_SUFFIXES):
        return {"ok": False, "measure_with": "godot_inspect_resource",
                "why": (f"{path} is a mesh. Its size is an AABB in metres, not "
                        "an opaque pixel box — measure it with "
                        "godot_inspect_resource (engine-instantiated mesh and "
                        "collider bounds) or blender_scene_stats, and compare "
                        "THAT against the contract.")}
    dimension = project_dimension(root)
    if dimension == "3d" and klass not in SCREEN_SPACE_CLASSES:
        return {"ok": False, "measure_with": "godot_inspect_resource",
                "why": (f"this project is 3D, so the {klass!r} band is a claim "
                        "about world size and a PNG's opaque pixel box cannot "
                        "make it. Measured on the benchmark: a 0.24 m cat's "
                        "turnaround render scored '30.00 player-heights tall'. "
                        "Measure the instantiated geometry with "
                        "godot_inspect_resource. Screen-space classes "
                        f"({', '.join(SCREEN_SPACE_CLASSES)}) are still "
                        "measurable here because pixels are their unit.")}
    return {"ok": True, "why": "", "measure_with": "scale_check"}


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


def contract(root: str | os.PathLike[str]) -> dict:
    """The resolved contract: defaults, then the project's overrides.

    Falls back to the sprite contract's cell height for ``player_height_px``
    when the project has one and has not declared a scale — the two contracts
    are about the same body and making somebody type it twice is how they
    disagree.
    """
    doc = _doc(root)
    out = dict(DEFAULTS)
    out.update({k: v for k, v in doc.items() if k in DEFAULTS})
    if not int(out.get("player_height_px") or 0):
        out["player_height_px"] = _sprite_cell_height(root)
    classes: dict[str, dict] = {}
    for name, band in DEFAULT_CLASSES.items():
        merged = dict(band)
        override = (doc.get("classes") or {}).get(name)
        if isinstance(override, dict):
            for key in ("low", "high"):
                if override.get(key) is not None:
                    merged[key] = float(override[key])
        classes[name] = merged
    out["classes"] = classes
    out["overrides"] = doc.get("overrides") if isinstance(
        doc.get("overrides"), dict) else {}
    return out


def _sprite_cell_height(root: str | os.PathLike[str]) -> int:
    """The sprite contract's cell height, but ONLY if a human set one.

    Reads the stored doc rather than ``spritecontract.load``, which normalises
    an absent contract to a 128px default. Inheriting that default would mean
    every project silently has a player height it never declared, and every
    scale check would grade against a number nobody chose — which is the same
    "looks about right" review this module exists to replace, with a decimal
    point on it.
    """
    try:
        from . import spritecontract as _sprite

        doc = _ws.get(root, _sprite.SEAT, _sprite.DOC_KEY, {}) or {}
        cell = doc.get("cell") or []
        return int(cell[1]) if len(cell) > 1 else 0
    except Exception:
        return 0


def set_contract(root: str | os.PathLike[str], *,
                 player_height_px: Optional[int] = None,
                 tile_px: Optional[int] = None,
                 classes: Optional[dict] = None, by: str = "") -> dict:
    doc = _doc(root)
    if player_height_px is not None:
        height = int(player_height_px)
        if height < 4:
            raise ValueError("player_height_px is the player's height in "
                             "pixels; 4 is the smallest thing that could be one")
        doc["player_height_px"] = height
    if tile_px is not None:
        doc["tile_px"] = int(tile_px)
    if classes:
        held = doc.get("classes")
        doc["classes"] = held if isinstance(held, dict) else {}
        for name, band in classes.items():
            if name not in DEFAULT_CLASSES:
                raise ValueError(
                    f"unknown scale class {name!r}; classes are {CLASSES}")
            if not isinstance(band, dict):
                raise ValueError(f"{name}: a band is {{low, high}}")
            low = float(band.get("low", DEFAULT_CLASSES[name]["low"]))
            high = float(band.get("high", DEFAULT_CLASSES[name]["high"]))
            if not 0 < low < high:
                raise ValueError(
                    f"{name}: the band must be 0 < low < high, got "
                    f"low={low} high={high}")
            doc["classes"][name] = {"low": low, "high": high}
    _save(root, doc)
    activity.log(root, "scale", "scale contract updated", seat=SEAT)
    return contract(root)


# ── measuring ───────────────────────────────────────────────────────────────

def extents(path: str | os.PathLike[str]) -> dict:
    """The opaque bounding box of an image, in pixels.

    THE BOX, NOT THE CANVAS. A 512x512 sheet holding a 40px mug is a 40px mug,
    and the canvas size is what a contact sheet shows you instead — which is
    the entire reason a contact sheet cannot review scale.
    """
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGBA")
        canvas = list(im.size)
        box = im.getchannel("A").point(
            lambda a: 255 if a > ALPHA_FLOOR else 0).getbbox()
    if not box:
        return {"canvas": canvas, "width": 0, "height": 0, "empty": True}
    left, top, right, bottom = box
    return {"canvas": canvas, "width": right - left, "height": bottom - top,
            "box": [left, top, right, bottom], "empty": False}


def check(root: str | os.PathLike[str], path: str | os.PathLike[str],
          klass: str, *, frames: int = 1) -> dict:
    """Measure one asset against its class band, at game scale.

    ``frames`` divides the measured width for a horizontal sheet, so a 6-frame
    strip is graded as one 1/6-width sprite rather than as a six-player-wide
    prop. Height is untouched — a strip is one row.
    """
    if klass not in DEFAULT_CLASSES:
        raise ValueError(f"unknown scale class {klass!r}; classes are {CLASSES}")
    guard = dimension_guard(root, str(path), klass)
    if not guard["ok"]:
        raise WrongDimension(guard["why"])
    got = contract(root)
    player = int(got.get("player_height_px") or 0)
    if player < 4:
        raise NotDeclared(
            "this project has not declared player_height_px, so nothing can "
            "be measured against anything — scale_contract_set(player_height_px"
            "=...) first. Every 'looks about right' review that shipped a "
            "chair-sized mug happened in the absence of this number.")
    absolute = os.path.join(os.fspath(root), str(path)) \
        if not os.path.isabs(str(path)) else str(path)
    measured = extents(absolute)
    if measured["empty"]:
        return {"ok": False, "klass": klass, "path": str(path),
                "measured": measured, "player_height_px": player,
                "flags": ["the image has no opaque pixels — there is nothing "
                          "here to be the wrong size"]}
    band = got["classes"][klass]
    override = (got.get("overrides") or {}).get(
        str(path).replace("\\", "/"))
    if isinstance(override, dict):
        band = {"low": float(override.get("low", band["low"])),
                "high": float(override.get("high", band["high"]))}
    height = measured["height"] / float(player)
    width = (measured["width"] / max(1, int(frames))) / float(player)
    flags: list[str] = []
    if height < band["low"]:
        flags.append(
            f"{height:.2f} player-heights tall; {klass} starts at "
            f"{band['low']:.2f}. At game scale this is "
            f"{measured['height']}px against a {player}px player — it will "
            f"read as debris, not as a {klass}.")
    if height > band["high"]:
        flags.append(
            f"{height:.2f} player-heights tall; {klass} tops out at "
            f"{band['high']:.2f}. At game scale this is {measured['height']}px "
            f"against a {player}px player.")
    if klass in ("prop", "furniture", "door") and width > band["high"] * 2.5:
        flags.append(
            f"{width:.2f} player-heights WIDE against a {height:.2f} height — "
            "at game scale that is a wall, and it was probably generated "
            "against a square canvas rather than the room")
    return {
        "ok": not flags, "klass": klass, "path": str(path), "flags": flags,
        "player_height_px": player, "frames": int(frames),
        "measured": {**measured,
                     "height_players": round(height, 3),
                     "width_players": round(width, 3)},
        "band": band,
        "note": DEFAULT_CLASSES[klass]["note"],
    }


def record(root: str | os.PathLike[str], path: str | os.PathLike[str],
           klass: str, *, frames: int = 1) -> dict:
    """Measure, and attach the result to the asset's newest revision."""
    from . import artifacts as _artifacts

    got = check(root, path, klass, frames=frames)
    got.setdefault("tool", "scale_check")
    got.setdefault("dimension", project_dimension(root))
    try:
        _artifacts.record_check(root, path, CHECK_KEY, got)
    except Exception as exc:                                      # noqa: BLE001
        got["recorded"] = False
        got["record_error"] = f"{type(exc).__name__}: {exc}"
        return got
    got["recorded"] = True
    # A REAL MEASUREMENT RETRACTS THE ROW THAT ASKED FOR ONE. Without this the
    # gate carries its old complaint alongside the answer to it, and the only
    # cure for a false blocker is to remember which line it was on.
    if got["ok"]:
        try:
            from . import findings as _findings

            _findings.supersede_key(
                root, "scale", str(path),
                why=(f"measured at game scale as {klass}: "
                     f"{got['measured'].get('height_players')} player-heights"),
                tool="scale_check", measured=got.get("measured") or {})
        except Exception:                                         # noqa: BLE001
            pass
    activity.log(root, "scale",
                 f"{'ok' if got['ok'] else 'OFF-SCALE'}: {path} as {klass}",
                 seat="qa", ref=str(path))
    return got


# ── the release gate's question ─────────────────────────────────────────────

#: Artifact statuses that mean the asset is in the game. A candidate nobody
#: took is not something a release gate should block on.
DELIVERED = ("approved", "integrated")

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def unmeasured(root: str | os.PathLike[str], limit: int = 400) -> list[str]:
    """Delivered image assets with no scale measurement, or a failing one.

    Kept as sentences for every existing caller. :func:`unmeasured_findings`
    is the same answer with its provenance attached, and it is what the release
    gate reads now.
    """
    return [f["claim"] for f in unmeasured_findings(root, limit=limit)]


def unmeasured_findings(root: str | os.PathLike[str],
                        limit: int = 400) -> list[dict]:
    """The scale gate's rows, each carrying WHAT MEASURED IT and HOW IT CLEARS.

    EVERY ROW MUST NAME A RUNNABLE ACTION. The old version emitted, verbatim,
    ``scale_check('<path>', klass)`` for every delivered image — including a 3D
    character's turnaround render, for which no ``klass`` existed and no pixel
    measurement would have meant anything. See :class:`WrongDimension` and
    :mod:`bgate_core.findings`.

    On a 3D project a world-space asset does not get a pixel row at all; it
    gets a row naming the engine measurement that CAN answer it.
    """
    from . import artifacts as _artifacts, findings as _findings

    got = contract(root)
    dimension = project_dimension(root)
    if int(got.get("player_height_px") or 0) < 4:
        return [_findings.make(
            gate="scale", key="contract:player_height_px",
            kind=_findings.BLOCKING,
            claim=("no player_height_px is declared, so no asset in this "
                   "project has ever been checked at game scale"),
            tool="scalecontract.contract",
            measured={"player_height_px": got.get("player_height_px")},
            dimension=dimension,
            clears_by="scale_contract_set(player_height_px=<pixels>)")]

    out: list[dict] = []
    newest: dict[str, dict] = {}
    for status in DELIVERED:
        for row in _artifacts.list_revisions(root, status=status, limit=limit):
            path = str(row.get("path") or "")
            if not path.lower().endswith(_IMAGE_SUFFIXES + MESH_SUFFIXES):
                continue
            prior = newest.get(path)
            if prior is None or int(row["revision"]) > int(prior["revision"]):
                newest[path] = row
    for path, row in sorted(newest.items()):
        scale = (row.get("metadata") or {}).get(CHECK_KEY)
        klass = (scale or {}).get("klass") if isinstance(scale, dict) else None
        guard = dimension_guard(root, path, str(klass or "prop"))
        if isinstance(scale, dict) and scale.get("ok"):
            continue
        if isinstance(scale, dict) and not scale.get("ok"):
            flags = "; ".join(scale.get("flags") or ["off-scale"])
            out.append(_findings.make(
                gate="scale", key=path, kind=_findings.BLOCKING,
                claim=f"{path} is off-scale as {klass or 'unknown'}: {flags}",
                tool=str(scale.get("tool") or "scale_check"),
                inputs={"path": path, "klass": klass},
                measured=dict(scale.get("measured") or {}),
                dimension=dimension,
                clears_by=(f"regenerate or rescale {path} so it lands inside "
                           f"the {klass or 'declared'} band, then re-run the "
                           "measurement")))
            continue
        if not guard["ok"]:
            # THE ROW A PIXEL TOOL MAY NOT WRITE. It still blocks — an
            # unmeasured delivered asset is unmeasured — but it names the
            # measurement that can actually answer it, so the row is clearable.
            out.append(_findings.make(
                gate="scale", key=path, kind=_findings.BLOCKING,
                claim=(f"{path} is in the game with no scale measurement, and "
                       "a pixel measurement cannot make one here: "
                       + guard["why"]),
                tool="scalecontract.dimension_guard",
                inputs={"path": path, "dimension": dimension},
                measured={"dimension": dimension, "pixel_measurable": False},
                dimension=dimension,
                clears_by=(f"{guard['measure_with']}('{path}') and compare the "
                           "measured bounds against the size this asset class "
                           "is contracted to be")))
            continue
        out.append(_findings.make(
            gate="scale", key=path, kind=_findings.BLOCKING,
            claim=(f"{path} is in the game with no scale measurement. A "
                   "contact-sheet review cannot have caught a scale error — "
                   "every asset on a contact sheet is drawn the same size"),
            tool="scalecontract.unmeasured",
            inputs={"path": path},
            measured={"recorded": False},
            dimension=dimension,
            clears_by=(f"scale_check('{path}', klass) at game scale; classes "
                       f"are {', '.join(CLASSES)}")))
    return out


def state(root: str | os.PathLike[str]) -> dict:
    got = contract(root)
    return {**got, "class_notes": {k: v["note"]
                                   for k, v in DEFAULT_CLASSES.items()},
            "unmeasured": unmeasured(root)}
