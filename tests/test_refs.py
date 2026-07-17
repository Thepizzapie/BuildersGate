"""Reference anchors + multi-frame animation grouping."""
from __future__ import annotations

from pathlib import Path

import pytest

from bgate_adapters import sprites
from bgate_core import refs, seats


@pytest.fixture()
def anchor(tmp_path):
    src = tmp_path / "approved_tommy.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"ref" * 20)
    return src


class TestRefPins:
    def test_pin_copies_into_project(self, root, anchor):
        got = refs.pin(root, "Tommy Reference", str(anchor), kind="character",
                       note="approved by user")
        assert got["name"] == "tommy-reference"
        pinned = Path(got["path"])
        assert pinned.exists()
        assert str(pinned).startswith(str(root))  # durable, inside the project
        # The pin survives the source dir being cleaned up.
        anchor.unlink()
        assert pinned.exists()

    def test_resolve_name_and_path_and_missing(self, root, anchor):
        refs.pin(root, "style-anchor", str(anchor))
        assert refs.resolve(root, "style-anchor") == refs.get(root, "style-anchor")["path"]
        assert refs.resolve(root, str(anchor)) == str(anchor)  # real path passes
        with pytest.raises(LookupError, match="pin it first"):
            refs.resolve(root, "never-pinned")

    def test_repin_upgrades_in_place(self, root, anchor, tmp_path):
        refs.pin(root, "anchor", str(anchor))
        better = tmp_path / "better.png"
        better.write_bytes(b"\x89PNG\r\n\x1a\n" + b"v2" * 40)
        got = refs.pin(root, "anchor", str(better), note="v2")
        assert Path(got["path"]).read_bytes().endswith(b"v2" * 40)
        assert len(refs.list_refs(root)) == 1

    def test_unpin_keeps_the_file(self, root, anchor):
        pinned = Path(refs.pin(root, "anchor", str(anchor))["path"])
        refs.unpin(root, "anchor")
        assert refs.list_refs(root) == []
        assert pinned.exists()  # deleting canon art is a human call

    def test_bad_kind_and_missing_file(self, root, anchor):
        with pytest.raises(ValueError, match="kind"):
            refs.pin(root, "x", str(anchor), kind="vibes")
        with pytest.raises(FileNotFoundError):
            refs.pin(root, "x", "C:/nope.png")

    def test_briefs_surface_the_pins(self, root, anchor):
        refs.pin(root, "tommy-ref", str(anchor), kind="character")
        brief = seats.brief(root, "art")
        assert brief["pinned_refs"][0]["name"] == "tommy-ref"


class TestMultiFrameGrouping:
    def test_grouping(self):
        got = sprites._group_frames(["idle", "walk/0", "walk/1", "walk/2", "jab/0", "jab/1"])
        assert got == [("idle", 1), ("walk", 3), ("jab", 2)]

    def test_from_pose_images_builds_multiframe_tres(self, tmp_path):
        from PIL import Image, ImageDraw

        def blob(name, shade):
            img = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((40, 60, 160, 280), fill=(shade, 60, 40, 255))
            p = tmp_path / f"{name.replace('/', '_')}.png"
            img.save(p)
            return (name, str(p))

        # Deliberately interleaved input — assembly must regroup contiguously.
        files = [blob("walk/0", 200), blob("idle", 120), blob("walk/1", 220)]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "out"),
                                       name="t", frame_size=(160, 240))
        assert got["ok"] is True
        assert got["animations"] == {"walk": 2, "idle": 1}

        tres = Path(got["tres"]).read_text(encoding="utf-8")
        # walk owns regions 0 and 1 (contiguous), idle region 2.
        assert '&"walk"' in tres and '&"idle"' in tres
        walk_block = tres.split('&"walk"')[0]
        assert 'SubResource("atlas_0")' in walk_block
        assert 'SubResource("atlas_1")' in walk_block
        # Sheet width = 3 frames.
        from PIL import Image as I
        assert I.open(got["sheet"]).size == (480, 240)

    def test_single_frame_names_unchanged(self, tmp_path):
        """Plain names keep the old one-anim-per-pose behavior exactly."""
        got = sprites._group_frames(["idle", "jab", "ko"])
        assert got == [("idle", 1), ("jab", 1), ("ko", 1)]
