"""The auto-QA gate's scan, against a seeded database.

This module had NO test and was silently dead in production: the scan query
hardcoded three ``?`` placeholders while GATED_SEATS grew to four, so every
pass raised a binding error that the deliberate fail-safe swallowed. Nothing
was ever reviewed and nothing ever said so. The tests below pin the parts that
failure touched — the placeholder/parameter agreement, which seats are gated,
the one-open-round rule, and the cutoff — by calling ``_scan_once`` directly so
a raise is a failure instead of a shrug.
"""
from __future__ import annotations

import threading
import time

import pytest

from bgate_core import db, queue
from bgate_ui import dispatch as _dispatch
from bgate_ui import followup
from bgate_ui import qa_gate

EPOCH = "1970-01-01 00:00:00"


@pytest.fixture()
def dispatched(monkeypatch):
    """Capture the gate's dispatch calls instead of spawning agents."""
    calls: list[int] = []
    monkeypatch.setattr(_dispatch, "dispatch",
                        lambda root, item_id, **kw: calls.append(item_id) or
                        {"ok": True, "item_id": item_id})
    return calls


def _gates(root) -> list[dict]:
    return [dict(r) for r in db.connect(root).execute(
        "SELECT * FROM work_item WHERE source = 'qa-gate' ORDER BY id")]


def _done(root, seat: str, title: str, result: str = "shipped it", **kw) -> dict:
    item = queue.add(root, seat, title, **kw)
    return queue.set_status(root, item["id"], "done", result=result)


def _bump(root, item_id: int, stamp: str) -> None:
    """Force updated_at — SQLite stores second resolution, so a same-second
    re-completion is indistinguishable from the first without this."""
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET updated_at = ? WHERE id = ?",
                     (stamp, item_id))


class TestScan:
    def test_a_done_maker_item_gets_a_qa_follow_up_and_is_dispatched(
            self, root, dispatched):
        item = _done(root, "art", "HUD meter", result="frame + fill shipped")
        qa_gate._scan_once(root, EPOCH)

        gates = _gates(root)
        assert len(gates) == 1
        gate = gates[0]
        assert gate["seat"] == "qa"
        assert gate["source_ref"] == str(item["id"])
        assert gate["priority"] == 8
        assert str(item["id"]) in gate["title"] and "HUD meter" in gate["title"]
        # The brief must carry the CLAIM, or the reviewer has nothing to check.
        assert "frame + fill shipped" in gate["brief"]
        assert f"item-{item['id']}.jsonl" in gate["brief"]
        assert dispatched == [gate["id"]]

    @pytest.mark.parametrize("seat", qa_gate.GATED_SEATS)
    def test_every_gated_seat_is_reviewed(self, root, dispatched, seat):
        """Also the regression guard for the placeholder bug: the query builds
        its ``?`` list FROM GATED_SEATS, so adding a seat cannot desync it."""
        _done(root, seat, f"{seat} deliverable")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 1

    # tech WAS on this list: the old default left tech and cinematic closing
    # on the agent's word alone, and both are gated now. qa and director stay
    # ungated forever — that is recursion, not review.
    @pytest.mark.parametrize("seat", ("qa", "director"))
    def test_ungated_seats_are_ignored(self, root, dispatched, seat):
        _done(root, seat, f"{seat} deliverable")
        qa_gate._scan_once(root, EPOCH)
        assert _gates(root) == []
        assert dispatched == []

    def test_the_gate_never_gates_itself(self, root, dispatched):
        """A QA follow-up completing must not spawn a QA follow-up for it."""
        _done(root, "art", "sprite sheet")
        qa_gate._scan_once(root, EPOCH)
        gate_id = _gates(root)[0]["id"]
        # The reviewer completes its own item; it is seat 'qa' AND source
        # 'qa-gate' — both exclusions, belt and braces.
        queue.set_status(root, gate_id, "done", result="VERDICT: PASS")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 1

    def test_queued_and_failed_items_are_not_reviewed(self, root, dispatched):
        queue.add(root, "art", "still queued")
        failed = queue.add(root, "gameplay", "went wrong")
        queue.set_status(root, failed["id"], "failed", result="nope")
        qa_gate._scan_once(root, EPOCH)
        assert _gates(root) == []

    def test_items_completed_before_the_cutoff_are_not_reviewed(
            self, root, dispatched):
        """Server start must not QA-bomb the whole historical queue."""
        _done(root, "art", "ancient history")
        qa_gate._scan_once(root, "2999-01-01 00:00:00")
        assert _gates(root) == []
        # ...and the same item IS picked up once the cutoff allows it.
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 1


class TestRounds:
    def test_rescanning_does_not_duplicate_an_open_gate(self, root, dispatched):
        _done(root, "audio", "footstep set")
        for _ in range(3):
            qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 1
        assert len(dispatched) == 1

    def test_a_closed_gate_is_not_reopened_while_the_original_sits_still(
            self, root, dispatched):
        item = _done(root, "narrative", "barks pass")
        qa_gate._scan_once(root, EPOCH)
        gate = _gates(root)[0]
        queue.set_status(root, gate["id"], "done", result="VERDICT: PASS")
        # The original has not moved since the gate was created.
        _bump(root, item["id"], "2000-01-01 00:00:00")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 1

    def test_a_re_done_original_gets_a_fresh_round(self, root, dispatched):
        item = _done(root, "art", "the HUD again")
        qa_gate._scan_once(root, EPOCH)
        gate = _gates(root)[0]
        queue.set_status(root, gate["id"], "done",
                         result="VERDICT: FAIL — bare fills")
        queue.reopen(root, item["id"], "bare fills, no hollow frame")
        queue.set_status(root, item["id"], "done", result="round two")
        _bump(root, item["id"], "2999-01-01 00:00:00")  # strictly after the gate

        qa_gate._scan_once(root, EPOCH)
        gates = _gates(root)
        assert len(gates) == 2
        assert [g["source_ref"] for g in gates] == [str(item["id"])] * 2
        assert dispatched == [gates[0]["id"], gates[1]["id"]]

    def test_a_dead_reviewer_does_not_count_as_a_review(self, root, dispatched):
        """THE SILENT PASS, closed 2026-08-19. A QA agent that crashed was
        reaped 'failed' with no verdict — and because any closed gate row used
        to count as the last review, the original item then passed the gate
        without anyone ever looking at it. A verdict-less round now counts for
        nothing: the sweep files a fresh reviewer."""
        item = _done(root, "art", "the unseen HUD")
        qa_gate._scan_once(root, EPOCH)
        gate = _gates(root)[0]
        # The reviewer dies: the reaper banks it failed, no VERDICT anywhere.
        queue.set_status(root, gate["id"], "failed",
                         result="session exited 1 without reporting")
        _bump(root, item["id"], "2000-01-01 00:00:00")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 2          # a fresh round was filed

    def test_a_verdictless_done_round_does_not_count_either(self, root,
                                                            dispatched):
        """Exit 0 with no verdict text is the reap path's 'done' — the same
        silence wearing a green status."""
        item = _done(root, "art", "the unjudged HUD")
        qa_gate._scan_once(root, EPOCH)
        gate = _gates(root)[0]
        queue.set_status(root, gate["id"], "done",
                         result="reported success without calling queue_complete")
        _bump(root, item["id"], "2000-01-01 00:00:00")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == 2

    def test_a_reviewer_that_always_dies_escalates_instead_of_burning(
            self, root, dispatched):
        """The runaway guard: re-reviewing is bounded at twice the round cap
        in total filings, then the director decides."""
        item = _done(root, "art", "the cursed HUD")
        cap = qa_gate.max_rounds(root)
        for n in range(cap * 2):
            qa_gate._scan_once(root, EPOCH)
            open_gates = [g for g in _gates(root)
                          if g["status"] not in ("done", "failed")]
            assert len(open_gates) == 1, f"round {n}: expected one open gate"
            queue.set_status(root, open_gates[0]["id"], "failed",
                             result="died again")
            _bump(root, item["id"], "2000-01-01 00:00:00")
        qa_gate._scan_once(root, EPOCH)
        assert len(_gates(root)) == cap * 2    # no fresh round past the bound
        assert qa_gate.escalated(root, str(item["id"]))

    def test_several_originals_each_get_their_own_gate(self, root, dispatched):
        ids = [_done(root, "art", f"asset {n}")["id"] for n in range(3)]
        qa_gate._scan_once(root, EPOCH)
        assert sorted(g["source_ref"] for g in _gates(root)) == \
            sorted(str(i) for i in ids)
        assert len(dispatched) == 3


class TestStart:
    """THE LOOP MOVED, so these test its new owner.

    This module used to own a daemon thread with a wall-clock cutoff — it
    reviewed "only transitions after the server started", so every completion
    that happened while the dashboard was down was never reviewed and nothing
    said so. `bgate_ui.followup` drives the gate from the event bus' cursor now,
    which is a row id and therefore survives a restart. What is asserted here is
    what the old tests asserted about the thread: the kill switch, idempotence,
    and that a raising tick cannot take the dashboard down.
    """

    def test_qa_gate_no_longer_owns_a_thread(self):
        """The regression this class exists to prevent from coming back. Two
        loops scanning for completed items is two QA rounds per deliverable."""
        for gone in ("start", "_run", "_started", "POLL_S"):
            assert not hasattr(qa_gate, gone), (
                f"qa_gate.{gone} is back — the router owns the loop now, and a "
                "second scanner double-gates every item")

    def test_disabled_by_env(self, root, monkeypatch):
        followup.reset()
        for value in ("0", "false", "off"):
            monkeypatch.setenv("BGATE_FOLLOWUP", value)
            assert followup.start(str(root)) is False

    def test_start_is_idempotent_and_does_not_block(self, root, monkeypatch):
        followup.reset()
        monkeypatch.delenv("BGATE_FOLLOWUP", raising=False)
        # The tick interval is long enough that the thread sleeps for the whole
        # test; it is a daemon, so it cannot outlive the run. Count only the
        # threads THIS test starts — an earlier test that built a TestClient ran
        # the app's startup hook, and its sleeping daemon would otherwise be
        # attributed here and fail out of order.
        def router_threads() -> set:
            return {t for t in threading.enumerate() if t.name == "bgate-followup"}

        before = router_threads()
        assert followup.start(str(root)) is True
        assert followup.start(str(root)) is True
        started = router_threads() - before
        assert len(started) == 1, "the second start() spawned another router"
        assert next(iter(started)).daemon

    def test_the_router_swallows_a_broken_tick(self, root, monkeypatch):
        """Fail-safe: the router must never take the dashboard down. It is also
        why the gate's decision logic is tested directly — a swallowed exception
        is exactly how the placeholder bug went unseen for so long."""
        boom = []

        def _raise(*_a, **_k):
            boom.append(1)
            raise RuntimeError("seeded failure")

        followup.reset()
        monkeypatch.setattr(followup, "tick", _raise)
        monkeypatch.setattr(followup, "POLL_S", 0.01, raising=False)
        monkeypatch.delenv("BGATE_FOLLOWUP", raising=False)
        assert followup.start(str(root)) is True
        deadline = time.monotonic() + 5
        while not boom and time.monotonic() < deadline:
            time.sleep(0.01)
        assert boom, "the router never ticked"
        assert any(t.name == "bgate-followup" and t.is_alive()
                   for t in threading.enumerate())
        followup.reset()
