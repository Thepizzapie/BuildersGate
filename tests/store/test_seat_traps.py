"""The traps ride in the brief, because memory is not a distribution mechanism.

MEASURED: two agents independently hit the `.tscn` Transform3D transpose in one
night. Once the trap list started being pasted into briefs by hand, nobody did.
That is the whole argument — and the reason to move it out of a human's habit and
into the thing the board generates, since the hand-pasting only happened because
someone happened to remember.

WHAT QUALIFIES. Every row is a bug that has already cost a real run AND produces
no error a search would find. That second half is the load-bearing one: an agent
can recover from a traceback on its own, so a traceback-producing bug in this
list is pure cost. A brief is a budget and a list nobody finishes is a list
nobody reads.

The most important test here is the negative one: a 2D project must not be billed
for the Transform3D paragraph. A trap list that goes out whole to everyone
becomes boilerplate, and boilerplate gets skimmed — at which point the transpose
bug comes back and the list is what made it invisible.
"""
from __future__ import annotations

import json


from bgate_core.store import project
from bgate_core.board import seats


def _brief(root, role="tech"):
    return seats.brief(root, role)


class TestTheListIsGated:
    def test_a_2d_project_is_not_told_about_transform3d(self, root):
        """The negative that keeps the list readable."""
        text = " ".join(seats.traps_for("tech", "2d"))
        assert "Transform3D" not in text
        assert "winding" not in text.lower()

    def test_a_3d_project_is(self, root):
        text = " ".join(seats.traps_for("tech", "3d"))
        assert "ROW-major" in text
        assert "TRANSPOSE" in text

    def test_a_mixed_project_gets_the_3d_traps_too(self):
        """2d+3d is a 3D game with a 2D HUD, not a compromise between them."""
        assert seats.traps_for("tech", "2d+3d") == seats.traps_for("tech", "3d")

    def test_the_engine_traps_reach_every_dimension(self):
        """A parse error looking like a hang has nothing to do with 3D."""
        for dim in ("2d", "3d", "2d+3d"):
            text = " ".join(seats.traps_for("tech", dim))
            assert "PARSE ERROR" in text, dim
            assert "preload" in text, dim

    def test_the_srgb_trap_goes_to_the_seats_that_can_hit_it(self):
        """Only art and tech touch the Blender→Godot material path."""
        assert any("LINEAR" in t for t in seats.traps_for("art", "3d"))
        assert not any("LINEAR" in t for t in seats.traps_for("qa", "3d"))


class TestItIsInTheBrief:
    def test_the_brief_carries_traps(self, root):
        assert _brief(root)["traps"]

    def test_the_brief_says_the_mcp_tools_are_deferred(self, root):
        """Universal and non-obvious: an agent that has not been told concludes
        the tools do not exist and works around their absence."""
        rules = " ".join(_brief(root)["rules"])
        assert "DEFERRED" in rules
        assert "ToolSearch" in rules
        assert "project_dir" in rules

    def test_the_traps_follow_the_project_dimension(self, root):
        """Not a static blob: the brief asks the project what it is. This is also
        why project_set_dimension matters — a stale record aims this at the wrong
        kind of game."""
        before = _brief(root)["traps"]
        project.set_dimension(root, "3d")
        after = _brief(root)["traps"]
        assert len(after) > len(before)
        assert any("ROW-major" in t for t in after)

    def test_the_existing_rules_survived(self, root):
        """The tooling rule is prepended, not substituted — the boundary rule
        and the work manifest are what stop an agent leaving its project and
        dying without a checkpoint trail. (The lane rule went advisory on
        2026-08-19; the project boundary is the enforced line now.)"""
        rules = " ".join(_brief(root)["rules"])
        assert "boundary is enforced" in rules
        assert "WORK MANIFEST" in rules


class TestItStaysAffordable:
    def test_the_traps_are_a_small_fraction_of_the_brief_budget(self, root):
        """A brief is a budget. If this ever grows past a tenth of it, the bar
        for adding a row has slipped."""
        worst = sum(len(t) for t in seats.traps_for("art", "3d"))
        assert worst < seats.BRIEF_CHARS // 5

    def test_the_brief_still_fits(self, root):
        project.set_dimension(root, "3d")
        assert len(json.dumps(_brief(root), default=str)) <= seats.BRIEF_CHARS

    def test_every_trap_is_one_paragraph_not_an_essay(self):
        for trap in seats.TRAPS:
            assert len(trap["text"]) < 600, trap["text"][:60]
