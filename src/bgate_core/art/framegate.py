"""The identity gate — fails a FRAME, not a score nobody reads.

MEASURED (EXIT 67, 2026-09-20/21, two art seats running at once): a gnome's
sheet came back with frames 1-6 belonging to a bearded biker; diesel dog frame
4 was the sniper; a flyer's poses were a gator wearing rotors. The upload
collision that caused it is fixed (commit de59d3a — kie now names uploads by
content hash), but every check that ran AFTER the collision — alpha audit,
stitch, ``sprite_sheet_check``, ``consistency_check`` — PASSED the wrong
character, because none of them ever compared a frame against THIS
character's own anchor. A vision judge (``consistency_check``) is a score;
when it sits out (no key, rate limit, a provider hiccup) nothing takes its
place. This module is the arithmetic that always runs: silhouette ratio,
palette distance, connected-component shape, near-duplicate detection — cheap
enough to run on every frame, every time, no model call, no key required.

Same build also shipped ghosts every one of those gates missed (ITEM 5): the
sniper's death_0-2 carried a translucent cyan second head and arm (the model
doubles the figure on any "falling" description — a known trigger, see
``FALL_PROMPT_RISK`` below); death_3 had the rifle standing upright with no
figure at all; relocate_0/1 had cyan smears the chroma key half-cut. And a
gas-pump turret prop grew arms and legs in fire_1 and sweep_1 (ITEM 6) because
nothing checked that a PROP's connected-component shape held still.

Everything here is arithmetic over an RGBA :class:`PIL.Image.Image` — alpha
threshold, connected components (flood fill), bounding boxes, palette
histograms. No model call, no network, no money. A verdict is a dict with
``ok`` and a ``checks`` list; each check that FIRES names the measured value
against its threshold, because "frame 4 failed" without a number is not
something the next agent can act on.

Provenance (ITEMS 2/3): every pose PNG and reference view ``image_sprites``
writes gets a JSON sidecar recording the ANCHOR HASH it was generated against.
Assembling a sheet refuses to stitch a frame whose sidecar disagrees with the
current anchor, or has none — the failure this closes is an agent told to
"regenerate only what is wrong" re-stitching a contaminated 05:xx frame next
to a clean 08:xx one, because nothing in the folder recorded which anchor
either was generated against.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image

# ---------------------------------------------------------------------------
# Thresholds — module constants, each with the measured reason.
# ---------------------------------------------------------------------------

#: Alpha at/above this is opaque for silhouette measurement. Matches
#: spritefit.ALPHA_HARD so a "silhouette" means the same pixels everywhere.
ALPHA_HARD = 128

#: A pixel this translucent or more transparent is background noise, not a
#: ghost — chroma-key spill and antialiasing both live under 20.
ALPHA_GHOST_LO = 20
#: A pixel this opaque or more is part of the main figure's own body, not a
#: "translucent" second element — a clean silhouette edge legitimately climbs
#: this high over 2-3px of antialiasing.
ALPHA_GHOST_HI = 230

#: Standing-height / width ratio may drift this much from the anchor's own
#: ratio before a frame is a different shape, not a different pose. Measured
#: against the party-fit tolerance in spritefit.HEIGHT_TOL (6%); a pose can
#: legitimately crouch or lunge wider than idle, so this is looser.
RATIO_TOL = 0.35

#: Average nearest-colour RGB distance (0-441 diagonal) a frame's opaque
#: pixels may sit from the anchor's palette before it reads as a different
#: character's colours. The bearded biker's frames on the gnome's sheet
#: measured >150; a same-character pose that leans into shadow measured <40.
PALETTE_DIST_MAX = 90.0

#: A secondary alpha component (not the frame's main silhouette) whose alpha
#: sits in the ghost band and whose area exceeds this many px fails as
#: translucency bleed. Measured: the sniper's death frames carried a second
#: head+arm blob of 900-2400px at 640x960 render, cyan smears on relocate_0/1
#: measured 300-700px.
TRANSLUCENT_GHOST_AREA_PX = 180

#: A secondary component whose bbox top falls in the figure's own top-35%
#: band (shoulder line up) and whose area exceeds this fails as a second
#: head. Same measured death-frame blob as above; a raised hand or held prop
#: in that band is normally CONNECTED to the main silhouette, not separate.
SECOND_HEAD_AREA_PX = 250
#: Where the "shoulder line" sits, as a fraction of the alpha bbox height
#: from the top. Per the item spec.
SHOULDER_BAND = 0.35

#: More components than this (main figure + this many extras: a held weapon,
#: a detached hat brim) is a component-count sanity failure — most
#: single-character frames have 1-2 components; a rifle standing upright
#: alone next to nothing (death_3) is 1 component that is NOT the figure.
MAX_COMPONENTS = 4
#: A secondary component at least this fraction of the main component's area
#: is "the size of the figure" — a detached duplicate, not a prop.
DETACHED_OBJECT_AREA_FRAC = 0.5

#: Two frames meant to be adjacent in a cycle (or literal repeats) whose
#: opaque pixels differ by less than this fraction of the union area are
#: near-duplicates — the model returned (near) the same image twice.
NEAR_DUP_MAX_DIFF_FRACTION = 0.03

#: A prop (subject_class="prop") frame whose main-component bbox aspect
#: (w/h) has moved from the anchor's aspect by more than this fraction, OR
#: whose component count grew by more than PROP_COMPONENT_GROWTH over the
#: anchor's, reads as limb-like protrusions. Measured: the gas pump turret's
#: aspect moved ~0.6 -> ~1.1 in fire_1 when it grew arms.
PROP_ASPECT_TOL = 0.30
PROP_COMPONENT_GROWTH = 1

#: Prompts describing a fall/death/collapse are a known doubling trigger —
#: the model paints the figure mid-fall AND its start pose, overlapping. Any
#: pose name or description containing one of these should prefer
#: fall-from-pose compositing over a fresh generation (see sprites.py
#: fall_from_pose). Substring match, case-insensitive.
FALL_PROMPT_RISK = ("fall", "death", "die", "collapse", "ko", "faint")


class FrameGateError(ValueError):
    """A gate input that cannot be judged at all (bad path, empty image)."""


# ---------------------------------------------------------------------------
# Pixels in, plain data out
# ---------------------------------------------------------------------------

def _rgba(img: Image.Image) -> Image.Image:
    return img.convert("RGBA") if img.mode != "RGBA" else img


def opaque_bbox(img: Image.Image, hard: int = ALPHA_HARD) -> Optional[tuple[int, int, int, int]]:
    """Bounding box of pixels at/above `hard` alpha, or None if there are none."""
    alpha = _rgba(img).getchannel("A").point(lambda a: 255 if a >= hard else 0)
    return alpha.getbbox()


def _components(mask: list[list[bool]], w: int, h: int) -> list[dict]:
    """Flood-fill connected components (4-neighbour) over a boolean grid.

    Returns [{area, box: (x0,y0,x1,y1)}], largest first. Pure BFS — frame
    sizes here are a few hundred px on a side, small enough that this never
    needs numpy/scipy.
    """
    seen = [[False] * w for _ in range(h)]
    out: list[dict] = []
    for y0 in range(h):
        row = mask[y0]
        for x0 in range(w):
            if not row[x0] or seen[y0][x0]:
                continue
            stack = [(x0, y0)]
            seen[y0][x0] = True
            area = 0
            minx = maxx = x0
            miny = maxy = y0
            while stack:
                x, y = stack.pop()
                area += 1
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny][nx] and mask[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            out.append({"area": area, "box": (minx, miny, maxx + 1, maxy + 1)})
    out.sort(key=lambda c: -c["area"])
    return out


def components_at(img: Image.Image, *, lo: int = ALPHA_GHOST_LO) -> list[dict]:
    """Connected components of pixels with alpha >= `lo` (default: any non-fully-
    transparent pixel), largest first."""
    img = _rgba(img)
    w, h = img.size
    alpha = img.getchannel("A")
    px = alpha.load()
    mask = [[px[x, y] >= lo for x in range(w)] for y in range(h)]
    return _components(mask, w, h)


def _mean_alpha_in_box(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    a = _rgba(img).getchannel("A").crop(box)
    data = list(a.getdata())
    return sum(data) / len(data) if data else 0.0


def palette_of(img: Image.Image, colors: int = 12, *, hard: int = ALPHA_HARD) -> list[tuple[int, int, int]]:
    """The `colors` most common opaque RGB values, most common first."""
    img = _rgba(img)
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b, a in img.getdata():
        if a >= hard:
            key = (r, g, b)
            counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [c for c, _ in ranked[:colors]]


def palette_distance(img: Image.Image, anchor_palette: Sequence[tuple[int, int, int]], *,
                     colors: int = 12) -> float:
    """Mean distance from this frame's opaque colours to their nearest anchor
    colour. Empty frame or empty anchor palette -> 0.0 (nothing to disagree on)."""
    if not anchor_palette:
        return 0.0
    frame_pal = palette_of(img, colors=colors)
    if not frame_pal:
        return 0.0
    total = 0.0
    for c in frame_pal:
        best = min(
            ((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2 + (c[2] - a[2]) ** 2) ** 0.5
            for a in anchor_palette)
        total += best
    return total / len(frame_pal)


def pixel_diff_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fraction of the union of opaque pixels that differ (position OR colour)
    between two SAME-SIZE frames. 1.0 if the sizes differ (can't compare)."""
    a, b = _rgba(a), _rgba(b)
    if a.size != b.size:
        return 1.0
    da, db = list(a.getdata()), list(b.getdata())
    union = 0
    diff = 0
    for pa, pb in zip(da, db):
        opaque_a = pa[3] >= ALPHA_HARD
        opaque_b = pb[3] >= ALPHA_HARD
        if not (opaque_a or opaque_b):
            continue
        union += 1
        if opaque_a != opaque_b or pa[:3] != pb[:3]:
            diff += 1
    return (diff / union) if union else 0.0


# ---------------------------------------------------------------------------
# The anchor — what every frame is judged against
# ---------------------------------------------------------------------------

def anchor_band(anchor: Image.Image) -> dict:
    """The reference facts a frame is judged against: bbox ratio, palette,
    aspect, component count. Call once per character; reuse for every frame."""
    box = opaque_bbox(anchor)
    if box is None:
        raise FrameGateError("anchor has no opaque pixels - nothing to anchor to")
    w = box[2] - box[0]
    h = box[3] - box[1]
    comps = components_at(anchor)
    return {
        "box": box, "width": w, "height": h,
        "ratio": (h / w) if w else 0.0,
        "aspect": (w / h) if h else 0.0,
        "palette": palette_of(anchor),
        "components": len(comps),
    }


def anchor_band_path(path: str | os.PathLike[str]) -> dict:
    with Image.open(path) as im:
        return anchor_band(im.convert("RGBA"))


# ---------------------------------------------------------------------------
# The per-frame verdict
# ---------------------------------------------------------------------------

def frame_verdict(frame: Image.Image, anchor: dict, *,
                  subject_class: str = "character",
                  adjacent: Optional[Image.Image] = None,
                  adjacent_name: str = "") -> dict:
    """Judge ONE frame against the character's anchor band.

    subject_class: "character" runs the full set (identity + ghost + shape).
    "prop" additionally runs the limb-growth check and is lenient on the
    palette/ratio checks a moving prop legitimately varies on.

    Returns {ok, checks: [{name, fired, measured, threshold, detail}, ...]}.
    `ok` is False iff any check with fired=True is present. Every check
    always appears, fired or not, so a caller can see what was measured even
    on a pass.
    """
    frame = _rgba(frame)
    checks: list[dict] = []

    def _check(name: str, fired: bool, measured: Any, threshold: Any, detail: str) -> None:
        checks.append({"name": name, "fired": bool(fired), "measured": measured,
                       "threshold": threshold, "detail": detail})

    box = opaque_bbox(frame)
    comps = components_at(frame)
    main = comps[0] if comps else None

    # --- shape vs anchor band -------------------------------------------
    if box is None or main is None:
        _check("empty_frame", True, 0, ">0 opaque px",
               "no opaque pixels - the frame is blank")
    else:
        w = box[2] - box[0]
        h = box[3] - box[1]
        ratio = (h / w) if w else 0.0
        if anchor.get("ratio"):
            rel = abs(ratio - anchor["ratio"]) / anchor["ratio"]
            fired = rel > RATIO_TOL
            _check("shape_outlier", fired, round(ratio, 3),
                   f"{anchor['ratio']:.3f} +/- {RATIO_TOL:.0%}",
                   f"height/width ratio {ratio:.3f} vs anchor {anchor['ratio']:.3f}")

        # --- palette vs anchor ------------------------------------------
        if subject_class != "prop":
            dist = palette_distance(frame, anchor.get("palette") or [])
            fired = dist > PALETTE_DIST_MAX
            _check("wrong_palette", fired, round(dist, 1), PALETTE_DIST_MAX,
                   f"mean nearest-colour distance {dist:.1f} vs anchor palette")

    # --- ghost / doubling checks -----------------------------------------
    extras = comps[1:]
    ghost_area = 0
    second_head = None
    for c in extras:
        mean_a = _mean_alpha_in_box(frame, c["box"])
        if ALPHA_GHOST_LO <= mean_a <= ALPHA_GHOST_HI:
            ghost_area += c["area"]
        if main is not None:
            main_top = main["box"][1]
            main_h = main["box"][3] - main["box"][1]
            shoulder_y = main_top + main_h * SHOULDER_BAND
            if c["box"][1] <= shoulder_y and c["area"] >= SECOND_HEAD_AREA_PX:
                if second_head is None or c["area"] > second_head["area"]:
                    second_head = c
    _check("translucency_ghost", ghost_area > TRANSLUCENT_GHOST_AREA_PX,
          ghost_area, TRANSLUCENT_GHOST_AREA_PX,
          "translucent pixels outside the main silhouette")
    _check("second_head", second_head is not None,
          (second_head or {}).get("area", 0), SECOND_HEAD_AREA_PX,
          "a secondary component above the shoulder line - doubled figure")

    detached = any(c["area"] >= DETACHED_OBJECT_AREA_FRAC * (main["area"] if main else 1)
                   for c in extras)
    _check("component_count", len(comps) > MAX_COMPONENTS or detached,
          len(comps), MAX_COMPONENTS,
          "too many disjoint alpha blobs, or one the size of the figure")

    # --- prop drift --------------------------------------------------------
    if subject_class == "prop" and box is not None:
        w = box[2] - box[0]
        h = box[3] - box[1]
        aspect = (w / h) if h else 0.0
        anchor_aspect = anchor.get("aspect") or aspect
        rel = abs(aspect - anchor_aspect) / anchor_aspect if anchor_aspect else 0.0
        comp_growth = len(comps) - int(anchor.get("components") or 1)
        fired = rel > PROP_ASPECT_TOL or comp_growth > PROP_COMPONENT_GROWTH
        _check("prop_grew_limbs", fired,
              {"aspect": round(aspect, 3), "components": len(comps)},
              {"aspect_tol": PROP_ASPECT_TOL, "component_growth": PROP_COMPONENT_GROWTH},
              f"aspect {aspect:.3f} vs anchor {anchor_aspect:.3f}, "
              f"components {len(comps)} vs anchor {anchor.get('components')}")

    # --- near-duplicate vs an adjacent cycle frame -------------------------
    if adjacent is not None:
        diff = pixel_diff_fraction(frame, adjacent)
        fired = diff < NEAR_DUP_MAX_DIFF_FRACTION
        _check("near_duplicate", fired, round(diff, 4), NEAR_DUP_MAX_DIFF_FRACTION,
              f"differs from {adjacent_name or 'adjacent frame'} by only "
              f"{diff:.1%} of opaque pixels")

    ok = not any(c["fired"] for c in checks)
    return {"ok": ok, "checks": checks,
           "failed": [c["name"] for c in checks if c["fired"]]}


def is_fall_risk(name: str, description: str = "") -> bool:
    """Does this pose's name/description match a known doubling trigger?"""
    text = f"{name} {description}".lower()
    return any(t in text for t in FALL_PROMPT_RISK)


def gate_sheet(frame_paths: dict[str, str], anchor_path: str | os.PathLike[str], *,
              subject_class: str = "character",
              cycles: Optional[dict[str, list[str]]] = None) -> dict:
    """Judge every frame in a sheet against ONE anchor. `frame_paths` is
    {pose_name: path}; `cycles` is {animation_name: [pose_name, ...]} in
    playback order, used to check adjacent frames for near-duplicates -
    without it every frame skips that one check.

    Returns {ok, anchor, frames: {pose_name: verdict}, failed: [pose_name,...]}.
    """
    anchor = anchor_band_path(anchor_path)
    order_of: dict[str, tuple[str, int]] = {}
    for anim, seq in (cycles or {}).items():
        for i, pname in enumerate(seq):
            order_of[pname] = (anim, i)

    frames: dict[str, dict] = {}
    failed: list[str] = []
    for pname, path in frame_paths.items():
        adjacent_img = None
        adjacent_name = ""
        if pname in order_of:
            anim, i = order_of[pname]
            seq = cycles[anim]
            if len(seq) > 1:
                nxt = seq[(i + 1) % len(seq)]
                if nxt != pname and nxt in frame_paths:
                    adjacent_name = nxt
                    try:
                        adjacent_img = Image.open(frame_paths[nxt]).convert("RGBA")
                    except Exception:
                        adjacent_img = None
        try:
            with Image.open(path) as im:
                v = frame_verdict(im.convert("RGBA"), anchor,
                                  subject_class=subject_class,
                                  adjacent=adjacent_img, adjacent_name=adjacent_name)
        finally:
            if adjacent_img is not None:
                adjacent_img.close()
        frames[pname] = v
        if not v["ok"]:
            failed.append(pname)
    return {"ok": not failed, "anchor": {"path": str(anchor_path), **anchor},
           "frames": frames, "failed": failed}


# ---------------------------------------------------------------------------
# Provenance — which anchor a pose/reference file was generated against
# ---------------------------------------------------------------------------

def _sidecar_path(path: str | os.PathLike[str]) -> Path:
    return Path(str(path) + ".provenance.json")


def file_hash(path: str | os.PathLike[str]) -> str:
    """sha256 of a file's bytes - the same identity kie's content-hash upload
    naming (de59d3a) uses, so a frame's provenance and its upload name can be
    compared by the same measure."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_provenance(path: str | os.PathLike[str], *, anchor_hash: str,
                     prompt: str = "", extra: Optional[dict] = None) -> dict:
    """Sidecar recording what anchor this file was generated against, and
    when. Written next to the file it describes, never embedded - a PNG gets
    re-encoded by every tool in this pipeline and metadata does not survive."""
    doc = {"anchor_hash": anchor_hash, "prompt": prompt[:2000],
          "time": time.time(), **(extra or {})}
    _sidecar_path(path).write_text(json.dumps(doc), encoding="utf-8")
    return doc


def read_provenance(path: str | os.PathLike[str]) -> Optional[dict]:
    """The sidecar, or None - a MISSING sidecar is the normal state for any
    file this module has never written (a hand-placed reference), and is
    reported by callers as "no provenance", never crashed on."""
    p = _sidecar_path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def stale_frames(frame_paths: dict[str, str], current_anchor_hash: str) -> list[dict]:
    """Which frames' provenance disagrees with the CURRENT anchor, or is
    missing. [{name, path, reason, recorded_hash}]. This is what stops a
    regenerate-only-what-is-wrong reopen from re-stitching a 05:xx frame
    (generated against a since-replaced anchor) next to clean 08:xx frames -
    every frame in the folder is trusted forever otherwise."""
    stale: list[dict] = []
    for name, path in frame_paths.items():
        prov = read_provenance(path)
        if prov is None:
            stale.append({"name": name, "path": path, "reason": "no provenance sidecar",
                         "recorded_hash": None})
        elif prov.get("anchor_hash") != current_anchor_hash:
            stale.append({"name": name, "path": path,
                         "reason": "anchor hash differs from the current anchor",
                         "recorded_hash": prov.get("anchor_hash")})
    return stale


# ---------------------------------------------------------------------------
# ITEM 9 — conform a stamped asset (gear/weapon) to the pinned palette
# ---------------------------------------------------------------------------

#: A frame's stroke width (measured by erosion count on its opaque mask) more
#: than this many px from the anchor's own median stroke reads as an ink-
#: weight mismatch — a 1px-lined weapon stamped onto a palette-conformed torso.
LINE_WEIGHT_TOL_PX = 0.75


def _erode_once(mask: list[list[bool]], w: int, h: int) -> list[list[bool]]:
    out = [[False] * w for _ in range(h)]
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x]:
                continue
            if (x > 0 and not row[x - 1]) or (x < w - 1 and not row[x + 1]) \
                    or (y > 0 and not mask[y - 1][x]) or (y < h - 1 and not mask[y + 1][x]):
                continue
            out[y][x] = True
    return out


def median_stroke_width(img: Image.Image, *, hard: int = ALPHA_HARD) -> float:
    """Erode the opaque mask repeatedly; the erosion count that empties it,
    doubled, approximates the mask's thinnest-line stroke width in px. A
    solid fill erodes many times (its "stroke" is its narrowest dimension);
    a 1px ink line erodes once. Cheap proxy, not a real skeleton — good
    enough to catch "this asset's linework is a different weight"."""
    img = _rgba(img)
    w, h = img.size
    alpha = img.getchannel("A")
    px = alpha.load()
    mask = [[px[x, y] >= hard for x in range(w)] for y in range(h)]
    if not any(any(row) for row in mask):
        return 0.0
    steps = 0
    while any(any(row) for row in mask):
        mask = _erode_once(mask, w, h)
        steps += 1
        if steps > max(w, h):
            break
    return float(steps * 2)


def conform_stamp(stamp: Image.Image, anchor_palette: Sequence[tuple[int, int, int]],
                  anchor_stroke: float, *, hard: int = ALPHA_HARD) -> dict:
    """Conform a stamped asset (a weapon, a piece of gear) to the character's
    pinned palette, and flag an ink-weight mismatch the palette conform
    cannot fix.

    Returns {image, line_weight_flag, stamp_stroke, anchor_stroke}. The
    caller decides what to do with a flagged mismatch (relaunch the gear gen,
    or ship it and say so) - this reports, it does not refuse, per the
    report-shaped convention every other check here follows.
    """
    stamp = _rgba(stamp).copy()
    pal = [tuple(int(v) for v in c[:3]) for c in anchor_palette]
    if pal:
        cache: dict[tuple[int, int, int], tuple[int, int, int]] = {}
        px = stamp.load()
        for y in range(stamp.height):
            for x in range(stamp.width):
                r, g, b, a = px[x, y]
                if a < hard:
                    continue
                key = (r, g, b)
                hit = cache.get(key)
                if hit is None:
                    hit = min(pal, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2
                              + (c[2] - b) ** 2)
                    cache[key] = hit
                px[x, y] = (hit[0], hit[1], hit[2], a)
    stroke = median_stroke_width(stamp, hard=hard)
    flagged = abs(stroke - anchor_stroke) > LINE_WEIGHT_TOL_PX if anchor_stroke else False
    return {"image": stamp, "line_weight_flag": flagged,
           "stamp_stroke": stroke, "anchor_stroke": anchor_stroke}
