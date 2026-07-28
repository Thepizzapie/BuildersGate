"""Runs INSIDE Blender: render a character as transparent sprite frames.

argv after '--': <job_json> <result_json>

job = {
  "base_script":  bpy source that builds the character (and optionally a camera),
  "poses":        [{"name": "idle", "script": "..."}, ...]   per-pose bpy tweaks,
  "size":         [w, h] render resolution per frame,
  "out_dir":      where pose PNGs land,
  "engine":       render engine,
}

Contract mirrors _blender_runner: always write a result file, report per-pose
outcomes, and let a broken pose script fail THAT pose without killing the rest —
an artist iterating on the hook pose should still get the other seven frames.
"""
import json
import sys
import traceback

import bpy


def _ensure_camera():
    """If the base script made no camera, frame the scene with a default ortho.

    Sprites want orthographic projection: perspective distorts limbs at the
    frame edges and the silhouette changes as things move — death for sprite
    consistency.
    """
    scene = bpy.context.scene
    if scene.camera:
        return "script"
    cam_data = bpy.data.cameras.new("SpriteCam")
    cam_data.type = "ORTHO"

    # Frame everything visible with a small margin.
    xs, ys, zs = [], [], []
    for obj in scene.objects:
        if obj.type in ("CAMERA", "LIGHT"):
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ __import__("mathutils").Vector(corner)
            xs.append(world.x)
            ys.append(world.y)
            zs.append(world.z)
    if not xs:
        xs, ys, zs = [-1, 1], [-1, 1], [-1, 1]
    cx, cz = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2
    span = max(max(xs) - min(xs), max(zs) - min(zs)) or 2.0
    cam_data.ortho_scale = span * 1.15

    cam = bpy.data.objects.new("SpriteCam", cam_data)
    cam.location = (cx, min(ys) - span * 2.0, cz)
    cam.rotation_euler = (1.5707963, 0.0, 0.0)  # face +Y
    bpy.context.collection.objects.link(cam)
    scene.camera = cam
    return "auto"


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    job = json.loads(open(argv[0], encoding="utf-8").read())
    result = {"ok": False, "frames": [], "error": None}

    try:
        namespace = {"__name__": "__main__", "bpy": bpy}
        exec(compile(job["base_script"], "<base_script>", "exec"), namespace)

        scene = bpy.context.scene
        scene.render.engine = job.get("engine", "BLENDER_EEVEE_NEXT")
        scene.render.film_transparent = True  # the whole point: sprite alpha
        scene.render.resolution_x = job["size"][0]
        scene.render.resolution_y = job["size"][1]
        scene.render.image_settings.file_format = "PNG"
        scene.render.image_settings.color_mode = "RGBA"
        result["camera"] = _ensure_camera()

        import os
        os.makedirs(job["out_dir"], exist_ok=True)
        for pose in job["poses"]:
            entry = {"name": pose["name"], "ok": False}
            try:
                exec(compile(pose.get("script", "pass"), f"<pose:{pose['name']}>", "exec"),
                     namespace)
                path = os.path.join(job["out_dir"],
                                    f"{pose['name'].replace('/', '_')}.png")
                scene.render.filepath = path
                bpy.ops.render.render(write_still=True)
                entry.update(ok=os.path.exists(path), path=path)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["traceback"] = traceback.format_exc(limit=4)
            result["frames"].append(entry)

        result["ok"] = any(f["ok"] for f in result["frames"])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc(limit=6)

    with open(argv[1], "w", encoding="utf-8") as fh:
        json.dump(result, fh)


main()
