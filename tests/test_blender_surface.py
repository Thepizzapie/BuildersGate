"""The surface half of the 3D path: maps, alpha, the manifest, and the sweep.

Four of these are boundary tests and run REAL Blender — the whole question is
what the glTF exporter writes, and a mock would only assert that we called the
function we wrote. The exported .glb is parsed back out of its own bytes here,
because "the material carries an alpha mode" is a claim about the FILE, not
about the bpy call that preceded it.

The manifest and sweep tests deliberately do NOT need Blender. sweep() unlinks
absolute paths read off a JSON file that anything can write; that is a
confinement test, and a confinement test that skips itself on the machines
without Blender is a confinement test that never runs.
"""
from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path

import pytest

from bgate_adapters import blender

needs_blender = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed")
needs_pil = pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow not installed")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def glb_materials(path: Path) -> list[dict]:
    """The material list out of a .glb's JSON chunk. No importer involved."""
    data = Path(path).read_bytes()
    assert data[:4] == b"glTF", f"{path} is not a glb"
    offset = 12
    while offset < len(data):
        length, kind = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8: offset + 8 + length]
        if kind == 0x4E4F534A:                      # 'JSON'
            return json.loads(chunk.decode("utf-8")).get("materials") or []
        offset += 8 + length + (-length % 4)
    return []


def keyed_png(path: Path) -> Path:
    """An image with REAL transparency — a logo on a cut-out background."""
    from PIL import Image
    image = Image.new("RGBA", (32, 32), (255, 0, 255, 0))
    for y in range(8, 24):
        for x in range(8, 24):
            image.putpixel((x, y), (0, 200, 40, 255))
    image.save(path)
    return path


def flat_png(path: Path, colour=(200, 40, 40)) -> Path:
    from PIL import Image
    Image.new("RGB", (16, 16), colour).save(path)
    return path


ONE_SLOT = """
import bpy
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_plane_add(size=1)
obj = bpy.context.active_object
obj.name = "Decal"
bg_unwrap(obj)
bg_mat(obj, "decal_mat", (1, 1, 1))
"""

# THREE MATERIALS THAT ARE ACTUALLY USED. Assigning three slots and leaving
# every face on index 0 is not a multi-material mesh: glTF exports only the
# materials its primitives reference, and the round trip collapses back to one.
THREE_SLOTS = """
import bpy
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1)
obj = bpy.context.active_object
obj.name = "Body"
bg_unwrap(obj)
bg_mat(obj, "skin", (1, 0.8, 0.7))
bg_mat(obj, "eye", (0.1, 0.1, 0.1))
bg_mat(obj, "mouth", (0.6, 0.2, 0.2))
for index, poly in enumerate(obj.data.polygons):
    poly.material_index = index % 3
"""


@pytest.fixture(scope="module")
def stage(tmp_path_factory):
    """One directory of built layers and images, shared by the slow tests."""
    return tmp_path_factory.mktemp("surface")


@pytest.fixture(scope="module")
def one_slot(stage):
    out = stage / "decal.glb"
    got = blender.run_script(ONE_SLOT, export_glb=str(out), out_dir=str(stage))
    assert got["ok"], got.get("error")
    return out


@pytest.fixture(scope="module")
def three_slots(stage):
    out = stage / "body.glb"
    got = blender.run_script(THREE_SLOTS, export_glb=str(out), out_dir=str(stage))
    assert got["ok"], got.get("error")
    return out


# ---------------------------------------------------------------------------
# Maps — the fix for "everything ships as identical plastic"
# ---------------------------------------------------------------------------
@needs_blender
@needs_pil
class TestMaps:
    def test_non_albedo_maps_are_non_color(self, stage, one_slot):
        """A roughness value read as sRGB is simply the wrong number.

        The transfer curve is applied on the way in, so an authored 0.5 lands
        near 0.21 and the surface reads far glossier than anyone asked for.
        Only the maps feeding a COLOUR socket stay sRGB.
        """
        got = blender.apply_texture(
            one_slot, flat_png(stage / "albedo.png"), stage / "maps.glb",
            roughness=flat_png(stage / "rough.png", (128, 128, 128)),
            metallic=flat_png(stage / "metal.png", (0, 0, 0)),
            normal=flat_png(stage / "normal.png", (128, 128, 255)))
        assert got["ok"], got.get("error")

        spaces = got["colorspaces"]
        assert spaces["base_color"] == "sRGB"
        assert spaces["roughness"] == "Non-Color"
        assert spaces["metallic"] == "Non-Color"
        assert spaces["normal"] == "Non-Color"

    def test_every_map_reaches_its_own_socket(self, stage, one_slot):
        got = blender.apply_texture(
            one_slot, flat_png(stage / "albedo.png"), stage / "wired.glb",
            roughness=flat_png(stage / "rough.png", (128, 128, 128)),
            normal=flat_png(stage / "normal.png", (128, 128, 255)))
        assert got["ok"], got.get("error")
        wired = got["wired"]["decal_mat"]
        assert {"base_color", "roughness", "normal"} <= set(wired)

        # And the exporter agrees: a roughness map means a
        # metallicRoughnessTexture, not just a roughnessFactor somebody typed.
        material = glb_materials(stage / "wired.glb")[0]
        pbr = material.get("pbrMetallicRoughness") or {}
        assert "metallicRoughnessTexture" in pbr
        assert "normalTexture" in material

    def test_one_image_still_works(self, stage, one_slot):
        """The single-map call every existing caller makes must not change."""
        got = blender.apply_texture(one_slot, flat_png(stage / "albedo.png"),
                                    stage / "plain.glb")
        assert got["ok"], got.get("error")
        assert got["textured"] == ["decal_mat"]


# ---------------------------------------------------------------------------
# Alpha — the decal that used to ship as a solid rectangle
# ---------------------------------------------------------------------------
@needs_blender
@needs_pil
class TestAlpha:
    def test_keyed_image_does_not_export_opaque(self, stage, one_slot):
        """THE SCRAMBLED-LOGO FIX, END TO END.

        With the image's Alpha output never linked, the exporter wrote
        alphaMode OPAQUE and a keyed PNG rendered in Godot as a solid block of
        key colour over the cap — worse than the z-fighting the decal layer
        replaced.
        """
        out = stage / "keyed.glb"
        got = blender.apply_texture(one_slot, keyed_png(stage / "logo.png"), out,
                                    decal=True)
        assert got["ok"], got.get("error")
        assert got["alpha"] == "clip"

        mode = glb_materials(out)[0].get("alphaMode", "OPAQUE")
        assert mode != "OPAQUE"
        assert mode == "MASK"

    def test_opaque_image_is_left_opaque(self, stage, one_slot):
        """An RGBA file is not the same thing as a transparent one — marking
        every texture cut-out costs a render pass in the engine for nothing."""
        out = stage / "solid.glb"
        got = blender.apply_texture(one_slot, flat_png(stage / "albedo.png"), out)
        assert got["ok"], got.get("error")
        assert got["alpha"] == "opaque"
        assert glb_materials(out)[0].get("alphaMode", "OPAQUE") == "OPAQUE"

    def test_blend_mode_is_available_for_real_translucency(self, stage, one_slot):
        out = stage / "blended.glb"
        got = blender.apply_texture(one_slot, keyed_png(stage / "logo.png"), out,
                                    alpha="blend")
        assert got["ok"], got.get("error")
        assert glb_materials(out)[0].get("alphaMode") == "BLEND"

    def test_unknown_alpha_mode_is_refused_before_blender_starts(
            self, stage, one_slot):
        with pytest.raises(ValueError, match="alpha must be"):
            blender.apply_texture(one_slot, flat_png(stage / "albedo.png"),
                                  stage / "never.glb", alpha="transparentish")

    @needs_pil
    def test_transparency_probe_reads_pixels_not_channels(self, stage):
        assert blender._has_transparency(keyed_png(stage / "probe_keyed.png")) is True
        assert blender._has_transparency(flat_png(stage / "probe_flat.png")) is False

        from PIL import Image
        opaque_rgba = stage / "probe_rgba.png"
        Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(opaque_rgba)
        assert blender._has_transparency(opaque_rgba) is False


# ---------------------------------------------------------------------------
# Slots — one image on every surface is a stamp, not texturing
# ---------------------------------------------------------------------------
@needs_blender
@needs_pil
class TestMaterialSlots:
    def test_multi_slot_model_refuses_an_unnamed_material(self, stage, three_slots):
        """A body layer with skin/eye/mouth got the identical map on all three
        and reported success. Refuse, and NAME the slots so the caller can pick.
        """
        out = stage / "body_all_the_same.glb"
        got = blender.apply_texture(three_slots, flat_png(stage / "albedo.png"), out)

        assert got["ok"] is False
        assert got["refused"] == "ambiguous_material"
        assert set(got["slots"]) == {"skin", "eye", "mouth"}
        assert set(got["authored"]) == {"skin", "eye", "mouth"}
        # And nothing was written: a refusal that still exports leaves a file
        # that looks exactly like a success.
        assert not out.exists()

    def test_placeholder_materials_do_not_count_as_a_choice(self, stage):
        """A layer modelled without materials gets one grey placeholder per
        mesh, invented by this very call. Refusing over THAT would break every
        untextured layer while catching nothing anybody authored."""
        two = stage / "two_meshes.glb"
        script = """
import bpy
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
bpy.context.active_object.name = "A"
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 0, 0))
bpy.context.active_object.name = "B"
"""
        assert blender.run_script(script, export_glb=str(two),
                                  out_dir=str(stage))["ok"]

        got = blender.apply_texture(two, flat_png(stage / "albedo.png"),
                                    stage / "two_tex.glb")
        assert got["ok"], got.get("error")
        assert got["authored"] == []
        assert len(got["textured"]) == 2

    def test_naming_one_slot_textures_only_that_slot(self, stage, three_slots):
        got = blender.apply_texture(three_slots, flat_png(stage / "albedo.png"),
                                    stage / "body_skin.glb", material="skin")
        assert got["ok"], got.get("error")
        assert got["textured"] == ["skin"]

    def test_all_slots_is_the_explicit_opt_in(self, stage, three_slots):
        got = blender.apply_texture(three_slots, flat_png(stage / "albedo.png"),
                                    stage / "body_all.glb", all_slots=True)
        assert got["ok"], got.get("error")
        assert set(got["textured"]) == {"skin", "eye", "mouth"}

    def test_unmatched_material_is_a_failure_not_an_empty_list(
            self, stage, one_slot):
        """It used to texture nothing, export a copy of the input and report
        ok=True — the most expensive kind of success there is."""
        out = stage / "ghost.glb"
        got = blender.apply_texture(one_slot, flat_png(stage / "albedo.png"), out,
                                    material="NoSuchMaterial")

        assert got["ok"] is False
        assert got["refused"] == "no_such_material"
        assert got["textured"] == []
        assert "decal_mat" in got["slots"]
        assert not out.exists()

    def test_no_maps_at_all_is_refused_before_blender_starts(self, stage, one_slot):
        with pytest.raises(ValueError, match="at least one map"):
            blender.apply_texture(one_slot, None, stage / "never.glb")


# ---------------------------------------------------------------------------
# The record — a layer file that remembers how it was made
# ---------------------------------------------------------------------------
@needs_blender
@needs_pil
class TestLayerRecord:
    def test_export_records_its_own_script(self, stage, one_slot):
        record = blender.read_layer_record(one_slot)
        assert "primitive_plane_add" in record["script"]
        assert record["kit"] is True

    def test_texturing_carries_the_script_forward(self, stage, one_slot):
        out = stage / "carried.glb"
        got = blender.apply_texture(one_slot, flat_png(stage / "albedo.png"), out,
                                    roughness=flat_png(stage / "rough.png"))
        assert got["ok"], got.get("error")

        record = blender.read_layer_record(out)
        # The geometry's script AND the maps that went on it — the texture step
        # is where the chain used to end.
        assert "primitive_plane_add" in record["script"]
        assert set(record["textures"]) == {"base_color", "roughness"}
        assert record["textured_from"] == str(one_slot.resolve())


# ---------------------------------------------------------------------------
# The manifest and the sweep — no Blender required, on purpose
# ---------------------------------------------------------------------------
def _fake_result(sources: dict, *, rig: str = "skeleton",
                 root_name: str = "Player") -> tuple[dict, list]:
    """A combine() return, without running combine(). The manifest writer takes
    a plain dict, and every claim below is about what it persists."""
    parts = [
        {"name": "skeleton", "path": sources["skeleton"], "at": [0.0, 0.0, 0.0],
         "rotate": [0.0, 0.0, 0.0], "scale": 1.0, "bind": "none", "decal_on": ""},
        {"name": "body", "path": sources["body"], "at": [0.0, 0.0, 1.0],
         "rotate": [0.0, 0.0, 90.0], "scale": 2.0, "bind": "deform", "decal_on": ""},
        {"name": "logo", "path": sources["logo"], "at": [0.1, 0.0, 0.2],
         "rotate": [0.0, 45.0, 0.0], "scale": [1.0, 2.0, 3.0],
         "bind": "bone:Head", "decal_on": "body"},
    ]
    layers = [dict(part, objects=[part["name"].title()], tris=12, meshes=1,
                   source=part["path"], bound=part["bind"], imported=True,
                   is_rig=part["name"] == rig,
                   textures={"base_color": "skin.png"} if part["name"] == "body" else {},
                   textured_from="", script="# built " + part["name"])
              for part in parts]
    result = {"ok": True, "armature": "Skeleton", "rig": rig,
              "root_name": root_name, "parts": layers, "checks": [], "warnings": []}
    return result, parts


@pytest.fixture()
def assembled(tmp_path):
    """An asset directory with a manifest and real (empty) layer files."""
    tree = tmp_path / "assets" / "player"
    tree.mkdir(parents=True)
    asset = tree / "player.glb"
    asset.write_bytes(b"glTF-not-really")

    sources = {}
    for name, suffix in (("skeleton", ".blend"), ("body", ".glb"), ("logo", ".glb")):
        path = tree / f"{name}{suffix}"
        path.write_bytes(b"x" * 100)
        sources[name] = str(path)

    result, parts = _fake_result(sources)
    manifest = blender.write_manifest(asset, result, recipe=parts)
    return {"asset": asset, "tree": tree, "sources": sources,
            "manifest": Path(manifest)}


class TestManifest:
    def test_carries_the_placement_arguments(self, assembled):
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        logo = next(l for l in doc["layers"] if l["name"] == "logo")

        # Without these the manifest cannot rebuild the asset: a layer put back
        # at the origin, unrotated and unscaled, is a different asset.
        assert logo["at"] == [0.1, 0.0, 0.2]
        assert logo["rotate"] == [0.0, 45.0, 0.0]
        assert logo["scale"] == [1.0, 2.0, 3.0]
        assert logo["bind"] == "bone:Head"
        assert logo["decal_on"] == "body"

    def test_carries_the_rig_and_the_root_name(self, assembled):
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        assert doc["rig"] == "skeleton"
        assert doc["root_name"] == "Player"
        assert doc["recipe"]["rig"] == "skeleton"
        assert doc["recipe"]["root_name"] == "Player"
        assert next(l for l in doc["layers"] if l["name"] == "skeleton")["is_rig"]

    def test_carries_the_textures_and_the_generating_script(self, assembled):
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        body = next(l for l in doc["layers"] if l["name"] == "body")
        assert body["textures"] == {"base_color": "skin.png"}
        assert body["script"] == "# built body"

    def test_recipe_round_trips_into_combine_arguments(self, assembled):
        """The literal claim in the docstring: re-run this layer later."""
        recipe = blender.manifest_recipe(assembled["asset"])
        assert recipe["rig"] == "skeleton"
        assert recipe["root_name"] == "Player"
        assert [p["name"] for p in recipe["parts"]] == ["skeleton", "body", "logo"]
        assert recipe["missing"] == []

        # And it is accepted by the validator combine() puts its parts through,
        # unchanged — which is what "round trip" has to mean here.
        checked = blender._check_parts(recipe["parts"])
        assert [p["name"] for p in checked] == ["skeleton", "body", "logo"]
        assert checked[2]["rotate"] == [0.0, 45.0, 0.0]
        assert checked[2]["bind"] == "bone:Head"

    def test_a_swept_layer_still_names_the_script_that_built_it(self, assembled):
        Path(assembled["sources"]["body"]).unlink()
        recipe = blender.manifest_recipe(assembled["asset"])
        missing = recipe["missing"]
        assert [m["name"] for m in missing] == ["body"]
        assert missing[0]["script"] == "# built body"
        assert missing[0]["textures"] == {"base_color": "skin.png"}

    def test_recovers_a_recipe_from_a_manifest_that_has_none(self, assembled):
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc.pop("recipe")
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        recipe = blender.manifest_recipe(assembled["asset"])
        assert [p["name"] for p in recipe["parts"]] == ["skeleton", "body", "logo"]
        assert recipe["parts"][1]["at"] == [0.0, 0.0, 1.0]
        assert recipe["rig"] == "skeleton"


class TestSweep:
    def test_removes_the_intermediates(self, assembled):
        got = blender.sweep(assembled["asset"], dry_run=False)
        assert got["ok"] is True
        assert {Path(p).name for p in got["removed"]} == {"body.glb", "logo.glb"}
        assert not Path(assembled["sources"]["body"]).exists()
        assert assembled["asset"].exists()
        assert assembled["manifest"].exists()

    def test_keeps_the_rig(self, assembled):
        """Every other layer is bound TO the rig. Delete it and a rebuilt layer
        can never be re-combined — the character is finished forever."""
        got = blender.sweep(assembled["asset"], dry_run=False)

        assert Path(assembled["sources"]["skeleton"]).exists()
        assert "skeleton.blend" not in {Path(p).name for p in got["removed"]}
        assert "skeleton.blend" in {Path(p).name for p in got["kept"]}

    def test_refuses_a_source_outside_the_assets_tree(self, assembled, tmp_path):
        """A shared models/base_human.blend passed as a layer is not this
        asset's intermediate. Swept once, it is gone for every other asset."""
        shared = tmp_path / "shared" / "base_human.blend"
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b"y" * 50)

        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"].append({"name": "base", "objects": [], "tris": 0,
                              "bound": "none", "decal_on": "",
                              "source": str(shared), "at": [0, 0, 0],
                              "rotate": [0, 0, 0], "scale": 1.0, "bind": "none",
                              "is_rig": False, "textures": {}, "script": ""})
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        got = blender.sweep(assembled["asset"], dry_run=False)

        assert shared.exists(), "sweep deleted a file outside the asset's tree"
        assert str(shared) not in got["removed"]
        assert [r["source"] for r in got["refused"]] == [str(shared)]
        assert "outside" in got["refused"][0]["reason"]

    def test_a_hand_written_manifest_cannot_aim_it_anywhere(self, assembled,
                                                            tmp_path):
        """The manifest is an ordinary JSON file. Unconfined, reading absolute
        paths back out of one and unlinking them is an arbitrary-file-delete
        primitive pointed wherever its last editor liked."""
        victim = tmp_path / "important.txt"
        victim.write_text("do not delete me", encoding="utf-8")

        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"] = [{"name": "evil", "objects": [], "tris": 0,
                          "bound": "none", "decal_on": "", "source": str(victim),
                          "at": [0, 0, 0], "rotate": [0, 0, 0], "scale": 1.0,
                          "bind": "none", "is_rig": False, "textures": {},
                          "script": ""}]
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        got = blender.sweep(assembled["asset"], dry_run=False)

        assert victim.exists()
        assert victim.read_text(encoding="utf-8") == "do not delete me"
        assert got["removed"] == []
        assert got["refused"]

    def test_traversal_out_of_the_tree_is_refused(self, assembled, tmp_path):
        """`..` inside the tree still leaves the tree. This is why the check
        goes through the registry's normaliser instead of a startswith()."""
        victim = tmp_path / "assets" / "neighbour.glb"
        victim.write_bytes(b"z" * 20)

        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"] = [{"name": "sneaky", "objects": [], "tris": 0,
                          "bound": "none", "decal_on": "",
                          "source": str(assembled["tree"] / ".." / "neighbour.glb"),
                          "at": [0, 0, 0], "rotate": [0, 0, 0], "scale": 1.0,
                          "bind": "none", "is_rig": False, "textures": {},
                          "script": ""}]
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        got = blender.sweep(assembled["asset"], dry_run=False)

        assert victim.exists()
        assert got["removed"] == []
        assert got["refused"]

    def test_a_non_layer_suffix_is_not_a_layer(self, assembled):
        """combine() only ever accepts .glb/.gltf/.blend, so anything else in a
        `source` field was put there by hand."""
        stray = assembled["tree"] / "notes.txt"
        stray.write_text("keep", encoding="utf-8")

        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"] = [{"name": "notes", "objects": [], "tris": 0,
                          "bound": "none", "decal_on": "", "source": str(stray),
                          "at": [0, 0, 0], "rotate": [0, 0, 0], "scale": 1.0,
                          "bind": "none", "is_rig": False, "textures": {},
                          "script": ""}]
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        got = blender.sweep(assembled["asset"], dry_run=False)
        assert stray.exists()
        assert got["refused"][0]["source"] == str(stray)

    def test_dry_run_still_reports_and_still_refuses(self, assembled, tmp_path):
        shared = tmp_path / "shared" / "base_human.blend"
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b"y" * 50)
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"].append({"name": "base", "objects": [], "tris": 0,
                              "bound": "none", "decal_on": "",
                              "source": str(shared), "at": [0, 0, 0],
                              "rotate": [0, 0, 0], "scale": 1.0, "bind": "none",
                              "is_rig": False, "textures": {}, "script": ""})
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        got = blender.sweep(assembled["asset"], dry_run=True)

        assert {Path(p).name for p in got["removed"]} == {"body.glb", "logo.glb"}
        assert Path(assembled["sources"]["body"]).exists(), "dry run deleted a file"
        assert [r["source"] for r in got["refused"]] == [str(shared)]

    def test_records_what_it_refused_in_the_manifest(self, assembled, tmp_path):
        shared = tmp_path / "shared" / "base_human.blend"
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b"y" * 50)
        doc = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        doc["layers"].append({"name": "base", "objects": [], "tris": 0,
                              "bound": "none", "decal_on": "",
                              "source": str(shared), "at": [0, 0, 0],
                              "rotate": [0, 0, 0], "scale": 1.0, "bind": "none",
                              "is_rig": False, "textures": {}, "script": ""})
        assembled["manifest"].write_text(json.dumps(doc), encoding="utf-8")

        blender.sweep(assembled["asset"], dry_run=False)

        after = json.loads(assembled["manifest"].read_text(encoding="utf-8"))
        assert [Path(p).name for p in after["removed"]] == ["body.glb", "logo.glb"]
        assert after["refused"][0]["source"] == str(shared)


class TestConfinement:
    def test_accepts_a_path_inside_the_tree(self, tmp_path):
        (tmp_path / "layers").mkdir()
        inside = tmp_path / "layers" / "body.glb"
        inside.write_bytes(b"x")
        assert blender._confine(tmp_path, inside) == "layers/body.glb"

    def test_rejects_a_sibling_directory(self, tmp_path):
        outside = tmp_path.parent / "elsewhere.glb"
        with pytest.raises(ValueError):
            blender._confine(tmp_path / "asset", outside)
