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
                 generate: Optional[Callable] = None) -> dict:
    """Generate every planned part against one reference. Never raises for a
    provider failure; refuses BEFORE spending for a plan it must not buy.

    Returns ``{ok, parts: {slot: skin entry}, failed: [...], flags: [...],
    calls, cost_usd, reference_hash, stopped}``. ``ok`` is True only when
    every planned part landed and none is flagged.
    """
    todo = plan(template, parts)
    if not todo:
        raise KitError("nothing to generate - every requested slot is reused")
    if len(todo) > int(max_paid_calls):
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
        "stopped": stopped,
    }


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
