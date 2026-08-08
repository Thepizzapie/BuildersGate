"""The keyer shipped an opaque pink slab and called it clean.

WHAT HAPPENED. The keyable-background contract asks the model for a flat
PURE-MAGENTA backdrop and keys it out by RGB distance. Krea ignores the exact
colour and paints a DUSTY pink — measured at ~#c0559f on real frames, which is
143 away from (255,0,255) and therefore outside the default tol=125 sphere. So
the interior never keyed.

WHY NOTHING CAUGHT IT is the part worth keeping. The audit has five
measurements and every one of them inspects THE CUT: the frame border, the soft
edge, the RGB under zero alpha, the enclosed holes. None inspects what the cut
left behind. The border happened to key — it sat closest to the contract colour —
so `border_opaque` read clean, and with the interior fully opaque there was no
soft edge, no dirty alpha and no enclosed transparency to fail on either. Five
green checks over a solid rectangle, and `clean: true` on the way out.

That is the same shape as `image_generate(tileable=True)` reporting success for a
mirror pass that threw: a gate that validates a PROXY for the artifact instead of
the artifact. So the assertions here are on pixels and on the specific flag, and
the controls are the subject colours that must SURVIVE — a test that only proves
the backdrop dies would be satisfied by a keyer that erases everything.
"""
from __future__ import annotations

import pytest

from bgate_core import chroma

Image = pytest.importorskip("PIL.Image")

MAGENTA = (255, 0, 255)
# What Krea actually painted, not what it was asked for.
DUSTY = (192, 85, 159)
# Subject colours from the same build. Each must come through the key intact.
LEAF = (60, 120, 50)
TWIG = (110, 80, 50)
PLUME = (220, 200, 160)
PETAL = (240, 150, 180)     # the closest legitimate colour to the backdrop


def _plate(path, backdrop, subject, *, size=64):
    """A frame: flat backdrop with a solid subject block in the middle."""
    im = Image.new("RGBA", (size, size), (*backdrop, 255))
    for y in range(size // 4, 3 * size // 4):
        for x in range(size // 4, 3 * size // 4):
            im.putpixel((x, y), (*subject, 255))
    im.save(path)
    return path


def _opaque_fraction(path):
    with Image.open(path) as im:
        data = list(im.convert("RGBA").getdata())
    return sum(1 for *_, a in data if a > 200) / len(data)


class TestPinkness:
    """The scalar the fix turns on, checked against the measured colours."""

    def test_the_contract_colour_is_maximally_pink(self):
        assert chroma.pinkness(MAGENTA) == 255

    def test_the_backdrop_that_shipped_is_still_clearly_pink(self):
        """~90 — far below a distance key's reach, far above every subject."""
        assert 85 < chroma.pinkness(DUSTY) < 95

    @pytest.mark.parametrize("rgb,name",
                             [(LEAF, "leaf"), (TWIG, "twig"), (PLUME, "plume")])
    def test_subject_colours_are_not_pink(self, rgb, name):
        assert chroma.pinkness(rgb) < 10, name

    def test_a_petal_sits_between_them_and_is_spared(self):
        """The tightest case, and the reason the threshold is a fraction rather
        than as low as it could go: a pink subject must survive a magenta key."""
        floor = chroma.pinkness(MAGENTA) * chroma.PINK_KEY_FRACTION
        assert chroma.pinkness(PETAL) < floor < chroma.pinkness(DUSTY)


class TestThePredicateIsExact:
    """The same rule is evaluated three ways and they must not disagree.

    Caught for real: the band version runs inside ImageMath on int32, where `/`
    FLOORS, so a pixel whose true pinkness is 84.5 tested as 84 there and as 84.5
    in the per-pixel paths. The two disagreed on exactly the colours sitting on
    the threshold — the worst possible place — and only on those, so it survived
    every test using a colour comfortably either side of it.
    """

    def test_a_half_step_pixel_is_decided_the_same_way_everywhere(self, tmp_path):
        """(229,102,144) has pinkness 84.5 against a floor of 84. Whichever way
        it is decided, the keyer and the audit have to agree."""
        half = (229, 102, 144)
        assert chroma.pinkness(half) == 84.5
        floor = int(chroma.pinkness(MAGENTA) * chroma.PINK_KEY_FRACTION)
        assert floor == 84

        plate = _plate(tmp_path / "edge.png", half, TWIG)
        chroma.finish(str(plate), MAGENTA, name="magenta")
        with Image.open(plate) as im:
            backdrop_keyed = im.convert("RGBA").getpixel((0, 0))[3] == 0
        residual = chroma.audit(str(plate), chroma=MAGENTA)["residual_chroma"]
        # Agreement is the assertion, not which way it went: keyed backdrop means
        # no residual, unkeyed means residual. Never one without the other.
        assert backdrop_keyed == (residual == 0.0)

    def test_the_predicate_keeps_the_half_step_instead_of_dropping_it(self):
        """Doubling both sides removes the rounding question rather than picking
        a side of it: 84.5 > 84 stays True, which is what the float definition
        always meant. The floored band version answered False — the two were only
        ever going to differ here, on the .5, which is why nothing caught it."""
        assert chroma.pinkness((229, 102, 144)) == 84.5
        assert chroma.is_pinker_than((229, 102, 144), 84) is True
        assert chroma.is_pinker_than((228, 102, 144), 84) is False   # 84.0 !> 84


class TestTheDustyBackdropIsActuallyKeyed:
    def test_the_frame_is_no_longer_a_slab(self, tmp_path):
        plate = _plate(tmp_path / "heron.png", DUSTY, PLUME)
        assert _opaque_fraction(plate) == 1.0        # the control: it starts solid
        chroma.finish(str(plate), MAGENTA, name="magenta")
        assert _opaque_fraction(plate) < 0.30        # only the subject survives

    def test_the_audit_now_passes_it(self, tmp_path):
        plate = _plate(tmp_path / "heron.png", DUSTY, PLUME)
        assert chroma.finish(str(plate), MAGENTA, name="magenta")["ok"] is True

    def test_a_pure_magenta_backdrop_still_keys(self, tmp_path):
        """Regression guard on the path that always worked — the pinkness pass
        is a UNION with the distance key, never a replacement."""
        plate = _plate(tmp_path / "fox.png", MAGENTA, TWIG)
        assert chroma.finish(str(plate), MAGENTA, name="magenta")["ok"] is True
        assert _opaque_fraction(plate) < 0.30


class TestTheSubjectSurvives:
    """Without these, a keyer that erased the whole frame would pass above."""

    @pytest.mark.parametrize("subject,name", [(LEAF, "leaf"), (TWIG, "twig"),
                                              (PLUME, "plume"), (PETAL, "petal")])
    def test_the_middle_is_still_there(self, tmp_path, subject, name):
        plate = _plate(tmp_path / f"{name}.png", DUSTY, subject)
        chroma.finish(str(plate), MAGENTA, name="magenta")
        with Image.open(plate) as im:
            assert im.convert("RGBA").getpixel((32, 32))[3] > 200, name

    def test_a_non_pink_key_is_left_alone(self, tmp_path):
        """The pinkness pass is gated on the chroma being a pink. A cyan key over
        a pink subject must not have the subject eaten by a pass that does not
        apply to it."""
        cyan = (0, 255, 255)
        plate = _plate(tmp_path / "petal.png", cyan, PETAL)
        chroma.finish(str(plate), cyan, name="cyan")
        with Image.open(plate) as im:
            assert im.convert("RGBA").getpixel((32, 32))[3] > 200


class TestTheAuditCatchesWhatTheKeyMisses:
    """Belt and braces. The keyer is now better; the audit must still refuse a
    slab that reaches it by any other route, because the next provider will
    invent a backdrop nobody predicted."""

    def test_an_unkeyed_slab_is_flagged(self, tmp_path):
        plate = _plate(tmp_path / "slab.png", DUSTY, PLUME)
        got = chroma.audit(str(plate), chroma=MAGENTA)
        assert got["clean"] is False
        assert any("unkeyed backdrop" in f for f in got["flags"])

    def test_the_old_border_check_alone_would_have_passed_it(self, tmp_path):
        """Names the exact hole: every pre-existing measurement reads clean on
        the slab. If this ever starts failing, the new flag is redundant — but
        it is not, and this is the proof."""
        plate = _plate(tmp_path / "slab.png", DUSTY, PLUME)
        for x in range(64):                     # key just the border, as krea did
            for y in (0, 63):
                pass
        with Image.open(plate) as im:
            im = im.convert("RGBA")
            for i in range(64):
                for x, y in ((i, 0), (i, 63), (0, i), (63, i)):
                    im.putpixel((x, y), (0, 0, 0, 0))
            im.save(plate)
        got = chroma.audit(str(plate))          # no chroma: the old behaviour
        assert got["clean"] is True             # ...and it says the slab is fine
        assert got["residual_chroma"] is None   # because it never looked

    def test_not_checked_and_checked_clean_do_not_look_the_same(self, tmp_path):
        plate = _plate(tmp_path / "ok.png", MAGENTA, TWIG)
        chroma.finish(str(plate), MAGENTA, name="magenta")
        assert chroma.audit(str(plate))["residual_chroma"] is None
        assert chroma.audit(str(plate), chroma=MAGENTA)["residual_chroma"] == 0.0
