"""ITEM 11/31: a per-item paid-call budget and a re-roll cap.

MEASURED FAILURE: EXIT 67 death frames were re-rolled at $0.05-0.10 each in a
loop with no stop; #44 reached $7.67 and #21 $10.10 across attempts before a
human killed the agent by hand.
"""
from __future__ import annotations

import pytest

from bgate_core.art import chroma
from bgate_core.board import queue as _q
from bgate_core.store import settings as _settings


@pytest.fixture()
def item(root):
    return _q.add(root, "art", "generate death frame")


class TestPaidCallBudget:
    def test_default_budget_is_the_setting(self, root, item):
        used, budget = _q.paid_call_budget(root, item["id"])
        assert used == 0
        assert budget == _settings.get(root, "dispatch.default_max_paid_calls")

    def test_spend_increments_and_refuses_at_the_cap(self, root, item):
        _q.update(root, item["id"], max_paid_calls=2)
        first = _q.spend_paid_call(root, item["id"])
        assert first["ok"] and first["used"] == 1
        second = _q.spend_paid_call(root, item["id"])
        assert second["ok"] and second["used"] == 2
        third = _q.spend_paid_call(root, item["id"])
        assert third["ok"] is False
        assert third["code"] == "budget_exceeded_item"
        assert third["used"] == 2 and third["budget"] == 2

    def test_no_work_item_is_not_budget_checked(self, root):
        result = _q.spend_paid_call(root, None)
        assert result["ok"] is True

    def test_queue_update_raises_the_item_budget(self, root, item):
        _q.update(root, item["id"], max_paid_calls=1)
        used, budget = _q.paid_call_budget(root, item["id"])
        assert budget == 1
        _q.update(root, item["id"], max_paid_calls=99)
        used, budget = _q.paid_call_budget(root, item["id"])
        assert budget == 99


class TestChromaGenerateGate:
    def test_refuses_before_any_provider_call_once_budget_is_spent(self, root, item):
        _q.update(root, item["id"], max_paid_calls=1)
        _q.spend_paid_call(root, item["id"])  # exhaust the one call
        result = chroma.generate(
            "a death frame", str(root) + "/out.png",
            provider="openai", root=str(root), logical_name="death_frame",
            work_item_id=item["id"])
        assert result["ok"] is False
        assert result["code"] == "budget_exceeded_item"

    def test_reroll_cap_refuses_the_third_attempt(self, root, item, monkeypatch):
        from bgate_core.store import artifacts as _artifacts

        df = root / "df.png"
        df.write_bytes(b"fake-png")
        # Two prior revisions of the same logical_name inside this item -
        # simulating two already-generated attempts.
        for _ in range(2):
            _artifacts.register(
                root, "death_frame", df, producer="test",
                work_item_id=item["id"])
        result = chroma.generate(
            "a death frame", str(root) + "/out.png",
            provider="openai", root=str(root), logical_name="death_frame",
            work_item_id=item["id"])
        assert result["ok"] is False
        assert result["code"] == "reroll_cap"
        assert result["attempts"] == 2

    def test_reroll_cap_lifts_with_a_replace_reason(self, root, item, monkeypatch):
        from bgate_core.store import artifacts as _artifacts

        df = root / "df.png"
        df.write_bytes(b"fake-png")
        for _ in range(2):
            _artifacts.register(
                root, "death_frame", df, producer="test",
                work_item_id=item["id"])

        # Stub the first thing generate() does AFTER the reroll gate - if the
        # gate had refused, this stub is never reached and no exception
        # comes out at all. Raising proves generate() got past the gate.
        def boom(*a, **k):
            raise RuntimeError("past the reroll gate")

        monkeypatch.setattr(chroma, "pick_report", boom)
        with pytest.raises(RuntimeError, match="past the reroll gate"):
            chroma.generate(
                "a death frame", str(root) + "/out.png",
                provider="openai", root=str(root), logical_name="death_frame",
                task_kind="sprite", work_item_id=item["id"],
                replace_reason="tried a new pose")
