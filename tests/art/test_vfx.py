"""Deriving an effect animation from one key frame.

The bug this closes: effect animations were being bought from an image model as
a grid of N frames, which returns N INDEPENDENT DRAWINGS rather than an
animation. Shipped examples from one project — a mug that shatters over three
frames and is intact again in the fourth; a cloud whose palette pops between
frames 2 and 3; a "fading" burst that ends at full opacity; a trail whose frames
point in different directions. None of those are promptable away, because
identity over time is not something a text-to-image model holds.

So the tests here are mostly about IDENTITY AND CONTINUITY, not about pixels
being pretty — that is what a human looking at the sheet is for.
"""
from __future__ import annotations

import pytest

from bgate_core.art import vfx


def _key(tmp_path, size=48, blobs=1, colour=(240, 180, 90, 255)):
    """A key frame: one solid square, or `blobs` separate ones."""
    from PIL import Image

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    if blobs == 1:
        for y in range(size // 4, 3 * size // 4):
            for x in range(size // 4, 3 * size // 4):
                px[x, y] = colour
    else:
        step = size // (blobs + 1)
        for b in range(blobs):
            cx = step * (b + 1)
            for y in range(size // 2 - 4, size // 2 + 4):
                for x in range(cx - 4, cx + 4):
                    if 0 <= x < size and 0 <= y < size:
                        px[x, y] = colour
    path = tmp_path / f"key_{blobs}.png"
    img.save(path)
    return str(path)


class TestDeliverable:
    def test_it_emits_a_sheet_and_spriteframes(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", frames=4)
        assert r["ok"]
        assert r["sheet"].endswith("fx_sheet.png")
        assert r["tres"].endswith("fx_frames.tres")
        assert len(r["frames"]) == 4

    def test_the_tres_is_a_godot_resource_over_the_sheet(self, tmp_path):
        from pathlib import Path

        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", frames=3,
                        res_dir="assets/vfx")
        text = Path(r["tres"]).read_text(encoding="utf-8")
        assert 'type="SpriteFrames"' in text
        assert "res://assets/vfx/fx_sheet.png" in text
        assert text.count('type="AtlasTexture"') == 3

    def test_a_missing_key_frame_is_a_result_not_a_crash(self, tmp_path):
        r = vfx.animate(str(tmp_path / "nope.png"), str(tmp_path), "fx")
        assert not r["ok"] and "no key frame" in r["error"]

    def test_an_empty_key_frame_is_refused(self, tmp_path):
        from PIL import Image

        p = tmp_path / "blank.png"
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(p)
        r = vfx.animate(str(p), str(tmp_path), "fx")
        assert not r["ok"] and "transparent" in r["error"]

    def test_an_unknown_motion_is_refused_by_name(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="wobble")
        assert not r["ok"] and "wobble" in r["error"]


class TestRegistration:
    """Every frame anchors to the same pixel — the property that lets an effect
    stack on the projectile it belongs to without anyone computing an offset,
    and the one a per-frame alpha trim destroys."""

    def test_every_frame_is_the_cell_size(self, tmp_path):
        from PIL import Image

        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", frames=4,
                        cell=(64, 64))
        for p in r["frames"]:
            assert Image.open(p).size == (64, 64)

    def test_the_anchor_is_the_cell_centre(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", cell=(80, 80))
        assert r["anchor"] == [40, 40]

    def test_a_growing_effect_stays_centred(self, tmp_path):
        """A burst that grows must grow about its own centre. If it walked, the
        impact would drift off the body it went off against."""
        from PIL import Image

        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="burst",
                        frames=4, cell=(64, 64))
        for p in r["frames"][:3]:
            box = Image.open(p).getbbox()
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            assert abs(cx - 32) <= 3, f"{p} drifted horizontally to {cx}"
            assert abs(cy - 32) <= 3, f"{p} drifted vertically to {cy}"


class TestContinuity:
    """The whole reason this module exists rather than a prompt."""

    def test_the_palette_cannot_drift(self, tmp_path):
        """No frame may contain a colour the key frame did not. This is free
        here and impossible to guarantee from a model — a generated set had a
        cloud go white-and-orange in frames 1-2 and clean white in 3-4."""
        from PIL import Image

        key_path = _key(tmp_path)
        allowed = {c for c in Image.open(key_path).convert("RGBA").getdata()
                   if c[3] > vfx.ALPHA_FLOOR}
        r = vfx.animate(key_path, str(tmp_path), "fx", frames=4)
        for p in r["frames"]:
            got = {c for c in Image.open(p).convert("RGBA").getdata()
                   if c[3] > vfx.ALPHA_FLOOR}
            assert got <= allowed, f"{p} invented a colour"

    def test_a_decaying_effect_never_comes_back(self, tmp_path):
        """Monotonic decay after the peak. The mug that reassembled in frame 4
        is the failure being excluded."""
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="burst",
                        frames=5, peak=1)
        tail = r["coverage"][1:]
        assert tail[-1] < tail[0] * 0.5, r["coverage"]

    def test_it_ends_close_to_gone(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="burst",
                        frames=4)
        assert r["coverage"][-1] < 0.12, r["coverage"]

    def test_the_dissolve_is_a_subset_not_a_reshuffle(self, tmp_path):
        """A pixel that has gone stays gone. Otherwise the effect boils instead
        of thinning — the reason the fade is a stable ordered dissolve and not
        a random or per-frame threshold."""
        from PIL import Image

        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="churn",
                        frames=3, overrides={"fade": 0.4, "jitter": 0})
        alive = [{i for i, c in enumerate(Image.open(p).convert("RGBA").getdata())
                  if c[3] > vfx.ALPHA_FLOOR} for p in r["frames"]]
        assert alive[2] <= alive[1] <= alive[0]

    def test_alpha_stays_binary(self, tmp_path):
        """The fade is a dissolve, not an opacity ramp: half-transparent pixels
        read as a smudge against hard-edged art and trip the alpha audit's
        soft-alpha check."""
        from PIL import Image

        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", frames=4)
        for p in r["frames"]:
            for c in Image.open(p).convert("RGBA").getdata():
                assert c[3] == 0 or c[3] == 255, f"{p} has partial alpha {c[3]}"


class TestMotions:
    def test_a_loop_holds_its_size_and_never_fades(self, tmp_path):
        """A lingering hazard must stay put and stay the same while it is alive,
        or it pulses every time the animation wraps."""
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="churn",
                        frames=4)
        assert r["loop"] is True
        assert max(r["coverage"]) - min(r["coverage"]) < 0.02, r["coverage"]

    def test_a_spread_holds_its_last_frame(self, tmp_path):
        """A puddle does not evaporate while you are looking at it."""
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="spread",
                        frames=4)
        assert r["coverage"][-1] >= r["coverage"][0]
        assert r["loop"] is False

    def test_a_streak_shortens(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="streak",
                        frames=4)
        assert r["coverage"][-1] < r["coverage"][0]

    def test_loop_can_be_forced_against_the_motion(self, tmp_path):
        r = vfx.animate(_key(tmp_path), str(tmp_path), "fx", motion="burst",
                        frames=3, loop=True)
        assert r["loop"] is True

    def test_overrides_tune_without_a_new_motion(self, tmp_path):
        wide = vfx.animate(_key(tmp_path), str(tmp_path), "wide", frames=3,
                           overrides={"fade": 1.0})
        assert wide["coverage"][-1] > 0.2


class TestHonesty:
    """The result has to say what it could not do, or a caller reads silence as
    success."""

    def test_it_says_when_there_was_nothing_to_scatter(self, tmp_path):
        """`scatter` moves an effect's separate parts. A key frame drawn as one
        solid mass has one part, so the motion silently only grows it — which
        looks exactly like the tool working."""
        r = vfx.animate(_key(tmp_path, blobs=1), str(tmp_path), "fx",
                        motion="burst", frames=4)
        assert r["parts"] == 1
        assert any("ONE connected" in n for n in r["notes"])

    def test_it_stays_quiet_when_the_key_frame_is_in_pieces(self, tmp_path):
        r = vfx.animate(_key(tmp_path, blobs=3), str(tmp_path), "fx",
                        motion="burst", frames=4)
        assert r["parts"] == 3
        assert not any("ONE connected" in n for n in r["notes"])

    def test_parts_really_do_separate(self, tmp_path):
        """The claim `scatter` makes, measured against a control.

        Compared against ITS OWN no-scatter twin at the same frame, not against
        an earlier frame: the dissolve eats the outermost pixels, so a decaying
        effect's bounding box shrinks over time whether its parts flew apart or
        not. The first cut of this test compared frame 2 to frame 0 and failed
        for that reason — it was measuring the fade, not the scatter.
        """
        from PIL import Image

        key = _key(tmp_path, blobs=3)
        common = dict(motion="burst", frames=4, peak=0, cell=(96, 96))
        flew = vfx.animate(key, str(tmp_path), "flew", **common)
        held = vfx.animate(key, str(tmp_path), "held",
                           overrides={"scatter": 0.0}, **common)
        a = Image.open(flew["frames"][2]).getbbox()
        b = Image.open(held["frames"][2]).getbbox()
        assert (a[2] - a[0]) > (b[2] - b[0]), "scatter did not push parts apart"


class TestArtPixel:
    """The dissolve has to know how big an apparent pixel is, or it punches
    holes through the chunky blocks instead of removing them."""

    @pytest.mark.parametrize("scale", [1, 2, 4])
    def test_it_measures_the_block_size(self, tmp_path, scale):
        from PIL import Image

        small = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        px = small.load()
        for y in range(4, 12):
            for x in range(4, 12):
                px[x, y] = (200, 120, 60, 255) if (x + y) % 2 else (90, 60, 30, 255)
        big = small.resize((16 * scale, 16 * scale), Image.NEAREST)
        assert vfx.art_pixel(big) == scale
