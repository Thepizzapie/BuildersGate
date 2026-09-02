"""The generated map of the MCP surface.

The contract: the index is DERIVED from the live registry, never authored, so
it cannot name a tool this session does not serve and cannot omit one it does.
A written list fails both ways silently, which is how 231 tools came to have
no map but 1,710 words of hand-written prose in seat_brief.
"""
from __future__ import annotations

from bgate_core.store import modules
from bgate_core.board import toolindex

SAMPLE = [
    ("image_generate", "Generate ONE image and write it to the art dir. "
                       "More prose that must not appear."),
    ("sprite_plan", "The key poses and timing for standard animations. "
                    "FREE - spends nothing."),
    ("sprite_sheet_check", "LOOK AT A GENERATED POSE ROW BEFORE SPENDING "
                           "ANYTHING ELSE ON IT. Free - calls no model."),
    ("queue_add", "File a work item for a seat."),
    ("godot_status", "Is Godot there."),
]


class TestTheHeadline:
    def test_it_takes_the_first_sentence(self):
        assert toolindex.headline(
            "Generate ONE image and write it to the art dir. More prose."
        ) == "Generate ONE image and write it to the art dir"

    def test_a_short_first_sentence_borrows_the_next(self):
        # "Is Godot there." is a true first sentence and a useless index line.
        got = toolindex.headline("Is Godot there. Version and path, or what "
                                 "to install.")
        assert got.startswith("Is Godot there.")
        assert "Version" in got

    def test_a_dash_clause_is_elaboration(self):
        assert toolindex.headline(
            "The key poses and timing for standard animations - spends nothing."
        ) == "The key poses and timing for standard animations"

    def test_it_caps_a_runaway_line(self):
        got = toolindex.headline("word " * 80)
        assert len(got) <= 96
        assert got.endswith("...")

    def test_an_undocumented_tool_is_an_empty_line_not_a_crash(self):
        assert toolindex.headline("") == ""
        assert toolindex.headline(None) == ""


class TestTheGrouping:
    def test_a_tool_is_filed_under_every_craft_that_claims_it(self):
        got = toolindex.groups(SAMPLE)
        names = lambda g: [n for n, _ in got.get(g, [])]
        # sprite_sheet_check is image craft AND a verdict - both seats reach
        # for it, and filing it under one sends the other looking wrong.
        assert "sprite_sheet_check" in names("image")
        assert "sprite_sheet_check" in names("verdicts")

    def test_unclaimed_tools_land_in_the_spine(self):
        got = toolindex.groups(SAMPLE)
        assert [n for n, _ in got["spine"]] == ["godot_status", "queue_add"]

    def test_every_group_is_sorted(self):
        for rows in toolindex.groups(SAMPLE).values():
            assert rows == sorted(rows)


class TestTheSearch:
    def test_every_word_must_match_not_any(self):
        # "sprite sheet" is narrower than "sprite", not louder.
        broad = toolindex.matches(SAMPLE, "sprite")
        narrow = toolindex.matches(SAMPLE, "sprite sheet")
        assert len(narrow) < len(broad)
        assert [n for n, _ in narrow] == ["sprite_sheet_check"]

    def test_it_searches_the_first_line_too_not_only_names(self):
        assert [n for n, _ in toolindex.matches(SAMPLE, "poses")] == ["sprite_plan"]

    def test_an_empty_task_is_not_a_match_for_everything(self):
        assert toolindex.matches(SAMPLE, "") == []

    def test_a_miss_says_so_and_points_at_the_whole_map(self):
        got = toolindex.render(SAMPLE, task="nothing-matches-this")
        assert "tool_index()" in got


class TestTheRender:
    def test_the_count_is_distinct_tools_not_group_memberships(self):
        # sprite_sheet_check is in two groups; the surface is still 5 tools.
        assert "THE 5 TOOLS" in toolindex.render(SAMPLE)

    def test_it_names_the_seat_when_there_is_one(self):
        assert "the ART seat" in toolindex.render(SAMPLE, seat="art")
        assert "this session" in toolindex.render(SAMPLE)

    def test_the_spine_is_last(self):
        text = toolindex.render(SAMPLE)
        assert text.index("IMAGE") < text.index("SPINE")

    def test_no_schemas_leak_into_it(self):
        text = toolindex.render(SAMPLE)
        assert "More prose that must not appear" not in text

    def test_compact_is_names_only_and_much_smaller(self):
        full = toolindex.render(SAMPLE)
        small = toolindex.compact(SAMPLE)
        assert len(small) < len(full)
        assert "Generate ONE image" not in small
        assert "image_generate" in small


class TestItIsWiredToTheRealRegistry:
    def test_the_server_serves_an_index_of_what_it_registered(self):
        from bgate_mcp import server

        rows = server._registry_rows()
        assert len(rows) > 200          # the whole surface, not a stub
        registered = {name for name, _ in rows}
        # DERIVED, both directions: the index describes exactly this registry.
        text = toolindex.render(rows)
        for name in list(registered)[:40]:
            assert name in text

    def test_every_registered_tool_lands_in_exactly_one_index_group(self):
        # ... unless a craft table deliberately claims it twice, which is the
        # only legal way to appear more than once.
        from bgate_mcp import server

        rows = server._registry_rows()
        by_group = toolindex.groups(rows)
        for name, _ in rows:
            hits = sum(1 for g in by_group.values()
                       if any(n == name for n, _ in g))
            assert hits == max(1, len(modules.crafts_owning(name))), name

    def test_the_index_is_reachable_as_a_tool(self):
        from bgate_mcp import server

        assert "tool_index" in {n for n, _ in server._registry_rows()}

    def test_the_compact_map_rides_in_the_instructions(self):
        from bgate_mcp import server

        instructions = server.mcp.instructions or ""
        assert "tool_index()" in instructions
        assert "image_generate" in instructions
