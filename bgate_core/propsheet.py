"""Prop art, from a generated image to a packed atlas the level can use.

THIS EXISTS BECAUSE IT WAS DONE BY HAND FIRST AND THE HAND VERSION SKIPPED
STEPS. Character work cannot skip them — `animation_generate` runs conform,
defringe and the battery whether the caller remembers to or not. Props had no
such path, so a scratch script generated them, and it dropped the palette
conform, dropped the defringe, and shipped 32-pixel sprites carrying six
hundred colours of which two thirds were off the pinned palette. Nobody
decided that. There was simply nowhere for the decision to live.

So the chain lives here, in one function, and every step is mandatory:

  KEY the flat background out (a client-side flood from the border, never the
  provider's remove_bg — that one keys holes through pale material)
  FIT to the contract box from `props.art_spec`, stepping down in halves,
  because a 32x reduction in one jump turns a detailed subject into noise
  HARDEN the alpha, because pixel art has no partial alpha and the step-down
  leaves a feathered rim that `lock_palette` does not touch
  CONFORM to the pinned palette, which snaps colour and defringes stray ink
  SEAT it on the ground anchor, bottom-centre, so a prop that floats in its
  box does not float in the level

and then the atlas: every prop gets a 2x2 CELL SLOT whatever its real size,
because Godot refuses a spanning tile whose region overlaps another — it logs,
drops the tile, and the map draws nothing there — and never packing them close
enough to collide is cheaper than detecting it.

Pure image and layout work. Nothing here calls a generator: the caller brings
the pictures, this makes them usable.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

from bgate_core import props as _props

#: Cells per prop slot, each way. Two, because the biggest declared prop is 2x2
#: and a uniform slot makes overlap impossible by construction.
SLOT = 2

#: Prop slots per atlas row. Keeps the sheet roughly square at 21 types.
COLS = 6

#: Below this, an edge pixel is transparent; at or above it, opaque. Pixel art
#: has no in-between, and the feathered rim a box step-down leaves is the halo
#: the house rules already forbid.
ALPHA_CUT = 128

#: How far a prop's colours may sit from the pinned palette before the sheet is
#: refused. Squared-distance is not used: this is a plain RGB distance, so 30 is
#: about "a shade off" rather than "a different colour".
OFF_PALETTE_MAX = 0.02


class PropSheetError(ValueError):
    """Art that cannot be made to fit the contract."""


#: How much of a prop's cell EDGE may be opaque before the sheet is suspect.
#: A prop touching its border is either oversized or carrying its own
#: background — a wall torch came back with a slab of masonry attached and
#: filled 23% of its edge, a crate filled 56%, and the one prop judged good by
#: eye filled 3%. One number catches both defects.
BORDER_MAX = 0.10


def border_fill(image) -> float:
    """The share of a sprite's TOP, LEFT and RIGHT edges that is opaque.

    The bottom is deliberately excluded: every prop is seated on its ground
    anchor, so touching the bottom edge is where it meets the floor, not a
    defect. Counting it flagged a correctly-seated sprite and would have
    trained the gate to be ignored.
    """
    import numpy as np

    a = np.asarray(image.convert("RGBA"))
    op = a[..., 3] > 0
    ring = np.concatenate([op[0], op[:, 0], op[:, -1]])
    return float(ring.mean()) if ring.size else 0.0


def conform(image, *, size, palette: Optional[Sequence] = None,
            art_size=None):
    """One generated picture into one contract-shaped, palette-locked sprite.

    Returns ``(image, report)``. The report carries the numbers a caller should
    refuse on rather than a bare success: colour count, the share sitting off
    the pinned palette, and how many pixels were still partly transparent
    before the alpha was hardened.
    """
    from PIL import Image

    im = image.convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    if not im.width or not im.height:
        raise PropSheetError("the image is empty once the background is keyed "
                             "— the generation came back blank or all one "
                             "colour")
    w, h = int(size[0]), int(size[1])
    # THE ART BOX IS SMALLER THAN THE CELL. A prop scaled to fill its cell edge
    # to edge reads as a tile rather than as an object standing on one — the
    # crate came back at 91% of its cell and looked like flooring.
    aw, ah = (int(art_size[0]), int(art_size[1])) if art_size else (w, h)

    # STEP DOWN IN HALVES. A 1024->32 reduction sampled in one go survived a
    # barrel's simple silhouette and turned a chest into brown noise; a box
    # filter applied repeatedly averages detail away instead of dropping it.
    scale = min(aw / im.width, ah / im.height)
    target = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    while im.width > target[0] * 2 and im.height > target[1] * 2:
        im = im.resize((im.width // 2, im.height // 2), Image.BOX)
    if im.size != target:
        im = im.resize(target, Image.BOX)

    # SEAT ON THE GROUND ANCHOR — bottom centre, per art_spec.
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(im, ((w - im.width) // 2, h - im.height))

    import numpy as np

    arr = np.asarray(out).copy()
    feathered = int(((arr[..., 3] > 0) & (arr[..., 3] < 255)).sum())
    arr[..., 3] = np.where(arr[..., 3] >= ALPHA_CUT, 255, 0)
    out = Image.fromarray(arr)

    report = {"size": [w, h], "art_size": [aw, ah], "feathered": feathered,
              "border_fill": border_fill(out)}
    if palette:
        pal = np.array([[int(v) for v in c[:3]] for c in palette])
        opaque = np.asarray(out)[np.asarray(out)[..., 3] > 0][:, :3].astype(int)
        if len(opaque):
            far = np.sqrt(((opaque[:, None, :] - pal[None, :, :]) ** 2)
                          .sum(axis=2)).min(axis=1)
            report["colours_before"] = len({tuple(p) for p in opaque})
            report["off_palette_before"] = float((far > 30).mean())
    return out, report


def measure(image, palette: Optional[Sequence] = None) -> dict:
    """Colour count, off-palette share and feathering, for a gate to refuse on."""
    import numpy as np

    a = np.asarray(image.convert("RGBA"))
    opaque = a[a[..., 3] > 0][:, :3].astype(int)
    out = {"colours": len({tuple(p) for p in opaque}) if len(opaque) else 0,
           "feathered": int(((a[..., 3] > 0) & (a[..., 3] < 255)).sum()),
           "opaque_fraction": float((a[..., 3] > 0).mean()),
           "border_fill": border_fill(image)}
    if palette is not None and len(opaque):
        pal = np.array([[int(v) for v in c[:3]] for c in palette])
        far = np.sqrt(((opaque[:, None, :] - pal[None, :, :]) ** 2)
                      .sum(axis=2)).min(axis=1)
        out["off_palette"] = float((far > 30).mean())
    return out


def slots_for(names: Sequence[str], *, view: str = "") -> list:
    """One atlas slot per DRAWING, which is not one per type.

    A wall mount needs a drawing per facing: Godot's flip bit mirrors a sprite
    but not its `texture_origin`, so a mirrored torch cannot also be seated
    against the opposite wall.
    """
    out = []
    for name in names:
        spec = _props.art_spec(name, view=view)
        if spec["facings"]:
            out += [(name, f) for f in spec["facings"]]
        else:
            out.append((name, ""))
    return out


def pack(images: dict, names: Sequence[str], *, tile_px: int = 32,
         view: str = "") -> dict:
    """Lay the sprites out. ``{image, atlas, tiles, sizes, spec}``

    ``images`` maps a type to its conformed sprite. ``atlas`` is the map
    `props.cells` takes; ``spec`` is the same thing as the string
    `level_generate` accepts, so a caller never has to build that by hand —
    which is what a string mini-language in a tool parameter otherwise forces.
    """
    from PIL import Image

    slots = slots_for(names, view=view)
    missing = sorted({n for n, _ in slots} - set(images))
    if missing:
        raise PropSheetError(f"no sprite supplied for {missing}")

    rows = (len(slots) + COLS - 1) // COLS
    sheet = Image.new("RGBA", (COLS * SLOT * tile_px, rows * SLOT * tile_px),
                      (0, 0, 0, 0))
    atlas: dict = {}
    sizes: dict = {}
    tiles: list = []
    for i, (name, facing) in enumerate(slots):
        spec = _props.art_spec(name, tile_px=tile_px, view=view)
        w, h = spec["cells"]
        ax, ay = (i % COLS) * SLOT, (i // COLS) * SLOT
        im = images[name]
        if facing and facing != spec.get("facings", [facing])[0]:
            im = im.transpose(Image.FLIP_LEFT_RIGHT)
        sheet.alpha_composite(im.convert("RGBA"), (ax * tile_px, ay * tile_px))
        tiles.append((ax, ay))
        if (w, h) != (1, 1):
            sizes[(ax, ay)] = (w, h)
        if facing:
            atlas.setdefault(name, {})[facing] = (ax, ay)
        else:
            atlas[name] = (ax, ay)

    bits = []
    for name, v in atlas.items():
        if isinstance(v, dict):
            bits += [f"{name}.{f}={c[0]},{c[1]}" for f, c in sorted(v.items())]
        else:
            bits.append(f"{name}={v[0]},{v[1]}")
    return {"image": sheet, "atlas": atlas, "tiles": tiles, "sizes": sizes,
            "spec": " ".join(sorted(bits)), "slots": len(slots)}


def animation_frames(names: Sequence[str], atlas: dict, *,
                     view: str = "") -> dict:
    """``{(ax, ay): {frames, columns, speed}}`` for `tilemap.write_tileset`.

    Only the types that declare a LOOP. A state machine is not a loop and
    cannot be expressed as one — `art_spec` reports which a type has.
    """
    out: dict = {}
    for name in names:
        spec = _props.art_spec(name, view=view)
        if spec["motion"] != "loop":
            continue
        spot = atlas.get(name)
        coords = (list(spot.values()) if isinstance(spot, dict)
                  else [spot] if spot else [])
        for c in coords:
            out[tuple(c)] = {"frames": spec["frames"], "columns": spec["frames"],
                             "speed": float(spec["fps"])}
    return out


def write(sheet, dest: str | os.PathLike[str]) -> str:
    """Save the packed atlas, making the folder if it is not there."""
    from pathlib import Path

    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(p)
    return str(p)
