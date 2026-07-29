"""The talking-portrait sheet: registration, drift measurement, and the .tres.

No API calls here. Every test builds its frames with PIL, because the things
worth asserting are the ones that go wrong AFTER generation: a frame that came
back slightly the wrong size, a frame that came back the wrong colour, and a
crop that throws away the mouth.
"""
from __future__ import annotations

import pathlib

import pytest

from bgate_core import talkhead


def _face(path: pathlib.Path, *, size: int = 400, head: int = 200,
          colour: tuple[int, int, int] = (200, 160, 120),
          jaw: int = 0) -> str:
    """A head on transparency: a square 'head' with an optional 'jaw' below it.

    `jaw` extends the silhouette DOWNWARD only, which is exactly what an open
    mouth does to a real frame and is the case the width-registration rule
    exists for.
    """
    from PIL import Image

    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = im.load()
    x0 = (size - head) // 2
    y0 = 40
    for y in range(y0, y0 + head + jaw):
        for x in range(x0, x0 + head):
            px[x, y] = (*colour, 255)
    im.save(path)
    return str(path)


class TestDrift:
    def test_identical_frames_have_no_drift(self, tmp_path):
        frames = {n: _face(tmp_path / f"{n}.png") for n in ("rest", "half")}
        got = talkhead.drift(frames)
        assert got["rest"]["drift"] == 0.0
        assert got["half"]["ok"]

    def test_a_colour_shifted_frame_is_caught(self, tmp_path):
        """The real failure: three frames agree, the fourth comes back tinted.

        Invisible in a 128px cell, obvious as a flicker at 10fps. This is the
        whole reason the number exists.
        """
        frames = {
            "rest": _face(tmp_path / "rest.png"),
            "half": _face(tmp_path / "half.png", colour=(120, 140, 200)),
        }
        got = talkhead.drift(frames)
        assert not got["half"]["ok"]
        assert got["half"]["drift"] > talkhead.DRIFT_LIMIT
        assert got["worst"] == got["half"]["drift"]

    def test_a_small_shift_is_tolerated(self, tmp_path):
        frames = {
            "rest": _face(tmp_path / "rest.png"),
            "half": _face(tmp_path / "half.png", colour=(203, 162, 122)),
        }
        assert talkhead.drift(frames)["half"]["ok"]

    def test_an_unknown_anchor_is_refused(self, tmp_path):
        frames = {"rest": _face(tmp_path / "rest.png")}
        with pytest.raises(ValueError):
            talkhead.drift(frames, anchor="nope")


class TestSheet:
    def test_every_cell_is_square_and_the_same_size(self, tmp_path):
        from PIL import Image

        frames = [(n, _face(tmp_path / f"{n}.png")) for n in talkhead.MOUTHS]
        got = talkhead.sheet(frames, tmp_path / "sheet.png", cell=64)
        im = Image.open(got["path"])
        assert im.size == (64 * len(frames), 64)
        assert got["order"] == list(talkhead.MOUTHS)

    def test_an_open_jaw_does_not_shrink_the_head(self, tmp_path):
        """Registration is on WIDTH, and this is why.

        The 'wide' frame's silhouette is taller because the jaw dropped. Under
        height registration every talking frame would be scaled down to match,
        so the face would pulse smaller on every syllable. Width is unchanged by
        a jaw, so the scale factor must stay 1.
        """
        frames = [
            ("rest", _face(tmp_path / "rest.png")),
            ("wide", _face(tmp_path / "wide.png", jaw=60)),
        ]
        got = talkhead.sheet(frames, tmp_path / "sheet.png", cell=64)
        assert got["registration"]["wide"]["scale"] == pytest.approx(1.0, abs=0.01)

    def test_a_differently_scaled_frame_is_normalised(self, tmp_path):
        frames = [
            ("rest", _face(tmp_path / "rest.png", head=200)),
            ("half", _face(tmp_path / "half.png", head=160)),
        ]
        got = talkhead.sheet(frames, tmp_path / "sheet.png", cell=64)
        assert got["registration"]["half"]["scale"] == pytest.approx(1.25, abs=0.02)

    def test_the_crop_keeps_the_lower_face(self, tmp_path):
        """The bug this file was written after: the crop square was sized off
        the SOURCE IMAGE instead of the silhouette, so every cell was cut at the
        eyes and the mouth — the only animated part — was thrown away."""
        from PIL import Image

        frames = [("rest", _face(tmp_path / "rest.png", size=1024, head=900))]
        got = talkhead.sheet(frames, tmp_path / "sheet.png", cell=64)
        cell = Image.open(got["path"]).convert("RGBA")
        alpha = cell.getchannel("A")
        # The bottom row of the cell must still be inside the head.
        bottom = [alpha.getpixel((x, 63)) for x in range(20, 44)]
        assert max(bottom) > 200, "crop cut above the mouth"

    def test_an_empty_frame_is_refused(self, tmp_path):
        from PIL import Image

        blank = tmp_path / "blank.png"
        Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(blank)
        with pytest.raises(ValueError):
            talkhead.sheet([("rest", str(blank))], tmp_path / "s.png")


class TestSpriteFrames:
    def test_talk_loops_and_blink_does_not(self):
        tres = talkhead.spriteframes("cat_talk.png")
        talk = tres.split('&"talk"')[0]
        blink = tres.split('&"blink"')[0].split('&"talk"')[-1]
        assert '"loop": true' in talk
        assert '"loop": false' in blink

    def test_blink_is_not_in_the_talk_cycle(self):
        """A blink on every syllable is a twitch, not a character."""
        assert "blink" not in talkhead.TALK_CYCLE

    def test_the_cycle_returns_to_rest(self):
        """rest -> half -> wide -> half loops back into rest cleanly. A cycle
        ending on `wide` would snap the jaw shut between loops."""
        assert talkhead.TALK_CYCLE[0] == "rest"
        assert talkhead.TALK_CYCLE[-1] != "wide"

    def test_regions_step_one_cell_at_a_time(self):
        tres = talkhead.spriteframes("cat_talk.png", cell=128)
        regions = [l for l in tres.splitlines() if l.startswith("region =")]
        assert regions[0] == "region = Rect2(0, 0, 128, 128)"
        assert regions[1] == "region = Rect2(128, 0, 128, 128)"

    def test_the_texture_path_is_the_sheet(self):
        assert 'path="cat_talk.png"' in talkhead.spriteframes("cat_talk.png")


class TestPrompt:
    def test_every_mouth_has_a_prompt(self):
        for frame in talkhead.MOUTHS:
            assert talkhead.prompt_for("a cat.", frame)

    def test_an_unknown_frame_is_refused(self):
        with pytest.raises(ValueError):
            talkhead.prompt_for("a cat.", "smirk")

    def test_the_hold_clause_only_appears_with_an_anchor(self):
        """Without a reference there is nothing to hold identical to, and the
        clause would be instructing the model about an image it cannot see."""
        assert "Change ONLY" not in talkhead.prompt_for("a cat.", "rest")
        assert "Change ONLY" in talkhead.prompt_for("a cat.", "rest",
                                                    has_anchor=True)
