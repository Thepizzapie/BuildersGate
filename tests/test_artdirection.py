"""The bible reaching the image prompt.

The bug this closes: a project whose bible says ART DIRECTION LOCKED — "true
chunky pixel art, no painterly rendering, isometric 2:1, dark neon office
palette" — generated a painterly straight-on fantasy paladin, because no image
path had ever read the bible. Narrative writes pass through canon.check; art
passed through nothing.
"""
from __future__ import annotations

import pytest

from bgate_core import artdirection as ad
from bgate_core import bible


@pytest.fixture()
def brief(root):
    bible.add(root, "constraint", "ART DIRECTION LOCKED: the pixel set",
              "true chunky pixel art, visible pixel grid, no painterly "
              "rendering, dark neon office palette", rank=1)
    bible.add(root, "constraint", "PROJECTION LOCKED: isometric 2:1",
              "floor tiles are 2:1 diamonds, NOT flat top-down", rank=2)
    bible.add(root, "constraint", "Style anchor",
              "16-bit SNES-era tile art, chibi proportions", rank=3)
    # Real, locked, and nothing to do with how a picture looks.
    bible.add(root, "constraint", "Determinism",
              "same state and same seed produce the same outcome", rank=4)
    return root


class TestConstraintSelection:
    def test_it_finds_the_art_constraints(self, brief):
        titles = [c["title"] for c in ad.constraints(brief)]
        assert any("ART DIRECTION" in t for t in titles)
        assert any("PROJECTION" in t for t in titles)

    def test_determinism_is_not_an_art_constraint(self, brief):
        """Locked and important, but it does not describe a picture — putting it
        in an image prompt buys nothing but tokens."""
        assert not any(c["title"] == "Determinism" for c in ad.constraints(brief))

    def test_locked_is_read_off_the_title(self, brief):
        locked = [c["title"] for c in ad.constraints(brief) if ad.is_locked(c)]
        assert len(locked) == 2
        assert all("LOCKED" in t for t in locked)

    def test_locked_constraints_come_first(self, brief):
        found = ad.constraints(brief)
        assert ad.is_locked(found[0])

    def test_locked_only_filters(self, brief):
        assert len(ad.constraints(brief, locked_only=True)) == 2


class TestClause:
    def test_it_tells_the_model_what_to_draw(self, brief):
        text = ad.clause(brief).lower()
        assert "pixel art" in text and "painterly" in text
        assert "isometric" in text

    def test_it_never_quotes_the_bible_verbatim(self, brief):
        """Bible prose is written for a PERSON. Handed to an image model it gets
        DRAWN — a real run produced a room with "DIRECTOR'S FAVORITE" rendered
        as neon signage. The bible is read, never quoted."""
        text = ad.clause(brief).lower()
        for editorial in ("director", "favourite", "favorite", "locked",
                          "rendering target", "pinned", "the pixel set"):
            assert editorial not in text, f"{editorial!r} leaked into the prompt"

    def test_it_forbids_text_in_the_image(self, brief):
        """The single most common failure once a style clause exists, and it
        must survive truncation — it is appended after the budget."""
        assert "no text, letters" in ad.clause(brief, limit=80).lower()

    def test_advisory_direction_is_included(self, brief):
        assert "snes" in ad.clause(brief).lower()

    def test_it_is_budgeted(self, brief):
        """A prompt that is mostly boilerplate stops steering the model."""
        style = ad.clause(brief, limit=200).split(".")[0]
        assert len(style) <= 210

    def test_a_project_with_no_bible_gets_no_clause(self, root):
        assert ad.clause(root) == ""

    def test_the_clause_follows_the_bible(self, brief):
        """The whole point: editing the bible changes the art. A hardcoded style
        string would be the same bug wearing a different hat."""
        before = ad.clause(brief).lower()
        bible.add(brief, "constraint", "PALETTE LOCKED: monochrome",
                  "greyscale only, no hue", rank=0)
        after = ad.clause(brief, limit=900).lower()
        assert "greyscale" in after and "greyscale" not in before


class TestCheck:
    def test_it_never_fails_art_it_cannot_judge(self, brief, tmp_path):
        """The pixel test was tried as a gate and withdrawn — it rejected the
        project's own on-model sprites. A gate that fails real art is worse than
        no gate, because the work is already paid for."""
        from PIL import Image
        p = tmp_path / "frame.png"
        Image.new("RGBA", (64, 64), (30, 30, 40, 255)).save(p)
        verdict = ad.check(brief, p, anchors=[])
        assert verdict["ok"] is True
        assert not [f for f in verdict["flags"] if f["locked"]]

    def test_it_still_reports_what_it_measured(self, brief, tmp_path):
        from PIL import Image
        p = tmp_path / "frame.png"
        Image.new("RGBA", (64, 64), (30, 30, 40, 255)).save(p)
        measured = ad.check(brief, p, anchors=[])["measured"]
        assert "pixel_block" in measured and measured["pixel_required"] is True

    def test_palette_drift_is_advisory_when_the_bible_did_not_lock_it(
            self, root, tmp_path):
        from PIL import Image
        bible.add(root, "constraint", "Style anchor", "muted greys", rank=1)
        anchor = tmp_path / "anchor.png"
        Image.new("RGBA", (32, 32), (40, 40, 40, 255)).save(anchor)
        art = tmp_path / "art.png"
        Image.new("RGBA", (32, 32), (255, 0, 255, 255)).save(art)
        verdict = ad.check(root, art, anchors=[str(anchor)], max_palette_distance=50)
        assert any(f["flag"] == "palette_drift" for f in verdict["flags"])
        assert verdict["ok"] is True   # reported, not fatal

    def test_a_missing_bible_does_not_explode(self, root, tmp_path):
        from PIL import Image
        p = tmp_path / "x.png"
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(p)
        assert ad.check(root, p, anchors=[])["ok"] is True


class TestWiring:
    def test_chroma_generate_appends_the_clause(self, brief, monkeypatch, tmp_path):
        """The one door every generation walks through must carry the bible."""
        from bgate_core import chroma
        seen = {}

        def fake(prompt, out_path, **kw):
            seen["prompt"] = prompt
            return {"ok": False, "error": "stopped before spending"}

        monkeypatch.setattr("bgate_adapters.krea.generate", fake)
        chroma.generate("a chair", tmp_path / "o.png", provider="krea",
                        model="krea-2-medium", task_kind="prop", root=brief)
        assert "painterly" in seen["prompt"].lower()

    def test_no_project_means_no_clause_and_no_crash(self, monkeypatch, tmp_path):
        from bgate_core import chroma
        seen = {}
        monkeypatch.setattr("bgate_adapters.krea.generate",
                            lambda p, o, **k: (seen.update(prompt=p),
                                               {"ok": False, "error": "x"})[1])
        chroma.generate("a chair", tmp_path / "o.png", provider="krea",
                        model="krea-2-medium", task_kind="prop")
        # The keyable-background clause still applies (task_kind "prop" is
        # keyed) — what must be absent is the ART DIRECTION text.
        assert "ART DIRECTION" not in seen["prompt"]
        assert "Style direction:" not in seen["prompt"]


class TestScopeByKind:
    """A directive about BODIES is not a directive about a spark.

    Measured on a corporate-satire project: "chibi proportions, large head,
    short body" — correct for that game's characters, and what its bible asks
    for — was being appended to a request for a muzzle spark. Four generations
    in a row came back as a big-headed figure with the spark drawn beside it.
    The clause is appended last, so nothing in the prompt could outvote it.
    """

    def test_a_character_still_gets_the_anatomy(self, brief):
        assert "chibi" in ad.clause(brief, task_kind="animation")

    def test_an_effect_does_not(self, brief):
        assert "chibi" not in ad.clause(brief, task_kind="vfx")

    def test_a_prop_does_not_either(self, brief):
        """A desk was being told to have a large head, too — the same bug, just
        less visible than it was on the spark."""
        assert "chibi" not in ad.clause(brief, task_kind="prop")

    def test_an_effect_gets_no_projection(self, brief):
        """A radial burst has no projection to be wrong about; asking for one
        gets a burst drawn standing on a diamond of ground."""
        assert "isometric" not in ad.clause(brief, task_kind="vfx")

    def test_a_tile_still_gets_projection(self, brief):
        assert "isometric" in ad.clause(brief, task_kind="tile")

    def test_the_universal_directives_reach_everything(self, brief):
        for kind in ("animation", "prop", "tile", "vfx", "ui", ""):
            assert "pixel art" in ad.clause(brief, task_kind=kind)

    def test_an_unspecified_kind_is_not_narrowed(self, brief):
        """Scoping subtracts only from a caller that SAID what it was making.
        Treating "" as none-of-the-above silently stripped the projection
        directive from every path that had never needed to name its kind."""
        assert ad.clause(brief) == ad.clause(brief, task_kind="animation")
