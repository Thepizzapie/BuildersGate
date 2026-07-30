"""The heartbeat — events that come from elapsed time, not from a transition.

THE GAP THIS CLOSES. The bus is transition-driven, and half of what goes wrong
is an ABSENCE of transitions: an item parked in `review` that nobody approves, a
chain whose next link never became ready, a question nobody answered. None of
those emit anything, so the quiet failure this whole surface exists to fix would
have been reintroduced one layer up.

Every test here drives a real board and a real `event` table — this file is the
only place the stall SQL is ever executed.
"""
from __future__ import annotations

import pytest

from bgate_core import db, events, gates, queue, settings
from bgate_ui import heartbeat


def _age(root, item_id: int, minutes: int) -> None:
    """Push an item's clock back. SQLite stores second resolution, so a test
    cannot wait for a two-hour window to elapse honestly."""
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE work_item SET updated_at = datetime('now', ?) WHERE id = ?",
            (f"-{int(minutes)} minutes", item_id))


def _kinds(root) -> list:
    return [e["kind"] for e in events.since(root, 0)["events"]]


@pytest.fixture()
def held(root):
    """A chain whose first link is finished and waiting on a human."""
    gates.set_mode(root, gates.BUILDERS)
    first, second = queue.add_chain(root, [
        {"seat": "tech", "title": "bake the scene"},
        {"seat": "gameplay", "title": "wire the view"}])
    queue.complete(root, first["id"], result="baked")     # -> review
    return first, second


class TestStall:
    def test_a_fresh_stall_says_nothing(self, root, held):
        """The window is the whole point: everything is 'not moving' the instant
        after it moves, and a reminder then is noise."""
        got = heartbeat.tick(root)
        assert got["stalled"] == [] and got["aging"] == []

    def test_a_chain_parked_past_the_window_is_reported_once(self, root, held):
        first, _second = held
        _age(root, first["id"], 240)
        got = heartbeat.tick(root)
        assert len(got["stalled"]) == 1
        payload = got["stalled"][0]
        assert payload["head"]["item"] == first["id"]
        assert payload["reason"] and payload["idle_min"] >= 120
        assert "chain.stalled" in _kinds(root)

        # Second pass, nothing moved: silence, not a repeat. A reminder that
        # fires every tick is a channel nobody reads within an hour.
        again = heartbeat.tick(root)
        assert again["stalled"] == []

    def test_movement_re_arms_the_reminder(self, root, held):
        first, _second = held
        _age(root, first["id"], 240)
        heartbeat.tick(root)
        queue.approve(root, first["id"], by="adrian")     # it moved
        heartbeat.tick(root)                              # clears the mark
        _age(root, _second["id"], 240)
        assert heartbeat.tick(root)["stalled"], \
            "a chain that stalled AGAIN after moving must speak again"

    def test_a_running_head_is_not_a_stall(self, root):
        first, _ = queue.add_chain(root, [
            {"seat": "tech", "title": "bake"}, {"seat": "art", "title": "draw"}])
        queue.set_status(root, first["id"], "dispatched")
        _age(root, first["id"], 600)
        assert heartbeat.tick(root)["stalled"] == [], \
            "an agent is working on it — that is not a stall"

    def test_a_finished_chain_drops_its_mark(self, root, held):
        first, second = held
        _age(root, first["id"], 240)
        heartbeat.tick(root)
        queue.approve(root, first["id"], by="adrian")
        queue.set_status(root, second["id"], "done", result="wired")
        got = heartbeat.tick(root)
        assert got["stalled"] == []
        assert got["cleared"] >= 1, "the mark for a finished chain was kept"


class TestAging:
    def test_a_review_item_past_the_window_ages(self, root):
        gates.set_mode(root, gates.BUILDERS)
        item = queue.add(root, "art", "a sprite")
        queue.complete(root, item["id"], result="drew it")
        _age(root, item["id"], 240)
        got = heartbeat.tick(root)
        assert [a["item"] for a in got["aging"]] == [item["id"]]
        assert "item.aging" in _kinds(root)

    def test_one_situation_never_produces_two_pings(self, root, held):
        """A chain head sitting in `review` is both a stalled chain and an aging
        item. Two events for one thing to do is how a channel earns its mute."""
        first, _second = held
        _age(root, first["id"], 240)
        got = heartbeat.tick(root)
        stalled_items = {p["head"]["item"] for p in got["stalled"]}
        aging_items = {p["item"] for p in got["aging"]}
        assert not (stalled_items & aging_items)


class TestWindowAndSafety:
    def test_the_window_comes_from_the_registry(self, root):
        assert heartbeat.window_h(root) == \
            settings.setting("notify.stall_hours").default
        settings.set(root, "notify.stall_hours", 1)
        assert heartbeat.window_h(root) == 1

    def test_an_unreadable_setting_falls_back_rather_than_raising(self, root,
                                                                  monkeypatch):
        monkeypatch.setattr(settings, "get",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        assert heartbeat.window_h(root) == heartbeat.DEFAULT_STALL_H

    def test_a_tick_on_a_broken_board_never_raises(self, root, monkeypatch):
        """It runs on the router's thread. An exception here stops the follow-up
        loop, which stops notifications — the failure being fixed."""
        monkeypatch.setattr(heartbeat, "_rows",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        got = heartbeat.tick(root)
        assert got["stalled"] == [] and got["aging"] == []

    def test_reset_forgets_every_mark(self, root, held):
        first, _second = held
        _age(root, first["id"], 240)
        assert heartbeat.tick(root)["stalled"]
        heartbeat.reset(root)
        _age(root, first["id"], 240)
        assert heartbeat.tick(root)["stalled"], "reset did not clear the marks"
