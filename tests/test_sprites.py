"""The sprite factory + game screenshot, against REAL Blender and Godot.

The contract that matters: transparent frames (alpha actually present), a sheet
whose regions line up with the .tres, a Godot project that accepts the pair, and
a screenshot that shows the real game. All slow, all skipped without the tools.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from bgate_adapters import blender, godot, sprites
from bgate_core import scaffold

needs_blender = pytest.mark.skipif(not blender.available()["available"],
                                   reason="Blender not installed")
needs_both = pytest.mark.skipif(
    not (blender.available()["available"] and godot.available()["available"]),
    reason="needs Blender and Godot")

BOXER = """
import bpy, math
for o in list(bpy.context.scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.9))
body = bpy.context.active_object
body.name = "Body"
body.scale = (0.35, 0.3, 0.85)
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.28, location=(0, 0, 2.05))
head = bpy.context.active_object
head.name = "Head"
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.18, location=(0.55, -0.2, 1.35))
glove = bpy.context.active_object
glove.name = "Glove"
mat = bpy.data.materials.new("Skin")
mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.3, 0.2, 1)
for ob in (body, head, glove):
    ob.data.materials.append(mat)
ld = bpy.data.lights.new("Key", type='SUN')
ld.energy = 3
lo = bpy.data.objects.new("Key", ld)
lo.rotation_euler = (0.6, 0.2, 0)
bpy.context.collection.objects.link(lo)
"""

POSES = [
    {"name": "idle", "script": "pass"},
    {"name": "jab", "script": "bpy.data.objects['Glove'].location.x = 1.1"},
    {"name": "block", "script": "bpy.data.objects['Glove'].location = (0.2, -0.35, 1.8)"},
]


def _png_size(path: str) -> tuple[int, int]:
    header = Path(path).read_bytes()[:24]
    w, h = struct.unpack(">II", header[16:24])
    return w, h


def _has_transparency(path: str) -> bool:
    """True if any pixel's alpha < 255. Minimal PNG decode via zlib/struct —
    enough for our own RGBA output, no image library needed."""
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    lo, hi = img.getextrema()[3]
    return lo < 255


@needs_blender
@pytest.mark.slow
class TestSpriteFactory:
    @pytest.fixture(scope="class")
    def rendered(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("sprites")
        got = sprites.render_sprites(BOXER, POSES, out_dir=str(out),
                                     name="boxer", size=(96, 96))
        assert got["ok"] is True, got
        return got

    def test_all_poses_render(self, rendered):
        assert set(rendered["frames"]) == {"idle", "jab", "block"}
        assert rendered["failed"] == []

    def test_frames_are_transparent(self, rendered):
        """film_transparent is the whole point — a black background makes the
        sprite unusable and the failure is invisible in a bool check."""
        for path in rendered["frames"].values():
            assert _has_transparency(path), f"opaque background: {path}"

    def test_sheet_geometry_matches_tres_regions(self, rendered):
        w, h = _png_size(rendered["sheet"])
        assert (w, h) == (96 * 3, 96)
        tres = Path(rendered["tres"]).read_text(encoding="utf-8")
        for i in range(3):
            assert f"region = Rect2({i * 96}, 0, 96, 96)" in tres

    def test_tres_declares_every_pose_animation(self, rendered):
        tres = Path(rendered["tres"]).read_text(encoding="utf-8")
        for pose in ("idle", "jab", "block"):
            assert f'&"{pose}"' in tres

    def test_poses_actually_differ(self, rendered):
        """The pose scripts must move things — identical frames mean the
        exec-per-pose plumbing silently did nothing."""
        idle = Path(rendered["frames"]["idle"]).read_bytes()
        jab = Path(rendered["frames"]["jab"]).read_bytes()
        assert idle != jab

    def test_broken_pose_fails_alone(self, tmp_path):
        got = sprites.render_sprites(
            BOXER,
            [{"name": "good", "script": "pass"},
             {"name": "bad", "script": "definitely_not_defined()"}],
            out_dir=str(tmp_path), name="p", size=(64, 64))
        assert got["ok"] is True
        assert "good" in got["frames"]
        assert got["failed"][0]["name"] == "bad"

    def test_duplicate_pose_names_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="duplicate"):
            sprites.render_sprites(BOXER, [{"name": "a"}, {"name": "a"}],
                                   out_dir=str(tmp_path))


@needs_both
@pytest.mark.slow
class TestSpritesIntoGodot:
    def test_sheet_and_tres_import_and_load(self, tmp_path):
        rendered = sprites.render_sprites(BOXER, POSES[:2], out_dir=str(tmp_path),
                                          name="boxer", size=(64, 64))
        project = tmp_path / "game"
        scaffold.new_project(project, "SpriteProbe", kind="2d")

        godot.import_asset(str(project), rendered["sheet"], dest_rel="assets/sprites")
        godot.import_asset(str(project), rendered["tres"], dest_rel="assets/sprites")

        # The engine's word, not the file's: load the SpriteFrames and ask it.
        got = godot.run_script(
            """
extends SceneTree

func _init():
	var sf: SpriteFrames = load("res://assets/sprites/boxer_frames.tres")
	if sf == null:
		print("LOAD_FAILED")
	else:
		var names := sf.get_animation_names()
		print("ANIMS ", names)
		var tex := sf.get_frame_texture("idle", 0)
		print("FRAME_SIZE ", tex.get_width(), "x", tex.get_height())
	quit()
""",
            project_dir=str(project), timeout=120)
        assert got["ok"] is True, got
        assert "jab" in got["stdout"] and "idle" in got["stdout"]
        assert "FRAME_SIZE 64x64" in got["stdout"]


@pytest.mark.skipif(not godot.available()["available"], reason="Godot not installed")
@pytest.mark.slow
class TestScreenshot:
    def test_captures_the_running_game(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "ShotProbe", kind="2d")
        godot.check_project(str(project), timeout=240)

        out = tmp_path / "shot.png"
        got = godot.screenshot(str(project), str(out), at=0.6)
        assert got["ok"] is True, got
        assert out.exists() and out.stat().st_size > 1000
        w, h = _png_size(str(out))
        assert w > 0 and h > 0

    def test_injection_is_always_cleaned_up(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "ShotProbe", kind="2d")
        godot.check_project(str(project), timeout=240)
        godot.screenshot(str(project), str(tmp_path / "s.png"), at=0.5)

        assert not (project / "override.cfg").exists()
        assert not (project / ".bgate_shot.gd").exists()

    def test_refuses_to_clobber_existing_override(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "ShotProbe", kind="2d")
        (project / "override.cfg").write_text("[user]\nprecious=true\n")

        got = godot.screenshot(str(project), str(tmp_path / "s.png"))
        assert got["ok"] is False
        assert "refusing" in got["error"]
        assert (project / "override.cfg").read_text() == "[user]\nprecious=true\n"
