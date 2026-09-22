"""A failure reaches someone who can act, every time, and the director is
dispatched to act on it.

MEASURED (exit-67-r2, 2026-09-22): the graybox gate #7 failed, was escalated
(#52, closed), was reopened and FAILED AGAIN with a real finding - a wall
in overpass_climb the bot cannot clear - and that second failure reached
nobody: one escalation per item, ever. The board sat idle for an hour with
a diagnosed defect in its result note. The QA-loop escalation was held for
a human on top of that.
"""
from __future__ import annotations

from bgate_core.board import queue
from bgate_ui.agents import followup


class TestEveryFailureReachesTheDirector:
    def test_a_closed_escalation_does_not_silence_the_next_failure(self, root):
        item = queue.add(root, "qa", "graybox gate", brief="drive the loop")
        queue.set_status(root, item["id"], "failed", result="no probe result")
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "first"})
        first = [r for r in queue.list_items(root)
                 if r["source"] == followup.FAIL_ESCALATION_SOURCE]
        assert len(first) == 1
        queue.set_status(root, first[0]["id"], "done", result="reopened it")
        queue.reopen(root, item["id"], "try once more")
        queue.set_status(root, item["id"], "failed",
                         result="a wall at column 8 blocks every seed")
        assert followup.fail_escalated(root, item["id"]) is False
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "second"})
        both = [r for r in queue.list_items(root)
                if r["source"] == followup.FAIL_ESCALATION_SOURCE]
        assert len(both) == 2

    def test_an_open_escalation_still_dedups(self, root):
        item = queue.add(root, "qa", "graybox gate", brief="x")
        queue.set_status(root, item["id"], "failed", result="no")
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "first"})
        assert followup.fail_escalated(root, item["id"]) is True
        followup._do_fail_escalate(str(root), {"item": item["id"], "reason": "again"})
        assert len([r for r in queue.list_items(root)
                    if r["source"] == followup.FAIL_ESCALATION_SOURCE]) == 1

    def test_the_qa_loop_escalation_is_dispatched_not_held(self, root):
        row = queue.add(root, "director", "QA loop: #5 failed 6 rounds",
                        brief="decide", source="qa-gate-escalation", source_ref="5")
        assert row["id"] in {r["id"] for r in queue.ready(root)}

    def test_the_brief_demands_ready_work_and_the_fix_and_rehang_move(self, root):
        item = queue.add(root, "qa", "graybox gate", brief="x")
        queue.set_status(root, item["id"], "failed", result="a wall blocks the bot")
        brief = followup.failure_escalation_brief(root, queue.get(root, item["id"]),
                                                  {"reason": "budget spent"})
        assert "DONE MEANS SOMETHING IS READY" in brief
        assert "queue_add_dependency" in brief and "FILE THE FIX" in brief


class TestTheAnswerReQueuesTheAsker:
    def test_answering_a_failed_items_question_reopens_it(self, root):
        from bgate_core.board import steerbox
        item = queue.add(root, "art", "player kit", brief="make it")
        q = steerbox.ask(root, "sheet failed twice - retry, change prompt or park?",
                         item_id=item["id"], seat="art",
                         options=["retry", "change the prompt", "park"])
        queue.set_status(root, item["id"], "failed", result="asked the human")
        steerbox.answer(root, q["seq"], "change the prompt", by="human")
        got = queue.get(root, item["id"])
        assert got["status"] == "queued"
        assert "the human answered: change the prompt" in (got.get("result") or "")
