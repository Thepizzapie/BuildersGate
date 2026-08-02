"""Two defects found by delivering a character into a real game and booting it.

Both are about what `deliver_asset` WRITES, not about what the engine says, so
nothing here launches Godot: the four engine calls deliver_asset makes are
stubbed and the .tscn on disk is the thing under test.

  * The generated scene shipped a Camera3D. Fine for a standalone preview, and
    actively wrong the moment the scene is instanced into a level that already
    has a player camera — Godot makes the first camera into the tree current, so
    the game booted looking out of the delivered character's eye sockets.

  * Every delivery rewrote the .tscn from scratch. Iterating on the .glb
    therefore destroyed every hand edit made to the scene, silently. In the
    session that found this, the camera above had to be stripped out by hand
    FIVE times, because each redelivery put it straight back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bgate_adapters import godot


# ---------------------------------------------------------------------------
# The scene text, on its own
# ---------------------------------------------------------------------------

def _scene(**kw) -> str:
    kw.setdefault("node_name", "Pirate")
    kw.setdefault("bounds_size", [0.8, 1.8, 0.7])
    kw.setdefault("bounds_position", [-0.4, 0.0, -0.35])
    return godot.character_scene_text("res://assets/pirate.glb", **kw)


class TestTheDeliveredSceneDoesNotStealTheView:

    def test_a_character_scene_carries_no_camera_by_default(self):
        """OBSERVED on boot: a delivered pirate instanced into a level that
        already had a player camera, and the first frame of the game was the
        inside of the pirate's head. A scene built to be dropped into a level
        cannot bring a camera that might win."""
        text = _scene()

        assert "Camera3D" not in text, text

    def test_the_rest_of_the_scene_is_unchanged_by_dropping_the_camera(self):
        """The camera was the only thing removed. The body, the instanced model
        and the fitted capsule are what the scene exists for."""
        text = _scene()

        assert '[node name="Pirate" type="CharacterBody3D"]' in text
        assert ('[node name="Model" parent="." instance=ExtResource("1_model")]'
                in text)
        assert '[node name="CollisionShape3D" type="CollisionShape3D" parent="."]' in text

    def test_an_opted_in_camera_still_refuses_to_be_current(self):
        """templates/3d's player.gd does `@onready var _camera := $Camera3D`, so
        the option has to survive for a scene that IS the player. It stays
        harmless by never claiming the view: node order decides `current` when
        nobody declares it, and node order is exactly what put the boot frame
        inside the character's head."""
        text = _scene(with_camera=True)

        assert '[node name="Camera3D" type="Camera3D" parent="."]' in text
        assert "current = false" in text
        assert "current = true" not in text

    def test_load_steps_still_counts_what_is_there_with_a_camera(self):
        """A Camera3D is neither an ext_resource nor a sub_resource, so the
        header must not move — Godot trusts load_steps and a wrong count is a
        load failure, not a warning."""
        for kwargs in ({}, {"with_camera": True}):
            text = _scene(**kwargs)
            declared = int(text.split("load_steps=", 1)[1].split()[0].rstrip("]"))
            present = text.count("[ext_resource ") + text.count("[sub_resource ")
            assert declared == present + 1, (kwargs, declared, present)


# ---------------------------------------------------------------------------
# A delivery, with the engine stubbed out
# ---------------------------------------------------------------------------

def _view() -> dict:
    """inspect_resource's shape for a 1.8 m skinned character, trimmed to the
    keys deliver_asset actually reads."""
    return {
        "ok": True, "root_type": "Node3D", "total_tris": 4210,
        "skeleton_count": 1, "animation_count": 1, "animations": ["Idle"],
        "blend_shapes": [], "collider_count": 1,
        "meshes": [{"name": "Body", "path": "Body", "skinned": True,
                    "aabb_global_position": [-0.4, 0.0, -0.35]}],
        "materials": {"surfaces": 3, "with_albedo_texture": 3,
                      "without_albedo_texture": []},
        "size_check": {"ok": True, "longest_axis_m": 1.8,
                       "metres": [0.8, 1.8, 0.7]},
    }


@pytest.fixture()
def delivery(tmp_path, monkeypatch):
    """A project, a .glb, and every engine call deliver_asset makes replaced.

    Nothing here needs Godot installed: import_asset/inspect_resource/
    check_project/screenshot are the whole engine surface of this function.
    """
    project = tmp_path / "game"
    (project / "assets").mkdir(parents=True)
    project.joinpath("project.godot").write_text("config_version=5\n",
                                                 encoding="utf-8")
    glb = tmp_path / "out" / "pirate.glb"
    glb.parent.mkdir(parents=True)
    glb.write_bytes(b"glTF-not-really")

    def fake_import(project_dir, glb_path, *, dest_rel="assets", timeout=300):
        copied = Path(project_dir) / dest_rel / Path(glb_path).name
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(Path(glb_path).read_bytes())
        return {"ok": True, "copied_to": str(copied),
                "res_path": f"res://{dest_rel}/{copied.name}",
                "engine_view": _view()}

    monkeypatch.setattr(godot, "import_asset", fake_import)
    monkeypatch.setattr(godot, "inspect_resource",
                        lambda *a, **k: _view())
    monkeypatch.setattr(godot, "check_project",
                        lambda *a, **k: {"ok": True, "errors": []})
    monkeypatch.setattr(godot, "_import_uid", lambda *a, **k: "uid://pirate1")

    def fake_shot(project_dir, out_path, **kwargs):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"PNG")
        return {"ok": True, "path": str(out_path)}

    monkeypatch.setattr(godot, "screenshot", fake_shot)

    def deliver(**kw):
        kw.setdefault("screenshot_dir", str(tmp_path / "shots"))
        return godot.deliver_asset(str(project), str(glb), **kw)

    deliver.project = project                    # type: ignore[attr-defined]
    deliver.scene = project / "scenes" / "pirate.tscn"  # type: ignore[attr-defined]
    return deliver


class TestRedeliveryKeepsTheHumansScene:

    def test_a_first_delivery_writes_the_scene(self, delivery):
        got = delivery()

        assert got["scene_action"] == "written"
        assert delivery.scene.exists()
        assert "instance=ExtResource" in delivery.scene.read_text(encoding="utf-8")

    def test_a_delivered_scene_has_no_camera_in_it(self, delivery):
        """The defect as it actually reached the game: not a string in a
        generator, a node in a file the engine loaded."""
        delivery()

        assert "Camera3D" not in delivery.scene.read_text(encoding="utf-8")

    def test_a_redelivery_does_not_destroy_hand_edits(self, delivery):
        """THE defect. Delivering the same asset again used to rewrite the file
        wholesale, so a script attached in the editor, an added hurtbox or a
        nudged transform vanished with no diff and no message."""
        delivery()
        edited = delivery.scene.read_text(encoding="utf-8") + (
            '\n[node name="Hurtbox" type="Area3D" parent="."]\n'
            'collision_layer = 4\n')
        delivery.scene.write_text(edited, encoding="utf-8")

        got = delivery()

        after = delivery.scene.read_text(encoding="utf-8")
        assert got["scene_action"] == "rewired"
        assert '[node name="Hurtbox" type="Area3D" parent="."]' in after
        assert "collision_layer = 4" in after

    def test_a_redelivery_still_points_the_scene_at_the_new_import(self, delivery):
        """Preserving edits must not mean ignoring the asset. The reason to
        redeliver at all is to see the NEW mesh in the game, so the one line
        that is allowed to change is the model reference."""
        delivery.scene.parent.mkdir(parents=True, exist_ok=True)
        delivery.scene.write_text(
            "[gd_scene load_steps=2 format=3]\n\n"
            '[ext_resource type="PackedScene" uid="uid://stale" '
            'path="res://assets/OLD.glb" id="1_model"]\n\n'
            '[node name="Pirate" type="CharacterBody3D"]\n\n'
            '[node name="Model" parent="." instance=ExtResource("1_model")]\n',
            encoding="utf-8")

        got = delivery()

        after = delivery.scene.read_text(encoding="utf-8")
        assert got["scene_action"] == "rewired"
        assert 'path="res://assets/pirate.glb"' in after
        assert 'uid="uid://pirate1"' in after
        assert "OLD.glb" not in after and "uid://stale" not in after
        assert 'id="1_model"' in after

    def test_a_renumbered_model_resource_is_found_by_its_extension(self, delivery):
        """A human who reorganised the scene may have renumbered the ids.
        Matching only on id="1_model" would silently leave them on the old
        mesh — the failure looks like the delivery never ran."""
        delivery.scene.parent.mkdir(parents=True, exist_ok=True)
        delivery.scene.write_text(
            "[gd_scene load_steps=3 format=3]\n\n"
            '[ext_resource type="Script" path="res://scripts/player.gd" id="1_s"]\n'
            '[ext_resource type="PackedScene" path="res://assets/OLD.glb" '
            'id="7_mesh"]\n\n'
            '[node name="Pirate" type="CharacterBody3D"]\n'
            'script = ExtResource("1_s")\n',
            encoding="utf-8")

        got = delivery()

        after = delivery.scene.read_text(encoding="utf-8")
        assert got["scene_action"] == "rewired"
        assert 'path="res://assets/pirate.glb" id="7_mesh"' in after
        assert 'script = ExtResource("1_s")' in after   # untouched
        assert 'path="res://scripts/player.gd"' in after

    def test_a_scene_with_no_model_reference_is_left_alone_and_says_so(
            self, delivery):
        """Silence here is the dangerous outcome: the human would read a green
        delivery and go looking for a new mesh that was never wired in."""
        delivery.scene.parent.mkdir(parents=True, exist_ok=True)
        original = ('[gd_scene format=3]\n\n'
                    '[node name="Pirate" type="Node3D"]\n')
        delivery.scene.write_text(original, encoding="utf-8")

        got = delivery()

        assert delivery.scene.read_text(encoding="utf-8") == original
        assert got["scene_action"] == "left_alone"
        step = next(s for s in got["steps"] if s["step"] == "scenes")
        assert step["ok"] is False
        assert "overwrite_scene=True" in step["note"]

    def test_overwrite_scene_is_the_way_back_to_a_generated_tree(self, delivery):
        """The escape hatch has to exist and has to be explicit — a scene the
        human has broken past repair is still a thing you want to regenerate."""
        delivery()
        delivery.scene.write_text("[gd_scene format=3]\n\n"
                                  '[node name="Junk" type="Node3D"]\n',
                                  encoding="utf-8")

        got = delivery(overwrite_scene=True)

        after = delivery.scene.read_text(encoding="utf-8")
        assert got["scene_action"] == "written"
        assert "Junk" not in after
        assert '[node name="Model" parent="." instance=ExtResource("1_model")]' in after

    def test_the_preview_scene_is_still_regenerated_every_time(self, delivery):
        """The preview is output, not a file anyone edits: it is a photo studio
        built around THIS asset's measured bounds and thrown away with the
        screenshot. Preserving it would freeze the framing of the first
        delivery onto every later one."""
        delivery()
        preview = delivery.project / "scenes" / "pirate_preview.tscn"
        preview.write_text("stale\n", encoding="utf-8")

        delivery()

        assert "PreviewCamera" in preview.read_text(encoding="utf-8")

    def test_delivering_main_glb_does_not_eat_the_projects_main_scene(
            self, delivery):
        """The scene name comes from the .glb filename with no reservation
        list, and the 3d scaffold ships scenes/main.tscn — so `main.glb` aimed
        the generator straight at the user's main scene. This is why the guard
        is an existence check and not a blocklist of names: nothing here knows
        that `main` is special, and it does not need to."""
        main = delivery.project / "scenes" / "main.tscn"
        main.parent.mkdir(parents=True, exist_ok=True)
        # The template's own shape: a script ext_resource and no model.
        original = ('[gd_scene load_steps=2 format=3]\n\n'
                    '[ext_resource type="Script" path="res://scripts/player.gd" '
                    'id="1_player"]\n\n'
                    '[node name="Main" type="Node3D"]\n')
        main.write_text(original, encoding="utf-8")
        glb = Path(delivery.project).parent / "out" / "main.glb"
        glb.write_bytes(b"glTF-not-really")

        got = godot.deliver_asset(str(delivery.project), str(glb),
                                  screenshot_dir=str(delivery.project / "shots"))

        assert main.read_text(encoding="utf-8") == original
        assert got["scene_action"] == "left_alone"

    def test_the_checks_contract_is_unchanged(self, delivery):
        """Nothing above may move the gate. Callers index these rows by name."""
        got = delivery()

        assert [c["check"] for c in got["checks"]] == [
            "loads_in_engine", "has_geometry", "materials_carry_a_texture",
            "real_world_size", "has_collider", "has_skeleton",
            "has_animations", "has_blend_shapes"]
        assert all({"check", "required", "ok", "measured"} <= set(c)
                   for c in got["checks"])
        assert got["ok"] is True


# ---------------------------------------------------------------------------
# The template script has to survive the node this change removed
# ---------------------------------------------------------------------------

PLAYER_GD = (Path(__file__).resolve().parents[1]
             / "templates" / "3d" / "scripts" / "player.gd"
             ).read_text(encoding="utf-8")
# The comments explaining the fix necessarily QUOTE the spelling the fix
# removed, so the code has to be read without them.
PLAYER_CODE = "\n".join(line for line in PLAYER_GD.splitlines()
                        if not line.strip().startswith("#"))


class TestThePlayerScriptToleratesACameralessBody:
    """templates/3d/scripts/player.gd is what the scaffold attaches to the
    player, and deliver_asset generates CharacterBody3D scenes with the same
    node names — so it gets attached to characters that now have no Camera3D.
    A bare `$Camera3D` would turn the camera fix into a per-event null deref,
    which is a worse bug than the one it replaced."""

    def test_the_camera_is_fetched_without_asserting_it_exists(self):
        assert 'get_node_or_null("Camera3D")' in PLAYER_CODE
        assert "$Camera3D" not in PLAYER_CODE

    def test_every_use_of_the_camera_is_guarded(self):
        """One unguarded line is the whole bug — it only takes the first mouse
        move to hit it."""
        uses = [line for line in PLAYER_CODE.splitlines()
                if "_camera." in line]
        assert uses, PLAYER_CODE
        lines = PLAYER_CODE.splitlines()
        for line in uses:
            block = lines[:lines.index(line)]
            guard = next((prev for prev in reversed(block)
                          if prev.strip() and not prev.strip().startswith("#")),
                         "")
            assert "if _camera" in guard or "_camera." in guard, line

    def test_the_body_still_turns_without_a_camera(self):
        """Yaw is on the BODY. Guarding the whole input branch instead of just
        the pitch would leave a camera-less character unable to face where it
        is walking, which reads as broken input rather than a missing node."""
        turn = PLAYER_CODE.split("rotate_y(", 1)[0].splitlines()
        assert not any("if _camera" in line for line in turn[-3:]), turn[-3:]
