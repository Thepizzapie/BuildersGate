"""board_wait: one wake instead of one per event.

MEASURED (GNOMEFALL, 2026-10-16): a director following the board through a
per-event monitor woke ~25 times an hour, mostly for "#NNN qa DONE" lines it
did nothing about. `wait` returns once, for the reasons a director acts on.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue, wait
from bgate_core.store import db


def _set_status(root, item_id: int, status: str) -> None:
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET status = ? WHERE id = ?", (status, item_id))


class FakeClock:
    """A clock the test advances; sleep() moves it and runs a hook so the
    board can change 'while' the wait is asleep."""

    def __init__(self):
        self.t = 0.0
        self.on_sleep = None
        self.sleeps = 0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def _wait(root, clock, **kw):
    return wait.wait(root, clock=clock.now, sleep=clock.sleep, poll_s=10, **kw)


class TestReasons:
    def test_an_empty_board_returns_clear_at_once(self, root):
        clock = FakeClock()
        out = _wait(root, clock, until="attention", timeout_s=600)
        assert out["reason"] == "clear"
        assert clock.sleeps == 0

    def test_a_done_item_does_not_wake_attention_but_clear_does(self, root):
        a = queue.add(root, "tech", "a", brief="x")
        b = queue.add(root, "art", "b", brief="x")
        clock = FakeClock()

        def board_moves(n):
            if n == 1:
                _set_status(root, a["id"], "done")
            if n == 2:
                _set_status(root, b["id"], "done")

        clock.on_sleep = board_moves
        out = _wait(root, clock, until="attention", timeout_s=600)
        assert out["reason"] == "clear"
        assert clock.sleeps == 2            # the first 'done' did not wake it
        assert out["since"]["done"] == [a["id"], b["id"]]

    def test_change_wakes_on_the_first_finished_item(self, root):
        a = queue.add(root, "tech", "a", brief="x")
        queue.add(root, "art", "b", brief="x")
        clock = FakeClock()
        clock.on_sleep = lambda n: _set_status(root, a["id"], "done") if n == 1 else None
        out = _wait(root, clock, until="change", timeout_s=600)
        assert out["reason"] == "change"
        assert out["since"]["done"] == [a["id"]]
        assert clock.sleeps == 1

    def test_attention_wakes_on_a_new_failed_item_and_names_it(self, root):
        a = queue.add(root, "tech", "a", brief="x")
        queue.add(root, "art", "b", brief="x")
        clock = FakeClock()
        clock.on_sleep = lambda n: _set_status(root, a["id"], "failed") if n == 1 else None
        out = _wait(root, clock, until="attention", timeout_s=600)
        assert out["reason"] == "attention"
        assert [x["id"] for x in out["since"]["attention"]] == [a["id"]]
        assert out["since"]["failed"] == [a["id"]]

    def test_attention_ignores_what_was_already_waiting_when_the_call_began(self, root):
        stale = queue.add(root, "director", "old question", brief="x")
        queue.add(root, "art", "b", brief="x")
        clock = FakeClock()
        out = _wait(root, clock, until="attention", timeout_s=30)
        assert out["reason"] == "timeout"
        assert out["since"]["attention"] == []
        assert stale["id"] in out["snapshot"]["open"]

    def test_a_director_item_filed_during_the_wait_wakes_it(self, root):
        queue.add(root, "art", "b", brief="x")
        clock = FakeClock()
        clock.on_sleep = (lambda n: queue.add(root, "director", "ADVANCE: slice -> production",
                                              brief="x") if n == 2 else None)
        out = _wait(root, clock, until="attention", timeout_s=600)
        assert out["reason"] == "attention"
        assert out["since"]["attention"][0]["seat"] == "director"
        assert clock.sleeps == 2

    def test_items_waits_for_every_named_id(self, root):
        a = queue.add(root, "tech", "a", brief="x")
        b = queue.add(root, "art", "b", brief="x")
        queue.add(root, "qa", "c", brief="x")
        clock = FakeClock()

        def moves(n):
            if n == 1:
                _set_status(root, a["id"], "done")
            if n == 3:
                _set_status(root, b["id"], "cancelled")

        clock.on_sleep = moves
        out = _wait(root, clock, until="items", item_ids=f"{a['id']},#{b['id']}", timeout_s=600)
        assert out["reason"] == "items"
        assert clock.sleeps == 3

    def test_quiet_means_nothing_running_and_nothing_dispatched(self, root):
        a = queue.add(root, "tech", "a", brief="x")
        _set_status(root, a["id"], "dispatched")
        clock = FakeClock()
        clock.on_sleep = lambda n: _set_status(root, a["id"], "queued") if n == 2 else None
        out = _wait(root, clock, until="quiet", timeout_s=600)
        assert out["reason"] == "quiet"
        assert clock.sleeps == 2

    def test_timeout_is_a_reason_not_an_error_and_is_capped(self, root):
        queue.add(root, "tech", "a", brief="x")
        clock = FakeClock()
        out = _wait(root, clock, until="clear", timeout_s=10_000_000)
        assert out["reason"] == "timeout"
        assert out["waited_s"] <= wait.MAX_TIMEOUT_S

    def test_bad_arguments_fail_loudly(self, root):
        with pytest.raises(ValueError):
            wait.wait(root, until="soon", timeout_s=1, sleep=lambda s: None)
        with pytest.raises(ValueError):
            wait.wait(root, until="items", timeout_s=1, sleep=lambda s: None)


class TestSnapshot:
    def test_snapshot_counts_and_flags_what_needs_the_director(self, root):
        queue.add(root, "tech", "a", brief="x")
        d = queue.add(root, "director", "decide", brief="x")
        snap = wait.snapshot(root)
        assert snap["counts"]["queued"] == 2
        assert [x["id"] for x in snap["attention"]] == [d["id"]]
        assert "titles" not in wait._public(snap)
