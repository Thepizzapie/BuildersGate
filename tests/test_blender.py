"""Blender adapter, exercised against REAL Blender.

Mocking the subprocess here would test nothing worth testing — the whole risk in
this adapter is the boundary: does bpy actually run, do stats come back, does a
broken script report its traceback instead of vanishing. Skipped when Blender
isn't installed rather than faked.
"""
from __future__ import annotations

import pytest

from bgate_adapters import blender

pytestmark = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed"
)

CUBE = """
import bpy
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
cube = bpy.context.active_object
cube.name = "TestCube"
print("built", cube.name)
"""


class TestDiscovery:
    def test_finds_blender(self):
        assert blender.available()["available"] is True

    def test_reports_version(self):
        assert "Blender" in blender.version()["version"]

    def test_bad_override_is_explicit(self, monkeypatch):
        monkeypatch.setenv("BGATE_BLENDER", r"C:\nope\blender.exe")
        with pytest.raises(blender.BlenderNotFound, match="missing file"):
            blender.find_blender()


class TestRunScript:
    def test_empty_scene_has_no_default_cube(self, tmp_path):
        got = blender.run_script("pass", out_dir=str(tmp_path))
        assert got["ok"] is True
        # --factory-startup DOES load the default scene; the point is that it's
        # the known baseline (Cube/Camera/Light), not a user's customized one.
        assert {o["name"] for o in got["scene"]["objects"]} == {"Cube", "Camera", "Light"}

    def test_builds_geometry_and_reports_stats(self, tmp_path):
        got = blender.run_script(CUBE, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert "built TestCube" in got["print"]

        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["type"] == "MESH"
        assert cube["tris"] == 12  # a cube is 6 quads = 12 tris
        assert cube["verts"] == 8

    def test_totals_aggregate_across_meshes(self, tmp_path):
        script = CUBE + """
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 0, 0))
bpy.context.active_object.name = "Second"
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        # Default Cube + TestCube + Second = 3 meshes, 12 tris each.
        assert got["scene"]["totals"]["meshes"] == 3
        assert got["scene"]["totals"]["tris"] == 36

    def test_modifiers_are_evaluated_not_ignored(self, tmp_path):
        """Stats come from the evaluated mesh — a subsurf must change the count."""
        script = CUBE + """
mod = cube.modifiers.new("Subsurf", "SUBSURF")
mod.levels = 2
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["tris"] > 12

    def test_missing_uv_is_warned(self, tmp_path):
        script = """
import bpy
mesh = bpy.data.meshes.new("Bare")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
bpy.context.collection.objects.link(bpy.data.objects.new("Bare", mesh))
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        bare = next(o for o in got["scene"]["objects"] if o["name"] == "Bare")
        assert bare["uv_layers"] == 0
        assert "UV" in bare["warning"]

    def test_materials_are_listed(self, tmp_path):
        script = CUBE + """
mat = bpy.data.materials.new("Emberglass")
cube.data.materials.append(mat)
"""
        got = blender.run_script(script, out_dir=str(tmp_path))
        assert "Emberglass" in got["scene"]["materials"]
        cube = next(o for o in got["scene"]["objects"] if o["name"] == "TestCube")
        assert cube["materials"] == ["Emberglass"]


class TestFailures:
    def test_syntax_error_returns_payload_not_exception(self, tmp_path):
        got = blender.run_script("def (((:", out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "SyntaxError" in got["error"]

    def test_undefined_name_is_reported(self, tmp_path):
        got = blender.run_script("cube.location = (1, 2, 3)", out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "NameError" in got["error"]

    def test_runtime_error_reports_scene_anyway(self, tmp_path):
        got = blender.run_script(CUBE + "\nraise ValueError('boom')",
                                 out_dir=str(tmp_path))
        assert got["ok"] is False
        assert "boom" in got["error"]
        assert "ValueError" in got["traceback"]
        # Partial state is diagnostic — the cube it built before dying is visible.
        assert any(o["name"] == "TestCube" for o in got["scene"]["objects"])

    def test_missing_blend_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            blender.run_script("pass", blend_file=str(tmp_path / "ghost.blend"))

    def test_bad_engine_rejected(self):
        with pytest.raises(ValueError, match="engine"):
            blender.run_script("pass", engine="UNREAL")


class TestRender:
    def test_render_writes_a_png(self, tmp_path):
        script = CUBE + """
bpy.context.scene.render.resolution_x = 64
bpy.context.scene.render.resolution_y = 64
"""
        got = blender.run_script(script, render=True, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["render"]["rendered"] is True

        png = tmp_path / "render.png"
        assert png.exists() and png.stat().st_size > 0
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_without_camera_explains_itself(self, tmp_path):
        script = """
import bpy
for obj in list(bpy.context.scene.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
"""
        got = blender.run_script(script, render=True, out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["render"]["rendered"] is False
        assert "camera" in got["render"]["reason"]


class TestStdinIsolation:
    """Regression guard for a bug that cost an hour to find.

    Under a stdio MCP server, the server's stdin IS the client's protocol
    channel. A Blender that inherits it blocks forever at ~0% CPU — which looks
    exactly like a slow render and gets misdiagnosed as a GPU stall. Nothing in
    a normal test run catches this, because standalone stdin is a terminal.
    """

    def test_spawn_always_detaches_stdin(self, monkeypatch):
        captured = {}

        def spy(cmd, **kwargs):
            captured.update(kwargs)
            return blender.subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(blender.subprocess, "run", spy)
        blender._spawn(["x"], timeout=1)
        assert captured["stdin"] is blender.subprocess.DEVNULL

    def test_run_script_does_not_inherit_stdin(self, tmp_path, monkeypatch):
        captured = {}

        def spy(cmd, **kwargs):
            captured.update(kwargs)
            raise blender.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(blender.subprocess, "run", spy)
        blender.run_script("pass", out_dir=str(tmp_path), timeout=1)
        assert captured["stdin"] is blender.subprocess.DEVNULL


class TestWarmup:
    def test_warmup_renders_and_reports(self, tmp_path):
        got = blender.warmup("BLENDER_EEVEE_NEXT", out_dir=str(tmp_path))
        assert got["ok"] is True
        assert got["warmed"] is True

    def test_workbench_needs_no_warmup(self, tmp_path):
        got = blender.warmup("BLENDER_WORKBENCH", out_dir=str(tmp_path))
        assert got["warmed"] is False
        assert "no warmup" in got["reason"]

    def test_cold_gpu_render_gets_a_generous_timeout(self, tmp_path, monkeypatch):
        """A caller's small timeout must not cause a bogus cold-start failure."""
        monkeypatch.setattr(blender, "_warmed", set())
        captured = {}

        def spy(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise blender.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(blender.subprocess, "run", spy)
        got = blender.run_script("pass", render=True, engine="BLENDER_EEVEE_NEXT",
                                 timeout=5, out_dir=str(tmp_path))
        assert captured["timeout"] == blender.COLD_START_TIMEOUT
        assert "shader warmup" in got["hint"]


class TestSceneStats:
    def test_reads_a_saved_blend(self, tmp_path):
        blend = tmp_path / "saved.blend"
        blender.run_script(CUBE + f"\nbpy.ops.wm.save_as_mainfile(filepath=r'{blend}')",
                           out_dir=str(tmp_path))
        assert blend.exists()

        got = blender.scene_stats(str(blend))
        assert got["ok"] is True
        assert any(o["name"] == "TestCube" for o in got["scene"]["objects"])
