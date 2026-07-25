"""The cut line, proved at the two places it has to bite.

`bgate_core/scope.py` is tested on its own in test_scope_world.py. This module
tests the *wiring* — that queue.add and dispatch actually call it. The audit's
harshest finding was controls drawn as hard gates that enforce nothing, and a
gate whose call site is untested is one refactor away from being exactly that.
"""
from __future__ import annotations

import pytest

from bgate_core import bible, queue, scope
from bgate_ui import dispatch


@pytest.fixture()
def tiered(root):
    """A bible with two tiers and the cut line drawn between them."""
    shipping = bible.add(root, "scope_tier", "Tier 1 — shipping", rank=1)
    line = bible.add(root, "cut_line", "CUT LINE", rank=2)
    nice = bible.add(root, "scope_tier", "Tier 2 — nice to have", rank=3)
    return {"root": root, "above": shipping["id"], "below": nice["id"],
            "line": line["id"]}


class TestQueueAdd:
    def test_work_above_the_line_files_normally(self, tiered):
        item = queue.add(tiered["root"], "gameplay", "hit detection",
                         scope_tier_id=tiered["above"])
        assert item["scope_tier_id"] == tiered["above"]

    def test_work_below_the_line_is_refused(self, tiered):
        with pytest.raises(scope.OutOfScope) as caught:
            queue.add(tiered["root"], "gameplay", "cosmetic hat physics",
                      scope_tier_id=tiered["below"])
        assert caught.value.verdict["code"] == "below_cut_line"

    def test_the_refusal_is_a_valueerror(self, tiered):
        """Every existing caller maps ValueError to a 400. If OutOfScope ever
        stops subclassing it, they all start returning 500 instead."""
        with pytest.raises(ValueError):
            queue.add(tiered["root"], "art", "extra idle animation",
                      scope_tier_id=tiered["below"])

    def test_refused_work_is_not_written(self, tiered):
        with pytest.raises(scope.OutOfScope):
            queue.add(tiered["root"], "gameplay", "cosmetic hat physics",
                      scope_tier_id=tiered["below"])
        assert queue.list_items(tiered["root"]) == []

    def test_untiered_work_still_files(self, tiered):
        """Deliberate: refusing untiered work would make the first cut line
        anyone draws reject the whole existing queue."""
        assert queue.add(tiered["root"], "tech", "no tier")["id"]

    def test_no_cut_line_means_no_gate(self, root):
        tier = bible.add(root, "scope_tier", "Tier 9", rank=9)
        assert queue.add(root, "tech", "anything",
                         scope_tier_id=tier["id"])["id"]


class TestDispatch:
    def test_dispatch_refuses_work_the_line_retroactively_cut(self, tiered):
        """The line moves. An item queued legitimately can be out of scope by
        the time anyone dispatches it — that is the case worth catching, since
        the queue-time gate cannot see the future."""
        root = tiered["root"]
        item = queue.add(root, "gameplay", "hit detection",
                         scope_tier_id=tiered["above"])
        # Producer redraws the line above the tier this work sits in.
        bible.update(root, tiered["line"], rank=0)

        got = dispatch.dispatch(str(root), item["id"])
        assert got["ok"] is False
        assert got["code"] == "out_of_scope"
        assert "cut line" in got["error"].lower()

    def test_in_scope_work_passes_the_scope_gate(self, tiered, monkeypatch):
        """It must fail for some *later* reason (no CLI on PATH), never at the
        scope gate — otherwise this test would pass even if the gate refused
        everything."""
        monkeypatch.setattr(dispatch, "find_claude", lambda *a, **k: "")
        item = queue.add(tiered["root"], "gameplay", "hit detection",
                         scope_tier_id=tiered["above"])
        got = dispatch.dispatch(str(tiered["root"]), item["id"])
        assert got["ok"] is False
        assert got.get("code") != "out_of_scope"
