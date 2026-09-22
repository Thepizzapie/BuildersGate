"""One work item gets a fixed number of runs in its life, and then the
answer to "run it again" is no, for everyone.

MEASURED (EXIT 67 r2, 2026-09-22): one item ran NINE times (~$33) through
its auto-retry, a director reopen and two dashboard send-backs, and no run
could land it because the brief was five deliverables wide. Every path that
buys a run now reads the cap: autopilot's readiness, the dispatch button,
reopen, and the failure escalation - which cancels the item and asks for a
split instead of shelving it under a reopen button. And a parked or
cancelled item stays that way when a killed run is banked as failed; that
overwrite is how a parked item grew a reopen button and bought run seven.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue
from bgate_core.store import settings
from bgate_ui.agents import dispatch, followup


def _spent(root, item_id: int, runs: int) -> dict:
    """An item that has had `runs` runs and just failed the last one."""
    queue.set_status(root, item_id, "failed", result="no")
    for n in range(runs - 1):
        queue.reopen(root, item_id, f"round {n + 2}")
        queue.set_status(root, item_id, "failed", result="no")
    return queue.get(root, item_id)


class TestTheCap:
    def test_the_default_is_three_runs(self, root):
        assert queue.attempt_cap(root) == 3
        item = queue.add(root, "tech", "x", brief="x")
        assert queue.runs_of(item) == 1
        assert queue.over_attempt_cap(root, item) is False

    def test_reopen_refuses_past_the_cap_and_says_split(self, root):
        item = queue.add(root, "tech", "x", brief="x")
        spent = _spent(root, item["id"], 3)
        assert queue.runs_of(spent) == 3
        with pytest.raises(ValueError) as exc:
            queue.reopen(root, item["id"], "one more")
        assert "queue_add_chain" in str(exc.value)
        assert "dispatch.max_attempts" in str(exc.value)

    def test_the_cap_is_a_setting_the_human_can_raise(self, root):
        item = queue.add(root, "tech", "x", brief="x")
        _spent(root, item["id"], 3)
        settings.set(root, "dispatch.max_attempts", 5)
        assert queue.reopen(root, item["id"], "a fourth, on purpose")["status"] == "queued"

    def test_readiness_never_lists_an_over_cap_item(self, root):
        item = queue.add(root, "tech", "x", brief="x")
        _spent(root, item["id"], 3)
        # Force it queued behind the guard, the way a raw status write would.
        queue.set_status(root, item["id"], "queued")
        assert item["id"] not in {r["id"] for r in queue.ready(root)}

    def test_dispatch_refuses_with_the_cap_named(self, root, monkeypatch):
        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        item = queue.add(root, "tech", "x", brief="x")
        _spent(root, item["id"], 3)
        queue.set_status(root, item["id"], "queued")
        got = dispatch._spawn(str(root), item["id"])
        assert got["ok"] is False and got["code"] == "attempt_cap"
        assert got["detail"]["runs"] == 3 and got["detail"]["cap"] == 3


class TestDeadItemsStayDead:
    def test_a_parked_item_is_not_failed_by_a_late_reap(self, root):
        item = queue.add(root, "tech", "x", brief="x")
        queue.park(root, item["id"], "later")
        queue.set_status(root, item["id"], "failed", result="session exited 137")
        assert queue.get(root, item["id"])["status"] == "parked"

    def test_a_cancelled_item_is_not_failed_by_a_late_reap(self, root):
        item = queue.add(root, "tech", "x", brief="x")
        queue.cancel(root, item["id"], "split")
        queue.set_status(root, item["id"], "failed", result="session exited 137")
        assert queue.get(root, item["id"])["status"] == "cancelled"


class TestTheEscalationSplits:
    def test_at_the_cap_the_item_is_cancelled_and_the_director_is_asked_to_split(self, root):
        item = queue.add(root, "gameplay", "five deliverables", brief="x")
        _spent(root, item["id"], 3)
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "no path"})
        assert queue.get(root, item["id"])["status"] == "cancelled"
        director = [r for r in queue.list_items(root, status="queued")
                    if r["seat"] == "director"]
        assert director, "no escalation filed"
        assert "CANCELLED AT THE RUN CAP" in director[-1]["brief"]
        assert "queue_add_chain" in director[-1]["brief"]

    def test_under_the_cap_the_item_is_merely_escalated(self, root):
        item = queue.add(root, "gameplay", "one deliverable", brief="x")
        _spent(root, item["id"], 2)
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "no path"})
        assert queue.get(root, item["id"])["status"] == "failed"
