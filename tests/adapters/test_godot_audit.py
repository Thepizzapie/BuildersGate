"""godot_scene_audit and godot_export_verify.

The static checks run without an engine on hand-written .tscn text. The engine
tests build a scene with the defects catnip-fiend and Corniche shipped - a prop
floating off its shelf, a book on the landing, a mesh whose collider was not
resized, a scene whose instances share a mutated sub_resource - and assert the
audit names each one. The export test exports a pck with the shared Web preset,
then breaks the project copy so the diff has something real to find.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bgate_adapters import godot, godot_audit
from bgate_core.store import scaffold

needs_godot = pytest.mark.skipif(not godot.available()["available"], reason="Godot not installed")

PROP_SCENE = '''[gd_scene load_steps=4 format=3]

[ext_resource type="Script" path="res://scripts/sizer.gd" id="1"]

[sub_resource type="BoxMesh" id="m"]
size = Vector3(1, 1, 1)

[sub_resource type="BoxShape3D" id="s"]
size = Vector3(1, 1, 1)

[node name="Prop" type="StaticBody3D"]
script = ExtResource("1")

[node name="Mesh" type="MeshInstance3D" parent="."]
mesh = SubResource("m")

[node name="Shape" type="CollisionShape3D" parent="."]
shape = SubResource("s")
'''

SIZER = '''extends StaticBody3D
@export var size := Vector3(1, 1, 1)
func _ready() -> void:
	$Mesh.mesh.size = size
	$Shape.shape.size = size
'''

AUDIT_SCENE = '''[gd_scene load_steps=12 format=3]

[ext_resource type="PackedScene" path="res://scenes/prop.tscn" id="p"]

[sub_resource type="BoxMesh" id="ground_m"]
size = Vector3(20, 0.2, 20)
[sub_resource type="BoxShape3D" id="ground_s"]
size = Vector3(20, 0.2, 20)
[sub_resource type="BoxMesh" id="shelf_m"]
size = Vector3(2, 0.1, 0.6)
[sub_resource type="BoxShape3D" id="shelf_s"]
size = Vector3(2, 0.1, 0.6)
[sub_resource type="BoxMesh" id="book_m"]
size = Vector3(0.3, 0.25, 0.2)
[sub_resource type="BoxShape3D" id="book_s"]
size = Vector3(0.3, 0.25, 0.2)
[sub_resource type="BoxMesh" id="float_m"]
size = Vector3(0.5, 0.5, 0.5)
[sub_resource type="BoxShape3D" id="float_s"]
size = Vector3(0.5, 0.5, 0.5)
[sub_resource type="BoxMesh" id="bad_m"]
size = Vector3(1, 2, 1)
[sub_resource type="BoxShape3D" id="bad_s"]
size = Vector3(1, 0.5, 1)
[sub_resource type="BoxMesh" id="ghost_m"]
size = Vector3(1, 1, 1)

[node name="Audit" type="Node3D"]

[node name="Ground" type="StaticBody3D" parent="."]
[node name="Mesh" type="MeshInstance3D" parent="Ground"]
mesh = SubResource("ground_m")
[node name="Shape" type="CollisionShape3D" parent="Ground"]
shape = SubResource("ground_s")

[node name="Shelf" type="StaticBody3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1.05, 0)
[node name="Mesh" type="MeshInstance3D" parent="Shelf"]
mesh = SubResource("shelf_m")
[node name="Shape" type="CollisionShape3D" parent="Shelf"]
shape = SubResource("shelf_s")

[node name="Book" type="StaticBody3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1.225, 0)
[node name="Mesh" type="MeshInstance3D" parent="Book"]
mesh = SubResource("book_m")
[node name="Shape" type="CollisionShape3D" parent="Book"]
shape = SubResource("book_s")

[node name="Floater" type="StaticBody3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 4, 0.55, 4)
[node name="Mesh" type="MeshInstance3D" parent="Floater"]
mesh = SubResource("float_m")
[node name="Shape" type="CollisionShape3D" parent="Floater"]
shape = SubResource("float_s")

[node name="BadCollider" type="StaticBody3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, -4, 1.1, -4)
[node name="Mesh" type="MeshInstance3D" parent="BadCollider"]
mesh = SubResource("bad_m")
[node name="Shape" type="CollisionShape3D" parent="BadCollider"]
shape = SubResource("bad_s")

[node name="Ghost" type="MeshInstance3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, -4, 0.6, 4)
mesh = SubResource("ghost_m")

[node name="PropA" parent="." instance=ExtResource("p")]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 6, 0.6, -6)
size = Vector3(1, 1.2, 1)

[node name="PropB" parent="." instance=ExtResource("p")]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 8, 0.6, -6)
size = Vector3(1, 0.4, 1)
'''


def _project(tmp_path):
    proj = tmp_path / "game"
    scaffold.new_project(proj, "Audit", kind="3d")
    (proj / "scripts" / "sizer.gd").write_text(SIZER, encoding="utf-8")
    (proj / "scenes" / "prop.tscn").write_text(PROP_SCENE, encoding="utf-8")
    (proj / "scenes" / "audit.tscn").write_text(AUDIT_SCENE, encoding="utf-8")
    return proj


class TestStatic:
    def test_fresh_scaffold_boots_into_the_demo(self, tmp_path):
        proj = _project(tmp_path)
        got = godot_audit.static_checks(str(proj), "res://scenes/main.tscn")
        assert got["boot_scene"] == "res://scenes/main.tscn"
        assert any(f["code"] == "boot_is_scaffold" for f in got["findings"])

    def test_instanced_scene_with_mutating_script_is_named(self, tmp_path):
        proj = _project(tmp_path)
        got = godot_audit.static_checks(str(proj), "res://scenes/audit.tscn")
        codes = [f["code"] for f in got["findings"]]
        assert "instanced_subresource_mutated" in codes
        row = got["instanced_scenes"][0]
        assert row["scene"] == "res://scenes/prop.tscn" and row["instances"] == 2
        assert not row["local_to_scene"]

    def test_local_to_scene_clears_it(self, tmp_path):
        proj = _project(tmp_path)
        fixed = PROP_SCENE.replace('[sub_resource type="BoxMesh" id="m"]\n',
                                   '[sub_resource type="BoxMesh" id="m"]\nresource_local_to_scene = true\n')
        fixed = fixed.replace('[sub_resource type="BoxShape3D" id="s"]\n',
                              '[sub_resource type="BoxShape3D" id="s"]\nresource_local_to_scene = true\n')
        (proj / "scenes" / "prop.tscn").write_text(fixed, encoding="utf-8")
        got = godot_audit.static_checks(str(proj), "res://scenes/audit.tscn")
        assert "instanced_subresource_mutated" not in [f["code"] for f in got["findings"]]

    def test_shared_subresource_inside_one_scene(self, tmp_path):
        proj = _project(tmp_path)
        scene = '''[gd_scene load_steps=3 format=3]
[ext_resource type="Script" path="res://scripts/sizer.gd" id="1"]
[sub_resource type="BoxMesh" id="m"]
size = Vector3(1, 1, 1)
[node name="Root" type="Node3D"]
[node name="A" type="StaticBody3D" parent="."]
script = ExtResource("1")
[node name="Mesh" type="MeshInstance3D" parent="A"]
mesh = SubResource("m")
[node name="B" type="StaticBody3D" parent="."]
script = ExtResource("1")
[node name="Mesh" type="MeshInstance3D" parent="B"]
mesh = SubResource("m")
'''
        (proj / "scenes" / "shared.tscn").write_text(scene, encoding="utf-8")
        got = godot_audit.static_checks(str(proj), "res://scenes/shared.tscn")
        hit = [f for f in got["findings"] if f["code"] == "shared_subresource_mutated"]
        assert hit and "A/Mesh" in hit[0]["node"] and "B/Mesh" in hit[0]["node"]

    def test_trimesh_on_rigid_body(self, tmp_path):
        proj = _project(tmp_path)
        scene = '''[gd_scene load_steps=2 format=3]
[sub_resource type="ConcavePolygonShape3D" id="c"]
[node name="Root" type="Node3D"]
[node name="Crate" type="RigidBody3D" parent="."]
[node name="Shape" type="CollisionShape3D" parent="Crate"]
shape = SubResource("c")
'''
        (proj / "scenes" / "rigid.tscn").write_text(scene, encoding="utf-8")
        got = godot_audit.static_checks(str(proj), "res://scenes/rigid.tscn")
        assert any(f["code"] == "trimesh_on_moving_body" for f in got["findings"])


@needs_godot
class TestEngine:
    @pytest.mark.slow
    def test_audit_names_every_planted_defect(self, tmp_path):
        proj = _project(tmp_path)
        got = godot_audit.audit(str(proj), "res://scenes/audit.tscn", player_height=1.8,
                                player_radius=0.3)
        assert got.get("error") is None, got
        by_code = {}
        for f in got["errors"] + got["warnings"] + got["info"]:
            by_code.setdefault(f["code"], []).append(f["node"])
        assert "Floater/Mesh" in by_code.get("floating", []), by_code
        # The two instances share one resized BoxMesh: both took PropB's 0.4 m
        # height and both now hover 0.3 m up. The static check names the cause,
        # the engine pass shows the symptom.
        assert "PropB/Mesh" in by_code.get("floating", []), by_code
        assert "BadCollider/Mesh" in by_code.get("collider_mismatch", []), by_code
        assert "Ghost" in by_code.get("no_collider", []), by_code
        assert "Shelf" in by_code.get("no_headroom", []) + by_code.get("partial_headroom", []), by_code
        assert "instanced_subresource_mutated" in by_code
        # The book rests on the shelf, the shelf on nothing but the wall of air
        # below it: the book must NOT be called floating.
        assert "Book/Mesh" not in by_code.get("floating", [])
        assert got["ok"] is False
        shelf = [s for s in got["surfaces"] if s["node"] == "Shelf"][0]
        assert shelf["clear_fraction"] < 1.0

    @pytest.mark.slow
    def test_boot_scene_default_and_a_clean_scene_passes(self, tmp_path):
        proj = _project(tmp_path)
        got = godot_audit.audit(str(proj))
        assert got["is_boot_scene"] and got["ok"] is False
        assert [e["code"] for e in got["errors"]] == ["boot_is_scaffold"]
        clean = '''[gd_scene load_steps=5 format=3]
[sub_resource type="BoxMesh" id="g"]
size = Vector3(10, 0.2, 10)
[sub_resource type="BoxShape3D" id="gs"]
size = Vector3(10, 0.2, 10)
[sub_resource type="BoxMesh" id="c"]
size = Vector3(1, 1, 1)
[sub_resource type="BoxShape3D" id="cs"]
size = Vector3(1, 1, 1)
[node name="Clean" type="Node3D"]
[node name="Ground" type="StaticBody3D" parent="."]
[node name="Mesh" type="MeshInstance3D" parent="Ground"]
mesh = SubResource("g")
[node name="Shape" type="CollisionShape3D" parent="Ground"]
shape = SubResource("gs")
[node name="Crate" type="StaticBody3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 2, 0.6, 2)
[node name="Mesh" type="MeshInstance3D" parent="Crate"]
mesh = SubResource("c")
[node name="Shape" type="CollisionShape3D" parent="Crate"]
shape = SubResource("cs")
'''
        (proj / "scenes" / "clean.tscn").write_text(clean, encoding="utf-8")
        got = godot_audit.audit(str(proj), "res://scenes/clean.tscn")
        assert got["ok"] is True and got["warnings"] == [], got


@needs_godot
class TestExportVerify:
    @pytest.mark.slow
    def test_diff_finds_what_the_pck_lost(self, tmp_path):
        proj = _project(tmp_path)
        godot.check_project(str(proj), timeout=240)
        pck = proj / "build" / "game.pck"
        pck.parent.mkdir()
        proc = subprocess.run([godot.find_godot(), "--headless", "--path", str(proj),
                               "--export-pack", "Web", str(pck)],
                              capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
                              creationflags=godot._NO_WINDOW, **godot._TEXT)
        if not pck.is_file():
            pytest.skip("pck export unavailable here: " + (proc.stderr or proc.stdout)[-400:])
        same = godot_audit.export_verify(str(proj), str(pck), "res://scenes/audit.tscn")
        assert same.get("error") is None, same
        assert same["ok"] is True and same["nodes_editor"] == same["nodes_shipped"] > 10
        # Now the project drifts from the build: an override changes, a node
        # goes, a collider grows. The diff must name all three.
        text = (proj / "scenes" / "audit.tscn").read_text(encoding="utf-8")
        text = text.replace("size = Vector3(1, 1.2, 1)", "size = Vector3(1, 1.9, 1)")
        text = text.replace('[sub_resource type="BoxShape3D" id="float_s"]\nsize = Vector3(0.5, 0.5, 0.5)',
                            '[sub_resource type="BoxShape3D" id="float_s"]\nsize = Vector3(0.9, 0.5, 0.5)')
        text = text.replace('[node name="Ghost" type="MeshInstance3D" parent="."]\n'
                            'transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, -4, 0.6, 4)\n'
                            'mesh = SubResource("ghost_m")\n', "")
        (proj / "scenes" / "audit.tscn").write_text(text, encoding="utf-8")
        drift = godot_audit.export_verify(str(proj), str(pck), "res://scenes/audit.tscn")
        assert drift["ok"] is False
        fields = {(d["node"], d["field"]) for d in drift["diffs"]}
        assert ("PropA", "exports.size") in fields, fields
        assert ("Floater/Shape", "shape_size") in fields, fields
        assert ("Ghost", "node") in fields, fields
        ghost = [d for d in drift["diffs"] if d["node"] == "Ghost"][0]
        assert ghost["editor"] is None and ghost["shipped"] == "MeshInstance3D"
        assert json.dumps(drift)  # serialisable for the MCP payload
