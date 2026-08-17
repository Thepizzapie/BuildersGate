"""Playable previews: the GIF writer that makes motion reviewable.

Everything here is pure Pillow; no Aseprite. What is being pinned: authored
timing lands per frame, loop flags are honoured, one palette serves the whole
GIF (a per-frame palette is how a conformed animation comes back FLICKERING
in the preview of all places), and a failed animation is absent rather than
fatal — the sheet shipped, the preview is decoration.
"""
from __future__ import annotations

import pytest

from bgate_core import animgif


def _frames(tmp_path, count=3, size=(16, 16)):
    from PIL import Image

    paths = []
    for i in range(count):
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        img.paste((200, 40 + i * 20, 40, 255), (2, 2, 14, 14))
        p = tmp_path / f"f{i}.png"
        img.save(p)
        paths.append(str(p))
    return paths


class TestWriteGif:
    def test_durations_land_per_frame(self, tmp_path):
        from PIL import Image

        out = tmp_path / "a.gif"
        got = animgif.write_gif(_frames(tmp_path), str(out),
                                durations=[125, 125, 250])
        assert got["ok"] is True and got["frames"] == 3
        with Image.open(out) as gif:
            durs = []
            for i in range(gif.n_frames):
                gif.seek(i)
                durs.append(gif.info["duration"])
        # GIF stores centiseconds — 125ms floors to 120. The format's own
        # granularity, not a writer bug; the .tres carries the exact number.
        assert durs == [120, 120, 250]

    def test_loop_flag_is_honoured(self, tmp_path):
        from PIL import Image

        looping = tmp_path / "loop.gif"
        once = tmp_path / "once.gif"
        animgif.write_gif(_frames(tmp_path), str(looping),
                          durations=[100] * 3, loop=True)
        animgif.write_gif(_frames(tmp_path), str(once),
                          durations=[100] * 3, loop=False)
        with Image.open(looping) as gif:
            assert gif.info.get("loop") == 0          # forever
        with Image.open(once) as gif:
            assert gif.info.get("loop", None) in (1, None)  # play once

    def test_transparency_survives(self, tmp_path):
        from PIL import Image

        out = tmp_path / "t.gif"
        animgif.write_gif(_frames(tmp_path), str(out), durations=[100] * 3)
        with Image.open(out) as gif:
            rgba = gif.convert("RGBA")
            assert rgba.getpixel((0, 0))[3] == 0, "corner should stay transparent"
            assert rgba.getpixel((8, 8))[3] == 255

    def test_a_missing_frame_reports_not_raises(self, tmp_path):
        got = animgif.write_gif([str(tmp_path / "ghost.png")],
                                str(tmp_path / "x.gif"), durations=[100])
        assert got["ok"] is False and "error" in got


class TestWriteGifs:
    def test_one_gif_per_animation_with_timing(self, tmp_path):
        paths = _frames(tmp_path, count=4)
        written = animgif.write_gifs(
            {"idle": paths[:2], "ko": paths[2:]},
            str(tmp_path / "out"), "hero",
            timing={"idle": {"fps": 4.0}}, fps=8.0, no_loop=("ko",))
        assert set(written) == {"idle", "ko"}
        from PIL import Image

        with Image.open(written["idle"]) as gif:
            gif.seek(0)
            assert gif.info["duration"] == 250        # 1 hold at 4fps
            assert gif.info.get("loop") == 0
        with Image.open(written["ko"]) as gif:
            assert gif.info.get("loop", None) in (1, None)  # NO_LOOP name

    def test_a_broken_animation_is_absent_not_fatal(self, tmp_path):
        paths = _frames(tmp_path, count=2)
        written = animgif.write_gifs(
            {"good": paths, "bad": [str(tmp_path / "ghost.png")], "empty": []},
            str(tmp_path / "out"), "hero")
        assert set(written) == {"good"}


class TestDurations:
    def test_holds_become_ms_at_the_animation_fps(self):
        assert animgif.durations_ms(
            3, {"holds": [1, 2, 1], "fps": 10.0}, 8.0) == [100, 200, 100]

    def test_short_frames_are_clamped_up_not_dropped(self):
        """Browsers clamp <2cs GIF frames to 10cs — clamping ourselves keeps
        the timing we asked for instead of one a renderer invents."""
        assert animgif.durations_ms(1, {"fps": 240.0}, 8.0) == [
            pytest.approx(animgif.MIN_FRAME_MS)]
