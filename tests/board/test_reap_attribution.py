"""ITEM 27 — a chained agent's death is attributed to what it was CARRYING.

MEASURED (EXIT 67, 2026-09-20/21): a run dispatched on #4 chained through
queue_claim_next to #35, #13, #42, #46 and died at the runtime ceiling
mid-#46. The old ``_trip`` banked the kill against the item it was FIRST
dispatched on (#4) unconditionally, even though #4 had already been closed
by queue_complete hours earlier. Five items needed a manual re-close.

``_holder_item`` is the fix: it asks which item this run's actor stamp
(``agent:item-<id>``) still holds open (status == 'dispatched'), and that is
what a ceiling kill fails — never an item that already settled.
"""
from __future__ import annotations

from bgate_core.board import queue
from bgate_ui.agents import dispatch


def _dispatch_row(root, seat="tech", title="root item") -> int:
    item = queue.add(root, seat, title, brief="do the thing")
    queue.set_status(root, item["id"], "dispatched")
    return int(item["id"])


def _claim_row(root, original_id: int, seat="tech", title="chained item") -> int:
    """A second item claimed by the same run, the way queue_claim_next stamps it."""
    item = queue.add(root, seat, title, brief="do the next thing")
    queue.set_status(root, item["id"], "dispatched")
    queue.set_run_fields(root, item["id"], actor=f"agent:item-{original_id}")
    return int(item["id"])


class TestHolderItem:
    def test_holder_is_the_open_chained_claim_not_the_original(self, root):
        original = _dispatch_row(root, title="#4 original")
        # #4 finished and closed HOURS earlier via queue_complete.
        queue.complete(root, original, result="done for real", failed=False)
        chained = _claim_row(root, original, title="#46 chained, still open")

        holder = dispatch._holder_item(root, original)

        assert holder == chained
        assert queue.get(root, original)["status"] == "done"

    def test_holder_falls_back_to_original_when_nothing_was_ever_claimed(self, root):
        original = _dispatch_row(root, title="ordinary dispatch, no chain")

        assert dispatch._holder_item(root, original) == original

    def test_holder_is_none_once_everything_the_run_touched_is_settled(self, root):
        original = _dispatch_row(root, title="closed original")
        queue.complete(root, original, result="done", failed=False)
        chained = _claim_row(root, original, title="closed chain link")
        queue.complete(root, chained, result="also done", failed=False)

        assert dispatch._holder_item(root, original) is None

    def test_holder_prefers_the_latest_of_several_open_claims(self, root):
        original = _dispatch_row(root, title="original")
        queue.complete(root, original, result="done", failed=False)
        _claim_row(root, original, title="earlier open claim")
        latest = _claim_row(root, original, title="latest open claim")

        assert dispatch._holder_item(root, original) == latest


class TestTripAttribution:
    def test_ceiling_kill_fails_the_chained_holder_not_the_closed_original(
        self, root, monkeypatch
    ):
        original = _dispatch_row(root, title="#4 closed hours ago")
        queue.complete(root, original, result="genuinely finished", failed=False)
        holder = _claim_row(root, original, title="#46 still being worked")

        class _FakeProc:
            pid = 999999

            def poll(self):
                return 137

        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: None)
        monkeypatch.setattr(dispatch, "_final_event", lambda root_, item_id: {})
        monkeypatch.setattr(dispatch, "_finalize", lambda *a, **k: None)
        entry = {"proc": _FakeProc(), "started_at": 0.0, "log": ""}

        dispatch._trip(root, original, entry, "killed: ran past the runtime ceiling")

        assert queue.get(root, original)["status"] == "done", (
            "the item that closed hours ago must not be reopened as failed"
        )
        assert queue.get(root, holder)["status"] == "failed", (
            "the item the run was actually holding when killed must be failed"
        )
