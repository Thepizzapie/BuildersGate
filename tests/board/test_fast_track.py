"""The QA gate scope and the brief budget.

Two levers for a board that is spending its slots on ceremony: a chain gets
one reviewer instead of one per link, and a brief may be as long as the
feature needs.
"""
from __future__ import annotations


from bgate_core.board import queue
from bgate_core.store import settings
from bgate_ui.agents import followup, qa_gate


class TestQaGateScope:
    def _done_event(self, item):
        return {"id": 1, "kind": "item.done", "ref": str(item["id"]),
                "created_at": "2026-10-04 00:00:00"}

    def _settings(self, scope):
        return {"gate_mode": "agent", "gated_seats": qa_gate.GATED_SEATS,
                "qa_gate": scope, "max_rounds": 3, "max_per_hour": 4}

    def test_all_reviews_a_single_item(self):
        item = {"id": 5, "seat": "gameplay", "source": "manual", "attempts": 0,
                "updated_at": "2026-10-04 00:00:01", "result": "done"}
        board = {"items": {5: item}, "qa": {5: {"chain_end": False}}}
        got = followup._branch_qa(self._done_event(item), item, self._settings("all"), board)
        assert got and got[0]["kind"] == "qa_spawn"

    def test_chains_skips_a_single_item_and_a_middle_link(self):
        single = {"id": 5, "seat": "gameplay", "source": "manual", "attempts": 0,
                  "updated_at": "2026-10-04 00:00:01", "result": "done"}
        middle = {"id": 6, "seat": "gameplay", "source": "manual", "attempts": 0,
                  "chain_id": "c1", "chain_pos": 1,
                  "updated_at": "2026-10-04 00:00:01", "result": "done"}
        board = {"items": {5: single, 6: middle},
                 "qa": {5: {"chain_end": False}, 6: {"chain_end": False}}}
        for item in (single, middle):
            got = followup._branch_qa(self._done_event(item), item,
                                      self._settings("chains"), board)
            assert got and got[0]["kind"] == "skip"

    def test_chains_reviews_the_last_link(self):
        last = {"id": 7, "seat": "gameplay", "source": "manual", "attempts": 0,
                "chain_id": "c1", "chain_pos": 2,
                "updated_at": "2026-10-04 00:00:01", "result": "done"}
        board = {"items": {7: last}, "qa": {7: {"chain_end": True}}}
        got = followup._branch_qa(self._done_event(last), last, self._settings("chains"), board)
        assert got and got[0]["kind"] == "qa_spawn"

    def test_none_reviews_nothing(self):
        last = {"id": 7, "seat": "art", "source": "manual", "attempts": 0,
                "chain_id": "c1", "chain_pos": 2,
                "updated_at": "2026-10-04 00:00:01", "result": "per-frame verdict ok"}
        board = {"items": {7: last}, "qa": {7: {"chain_end": True}}}
        got = followup._branch_qa(self._done_event(last), last, self._settings("none"), board)
        assert got and got[0]["kind"] == "skip"

    def test_is_chain_end_reads_the_board(self, root):
        links = [queue.add(root, "gameplay", f"link {i}", brief="x",
                           chain_id="c9", chain_pos=i) for i in (1, 2, 3)]
        assert not qa_gate.is_chain_end(root, links[0])
        assert qa_gate.is_chain_end(root, links[-1])
        assert not qa_gate.is_chain_end(root, {"id": 99, "chain_id": ""})

    def test_scope_setting_is_read_per_project(self, root):
        assert qa_gate.gate_scope(root) == "all"
        settings.set(root, "qa.gate", "chains")
        assert qa_gate.gate_scope(root) == "chains"
        assert followup.load_settings(root)["qa_gate"] == "chains"


class TestBriefBudget:
    LONG = "x" * 1200

    def test_default_budget_flags_a_long_brief(self, root):
        got = queue.scope_grade(self.LONG, root=root)
        assert any("characters" in r for r in got["reasons"])

    def test_raised_budget_lets_a_feature_brief_through(self, root):
        settings.set(root, "board.brief_chars", 3000)
        got = queue.scope_grade(self.LONG, root=root)
        assert not any("characters" in r for r in got["reasons"])

    def test_budget_without_a_root_is_the_guideline(self):
        assert queue.brief_budget(None) == (900, 2)
