"""The form tools: hull, skin, blob, sweep, rock - the smooth shape built first.

Pure tests pin the argument contracts. The Blender tests build each form and
prove the property the tool exists for: a hull from two outlines is ONE shell
whose bounds are the outlines' bounds; a stick figure becomes one welded body,
not a bundle of tubes; blobs merge into one shell; a sweep tapers; the same
seed gives the same rock. Plus the worked example runs as written.
"""

from __future__ import annotations

import json
import struct

import pytest

from bgate_adapters import blender, form, surface
from bgate_adapters import _blender_kit as kit

needs_blender = pytest.mark.skipif(not blender.available().get("available"),
                                   reason="Blender not installed")

SIDE = [[-2.0, 0.35], [-1.9, 0.7], [-0.6, 0.75], [0.3, 1.2], [1.2, 1.2], [1.9, 0.8], [2.0, 0.35],
        [1.7, 0.3], [-1.7, 0.3]]
TOP = [[-2.0, 0.0], [-1.9, 0.7], [0.5, 0.9], [2.0, 0.6], [2.0, 0.0]]


def _glb_json(path) -> dict:
    data = path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20:20 + length])


def _shells(path, name):
    """Connected components of one object in a glb, counted on a welded copy."""
    got = surface.look_audit(path)
    part = next(p for p in got["parts"] if p["name"] == name)
    return part["shells"], part


class TestContracts:
    def test_kit_carries_the_form_functions(self):
        for fn in ("bg_hull", "bg_skin", "bg_blob", "bg_sweep", "bg_round", "bg_rock", "bg_form_help"):
            assert f"def {fn}" in kit.KIT, fn
        assert "BG_FORM_EXAMPLE" in kit.KIT
        # The form kit is spliced AFTER the surface kit it calls into.
        assert kit.KIT.index("def bg_fuse") < kit.KIT.index("def bg_hull")

    def test_malformed_input_is_refused_before_blender(self, tmp_path):
        with pytest.raises(ValueError):
            form.hull([[0, 0], [1, 0]], TOP, tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.hull(SIDE, [[0, 0, 0], [1, 0, 0], [1, 1, 0]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.skin([[0, 0, 0, 0.1]], [[0, 0]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.skin([[0, 0, 0, 0.1], [1, 0, 0, 0.1]], [[0, 5]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.blob([[0, 0, 0]], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.sweep([[0, 0, 0], [1, 0, 0]], [0.1, 0.1, 0.1], tmp_path / "x.glb")
        with pytest.raises(ValueError):
            form.rock(tmp_path / "x.glb", size=[1, 0, 1])
        with pytest.raises(ValueError):
            form.rock(tmp_path / "x.glb", preset="velvet")


@needs_blender
class TestInBlender:
    @pytest.mark.slow
    def test_hull_is_one_shell_bounded_by_its_outlines(self, tmp_path):
        got = form.hull(SIDE, TOP, tmp_path / "hull.glb", name="Body", round=1, target_tris=4000,
                        preset="car_paint", colour="#7a1020")
        assert got["ok"], got
        dims = got["dims"]
        assert abs(dims[0] - 4.0) < 0.15 and abs(dims[1] - 1.8) < 0.15 and abs(dims[2] - 0.9) < 0.12
        shells, part = _shells(tmp_path / "hull.glb", "Body")
        assert shells == 1 and part["uv"] == 1
        assert got["tris"] <= 4000 * 1.2

    @pytest.mark.slow
    def test_hull_round_zero_keeps_the_silhouette_edges(self, tmp_path):
        got = form.hull(SIDE, TOP, tmp_path / "hard.glb", name="Block", round=0)
        assert got["ok"], got
        shells, part = _shells(tmp_path / "hard.glb", "Block")
        assert shells == 1
        # A hard hull is a few hundred tris, not a remesh.
        assert got["tris"] < 2000

    @pytest.mark.slow
    def test_skin_is_one_welded_body_not_a_bundle_of_tubes(self, tmp_path):
        joints = [[0, 0, 0.6, 0.18], [-0.5, 0, 0.6, 0.16], [0.4, 0, 0.8, 0.1],
                  [0.15, 0.15, 0.3, 0.06], [0.15, 0.17, 0.05, 0.05],
                  [0.15, -0.15, 0.3, 0.06], [0.15, -0.17, 0.05, 0.05]]
        links = [[0, 1], [0, 2], [0, 3], [3, 4], [0, 5], [5, 6]]
        got = form.skin(joints, links, tmp_path / "beast.glb", name="Beast", subsurf=1)
        assert got["ok"], got
        shells, part = _shells(tmp_path / "beast.glb", "Beast")
        assert shells == 1 and got["tris"] > 200

    @pytest.mark.slow
    def test_blobs_merge_into_one_shell(self, tmp_path):
        got = form.blob([[0, 0, 0.4, 0.5], [0.5, 0, 0.5, 0.4], [-0.4, 0.1, 0.45, 0.35]],
                        tmp_path / "blob.glb", name="Boulder", resolution=0.08)
        assert got["ok"], got
        shells, _ = _shells(tmp_path / "blob.glb", "Boulder")
        assert shells == 1

    @pytest.mark.slow
    def test_sweep_tapers_along_its_path(self, tmp_path):
        got = form.sweep([[0, 0, 0], [0, 0, 1], [0.3, 0, 1.8]], [0.2, 0.1, 0.02], tmp_path / "horn.glb",
                         name="Horn", segments=12)
        assert got["ok"], got
        dims = got["dims"]
        assert dims[2] > 1.6 and dims[0] < 0.8

    @pytest.mark.slow
    def test_same_seed_same_rock(self, tmp_path):
        a = form.rock(tmp_path / "a.glb", seed=5, detail=3)
        b = form.rock(tmp_path / "b.glb", seed=5, detail=3)
        c = form.rock(tmp_path / "c.glb", seed=6, detail=3)
        assert a["ok"] and b["ok"] and c["ok"]
        assert a["tris"] == b["tris"] == 320
        assert [round(x, 4) for x in a["dims"]] == [round(x, 4) for x in b["dims"]]
        assert a["dims"] != c["dims"]
        # It sits on the ground: the bottom is at z=0.
        assert abs(a["dims"][2] - 0.6) < 0.05

    @pytest.mark.slow
    def test_the_worked_example_runs_as_written(self, tmp_path):
        got = blender.run_script("bg_wipe()\nexec(BG_FORM_EXAMPLE)\n",
                                 export_glb=str(tmp_path / "example.glb"), timeout=600, record=False)
        assert got["ok"], got.get("error")
        names = {o["name"] for o in got["scene"]["objects"] if o["type"] == "MESH"}
        assert {"Body", "Beast", "Boulder"} <= names
        doc = _glb_json(tmp_path / "example.glb")
        # The car paint's flat colour reaches the file even though its graph is procedural.
        paint = next(m for m in doc["materials"] if m["name"].startswith("Paint"))
        base = paint["pbrMetallicRoughness"]["baseColorFactor"]
        assert base[0] > base[1] and base[0] > base[2], base
