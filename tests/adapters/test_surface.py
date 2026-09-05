"""The surface tools: fuse, shade, lathe, loft, material presets, bake, decals.

Pure tests pin the argument contracts and the preset registry (the same names
the kit uses inside Blender). The Blender tests build a deliberately tacked
asset - a branch shoved through a trunk, a box with a sphere floating in front
of it - and prove each operation on it: the fuse yields ONE shell with its
materials carried over, the bake yields maps with detail in them (a bark
albedo is not one colour), the decal is a conformed alpha-clipped sheet.
"""

from __future__ import annotations

import json
import struct

import pytest

from bgate_adapters import blender, surface
from bgate_adapters import _blender_kit as kit

needs_blender = pytest.mark.skipif(not blender.available().get("available"),
                                   reason="Blender not installed")

TACKED = '''
trunk = bg_cyl("Trunk", radius=0.25, depth=3.0, at=(0, 0, 1.5), verts=12)
limb = bg_cyl("Limb", radius=0.1, depth=1.6, at=(0.55, 0, 2.6), verts=8)
limb.rotation_euler = (0, 0.9, 0)
bg_mat(trunk, "Bark", (0.4, 0.3, 0.2)); bg_mat(limb, "Bark", (0.4, 0.3, 0.2))
cab = bg_box("Cab", size=(1.6, 3.0, 0.9), at=(3, 0, 0.9))
lamp = bg_ball("Lamp", radius=0.14, at=(3.5, -1.55, 0.9), segments=16, rings=8)
bg_mat(cab, "Paint", (0.7, 0.1, 0.1)); bg_mat(lamp, "Lens", (0.9, 0.9, 0.8))
for o in (trunk, limb, cab, lamp):
    bg_finish(o)
'''


def _glb_json(path) -> dict:
    data = path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20:20 + length])


class TestContracts:
    def test_presets_match_the_kit(self):
        for name in surface.MATERIAL_PRESETS:
            assert f'"{name}"' in kit.KIT, name
        assert "def bg_fuse" in kit.KIT and "def bg_lathe" in kit.KIT and "def bg_loft" in kit.KIT
        assert "BG_SURFACE_EXAMPLE" in kit.KIT

    def test_lathe_and_loft_refuse_malformed_input(self, tmp_path):
        with pytest.raises(ValueError):
            surface.lathe([[0.1, 0.0]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            surface.lathe([[0.1, 0.0, 9], [0.2, 1.0]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            surface.loft([[[0, 0, 0], [1, 0, 0]]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            surface.loft([[[0, 0, 0], [1, 0, 0]], [[0, 1, 0]]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            surface.lathe([[0.1, 0.0], [0.2, 1.0]], tmp_path / "x.glb", preset="velvet")

    def test_material_rows_are_checked_before_blender(self, tmp_path):
        model = tmp_path / "m.glb"
        model.write_bytes(b"glTF")
        with pytest.raises(ValueError):
            surface.apply_materials(model, tmp_path / "o.glb", [{"target": "Paint", "preset": "nope"}])
        with pytest.raises(ValueError):
            surface.apply_materials(model, tmp_path / "o.glb", [])
        with pytest.raises(ValueError):
            surface.bake(model, tmp_path / "o.glb", textures_dir=tmp_path / "t", maps=["albedo", "height"])

    def test_missing_model_is_named(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            surface.fuse(tmp_path / "missing.glb", tmp_path / "o.glb")
        with pytest.raises(FileNotFoundError):
            surface.decals(tmp_path / "missing.glb", tmp_path / "o.glb",
                           [{"image": str(tmp_path / "no.png"), "position": [0, 0, 0]}])

    def test_panel_line_image_is_transparent_with_a_dark_line(self, tmp_path):
        from PIL import Image
        path = surface.panel_line_image(tmp_path / "line.png", length_px=128, width_px=8)
        img = Image.open(path).convert("RGBA")
        assert img.size[0] == 128
        alpha = img.getchannel("A")
        assert alpha.getpixel((64, 0)) == 0                 # transparent margin
        assert alpha.getpixel((64, img.size[1] // 2)) > 150  # the line


@needs_blender
class TestInBlender:
    @pytest.fixture(scope="class")
    def tacked(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("surface") / "tacked.glb"
        got = blender.run_script(TACKED, export_glb=str(out), timeout=300, record=False)
        assert got.get("ok"), got.get("error")
        return out

    @pytest.mark.slow
    def test_fuse_makes_one_shell_and_keeps_the_material(self, tacked, tmp_path):
        got = surface.fuse(tacked, tmp_path / "fused.glb", objects=["Trunk", "Limb"], name="Tree",
                           smooth=2, target_tris=3000)
        assert got["ok"], got
        assert got["parts"] == 2 and got["fused"] == "Tree"
        assert got["tris_after"] <= 3000 and got["materials"] == ["Bark"]
        names = [o["name"] for o in got["objects"]]
        assert "Tree" in names and "Limb" not in names

    @pytest.mark.slow
    def test_shade_bevels_by_default(self, tacked, tmp_path):
        got = surface.shade(tacked, tmp_path / "shaded.glb", objects=["Cab"])
        assert got["ok"] and got["shaded"][0]["name"] == "Cab"
        assert got["shaded"][0]["bevel"] == pytest.approx(0.015, abs=1e-3)   # 0.5% of 3 m
        cab = [o for o in got["objects"] if o["name"] == "Cab"][0]
        assert cab["tris"] > 12                                            # the bevel added faces

    @pytest.mark.slow
    def test_lathe_and_loft_build_closed_smooth_forms(self, tmp_path):
        tyre = surface.lathe([[0.22, -0.11], [0.34, -0.06], [0.34, 0.06], [0.22, 0.11], [0.22, -0.11]],
                             tmp_path / "tyre.glb", name="Tyre", segments=24, preset="rubber", colour="#141414")
        assert tyre["ok"] and tyre["materials"] == ["TyreMat"]
        assert tyre["dims"][0] == pytest.approx(0.68, abs=0.01) and tyre["dims"][2] == pytest.approx(0.22, abs=0.01)
        hull = [[[-0.9, y, 0.3], [-0.9, y, 0.9], [0.0, y, 1.2], [0.9, y, 0.9], [0.9, y, 0.3]]
                for y in (-2.0, 0.0, 2.0)]
        got = surface.loft(hull, tmp_path / "hull.glb", name="Hull", smooth=1, preset="car_paint", colour="#8b1a2b")
        assert got["ok"] and got["name"] == "Hull" and got["tris"] > 20
        assert got["dims"][1] == pytest.approx(4.0, abs=0.3)

    @pytest.mark.slow
    def test_materials_write_a_sidecar_and_the_bake_carries_detail(self, tacked, tmp_path):
        assign = [{"target": "Paint", "preset": "painted_metal_worn", "colour": "#8b1a2b", "wear": 0.6},
                  {"target": "Bark", "preset": "bark", "colour": "#6b5a45"},
                  {"target": "Lens", "preset": "emissive", "colour": "#fff2c0"}]
        got = surface.apply_materials(tacked, tmp_path / "materials.glb", assign)
        assert got["ok"] and got["missing"] == []
        assert (tmp_path / "materials.blend").is_file()
        assert [a["slots"] for a in got["applied"]] == [1, 2, 1]
        baked = surface.bake(tmp_path / "materials.blend", tmp_path / "baked.glb",
                             textures_dir=tmp_path / "tex", objects=["Trunk"], resolution=128, samples=4)
        assert baked["ok"], baked
        from PIL import Image
        albedo = Image.open(baked["baked"][0]["maps"]["albedo"]).convert("RGB")
        colours = {albedo.getpixel((x, y)) for x in range(0, 128, 4) for y in range(0, 128, 4)}
        assert len(colours) > 40, "a bark bake is not one colour"
        doc = _glb_json(tmp_path / "baked.glb")
        assert doc.get("images"), "the baked glb carries no images"
        assert any(m.get("normalTexture") for m in doc.get("materials", []))

    @pytest.mark.slow
    def test_bake_refuses_an_unknown_target_by_name(self, tacked, tmp_path):
        got = surface.bake(tacked, tmp_path / "b.glb", textures_dir=tmp_path / "t", objects=["Cab"],
                           resolution=64, samples=2,
                           assign=[{"target": "Nowhere", "preset": "plastic"}])
        assert got["ok"] is False and "Nowhere" in got["error"]

    @pytest.mark.slow
    def test_decals_are_conformed_alpha_clipped_sheets(self, tacked, tmp_path):
        line = surface.panel_line_image(tmp_path / "line.png")
        got = surface.decals(tacked, tmp_path / "decal.glb",
                             [{"image": line, "target": "Cab", "position": [3.0, 0.0, 1.35],
                               "normal": [0, 0, 1], "size": [1.4, 0.12], "name": "HoodLine"}])
        assert got["ok"] and got["placed"][0]["target"] == "Cab"
        doc = _glb_json(tmp_path / "decal.glb")
        mats = {m["name"]: m for m in doc["materials"]}
        assert mats["HoodLineMat"]["alphaMode"] == "MASK"
        assert doc.get("images")
