"""The icon system, pinned.

The app used to draw its UI with ~20 unrelated Unicode glyphs (⛰ ◈ ▲ ◆ ✎ ♪ ▦
⌖ ⬡ ⚙) and a brand mark made of two ASCII characters, `⌐¬`. Those resolve
through whatever symbol font the OS falls back to, so stroke weight, optical
size and baseline drift per machine and no CSS can reconcile them.

These tests exist to stop them coming back one convenient character at a time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
ICONS = STATIC / "icons.js"
INDEX = STATIC / "index.html"

# Symbol-font characters that were doing icon duty. Ordinary punctuation and
# arrows used in prose (→, ·, ×) are fine — this is about pictographs.
GLYPHS = "⛰◈▲◆✎♪▦⌖⬡⚙⑃✦✂☑¶◎◉▤▧⊞⧗⌁¬⌐"


def icon_names() -> set[str]:
    src = ICONS.read_text(encoding="utf-8")
    block = src.split("const P = {", 1)[1].split("\n  };", 1)[0]
    return set(re.findall(r"^\s*(\w+):\s*`", block, re.M))


class TestIconSet:
    def test_the_set_exists_and_is_not_thin(self):
        assert len(icon_names()) >= 30

    def test_every_icon_shares_one_grid(self):
        """A shared viewBox is what makes a set look like a set.

        The logo is deliberately outside this. It is traced from
        packaging/logo.svg and keeps the artwork's own user units so the path
        data stays copy-pasteable from the file; forcing it onto the icon grid
        would mean rounding it, and a redrawn mark is how it went wrong before.
        """
        src = ICONS.read_text(encoding="utf-8")
        # Drop the logo builder, not everything before the first mention of it —
        # the module docstring names it, and splitting on the name alone cut the
        # icon builder out too and left nothing to assert on.
        logo = re.search(r"BGIcon\.logo = function.*?\n  \};", src, re.S)
        assert logo, "BGIcon.logo went missing"
        boxes = set(re.findall(r'viewBox="([^"]+)"', src.replace(logo.group(0), "")))
        assert boxes == {"0 0 24 24"}, boxes

    def test_one_stroke_weight(self):
        src = ICONS.read_text(encoding="utf-8")
        assert 'stroke-width="1.75"' in src
        # 1.75 is the whole set. The logo used to add four more weights because
        # it was drawn with strokes; it is filled paths now and adds none.
        weights = set(re.findall(r'stroke-width="([\d.]+)"', src))
        assert weights == {"1.75"}, weights

    def test_icons_inherit_colour(self):
        """currentColor is why one icon works on the rail, a chip and a button."""
        assert 'stroke="currentColor"' in ICONS.read_text(encoding="utf-8")

    def test_a_missing_icon_is_visible_not_blank(self):
        """A silently blank slot is how the old glyph mess survived so long."""
        src = ICONS.read_text(encoding="utf-8")
        assert "bgi-missing" in src and "stroke-dasharray" in src

    @pytest.mark.parametrize("seat", ["director", "narrative", "gameplay", "tech",
                                      "art", "audio", "qa"])
    def test_every_seat_has_an_icon(self, seat):
        assert seat in icon_names()

    @pytest.mark.parametrize("view", ["overview", "agents", "studio", "seats",
                                      "playtests", "assets", "atlas", "world",
                                      "timeline"])
    def test_every_rail_view_has_an_icon(self, view):
        assert view in icon_names()


class TestShell:
    def test_the_brand_mark_is_geometry_not_ascii(self):
        html = INDEX.read_text(encoding="utf-8")
        assert "⌐¬" not in html
        assert 'data-icon="logo"' in html

    def test_there_is_a_favicon(self):
        """There was none at all — the tab showed the browser default."""
        html = INDEX.read_text(encoding="utf-8")
        assert 'rel="icon"' in html
        assert (STATIC / "favicon.svg").is_file()

    def test_every_rail_item_names_an_icon(self):
        html = INDEX.read_text(encoding="utf-8")
        rows = re.findall(r'data-view="(\w+)"[^>]*>\s*<span class="ic"([^>]*)>', html)
        assert rows, "no rail items found — did the markup move?"
        for view, attrs in rows:
            assert 'data-icon="' in attrs, f"{view} still has a bare glyph"

    def test_no_pictograph_glyphs_left_in_the_shell(self):
        html = INDEX.read_text(encoding="utf-8")
        # Ignore the comment that documents the old glyphs for posterity.
        body = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        found = {g for g in GLYPHS if g in body}
        assert not found, f"unicode icons still in index.html: {found}"

    def test_the_icon_upgrade_is_coalesced(self):
        """upgrade() writes DOM, which re-triggers the observer; panels also
        rerender on a 3s poll. Scanning per mutation would sit on the critical
        path of every repaint."""
        html = INDEX.read_text(encoding="utf-8")
        assert "MutationObserver" in html and "requestAnimationFrame" in html


class TestSeatModules:
    def test_seat_tab_glyphs_come_from_the_icon_set(self):
        core = (STATIC / "seats" / "_core.js").read_text(encoding="utf-8")
        assert "BGIcon" in core
        assert '"◆"' not in core and '"¶"' not in core

    def test_no_pictograph_glyphs_left_in_seat_modules(self):
        for path in sorted((STATIC / "seats").glob("*.js")):
            src = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            src = re.sub(r"//.*", "", src)
            found = {g for g in GLYPHS if g in src}
            assert not found, f"{path.name} still uses {found}"

    def test_flow_tabs_use_icons(self):
        flows = (STATIC / "flows.js").read_text(encoding="utf-8")
        assert "BGIcon(" in flows
        assert 'glyph: "⬡"' not in flows
