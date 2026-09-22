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
                 mode: str = "sheet", description: str = "") -> dict:
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
        description = (description or (profile or {}).get("traits") or "").strip()
        if not description:
            raise KitError(
                "a sheet kit paints the character from a WRITTEN description "
                "(the reference image is never sent to the model - it pasted "
                "the reference into the pieces). Pass description='...' - "
                "look at the reference and write its outfit, colours, "
                "materials and art style - or set the pin's traits with "
                "profile_set")
        got = generate_sheet(root, name, str(ref), todo, out_dir=out,
                             provider=provider, spec=spec, quality=quality,
                             note=note, profile=profile,
                             work_item_id=work_item_id, generate=generate,
                             description=description)
        # EVERY FLAGGED PIECE IS REPAINTED ALONE, once. MEASURED (exit-67-r2,
        # 2026-09-22): on one nine-silhouette sheet the model painted the arm,
        # forearm and boot exactly and left the head, hip, thigh and shin
        # grey (it drew a whole bust in the torso cell). One silhouette per
        # image is the task it does reliably; the checks decide which.
        if len(todo) > 1 and not got["stopped"]:
            bad = [p for p in todo
                   if p["slot"] not in got["made"]
                   or any(f.get("slot") == p["slot"] for f in got["flags"])]
            for part in bad:
                if got["calls"] >= int(max_paid_calls):
                    break
                one = generate_sheet(root, name, str(ref), [part], out_dir=out,
                                     provider=provider, spec=spec,
                                     quality=quality, note=note,
                                     profile=profile, work_item_id=work_item_id,
                                     generate=generate, description=description)
                got["calls"] += one["calls"]
                got["cost"] += one["cost"]
                slot = part["slot"]
                mine = [f for f in one["flags"] if f.get("slot") == slot]
                old = [f for f in got["flags"] if f.get("slot") == slot]
                if slot in one["made"] and (slot not in got["made"] or len(mine) < len(old)
                                            or not mine):
                    got["made"][slot] = one["made"][slot]
                    got["flags"] = [f for f in got["flags"] if f.get("slot") != slot] + mine
                    got["failed"] = [f for f in got["failed"] if f.get("slot") != slot]
        # THE TORSO LOSES ITS ARM: one edit of our own piece (unarm_torso).
        if "torso" in got["made"] and got["calls"] >= int(max_paid_calls):
            got["flags"].append({"slot": "torso", "note": "no paid call left "
                                 "for the edit that takes the arm off the "
                                 "torso (max_paid_calls) - it may carry a "
                                 "stub sleeve; a torso costs 2 calls"})
        elif "torso" in got["made"]:
            from ..art import chroma as _chroma
            _, crgb = _chroma.pick(str(ref))
            un = unarm_torso(root, name, got["made"]["torso"]["texture"],
                             chroma_rgb=crgb, provider=provider,
                             quality=quality, work_item_id=work_item_id,
                             generate=generate)
            got["calls"] += 1
            got["cost"] += un["cost"]
            if un["ok"]:
                got["made"]["torso"]["part_hash"] = cutout.part_hash(
                    got["made"]["torso"]["texture"])
            else:
                got["flags"].append({"slot": "torso", "note": "the arm could "
                                     "not be taken off the torso (" + un["error"]
                                     + "); it may show a stub sleeve when the "
                                     "arm swings - cutout_part_rerun torso"})
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
# A SHEET IS ONE CALL. The layout image below is drawn by us: one labelled
# cell per piece holding that piece's SILHOUETTE (cutoutshape) in flat grey,
# every one at the same scale, and nothing else. The pinned reference is a
# separate image, the character's look. The model paints the character into
# the silhouettes; the reference itself is never on the sheet or cut.
#
# THE SILHOUETTE IS THE CONTRACT (2026-09-22). Cells that only named a part
# got back pieces of any size that did not meet at their joints - "the rig
# is not even connected". Now the harness clips the paint to the silhouette,
# so every piece has the template's size, the template's pivot, and a joint
# disc that its neighbour's disc turns over. The reference is never cut.

SHEET_SIZE = (1536, 1024)
SHEET_MARGIN = 16
SHEET_LABEL = 26
SHEET_FIGURE_W = 400
SHEET_LAYOUT = "_sheet_layout"
#: Sheet pixels per template pixel for the silhouettes: at most this, less
#: when the cells are too small for it.
SHEET_SCALE = 5.0
SHEET_SCALE_MAX = 10.0
SILHOUETTE_GREY = (150, 150, 150)
#: Template px the clip grows the silhouette by, so the painted outline on
#: the silhouette's edge survives the clip.
CLIP_GROW = 1.2


def sheet_layout(parts: list[dict], reference_path: str | os.PathLike[str],
                 out_dir: str | os.PathLike[str],
                 chroma_rgb: tuple[int, int, int], suffix: str = "") -> dict:
    """Draw the layout the model paints. Returns ``{png, json, cells,
    shapes, size, scale}`` and writes both files into ``out_dir``.

    ``shapes`` maps slot -> (x0, y0, x1, y1): where that slot's silhouette
    bbox sits on the layout, in layout pixels.
    """
    from PIL import Image, ImageDraw
    from . import cutoutshape as _shape

    W, H = SHEET_SIZE
    M, L = SHEET_MARGIN, SHEET_LABEL
    canvas = Image.new("RGBA", (W, H), (*chroma_rgb, 255))
    draw = ImageDraw.Draw(canvas)
    ink = (40, 40, 40, 255)

    # NO FIGURE ON THE LAYOUT. The reference goes to the model as its own
    # image, for the look only. Plated onto the sheet, the model copied it:
    # the torso cell came back as the reference's head and bust (2026-09-22).
    shaped = [p for p in parts if _shape.has_shape(p["slot"])]
    n = max(1, len(shaped))
    rows = 1 if n <= 4 else 2
    cols = -(-n // rows)
    x_start = M
    area_w = W - M - x_start
    cell_w = (area_w - (cols - 1) * M) // cols
    cell_h = (H - (rows + 1) * M - rows * L) // rows
    cells: dict[str, tuple[int, int, int, int]] = {}
    shapes: dict[str, tuple[int, int, int, int]] = {}
    # One scale for the whole sheet, as big as the tightest cell allows: a
    # lone silhouette on a rerun sheet is painted large, not as a speck.
    scale_px = SHEET_SCALE_MAX
    for part in shaped:
        bx0, by0, bx1, by1 = _shape.bbox(part["slot"])
        scale_px = min(scale_px, 0.8 * cell_w / (bx1 - bx0),
                       0.8 * cell_h / (by1 - by0))
    for i, part in enumerate(shaped):
        slot = part["slot"]
        r, c = divmod(i, cols)
        x0 = x_start + c * (cell_w + M)
        y_top = M + r * (cell_h + L + M)
        y0 = y_top + L
        cells[slot] = (x0, y0, x0 + cell_w, y0 + cell_h)
        draw.text((x0 + 4, y_top + 6), slot.replace("_near", "").upper(), fill=ink)
        m = _shape.mask(slot, scale_px)
        sx = x0 + (cell_w - m.width) // 2
        sy = y0 + (cell_h - m.height) // 2
        grey = Image.new("RGBA", m.size, (*SILHOUETTE_GREY, 255))
        canvas.paste(grey, (sx, sy), m)
        shapes[slot] = (sx, sy, sx + m.width, sy + m.height)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{SHEET_LAYOUT}{suffix}.png"
    canvas.save(png)
    meta = {"size": [W, H], "cells": {k: list(v) for k, v in cells.items()},
            "shapes": {k: list(v) for k, v in shapes.items()},
            "scale": scale_px, "chroma": list(chroma_rgb)}
    js = out / f"{SHEET_LAYOUT}{suffix}.json"
    js.write_text(__import__("json").dumps(meta, indent=1), encoding="utf-8")
    return {"png": str(png), "json": str(js), "cells": cells, "shapes": shapes,
            "size": (W, H), "scale": scale_px}


def sheet_prompt(parts: list[dict], *, view: str = "side",
                 profile: Optional[dict] = None, note: str = "",
                 description: str = "") -> str:
    """Paint the character, as DESCRIBED, into the silhouettes.

    USER DIRECTIVE (2026-09-22): the reference is a reference, never cut.
    Shown the reference image, the image model pasted the reference's own
    head and bust into the torso silhouette. So the call carries no image
    of the character at all - only this description of it - and the model
    has nothing to copy.
    """
    from . import cutoutshape as _shape

    traits = (profile or {}).get("traits") or ""
    style = (profile or {}).get("style") or ""
    negative = (profile or {}).get("negative") or ""
    cells = "; ".join(
        f"{p['slot'].replace('_near', '').upper()}: {_shape.WHAT.get(_shape.key(p['slot']), p['slot'])}"
        for p in parts if _shape.has_shape(p["slot"]))
    lines = [
        "A 2D CUTOUT PUPPET PAINTING SHEET. Repaint this layout image "
        "keeping every cell, label and grey shape exactly where it is. "
        f"The character: {description.strip().rstrip('.')}. Never draw the "
        "whole character anywhere; only its parts, each inside its grey "
        "silhouette.",
        "Each labelled cell holds one flat grey SILHOUETTE. Paint that body "
        "part of the character INTO the silhouette, filling it completely "
        "and exactly, edge to edge, with a clean dark outline on the "
        "silhouette's own edge: nothing outside the grey shape, no grey left "
        f"inside it. Strict {view} view, facing right.",
        "The round ends of a shape are JOINTS: paint them as the same cloth "
        "or skin continuing round the end, so when the puppet bends the ends "
        "tuck under each other - never a ball, knob, socket or flat cut.",
        f"Silhouettes: {cells}.",
        "Flat backdrop colour everywhere outside the silhouettes. No extra "
        "parts, no whole figures, no props, no shadows, no new text.",
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


#: A provider TIMEOUT is retried this many times. MEASURED (kie, 2026-09-22):
#: about half the calls of an evening came back 524 "generate task timeout"
#: and the same request went through on the next try.
TIMEOUT_RETRIES = 2


def _call(generate: Callable, *args, **kw) -> dict:
    """generate(), retried on a provider timeout only - never on a refusal
    or a bad request, which would fail the same way again."""
    res: dict = {}
    for _ in range(TIMEOUT_RETRIES + 1):
        res = generate(*args, **kw) or {}
        err = str(res.get("error") or "").lower()
        if res.get("ok") or not ("timeout" in err or "524" in err or "timed out" in err):
            return res
    return res


UNARM_PROMPT = (
    "Edit this image: it is the torso piece of a 2D cutout puppet, seen "
    "from the side. REMOVE the arm and the shirt sleeve completely - the "
    "upper arm, the forearm, the skin, everything of the arm. Where the arm "
    "was, paint the side of the vest and shirt continuing naturally - solid "
    "fabric, NO armhole, no sleeve opening, no oval, no patch - same "
    "colours, pattern, outline and style. Keep everything else exactly as "
    "it is: the same outline shape, anything worn on the back, pockets, "
    "collar, the size and position. Keep the flat backdrop colour.")


def unarm_torso(root, name: str, texture: str, *, chroma_rgb, provider: str,
                quality: str, work_item_id, generate: Callable) -> dict:
    """One EDIT of our own torso piece: take the arm off it.

    MEASURED (exit-67-r2, 2026-09-22): asked for a torso, the image model
    paints the near arm on it every time - four prompts, four arms - and a
    torso with an arm on it leaves a stub sleeve behind whenever the real
    arm piece swings. Removing a thing is what these models do reliably, so
    the torso is painted, then edited. The reference is not involved.

    Returns {ok, cost, error}; the texture is replaced only on success.
    """
    import numpy as np
    from PIL import Image
    from ..art import chroma as _chroma
    from . import cutoutshape as _shape

    piece = Image.open(texture).convert("RGBA")
    W, H = 1024, 1536
    k = min((W - 120) / piece.width, (H - 120) / piece.height)
    big = piece.resize((round(piece.width * k), round(piece.height * k)), Image.NEAREST)
    canvas = Image.new("RGBA", (W, H), (*chroma_rgb, 255))
    off = ((W - big.width) // 2, (H - big.height) // 2)
    canvas.alpha_composite(big, off)
    src = Path(texture).with_name("_unarm_in.png")
    canvas.convert("RGB").save(src)
    out = Path(texture).with_name("_unarm_out.png")
    res = _call(generate, UNARM_PROMPT, str(out), provider=provider, task_kind="sheet",
                   keyed=False, quality=quality, size=f"{W}x{H}",
                   ref_paths=[str(src)], root=root,
                   logical_name=f"{name}.torso_unarm",
                   work_item_id=work_item_id) or {}
    cost = float(res.get("cost_usd") or 0.0)
    if not res.get("ok") or not out.is_file():
        return {"ok": False, "cost": cost, "error": str(res.get("error") or "no edit")}
    edited = Image.open(out).convert("RGBA").resize((W, H), Image.LANCZOS)
    _chroma.key(edited, chroma_rgb)
    region = edited.crop((off[0], off[1], off[0] + big.width, off[1] + big.height))
    region = region.resize(piece.size, Image.LANCZOS)
    clip = np.asarray(_shape.mask("torso", _shape.TEX, grow=CLIP_GROW).resize(piece.size))
    a = np.minimum(np.asarray(region.getchannel("A")), clip)
    region.putalpha(Image.fromarray(a.astype("uint8"), "L"))
    region = defringe(clear_grey(defringe(region, chroma_rgb, erode=0)),
                      chroma_rgb, erode=1)
    region.save(texture)
    return {"ok": True, "cost": cost, "error": ""}


def clear_grey(piece, tol: int = 10):
    """Make the silhouette's own flat grey transparent, feathering its edge.

    Tight on purpose: at 30 it also ate the vest's buckle and the shirt's
    highlights and left holes all over the torso (2026-09-22). Unpainted
    silhouette is FLAT grey; painted grey is not."""
    import numpy as np
    from PIL import Image, ImageFilter
    arr = np.asarray(piece.convert("RGBA")).copy()
    d = np.abs(arr[:, :, :3].astype(int) - np.array(SILHOUETTE_GREY)).sum(axis=2)
    grey = d < tol
    near = (d < tol * 2) & ~grey
    arr[grey, 3] = 0
    arr[near, 3] = (arr[near, 3] * 0.5).astype("uint8")
    return Image.fromarray(arr, "RGBA")


def slice_sheet(sheet_path: str | os.PathLike[str], layout: dict,
                out_dir: str | os.PathLike[str],
                chroma_rgb: tuple[int, int, int],
                rig_height_px: int = 0) -> dict:
    """Key the painted sheet and clip every silhouette back out.

    Each piece is found where its silhouette was drawn - registered to the
    paint's own bounding box in the cell, because the model keeps the layout
    but not always its exact scale - resized to cutoutshape.TEX px per
    template px and clipped to the silhouette, grown by CLIP_GROW. The size
    and pivot therefore come from the template, never from the paint.

    Returns ``{parts: {slot: path}, empty, clipped, gaps, grey, scale}``:
    `empty` slots had nothing painted; `grey` slots were left mostly
    unpainted; `gaps` lists joint discs the paint does not cover.
    `rig_height_px` is accepted for the old call shape and ignored.
    """
    import numpy as np
    from PIL import Image
    from ..art import chroma as _chroma
    from . import cutoutshape as _shape

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lw, lh = layout["size"]
    unit = float(layout.get("scale") or SHEET_SCALE)
    with Image.open(sheet_path) as src:
        sheet = src.convert("RGBA")
    sx, sy = sheet.width / lw, sheet.height / lh
    _chroma.key(sheet, chroma_rgb)
    parts: dict[str, str] = {}
    empty, grey_left, gaps, clipped = [], [], [], []
    for slot, (x0, y0, x1, y1) in layout["cells"].items():
        cw, ch = (x1 - x0) * sx, (y1 - y0) * sy
        ix, iy = max(6, int(cw * 0.02)), max(6, int(ch * 0.02))
        rect = (int(x0 * sx) + ix, int(y0 * sy) + iy,
                int(x1 * sx) - ix, int(y1 * sy) - iy)
        cell = _main_blobs(sheet.crop(rect))
        box = cell.getbbox()
        if not box or (box[2] - box[0]) < 4 or (box[3] - box[1]) < 4:
            empty.append(slot)
            continue
        # Where the silhouette was drawn, in this cell crop's pixels.
        want = layout["shapes"][slot]
        ww, wh = (want[2] - want[0]) * sx, (want[3] - want[1]) * sy
        wx0, wy0 = want[0] * sx - rect[0], want[1] * sy - rect[1]
        drawn = (wx0, wy0, wx0 + ww, wy0 + wh)
        # USE IT unless the paint is clearly somewhere else. Registering
        # every piece to its paint's bbox stretched a piece whose paint
        # stopped short, and hid the very hole joint_gaps looks for.
        ix = max(0.0, min(drawn[2], box[2]) - max(drawn[0], box[0]))
        iy = max(0.0, min(drawn[3], box[3]) - max(drawn[1], box[1]))
        inter = ix * iy
        union = ww * wh + (box[2] - box[0]) * (box[3] - box[1]) - inter
        if union > 0 and inter / union >= 0.5:
            src_box = drawn
        else:
            # The model moved or rescaled the piece: map the silhouette onto
            # the paint, and say so.
            bw, bh = box[2] - box[0], box[3] - box[1]
            k = (bw / ww + bh / wh) / 2.0
            clipped.append(slot)
            cxp, cyp = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
            src_box = (cxp - ww * k / 2, cyp - wh * k / 2,
                       cxp + ww * k / 2, cyp + wh * k / 2)
        m = _shape.mask(slot, _shape.TEX)
        piece = cell.crop(tuple(int(round(v)) for v in src_box)).resize(
            m.size, Image.LANCZOS)
        clip = _shape.mask(slot, _shape.TEX, grow=CLIP_GROW)
        a = np.minimum(np.asarray(piece.getchannel("A")), np.asarray(clip))
        piece.putalpha(Image.fromarray(a.astype("uint8"), "L"))
        piece = defringe(piece, chroma_rgb, erode=0)
        rgb = np.asarray(piece.convert("RGB")).astype(int)
        opaque = np.asarray(piece.getchannel("A")) > 128
        g = SILHOUETTE_GREY
        greyish = (np.abs(rgb - np.array(g)).sum(axis=2) < 30) & opaque
        if opaque.any() and greyish.sum() / opaque.sum() > 0.25:
            grey_left.append(slot)
        # UNPAINTED SILHOUETTE IS NOT ART. Left in, it drew a grey ghost
        # round the torso and hips in every pose (2026-09-22). Cleared, and
        # the 1 px antialiased ring between paint and grey eroded (a pale
        # rim round every boot and leg); a joint it leaves bare is then
        # reported by joint_gaps.
        piece = defringe(clear_grey(piece), chroma_rgb, erode=1)
        target = out / f"{slot}.png"
        piece.save(target)
        parts[slot] = str(target)
        gaps += _shape.joint_gaps(slot, str(target))
    return {"parts": parts, "empty": empty, "clipped": clipped,
            "grey": grey_left, "gaps": gaps, "scale": (sx, sy),
            "part_scale": round(1.0 / _shape.TEX, 4)}


def defringe(part, chroma_rgb: tuple[int, int, int], erode: int = 1,
             band: int = 3):
    """Strip the key colour off a part's edge: despill, then erode alpha.

    MEASURED (exit-67-r2 player kit): chroma.key's distance despill leaves a
    cyan rim on every part - the model antialiases the part into the
    backdrop, and a pixel half part, half cyan is too far from pure cyan to
    key and too close to look like the part. Rigged, every joint shows it.

    Despill works on the key's DOMINANT channels (the ones the chroma has
    high: G and B for cyan, R and B for magenta): within `band` px of
    transparency, any excess of those over the others is removed. Only the
    rim is touched, so a part that is legitimately that colour keeps it
    inside. Then alpha is eroded `erode` px at the part's final size, which
    takes the last antialiased ring off rather than despilling it grey.
    """
    from PIL import Image, ImageChops, ImageFilter
    part = part.convert("RGBA")
    r, g, b, a = part.split()
    hard = a.point(lambda v: 255 if v >= 128 else 0)
    rim = ImageChops.subtract(
        hard, hard.filter(ImageFilter.MinFilter(2 * band + 1)))
    chans = {"r": r, "g": g, "b": b}
    hi = [k for k, v in zip("rgb", chroma_rgb) if v >= 128]
    lo = [k for k in "rgb" if k not in hi]
    if hi and lo:
        other = chans[lo[0]]
        for k in lo[1:]:
            other = ImageChops.lighter(other, chans[k])
        dom = chans[hi[0]]
        for k in hi[1:]:
            dom = ImageChops.darker(dom, chans[k])
        spill = ImageChops.subtract(dom, other)
        for k in hi:
            fixed = ImageChops.subtract(chans[k], spill)
            chans[k] = Image.composite(fixed, chans[k], rim)
    if erode:
        a = a.filter(ImageFilter.MinFilter(2 * erode + 1))
    return Image.merge("RGBA", (chans["r"], chans["g"], chans["b"], a))


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
                   generate: Callable, description: str = "") -> dict:
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
                          profile=profile, note=note, description=description)
    prompt = prompt + _chroma.clause((chroma_name, chroma_rgb))
    sheet_png = Path(out_dir) / f"_sheet{suffix}.png"
    # keyed=False: WE key it, against the colour WE drew the layout in. The
    # keyable path would pick its own colour from the layout's palette (which
    # is mostly our backdrop) and land on a different one.
    result = _call(generate, prompt, str(sheet_png), provider=provider,
                      task_kind="sheet", keyed=False, quality=quality,
                      size=f"{SHEET_SIZE[0]}x{SHEET_SIZE[1]}",
                      # The LAYOUT only: the reference is never sent (see
                      # sheet_prompt), so it cannot be pasted into a piece.
                      ref_paths=[layout["png"]], root=root,
                      logical_name=f"{name}.parts_sheet",
                      work_item_id=work_item_id) or {}
    cost = float(result.get("cost_usd") or 0.0)
    if not result.get("ok") or not sheet_png.is_file():
        err = str(result.get("error") or "no sheet written")
        return {"made": {}, "failed": [{"slot": p["slot"], "error": err} for p in todo],
                "flags": [], "calls": 1, "cost": cost,
                "stopped": f"the sheet did not come back: {err}",
                "sheet": str(sheet_png), "prompt": prompt}
    from . import cutoutshape as _shape
    cut = slice_sheet(sheet_png, layout, out_dir, chroma_rgb)
    ref_hash = file_hash(ref)
    made: dict[str, dict] = {}
    flags: list[dict] = []
    for slot in cut["clipped"]:
        flags.append({"slot": slot, "note": f"{slot} was painted away from its "
                      "silhouette (moved or rescaled); it was mapped back and "
                      "clipped - look at it; cutout_part_rerun redraws one slot"})
    for slot in cut["grey"]:
        flags.append({"slot": slot, "note": f"{slot} was left mostly grey - "
                      "the model did not paint the silhouette"})
    for gap in cut["gaps"]:
        flags.append(gap)
    for part in todo:
        slot = part["slot"]
        path = cut["parts"].get(slot)
        if not path:
            continue
        # The TEMPLATE's pivot and size: the piece is the silhouette.
        made[slot] = {"texture": path, "part_hash": cutout.part_hash(path),
                      "anchor_hash": ref_hash, "prompt": prompt,
                      "pivot": _shape.pivot(slot), "pivot_source": "default",
                      "scale": round(1.0 / _shape.TEX, 5), "fit": False,
                      "shape": _shape.key(slot), "sheet": str(sheet_png)}
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
