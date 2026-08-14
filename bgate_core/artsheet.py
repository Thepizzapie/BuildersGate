"""The sheet in progress, frame by frame, measured against the pin.

The art seat's brief is an order of operations — pin the reference, condition
every frame on it, MEASURE THE RESULT — and the workspace could show the first
two and not the third. Every art artifact already carries a whole-sheet alpha
audit and a chroma report from the moment it was generated, but a sheet is a ROW
OF FRAMES and a whole-sheet average is exactly the wrong resolution for the
question being asked: one bad cell in six moves a mean by a sixth and disappears,
which is how a sheet with a white halo on the follow-through frame passes.

So this does two separate things and keeps them separate:

  * REPORTS what generation already measured (``metadata.alpha``,
    ``metadata.chroma``, ``metadata.ref_pins``). Read, never recomputed — a
    number the generator wrote about the bytes it wrote is the authority.
  * MEASURES PER FRAME, by slicing the row into cells and running the same
    :func:`chroma.audit` over each one. This is new work and it is the point:
    the flag lands on the frame that earned it.

FRAME GEOMETRY IS DERIVED, NOT ASSUMED. A row sheet is N square cells side by
side, so N = width / height when that divides exactly. When it does not — a
grid, a single portrait, a tile — the frame count is unknown and this says so
rather than inventing a split, because a wrong split measures the seam between
two frames and calls it a hole.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

# THE GATES COME FROM THE MODULE THAT ENFORCES THEM. measures() ships each
# threshold alongside its number so no reader has to keep a second copy — the
# panel used to invent its own and got both directions wrong.
from . import chroma as _chroma

#: Artifact kinds this seat owns. An audio revision is an artifact too, and a
#: "newest artifact" that ignored kind put an .mp3 in the art panel.
ART_KINDS = frozenset({"texture", "sprite", "sheet", "image", "portrait",
                       "tile", "concept"})

#: A revision that has been replaced is not the sheet in progress.
DEAD_STATUS = frozenset({"superseded", "rejected", "discarded"})

#: Cells beyond this are not a strip and slicing them costs more than it says.
MAX_FRAMES = 24

# (resolved path, mtime_ns, size, n) -> per-frame rows
_FRAME_CACHE: dict[tuple[str, int, int, int], list[dict]] = {}


def _num(meta: dict, *path: str) -> Optional[float]:
    cur: Any = meta
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None


def frame_count(size: str) -> Optional[int]:
    """How many frames a ``"1536x512"`` row holds, or None if it is not a row.

    None is a real answer here. A 1024x1024 portrait is one image, and calling
    it "one frame" and calling a 3-cell row "three frames" using the same word
    would let the caller draw a strip over something that has no strip.
    """
    try:
        w, h = (int(part) for part in str(size).lower().split("x", 1))
    except (ValueError, TypeError):
        return None
    if w <= 0 or h <= 0 or w % h:
        return None
    n = w // h
    return n if 1 < n <= MAX_FRAMES else None


def _copy(rows: list[dict]) -> list[dict]:
    """A caller's edit must not reach the next caller. ``dict(row)`` is shallow
    and the flag LIST is the field a caller is most likely to append to."""
    return [{k: (list(v) if isinstance(v, list) else v) for k, v in row.items()}
            for row in rows]


def measure_frames(path: str | os.PathLike[str], n: int, *,
                   chroma_rgb: Optional[Sequence[int]] = None) -> list[dict]:
    """Slice a row of ``n`` cells and audit each one.

    Returns ``[{index, flags, review, clean, dirty_alpha, white_fringe,
    border_opaque, soft_alpha, hollow}]``. An unreadable image yields ``[]`` —
    the caller renders "not measured", which is different from "clean".
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size, n)
    hit = _FRAME_CACHE.get(key)
    if hit is not None:
        return _copy(hit)

    try:
        from PIL import Image

        from . import chroma as _chroma
    except ImportError:
        return []

    rows: list[dict] = []
    try:
        with Image.open(p) as im:
            im = im.convert("RGBA")
            w, h = im.size
            cell = w // n
            import tempfile
            for i in range(n):
                crop = im.crop((i * cell, 0, (i + 1) * cell, h))
                # chroma.audit reads a PATH — it is the generation-time gate and
                # every caller has a file. A temp cell is cheaper than teaching
                # the gate a second entry point that could drift from it.
                with tempfile.TemporaryDirectory() as tmp:
                    cellpath = Path(tmp) / f"cell{i}.png"
                    crop.save(cellpath)
                    rep = _chroma.audit(cellpath, chroma_rgb)
                # BORDER OPACITY IS NOT A PER-FRAME PROPERTY and reporting it
                # as one flagged every cell of a clean sheet. The measurement
                # asks "is the outer edge of this image opaque", which detects
                # an unkeyed backdrop on a whole picture; on a cell it detects
                # the CUT — a walk cycle whose character crosses the seam is
                # opaque at the cell edge by construction. Measured on this
                # project: 0.0 for the sheet, 0.20/0.43/0.22 for its three
                # cells, all three "background bleed". The whole-sheet number
                # is the one that means it, and it is in `measures`.
                flags = [f for f in (rep.get("flags") or [])
                         if not str(f).startswith("background bleed")]
                rows.append({
                    "index": i,
                    "flags": flags,
                    "review": list(rep.get("review") or []),
                    "clean": not flags,
                    "dirty_alpha": rep.get("dirty_alpha"),
                    "white_fringe": rep.get("white_fringe"),
                    "soft_alpha": rep.get("soft_alpha"),
                    "hollow": rep.get("hollow"),
                })
    except Exception:                                            # noqa: BLE001
        # A corrupt PNG must not take the workspace down, and "" is the honest
        # answer: nothing was measured.
        return []

    _FRAME_CACHE[key] = rows
    return _copy(rows)


def measures(meta: dict) -> list[dict]:
    """The whole-sheet numbers generation already wrote, as ``{label, value}``.

    ``value`` is the measured fraction and ``good`` says which direction is
    better, because half of these are "more is worse" (dirty alpha) and half are
    "more is better" (chroma distance) and a bar that got that backwards would
    read as a pass. Only measurements that EXIST appear — a key absent from the
    metadata is a row that is not drawn, never a zero.
    """
    out: list[dict] = []

    def add(label: str, value: Optional[float], *, hi_is_good: bool,
            scale: float = 1.0, note: str = "",
            gate: Optional[float] = None) -> None:
        if value is None:
            return
        # THE UNIT TRAVELS WITH THE NUMBER. Most of these are fractions of 1;
        # chroma distance is a length in RGB space and rendering it with the
        # fractions' three decimals put "223.600" in a column of 0.024s.
        display = (f"{value:.3f}" if scale == 1.0
                   else f"{value:.0f} / {scale:.0f}")
        row = {"label": label, "value": round(float(value), 3),
               "display": display,
               "fraction": max(0.0, min(1.0, float(value) / scale)),
               "hi_is_good": hi_is_good, "note": note}
        # THE GATE TRAVELS WITH THE NUMBER, so a reader never has to know which
        # constant applies. Before this the panel invented its own thresholds
        # (>0.5 good, <0.15 bad) and got both directions wrong against the gates
        # chroma.py actually enforces: a 0.10 border is a hard bleed failure and
        # drew green, while 20 of 40 families on the reference project drew
        # orange for chroma headroom that was comfortably over the floor. A UI
        # that recomputes a verdict is a second copy of the rule, and the copy
        # is the one that drifts.
        if gate is not None:
            row["gate"] = float(gate)
            row["passes"] = (float(value) >= float(gate) if hi_is_good
                             else float(value) <= float(gate))
        out.append(row)

    add("dirty alpha", _num(meta, "alpha", "dirty_alpha"), hi_is_good=False,
        gate=_chroma.DIRTY_ALPHA_MAX,
        note="transparent pixels still carrying RGB")
    add("white halo", _num(meta, "alpha", "white_fringe"), hi_is_good=False,
        gate=_chroma.WHITE_FRINGE_MAX,
        note="near-white feathering around the sprite")
    add("soft alpha", _num(meta, "alpha", "soft_alpha"), hi_is_good=False,
        gate=_chroma.SOFT_ALPHA_MAX,
        note="partial-alpha edge — the backdrop shaded into the subject")
    add("background bleed", _num(meta, "alpha", "border_opaque"), hi_is_good=False,
        gate=_chroma.BORDER_OPAQUE_MAX,
        note="opaque frame border — the backdrop was not keyable")
    add("hollow interior", _num(meta, "alpha", "hollow"), hi_is_good=False,
        gate=_chroma.HOLLOW_FAIL,
        note="transparency enclosed by the figure")
    add("residual chroma", _num(meta, "alpha", "residual_chroma"), hi_is_good=False,
        gate=_chroma.RESIDUAL_CHROMA_MAX,
        note="key colour still opaque in the frame")
    dist = _num(meta, "chroma", "distance")
    if dist is not None:
        # 442 is the diagonal of the RGB cube — the largest distance possible —
        # so the fraction is a real proportion rather than a chosen ceiling.
        add("chroma headroom", dist, hi_is_good=True, scale=441.7,
            gate=_chroma.MIN_SAFE_DISTANCE,
            note=f"{meta.get('chroma', {}).get('name', 'key')} is this far from "
                 "the nearest colour in the art")
    return out


def pick(artifacts: Sequence[dict]) -> Optional[dict]:
    """The sheet in progress: the newest live art revision.

    "Live" excludes superseded rows — a family's r3 and r4 are the same picture
    and showing r3 because it sorted first is how the panel showed work that had
    already been replaced.
    """
    for a in artifacts:
        if str(a.get("kind") or "") not in ART_KINDS:
            continue
        if str(a.get("status") or "") in DEAD_STATUS:
            continue
        return a
    return None


def report(artifact: Optional[dict], *, root: str | os.PathLike[str],
           slice_frames: bool = True) -> dict:
    """Everything the Sheets panel draws for one artifact.

    ``{sheet, frames, measures, pin, flags}``. ``sheet`` is None when the
    project has generated no art — the caller says which tool would file one.
    """
    if not artifact:
        return {"sheet": None, "frames": [], "measures": [], "pin": None,
                "flags": []}
    meta = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    size = str(meta.get("size") or "")
    n = frame_count(size)
    chroma_rgb = None
    craw = meta.get("chroma")
    if isinstance(craw, dict) and isinstance(craw.get("rgb"), (list, tuple)):
        chroma_rgb = list(craw["rgb"])[:3]

    frames: list[dict] = []
    if n and slice_frames:
        full = Path(root) / str(artifact.get("path") or "")
        frames = measure_frames(full, n, chroma_rgb=chroma_rgb)

    pins = meta.get("ref_pins")
    pin = None
    if isinstance(pins, list) and pins and isinstance(pins[0], dict):
        first = pins[0]
        pin = {"name": first.get("name"), "revision": first.get("revision"),
               "path": first.get("path"),
               # The pin is what every frame was conditioned on. Saying so is
               # the difference between a thumbnail and a contract.
               "note": "approved — every frame conditions on this"}

    return {
        "sheet": {
            "id": artifact.get("id"),
            "logical_name": artifact.get("logical_name"),
            "revision": artifact.get("revision"),
            "path": artifact.get("path"),
            "status": artifact.get("status"),
            "kind": artifact.get("kind"),
            "created_at": artifact.get("created_at"),
            "review_note": artifact.get("review_note"),
            "size": size,
            "frames": n,
            "producer": artifact.get("producer"),
            "work_item_id": artifact.get("work_item_id"),
            "promoted": bool((meta.get("integration") or {}).get("promoted"))
            if isinstance(meta.get("integration"), dict) else None,
        },
        "frames": frames,
        "measures": measures(meta),
        "pin": pin,
        "flags": list((meta.get("alpha") or {}).get("flags") or [])
        if isinstance(meta.get("alpha"), dict) else [],
    }
