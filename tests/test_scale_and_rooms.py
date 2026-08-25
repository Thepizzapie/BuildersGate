"""Scale measured at game scale, and rooms reviewed as whole rooms."""
from __future__ import annotations

import pytest

from bgate_core import roomqa, scalecontract


def _png(path, size, box=None, alpha=255):
    """An image with an opaque rectangle in it, for measuring."""
    from PIL import Image

    im = Image.new("RGBA", size, (0, 0, 0, 0))
    if box:
        block = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]),
                          (200, 40, 40, alpha))
        im.paste(block, (box[0], box[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


class TestScale:
    def test_nothing_can_be_measured_without_a_player_height(self, root):
        _png(root / "art" / "mug.png", (64, 64), (0, 0, 32, 32))
        with pytest.raises(scalecontract.NotDeclared):
            scalecontract.check(root, "art/mug.png", "prop")

    def test_the_box_is_measured_not_the_canvas(self, root):
        # A 512x512 sheet holding a 40px mug is a 40px mug. This is the whole
        # reason a contact sheet cannot review scale.
        _png(root / "art" / "mug.png", (512, 512), (10, 10, 50, 50))
        got = scalecontract.extents(root / "art" / "mug.png")
        assert got["canvas"] == [512, 512]
        assert (got["width"], got["height"]) == (40, 40)

    def test_a_chair_sized_mug_is_caught(self, root):
        scalecontract.set_contract(root, player_height_px=64)
        _png(root / "art" / "mug.png", (128, 128), (0, 0, 60, 60))
        got = scalecontract.check(root, "art/mug.png", "prop")
        assert not got["ok"]
        assert any("tops out" in f for f in got["flags"])

    def test_a_hatch_sized_door_is_caught(self, root):
        scalecontract.set_contract(root, player_height_px=64)
        _png(root / "art" / "door.png", (128, 128), (0, 0, 30, 60))
        got = scalecontract.check(root, "art/door.png", "door")
        assert not got["ok"]
        assert any("starts at" in f for f in got["flags"])

    def test_a_strip_is_graded_per_frame(self, root):
        scalecontract.set_contract(root, player_height_px=64)
        _png(root / "art" / "walk.png", (384, 64), (0, 0, 384, 60))
        wide = scalecontract.check(root, "art/walk.png", "enemy")
        framed = scalecontract.check(root, "art/walk.png", "enemy", frames=6)
        assert wide["measured"]["width_players"] == 6.0
        assert framed["measured"]["width_players"] == 1.0

    def test_an_empty_image_is_not_a_pass(self, root):
        scalecontract.set_contract(root, player_height_px=64)
        _png(root / "art" / "blank.png", (64, 64))
        got = scalecontract.check(root, "art/blank.png", "prop")
        assert not got["ok"]

    def test_an_undeclared_project_owes_the_release_gate_a_number(self, root):
        assert any("player_height_px" in row
                   for row in scalecontract.unmeasured(root))

    def test_a_band_must_be_ordered(self, root):
        with pytest.raises(ValueError, match="0 < low < high"):
            scalecontract.set_contract(root, classes={"door": {"low": 2.0,
                                                               "high": 1.0}})

    def test_an_unknown_class_is_refused(self, root):
        with pytest.raises(ValueError, match="unknown scale class"):
            scalecontract.set_contract(root, classes={"vehicle": {"low": 1.0,
                                                                  "high": 2.0}})


SCENE_HEADER = "[gd_scene load_steps=1 format=3]\n\n"


def _room(root, name, nodes):
    """A .tscn with positioned Sprite2D nodes — enough for the outline."""
    body = [SCENE_HEADER, '[node name="Room" type="Node2D"]\n\n']
    for label, (x, y) in nodes:
        body.append(f'[node name="{label}" type="Sprite2D" parent="."]\n'
                    f"position = Vector2({x}, {y})\n\n")
    path = root / "game" / "levels" / f"{name}.tscn"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(body), encoding="utf-8")
    return f"game/levels/{name}.tscn"


class TestRoomMeasurement:
    def test_an_empty_room_says_so(self, root):
        scene = _room(root, "empty", [])
        got = roomqa.measure(root, scene)
        assert any("empty rectangle" in f for f in got["findings"])

    def test_furniture_shoved_against_the_walls_is_caught(self, root):
        # Everything on the perimeter of a 1000x1000 room, nothing in it.
        nodes = [(f"P{i}", at) for i, at in enumerate([
            (0, 0), (500, 0), (1000, 0), (0, 500), (1000, 500),
            (0, 1000), (500, 1000), (1000, 1000)])]
        scene = _room(root, "perimeter", nodes)
        got = roomqa.measure(root, scene, bounds=[0, 0, 1000, 1000])
        assert any("against a wall" in f for f in got["findings"])
        assert any("empty floor" in f for f in got["findings"])

    def test_a_pile_is_not_a_focal_point(self, root):
        nodes = [(f"P{i}", (500 + i, 500 + i)) for i in range(8)]
        scene = _room(root, "pile", nodes)
        got = roomqa.measure(root, scene, bounds=[0, 0, 1000, 1000])
        assert any("that is a pile" in f for f in got["findings"])

    def test_a_missing_scene_is_an_error_not_an_empty_pass(self, root):
        with pytest.raises(FileNotFoundError):
            roomqa.measure(root, "game/levels/ghost.tscn")


class TestRoomReview:
    def test_a_cutout_is_not_a_room_shot(self, root):
        scene = _room(root, "lobby", [("A", (100, 100))])
        _png(root / "shot.png", (640, 360), (0, 0, 64, 64))     # mostly alpha
        with pytest.raises(ValueError, match="cutout of an asset"):
            roomqa.review(root, scene, shot="shot.png", verdict="pass",
                          notes="the lobby reads well from the doorway in")

    def test_a_square_crop_is_refused(self, root):
        scene = _room(root, "lobby", [("A", (100, 100))])
        _png(root / "shot.png", (512, 512), (0, 0, 512, 512))
        with pytest.raises(ValueError, match="is a crop"):
            roomqa.review(root, scene, shot="shot.png", verdict="pass",
                          notes="the lobby reads well from the doorway in")

    def test_a_four_three_game_is_not_refused_for_being_four_three(self, root):
        # A hardcoded 16:9 band would refuse every screenshot a 4:3 project
        # ever takes, which is a gate that gets switched off wholesale.
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "project.godot").write_text(
            "[display]\n\nwindow/size/viewport_width=1024\n"
            "window/size/viewport_height=768\n", encoding="utf-8")
        scene = _room(root, "lobby", [("A", (100, 100))])
        _png(root / "shot.png", (1024, 768), (0, 0, 1024, 768))
        got = roomqa.review(root, scene, shot="shot.png", verdict="fail",
                            notes="one prop in the whole room, and it is in "
                                  "the corner")
        assert got["verdict"] == "fail"

    def test_a_widescreen_crop_of_a_four_three_game_is_refused(self, root):
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "project.godot").write_text(
            "[display]\n\nwindow/size/viewport_width=1024\n"
            "window/size/viewport_height=768\n", encoding="utf-8")
        scene = _room(root, "lobby", [("A", (100, 100))])
        _png(root / "shot.png", (1280, 360), (0, 0, 1280, 360))
        with pytest.raises(ValueError, match="this is a crop of one"):
            roomqa.review(root, scene, shot="shot.png", verdict="fail",
                          notes="one prop in the whole room, in the corner")

    def test_a_pass_cannot_stand_over_a_measured_finding(self, root):
        scene = _room(root, "lobby", [("A", (0, 0)), ("B", (1000, 0)),
                                      ("C", (0, 1000)), ("D", (1000, 1000))])
        _png(root / "shot.png", (1280, 720), (0, 0, 1280, 720))
        with pytest.raises(ValueError, match="answer them one by one"):
            roomqa.review(root, scene, shot="shot.png", verdict="pass",
                          notes="the lobby reads well from the doorway in",
                          bounds=[0, 0, 1000, 1000])

    def test_a_fail_is_always_allowed(self, root):
        scene = _room(root, "lobby", [("A", (0, 0)), ("B", (1000, 1000))])
        _png(root / "shot.png", (1280, 720), (0, 0, 1280, 720))
        got = roomqa.review(root, scene, shot="shot.png", verdict="fail",
                            notes="everything is against the walls and the "
                                  "middle is bare floor")
        assert got["verdict"] == "fail"

    def test_an_override_is_per_finding(self, root):
        scene = _room(root, "lobby", [("A", (0, 0)), ("B", (1000, 0)),
                                      ("C", (0, 1000)), ("D", (1000, 1000))])
        _png(root / "shot.png", (1280, 720), (0, 0, 1280, 720))
        findings = roomqa.measure(root, scene,
                                  bounds=[0, 0, 1000, 1000])["findings"]
        for finding in findings:
            roomqa.override(root, scene, finding,
                            "this is a deliberate empty atrium and the "
                            "emptiness is the whole shot")
        got = roomqa.review(root, scene, shot="shot.png", verdict="fail",
                            notes="recording the overrides against the room",
                            bounds=[0, 0, 1000, 1000])
        assert len(got["overrides"]) == len(findings)

    def test_an_override_costs_a_sentence(self, root):
        with pytest.raises(ValueError, match="costs a sentence"):
            roomqa.override(root, "game/levels/lobby.tscn", "too empty", "fine")

    def test_an_unreviewed_room_owes_the_release_gate(self, root):
        scene = _room(root, "lobby", [("A", (100, 100))])
        assert any(scene in row for row in roomqa.unreviewed(root))
