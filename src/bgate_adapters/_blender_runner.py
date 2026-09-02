"""Runs INSIDE Blender's Python. Not imported by the rest of Builders Gate.

Contract: exec the agent's script, then report the scene as structured facts.
A crash in the agent's script must still produce a result file — a silent
non-zero exit tells the agent nothing about what broke.

argv after '--': <script_path> <result_path> <render_path|-> <engine> <glb_path|->
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


def _game_readiness(depsgraph):
    """The asset problems that only surface once it's in the engine.

    Each of these is cheap here and expensive later: a mesh with no UVs cannot be
    textured, a non-uniform scale shears every child, an off-origin mesh rotates
    around empty air, and n-gons triangulate unpredictably across exporters.
    """
    import bpy

    issues = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        name = obj.name

        scale = tuple(round(v, 4) for v in obj.scale)
        if len({round(v, 3) for v in scale}) > 1:
            issues.append({"object": name, "issue": "non_uniform_scale",
                           "detail": f"scale {scale} shears children and normals",
                           "fix": "apply scale (Ctrl+A) before export"})
        elif any(abs(v - 1.0) > 1e-3 for v in scale):
            issues.append({"object": name, "issue": "unapplied_scale",
                           "detail": f"scale {scale} is baked into the object, not the mesh",
                           "fix": "apply scale so the engine sees true dimensions"})

        try:
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
        except Exception:
            continue
        try:
            if not mesh.uv_layers:
                issues.append({"object": name, "issue": "no_uv",
                               "detail": "cannot be textured",
                               "fix": "unwrap (smart_project in EDIT mode)"})
            ngons = sum(1 for p in mesh.polygons if len(p.vertices) > 4)
            if ngons:
                issues.append({"object": name, "issue": "ngons", "count": ngons,
                               "detail": "n-gons triangulate unpredictably per exporter",
                               "fix": "triangulate or use quads"})
            if not obj.material_slots:
                issues.append({"object": name, "issue": "no_material",
                               "detail": "imports with a default grey material",
                               "fix": "assign a material before export"})
        finally:
            try:
                evaluated.to_mesh_clear()
            except Exception:
                pass
    return issues


def _export_flags():
    """What THIS scene needs from the exporter. Measured, not assumed."""
    import bpy

    shaped, actions, armatures = [], [], []
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            armatures.append(obj.name)
        elif obj.type == "MESH":
            keys = getattr(obj.data, "shape_keys", None)
            # A lone "Basis" is not a blend shape, it is the rest state.
            if keys is not None and len(keys.key_blocks) > 1:
                shaped.append(obj.name)
    for action in bpy.data.actions:
        actions.append(action.name)
    return {"shape_keys": shaped, "armatures": armatures, "actions": actions}


def _apply_modifiers_in_script():
    """Apply non-Armature modifiers ourselves, so export_apply can stay OFF.

    Only called when the scene has shape keys — see _export_glb. Armature
    modifiers are never applied (that would freeze the pose and destroy the
    skin), which is exactly what export_apply does too.
    """
    import bpy

    applied, skipped = [], []
    try:
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

    for obj in list(bpy.context.scene.objects):
        if obj.type != "MESH" or not obj.modifiers:
            continue
        keys = getattr(obj.data, "shape_keys", None)
        shaped = keys is not None and len(keys.key_blocks) > 1
        # Multi-user mesh data cannot be modified in place; give this object
        # its own copy rather than failing or mutating a sibling.
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        for mod in list(obj.modifiers):
            ref = f"{obj.name}/{mod.name}"
            if mod.type == "ARMATURE":
                continue  # never applied, by design
            if shaped:
                # Blender itself refuses this — a modifier cannot be applied to
                # a mesh carrying shape keys. Reported rather than swallowed:
                # the geometry ships at base resolution and the human has to
                # know which of the two we kept.
                skipped.append({"modifier": ref, "reason": "mesh has shape keys",
                                "effect": "exported at base resolution",
                                "fix": "bake the modifier into each shape key, "
                                       "or drop the modifier"})
                continue
            try:
                with bpy.context.temp_override(object=obj, active_object=obj,
                                               selected_objects=[obj],
                                               selected_editable_objects=[obj]):
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                applied.append(ref)
            except Exception as exc:
                skipped.append({"modifier": ref,
                                "reason": f"{type(exc).__name__}: {exc}"})
    return applied, skipped


def _export_glb(path):
    """Export the scene to a single .glb, with game-appropriate settings.

    Modifiers must reach the engine: Blender defaults export_apply to FALSE, so a
    naive export silently ships the BASE mesh — your bevel, subsurf, and mirror
    modifiers simply don't come out the other side. The asset looks right in
    Blender and wrong in the engine, which is a miserable thing to debug.

    BUT export_apply is not free, and its cost was invisible here for a long
    time. Blender's own wording for the flag is "Apply modifiers (excluding
    Armatures) to mesh objects — WARNING: prevents exporting shape keys". So
    with it forced on, blend shapes were structurally impossible on this path:
    no facial expression, no corrective shape, no blink, ever. MEASURED on a
    rigged cylinder with three shape keys and a subsurf — the exported .glb came
    back with `morph_targets: 0` and no targetNames, silently.

    So the flag is now conditional. No shape keys in the scene: unchanged,
    export_apply stays on and every existing export behaves exactly as before.
    Shape keys present: export_apply goes off and we apply the modifiers
    ourselves first, which keeps the modifier intent for every mesh that CAN
    take it and reports the ones that cannot (Blender refuses to apply a
    modifier to a shape-keyed mesh at all — that is its rule, not ours).

    Animations are likewise made explicit rather than left to the default. The
    default happens to be True on 4.5, but "happens to be" is how a character
    ships as a T-pose statue after a version bump.
    """
    import bpy

    flags = _export_flags()
    has_shapes = bool(flags["shape_keys"])
    modifiers = {"applied": [], "skipped": []}
    if has_shapes:
        modifiers["applied"], modifiers["skipped"] = _apply_modifiers_in_script()

    kwargs = {
        "filepath": path,
        "export_format": "GLB",       # single self-contained file
        "export_apply": not has_shapes,   # <- see docstring.
        "export_yup": True,           # Godot is Y-up
        "use_selection": False,
        "export_materials": "EXPORT",
        "export_cameras": False,
        "export_lights": False,
        # Explicit, because a character with no AnimationPlayer and no blend
        # shapes is the exact failure this function is here to prevent.
        "export_animations": bool(flags["actions"]),
        "export_morph": has_shapes,
        "export_skins": bool(flags["armatures"]),
    }
    # Blender renames export flags between versions; drop anything this build
    # doesn't know rather than dying on an unexpected keyword.
    known = set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())
    dropped = sorted(k for k in kwargs if k != "filepath" and k not in known)
    kwargs = {k: v for k, v in kwargs.items() if k in known or k == "filepath"}

    bpy.ops.export_scene.gltf(**kwargs)
    import os
    return {
        "exported": os.path.exists(path),
        "path": path,
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        # True when modifier intent reached the .glb by EITHER route. Callers
        # have asserted on this key since before the shape-key path existed.
        "applied_modifiers": bool(kwargs.get("export_apply")) or bool(
            modifiers["applied"]),
        "export_apply": bool(kwargs.get("export_apply")),
        "modifiers": modifiers,
        "shape_key_meshes": flags["shape_keys"],
        "armatures": flags["armatures"],
        "actions": flags["actions"],
        "exported_animations": bool(kwargs.get("export_animations")),
        "exported_morph_targets": bool(kwargs.get("export_morph")),
        "unsupported_flags": dropped,
    }


def _set_engine(scene, name):
    """Set the engine, trying EEVEE's other spelling before falling back.

    4.2 named the rewrite BLENDER_EEVEE_NEXT; 5.x took BLENDER_EEVEE back. The
    fallback below used to go straight to Workbench on the unknown name, so a
    5.x box silently rendered every EEVEE request in Workbench and reported
    success. Which spelling is real is asked of bpy, not inferred from a
    version string.
    """
    aliases = {"BLENDER_EEVEE_NEXT": "BLENDER_EEVEE",
               "BLENDER_EEVEE": "BLENDER_EEVEE_NEXT"}
    for candidate in (name, aliases.get(name)):
        if not candidate:
            continue
        try:
            scene.render.engine = candidate
            return candidate
        except TypeError:
            continue
    scene.render.engine = "BLENDER_WORKBENCH"
    return "BLENDER_WORKBENCH"


def _render(path, engine):
    scene = bpy.context.scene
    if not scene.camera:
        # Without a camera there is nothing to render; say so rather than
        # failing the whole run — the stats are still worth returning.
        return {"rendered": False, "reason": "scene has no camera"}
    asked = engine
    used = _set_engine(scene, engine)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    out = {"rendered": True, "path": path, "engine": scene.render.engine}
    if used != asked:
        # A downgrade must be stated. Reporting rendered=True under a quietly
        # different engine is how a Workbench frame gets signed off as EEVEE.
        out["requested_engine"] = asked
        out["engine_fallback"] = (
            "%s is not available in this Blender — rendered with %s instead"
            % (asked, used))
    return out


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    script_path, result_path, render_path, engine = argv[0], argv[1], argv[2], argv[3]
    glb_path = argv[4] if len(argv) > 4 else "-"

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

    try:
        result["issues"] = _game_readiness(bpy.context.evaluated_depsgraph_get())
    except Exception as exc:
        result["issues"] = [{"issue": "readiness_check_failed", "detail": str(exc)}]

    if glb_path != "-" and result["ok"]:
        try:
            result["glb"] = _export_glb(glb_path)
        except Exception as exc:
            result["glb"] = {"exported": False,
                             "error": f"{type(exc).__name__}: {exc}"}

    if render_path != "-" and result["ok"]:
        try:
            result["render"] = _render(render_path, engine)
        except Exception as exc:
            result["render"] = {"rendered": False, "reason": f"{type(exc).__name__}: {exc}"}

    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh)


main()
