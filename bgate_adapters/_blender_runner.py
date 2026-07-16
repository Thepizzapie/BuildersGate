"""Runs INSIDE Blender's Python. Not imported by the rest of Builders Gate.

Contract: exec the agent's script, then report the scene as structured facts.
A crash in the agent's script must still produce a result file — a silent
non-zero exit tells the agent nothing about what broke.

argv after '--': <script_path> <result_path> <render_path|-> <engine>
"""
import json
import sys
import traceback

import bpy


def _mesh_stats(obj, depsgraph):
    """Triangle/vert counts off the EVALUATED object, so modifiers count."""
    try:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
    except Exception:
        return {"tris": 0, "verts": 0, "uv_layers": 0, "error": "mesh eval failed"}
    try:
        mesh.calc_loop_triangles()
        return {
            "tris": len(mesh.loop_triangles),
            "verts": len(mesh.vertices),
            "uv_layers": len(mesh.uv_layers),
        }
    finally:
        try:
            evaluated.to_mesh_clear()
        except Exception:
            pass


def _scene_report():
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects, totals = [], {"tris": 0, "verts": 0}
    for obj in bpy.context.scene.objects:
        entry = {
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 4) for v in obj.location],
            "materials": [s.material.name for s in obj.material_slots if s.material],
        }
        if obj.type == "MESH":
            stats = _mesh_stats(obj, depsgraph)
            entry.update(stats)
            totals["tris"] += stats.get("tris", 0)
            totals["verts"] += stats.get("verts", 0)
            if not stats.get("uv_layers"):
                entry["warning"] = "no UV layer — cannot texture this mesh"
        objects.append(entry)

    return {
        "objects": objects,
        "totals": {
            **totals,
            "objects": len(objects),
            "meshes": sum(1 for o in objects if o["type"] == "MESH"),
        },
        "materials": [m.name for m in bpy.data.materials],
        "collections": [c.name for c in bpy.data.collections],
        "frame_range": [bpy.context.scene.frame_start, bpy.context.scene.frame_end],
    }


def _render(path, engine):
    scene = bpy.context.scene
    if not scene.camera:
        # Without a camera there is nothing to render; say so rather than
        # failing the whole run — the stats are still worth returning.
        return {"rendered": False, "reason": "scene has no camera"}
    try:
        scene.render.engine = engine
    except TypeError:
        scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return {"rendered": True, "path": path, "engine": scene.render.engine}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    script_path, result_path, render_path, engine = argv[0], argv[1], argv[2], argv[3]

    result = {"ok": False, "error": None, "traceback": None, "print": ""}

    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    try:
        with open(script_path, encoding="utf-8") as fh:
            code = compile(fh.read(), "<agent_script>", "exec")
        # Give the script a real module namespace so `import bpy` inside it and
        # top-level defs behave the way they would in Blender's text editor.
        namespace = {"__name__": "__main__", "bpy": bpy}
        with redirect_stdout(buffer):
            exec(code, namespace)
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc(limit=6)
    result["print"] = buffer.getvalue()[-4000:]

    # Report the scene even after a failure — partial state is diagnostic.
    try:
        result["scene"] = _scene_report()
    except Exception as exc:
        result["scene"] = {"error": f"scene report failed: {exc}"}

    if render_path != "-" and result["ok"]:
        try:
            result["render"] = _render(render_path, engine)
        except Exception as exc:
            result["render"] = {"rendered": False, "reason": f"{type(exc).__name__}: {exc}"}

    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh)


main()
