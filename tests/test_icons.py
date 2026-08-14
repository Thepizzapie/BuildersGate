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

from bgate_core import seats as _seats

# The frontend SOURCE tree. bgate_ui/static is the Vite build output (these
# files copied verbatim, plus dist/), so the source is what to assert against.
STATIC = Path(__file__).resolve().parents[1] / "frontend" / "public"
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

    # DERIVED FROM THE SEAT TABLE, NOT RETYPED. This list was a literal, so
    # adding the eighth seat left it green while the rail drew a dashed
    # bgi-missing box — the exact "silently blank slot" the test above exists to
    # catch, passing because the parametrize list had never heard of the seat.
    @pytest.mark.parametrize("seat", sorted(_seats.ROLES))
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
        """THE RAIL MOVED, THE RULE DID NOT.

        It used to be eleven `<button data-view=… ><span class="ic"
        data-icon=…>` rows in index.html. The 4a shell renders it from
        frontend/src/shell/nav.ts — four areas, each with its screens — so the
        markup this asserted against is gone, but the thing it was protecting is
        not: every destination NAMES an icon, and nobody pastes a glyph.
        """
        nav = (INDEX.parent.parent.parent
               / "frontend" / "src" / "shell" / "nav.ts").read_text(encoding="utf-8")
        assert nav.count("AREAS"), "the nav table moved again — find it"
        labels = re.findall(r'\blabel:\s*"', nav)
        icons = re.findall(r'\bicon:\s*"', nav)
        assert labels, "no destinations found — did the table move?"
        assert len(icons) == len(labels), (
            f"{len(labels)} destinations but {len(icons)} icons — "
            "one of them is wearing a glyph or nothing")

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
