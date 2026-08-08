"""Godot 4.7 downgrades glTF alphaMode:MASK, silently, and it costs frames.

THE TRAP. `MASK` in glTF means "hard cutout" and the engine-side equivalent is
ALPHA_SCISSOR — an opaque-pass decision resolved per fragment. Godot 4.7 imports
it as DEPTH_PRE_PASS instead, which puts the surface in the SORTED TRANSPARENT
pass. Nothing errors. Nothing is logged. A screenshot of one tree looks
identical either way.

What changes is a forest. Hundreds of alpha quads that should have been opaque
cutouts are now depth-sorted every frame and cannot early-z against each other,
so the symptom is a framerate cliff with no error to grep for and no visual tell
— which is the worst possible shape for a bug, and the reason this is worth
detecting at import time rather than leaving to a profiler.

Reported, not corrected: whether a MASK surface wants scissor (foliage) or the
transparent pass (genuinely translucent) is not knowable from the file.

The parser is tested against bytes rather than a fixture .glb because the point
is that reading the JSON chunk needs no glTF library and no Blender — this runs
on the import path and must not grow a dependency in order to warn.
"""
from __future__ import annotations

import json
import struct


from bgate_adapters import godot


def _gltf_doc(*alpha_modes):
    return {"asset": {"version": "2.0"},
            "materials": [{"name": f"mat{i}", **({"alphaMode": m} if m else {})}
                          for i, m in enumerate(alpha_modes)]}


def _write_glb(path, doc):
    """A minimal but REAL .glb: 12-byte header, then the JSON chunk."""
    blob = json.dumps(doc).encode("utf-8")
    blob += b" " * ((4 - len(blob) % 4) % 4)          # chunks are 4-byte aligned
    header = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(blob))
    path.write_bytes(header + struct.pack("<I", len(blob)) + b"JSON" + blob)
    return path


class TestReadingTheJsonChunk:
    def test_a_glb_is_parsed_without_a_gltf_library(self, tmp_path):
        doc = _gltf_doc("OPAQUE")
        got = godot.gltf_json(_write_glb(tmp_path / "tree.glb", doc))
        assert got["materials"][0]["name"] == "mat0"

    def test_a_plain_gltf_is_parsed_too(self, tmp_path):
        p = tmp_path / "tree.gltf"
        p.write_text(json.dumps(_gltf_doc("MASK")), encoding="utf-8")
        assert godot.gltf_json(p)["materials"][0]["alphaMode"] == "MASK"

    def test_a_non_gltf_file_is_none_not_an_error(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("hello", encoding="utf-8")
        assert godot.gltf_json(p) is None

    def test_a_truncated_glb_is_none_not_an_exception(self, tmp_path):
        """A missing warning is a far smaller problem than an import that fails
        because the warner threw."""
        p = tmp_path / "broken.glb"
        p.write_bytes(b"glTF" + b"\x00" * 6)
        assert godot.gltf_json(p) is None

    def test_a_missing_file_is_none(self, tmp_path):
        assert godot.gltf_json(tmp_path / "nope.glb") is None


class TestTheWarning:
    def test_mask_is_named_with_the_material(self, tmp_path):
        got = godot.alpha_mode_report(
            _write_glb(tmp_path / "tree.glb", _gltf_doc("MASK")))
        assert got["masked_materials"] == ["mat0"]
        assert "mat0" in got["warning"]

    def test_it_names_what_godot_actually_does(self, tmp_path):
        """The whole value is the specific pair of words. "Check your alpha
        settings" would send someone to the same afternoon this prevents."""
        got = godot.alpha_mode_report(
            _write_glb(tmp_path / "tree.glb", _gltf_doc("MASK")))
        assert "DEPTH_PRE_PASS" in got["warning"]
        assert "ALPHA_SCISSOR" in got["warning"]

    def test_opaque_and_blend_are_left_alone(self, tmp_path):
        """The control. A warning on every material is a warning nobody reads."""
        got = godot.alpha_mode_report(
            _write_glb(tmp_path / "rock.glb", _gltf_doc("OPAQUE", "BLEND", None)))
        assert got["masked_materials"] == []
        assert got["warning"] == ""

    def test_only_the_masked_ones_are_listed_in_a_mixed_file(self, tmp_path):
        doc = {"asset": {"version": "2.0"}, "materials": [
            {"name": "bark", "alphaMode": "OPAQUE"},
            {"name": "leaves", "alphaMode": "MASK"},
            {"name": "glass", "alphaMode": "BLEND"},
        ]}
        got = godot.alpha_mode_report(_write_glb(tmp_path / "tree.glb", doc))
        assert got["masked_materials"] == ["leaves"]

    def test_a_long_list_is_summarised_rather_than_dumped(self, tmp_path):
        got = godot.alpha_mode_report(
            _write_glb(tmp_path / "forest.glb", _gltf_doc(*(["MASK"] * 9))))
        assert len(got["masked_materials"]) == 9
        assert "+3 more" in got["warning"]

    def test_checked_and_clean_does_not_look_like_not_checked(self, tmp_path):
        """A glTF with no MASK returns a dict; a .obj returns None. Collapsing
        those is how a check gets quietly skipped for a whole file type."""
        clean = godot.alpha_mode_report(
            _write_glb(tmp_path / "rock.glb", _gltf_doc("OPAQUE")))
        p = tmp_path / "rock.obj"
        p.write_text("v 0 0 0", encoding="utf-8")
        assert clean is not None and clean["masked_materials"] == []
        assert godot.alpha_mode_report(p) is None
