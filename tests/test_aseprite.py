"""The Aseprite leg: discovery, the exact-JSON .tres, and the real CLI.

Two kinds of test here. The pure ones (discovery rules, the asejson
converter, the anim-spec encoding) run everywhere and pin the contracts that
do not need Aseprite to be true. The ones marked ``requires_aseprite`` drive
the real binary — conform, master, export — because the Lua surface is the
part most worth checking against an actual install and no stub would have
caught the tag-range shift that saveAs-then-tag produced on 1.3.18.
"""
from __future__ import annotations


import pytest

from bgate_adapters import aseprite
from bgate_core import asejson

_no_aseprite = pytest.mark.skipif(
    not aseprite.available().get("available"),
    reason="needs a real Aseprite")


def requires_aseprite(obj):
    """Slow as well as skipped-when-missing, same shape as the blender suites."""
    return pytest.mark.slow(_no_aseprite(obj))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_a_broken_override_refuses_rather_than_falling_through(
            self, monkeypatch, tmp_path):
        """Falling through hands back the very binary the override existed to
        avoid, and reports success. Same rule as toolbin.resolve."""
        monkeypatch.setenv("BGATE_ASEPRITE", str(tmp_path / "nope.exe"))
        with pytest.raises(aseprite.AsepriteNotFound, match="missing file"):
            aseprite.find_aseprite()

    def test_an_override_that_exists_wins_over_everything(
            self, monkeypatch, tmp_path):
        fake = tmp_path / "aseprite.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("BGATE_ASEPRITE", str(fake))
        assert aseprite.find_aseprite() == str(fake)

    def test_available_reports_rather_than_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BGATE_ASEPRITE", str(tmp_path / "nope.exe"))
        probe = aseprite.available()
        assert probe["available"] is False and "missing file" in probe["reason"]

    def test_version_parsing(self):
        assert aseprite.parsed_version("Aseprite 1.3.18.2-x64") == (1, 3, 18, 2)
        assert aseprite.parsed_version("garbage") == ()


# ---------------------------------------------------------------------------
# The anim-spec encoding master() sends to Lua
# ---------------------------------------------------------------------------
class TestMasterSpec:
    def test_an_animation_with_no_frames_is_refused(self):
        with pytest.raises(aseprite.AsepriteError, match="no frames"):
            aseprite.master("s.png", "o.aseprite", cell=(8, 8),
                            anims=[{"name": "idle", "durations_ms": []}])

    def test_a_name_that_breaks_the_wire_format_is_refused(self):
        """':' and '|' are the encoding's own separators — a name carrying one
        would silently truncate its neighbour's durations."""
        with pytest.raises(aseprite.AsepriteError, match="cannot carry"):
            aseprite.master("s.png", "o.aseprite", cell=(8, 8),
                            anims=[{"name": "a:b", "durations_ms": [100]}])


# ---------------------------------------------------------------------------
# Export JSON -> SpriteFrames
# ---------------------------------------------------------------------------
def _export(frames, tags):
    return {"frames": [{"filename": str(i), "frame": rect, "duration": dur}
                       for i, (rect, dur) in enumerate(frames)],
            "meta": {"frameTags": tags}}


_RECTS = [({"x": 0, "y": 0, "w": 16, "h": 24}, 125),
          ({"x": 16, "y": 0, "w": 16, "h": 24}, 125),
          ({"x": 32, "y": 0, "w": 16, "h": 24}, 250)]


class TestAseJson:
    def test_regions_come_from_the_json_not_a_grid_guess(self):
        text = asejson.spriteframes_text(
            _export(_RECTS, [{"name": "idle", "from": 0, "to": 2}]),
            "x_sheet.png", "assets/sprites")
        assert "region = Rect2(16, 0, 16, 24)" in text
        assert 'path="res://assets/sprites/x_sheet.png"' in text

    def test_authored_timing_survives_exactly(self):
        """[125, 125, 250]ms is speed 8 with holds [1, 1, 2] — the smallest
        integer holds that reproduce the authored timing."""
        text = asejson.spriteframes_text(
            _export(_RECTS, [{"name": "idle", "from": 0, "to": 2}]),
            "x_sheet.png", "a")
        assert '"speed": 8.0' in text
        assert text.count('"duration": 1.0') == 2
        assert text.count('"duration": 2.0') == 1

    def test_a_play_once_tag_does_not_loop(self):
        text = asejson.spriteframes_text(
            _export(_RECTS, [{"name": "ko", "from": 0, "to": 2, "repeat": "1"}]),
            "x.png", "a")
        assert '"loop": false' in text

    def test_pingpong_is_baked_into_the_frame_list(self):
        """Godot has no ping-pong loop mode, so 0,1,2 plays as 0,1,2,1 — same
        bake sprites.py does for animspec ping-pong."""
        text = asejson.spriteframes_text(
            _export(_RECTS, [{"name": "float", "from": 0, "to": 2,
                              "direction": "pingpong"}]),
            "x.png", "a")
        assert text.count('SubResource("atlas_1")') == 2

    def test_no_tags_becomes_one_default_animation(self):
        text = asejson.spriteframes_text(_export(_RECTS, []), "x.png", "a")
        assert '&"default"' in text and '"loop": true' in text

    def test_untagged_frames_are_not_shipped(self):
        """A scratch frame in the master exists, but was never part of any
        animation — inventing an animation for it would ship it."""
        text = asejson.spriteframes_text(
            _export(_RECTS, [{"name": "idle", "from": 0, "to": 1}]),
            "x.png", "a")
        assert "atlas_2" not in text and "Rect2(32" not in text

    def test_byte_stable(self):
        data = _export(_RECTS, [{"name": "idle", "from": 0, "to": 2}])
        assert (asejson.spriteframes_text(data, "x.png", "a")
                == asejson.spriteframes_text(data, "x.png", "a"))

    def test_a_tag_past_the_last_frame_is_refused(self):
        with pytest.raises(asejson.AseJsonError, match="disagree"):
            asejson.spriteframes_text(
                _export(_RECTS, [{"name": "idle", "from": 0, "to": 9}]),
                "x.png", "a")

    def test_the_hash_format_is_refused_with_the_fix_named(self):
        with pytest.raises(asejson.AseJsonError, match="json-array"):
            asejson.spriteframes_text({"frames": {"a": {}}}, "x.png", "a")


# ---------------------------------------------------------------------------
# The durations image_sprites hands to master()
# ---------------------------------------------------------------------------
class TestAnimSpecs:
    def test_holds_become_milliseconds_at_the_animation_fps(self):
        from bgate_mcp.server import _ase_anim_specs
        [spec] = _ase_anim_specs(
            {"attack": 3}, {"attack": {"holds": [1, 2, 1], "fps": 10.0,
                                       "loop": False}}, 8.0)
        assert spec["durations_ms"] == [100, 200, 100]
        assert spec["loop"] is False

    def test_the_no_loop_names_hold_without_timing(self):
        from bgate_mcp.server import _ase_anim_specs
        specs = _ase_anim_specs({"idle": 2, "ko": 2}, None, 8.0)
        by_name = {s["name"]: s for s in specs}
        assert by_name["idle"]["loop"] is True
        assert by_name["ko"]["loop"] is False
        assert by_name["idle"]["durations_ms"] == [125, 125]


# ---------------------------------------------------------------------------
# The real binary
# ---------------------------------------------------------------------------
@requires_aseprite
class TestRealAseprite:
    def _sheet(self, tmp_path, cw=8, ch=8, frames=4):
        """A tiny gradient-noise sheet: many colours, some transparency."""
        from PIL import Image
        img = Image.new("RGBA", (cw * frames, ch), (0, 0, 0, 0))
        px = img.load()
        for x in range(cw * frames):
            for y in range(ch):
                if (x + y) % 5 == 0:
                    continue                     # keep holes transparent
                px[x, y] = (x * 7 % 256, y * 31 % 256, (x + y) * 13 % 256, 255)
        p = tmp_path / "sheet.png"
        img.save(p)
        return p

    def test_conform_reduces_colours_and_keeps_alpha(self, tmp_path):
        from PIL import Image
        src = self._sheet(tmp_path)
        out = tmp_path / "conformed.png"
        got = aseprite.conform(str(src), str(out), max_colors=8)
        assert got["ok"] and got["colors"] <= 8
        before = Image.open(src).convert("RGBA")
        after = Image.open(out).convert("RGBA")
        assert after.size == before.size
        holes = [i for i, p in enumerate(before.getdata()) if p[3] == 0]
        data = list(after.getdata())
        assert holes and all(data[i][3] == 0 for i in holes)

    def test_conform_to_a_fixed_palette_uses_only_it(self, tmp_path):
        from PIL import Image
        src = self._sheet(tmp_path)
        out = tmp_path / "locked.png"
        palette = [(10, 10, 10), (200, 200, 200)]
        got = aseprite.conform(str(src), str(out), palette=palette)
        assert got["ok"]
        opaque = {p[:3] for p in Image.open(out).convert("RGBA").getdata()
                  if p[3] > 0}
        assert opaque <= set(palette)

    def test_master_export_round_trip_is_exact(self, tmp_path):
        """The whole reason this integration exists: what goes in as timing
        comes back as the same rects, durations and tags — with tag ranges
        CORRECT, which is the regression the frames-first Lua build fixed."""
        src = self._sheet(tmp_path, frames=5)
        master = tmp_path / "m.aseprite"
        got = aseprite.master(
            str(src), str(master), cell=(8, 8),
            anims=[{"name": "idle", "durations_ms": [125, 125], "loop": True},
                   {"name": "hit", "durations_ms": [80, 160, 240], "loop": False}])
        assert got["ok"] and got["frames"] == 5 and got["tags"] == 2

        data = aseprite.export(str(master), str(tmp_path / "out.png"),
                               str(tmp_path / "out.json"))
        durs = [f["duration"] for f in data["frames"]]
        assert durs == [125, 125, 80, 160, 240]
        tags = {t["name"]: t for t in data["meta"]["frameTags"]}
        assert (tags["idle"]["from"], tags["idle"]["to"]) == (0, 1)
        assert (tags["hit"]["from"], tags["hit"]["to"]) == (2, 4)
        assert str(tags["hit"].get("repeat")) == "1"

        text = asejson.spriteframes_text(data, "out.png", "assets/sprites")
        assert '"speed": 12.5' in text          # gcd(80,160,240)=80
        assert '"loop": false' in text and '&"idle"' in text
