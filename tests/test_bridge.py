"""The Blender -> Godot round trip, end to end, against BOTH real tools.

This is the spine of "make solid games": an agent models in Blender, exports glTF,
and the asset shows up usable in Godot. Every assertion here is about what the
ENGINE actually loaded, not what a file claims — because "the .glb exists" and
"Godot built a mesh from it" are different facts, and only the second one ships.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bgate_adapters import blender, godot
from bgate_core import scaffold

both = pytest.mark.skipif(
    not (blender.available()["available"] and godot.available()["available"]),
    reason="needs both Blender and Godot",
)

# A shard with a bevel MODIFIER — the modifier is the point. If export doesn't
# apply it, Godot receives the un-beveled base and the tri count gives it away.
SHARD = """
import bpy, math
for o in list(bpy.context.scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.mesh.primitive_cone_add(vertices=6, radius1=0.6, depth=2.0)
ob = bpy.context.active_object
ob.name = "Shard"
bev = ob.modifiers.new("Bevel", "BEVEL")
bev.width, bev.segments = 0.05, 2
mat = bpy.data.materials.new("Emberglass")
mat.use_nodes = True
ob.data.materials.append(mat)
bpy.ops.object.select_all(action='DESELECT')
ob.select_set(True)
bpy.context.view_layer.objects.active = ob
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')
"""


@pytest.mark.slow
class TestBlenderExport:
    @pytest.mark.skipif(not blender.available()["available"], reason="no Blender")
    def test_exports_a_glb(self, tmp_path):
        glb = tmp_path / "shard.glb"
        got = blender.run_script(SHARD, export_glb=str(glb), out_dir=str(tmp_path))
        assert got["ok"] is True, got.get("error")
        assert got["glb"]["exported"] is True
        assert glb.exists() and glb.stat().st_size > 0
        assert got["glb"]["applied_modifiers"] is True

    @pytest.mark.skipif(not blender.available()["available"], reason="no Blender")
    def test_reports_game_readiness_issues(self, tmp_path):
        """No UVs / no material must be flagged BEFORE it reaches the engine.

        Built from pydata so the mesh genuinely has no UV layer — most bpy
        primitives (ico_sphere included) auto-create one.
        """
        bare = """
import bpy
mesh = bpy.data.meshes.new("Bare")
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),(1,1,1)], [], [(0,1,2),(1,3,2)])
bpy.context.collection.objects.link(bpy.data.objects.new("Bare", mesh))
"""
        got = blender.run_script(bare, out_dir=str(tmp_path))
        issues = {i["issue"] for i in got["issues"]}
        assert "no_uv" in issues
        assert "no_material" in issues

    @pytest.mark.skipif(not blender.available()["available"], reason="no Blender")
    def test_clean_mesh_has_no_issues(self, tmp_path):
        """A triangulated, unwrapped, textured mesh should pass clean.

        The bare SHARD has a hex n-gon base (correctly flagged) — a real game
        asset triangulates, which is exactly the fix the check recommends.
        """
        clean = SHARD + """
tri = ob.modifiers.new("Tri", "TRIANGULATE")
"""
        got = blender.run_script(clean, out_dir=str(tmp_path))
        shard_issues = [i for i in got["issues"] if i.get("object") == "Shard"]
        assert shard_issues == [], shard_issues


@both
@pytest.mark.slow
class TestRoundTrip:
    def test_blender_to_godot_asset_lands_usable(self, tmp_path):
        # 1. Blender builds + exports.
        glb = tmp_path / "shard.glb"
        exported = blender.run_script(SHARD, export_glb=str(glb), out_dir=str(tmp_path))
        assert exported["glb"]["exported"], exported

        # 2. A real project to import it into.
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="3d")

        # 3. Import + engine verification.
        result = godot.import_asset(str(project), str(glb))
        assert result["ok"] is True, result
        assert result["import"]["ok"], result["import"]

        # 4. The engine actually built a mesh — the fact that matters.
        view = result["engine_view"]
        assert view["ok"] is True
        assert view["total_tris"] > 0, "Godot imported a scene with no geometry"
        shard = next((m for m in view["meshes"] if "Shard" in m["name"]), None)
        assert shard is not None, [m["name"] for m in view["meshes"]]
        assert shard["surfaces"][0]["has_uv"] is True

    def test_modifiers_survive_the_trip(self, tmp_path):
        """The bevel must be in the geometry Godot received.

        A 6-sided cone is 8 verts / ~8 tris raw; a 2-segment bevel multiplies
        that. If export silently dropped modifiers, Godot sees the low count.
        """
        glb = tmp_path / "shard.glb"
        blender.run_script(SHARD, export_glb=str(glb), out_dir=str(tmp_path))
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="3d")

        view = godot.import_asset(str(project), str(glb))["engine_view"]
        assert view["total_tris"] > 20, (
            f"only {view['total_tris']} tris — bevel modifier was dropped on export")

    def test_missing_asset_is_reported(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="3d")
        got = godot.import_asset(str(project), str(tmp_path / "ghost.glb"))
        assert got["ok"] is False
        assert "not found" in got["error"]


@both
@pytest.mark.slow
class TestEngineInspection:
    def test_inspect_reports_missing_resource(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="3d")
        got = godot.inspect_resource(str(project), "res://does_not_exist.glb")
        assert got["ok"] is False
