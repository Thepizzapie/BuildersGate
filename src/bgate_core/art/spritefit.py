"""One standing height per character, on every sheet, mechanically.

THE FAULT THIS ENDS. On the benchmark game the four party members were
generated into a 64x96 cell and stood 49, 52, 49 and 64 pixels tall - three
different sizes of person, one of them 30% taller than the rest - and every
asset-level check passed, because each sheet was internally consistent. The
enemies, generated separately, stood 96 to 320. The frame read as a party of
ants under a wall of bosses. The art agent then wrote a script by hand
(party_fit.py) that did exactly what this module does: one scale factor per
character from the idle frame, LANCZOS on premultiplied alpha, alpha hardened
to 0/255, feet on one row, colours snapped back to the pinned palette. That
script lived in one game's art/ folder; this is it as a tool.

Two operations:

  fit_sheet    a sheet -> the same sheet with its ANCHOR frame (the idle)
               standing exactly `standing_px` tall, every other frame scaled
               by the same factor so a raised staff stays raised, feet on
               `feet_row`. A frame that does not fit the cell after scaling
               is shrunk alone and REPORTED, never cropped: a cropped-off
               weapon arm is what an unreported overflow shipped as.
  family_check a character's sheets (battle, overworld, portrait ...) read
               together: do they agree on the standing height, the feet row
               and the palette? A family that disagrees is two characters
               wearing one name.

Pure pixels. No model, no engine, no money.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image

#: Alpha at or above this is opaque after a fit; everything below is clear.
ALPHA_HARD = 128
#: The feet row when nobody says: leave a little air under the figure so a
#: one-pixel shadow line is not cut off.
FEET_MARGIN = 3
#: Upscaling pixel art invents pixels. The benchmark's party needed 1.8x
#: (49px figures in a 96px cell) and shipped fine from it; past 2x the fit
#: refuses and says to regenerate at size. Any upscale is reported.
MAX_UPSCALE = 2.0
#: Family tolerance on the standing height, as a fraction.
HEIGHT_TOL = 0.06
#: Feet rows in a family may differ by this many pixels.
FEET_TOL = 2
#: Share of a sheet's opaque pixels that may be colours no sibling uses.
PALETTE_TOL = 0.12


class FitError(ValueError):
    """A sheet this cannot fit honestly; refused, never approximated."""


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _frames(img: Image.Image, cell: tuple[int, int]) -> list[tuple[int, int]]:
    """Top-left of every cell in a uniform grid, row-major."""
    cw, ch = cell
    if cw <= 0 or ch <= 0:
        raise FitError("cell must be [width, height] > 0")
    if img.width % cw or img.height % ch:
        raise FitError(f"{img.width}x{img.height} is not a whole number of "
                       f"{cw}x{ch} cells - say src_cell for the sheet's own grid")
    return [(x * cw, y * ch) for y in range(img.height // ch)
            for x in range(img.width // cw)]


def _bbox(frame: Image.Image) -> Optional[tuple[int, int, int, int]]:
    alpha = frame.getchannel("A").point(lambda a: 255 if a >= ALPHA_HARD else 0)
    return alpha.getbbox()


def measure(path: str | os.PathLike[str], cell: Sequence[int], *,
            src_cell: Optional[Sequence[int]] = None) -> dict:
    """Per-frame opaque boxes of a sheet, plus the anchor's standing height."""
    with Image.open(path) as im:
        img = im.convert("RGBA")
    grid = tuple(int(v) for v in (src_cell or cell))
    boxes = []
    for (x, y) in _frames(img, grid):
        frame = img.crop((x, y, x + grid[0], y + grid[1]))
        bb = _bbox(frame)
        boxes.append({"cell": [x, y], "box": list(bb) if bb else None,
                      "height": (bb[3] - bb[1]) if bb else 0,
                      "width": (bb[2] - bb[0]) if bb else 0,
                      "feet": bb[3] if bb else None})
    return {"path": str(path), "size": [img.width, img.height], "cell": list(grid),
            "frames": boxes}


def _palette_of(img: Image.Image) -> dict[tuple[int, int, int], int]:
    out: dict[tuple[int, int, int], int] = {}
    raw = img.tobytes()
    for i in range(0, len(raw), 4):
        if raw[i + 3] >= ALPHA_HARD:
            key = (raw[i], raw[i + 1], raw[i + 2])
            out[key] = out.get(key, 0) + 1
    return out


def _snap(img: Image.Image, palette: Sequence[Sequence[int]]) -> Image.Image:
    """Every opaque pixel to its nearest palette colour; alpha untouched."""
    pal = [tuple(int(v) for v in c[:3]) for c in palette]
    if not pal:
        return img
    cache: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a < ALPHA_HARD:
                continue
            key = (r, g, b)
            hit = cache.get(key)
            if hit is None:
                hit = min(pal, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2)
                cache[key] = hit
            px[x, y] = (hit[0], hit[1], hit[2], 255)
    return img


def _resample(frame: Image.Image, scale: float) -> Image.Image:
    """LANCZOS on premultiplied alpha, then the alpha hardened to 0/255.

    Premultiplying stops the transparent pixels' (usually black) colour from
    bleeding into the edge, which is the dark fringe every naive resize of a
    keyed sprite carries; hardening the alpha keeps it pixel art.
    """
    if frame.width == 0 or frame.height == 0:
        return frame
    w = max(1, round(frame.width * scale))
    h = max(1, round(frame.height * scale))
    if scale == 1.0:
        out = frame.copy()
    else:
        pre = Image.new("RGBA", frame.size)
        src = frame.load(); dst = pre.load()
        for y in range(frame.height):
            for x in range(frame.width):
                r, g, b, a = src[x, y]
                dst[x, y] = (r * a // 255, g * a // 255, b * a // 255, a)
        small = pre.resize((w, h), Image.LANCZOS)
        out = Image.new("RGBA", (w, h))
        sp = small.load(); op = out.load()
        for y in range(h):
            for x in range(w):
                r, g, b, a = sp[x, y]
                if a >= ALPHA_HARD:
                    op[x, y] = (min(255, r * 255 // a), min(255, g * 255 // a),
                                min(255, b * 255 // a), 255)
                else:
                    op[x, y] = (0, 0, 0, 0)
    hard = out.getchannel("A").point(lambda a: 255 if a >= ALPHA_HARD else 0)
    out.putalpha(hard)
    return out


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------

def fit_sheet(src: str | os.PathLike[str], out: str | os.PathLike[str], *,
              cell: Sequence[int], standing_px: int, feet_row: Optional[int] = None,
              anchor_frame: int = 0, src_cell: Optional[Sequence[int]] = None,
              palette: Optional[Sequence[Sequence[int]]] = None,
              max_upscale: float = MAX_UPSCALE) -> dict:
    """Refit a sheet so its anchor frame stands `standing_px` tall.

    `cell` is the OUTPUT cell; `src_cell` the input grid when it differs
    (the benchmark went 64x96 -> 96x96 because a 90px figure's attack frame
    is 91px wide). Every frame keeps its own offset from the anchor's feet,
    scaled - a jump frame stays in the air. Returns the per-frame report;
    `overflow` names frames that had to be shrunk alone to fit the cell.
    """
    cw, ch = (int(cell[0]), int(cell[1]))
    standing_px = int(standing_px)
    if standing_px < 4 or standing_px > ch:
        raise FitError(f"standing_px {standing_px} must be 4..{ch} (the cell height)")
    feet = int(feet_row) if feet_row is not None else ch - FEET_MARGIN
    if not 0 < feet <= ch:
        raise FitError(f"feet_row {feet} is outside the {ch}px cell")
    with Image.open(src) as im:
        img = im.convert("RGBA")
    grid = tuple(int(v) for v in (src_cell or (cw, ch)))
    origins = _frames(img, grid)
    if not origins:
        raise FitError("the sheet has no cells")
    if not 0 <= anchor_frame < len(origins):
        raise FitError(f"anchor_frame {anchor_frame} is past the {len(origins)} cells")
    frames = [img.crop((x, y, x + grid[0], y + grid[1])) for (x, y) in origins]
    boxes = [_bbox(f) for f in frames]
    abox = boxes[anchor_frame]
    if abox is None:
        raise FitError(f"the anchor frame {anchor_frame} is empty - it should be the idle")
    a_height = abox[3] - abox[1]
    scale = standing_px / float(a_height)
    if scale > max_upscale:
        raise FitError(
            f"the anchor stands {a_height}px and {standing_px}px asks for a "
            f"{scale:.2f}x upscale (limit {max_upscale}x) - pixel art invented that "
            "far reads as blur; regenerate at size instead")
    a_feet_src = abox[3]
    a_cx_src = (abox[0] + abox[2]) / 2.0

    columns = img.width // grid[0]
    rows = img.height // grid[1]
    sheet = Image.new("RGBA", (columns * cw, rows * ch), (0, 0, 0, 0))
    report: list[dict] = []
    overflow: list[int] = []
    for i, (frame, bb) in enumerate(zip(frames, boxes)):
        col, row = i % columns, i // columns
        if bb is None:
            report.append({"frame": i, "height_before": 0, "height_after": 0})
            continue
        crop = frame.crop(bb)
        fitted = _resample(crop, scale)
        # Where this frame's feet sit relative to the anchor's, in the output.
        feet_delta = (a_feet_src - bb[3]) * scale
        bottom = feet - round(feet_delta)
        this_scale = scale
        if fitted.height > ch or fitted.width > cw:
            shrink = min(ch / fitted.height, cw / fitted.width)
            fitted = _resample(crop, scale * shrink)
            this_scale = scale * shrink
            overflow.append(i)
            bottom = min(bottom, ch)
        top = bottom - fitted.height
        if top < 0:
            bottom -= top
            top = 0
        cx = round(cw / 2.0 + ((bb[0] + bb[2]) / 2.0 - a_cx_src) * scale)
        left = max(0, min(cw - fitted.width, cx - fitted.width // 2))
        sheet.paste(fitted, (col * cw + left, row * ch + top), fitted)
        report.append({"frame": i, "height_before": bb[3] - bb[1],
                       "height_after": fitted.height, "scale": round(this_scale, 4),
                       "feet_row": top + fitted.height})
    if palette:
        sheet = _snap(sheet, palette)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return {"ok": True, "path": str(out), "cell": [cw, ch], "src_cell": list(grid),
            "frames": len(frames), "anchor_frame": anchor_frame,
            "anchor_height_before": a_height, "standing_px": standing_px,
            "scale": round(scale, 4), "upscaled": scale > 1.05,
            "feet_row": feet, "overflow": overflow,
            "palette_snapped": bool(palette), "report": report}


# ---------------------------------------------------------------------------
# The family
# ---------------------------------------------------------------------------

def family_check(sheets: Sequence[dict], *, standing_px: int = 0,
                 height_tol: float = HEIGHT_TOL, feet_tol: int = FEET_TOL,
                 palette_tol: float = PALETTE_TOL, same_character: bool = True,
                 palette: Optional[Sequence[Sequence[int]]] = None) -> dict:
    """Do a character's sheets agree with each other, and with the contract?

    `sheets` is [{"path", "cell", "label"?, "anchor_frame"?}]. With
    `standing_px` every sheet's anchor is held to it; without, the sheets are
    held to their own median. Findings: height_mismatch, feet_drift,
    palette_drift, off_palette, empty_anchor.

    `same_character` is what `palette_drift` means: the sheets are one
    character, so a colour only one of them uses is drift. For a PARTY -
    four people, four outfits - pass False and the sibling comparison is
    skipped; the heights still have to agree. With `palette` (the project's
    pinned colours) every sheet is also held to it: `off_palette`.
    """
    rows: list[dict] = []
    palettes: list[dict[tuple[int, int, int], int]] = []
    for s in sheets:
        cell = tuple(int(v) for v in s["cell"])
        anchor = int(s.get("anchor_frame", 0))
        with Image.open(s["path"]) as im:
            img = im.convert("RGBA")
        origins = _frames(img, cell)
        label = str(s.get("label") or Path(s["path"]).stem)
        if not 0 <= anchor < len(origins):
            rows.append({"label": label, "path": str(s["path"]), "height": 0, "feet": None})
            palettes.append({})
            continue
        x, y = origins[anchor]
        frame = img.crop((x, y, x + cell[0], y + cell[1]))
        bb = _bbox(frame)
        rows.append({"label": label, "path": str(s["path"]), "cell": list(cell),
                     "height": (bb[3] - bb[1]) if bb else 0,
                     "feet": (bb[3]) if bb else None,
                     "feet_from_bottom": (cell[1] - bb[3]) if bb else None})
        palettes.append(_palette_of(img))

    findings: list[dict] = []
    heights = [r["height"] for r in rows if r["height"]]
    target = float(standing_px) if standing_px else (
        sorted(heights)[len(heights) // 2] if heights else 0.0)
    for r in rows:
        if not r["height"]:
            findings.append({"code": "empty_anchor", "severity": "fail", "sheet": r["label"],
                             "detail": f"{r['label']}: the anchor frame has no opaque pixels"})
            continue
        if target and abs(r["height"] - target) > height_tol * target:
            findings.append({"code": "height_mismatch", "severity": "fail", "sheet": r["label"],
                             "measured": r["height"], "expected": round(target),
                             "detail": (f"{r['label']} stands {r['height']}px; the "
                                        f"{'contract' if standing_px else 'family'} says "
                                        f"{round(target)}px (±{int(height_tol * 100)}%)")})
    feet = [r["feet_from_bottom"] for r in rows if r.get("feet_from_bottom") is not None]
    if len(feet) > 1:
        med = sorted(feet)[len(feet) // 2]
        for r in rows:
            f = r.get("feet_from_bottom")
            if f is not None and abs(f - med) > feet_tol:
                findings.append({"code": "feet_drift", "severity": "warn", "sheet": r["label"],
                                 "measured": f, "expected": med,
                                 "detail": (f"{r['label']}'s feet sit {f}px above the cell "
                                            f"bottom; the family's sit at {med}px")})
    pinned = {tuple(int(v) for v in c[:3]) for c in (palette or [])}
    if pinned:
        for r, pal in zip(rows, palettes):
            total = sum(pal.values())
            off = sum(n for c, n in pal.items() if c not in pinned)
            share = off / float(total) if total else 0.0
            if share > palette_tol:
                findings.append({"code": "off_palette", "severity": "fail", "sheet": r["label"],
                                 "measured": round(share, 3), "expected": f"<= {palette_tol}",
                                 "detail": (f"{int(share * 100)}% of {r['label']}'s pixels are "
                                            "not in the pinned palette - conform it "
                                            "(sprite_fit snaps, aseprite conform quantises)")})
    if len(palettes) > 1 and same_character:
        for i, (r, pal) in enumerate(zip(rows, palettes)):
            if not pal:
                continue
            others: set[tuple[int, int, int]] = set()
            for j, other in enumerate(palettes):
                if j != i:
                    others |= set(other)
            total = sum(pal.values())
            strange = sum(n for c, n in pal.items() if c not in others)
            share = strange / float(total) if total else 0.0
            if share > palette_tol:
                findings.append({"code": "palette_drift", "severity": "warn", "sheet": r["label"],
                                 "measured": round(share, 3), "expected": f"<= {palette_tol}",
                                 "detail": (f"{int(share * 100)}% of {r['label']}'s pixels are "
                                            "colours no sibling sheet uses - a different "
                                            "palette, or a different character")})
    fails = [f for f in findings if f["severity"] == "fail"]
    return {"ok": not fails, "sheets": rows, "target_height": round(target) if target else 0,
            "findings": findings,
            "counts": {"fail": len(fails), "warn": len(findings) - len(fails)}}


def fit_to_band(path: str | os.PathLike[str], *, max_height: int,
                palette: Optional[Sequence[Sequence[int]]] = None) -> dict:
    """Shrink one keyed image (an enemy, a prop) so its opaque height is at
    most `max_height`, in place. The delivery-time half of the scale
    contract: an enemy generated at 320px against a 96px party is downscaled
    to the band's top before anyone wires it, not measured after."""
    with Image.open(path) as im:
        img = im.convert("RGBA")
    bb = _bbox(img)
    if bb is None:
        return {"ok": False, "reason": "no opaque pixels"}
    height = bb[3] - bb[1]
    if height <= max_height:
        return {"ok": True, "changed": False, "height": height}
    scale = max_height / float(height)
    crop = img.crop(bb)
    fitted = _resample(crop, scale)
    if palette:
        fitted = _snap(fitted, palette)
    out = Image.new("RGBA", fitted.size, (0, 0, 0, 0))
    out.paste(fitted, (0, 0), fitted)
    out.save(path)
    return {"ok": True, "changed": True, "height_before": height,
            "height_after": fitted.height, "scale": round(scale, 4),
            "size": [out.width, out.height]}


def as_palette(colors: Any) -> Optional[list[tuple[int, int, int]]]:
    """A pinned palette as this module reads it: a list of RGB triples, from
    a list of triples, a list of "#rrggbb" strings, or None."""
    if not colors:
        return None
    out = []
    for c in colors:
        if isinstance(c, str):
            s = c.lstrip("#")
            if len(s) >= 6:
                out.append((int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)))
        else:
            try:
                out.append((int(c[0]), int(c[1]), int(c[2])))
            except (TypeError, ValueError, IndexError):
                continue
    return out or None
