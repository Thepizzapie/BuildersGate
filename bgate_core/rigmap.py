"""The rig sidecar — what a human knows about a sprite sheet that the pixels do not.

A sheet is a grid of frames and nothing else. Everything that makes it RIGGABLE
— which cells form the walk cycle, where the main hand is in frame 7, where the
character's feet touch the ground — is knowledge that currently lives in the
artist's head and dies there. gear.py can MEASURE a grip anchor when five weapon
sheets happen to agree (``measure_anchors``) and INFER one otherwise, but a
project with one weapon has nothing to intersect and inference is a guess. The
missing piece was never the algorithm. It was a place for a person to say "the
hand is here" and have it stick.

That place is a sidecar: ``<sheet-stem>.rig.json`` next to the PNG. Sidecar and
not embedded metadata, because Godot's importer rewrites .import files, image
editors strip PNG tEXt chunks, and a sheet that gets repainted must keep its
labels. Sidecar and not the database, because the labels belong to the ART — they
must survive a checkout, a copy into another project, and a database that was
never initialised.

What the format buys, downstream:

  * ``anchors_for()`` yields ``gear.Anchor`` objects with source ``authored``,
    which ranks ABOVE every inferred source in gear.py's provenance ladder. A
    labelled sheet stops guessing.
  * ``spriteframes_text()`` writes a real Godot 4 SpriteFrames over an
    ARBITRARY grid — rows and columns, per-animation frame lists, per-animation
    loop and fps. sprites.py can only emit a horizontal strip in sheet order,
    which is why hand-painted multi-row sheets could never round-trip.
  * ``coverage()`` answers "which frames are still unlabelled", so the editor
    can show the gap instead of the artist discovering it mid-combat.

Frames are addressed by INDEX (``row * cols + col``) everywhere in the file
format, because that is what an artist counts and what Godot's animation frames
already are. Anchor coordinates are CELL-LOCAL pixels — same convention as
gear.py, so an anchor means the same thing in every frame.

Pure stdlib + Pillow. No database, no network, no engine.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = 1

# Where a label can hang. These mirror the gear layer skeleton in
# templates/2d/scenes/fighter.tscn, so a labelled slot maps 1:1 onto a real
# AnimatedSprite2D layer with no translation table. Free-form slots are allowed
# — the taxonomy is a suggestion, not a gate — but a slot NAME is normalised so
# "Main Hand" and "main_hand" can never split into two silently-different rigs.
#
# LOGICAL vs ANATOMICAL hands, and why both exist. main_hand/off_hand say which
# hand HOLDS THE WEAPON — that is what the gear layer system equips against.
# left_hand/right_hand say where the character's actual hands ARE in that frame.
# They are not interchangeable: a character that turns around mid-animation
# keeps the same main hand while its left and right swap sides of the sprite,
# and a two-handed grip needs both anatomical points while having one logical
# one. Labelling both is what lets a rig follow a turn without the weapon
# jumping across the body.
KNOWN_SLOTS: tuple[str, ...] = (
    "main_hand", "off_hand",
    "left_hand", "right_hand",
    "head", "body", "feet", "throwable",
    "pivot", "muzzle", "fx",
)

# The anatomical pair, in the order the editor offers them.
HAND_SLOTS: tuple[str, str] = ("left_hand", "right_hand")

# Provenance of an anchor, matching gear.py's vocabulary. `authored` is new and
# outranks everything there: a person pointed at the pixel.
AUTHORED = "authored"

_SLOT_RE = re.compile(r"[^a-z0-9_]+")
_ANIM_RE = re.compile(r"[^a-z0-9_]+")

# A sheet past this many cells is not a thing these pipelines produce, and a
# malformed grid ({"cols": 99999}) must not turn into a 40-million-cell loop.
MAX_CELLS = 4096

# Animations that must not loop by default. Same list sprites.py ships, kept
# here so a sidecar written by the editor and a sheet written by the renderer
# agree about what a death animation does.
NO_LOOP: tuple[str, ...] = ("death", "die", "ko", "hurt", "hit")


class RigError(ValueError):
    """A sidecar that cannot be trusted. Never raised for a MISSING sidecar —
    absent labels are the normal state, and `load` returns an empty rig."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def sidecar_path(sheet: str | os.PathLike[str]) -> Path:
    """``hero_sheet.png`` -> ``hero_sheet.rig.json``, alongside the sheet.

    Suffix-replacing rather than appending (``hero_sheet.png.rig.json``) keeps
    the sidecar out of Godot's importer: a file ending in a known image suffix
    would get an .import generated for it, and the editor would show a broken
    texture in the asset browser forever.
    """
    p = Path(sheet)
    return p.with_suffix("").with_name(p.stem + ".rig.json")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def slot_name(raw: str) -> str:
    """Fold a slot label to its canonical form. Empty input is refused."""
    name = _SLOT_RE.sub("_", str(raw or "").strip().lower()).strip("_")
    if not name:
        raise RigError("a label needs a slot name")
    return name


def anim_name(raw: str) -> str:
    name = _ANIM_RE.sub("_", str(raw or "").strip().lower()).strip("_")
    if not name:
        raise RigError("an animation needs a name")
    return name


def _int(value, field: str, *, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RigError(f"{field}: expected an integer, got {value!r}")
    if not lo <= n <= hi:
        raise RigError(f"{field}: {n} out of range [{lo}, {hi}]")
    return n


def _float(value, field: str, *, lo: float, hi: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise RigError(f"{field}: expected a number, got {value!r}")
    if not lo <= f <= hi:
        raise RigError(f"{field}: {f} out of range [{lo}, {hi}]")
    return f


def empty(grid: Optional[dict] = None) -> dict:
    """A well-formed rig with nothing in it. What `load` returns for no sidecar."""
    return {
        "version": SCHEMA_VERSION,
        "grid": dict(grid) if grid else None,
        "fps": 10.0,
        "animations": [],
        "labels": [],
        "notes": "",
        "updated_at": None,
    }


def normalise(data: dict, *, sheet_size: Optional[tuple[int, int]] = None) -> dict:
    """Validate and canonicalise a rig payload. Raises RigError on anything bad.

    Everything the editor POSTs comes through here, so the on-disk file is
    always in one shape and every reader downstream (gear, SpriteFrames export,
    coverage) can skip defensive parsing.
    """
    if not isinstance(data, dict):
        raise RigError("rig payload must be an object")

    out = empty()
    out["notes"] = str(data.get("notes") or "")[:4000]

    grid = data.get("grid")
    cells = 0
    if grid:
        if not isinstance(grid, dict):
            raise RigError("grid must be an object")
        cw = _int(grid.get("cell_w"), "grid.cell_w", lo=1, hi=8192)
        ch = _int(grid.get("cell_h"), "grid.cell_h", lo=1, hi=8192)
        cols = _int(grid.get("cols"), "grid.cols", lo=1, hi=MAX_CELLS)
        rows = _int(grid.get("rows"), "grid.rows", lo=1, hi=MAX_CELLS)
        cells = cols * rows
        if cells > MAX_CELLS:
            raise RigError(f"grid has {cells} cells; the cap is {MAX_CELLS}")
        if sheet_size is not None:
            w, h = sheet_size
            if cw * cols != w or ch * rows != h:
                raise RigError(
                    f"grid {cols}x{rows} of {cw}x{ch} does not tile the "
                    f"{w}x{h} sheet")
        out["grid"] = {"cell_w": cw, "cell_h": ch, "cols": cols, "rows": rows}

    out["fps"] = _float(data.get("fps", 10.0), "fps", lo=0.1, hi=240.0)

    def _frame(value, field: str) -> int:
        n = _int(value, field, lo=0, hi=MAX_CELLS - 1)
        if cells and n >= cells:
            raise RigError(f"{field}: frame {n} is past the last cell ({cells - 1})")
        return n

    seen_anims: set[str] = set()
    for i, raw in enumerate(data.get("animations") or []):
        if not isinstance(raw, dict):
            raise RigError(f"animations[{i}] must be an object")
        name = anim_name(raw.get("name"))
        if name in seen_anims:
            raise RigError(f"duplicate animation name {name!r}")
        seen_anims.add(name)
        frames = [_frame(f, f"animations[{i}].frames") for f in (raw.get("frames") or [])]
        if not frames:
            raise RigError(f"animation {name!r} has no frames")
        loop = raw.get("loop")
        out["animations"].append({
            "name": name,
            "frames": frames,
            "loop": bool(loop) if loop is not None else name not in NO_LOOP,
            "fps": (_float(raw["fps"], f"animations[{i}].fps", lo=0.1, hi=240.0)
                    if raw.get("fps") is not None else None),
        })

    seen_labels: set[tuple[str, int]] = set()
    for i, raw in enumerate(data.get("labels") or []):
        if not isinstance(raw, dict):
            raise RigError(f"labels[{i}] must be an object")
        slot = slot_name(raw.get("slot"))
        frame = _frame(raw.get("frame"), f"labels[{i}].frame")
        key = (slot, frame)
        if key in seen_labels:
            # One slot, one place, per frame. Two "main_hand" anchors in frame 3
            # is not extra information, it is an unresolvable contradiction the
            # gear stamper would silently pick a winner from.
            raise RigError(f"two {slot!r} labels on frame {frame}")
        seen_labels.add(key)
        lim_w = out["grid"]["cell_w"] if out["grid"] else 8192
        lim_h = out["grid"]["cell_h"] if out["grid"] else 8192
        entry = {
            "slot": slot,
            "frame": frame,
            "x": _float(raw.get("x"), f"labels[{i}].x", lo=0.0, hi=float(lim_w)),
            "y": _float(raw.get("y"), f"labels[{i}].y", lo=0.0, hi=float(lim_h)),
            "source": str(raw.get("source") or AUTHORED)[:32],
            "note": str(raw.get("note") or "")[:200],
        }
        if raw.get("angle") is not None:
            entry["angle"] = _float(raw["angle"], f"labels[{i}].angle",
                                    lo=-360.0, hi=360.0)
        out["labels"].append(entry)

    out["labels"].sort(key=lambda d: (d["frame"], d["slot"]))
    return out


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def load(sheet: str | os.PathLike[str]) -> dict:
    """The rig for a sheet. A missing sidecar is an EMPTY rig, not an error.

    A corrupt sidecar IS an error: silently discarding hand-placed anchors
    because a byte flipped would be the worst possible failure here.
    """
    path = sidecar_path(sheet)
    if not path.is_file():
        return empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RigError(f"{path.name} is unreadable: {exc}") from exc
    data = normalise(raw)
    data["updated_at"] = raw.get("updated_at")
    return data


def save(sheet: str | os.PathLike[str], data: dict, *,
         sheet_size: Optional[tuple[int, int]] = None) -> dict:
    """Normalise, then write the sidecar atomically. Returns what was written."""
    out = normalise(data, sheet_size=sheet_size)
    out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = sidecar_path(sheet)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return out


def delete(sheet: str | os.PathLike[str]) -> bool:
    path = sidecar_path(sheet)
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Reading the rig
# ---------------------------------------------------------------------------
def grid_of(data: dict, sheet_size: Optional[tuple[int, int]] = None) -> Optional[dict]:
    """The rig's grid, falling back to a 1x1 grid over the whole sheet."""
    if data.get("grid"):
        return data["grid"]
    if sheet_size:
        w, h = sheet_size
        return {"cell_w": int(w), "cell_h": int(h), "cols": 1, "rows": 1}
    return None


def frame_rc(data: dict, frame: int) -> tuple[int, int]:
    """Frame index -> (row, col) under the rig's grid."""
    grid = data.get("grid") or {"cols": 1}
    cols = max(1, int(grid.get("cols") or 1))
    return divmod(int(frame), cols)


def anchors_for(data: dict, slot: str):
    """The labelled anchors for one slot, as ``gear.Anchor`` objects.

    Imported lazily so this module stays usable (labels, animations, coverage)
    in a process that never touches the gear pipeline.
    """
    from bgate_core.gear import Anchor      # local: keeps gear optional here

    want = slot_name(slot)
    out = []
    for lab in data.get("labels") or []:
        if lab["slot"] != want:
            continue
        row, col = frame_rc(data, lab["frame"])
        out.append(Anchor(row=row, col=col, x=float(lab["x"]), y=float(lab["y"]),
                          source=lab.get("source") or AUTHORED, support=1))
    return out


def slots_used(data: dict) -> list[str]:
    return sorted({lab["slot"] for lab in data.get("labels") or []})


def coverage(data: dict) -> dict:
    """Which frames carry which slots, and which are still bare.

    The editor renders this directly: a frame strip where an unlabelled cell is
    visibly a hole. "Rigged" is not a boolean, it is a per-slot, per-frame grid,
    and pretending otherwise is how a weapon vanishes on exactly one attack.
    """
    grid = data.get("grid")
    cells = (int(grid["cols"]) * int(grid["rows"])) if grid else 0
    used = slots_used(data)
    by_slot: dict[str, list[int]] = {s: [] for s in used}
    for lab in data.get("labels") or []:
        by_slot[lab["slot"]].append(lab["frame"])
    # A frame only counts as "in an animation" if some animation names it —
    # sheets routinely carry padding cells nobody plays, and calling those
    # unrigged would make every sheet look broken.
    played: set[int] = set()
    for anim in data.get("animations") or []:
        played.update(anim["frames"])
    return {
        "frames": cells,
        "slots": used,
        "by_slot": {s: sorted(v) for s, v in by_slot.items()},
        "played": sorted(played),
        "missing": {
            s: sorted(played - set(by_slot[s])) for s in used
        } if played else {s: [] for s in used},
    }


# ---------------------------------------------------------------------------
# Godot export
# ---------------------------------------------------------------------------
def spriteframes_text(data: dict, sheet_filename: str, res_dir: str, *,
                      sheet_size: Optional[tuple[int, int]] = None) -> str:
    """A Godot 4 SpriteFrames over the rig's grid and animation list.

    sprites.py's writer assumes a horizontal strip consumed in sheet order,
    which is exactly the assumption a hand-painted multi-row sheet breaks. Here
    each animation names its own frames by index, so a 4x3 sheet whose walk
    cycle is the middle row and whose attack runs down a column both export
    correctly — and the regions are computed from the grid, not from position
    in a queue.

    Only the frames some animation actually references become AtlasTextures;
    padding cells cost nothing in the exported resource.
    """
    grid = grid_of(data, sheet_size)
    if not grid:
        raise RigError("no grid — a SpriteFrames needs cell dimensions")
    anims = data.get("animations") or []
    if not anims:
        raise RigError("no animations — label at least one before exporting")

    cw, ch = int(grid["cell_w"]), int(grid["cell_h"])
    cols = max(1, int(grid["cols"]))
    default_fps = float(data.get("fps") or 10.0)
    res_dir = res_dir.strip("/").replace("\\", "/")
    prefix = f"res://{res_dir}/" if res_dir else "res://"

    # Stable, deduplicated atlas ids: one sub-resource per referenced frame,
    # in ascending frame order, so a re-export produces a byte-identical file
    # for an unchanged rig (diffs stay reviewable).
    used = sorted({f for a in anims for f in a["frames"]})
    atlas_id = {f: i for i, f in enumerate(used)}

    lines = [
        f"[gd_resource type=\"SpriteFrames\" load_steps={len(used) + 2} format=3]",
        "",
        f"[ext_resource type=\"Texture2D\" path=\"{prefix}{sheet_filename}\" id=\"1\"]",
        "",
    ]
    for frame in used:
        row, col = divmod(frame, cols)
        lines += [
            f"[sub_resource type=\"AtlasTexture\" id=\"atlas_{atlas_id[frame]}\"]",
            "atlas = ExtResource(\"1\")",
            f"region = Rect2({col * cw}, {row * ch}, {cw}, {ch})",
            "",
        ]
    blocks = []
    for anim in anims:
        frames = ", ".join(
            '{\n"duration": 1.0,\n"texture": SubResource("atlas_%d")\n}' % atlas_id[f]
            for f in anim["frames"])
        speed = anim.get("fps") or default_fps
        blocks.append(
            '{\n"frames": [%s],\n"loop": %s,\n"name": &"%s",\n"speed": %s\n}'
            % (frames, "true" if anim["loop"] else "false", anim["name"], speed))
    lines += ["[resource]", "animations = [" + ", ".join(blocks) + "]", ""]
    return "\n".join(lines)


def autoslice(sheet_size: tuple[int, int], cell: tuple[int, int]) -> dict:
    """A grid from an explicit cell size. Refuses a cell that does not tile."""
    w, h = int(sheet_size[0]), int(sheet_size[1])
    cw, ch = int(cell[0]), int(cell[1])
    if cw <= 0 or ch <= 0:
        raise RigError("cell size must be positive")
    if w % cw or h % ch:
        raise RigError(f"cell {cw}x{ch} does not tile a {w}x{h} sheet")
    cols, rows = w // cw, h // ch
    if cols * rows > MAX_CELLS:
        raise RigError(f"{cols}x{rows} is {cols * rows} cells; the cap is {MAX_CELLS}")
    return {"cell_w": cw, "cell_h": ch, "cols": cols, "rows": rows}


def swap_hands(data: dict, frame: Optional[int] = None) -> dict:
    """Exchange left_hand and right_hand — on one frame, or on all of them.

    A character that turns around keeps its main hand and swaps its anatomical
    ones. Re-clicking both anchors to express that is the kind of chore that
    ends with a half-labelled sheet, so it is one action.
    """
    left, right = HAND_SLOTS
    for lab in data.get("labels") or []:
        if frame is not None and lab["frame"] != frame:
            continue
        if lab["slot"] == left:
            lab["slot"] = right
        elif lab["slot"] == right:
            lab["slot"] = left
    (data.get("labels") or []).sort(key=lambda d: (d["frame"], d["slot"]))
    return data


def hand_coverage(data: dict) -> dict[int, list[str]]:
    """frame -> which of the two anatomical hands carry an anchor there."""
    out: dict[int, list[str]] = {}
    for lab in data.get("labels") or []:
        if lab["slot"] in HAND_SLOTS:
            out.setdefault(lab["frame"], []).append(lab["slot"])
    return {f: sorted(v) for f, v in out.items()}


def rows_as_animations(grid: dict, names: Iterable[str]) -> list[dict]:
    """One animation per sheet ROW — the layout most hand-painted sheets use.

    A convenience for the editor's "guess my animations" button, not a rule:
    the artist renames and re-splits from there.
    """
    cols, rows = int(grid["cols"]), int(grid["rows"])
    names = list(names)
    out = []
    for r in range(rows):
        name = anim_name(names[r]) if r < len(names) and names[r] else f"anim_{r}"
        out.append({
            "name": name,
            "frames": [r * cols + c for c in range(cols)],
            "loop": name not in NO_LOOP,
            "fps": None,
        })
    return out
