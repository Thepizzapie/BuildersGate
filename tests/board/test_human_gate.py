"""Questions to the human carry their choices; a reopened item is dispatchable.

MEASURED (exit-67-r2, 2026-09-22): a director item was reopened for a third
run and sat queued but invisible (runs_of >= cap).
"""
from __future__ import annotations

from bgate_core.board import queue, steerbox


class TestTheCapCountsRuns:
    def test_a_reopen_for_the_last_allowed_run_is_dispatchable(self, root):
        item = queue.add(root, "director", "unblock", brief="decide")
        queue.set_status(root, item["id"], "failed", result="no")
        queue.reopen(root, item["id"], "round 2")
        queue.set_status(root, item["id"], "failed", result="no")
        queue.reopen(root, item["id"], "round 3")          # third of three
        assert item["id"] in {r["id"] for r in queue.ready(root)}
        queue.set_status(root, item["id"], "failed", result="no")
        assert queue.at_attempt_cap(root, queue.get(root, item["id"]))
        try:
            queue.reopen(root, item["id"], "round 4")
        except ValueError:
            pass
        else:
            raise AssertionError("a fourth run must be refused")


class TestAQuestionCarriesItsChoices:
    def test_options_are_stored_shaped_and_capped(self, root):
        got = steerbox.ask(root, "flatten the riser or reroute the bot?",
                           options=["Flatten it", "Reroute", "Reroute", " ",
                                    "Cut the section", "x" * 200, "e", "f", "g"])
        assert got["options"][:3] == ["Flatten it", "Reroute", "Cut the section"]
        assert len(got["options"]) == steerbox.MAX_OPTIONS
        assert all(len(o) <= steerbox.MAX_OPTION for o in got["options"])
        q = steerbox.question(root, got["seq"])
        assert q["options"] == got["options"]
        assert [x for x in steerbox.open_questions(root)
                if x["event_seq"] == got["seq"]][0]["options"] == got["options"]

    def test_a_question_without_options_has_an_empty_list(self, root):
        got = steerbox.ask(root, "anything?")
        assert steerbox.question(root, got["seq"])["options"] == []

    def test_clicking_an_option_is_an_ordinary_answer(self, root):
        got = steerbox.ask(root, "A or B?", options=["A", "B"])
        out = steerbox.answer(root, got["seq"], "B", by="human")
        assert steerbox.question(root, got["seq"])["answer"] == "B"
        assert out["ok"]
