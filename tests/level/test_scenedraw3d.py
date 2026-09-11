"""The 3D draw list: transforms, primitives, instances, and the wiring around it.

Geometry pinned without a project on disk, the way scenedraw's tests pin the
2D maths: a scene string in, world matrices and shapes out. The two conventions
that were measured wrong on a real project (row-major Transform3D, YXZ euler)
each get a test that would fail the other way round.
"""
from __future__ import annotations

import math

import pytest

from bgate_core.level import scenedraw3d as d, scenewire

HEAD = '[gd_scene load_steps=6 format=3 uid="uid://x"]\n\n'


def _scene(body: str, subs: str = "") -> str:
    return HEAD + subs + '\n[node name="World" type="Node3D"]\n' + body


class TestMatrices:
    def test_transform3d_is_row_major(self):
        # A 90 degree yaw: rows [[0,0,1],[0,1,0],[-1,0,0]]. Applied to +X it
        # gives -Z. Reading the twelve numbers as columns gives +Z instead,
        # which is the inverse rotation.
        m = d.mat_from_transform3d("Transform3D(0, 0, 1, 0, 1, 0, -1, 0, 0, 5, 0, 0)")
        x = [m[i][0] for i in range(3)]
        assert [round(v, 6) for v in x] == [0, 0, -1]
        assert [m[i][3] for i in range(3)] == [5, 0, 0]

    def test_position_rotation_scale_compose_as_yxz(self):
        m = d.mat_from_trs((5, 0, 0), (0, math.pi / 2, 0), (1, 1, 1))
        got = d.mat_from_transform3d("Transform3D(0, 0, 1, 0, 1, 0, -1, 0, 0, 5, 0, 0)")
        for i in range(3):
            for j in range(4):
                assert abs(m[i][j] - got[i][j]) < 1e-9

    def test_decompose_round_trips(self):
        m = d.mat_from_trs((1, 2, 3), (0.3, -1.1, 0.7), (2, 2, 2))
        got = d.decompose(m)
        assert got["position"] == [1, 2, 3]
        assert [round(v, 3) for v in got["rotation"]] == [0.3, -1.1, 0.7]
        assert got["scale"] == [2, 2, 2]

    def test_children_inherit_the_parent_matrix(self):
        text = _scene(
            '[node name="Rig" type="Node3D" parent="."]\n'
            'position = Vector3(10, 0, 0)\n'
            'rotation = Vector3(0, 1.5707963, 0)\n'
            '[node name="Arm" type="Node3D" parent="Rig"]\n'
            'position = Vector3(1, 0, 0)\n')
        out = d.draw_list(text, read=lambda p: None, model_url_of=lambda p, kind="model": None)
        arm = next(i for i in out["items"] if i["path"] == "Rig/Arm")
        pos = [arm["world"][i][3] for i in range(3)]
        assert [round(v, 4) for v in pos] == [10, 0, -1]


class TestPrimitives:
    SUBS = ('[sub_resource type="BoxMesh" id="BoxMesh_a"]\nsize = Vector3(4, 1, 2)\n'
            '[sub_resource type="CylinderMesh" id="Cyl_a"]\n'
            '[sub_resource type="StandardMaterial3D" id="Mat_a"]\nalbedo_color = Color(1, 0, 0, 1)\n'
            '[sub_resource type="BoxShape3D" id="Shape_a"]\nsize = Vector3(4, 1, 2)\n')

    def test_meshes_shapes_and_materials_resolve(self):
        text = _scene(
            '[node name="Floor" type="MeshInstance3D" parent="."]\n'
            'mesh = SubResource("BoxMesh_a")\n'
            'surface_material_override/0 = SubResource("Mat_a")\n'
            '[node name="Post" type="MeshInstance3D" parent="."]\n'
            'mesh = SubResource("Cyl_a")\n'
            '[node name="Body" type="StaticBody3D" parent="."]\n'
            '[node name="Shape" type="CollisionShape3D" parent="Body"]\n'
            'shape = SubResource("Shape_a")\n'
            '[node name="Sun" type="DirectionalLight3D" parent="."]\n'
            '[node name="Cam" type="Camera3D" parent="."]\ncurrent = true\nfov = 60.0\n',
            subs=self.SUBS)
        out = d.draw_list(text, read=lambda p: None, model_url_of=lambda p, kind="model": None)
        by = {i["path"]: i for i in out["items"]}
        assert by["Floor"]["draw"] == {"mesh": "BoxMesh", "kind": "box", "size": [4, 1, 2],
                                       "color": [1.0, 0.0, 0.0, 1.0]}
        # Godot's defaults, not zeros: a CylinderMesh with no lines is r 0.5, h 2.
        assert by["Post"]["draw"]["kind"] == "cylinder"
        assert by["Post"]["draw"]["height"] == 2.0 and by["Post"]["draw"]["top_radius"] == 0.5
        assert by["Body/Shape"]["draw"]["wire"] is True and by["Body/Shape"]["role"] == "collision"
        assert by["Sun"]["role"] == "light" and by["Sun"]["draw"]["light"] == "directional"
        assert out["camera"]["path"] == "Cam" and out["camera"]["fov"] == 60.0
        assert out["dimension"] == "3d"
        assert out["bounds"]["min"][0] <= -2 and out["bounds"]["max"][0] >= 2

    def test_a_2d_scene_is_not_3d(self):
        text = HEAD + '[node name="Level" type="Node2D"]\n[node name="P" type="Sprite2D" parent="."]\n'
        assert d.is_3d_scene(text) is False
        assert scenewire.scene_dimension(text) == "2d"
        # A Node root holding a world still counts.
        text = HEAD + '[node name="Main" type="Node"]\n[node name="W" type="Node3D" parent="."]\n'
        assert d.is_3d_scene(text) is True


class TestInstances:
    def test_a_tscn_instance_is_opened_and_placed(self):
        prop = HEAD + '[node name="Crate" type="Node3D"]\n[node name="Mesh" type="MeshInstance3D" parent="."]\n'
        text = (HEAD + '[ext_resource type="PackedScene" path="res://crate.tscn" id="1_c"]\n'
                '\n[node name="World" type="Node3D"]\n'
                '[node name="Crate" parent="." instance=ExtResource("1_c")]\n'
                'position = Vector3(3, 0, 0)\n')
        out = d.draw_list(text, read=lambda p: prop if p == "res://crate.tscn" else None,
                          model_url_of=lambda p, kind="model": None)
        by = {i["path"]: i for i in out["items"]}
        assert by["Crate"]["spatial"] is True and by["Crate"]["local"]["position"] == [3, 0, 0]
        inner = by["Crate/Mesh"]
        assert inner["of"] == "Crate" and inner["editable"] is False
        assert inner["world"][0][3] == 3

    def test_a_model_instance_becomes_a_model_draw(self):
        text = (HEAD + '[ext_resource type="PackedScene" path="res://car.glb" id="1_c"]\n'
                '\n[node name="World" type="Node3D"]\n'
                '[node name="Car" parent="." instance=ExtResource("1_c")]\n'
                'transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, -4)\n')
        out = d.draw_list(text, read=lambda p: None,
                          model_url_of=lambda p, kind="model": "/api/model3d/raw/car.glb")
        car = out["items"][1]
        assert car["draw"] == {"kind": "model", "url": "/api/model3d/raw/car.glb", "path": "res://car.glb"}
        assert car["has_transform"] is True and car["world"][2][3] == -4


class TestWiring:
    def test_wire_picks_3d_node_types_for_a_3d_scene(self):
        text = _scene("")
        got = scenewire.wire(text, "res://art/hero.png")
        assert got["node_type"] == "Sprite3D"
        got = scenewire.wire(text, "res://models/car.glb")
        assert got["node_type"] == "(instance)" and 'instance=ExtResource' in got["text"]
        assert 'type="PackedScene" path="res://models/car.glb"' in got["text"]
        two_d = HEAD + '[node name="Level" type="Node2D"]\n'
        assert scenewire.wire(two_d, "res://art/hero.png")["node_type"] == "Sprite2D"
        with pytest.raises(scenewire.WireError, match="3D"):
            scenewire.wire(two_d, "res://models/car.glb")

    def test_3d_values_are_writable(self):
        text = _scene('[node name="Crate" type="Node3D" parent="."]\n')
        got = scenewire.set_property(text, "Crate", "transform",
                                     "Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 2.5, 0, -3)")
        assert "transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 2.5, 0, -3)" in got["text"]
        got = scenewire.set_property(text, "Crate", "position", "Vector3(1, 2, 3)")
        assert "position = Vector3(1, 2, 3)" in got["text"]

    def test_the_parse_cache_returns_the_same_object_for_the_same_text(self):
        text = _scene('[node name="A" type="Node3D" parent="."]\n')
        assert scenewire.parse(text) is scenewire.parse(text)
        assert scenewire.parse(text) is not scenewire.parse(text + "\n")


class TestArrayMesh:
    """The file's own geometry, decoded from what the editor serialised."""

    @staticmethod
    def _mesh(compressed: bool) -> str:
        import base64
        import struct
        # A unit right triangle in the XZ plane at y=0, plus one more point.
        verts = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 2.0), (2.0, 1.0, 2.0)]
        aabb = (0.0, 0.0, 0.0, 2.0, 1.0, 2.0)
        if compressed:
            fmt = (1 << 35) | (1 << 29) | 1
            raw = b"".join(struct.pack("<4H", round(v[0] / 2 * 65535), round(v[1] / 1 * 65535),
                                       round(v[2] / 2 * 65535), 0) for v in verts)
        else:
            fmt = (1 << 35) | 1 | (1 << 1)              # + normals, planar after
            raw = (b"".join(struct.pack("<3f", *v) for v in verts)
                   + b"\0\0\0\0" * len(verts))
        idx = struct.pack("<6H", 0, 1, 2, 1, 3, 2)
        return (f'[sub_resource type="ArrayMesh" id="ArrayMesh_t"]\n'
                f'_surfaces = [{{\n"aabb": AABB({", ".join(str(a) for a in aabb)}),\n'
                f'"format": {fmt},\n"index_count": 6,\n'
                f'"index_data": PackedByteArray("{base64.b64encode(idx).decode()}"),\n'
                f'"primitive": 3,\n"vertex_count": 4,\n'
                f'"vertex_data": PackedByteArray("{base64.b64encode(raw).decode()}")\n}}]\n\n')

    @pytest.mark.parametrize("compressed", [True, False])
    def test_positions_and_indices_decode(self, compressed):
        import struct
        text = _scene('[node name="Road" type="MeshInstance3D" parent="."]\n'
                      'mesh = SubResource("ArrayMesh_t")\n', subs=self._mesh(compressed))
        subs = d.sub_resources(text)
        assert subs["ArrayMesh_t"]["props"]["_aabb"] == [0.0, 0.0, 0.0, 2.0, 1.0, 2.0]
        got = d.mesh_data(text, "ArrayMesh_t")
        assert got["vertex_count"] == 4 and got["triangle_count"] == 2
        pos = struct.unpack("<12f", got["positions"])
        assert [round(v, 3) for v in pos[3:6]] == [2.0, 0.0, 0.0]
        assert [round(v, 3) for v in pos[9:12]] == [2.0, 1.0, 2.0]
        assert struct.unpack("<6I", got["indices"]) == (0, 1, 2, 1, 3, 2)
        # The draw list names the mesh by id and carries the box for the
        # placeholder, and the bounds are the mesh's own extent.
        out = d.draw_list(text, read=lambda p: None, model_url_of=lambda p, kind="model": None)
        road = out["items"][1]["draw"]
        assert road["kind"] == "arraymesh" and road["id"] == "ArrayMesh_t"
        assert out["bounds"]["max"] == [2.0, 1.0, 2.0]

    def test_a_mesh_that_is_not_there_is_none(self):
        text = _scene("")
        assert d.mesh_data(text, "ArrayMesh_nope") is None
