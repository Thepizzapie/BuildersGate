"""Generating the PARTS of a cutout character — one keyed image per slot, all
conditioned on one pinned identity reference, each carrying the hash of the
reference it was drawn against.

WHY THIS EXISTS, MEASURED. EXIT 67 (2026-09-20) built a Metal Slug run-and-gun
cast on frame sheets. Per-frame generation re-rolls identity, proportions, the
weapon in the hand and the death pose on every call, and after twelve hours
the human failed the build on sight: gnome frames with a bearded biker in them,
a sniper whose death carried a second head, a gun floating in front of one
painted arm. The human had said on night one that the game needed a 2D rig.
This module is the generation half of that rig: about ten parts per character,
drawn ONCE, animated by the template forever, weapons hanging from the hand
bone so the grip is inside the hand in every frame by construction.

THE RULES THE FRAME PIPELINE TAUGHT US, applied here rather than re-learned:

  * One reference, hashed, on every part. `anchor_hash` on each skin entry and
    `reference_hash` on the document; `cutout.status` flags a part from another
    run as `stale_reference`. Nothing here can assemble a kit from two sources
    without the document saying so.
  * Parts go through `chroma.generate`, never a raw provider call. Model-native
    transparency ships fringes and punched eye-whites, and a fringe on a
    forearm's inner seam is visible against every torso forever.
  * Scale is checked at generation, not discovered at assembly. Each template
    part names its height as a fraction of the figure; the reference's own
    alpha bbox is the ruler; a part that comes back outside the band is
    FLAGGED (never silently rescaled) so a human can look before it is wired.
  * Money stops. The plan is priced before the first call and refused over
    `max_paid_calls`; two consecutive provider failures end the batch rather
    than re-rolling at $0.05 a frame (EXIT 67 #11: $10.10 on one item, killed by
    hand).

Pure orchestration: generation is injected (`generate=`), so the whole flow is
testable with a fake that writes PNGs, and the real run is one keyword away.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

from . import cutout

# A part further than this from its template height is flagged. Wide on
# purpose: a stocky character's torso is legitimately taller than a lanky
# one's, and the flag exists to catch a head the size of a torso, not taste.
SCALE_TOLERANCE = 0.35

# Consecutive provider/audit failures that end a batch. One failure is a
# re-roll; two in a row is the provider or the prompt, and re-rolling that is
# how a $0.05 frame becomes a $10 item.
MAX_CONSECUTIVE_FAILURES = 2

# The default per-kit ceiling on paid calls: every generated part of the
# largest template plus a retry each.
DEFAULT_MAX_PAID_CALLS = 20


class KitError(ValueError):
    """A kit request that must not spend money as asked."""


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def plan(template: str = "biped_v1", parts: Optional[list[str]] = None,
         include_equipment: bool = False) -> list[dict]:
    """Which slots to generate, in template order, with their specs.

    ``parts`` narrows to a subset (a retry, "just the head again"); a name the
    template does not know is refused rather than skipped, because a skipped
    part is a kit that assembles with a hole in it and nobody said why.
    """
    spec = cutout.template(template)
    specs = spec.get("parts") or {}
    reuse = spec.get("reuse") or {}
    wanted = [s["name"] for s in spec["slots"] if s["name"] not in reuse]
    if not include_equipment:
        wanted = [w for w in wanted if not (specs.get(w) or {}).get("equipment")]
    if parts:
        unknown = [p for p in parts if p not in {s["name"] for s in spec["slots"]}]
        if unknown:
            raise KitError(f"{template} has no slot(s) {unknown}; slots are "
                           f"{sorted(s['name'] for s in spec['slots'])}")
        reused = [p for p in parts if p in reuse]
        if reused:
            raise KitError(
                f"{reused} are reused from their near side and are not generated; "
                f"regenerate {sorted({reuse[p] for p in reused})} instead")
        wanted = [w for w in wanted if w in parts] + [
            p for p in parts if p not in wanted]
    out = []
    for slot in wanted:
        entry = dict(specs.get(slot) or {"what": slot.replace("_", " "),
                                         "height": 0.15})
        entry["slot"] = slot
        out.append(entry)
    return out


def estimate(template: str = "biped_v1", parts: Optional[list[str]] = None,
             quality: str = "medium") -> dict:
    """What a kit will cost before it buys anything."""
    from ..art.items import estimate_cost

    todo = plan(template, parts)
    return {"parts": [p["slot"] for p in todo], "calls": len(todo),
            "cost_usd": estimate_cost(len(todo), quality)}


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def prompt_for(part: dict, *, view: str = "side", profile: Optional[dict] = None,
               note: str = "") -> str:
    """One part, isolated, in the reference's style.

    "Nothing else in frame" is load-bearing: asked for a forearm, models paint
    the whole character and the caller crops a forearm out of a figure drawn at
    a different scale than the other nine parts. The part is the subject, the
    reference is the identity, and the character's own traits ride along from
    its visual profile so a green gnome's forearm is green.
    """
    traits = (profile or {}).get("traits") or ""
    style = (profile or {}).get("style") or ""
    negative = (profile or {}).get("negative") or ""
    lines = [
        f"ONE ISOLATED BODY PART for a 2D cutout puppet rig: the {part['what']} "
        "of the character in the reference image, and nothing else in frame.",
        f"Strict {view} view, exactly the projection of the reference; the same "
        "character, the same costume, the same colours and line weight as the "
        "reference.",
        "No other body parts, no whole figure, no background props, no shadow, "
        "no text. The part is cut clean at its joint seams with flat colour "
        "under the seam so it can overlap its neighbour.",
        "Centred, filling most of the frame.",
    ]
    if traits:
        lines.append(f"Character: {traits}.")
    if style:
        lines.append(f"Style: {style}.")
    if negative:
        lines.append(f"Never: {negative}.")
    if note:
        lines.append(note.strip())
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Measuring what came back
# ---------------------------------------------------------------------------

def file_hash(path: str | os.PathLike[str]) -> str:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else ""


def alpha_bbox(path: str | os.PathLike[str]) -> Optional[tuple[int, int, int, int]]:
    """The opaque extent of an image, or None for an empty one."""
    from PIL import Image

    with Image.open(path) as img:
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A").point(lambda a: 255 if a > 24 else 0)
        return alpha.getbbox()


def trim_alpha(path: str | os.PathLike[str], margin: int = 1) -> tuple[int, int]:
    """Crop a part to its alpha bounding box in place. Returns the new size.

    The pivot table is in FRACTIONS of the part's bbox, so a part shipped with
    a 1024 px transparent canvas around a 200 px forearm would hang from the
    middle of nowhere. Trim first; then the fraction means what it says.
    """
    from PIL import Image

    box = alpha_bbox(path)
    with Image.open(path) as img:
        rgba = img.convert("RGBA")
        if not box:
            return rgba.size
        x0, y0, x1, y1 = box
        x0, y0 = max(0, x0 - margin), max(0, y0 - margin)
        x1, y1 = min(rgba.width, x1 + margin), min(rgba.height, y1 + margin)
        cut = rgba.crop((x0, y0, x1, y1))
        cut.save(path)
        return cut.size


def reference_height(path: str | os.PathLike[str]) -> int:
    """The figure's height in the reference, in pixels: its alpha bbox for a
    keyed reference, the image height for an opaque one."""
    from PIL import Image

    box = alpha_bbox(path)
    with Image.open(path) as img:
        if box and (box[3] - box[1]) < img.height:
            return box[3] - box[1]
        return img.height


def scale_flag(part: dict, size: tuple[int, int], ref_height: int,
               *, tolerance: float = SCALE_TOLERANCE) -> Optional[dict]:
    """None when the part's height sits inside the band its template
    expects; otherwise what was expected, what came back and the ratio.

    A flag, not a fix. Rescaling silently would hide the one thing the human
    has to look at; the flag rides on the result and in the document notes.
    """
    expected = float(part.get("height") or 0.0) * float(ref_height)
    if expected <= 0 or not size or size[1] <= 0:
        return None
    ratio = size[1] / expected
    if abs(ratio - 1.0) <= tolerance:
        return None
    return {"slot": part["slot"], "expected_px": round(expected),
            "got_px": int(size[1]), "ratio": round(ratio, 2),
            "note": (f"{part['slot']} came back {ratio:.2f}x its template "
                     "height against the reference figure - check it before "
                     "wiring; cutout_part_rerun regenerates one part")}


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------

def generate_kit(root: str | os.PathLike[str], name: str, reference_path: str,
                 *, out_dir: str | os.PathLike[str], provider: str,
                 template: str = "biped_v1", parts: Optional[list[str]] = None,
                 quality: str = "medium", note: str = "",
                 profile: Optional[dict] = None,
                 max_paid_calls: int = DEFAULT_MAX_PAID_CALLS,
                 work_item_id: Optional[int] = None,
                 generate: Optional[Callable] = None,
                 mode: str = "sheet") -> dict:
    """Generate every planned part against one reference. Never raises for a
    provider failure; refuses BEFORE spending for a plan it must not buy.

    ``mode="sheet"`` (the default) buys ONE image: the whole kit drawn beside
    the figure on a layout we draw, then cropped back out (see the sheet
    section). ``mode="parts"`` is the old one-call-per-part loop, kept for a
    provider that cannot follow a layout.

    Returns ``{ok, parts: {slot: skin entry}, failed: [...], flags: [...],
    calls, cost_usd, reference_hash, stopped}``. ``ok`` is True only when
    every planned part landed and none is flagged.
    """
    todo = plan(template, parts)
    if not todo:
        raise KitError("nothing to generate - every requested slot is reused")
    mode = str(mode or "sheet").strip().lower()
    if mode not in ("sheet", "parts"):
        raise KitError(f"mode is 'sheet' or 'parts', not {mode!r}")
    if mode == "parts" and len(todo) > int(max_paid_calls):
        raise KitError(
            f"this kit is {len(todo)} paid generations against a ceiling of "
            f"{max_paid_calls} (max_paid_calls). Raise the ceiling on purpose, "
            "or generate a subset with parts=[...]")
    ref = Path(reference_path)
    if not ref.is_file():
        raise KitError(f"reference {reference_path} is not on disk")
    if generate is None:
        from ..art import chroma
        generate = chroma.generate

    spec = cutout.template(template)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ref_hash = file_hash(ref)
    ref_h = reference_height(ref)

    if mode == "sheet":
        got = generate_sheet(root, name, str(ref), todo, out_dir=out,
                             provider=provider, spec=spec, quality=quality,
                             note=note, profile=profile,
                             work_item_id=work_item_id, generate=generate)
        return {
            "ok": not got["failed"] and not got["flags"] and not got["stopped"],
            "parts": got["made"], "failed": got["failed"], "flags": got["flags"],
            "calls": got["calls"], "cost_usd": round(got["cost"], 4),
            "reference_hash": ref_hash, "reference_height_px": ref_h,
            "stopped": got["stopped"], "sheet": got["sheet"], "mode": "sheet",
        }

    made: dict[str, dict] = {}
    failed: list[dict] = []
    flags: list[dict] = []
    cost = 0.0
    calls = 0
    streak = 0
    stopped = ""
    for part in todo:
        slot = part["slot"]
        target = out / f"{slot}.png"
        prompt = prompt_for(part, view=spec.get("view", "side"),
                            profile=profile, note=note)
        calls += 1
        result = generate(prompt, str(target), provider=provider,
                          task_kind="part", quality=quality,
                          ref_paths=[str(ref)], root=root,
                          logical_name=f"{name}.part.{slot}",
                          work_item_id=work_item_id) or {}
        cost += float(result.get("cost_usd") or 0.0)
        if not result.get("ok") or not target.is_file():
            streak += 1
            failed.append({"slot": slot,
                           "error": str(result.get("error") or "no file written")})
            if streak >= MAX_CONSECUTIVE_FAILURES:
                stopped = (f"{streak} consecutive failures - the provider or the "
                           "prompt is the problem, not the seed; stopping rather "
                           "than re-rolling")
                break
            continue
        streak = 0
        size = trim_alpha(target)
        flag = scale_flag(part, size, ref_h)
        if flag:
            flags.append(flag)
        made[slot] = {
            "texture": str(target),
            "part_hash": cutout.part_hash(target),
            "anchor_hash": ref_hash,
            "prompt": prompt,
            "pivot_source": "default",
        }
    return {
        "ok": not failed and not flags and not stopped,
        "parts": made, "failed": failed, "flags": flags,
        "calls": calls, "cost_usd": round(cost, 4),
        "reference_hash": ref_hash, "reference_height_px": ref_h,
        "stopped": stopped, "mode": "parts",
    }


# ---------------------------------------------------------------------------
# The parts SHEET: one generation, every part, one style, one scale
# ---------------------------------------------------------------------------
# MEASURED (exit-67-r2 #8, 2026-09-22): nine parts bought one at a time from
# nine "ONE ISOLATED BODY PART" prompts came back in three different styles
# (painted, pixel, inked), at three different scales, and half were the wrong
# thing - a "hip" that was both legs and a boot, a "thigh" that was a pair of
# jeans, a "foot" the size of the torso, an "upper arm" that was a hand.
# Nothing in a per-part prompt can hold style or scale across calls, because
# the model never sees the other eight. The human's verdict: "art generation
# for the 2D rigging is impossible".
#
# A SHEET IS ONE CALL. The layout image below is drawn by us: the full figure
# in a big left cell, and one outlined, labelled cell per part. The model is
# asked to redraw the layout with each cell filled by the named part of that
# same figure; style and scale hold because every part is painted in one
# image beside the figure it is cut from. We key the flat backdrop ourselves
# and crop each cell back out by its known rectangle. Nine paid calls become
# one, and the result is judged as a whole.

SHEET_SIZE = (1536, 1024)
SHEET_MARGIN = 16
SHEET_LABEL = 26
SHEET_FIGURE_W = 400
SHEET_LAYOUT = "_sheet_layout"


def sheet_layout(parts: list[dict], reference_path: str | os.PathLike[str],
                 out_dir: str | os.PathLike[str],
                 chroma_rgb: tuple[int, int, int], suffix: str = "") -> dict:
    """Draw the layout the model redraws. Returns ``{png, json, cells,
    figure_height_px, size}`` and writes both files into ``out_dir``.

    ``cells`` maps slot -> (x0, y0, x1, y1) in layout pixels, the rectangle
    BELOW the label strip, which is all that is cropped back out.
    """
    from PIL import Image, ImageDraw

    W, H = SHEET_SIZE
    M, L = SHEET_MARGIN, SHEET_LABEL
    canvas = Image.new("RGBA", (W, H), (*chroma_rgb, 255))
    draw = ImageDraw.Draw(canvas)
    ink = (40, 40, 40, 255)

    # The figure, plated onto the key colour so a keyed reference and an
    # opaque one land the same way, scaled to the cell.
    fig_box = (M, M + L, M + SHEET_FIGURE_W, H - M)
    fw, fh = fig_box[2] - fig_box[0], fig_box[3] - fig_box[1]
    with Image.open(reference_path) as src:
        img = src.convert("RGBA")
        box = img.getbbox() or (0, 0, img.width, img.height)
        img = img.crop(box)
        scale = min((fw - 20) / max(1, img.width), (fh - 20) / max(1, img.height))
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))), Image.LANCZOS)
    fx = fig_box[0] + (fw - img.width) // 2
    fy = fig_box[3] - 10 - img.height
    canvas.alpha_composite(img, (fx, fy))
    figure_h = img.height
    # THE FIGURE CELL IS TIGHT. Left tall and half empty, the model used the
    # space above the figure for a part (a head landed there, and the HEAD
    # cell came back empty). The box hugs the figure; the label sits on it.
    fig_box = (fig_box[0], fy - L - 6, fig_box[2], fig_box[3])
    draw.rectangle(fig_box, outline=ink, width=2)
    draw.text((fig_box[0] + 4, fig_box[1] + 4), "FULL FIGURE (do not change)", fill=ink)

    # CELLS ARE SIZED TO THEIR PART. Equal tall cells invited the model to
    # fill them: the ARM_NEAR cell came back with the whole arm in it and
    # the rig got an 83 px upper arm on a 200 px figure. Each cell is the
    # part's template height (against the figure drawn on this sheet) with
    # a third of slack, no more, packed left to right in rows.
    x_start = fig_box[2] + M
    area_w = W - M - x_start
    cells: dict[str, tuple[int, int, int, int]] = {}
    x, y_top, row_h = x_start, M, 0
    for part in parts:
        ph = int(float(part.get("height") or 0.15) * figure_h * 1.35) + 8
        ph = max(60, min(ph, H - 2 * M - L))
        pw = max(int(ph * 0.75), int(figure_h * 0.16), 90)
        if x + pw > x_start + area_w:               # next row
            x, y_top = x_start, y_top + row_h + M
            row_h = 0
        y0 = y_top + L
        rect = (x, y0, x + pw, y0 + ph)
        cells[part["slot"]] = rect
        draw.rectangle(rect, outline=ink, width=2)
        draw.text((x + 4, y_top + 4), part["slot"].upper(), fill=ink)
        x += pw + M
        row_h = max(row_h, ph + L)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{SHEET_LAYOUT}{suffix}.png"
    canvas.save(png)
    meta = {"size": [W, H], "cells": {k: list(v) for k, v in cells.items()},
            "figure_height_px": figure_h, "chroma": list(chroma_rgb)}
    js = out / f"{SHEET_LAYOUT}{suffix}.json"
    js.write_text(__import__("json").dumps(meta, indent=1), encoding="utf-8")
    return {"png": str(png), "json": str(js), "cells": cells,
            "figure_height_px": figure_h, "size": (W, H)}


def sheet_prompt(parts: list[dict], *, view: str = "side",
                 profile: Optional[dict] = None, note: str = "") -> str:
    """The whole kit in one prompt: redraw the layout, fill each cell."""
    traits = (profile or {}).get("traits") or ""
    style = (profile or {}).get("style") or ""
    negative = (profile or {}).get("negative") or ""
    cells = "; ".join(f"{p['slot'].upper()}: {p.get('what') or p['slot']}"
                      for p in parts)
    lines = [
        "A 2D CUTOUT PUPPET PARTS SHEET, an exact redraw of the layout image "
        "(the second reference). Keep the full character in the large left "
        "cell exactly as it is. In each outlined, labelled cell draw ONLY the "
        "body part named above it, cut from that same character: the same "
        "style, colours, line weight and SCALE as the figure on the left, "
        f"strict {view} view, the same projection as the figure.",
        "EVERY PART ENDS IN A ROUNDED JOINT CAP: at each seam the part "
        "continues past the joint as a round, fully painted knob (shoulder "
        "ball, elbow, hip, knee, ankle) so that when the puppet bends the cap "
        "tucks under its neighbour and no gap opens. Never a flat cut, never "
        "a hard edge at a joint. The TORSO keeps its full shoulder mass and "
        "the collar; the HIP keeps the belt and the top of both thighs. One "
        "part per cell, nothing else in the cell: no whole figures, no extra "
        "parts, no props, no shadows, no text besides the existing labels.",
        f"Cells: {cells}.",
        "Keep every cell where it is and leave the space around the parts "
        "flat backdrop colour.",
    ]
    if traits:
        lines.append(f"Character: {traits}.")
    if style:
        lines.append(f"Style: {style}.")
    if negative:
        lines.append(f"Never: {negative}.")
    if note:
        lines.append(note.strip())
    return " ".join(lines)


def slice_sheet(sheet_path: str | os.PathLike[str], layout: dict,
                out_dir: str | os.PathLike[str],
                chroma_rgb: tuple[int, int, int],
                rig_height_px: int = 0) -> dict:
    """Key the sheet and crop every cell back out to ``<slot>.png``.

    ``rig_height_px`` is the template's figure height: every part is resized
    by rig_height / (the figure's height on the sheet) so it lands at the
    scale the rig is authored in. MEASURED (exit-67-r2 #8): a 2528 px sheet
    gave a 400 px torso for a 200 px rig and the agent wrote its own
    downscaler. 0 leaves the sheet's pixels alone.

    Returns ``{parts: {slot: path}, empty: [slots], scale: (sx, sy),
    part_scale}``. A cell with nothing but backdrop in it is EMPTY, not a
    part: it is reported so the caller can re-run that slot, never written
    as a blank texture.
    """
    from PIL import Image
    from ..art import chroma as _chroma

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lw, lh = layout["size"]
    with Image.open(sheet_path) as src:
        sheet = src.convert("RGBA")
        _chroma.key(sheet, chroma_rgb)
        sx, sy = sheet.width / lw, sheet.height / lh
        fig_h = float(layout.get("figure_height_px") or 0) * sy
        part_scale = (float(rig_height_px) / fig_h
                      if rig_height_px and fig_h > 0 else 1.0)
        parts: dict[str, str] = {}
        empty: list[str] = []
        for slot, (x0, y0, x1, y1) in layout["cells"].items():
            # The model redraws the cell borders, thicker and a little off;
            # a 4 px inset let them ride into every part (an arm 162 px tall
            # that was a 30 px arm plus two border lines). Inset by 3% of the
            # cell, then keep the largest blob and anything near its size.
            cw, ch = (x1 - x0) * sx, (y1 - y0) * sy
            ix, iy = max(8, int(cw * 0.03)), max(8, int(ch * 0.03))
            rect = (int(x0 * sx) + ix, int(y0 * sy) + iy,
                    int(x1 * sx) - ix, int(y1 * sy) - iy)
            cell = _main_blobs(sheet.crop(rect))
            box = cell.getbbox()
            if not box or (box[2] - box[0]) < 4 or (box[3] - box[1]) < 4:
                empty.append(slot)
                continue
            target = out / f"{slot}.png"
            part = cell.crop(box)
            if abs(part_scale - 1.0) > 0.01:
                part = part.resize((max(1, round(part.width * part_scale)),
                                    max(1, round(part.height * part_scale))),
                                   Image.LANCZOS)
            part.save(target)
            parts[slot] = str(target)
    return {"parts": parts, "empty": empty, "scale": (sx, sy),
            "part_scale": round(part_scale, 4)}


def _main_blobs(cell, keep: float = 0.08):
    """The cell with only its main connected blob(s) left opaque.

    Anything smaller than ``keep`` of the largest blob is erased: a border
    fragment, a stray dot, a label the model copied. A part is one blob, or
    a few near-equal ones (a hand with a separate thumb).
    """
    from PIL import Image
    w, h = cell.size
    alpha = cell.getchannel("A").load()
    seen = bytearray(w * h)
    blobs: list[tuple[int, list[int]]] = []
    for start in range(w * h):
        if seen[start] or alpha[start % w, start // w] == 0:
            continue
        stack = [start]
        seen[start] = 1
        px: list[int] = []
        while stack:
            i = stack.pop()
            px.append(i)
            x, y = i % w, i // w
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    j = ny * w + nx
                    if not seen[j] and alpha[nx, ny] != 0:
                        seen[j] = 1
                        stack.append(j)
        blobs.append((len(px), px))
    if not blobs:
        return cell
    biggest = max(n for n, _ in blobs)
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for n, px in blobs:
        if n >= biggest * keep:
            for i in px:
                mp[i % w, i // w] = 255
    out = cell.copy()
    out.putalpha(Image.composite(cell.getchannel("A"), Image.new("L", (w, h), 0), mask))
    return out


def generate_sheet(root, name: str, reference_path: str, todo: list[dict],
                   *, out_dir, provider: str, spec: dict, quality: str,
                   note: str, profile: Optional[dict], work_item_id,
                   generate: Callable) -> dict:
    """One paid call for the whole kit. Same return shape as the per-part
    loop's inner bookkeeping: ``(made, failed, flags, calls, cost, stopped)``.
    """
    from ..art import chroma as _chroma

    ref = Path(reference_path)
    chroma_name, chroma_rgb = _chroma.pick(str(ref))
    # A subset (cutout_part_rerun) gets its own sheet files; the full kit's
    # sheet and layout stay on disk for the human to judge the kit whole.
    full = {p["slot"] for p in plan(spec.get("name") or "biped_v1")} <= {p["slot"] for p in todo}
    suffix = "" if full else "_" + "-".join(p["slot"] for p in todo)[:48]
    layout = sheet_layout(todo, ref, out_dir, chroma_rgb, suffix=suffix)
    prompt = sheet_prompt(todo, view=spec.get("view", "side"),
                          profile=profile, note=note)
    prompt = prompt + _chroma.clause((chroma_name, chroma_rgb))
    sheet_png = Path(out_dir) / f"_sheet{suffix}.png"
    # keyed=False: WE key it, against the colour WE drew the layout in. The
    # keyable path would pick its own colour from the layout's palette (which
    # is mostly our backdrop) and land on a different one.
    result = generate(prompt, str(sheet_png), provider=provider,
                      task_kind="sheet", keyed=False, quality=quality,
                      size=f"{SHEET_SIZE[0]}x{SHEET_SIZE[1]}",
                      ref_paths=[str(ref), layout["png"]], root=root,
                      logical_name=f"{name}.parts_sheet",
                      work_item_id=work_item_id) or {}
    cost = float(result.get("cost_usd") or 0.0)
    if not result.get("ok") or not sheet_png.is_file():
        err = str(result.get("error") or "no sheet written")
        return {"made": {}, "failed": [{"slot": p["slot"], "error": err} for p in todo],
                "flags": [], "calls": 1, "cost": cost,
                "stopped": f"the sheet did not come back: {err}",
                "sheet": str(sheet_png), "prompt": prompt}
    rig_h = int(spec.get("height_px") or 0)
    cut = slice_sheet(sheet_png, layout, out_dir, chroma_rgb, rig_height_px=rig_h)
    ref_hash = file_hash(ref)
    # Parts are now at rig scale, so the ruler is the rig's figure height.
    fig_h = rig_h or layout["figure_height_px"] * cut["scale"][1]
    made: dict[str, dict] = {}
    flags: list[dict] = []
    for part in todo:
        slot = part["slot"]
        path = cut["parts"].get(slot)
        if not path:
            continue
        size = trim_alpha(path)
        flag = scale_flag(part, size, int(fig_h))
        if flag:
            flags.append(flag)
        made[slot] = {"texture": path, "part_hash": cutout.part_hash(path),
                      "anchor_hash": ref_hash, "prompt": prompt,
                      "pivot_source": "default", "sheet": str(sheet_png)}
    failed = [{"slot": s, "error": "the cell came back empty"} for s in cut["empty"]]
    return {"made": made, "failed": failed, "flags": flags, "calls": 1,
            "cost": cost, "stopped": "", "sheet": str(sheet_png), "prompt": prompt}


def fill_reuse(skin: dict, template: str = "biped_v1") -> dict:
    """Far-side slots take the near side's drawing, tinted back, unless the
    caller filled them. Inherits the provenance too."""
    spec = cutout.template(template)
    out = dict(skin)
    for far, near in (spec.get("reuse") or {}).items():
        if far not in out and near in out:
            entry = dict(out[near])
            entry["reuse_of"] = near
            entry["far_tint"] = spec.get("far_tint")
            out[far] = entry
    return out
