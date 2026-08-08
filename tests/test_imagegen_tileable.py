"""make_tileable actually tiles, and says so honestly when it does not.

THE DEFECT. ``Image.save`` dispatches on the destination path's SUFFIX. A caller
naming its output ``litter_albedo`` rather than ``litter_albedo.png`` — which is
what an agent passes roughly half the time, and which nothing in the tool
signature forbids — produced ``ValueError: unknown file extension:`` inside a
swallowing try, so the mirror pass never ran and the caller got a texture back.
The evidence arrived days later as a visible seam at 2.4m tiling across a
full-screen terrain floor.

THE SHAPE OF THE BUG IS THE POINT, and it recurs across this codebase: the
success flag was computed from the REQUEST, never from the artifact. The same
week, the alpha keyer returned ``clean: true`` while shipping an opaque slab,
because it validated the frame border rather than the interior. A tool that
reports its own success on the strength of what it was asked to do is not
evidence, and neither of these would have been caught by a test that only ever
passes a well-formed path.

So the control here is a path with no suffix, and the assertion is on PIXELS —
whether the left column actually mirrors the right — not on the returned flag.
"""
from __future__ import annotations

import shutil

import pytest

from bgate_adapters.imagegen import make_tileable

Image = pytest.importorskip("PIL.Image")


def _plate(path, w=16, h=16, fmt=None):
    """A plate with a strong LEFT-RIGHT GRADIENT — the thing a mirror pass has
    to visibly change. A flat colour tiles perfectly before and after, so it
    could not tell a working pass from a no-op."""
    im = Image.new("RGB", (w, h))
    for x in range(w):
        for y in range(h):
            im.putpixel((x, y), (x * (255 // max(1, w - 1)), 40, 90))
    im.save(path, format=fmt)
    return path


def _seams(path):
    """Mean worst-channel gap between the left edge and the right edge — what
    you would see as a hard line where the tile repeats. A mirrored tile joins
    by construction, so this collapses to ~0. Worst channel rather than the
    average of three, because a seam in one channel is a visible seam and
    averaging it against two clean ones is how a real gap reads as small."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        return sum(max(abs(a - b) for a, b in zip(im.getpixel((0, y)),
                                                  im.getpixel((w - 1, y))))
                   for y in range(h)) / h


class TestTheSuffixlessPath:
    """The exact call that failed in the field."""

    def test_it_reports_success(self, tmp_path):
        src = _plate(tmp_path / "plate.png")
        target = tmp_path / "litter_albedo"          # no extension, on purpose
        shutil.copy(src, target)
        assert make_tileable(str(target))["ok"] is True

    def test_the_pixels_actually_changed(self, tmp_path):
        """The assertion that matters. `ok: true` is the flag that lied before —
        the proof is the seam measurement, and it must fall to nothing."""
        src = _plate(tmp_path / "plate.png")
        target = tmp_path / "litter_albedo"
        shutil.copy(src, target)
        before = _seams(target)
        make_tileable(str(target))
        after = _seams(target)
        assert before > 200          # the control: the plate really did seam
        assert after < 2             # and now it does not

    def test_a_suffixed_path_is_unaffected(self, tmp_path):
        """Regression guard on the path that always worked."""
        target = _plate(tmp_path / "scree.png")
        assert make_tileable(str(target))["ok"] is True
        assert _seams(target) < 2


class TestItStillFailsHonestly:
    def test_a_file_that_is_not_an_image_is_reported_not_raised(self, tmp_path):
        """A texture that failed to tile is still a texture — this must never
        raise into a generation that already cost money."""
        junk = tmp_path / "notanimage.png"
        junk.write_bytes(b"not a png at all")
        out = make_tileable(str(junk))
        assert out["ok"] is False
        assert "could not mirror-tile" in out["note"]

    def test_a_missing_file_is_reported_not_raised(self, tmp_path):
        assert make_tileable(str(tmp_path / "nope.png"))["ok"] is False


class TestFormatIsCarried:
    def test_a_jpeg_stays_a_jpeg(self, tmp_path):
        """The source knows what it is even when the destination path does not.
        Writing PNG bytes into a .jpg the engine imports by extension is the
        same class of silent breakage this fix exists to end."""
        target = tmp_path / "grain"
        _plate(target, fmt="JPEG")
        assert make_tileable(str(target))["ok"] is True
        with Image.open(target) as im:
            assert im.format == "JPEG"
