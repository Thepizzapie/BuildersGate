"""blockout_generate: the pure spec layer without Godot, and one real bake.

The pure tests pin what the generator refuses and how a 2D plan becomes
non-overlapping rooms and corridors. The engine test builds the same house the
tool was developed against and asserts the MEASUREMENTS, not the file: every
room reachable on the baked navmesh, walkable floor after props, a too-low
room and a too-narrow door refused by name.
"""

from __future__ import annotations

import json
import re

import pytest

from bgate_adapters import godot
from bgate_core.level import blockout, levelgen

needs_godot = pytest.mark.skipif(not godot.available()["available"],
                                 reason="Godot not installed")

HOUSE = {
    "out_scene": "res://scenes/blockout/house.tscn",
    "player": {"height": 1.8, "radius": 0.4},
    "wall_height": 3.0, "auto_doors": True,
    "rooms": [
        {"name": "Kitchen", "x": 0, "z": 0, "w": 6, "d": 5,
         "props": [{"name": "Counter", "x": 0.0, "z": 0.0, "w": 4, "h": 0.92, "d": 0.6,
                    "climbable": True},
                   {"name": "Fridge", "x": 5.2, "z": 0.0, "w": 0.8, "h": 1.78, "d": 0.8}]},
        {"name": "Hall", "kind": "corridor", "x": 6, "z": 1.5, "w": 4, "d": 1.6},
        {"name": "Lounge", "x": 10, "z": 0, "w": 7, "d": 6},
        {"name": "Closet", "x": 0, "z": 5, "w": 2, "d": 2, "height": 2.2},
    ],
    "doors": [{"from": "Lounge", "side": "s", "width": 1.2}],
    "spawn": {"room": "Kitchen", "x": 3, "z": 3.5},
    "goals": [{"name": "Catnip", "room": "Lounge", "x": 6.5, "z": 0.5, "radius": 0.5}],
}


class TestValidate:
    def test_a_good_spec_has_no_problems(self):
        assert blockout.validate(HOUSE) == []

    def test_overlapping_rooms_are_refused_by_name(self):
        spec = {"rooms": [{"name": "A", "x": 0, "z": 0, "w": 4, "d": 4},
                          {"name": "B", "x": 2, "z": 2, "w": 4, "d": 4}]}
        problems = blockout.validate(spec)
        assert len(problems) == 1 and "A and B overlap by 2.00 x 2.00" in problems[0]

    def test_touching_rooms_are_not_overlapping(self):
        spec = {"rooms": [{"name": "A", "x": 0, "z": 0, "w": 4, "d": 4},
                          {"name": "B", "x": 4, "z": 0, "w": 4, "d": 4}]}
        assert blockout.validate(spec) == []

    def test_props_must_stay_inside_their_room(self):
        spec = {"rooms": [{"name": "A", "x": 0, "z": 0, "w": 4, "d": 4,
                           "props": [{"name": "Sofa", "x": 3, "z": 0, "w": 2, "h": 1, "d": 1}]}]}
        assert any("Sofa pokes outside" in p for p in blockout.validate(spec))

    def test_doors_need_known_rooms_or_a_side(self):
        spec = {"rooms": [{"name": "A", "x": 0, "z": 0, "w": 4, "d": 4}],
                "doors": [{"from": "A", "to": "Nowhere", "side": "up"}]}
        assert any("`side` is not n|s|e|w" in p for p in blockout.validate(spec))

    def test_empty_is_refused(self):
        assert blockout.validate({}) == ["spec.rooms is empty - nothing to block out"]


class TestFromPlan:
    def test_rooms_and_corridors_never_overlap(self):
        for seed in range(6):
            plan = levelgen.plan(32, 24, seed=seed)
            spec = blockout.spec_from_plan(plan, cell_m=1.5)
            assert blockout.validate(spec) == [], seed
            kinds = {r["kind"] for r in spec["rooms"]}
            assert kinds == {"room", "corridor"}, seed

    def test_corridors_end_flush_against_the_rooms_they_join(self):
        plan = levelgen.plan(32, 24, seed=3)
        spec = blockout.spec_from_plan(plan, cell_m=1.0)
        rooms = [r for r in spec["rooms"] if r["kind"] == "room"]
        corridors = [r for r in spec["rooms"] if r["kind"] == "corridor"]
        assert corridors
        # Every corridor piece shares an edge with a room or another piece.
        for c in corridors:
            touches = False
            for other in spec["rooms"]:
                if other is c:
                    continue
                shares_x = (abs(c["x"] + c["w"] - other["x"]) < 1e-6 or abs(other["x"] + other["w"] - c["x"]) < 1e-6)
                shares_z = (abs(c["z"] + c["d"] - other["z"]) < 1e-6 or abs(other["z"] + other["d"] - c["z"]) < 1e-6)
                ox = min(c["x"] + c["w"], other["x"] + other["w"]) - max(c["x"], other["x"])
                oz = min(c["z"] + c["d"], other["z"] + other["d"]) - max(c["z"], other["z"])
                if (shares_x and oz > 1e-6) or (shares_z and ox > 1e-6):
                    touches = True
                    break
            assert touches, c
        assert len(rooms) == len(plan["rooms"])

    def test_spawn_and_exit_land_in_their_rooms(self):
        plan = levelgen.plan(32, 24, seed=3)
        spec = blockout.spec_from_plan(plan, cell_m=2.0)
        by_name = {r["name"]: r for r in spec["rooms"]}
        s = spec["spawn"]
        room = by_name[s["room"]]
        assert 0 <= s["x"] <= room["w"] and 0 <= s["z"] <= room["d"]
        g = spec["goals"][0]
        room = by_name[g["room"]]
        assert 0 <= g["x"] <= room["w"] and 0 <= g["z"] <= room["d"]

    def test_bad_inputs_are_named(self):
        with pytest.raises(blockout.BlockoutError):
            blockout.spec_from_plan({"rooms": []})
        with pytest.raises(blockout.BlockoutError):
            blockout.spec_from_plan(levelgen.plan(32, 24, seed=1), cell_m=0)


@needs_godot
class TestRealBake:
    @pytest.mark.slow
    def test_house_bakes_measures_and_refuses_what_is_wrong(self, tmp_path):
        from bgate_core.store import scaffold
        proj = tmp_path / "game"
        scaffold.new_project(proj, "Blockout", kind="3d")
        spec = json.loads(json.dumps(HOUSE))
        spec["rooms"][3]["height"] = 1.5                       # a closet under the player
        spec["doors"].append({"from": "Kitchen", "to": "Hall", "width": 0.7})  # too narrow
        (proj / ".bgate_blockout_spec.json").write_text(json.dumps(spec), encoding="utf-8")
        src = (scaffold.TEMPLATES_DIR / "shared" / "tools" / "bgate_blockout_gen.gd").read_text(encoding="utf-8")
        got = godot.run_script(src, project_dir=str(proj), timeout=240)
        report = json.loads((proj / ".bgate_out" / "blockout_report.json").read_text(encoding="utf-8"))
        assert (proj / "scenes" / "blockout" / "house.tscn").is_file()
        errors = "\n".join(report["errors"])
        assert "room Closet: height 1.50 m under the 1.80 m player" in errors
        assert re.search(r"door Kitchen__Hall: 0\.70 m wide", errors)
        assert report["ok"] is False and got["ok"] is True
        rooms = {r["name"]: r for r in report["rooms"]}
        assert rooms["Kitchen"]["reachable_from_spawn"] is True
        assert 0.4 < rooms["Kitchen"]["coverage"] < 0.75          # the counter and fridge cost floor
        assert rooms["Lounge"]["reachable_from_spawn"] is False    # the 0.7 m door is the only way in
        assert report["navmesh"]["polygons"] > 0

    @pytest.mark.slow
    def test_a_sound_house_is_ok_and_fully_connected(self, tmp_path):
        from bgate_core.store import scaffold
        proj = tmp_path / "game"
        scaffold.new_project(proj, "Blockout", kind="3d")
        (proj / ".bgate_blockout_spec.json").write_text(json.dumps(HOUSE), encoding="utf-8")
        src = (scaffold.TEMPLATES_DIR / "shared" / "tools" / "bgate_blockout_gen.gd").read_text(encoding="utf-8")
        got = godot.run_script(src, project_dir=str(proj), timeout=240)
        report = json.loads((proj / ".bgate_out" / "blockout_report.json").read_text(encoding="utf-8"))
        assert got["ok"] is True and report["ok"] is True, report["errors"]
        assert all(r["reachable_from_spawn"] for r in report["rooms"])
        assert {d["name"] for d in report["doors"]} >= {"Kitchen__Hall", "Hall__Lounge", "Kitchen__Closet", "Lounge__outside"}
        scene = (proj / "scenes" / "blockout" / "house.tscn").read_text(encoding="utf-8")
        assert 'type="NavigationRegion3D"' in scene and "Lintel_Hall__Lounge" in scene
        assert 'name="Catnip" type="Area3D"' in scene
        # navigation cell size was written because 0.1 differs from Godot's 0.25
        assert "default_cell_size=0.1" in (proj / "project.godot").read_text(encoding="utf-8")
