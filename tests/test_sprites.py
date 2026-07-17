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


class TestPaintedSheetSlicing:
    """Slicing a painted pose row — pure PIL, no API, fully testable."""

    @pytest.fixture()
    def pose_row(self, tmp_path):
        """A fake 3-pose transparent row: colored blobs of differing heights."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (1536, 1024), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # pose 0: tall red, pose 1: short green, pose 2: wide blue
        draw.ellipse((100, 200, 400, 950), fill=(220, 40, 40, 255))
        draw.ellipse((612, 600, 900, 950), fill=(40, 220, 40, 255))
        draw.ellipse((1050, 500, 1500, 950), fill=(40, 40, 220, 255))
        path = tmp_path / "row.png"
        img.save(path)
        return path

    def test_slices_trims_and_bottom_aligns(self, pose_row, tmp_path):
        from PIL import Image

        got = sprites.from_painted_sheet(str(pose_row), ["idle", "duck", "wide"],
                                         out_dir=str(tmp_path / "out"), name="p",
                                         frame_size=(160, 240))
        assert got["ok"] is True and got["failed"] == []
        assert set(got["frames"]) == {"idle", "duck", "wide"}

        for path in got["frames"].values():
            frame = Image.open(path)
            assert frame.size == (160, 240)
            alpha = frame.getchannel("A")
            # Bottom-aligned: content must touch the bottom rows (fighters
            # stand on the ground), and the top-left corner stays transparent.
            bottom = [alpha.getpixel((x, 239)) for x in range(0, 160, 4)]
            assert max(bottom) > 0, "content does not reach the baseline"
            assert alpha.getpixel((0, 0)) == 0

    def test_sheet_and_tres_come_out_like_the_blender_path(self, pose_row, tmp_path):
        got = sprites.from_painted_sheet(str(pose_row), ["a", "b", "c"],
                                         out_dir=str(tmp_path), name="p",
                                         frame_size=(160, 240))
        assert _png_size(got["sheet"]) == (480, 240)
        tres = Path(got["tres"]).read_text(encoding="utf-8")
        assert 'type="SpriteFrames"' in tres
        assert "region = Rect2(320, 0, 160, 240)" in tres

    def test_empty_cell_is_reported_not_shipped(self, tmp_path):
        """The model drew 2 poses where 3 were asked — the gap must be loud."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (1536, 1024), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((100, 200, 400, 950), fill=(220, 40, 40, 255))
        draw.ellipse((612, 600, 900, 950), fill=(40, 220, 40, 255))
        # third column left empty
        path = tmp_path / "row.png"
        img.save(path)

        got = sprites.from_painted_sheet(str(path), ["idle", "jab", "ko"],
                                         out_dir=str(tmp_path / "o"), name="p")
        assert got["ok"] is True
        assert set(got["frames"]) == {"idle", "jab"}
        assert got["failed"][0]["name"] == "ko"
        assert "empty" in got["failed"][0]["error"]

    def test_fully_blank_image_fails_loudly(self, tmp_path):
        from PIL import Image

        blank = tmp_path / "blank.png"
        Image.new("RGBA", (300, 100), (0, 0, 0, 0)).save(blank)
        got = sprites.from_painted_sheet(str(blank), ["a"], out_dir=str(tmp_path),
                                         name="p")
        assert got["ok"] is False


class TestFromPoseImages:
    """The reference-first flow's assembly half — pure PIL, no API."""

    def _blob(self, tmp_path, name, box, size=(1024, 1536)):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse(box, fill=(200, 60, 40, 255))
        path = tmp_path / f"{name}.png"
        img.save(path)
        return str(path)

    def test_assembles_individual_poses(self, tmp_path):
        from PIL import Image

        files = [
            ("idle", self._blob(tmp_path, "idle", (300, 200, 700, 1400))),
            ("jab", self._blob(tmp_path, "jab", (200, 400, 900, 1400))),
        ]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "out"),
                                       name="t", frame_size=(160, 240))
        assert got["ok"] is True and got["failed"] == []
        assert _png_size(got["sheet"]) == (320, 240)
        for path in got["frames"].values():
            frame = Image.open(path)
            alpha = frame.getchannel("A")
            assert max(alpha.getpixel((x, 239)) for x in range(0, 160, 4)) > 0

        tres = Path(got["tres"]).read_text(encoding="utf-8")
        assert '&"idle"' in tres and '&"jab"' in tres

    def test_missing_and_empty_files_fail_alone(self, tmp_path):
        from PIL import Image

        empty = tmp_path / "empty.png"
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(empty)
        files = [
            ("good", self._blob(tmp_path, "good", (300, 200, 700, 1400))),
            ("gone", str(tmp_path / "nope.png")),
            ("blank", str(empty)),
        ]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "o"), name="t")
        assert got["ok"] is True
        assert set(got["frames"]) == {"good"}
        assert {f["name"] for f in got["failed"]} == {"gone", "blank"}


class TestEditValidation:
    def test_edit_requires_existing_reference(self, tmp_path, monkeypatch):
        from bgate_adapters import imagegen

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        with pytest.raises(FileNotFoundError):
            imagegen.edit("x", [str(tmp_path / "ghost.png")], str(tmp_path / "o.png"))
        with pytest.raises(ValueError, match="reference"):
            imagegen.edit("x", [], str(tmp_path / "o.png"))


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
