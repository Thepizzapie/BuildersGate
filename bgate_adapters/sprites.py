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


def from_pose_images(pose_files: list[tuple[str, str]], *, out_dir: str,
                     name: str, frame_size: tuple[int, int] = (160, 240),
                     res_dir: str = "assets/sprites", fps: float = 8.0,
                     min_fill: float = 0.01) -> dict:
    """Assemble individually-generated pose images into the sheet+tres contract.

    The reference-first flow's back half: each pose arrives as its own
    transparent PNG (generated via imagegen.edit against one reference
    character), gets alpha-trimmed, scaled, bottom-centered, and stitched.
    Same output contract as render_sprites / from_painted_sheet.

    pose_files: [(pose_name, png_path)] in animation order. A pose name may be
    "anim/idx" (e.g. "jab/0", "jab/1") — frames sharing the prefix become ONE
    multi-frame animation, ordered by idx. Bare names are 1-frame animations.
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
    for pose, path in pose_files:
        if not Path(path).is_file():
            failed.append({"name": pose, "error": f"file missing: {path}"})
            continue
        img = Image.open(path).convert("RGBA")
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
        scale = min(fw / trimmed.width, fh / trimmed.height)
        resized = trimmed.resize((max(1, int(trimmed.width * scale)),
                                  max(1, int(trimmed.height * scale))),
                                 Image.LANCZOS)
        frame = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        frame.paste(resized, ((fw - resized.width) // 2, fh - resized.height))
        dest = out / f"{name}_{pose.replace('/', '_')}.png"
        frame.save(dest)
        frame_files[pose] = str(dest)
        ordered.append(pose)

    if not frame_files:
        return {"ok": False, "failed": failed, "error": "no usable pose images"}

    sheet_path = out / f"{name}_sheet.png"
    _stitch([frame_files[p] for p in ordered], sheet_path)
    anims = _group_frames(ordered)
    tres_path = out / f"{name}_frames.tres"
    tres_path.write_text(_sprite_frames_tres(f"{name}_sheet.png", anims,
                                             frame_size, fps, res_dir),
                         encoding="utf-8")
    return {"ok": True, "frames": frame_files, "sheet": str(sheet_path),
            "tres": str(tres_path), "size": list(frame_size),
            "animations": {a: c for a, c in anims}, "failed": failed}


def _stitch(paths: list[str], out_path: Path) -> None:
    """Horizontal strip, frame order preserved — regions are index * width."""
    from PIL import Image

    images = [Image.open(p).convert("RGBA") for p in paths]
    w, h = images[0].size
    sheet = Image.new("RGBA", (w * len(images), h), (0, 0, 0, 0))
    for i, img in enumerate(images):
        sheet.paste(img, (i * w, 0))
    sheet.save(out_path)


def _sprite_frames_tres(sheet_filename: str, anims: list[tuple[str, int]],
                        size: tuple[int, int], fps: float, res_dir: str) -> str:
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
        blocks.append(
            '{\n"frames": [%s],\n"loop": true,\n"name": &"%s",\n"speed": %s\n}'
            % (frames, anim, fps))
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
