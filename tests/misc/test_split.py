"""Draggable pane boundaries, pinned.

A splitter fails SILENTLY and in a way nobody reports. It writes a CSS variable
onto a container and trusts a layout rule to consume it; rename the variable on
either side and the handle still renders, still drags, still saves a value, and
moves nothing at all. There is no console error and no visual clue — the pane
simply does not resize, which reads as "this app's panels aren't adjustable"
rather than as a bug. So the contract worth testing is not the drag maths, it
is that the two halves still refer to the same name.

The other half of the file guards the collapse. Every one of these boundaries
sits inside a responsive layout that stacks below some width, and a vertical
handle in a stacked layout is a drag target that writes a width onto a track
that is not being used — invisible until the window widens again, at which
point the pane comes back the wrong size and the cause is a drag from twenty
minutes ago.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
INDEX = STATIC / "index.html"
CSS = STATIC / "app.css"
SPLIT = STATIC / "split.js"

# Two of these views build their markup AND their stylesheet from strings in
# their own module, so the variable a handle writes and the rule that reads it
# are declared a hundred lines apart in the same file. That is the pairing most
# likely to drift, so both files are searched for handles and for CSS.
SOURCES = (INDEX, STATIC / "scenebuild.js", STATIC / "sceneview.js")


def handles() -> list[dict[str, str]]:
    """Every `.split` handle the app renders, as its attribute dict."""
    out = []
    for path in SOURCES:
        src = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<div\s+class=\"split[^\"]*\"[^>]*>", src):
            attrs = dict(re.findall(r'(data-split[\w-]*)="([^"]*)"', tag))
            attrs["_file"] = path.name
            out.append(attrs)
    return out


def stylesheets() -> str:
    """app.css plus the stylesheets the view modules inject at runtime."""
    return "\n".join(p.read_text(encoding="utf-8") for p in (CSS, *SOURCES[1:]))


class TestWiring:
    def test_the_boundaries_exist(self):
        """Atlas's two, and whatever else still has a draggable edge.

        THREE OF THESE WENT WITH THE SHELLS THAT OWNED THEM. `rail` was the old
        navigation's resize handle — the 4a rail is a fixed 56px icon column and
        the screen list beside it collapses rather than drags. `cockpit` and
        `ck-detail` belonged to the classic agents console, which is React now.
        A handle for a pane that no longer exists is a handle that writes a
        variable nobody reads, which is the exact failure the rest of this file
        was written to catch — so they are gone rather than kept for the test.

        Atlas is untouched and still has both of its own.
        """
        keys = {h.get("data-split") for h in handles()}
        assert {"sb-side", "sv-play"} <= keys, (
            "Atlas lost a boundary — that one is not part of the React migration")

    def test_every_handle_names_a_variable_the_css_consumes(self):
        """The silent failure this whole file exists for."""
        css = stylesheets()
        for h in handles():
            var = h.get("data-split-var")
            assert var, f"handle {h.get('data-split')!r} writes no variable"
            # Consumed, not merely mentioned: `var(--x)` in a property value.
            assert re.search(r"var\(\s*" + re.escape(var) + r"\s*[,)]", css), (
                f"{var} is written by a handle in {h['_file']} but no CSS rule "
                "reads it — the boundary will drag and do nothing"
            )

    def test_a_handle_rendered_from_js_is_rebound_after_its_panel_redraws(self):
        """innerHTML on the container throws the previous handle away.

        The module binds on DOMContentLoaded, which has long since fired by the
        time these panels mount, so a view that rebuilds its own markup has to
        say so. Without this the boundary renders and is simply inert.
        """
        for path in SOURCES[1:]:
            src = path.read_text(encoding="utf-8")
            assert "Split.init(" in src, (
                f"{path.name} renders a handle but never rebinds it"
            )

    def test_every_handle_has_a_storage_key_and_a_floor(self):
        for h in handles():
            assert h.get("data-split"), "no key means no persistence"
            assert h.get("data-split-min"), (
                f"{h['data-split']} has no minimum — a pane can be dragged shut"
            )

    def test_the_defaults_survive_an_undragged_boundary(self):
        """Each variable must carry its old value as the var() fallback.

        This is what keeps a fresh install looking exactly as designed: nothing
        writes these until somebody drags, so the fallback IS the layout.
        """
        css = stylesheets()
        for var in ("--ck-left-w", "--ckd-w", "--sb-side-w", "--sv-play-w"):
            assert re.search(r"var\(" + re.escape(var) + r",[^)]+\)", css), (
                f"{var} is read with no fallback — an undragged boundary "
                "would collapse to nothing"
            )


class TestCollapse:
    """Below each stacking breakpoint the handle must leave the layout."""

    def test_cockpit_splitter_is_hidden_when_the_panes_stack(self):
        css = CSS.read_text(encoding="utf-8")
        block = css.split("@media(max-width:1150px)", 1)[1].split("}\n  }", 1)[0]
        assert "grid-template-columns:1fr" in block
        assert ".cockpit > .split{display:none}" in block

    def test_rail_splitter_is_hidden_at_both_rail_breakpoints(self):
        css = CSS.read_text(encoding="utf-8")
        for width in ("1180px", "820px"):
            block = css.split(f"@media(max-width:{width})", 1)[1].split("\n  }", 1)[0]
            assert ".deck > .split{display:none}" in block, (
                f"the rail handle survives the {width} collapse"
            )

    def test_a_collapsed_grid_declares_no_track_for_the_hidden_handle(self):
        """The bug this caught, kept caught.

        `.deck` was left as `64px 0 1fr` while its handle went display:none.
        A hidden element is not a grid item, so the STAGE slid into the 0px
        track the handle had vacated and the whole page rendered as a rail and
        a sliver. Track count has to follow item count, not intent.
        """
        css = stylesheets()
        for media, selector, items in (
            ("1180px", ".deck", 2),   # rail + stage, handle gone
            ("1150px", ".cockpit", 1),  # stacked: one column, two rows
        ):
            block = css.split(f"@media(max-width:{media})", 1)[1]
            rule = re.search(
                re.escape(selector) + r"\{[^}]*grid-template-columns:([^;}]+)", block)
            assert rule, f"{selector} declares no columns inside {media}"
            tracks = len(rule.group(1).strip().split())
            assert tracks == items, (
                f"{selector} at {media} declares {tracks} tracks for {items} "
                "visible items — the extra track swallows a pane"
            )

    def test_hidden_means_display_none_not_just_invisible(self):
        """An opacity:0 handle is still tabbable and still draggable.

        The inspector handle DOES use opacity while its drawer is closed, which
        is correct — the drawer is coming back and the handle should animate in
        with it. A collapsed layout is not coming back until the window widens,
        so there the handle has to leave the box model entirely.
        """
        css = CSS.read_text(encoding="utf-8")
        for media in ("1150px", "1180px", "820px"):
            block = css.split(f"@media(max-width:{media})", 1)[1].split("\n  }", 1)[0]
            for rule in re.findall(r"[^{}\n]*\.split[^{}]*\{([^}]*)\}", block):
                assert "display:none" in rule, (
                    f"a handle rule inside the {media} collapse does not remove "
                    f"it from the layout: {rule!r}"
                )


class TestModule:
    def test_the_handle_is_reachable_without_a_mouse(self):
        src = SPLIT.read_text(encoding="utf-8")
        assert 'setAttribute("role", "separator")' in src
        assert 'setAttribute("tabindex", "0")' in src
        assert "ArrowLeft" in src and "Home" in src

    def test_a_restored_width_is_clamped_on_the_way_in(self):
        """A width saved on a wide monitor must not cover the pane beside it."""
        src = SPLIT.read_text(encoding="utf-8")
        restore = src.split("var saved = readStored(key);", 1)[1]
        assert "apply(saved" in restore, "restore bypasses the clamp"

    def test_storage_never_throws(self):
        """Private mode and disabled storage must not take the layout down."""
        src = SPLIT.read_text(encoding="utf-8")
        for fn in ("readStored", "write", "forget"):
            body = src.split("function " + fn, 1)[1].split("\n  }", 1)[0]
            assert "catch" in body, f"{fn} can throw and break the page"
