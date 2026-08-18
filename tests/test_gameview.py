"""The project's declared view, and what it decides.

The defect that motivated this module was measured, not theorised: a prop batch
prompted with "a high 3/4 top-down game view" came back ISOMETRIC — to an image
model "three-quarter" means the standard product render — and every prop showed
two side faces, to stand on a floor tileset drawn flat top-down. The prompt was
the proximate cause. The real one was that the view lived in a scratch prompt
instead of in the project, so every agent re-derived it and drifted.
"""
from __future__ import annotations

import pytest

from bgate_core import gameview as gv
from bgate_core import props


class TestTheThreeViews:
    def test_the_supported_views_are_the_three_2d_ones(self):
        assert gv.VIEWS == ("top_down", "side_scroller", "isometric")
        assert gv.DEFAULT_VIEW in gv.VIEWS

    @pytest.mark.parametrize("given,want", [
        ("platformer", "side_scroller"), ("iso", "isometric"),
        ("top-down", "top_down"), ("TOP DOWN", "top_down"),
        ("sidescroller", "side_scroller"), ("", "top_down"),
    ])
    def test_the_obvious_aliases_resolve(self, given, want):
        assert gv.normalise(given) == want

    def test_an_unsupported_view_is_refused_by_name(self):
        with pytest.raises(gv.ViewError, match="not a view this pipeline"):
            gv.normalise("first_person")

    def test_every_view_answers_every_downstream_question(self):
        for name in gv.VIEWS:
            d = gv.describe(name)
            assert d["tile_shape"] and d["tile_layout"]
            assert d["mounts"] and d["reachability"] and d["sprite_view"]


class TestTheCamera:
    """The clauses forbid the wrong reading BY NAME. A clause that only says
    what it wants inherits the model's default for everything it forgot to
    forbid, and the model's default for 'game prop' is isometric."""

    def test_the_top_down_clause_forbids_isometric_explicitly(self):
        clause = gv.camera_clause("top_down", "floor")
        assert "NOT isometric" in clause
        assert "NEVER show a left face and a right face" in clause

    def test_the_side_clause_forbids_top_down_explicitly(self):
        clause = gv.camera_clause("side_scroller", "floor")
        assert "NOT top-down" in clause and "NOT isometric" in clause

    def test_the_isometric_clause_ASKS_for_two_side_faces(self):
        """The exact thing the other two forbid — which is why one camera
        across all three views cannot work."""
        clause = gv.camera_clause("isometric", "floor")
        assert "TWO side faces" in clause

    def test_one_view_holds_several_cameras_by_mount(self):
        """A top-down game draws floor props overhead, wall props on the wall's
        face and decals perfectly flat. Asking one camera for all three is how
        a torch comes back with a slab of masonry attached."""
        floor = gv.camera_clause("top_down", "floor")
        wall = gv.camera_clause("top_down", "wall")
        decal = gv.camera_clause("top_down", "overlay")
        assert len({floor, wall, decal}) == 3
        assert "straight on from the front" in wall
        assert "PERFECTLY DIRECTLY ABOVE" in decal

    def test_an_unknown_mount_falls_back_rather_than_returning_nothing(self):
        assert gv.camera_clause("top_down", "nonsense") == \
            gv.camera_clause("top_down", "floor")


class TestWhatTheViewDecides:
    def test_only_isometric_asks_for_diamond_tiles(self):
        assert gv.tile_geometry("isometric") == ("isometric", "diamond_down")
        assert gv.tile_geometry("top_down")[0] == "square"
        assert gv.tile_geometry("side_scroller")[0] == "square"

    def test_gravity_changes_what_playable_even_means(self):
        """Under gravity, 'every floor cell is one connected region' is not the
        question — reachability by jump arc is."""
        assert gv.spec("side_scroller")["gravity"] == "s"
        assert gv.spec("side_scroller")["reach"] == "jumpable"
        assert gv.spec("top_down")["reach"] == "connected"

    def test_a_ceiling_mount_exists_only_where_it_means_something(self):
        assert gv.supports("side_scroller", "ceiling")
        assert not gv.supports("top_down", "ceiling")

    def test_a_colonnade_needs_depth_the_side_view_does_not_have(self):
        assert gv.supports("top_down", "pillar")
        assert not gv.supports("side_scroller", "pillar")


class TestPropsReadTheView:
    def test_the_camera_reaches_art_spec(self):
        """So a generator does not write its own and drift."""
        top = props.art_spec("barrel", view="top_down")
        side = props.art_spec("barrel", view="side_scroller")
        assert top["camera"] != side["camera"]
        assert "NOT isometric" in top["camera"]
        assert top["view"] == "top_down" and side["view"] == "side_scroller"

    def test_the_manifest_reports_what_falls_out_of_view(self):
        """Reported, not dropped — a generation that silently skipped them
        would look like a budget that came in under estimate."""
        m = props.art_manifest(view="side_scroller")
        assert "pillar" in m["out_of_view"]
        assert all(s["in_view"] for s in m["specs"])
        assert len(m["specs"]) + len(m["out_of_view"]) == len(props.PROP_TYPES)

    def test_planning_a_mount_the_view_lacks_is_refused_loudly(self):
        """A level quietly missing a whole class of prop looks like a density
        problem, which is the wrong thing to go and fix."""
        from bgate_core import levelgen
        lvl = levelgen.plan(48, 32, seed=7, room_fill=0.85)
        walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
        with pytest.raises(props.PropError, match="means nothing in a"):
            props.plan(lvl, seed=7, walls=walls, types=("pillar", "barrel"),
                       view="side_scroller")

    def test_a_top_down_plan_carries_the_view_it_was_made_for(self):
        from bgate_core import levelgen
        lvl = levelgen.plan(48, 32, seed=7, room_fill=0.85)
        walls = levelgen.wall_ring({tuple(c) for c in lvl["floor"]})
        pl = props.plan(lvl, seed=7, walls=walls, view="top_down")
        assert pl["view"] == "top_down"
        assert pl["checks"]["still_connected"]


class TestWhoCanSeeIt:
    """The view is shared ground: the art seat needs it to generate a prop and
    the level seats need it to know what a correct prop even looks like."""

    def test_both_the_generating_and_the_consuming_seats_hold_it(self):
        from bgate_core import modules
        for seat in ("art", "gameplay", "tech", "cinematic"):
            assert modules.seat_tool_enabled("game_view_get", seat), seat

    def test_a_tool_in_two_crafts_is_enabled_by_either(self):
        """This returned on the FIRST craft whose prefix matched, so a shared
        tool resolved to whichever craft was declared higher in the dict:
        `game_view_` is in both `image` and `level`, and gameplay — which holds
        `level` and not `image` — was refused it. Dict order is not a
        permission model."""
        from bgate_core import modules
        owners = {c for c, pre in modules.CRAFTS.items()
                  if any("game_view_get".startswith(p) for p in pre)}
        assert owners >= {"image", "level"}, owners
        assert modules.seat_tool_enabled("game_view_get", "gameplay")
        assert modules.seat_tool_enabled("game_view_get", "art")

    def test_a_seat_holding_neither_craft_still_does_not_get_it(self):
        from bgate_core import modules
        assert not modules.seat_tool_enabled("game_view_get", "narrative")
