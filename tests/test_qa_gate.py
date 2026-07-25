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

import pytest

from bgate_core import db, queue
from bgate_ui import dispatch as _dispatch
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

    @pytest.mark.parametrize("seat", ("qa", "director", "tech"))
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

    def test_several_originals_each_get_their_own_gate(self, root, dispatched):
        ids = [_done(root, "art", f"asset {n}")["id"] for n in range(3)]
        qa_gate._scan_once(root, EPOCH)
        assert sorted(g["source_ref"] for g in _gates(root)) == \
            sorted(str(i) for i in ids)
        assert len(dispatched) == 3


class TestStart:
    def test_disabled_by_env(self, root, monkeypatch):
        monkeypatch.setattr(qa_gate, "_started", qa_gate.threading.Event())
        for value in ("0", "false", "off"):
            monkeypatch.setenv("BGATE_QA_GATE", value)
            assert qa_gate.start(str(root)) is False

    def test_start_is_idempotent_and_does_not_block(self, root, monkeypatch):
        monkeypatch.setattr(qa_gate, "_started", qa_gate.threading.Event())
        monkeypatch.delenv("BGATE_QA_GATE", raising=False)
        # POLL_S is long enough that the thread sleeps for the whole test; it is
        # a daemon, so it cannot outlive the run.
        assert qa_gate.start(str(root)) is True
        assert qa_gate.start(str(root)) is True
        threads = [t for t in qa_gate.threading.enumerate()
                   if t.name == "bgate-qa-gate"]
        assert len(threads) == 1
        assert threads[0].daemon

    def test_the_watcher_swallows_a_broken_scan(self, root, monkeypatch):
        """Fail-safe: the gate must never take the dashboard down. (It is also
        why the placeholder bug went unseen — hence the direct _scan_once tests
        above.)"""
        boom = []

        def _raise(*_a, **_k):
            boom.append(1)
            raise RuntimeError("seeded failure")

        monkeypatch.setattr(qa_gate, "_scan_once", _raise)
        monkeypatch.setattr(qa_gate, "POLL_S", 0.01)
        monkeypatch.setattr(qa_gate, "_started", qa_gate.threading.Event())
        monkeypatch.delenv("BGATE_QA_GATE", raising=False)
        assert qa_gate.start(str(root)) is True
        deadline = qa_gate.time.monotonic() + 5
        while not boom and qa_gate.time.monotonic() < deadline:
            qa_gate.time.sleep(0.01)
        assert boom, "the watcher never scanned"
        # Still alive after raising.
        assert any(t.name == "bgate-qa-gate" and t.is_alive()
                   for t in qa_gate.threading.enumerate())
