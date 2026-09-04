"""The sprite editor's API — open a sheet, paint it, label it for rigging.

Generated sprite art lands 90% right and 10% wrong, and the 10% is always the
same kind of wrong: one stray pixel in the walk cycle, a halo the matte missed,
a hand that reads as a mitten at 64px. Re-rolling the generator to fix four
pixels is the most expensive possible way to fix four pixels, so this is the
cheap way — open the actual file, paint on it, save it back.

Three things this owns, and the boundaries matter:

  * PIXELS. The browser sends back a full PNG of the edited sheet; the server
    validates the dimensions did not change and writes it, keeping the previous
    bytes under .bgate_out/sprite_backups. Dimension-locking is not a
    formality — every SpriteFrames region, every gear anchor, and every
    aligned gear sheet is expressed in this sheet's exact geometry, so a resize
    would silently invalidate the whole rig.
  * LABELS. Slot anchors and animations go to the rig sidecar (bgate_core.art.rigmap),
    which is what turns gear.py's guessing into reading.
  * EXPORT. The labelled animations become a real Godot SpriteFrames .tres.

Everything is project-root-relative and refuses to escape it. Nothing here
talks to a model or spends money.
"""
from __future__ import annotations

import base64
import binascii
import io
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from PIL import Image

from bgate_core.art import rigmap
from bgate_ui import api
from bgate_ui.deps import root, safe_under

router = APIRouter()

# The editor paints RGBA and saves PNG. A .jpg sheet would silently lose its
# alpha on the round-trip, and a sprite sheet without alpha is not a sprite
# sheet, so the editable set is narrower than the previewable one on purpose.
EDITABLE = {".png", ".webp"}

# A canvas the browser can actually hold, and a payload the server will accept.
MAX_PIXELS = 16_000_000          # ~4000x4000
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

# The picker walks the tree; a project with a huge scratch directory must not
# turn one dropdown into a minute of stat() calls.
SCAN_CAP = 6000

BACKUP_DIRNAME = "sprite_backups"

# Frame regeneration. The cap is a cost guard, enforced by the editor per
# batch and stated to the user before anything is spent — twelve frames at
# high quality is two dollars, which is a decision, not a click.
MAX_REGEN_FRAMES = 12
# What one cell is upscaled to before the model sees it. 1024 is gpt-image's
# native square; anything smaller wastes the call and anything larger is
# resized by the API anyway.
REGEN_EDGE = 1024


def _backup_dir(project_root: Path) -> Path:
    return project_root / ".bgate_out" / BACKUP_DIRNAME


def _sheet(rel: str) -> tuple[Path, Path]:
    """Resolve an editable sheet inside the project. Raises on anything else."""
    base = root()
    target = safe_under(base, rel, must_be_image=True)
    if target.suffix.lower() not in EDITABLE:
        raise api.ApiError(415, "only .png and .webp sheets are editable",
                           detail={"rel": rel, "editable": sorted(EDITABLE)})
    if not target.is_file():
        raise api.not_found(f"no sheet at {rel}", rel=rel)
    return base, target


def _res_path(project_root: Path, target: Path) -> Optional[str]:
    """The ``res://`` address of a file, if it lives inside the Godot project."""
    for cand in (project_root, project_root / "game"):
        if (cand / "project.godot").is_file():
            try:
                return f"res://{target.relative_to(cand).as_posix()}"
            except ValueError:
                continue
    return None


def _describe(project_root: Path, target: Path) -> dict:
    with Image.open(target) as im:
        size = im.size
    rel = target.relative_to(project_root).as_posix()
    data = rigmap.load(target)
    # A sheet with no grid yet gets ONE suggestion, from the same detector the
    # gear pipeline uses — so the number the editor shows and the number the
    # rig pipeline would have used are the same number.
    suggested = None
    if not data.get("grid"):
        try:
            from bgate_core.art.gear import detect_grid
            with Image.open(target) as im:
                g = detect_grid(im.convert("RGBA"))
            suggested = {"cell_w": g.cell_w, "cell_h": g.cell_h,
                         "cols": g.cols, "rows": g.rows}
        except Exception:
            suggested = None      # detection is a convenience, never a blocker
    return {
        "rel": rel,
        "name": target.name,
        "width": size[0], "height": size[1],
        "mtime": int(target.stat().st_mtime),
        "res_path": _res_path(project_root, target),
        "sidecar": rigmap.sidecar_path(target).relative_to(project_root).as_posix(),
        "rig": data,
        "suggested_grid": suggested,
        "coverage": rigmap.coverage(data),
        "hands": rigmap.hand_coverage(data),
        "known_slots": list(rigmap.KNOWN_SLOTS),
        "hand_slots": list(rigmap.HAND_SLOTS),
    }


def _new_sheet_target(rel: str) -> tuple[Path, Path]:
    """Resolve a new PNG without allowing replacement or a project escape."""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel:
        raise api.bad_request("choose a project path for the sprite sheet")
    if not Path(rel).suffix:
        rel += ".png"
    project_root = root()
    target = safe_under(project_root, rel, must_be_image=True)
    if target.suffix.lower() != ".png":
        raise api.bad_request("new sprite sheets must be saved as .png", rel=rel)
    if target.exists():
        raise api.conflict("a file already exists at that path", rel=rel)
    return project_root, target


def _write_new_sheet(project_root: Path, target: Path, image: Image.Image,
                     grid: Optional[dict] = None) -> dict:
    """Atomically write a new editable sheet and its initial rig sidecar."""
    if image.width * image.height > MAX_PIXELS:
        raise api.ApiError(413, "image is too large to accept",
                           detail={"pixels": image.width * image.height,
                                   "max": MAX_PIXELS})
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".png.tmp")
    image.convert("RGBA").save(tmp, format="PNG")
    os.replace(tmp, target)
    try:
        rigmap.save(target, rigmap.empty(grid), sheet_size=image.size)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    try:
        from bgate_core.store import assets
        assets.track(project_root, target)
    except Exception:
        pass
    return _describe(project_root, target)


@router.get("/api/sprite/open")
def sprite_open(rel: str) -> dict:
    """Everything the editor needs on load. The pixels come from /api/preview."""
    project_root, target = _sheet(rel)
    return _describe(project_root, target)


@router.post("/api/sprite/create")
def sprite_create(payload: dict) -> dict:
    """Create a transparent, gridded PNG and open it as a first-class sheet."""
    project_root, target = _new_sheet_target(str(payload.get("rel") or ""))
    values = {}
    for key in ("cell_w", "cell_h", "cols", "rows"):
        try:
            values[key] = int(payload.get(key))
        except (TypeError, ValueError):
            raise api.bad_request(f"{key} must be an integer")
    if not 1 <= values["cell_w"] <= 2048 or not 1 <= values["cell_h"] <= 2048:
        raise api.bad_request("frame width and height must be between 1 and 2048")
    if not 1 <= values["cols"] <= 64 or not 1 <= values["rows"] <= 64:
        raise api.bad_request("columns and rows must be between 1 and 64")
    width = values["cell_w"] * values["cols"]
    height = values["cell_h"] * values["rows"]
    if width * height > MAX_PIXELS:
        raise api.ApiError(413, "sheet dimensions exceed the canvas limit",
                           detail={"width": width, "height": height,
                                   "pixels": width * height, "max": MAX_PIXELS})
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    return api.ok(_write_new_sheet(project_root, target, image, values))


@router.post("/api/sprite/import")
def sprite_import(payload: dict) -> dict:
    """Copy an external browser-picked image into the project as editable PNG."""
    project_root, target = _new_sheet_target(str(payload.get("rel") or ""))
    raw = str(payload.get("image") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise api.bad_request("choose an image to import")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api.ApiError(413, "image payload too large",
                           detail={"bytes": len(raw), "max": MAX_UPLOAD_BYTES})
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api.bad_request(f"image data is not valid base64: {exc}")
    try:
        incoming = Image.open(io.BytesIO(blob))
        if incoming.width * incoming.height > MAX_PIXELS:
            raise api.ApiError(413, "image is too large to accept",
                               detail={"pixels": incoming.width * incoming.height,
                                       "max": MAX_PIXELS})
        incoming.load()
    except api.ApiError:
        raise
    except Exception as exc:
        raise api.bad_request(f"file is not a readable image: {exc}")
    return api.ok(_write_new_sheet(project_root, target, incoming))



# WHAT AN AGENT LEAVES BEHIND IS NOT ALL ART.
#
# A seat agent working on one item writes the sheet it was asked for AND a pile
# of things it made in order to look at its own work: review contact sheets,
# before/after pairs, chroma checks, comparison strips. They are all .png, they
# all sit under art/, and to a file walker they are indistinguishable from the
# deliverable. In a real project that is most of the list — this one has 2,837
# editable images and several hundred of them are an agent's own screenshots.
#
# A picker that mixes them is a picker you scroll. So each row carries a `kind`
# and the editor defaults to `art`, with the rest one click away rather than
# gone: a review strip is occasionally exactly the file you want to open, and a
# filter that cannot be turned off is a file you can no longer reach.
#
# PATH AND NAME ONLY, DELIBERATELY. There is no metadata to consult — hand-
# painted art has no artifact row, which is the whole reason this walks the tree
# instead of reading the asset table. So the rule is the naming these pipelines
# already use, and it is conservative: anything unrecognised is `art`, because
# hiding a real sheet is a worse failure than showing a screenshot.
_REVIEW_MARKS = ("review", "_check", "check_", "compare", "contact",
                 "_before", "before_", "_after", "after_", "_qa", "qa_",
                 "screenshot", "capture", "evidence", "_diff", "diff_",
                 # Zoomed crops and onion-skin strips: both are a picture OF a
                 # sheet made to inspect it, never the sheet the engine loads.
                 "_zoom", "zoom_", "onion_", "_onion")
_REVIEW_DIRS = {"review", "reviews", "checks", "before", "after", "qa",
                "compare", "screenshots", "evidence", "_cleared"}


def _kind(rel: str) -> str:
    """`art` | `review` | `test` — what this file is FOR."""
    parts = rel.lower().split("/")
    head = parts[0] if parts else ""
    if head in ("tests", "test"):
        return "test"
    # tmp is where everything goes to be looked at once and forgotten.
    if head == "tmp":
        return "review"
    if _REVIEW_DIRS & set(parts[:-1]):
        return "review"
    name = parts[-1]
    if any(m in name for m in _REVIEW_MARKS):
        return "review"
    return "art"

@router.get("/api/sprite/list")
def sprite_list(limit: int = 300, q: Optional[str] = None) -> dict:
    """Every editable sheet in the project, newest first, with rig status.

    The editor's own file picker. Walking the tree here rather than reusing the
    asset workspace's list is deliberate: hand-painted art that never went
    through the generator has no artifact row, and those are exactly the files
    someone opens an editor to touch.

    The whole matching set is collected and sorted BEFORE the limit is applied.
    Truncating during the walk and sorting the remnant would put "newest first"
    on a list whose newest entries were never scanned — a lie that looks exactly
    like the truth.
    """
    project_root = root()
    limit = max(1, min(int(limit), 2000))
    needle = (q or "").strip().lower()
    skip = {".git", ".godot", "__pycache__", ".bgate_out", ".bgate",
            "node_modules", ".asset_work", "export", "build"}
    found = []
    scanned = 0
    for path in project_root.rglob("*"):
        if scanned >= SCAN_CAP:
            break
        if path.suffix.lower() not in EDITABLE or not path.is_file():
            continue
        if skip & set(path.parts):
            continue
        scanned += 1
        rel = path.relative_to(project_root).as_posix()
        if needle and needle not in rel.lower():
            continue
        try:
            stat = path.stat()
            with Image.open(path) as im:
                size = im.size
        except (OSError, ValueError):
            continue
        found.append({
            "rel": rel,
            "name": path.name,
            "width": size[0], "height": size[1],
            "bytes": stat.st_size, "mtime": int(stat.st_mtime),
            "rigged": rigmap.sidecar_path(path).is_file(),
            "kind": _kind(rel),
        })
    found.sort(key=lambda d: d["mtime"], reverse=True)
    kinds: dict = {}
    for row in found:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return {"sheets": found[:limit], "count": len(found[:limit]),
            "total": len(found), "truncated": len(found) > limit,
            # The counts are over EVERYTHING found, not over the truncated page:
            # a picker that says "art 412" while showing a 2000-row slice of a
            # 2837-row set would be describing a list nobody is looking at.
            "kinds": kinds, "query": needle}


@router.post("/api/sprite/save")
def sprite_save(payload: dict) -> dict:
    """Write the edited pixels back, after keeping a copy of the old ones.

    The dimension check is the load-bearing part. A sheet's geometry is a
    contract with its SpriteFrames regions, its gear anchors, and every aligned
    gear sheet drawn against it — so a save that changes the size is refused,
    not "handled".
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _sheet(rel)

    raw = str(payload.get("png") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise api.bad_request("no image data in the payload")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise api.ApiError(413, "image payload too large",
                           detail={"bytes": len(raw), "max": MAX_UPLOAD_BYTES})
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api.bad_request(f"image data is not valid base64: {exc}")

    try:
        incoming = Image.open(io.BytesIO(blob))
        incoming.load()
    except Exception as exc:
        raise api.bad_request(f"payload is not a readable image: {exc}")
    if incoming.width * incoming.height > MAX_PIXELS:
        raise api.ApiError(413, "image is too large to accept",
                           detail={"pixels": incoming.width * incoming.height})

    with Image.open(target) as current:
        old_size = current.size
    if incoming.size != old_size:
        raise api.conflict(
            "a save may not resize the sheet — every SpriteFrames region and "
            "gear anchor is expressed in these exact dimensions",
            was=list(old_size), got=list(incoming.size))

    # Optimistic concurrency: the editor sends the mtime it opened. Something
    # else touching the file mid-edit (the generator, Godot's importer, another
    # seat) must not be silently overwritten.
    expect = payload.get("mtime")
    if expect is not None and int(expect) != int(target.stat().st_mtime):
        raise api.conflict("the sheet changed on disk since you opened it",
                           rel=rel, on_disk=int(target.stat().st_mtime),
                           expected=int(expect))

    bdir = _backup_dir(project_root)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = bdir / f"{target.stem}.{stamp}{target.suffix}"
    shutil.copy2(target, backup)

    tmp = target.with_suffix(target.suffix + ".tmp")
    incoming.convert("RGBA").save(tmp, format=target.suffix.lstrip(".").upper()
                                  if target.suffix.lower() != ".png" else "PNG")
    os.replace(tmp, target)

    # Best-effort bookkeeping: a project without an initialised database is
    # still a project someone can paint in.
    tracked = None
    try:
        from bgate_core.store import assets
        tracked = assets.track(project_root, target)
    except Exception:
        tracked = None

    return api.ok({
        "rel": rel,
        "backup": backup.relative_to(project_root).as_posix(),
        "mtime": int(target.stat().st_mtime),
        "bytes": target.stat().st_size,
        "tracked": bool(tracked),
    })


@router.get("/api/sprite/regen/status")
def sprite_regen_status() -> dict:
    """Can frames be regenerated at all, and what does one cost?"""
    from bgate_adapters import imagegen
    probe = imagegen.available()
    return {
        "available": bool(probe.get("available")),
        "reason": probe.get("reason", ""),
        "qualities": list(imagegen.QUALITIES),
        "price_usd": dict(imagegen.IMAGE_PRICE_USD),
        "max_frames": MAX_REGEN_FRAMES,
        "upscale": REGEN_EDGE,
    }


@router.post("/api/sprite/regen")
def sprite_regen(payload: dict) -> dict:
    """Repaint ONE frame from a prompt, and hand the pixels back unwritten.

    The whole point of a frame-level regen is that the rest of the sheet is
    already right. So the request is scoped to a single cell and the response
    is a PNG the editor composites into its own canvas — the file on disk is
    not touched, the change lands in the editor's undo stack, and the existing
    save path (backup + mtime check) is still the only thing that writes.

    One frame per call, matching imagegen.edit's own contract: multi-pose
    generations are where character identity dies. The editor loops.

    Geometry. A 96x80 cell is far below what the model works at, so the cell is
    upscaled NEAREST to a square, edited, and brought back with an area
    downscale plus an alpha threshold — area-averaging is right for a ~10x
    reduction, and the threshold restores the crisp silhouette edge that
    averaging softens. It is a re-rasterisation, not a pixel-exact edit, and
    the editor says so before spending anything.

    The whole sheet rides along as a second reference. That is the consistency
    primitive the sprite pipeline already uses — without it the model repaints
    a different character into frame 7.
    """
    from bgate_adapters import imagegen

    rel = str(payload.get("rel") or "")
    project_root, target = _sheet(rel)

    probe = imagegen.available()
    if not probe.get("available"):
        raise api.unavailable(probe.get("reason") or "image generation is off")

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise api.bad_request("a regeneration needs a prompt saying what to change")
    if len(prompt) > 3000:
        raise api.bad_request("prompt is too long", chars=len(prompt))

    quality = str(payload.get("quality") or "medium")
    if quality not in imagegen.QUALITIES:
        raise api.bad_request(f"quality must be one of {imagegen.QUALITIES}",
                              got=quality)

    try:
        grid = rigmap.normalise({"grid": payload.get("grid")}).get("grid")
    except rigmap.RigError as exc:
        raise api.bad_request(str(exc), rel=rel)
    if not grid:
        grid = rigmap.load(target).get("grid")

    with Image.open(target) as im:
        sheet = im.convert("RGBA")
    if not grid:
        grid = {"cell_w": sheet.width, "cell_h": sheet.height,
                "cols": 1, "rows": 1}
    if grid["cell_w"] * grid["cols"] != sheet.width or \
            grid["cell_h"] * grid["rows"] != sheet.height:
        raise api.bad_request(
            "the grid does not tile this sheet — set it in the editor first",
            grid=grid, sheet=[sheet.width, sheet.height])

    cells = grid["cols"] * grid["rows"]
    frame = payload.get("frame")
    try:
        frame = int(frame)
    except (TypeError, ValueError):
        raise api.bad_request("frame must be an integer", got=payload.get("frame"))
    if not 0 <= frame < cells:
        raise api.bad_request(f"frame {frame} is outside this sheet's {cells} cells")

    row, col = divmod(frame, grid["cols"])
    box = (col * grid["cell_w"], row * grid["cell_h"],
           (col + 1) * grid["cell_w"], (row + 1) * grid["cell_h"])
    cell = sheet.crop(box)

    work = project_root / ".bgate_out" / "sprite_regen"
    work.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cell_ref = work / f"{target.stem}.f{frame}.{stamp}.in.png"
    sheet_ref = work / f"{target.stem}.{stamp}.sheet.png"
    out_png = work / f"{target.stem}.f{frame}.{stamp}.out.png"
    cell.resize((REGEN_EDGE, REGEN_EDGE), Image.Resampling.NEAREST).save(cell_ref)
    sheet.save(sheet_ref)

    framed = (
        f"{prompt}\n\n"
        "This is ONE frame of a sprite sheet, shown enlarged. Keep the same "
        "character, the same palette, the same art style, the same scale and "
        "the same footing as the reference sheet. Fill the frame the same way "
        "the original does. Transparent background, no added border, no "
        "caption, no second pose."
    )
    # allow_multi is set because the multi-pose guard exists to stop someone
    # asking ONE image to be a whole sheet. That is structurally impossible
    # here — the input is a single cell and the output is written back into
    # that cell — and without it a perfectly good prompt like "match the other
    # animation frames" is refused for containing the word "frames".
    result = imagegen.edit(
        framed, [str(cell_ref), str(sheet_ref)], str(out_png),
        size="1024x1024", quality=quality, transparent=True, allow_multi=True)
    if not result.get("ok"):
        raise api.unavailable(result.get("error") or "the image edit failed",
                              rel=rel, frame=frame)

    with Image.open(out_png) as raw:
        edited = raw.convert("RGBA").resize(
            (grid["cell_w"], grid["cell_h"]), Image.Resampling.BOX)
    # Area-averaging a 10x reduction leaves a soft alpha ramp all round the
    # silhouette. On a sprite sheet that ramp is the "halo" the de-halo tool
    # exists to remove, so it is cut here rather than shipped and complained
    # about later.
    alpha = edited.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    edited.putalpha(alpha)

    buf = io.BytesIO()
    edited.save(buf, format="PNG")
    return api.ok({
        "rel": rel, "frame": frame,
        "png": base64.b64encode(buf.getvalue()).decode(),
        "size": [grid["cell_w"], grid["cell_h"]],
        "seconds": result.get("seconds"),
        "usd": result.get("usd"),
        "model": result.get("model"),
        "revised_prompt": result.get("revised_prompt"),
        # Nothing was written to the sheet. Said explicitly because "the API
        # returned 200" is otherwise indistinguishable from "it saved".
        "written": False,
    })


@router.post("/api/sprite/rig")
def sprite_rig(payload: dict) -> dict:
    """Save the rig sidecar — the grid, the animations, the slot anchors."""
    rel = str(payload.get("rel") or "")
    project_root, target = _sheet(rel)
    with Image.open(target) as im:
        size = im.size
    try:
        saved = rigmap.save(target, payload.get("rig") or {}, sheet_size=size)
    except rigmap.RigError as exc:
        raise api.bad_request(str(exc), rel=rel)
    return api.ok({"rel": rel, "rig": saved, "coverage": rigmap.coverage(saved),
                   "sidecar": rigmap.sidecar_path(target)
                   .relative_to(project_root).as_posix()})


@router.post("/api/sprite/autogrid")
def sprite_autogrid(payload: dict) -> dict:
    """Detect a cell lattice, or validate one the artist typed in.

    Not saved — the editor previews the slicing first, because a wrong grid is
    obvious on screen and invisible in a number.
    """
    rel = str(payload.get("rel") or "")
    _, target = _sheet(rel)
    with Image.open(target) as im:
        size = im.size
        rgba = im.convert("RGBA")
        cw, ch = payload.get("cell_w"), payload.get("cell_h")
        if cw and ch:
            try:
                grid = rigmap.autoslice(size, (cw, ch))
            except rigmap.RigError as exc:
                raise api.bad_request(str(exc), rel=rel)
        else:
            from bgate_core.art.gear import detect_grid
            g = detect_grid(rgba)
            grid = {"cell_w": g.cell_w, "cell_h": g.cell_h,
                    "cols": g.cols, "rows": g.rows}
    return api.ok({"rel": rel, "grid": grid,
                   "frames": grid["cols"] * grid["rows"],
                   "suggested_animations": rigmap.rows_as_animations(grid, [])})


@router.post("/api/sprite/spriteframes")
def sprite_spriteframes(payload: dict) -> dict:
    """Export the labelled animations as a Godot 4 SpriteFrames resource.

    Written next to the sheet by default, because that is where Godot's
    importer and every existing .tres in these projects already live.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _sheet(rel)
    with Image.open(target) as im:
        size = im.size
    data = rigmap.load(target)
    if payload.get("rig"):
        try:
            data = rigmap.normalise(payload["rig"], sheet_size=size)
        except rigmap.RigError as exc:
            raise api.bad_request(str(exc), rel=rel)

    res_path = _res_path(project_root, target)
    if not res_path:
        raise api.bad_request(
            "this sheet is not inside the Godot project, so it has no res:// "
            "path to reference", rel=rel)
    res_dir = str(Path(res_path[len("res://"):]).parent.as_posix())
    try:
        text = rigmap.spriteframes_text(data, target.name, res_dir, sheet_size=size)
    except rigmap.RigError as exc:
        raise api.bad_request(str(exc), rel=rel)

    out = target.with_suffix("").with_name(target.stem + "_frames.tres")
    if payload.get("dry_run"):
        return api.ok({"rel": rel, "dry_run": True, "text": text,
                       "would_write": out.relative_to(project_root).as_posix()})
    if out.is_file():
        bdir = _backup_dir(project_root)
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, bdir / f"{out.stem}.{time.strftime('%Y%m%d-%H%M%S')}.tres")
    out.write_text(text, encoding="utf-8", newline="\n")
    return api.ok({"rel": rel,
                   "written": out.relative_to(project_root).as_posix(),
                   "animations": [a["name"] for a in data["animations"]]})
