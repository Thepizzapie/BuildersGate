"""The 2D sprite factory — Blender model in, engine-ready SpriteFrames out.

Why this exists: a 2D game's art bottleneck is producing CONSISTENT frames.
Hand-drawn sprites drift between poses; a rendered 3D model cannot — the same
rig, camera, and light produce every frame, and changing the material re-skins
the whole set. The pipeline: build once in bpy, render each pose transparent
and orthographic, stitch a sheet with PIL, and emit the Godot SpriteFrames
.tres so gameplay drops it into an AnimatedSprite2D with zero editor work.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import blender

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

RUNNER = Path(__file__).with_name("_blender_sprites.py")


def render_sprites(base_script: str, poses: list[dict], *, out_dir: str,
                   name: str = "sprite", size: tuple[int, int] = (128, 128),
                   engine: str = "BLENDER_EEVEE_NEXT", fps: float = 8.0,
                   res_dir: str = "assets/sprites", timeout: int = 420) -> dict:
    """Render poses -> <name>_sheet.png + <name>_frames.tres + per-pose PNGs.

    base_script  bpy source that builds the character. A camera is optional —
                 without one, an auto-framed ORTHO camera is added (perspective
                 warps silhouettes between poses; sprites need ortho).
    poses        [{"name": "idle", "script": "<bpy tweaks for this pose>"}].
                 A pose script that throws fails ONLY that pose.
    size         per-frame resolution.

    Returns {ok, frames, sheet, tres, failed:[...], seconds}.
    """
    if not poses:
        raise ValueError("no poses — nothing to render")
    names = [p["name"] for p in poses]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate pose names: {names}")

    exe = blender.find_blender()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="bgate_sprites_"))
    frames_dir = tmp / "frames"

    job = {"base_script": base_script, "poses": poses, "size": list(size),
           "out_dir": str(frames_dir), "engine": engine}
    (tmp / "job.json").write_text(json.dumps(job), encoding="utf-8")
    result_path = tmp / "result.json"

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [exe, "--background", "--factory-startup", "--python", str(RUNNER),
             "--", str(tmp / "job.json"), str(result_path)],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Blender timed out after {timeout}s"}
    elapsed = round(time.monotonic() - started, 2)

    if not result_path.exists():
        return {"ok": False, "error": "Blender exited without a result",
                "exit_code": proc.returncode,
                "stderr": (proc.stderr or "")[-1500:], "seconds": elapsed}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        result["seconds"] = elapsed
        return result

    rendered = [f for f in result["frames"] if f["ok"]]
    failed = [f for f in result["frames"] if not f["ok"]]

    sheet_path = out / f"{name}_sheet.png"
    _stitch([f["path"] for f in rendered], sheet_path)

    tres_path = out / f"{name}_frames.tres"
    tres_path.write_text(
        _sprite_frames_tres(f"{name}_sheet.png",
                            _group_frames([f["name"] for f in rendered]),
                            size, fps, res_dir),
        encoding="utf-8")

    # Keep the individual frames next to the sheet for inspection/iteration.
    frame_files = {}
    for frame in rendered:
        dest = out / f"{name}_{frame['name'].replace('/', '_')}.png"
        dest.write_bytes(Path(frame["path"]).read_bytes())
        frame_files[frame["name"]] = str(dest)

    return {
        "ok": True,
        "frames": frame_files,
        "sheet": str(sheet_path),
        "tres": str(tres_path),
        "size": list(size),
        "camera": result.get("camera"),
        "failed": [{"name": f["name"], "error": f.get("error")} for f in failed],
        "seconds": elapsed,
    }


def from_painted_sheet(image_path: str, pose_names: list[str], *, out_dir: str,
                       name: str, frame_size: tuple[int, int] = (160, 240),
                       res_dir: str = "assets/sprites", fps: float = 8.0,
                       min_fill: float = 0.01) -> dict:
    """Slice ONE painted pose-sheet image into engine-ready sprite frames.

    The painted path's consistency trick: an image model can't hold a character
    steady across separate generations, but it has no choice WITHIN one image —
    so the whole pose row is generated as a single transparent PNG and sliced
    here into equal columns (left to right = pose_names order).

    Per cell: alpha-bbox trim, scale to fit frame_size, bottom-center (fighters
    stand on the ground; center-centering makes them float when heights differ).
    Emits the same sheet + SpriteFrames .tres contract as render_sprites, so a
    painted set is a drop-in replacement for a rendered one.

    A cell whose alpha coverage is under min_fill lands in `failed` — the model
    drew fewer poses than asked, and silently shipping an empty frame would make
    a fighter vanish mid-state.
    """
    from PIL import Image

    if not pose_names:
        raise ValueError("no pose names")
    if len(set(pose_names)) != len(pose_names):
        raise ValueError(f"duplicate pose names: {pose_names}")

    src = Image.open(image_path).convert("RGBA")
    n = len(pose_names)
    cell_w = src.width // n
    fw, fh = frame_size

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    frame_files: dict[str, str] = {}
    failed: list[dict] = []
    ordered: list[str] = []
    for i, pose in enumerate(pose_names):
        cell = src.crop((i * cell_w, 0, (i + 1) * cell_w, src.height))
        bbox = cell.getbbox()  # None when fully transparent
        coverage = 0.0
        if bbox:
            trimmed = cell.crop(bbox)
            alpha = trimmed.getchannel("A")
            coverage = sum(1 for a in alpha.getdata() if a > 8) / (trimmed.width * trimmed.height or 1)
        if not bbox or coverage < min_fill:
            failed.append({"name": pose, "error": f"cell {i} is empty "
                           f"(alpha coverage {coverage:.3f}) — the model drew "
                           "fewer/misaligned poses; regenerate with a stricter "
                           "grid instruction"})
            continue

        scale = min(fw / trimmed.width, fh / trimmed.height)
        resized = trimmed.resize((max(1, int(trimmed.width * scale)),
                                  max(1, int(trimmed.height * scale))),
                                 Image.LANCZOS)
        frame = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        frame.paste(resized, ((fw - resized.width) // 2, fh - resized.height))
        dest = out / f"{name}_{pose}.png"
        frame.save(dest)
        frame_files[pose] = str(dest)
        ordered.append(pose)

    if not frame_files:
        return {"ok": False, "failed": failed,
                "error": "every cell was empty — is the source transparent PNG "
                         "actually a pose row?"}

    sheet_path = out / f"{name}_sheet.png"
    _stitch([frame_files[p] for p in ordered], sheet_path)
    tres_path = out / f"{name}_frames.tres"
    tres_path.write_text(_sprite_frames_tres(f"{name}_sheet.png",
                                             _group_frames(ordered),
                                             frame_size, fps, res_dir),
                         encoding="utf-8")
    return {"ok": True, "frames": frame_files, "sheet": str(sheet_path),
            "tres": str(tres_path), "size": list(frame_size), "failed": failed,
            "source": str(image_path)}


def _close_interior_holes(img, max_area: int = 700) -> int:
    """Refill transparent holes ENCLOSED by the sprite (in place).

    gpt-image's transparent-background mode punches white interior regions —
    most visibly the white of the eyes — to full transparency, leaving see-through
    holes in the face. This flood-fills transparency inward from the border to find
    the true exterior, then refills any *enclosed* transparent component under
    `max_area` px with opaque white. Small holes (eyes, sweat, highlights) close;
    large intended negative space (the gap between the legs) is left alone.
    Returns the pixel count filled.
    """
    px = img.load()
    W, H = img.size
    seen = bytearray(W * H)
    st = []
    for x in range(W):
        for y in (0, H - 1):
            if px[x, y][3] <= 16 and not seen[y * W + x]:
                seen[y * W + x] = 1; st.append((x, y))
    for y in range(H):
        for x in (0, W - 1):
            if px[x, y][3] <= 16 and not seen[y * W + x]:
                seen[y * W + x] = 1; st.append((x, y))
    while st:
        x, y = st.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < W and 0 <= ny < H and not seen[ny * W + nx] and px[nx, ny][3] <= 16:
                seen[ny * W + nx] = 1; st.append((nx, ny))
    visited = bytearray(W * H)
    filled = 0
    for y0 in range(H):
        for x0 in range(W):
            i0 = y0 * W + x0
            if px[x0, y0][3] <= 16 and not seen[i0] and not visited[i0]:
                comp = []; s2 = [(x0, y0)]; visited[i0] = 1
                while s2:
                    x, y = s2.pop(); comp.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        j = ny * W + nx
                        if 0 <= nx < W and 0 <= ny < H and px[nx, ny][3] <= 16 \
                                and not seen[j] and not visited[j]:
                            visited[j] = 1; s2.append((nx, ny))
                if len(comp) <= max_area:
                    # Fill with the average color of the OPAQUE ring bordering
                    # the hole, NOT a hardcoded white. The old white-repaint
                    # assumed every punched interior was a white eye; that was
                    # only true for the transparent-gen path. The chroma-key path
                    # (now primary) can leave a dark enclosed hole, and painting
                    # it white is a visible bug. Ring-average is correct for both:
                    # a white eye's ring is light, a dark gap's ring is dark.
                    rs = rg = rb = rn = 0
                    for x, y in comp:
                        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                            if 0 <= nx < W and 0 <= ny < H:
                                pr, pg, pb, pa = px[nx, ny]
                                if pa > 16:
                                    rs += pr; rg += pg; rb += pb; rn += 1
                    fill = ((rs // rn, rg // rn, rb // rn, 255) if rn
                            else (255, 255, 255, 255))
                    for x, y in comp:
                        px[x, y] = fill
                    filled += len(comp)
    return filled


# Animations whose feet LEAVE the floor — these must not be bottom-pinned like a
# grounded pose, or the character "jumps" without ever rising. Frames of these
# anims are lifted along a rise-and-fall arc (0 at the first/last frame, peak in
# the middle), and the whole set is scaled to reserve that headroom at the top.
AIRBORNE = ("jump", "jump_kick", "leap", "hop", "air")


def _arc_lift(idx: int, count: int, peak: int) -> int:
    """Vertical lift (px) for frame `idx` of an `count`-frame airborne anim.

    A half-sine: 0 on the first and last frame (feet planted for the crouch and
    the landing), maximum in the middle (apex, feet highest off the ground).
    """
    if count <= 1 or peak <= 0:
        return 0
    import math as _m
    return int(round(peak * _m.sin(_m.pi * idx / (count - 1))))


def from_pose_images(pose_files: list[tuple[str, str]], *, out_dir: str,
                     name: str, frame_size: tuple[int, int] = (160, 240),
                     res_dir: str = "assets/sprites", fps: float = 8.0,
                     min_fill: float = 0.01, ref_path: str | None = None,
                     airborne: tuple[str, ...] = AIRBORNE, arc: float = 0.22) -> dict:
    """Assemble individually-generated pose images into the sheet+tres contract.

    The reference-first flow's back half: each pose arrives as its own
    transparent PNG (generated via imagegen.edit against one reference
    character), gets alpha-trimmed, scaled, bottom-centered, and stitched.
    Same output contract as render_sprites / from_painted_sheet.

    pose_files: [(pose_name, png_path)] in animation order. A pose name may be
    "anim/idx" (e.g. "jab/0", "jab/1") — frames sharing the prefix become ONE
    multi-frame animation, ordered by idx. Bare names are 1-frame animations.
    ref_path: the approved character reference. When given, per-frame size is
    normalized to ITS visual mass (canon) instead of the batch median — see the
    area-anchor note below. Also returns a `sequence` block flagging height
    jitter within multi-frame animations.
    """
    from PIL import Image

    names = [n for n, _ in pose_files]
    if not names:
        raise ValueError("no poses")
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate pose names: {names}")

    # Frames of one animation must sit contiguously in the sheet (regions are
    # consumed sequentially per animation) — reorder by first appearance of the
    # anim, then by frame index within it.
    first_seen: dict[str, int] = {}
    for n in names:
        first_seen.setdefault(n.split("/", 1)[0], len(first_seen))

    def _order(entry):
        anim, _, idx = entry[0].partition("/")
        return (first_seen[anim], int(idx) if idx.isdigit() else 0)

    pose_files = sorted(pose_files, key=_order)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fw, fh = frame_size

    frame_files: dict[str, str] = {}
    failed: list[dict] = []
    ordered: list[str] = []

    # First pass: trim (+ eye-hole fill) every frame. We do NOT scale yet — the
    # whole set must share ONE scale so the character stays the SAME SIZE across
    # frames. The old per-frame fit-to-box scale grew/shrank him pose to pose
    # (a wider/taller silhouette got a smaller scale): the size-drift bug.
    trimmed_frames: list[tuple[str, "Image.Image"]] = []
    for pose, path in pose_files:
        if not Path(path).is_file():
            failed.append({"name": pose, "error": f"file missing: {path}"})
            continue
        img = Image.open(path).convert("RGBA")
        _close_interior_holes(img)   # gpt-image punches small interior regions
                                     # (eyes) to transparent; refill from the
                                     # surrounding ring color.
        bbox = img.getbbox()
        coverage = 0.0
        if bbox:
            trimmed = img.crop(bbox)
            alpha = trimmed.getchannel("A")
            coverage = sum(1 for a in alpha.getdata() if a > 8) / (trimmed.width * trimmed.height or 1)
        if not bbox or coverage < min_fill:
            failed.append({"name": pose,
                           "error": f"image is (near-)empty (coverage {coverage:.3f}) "
                                    "— transparent generation likely failed"})
            continue
        trimmed_frames.append((pose, trimmed))

    if not trimmed_frames:
        return {"ok": False, "failed": failed, "error": "no usable pose images"}

    # AREA-ANCHORED scale: the model draws the character at inconsistent overall
    # sizes frame to frame, so scaling by the bounding box leaves him visibly
    # bigger/smaller pose to pose. Opaque-pixel AREA is a pose-invariant size
    # anchor — the same character has ~the same amount of ink whether standing,
    # leaning or crouching — so we normalize each frame to a constant sqrt(area)
    # (its visual mass), then fit the whole set to the frame box. Genuine pose
    # height (a lean or crouch is shorter) is preserved; only draw-size drift is
    # removed.
    import statistics as _stats, math as _math

    def _mass(tr):
        return max(1, sum(1 for px in tr.getdata() if px[3] > 60))

    roots = [_math.sqrt(_mass(t)) for _, t in trimmed_frames]
    # ANCHOR the size target to the REFERENCE character's mass when we have it,
    # not to the batch median. The median follows the crowd: if several frames
    # drift large, the median is large and the GOOD frames get upscaled to match
    # the bad ones. The reference is canon — tie size to it so the set converges
    # on the character, not on the batch's central tendency. No ref -> median.
    target = None
    if ref_path and Path(ref_path).is_file():
        try:
            rimg = Image.open(ref_path).convert("RGBA")
            rbb = rimg.getbbox()
            target = _math.sqrt(_mass(rimg.crop(rbb) if rbb else rimg))
        except Exception:
            target = None
    if target is None:
        target = _stats.median(roots)
    # per-frame factor; clamped so a near-empty/odd frame can't explode or vanish.
    factors = [max(0.75, min(1.35, target / r)) for r in roots]
    # Leave a small margin so the character never touches the frame edge (QA
    # caught feet clipped at the bottom row and wide poses bleeding off the
    # sides). Feet sit on a ground line a few px up from the bottom.
    ground = max(3, int(round(fh * 0.035)))
    side = max(2, int(round(fw * 0.02)))
    avail_w = fw - side * 2
    avail_h = fh - ground
    # Reserve headroom for airborne lift so the apex frame can rise off the floor
    # without its head clipping the top edge. Only the sheets that actually carry
    # an airborne anim pay the shrink.
    anim_counts: dict[str, int] = {}
    for pose, _ in trimmed_frames:
        anim_counts.setdefault(pose.split("/", 1)[0], 0)
        anim_counts[pose.split("/", 1)[0]] += 1
    has_air = any(a in airborne for a in anim_counts)
    peak = int(round(fh * arc)) if has_air else 0
    fit_h = avail_h - peak
    norm_w = max(t.width * f for (_, t), f in zip(trimmed_frames, factors))
    norm_h = max(t.height * f for (_, t), f in zip(trimmed_frames, factors))
    base = min(avail_w / norm_w, fit_h / norm_h)
    seen: dict[str, int] = {}
    frame_h: dict[str, int] = {}   # placed height per frame — feeds the seq check
    for (pose, trimmed), f in zip(trimmed_frames, factors):
        s = base * f
        rw = max(1, int(round(trimmed.width * s)))
        rh = max(1, int(round(trimmed.height * s)))
        resized = trimmed.resize((rw, rh), Image.LANCZOS)
        anim = pose.split("/", 1)[0]
        idx = seen.get(anim, 0); seen[anim] = idx + 1
        lift = _arc_lift(idx, anim_counts[anim], peak) if anim in airborne else 0
        frame = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        frame.paste(resized, ((fw - rw) // 2, avail_h - rh - lift))  # ground line, minus airborne lift
        dest = out / f"{name}_{pose.replace('/', '_')}.png"
        frame.save(dest)
        frame_files[pose] = str(dest)
        frame_h[pose] = rh
        ordered.append(pose)

    sheet_path = out / f"{name}_sheet.png"
    _stitch([frame_files[p] for p in ordered], sheet_path)
    anims = _group_frames(ordered)
    tres_path = out / f"{name}_frames.tres"
    tres_path.write_text(_sprite_frames_tres(f"{name}_sheet.png", anims,
                                             frame_size, fps, res_dir),
                         encoding="utf-8")
    return {"ok": True, "frames": frame_files, "sheet": str(sheet_path),
            "tres": str(tres_path), "size": list(frame_size),
            "animations": {a: c for a, c in anims}, "failed": failed,
            "sequence": _sequence_flags(ordered, frame_h, airborne)}


def _sequence_flags(ordered: list[str], frame_h: dict[str, int],
                    airborne: tuple[str, ...], jump_frac: float = 0.18) -> dict:
    """Advisory motion-jitter check for multi-frame animations.

    Frames are horizontally centered by construction, so left/right drift can't
    survive assembly — the artifact that DOES survive is drawn HEIGHT popping
    between adjacent frames of one animation (the character growing/shrinking
    mid-swing). The area-anchor reduces this but genuine identity drift on a
    single frame still shows as a height spike. Report, don't gate: some anims
    legitimately change height, and airborne ones are excluded outright (their
    height changes are the arc). A flag means 'a human/vision pass should look',
    not 'reject' — same contract as the palette tripwire.
    """
    by_anim: dict[str, list[tuple[int, str]]] = {}
    for pose in ordered:
        anim, _, idx = pose.partition("/")
        by_anim.setdefault(anim, []).append((int(idx) if idx.isdigit() else 0, pose))
    flags = []
    for anim, items in by_anim.items():
        if len(items) < 2 or anim in airborne:
            continue
        items.sort()
        hs = [frame_h[p] for _, p in items]
        jump = max(abs(a - b) / max(a, b, 1) for a, b in zip(hs, hs[1:]))
        if jump > jump_frac:
            flags.append({"anim": anim, "max_adjacent_height_jump": round(jump, 3),
                          "height_range": [min(hs), max(hs)]})
    return {"ok": True, "flags": flags,
            "flagged": [f["anim"] for f in flags]}


def _stitch(paths: list[str], out_path: Path) -> None:
    """Horizontal strip, frame order preserved — regions are index * width."""
    from PIL import Image

    images = [Image.open(p).convert("RGBA") for p in paths]
    w, h = images[0].size
    sheet = Image.new("RGBA", (w * len(images), h), (0, 0, 0, 0))
    for i, img in enumerate(images):
        sheet.paste(img, (i * w, 0))
    sheet.save(out_path)


# Animations that must play ONCE and hold their last frame — a looping fall
# would knock the fighter down forever. Applied by name in every emitter.
NO_LOOP = ("ko", "death", "fall", "intro", "victory")


def _sprite_frames_tres(sheet_filename: str, anims: list[tuple[str, int]],
                        size: tuple[int, int], fps: float, res_dir: str,
                        no_loop: tuple[str, ...] = NO_LOOP) -> str:
    """A Godot 4 SpriteFrames resource over a horizontal strip sheet.

    anims: [(animation_name, frame_count)] in sheet order — regions are
    consumed sequentially, so a 2-frame walk after a 1-frame idle occupies
    regions 1 and 2. Multi-frame animations are what make motion feel sharp:
    AnimatedSprite2D cycles the frames at `fps` natively, no code needed.

    res_dir is where the pair will live INSIDE the game project
    (res://<res_dir>/<sheet>), so import them together to that folder.
    """
    w, h = size
    res_dir = res_dir.strip("/").replace("\\", "/")
    total = sum(count for _, count in anims)
    lines = [
        f'[gd_resource type="SpriteFrames" load_steps={total + 2} format=3]',
        "",
        f'[ext_resource type="Texture2D" path="res://{res_dir}/{sheet_filename}" id="1"]',
        "",
    ]
    for i in range(total):
        lines += [
            f'[sub_resource type="AtlasTexture" id="atlas_{i}"]',
            'atlas = ExtResource("1")',
            f"region = Rect2({i * w}, 0, {w}, {h})",
            "",
        ]
    blocks = []
    index = 0
    for anim, count in anims:
        frames = ", ".join(
            '{\n"duration": 1.0,\n"texture": SubResource("atlas_%d")\n}' % (index + f)
            for f in range(count))
        loop = "false" if anim in no_loop else "true"
        blocks.append(
            '{\n"frames": [%s],\n"loop": %s,\n"name": &"%s",\n"speed": %s\n}'
            % (frames, loop, anim, fps))
        index += count
    lines += ["[resource]", "animations = [" + ", ".join(blocks) + "]", ""]
    return "\n".join(lines)


def _group_frames(names: list[str]) -> list[tuple[str, int]]:
    """Group frame names into (animation, count) preserving first-appearance
    order. "jab/0", "jab/1" -> ("jab", 2); a bare "idle" is a 1-frame anim.
    """
    order: list[str] = []
    counts: dict[str, int] = {}
    for name in names:
        anim = name.split("/", 1)[0]
        if anim not in counts:
            order.append(anim)
            counts[anim] = 0
        counts[anim] += 1
    return [(anim, counts[anim]) for anim in order]
