"""EXIT 67 gripe 39c — a peer's landing warns a still-running neighbour.

An item's auto-commit can land inside another RUNNING agent's own seat lanes
(two art items sharing ``game/assets/**``, say) and nothing told the agent
still working that its files had just moved under it. ``_notify_peer_seats``
is the fix: after a successful auto-commit, every OTHER live entry in
``dispatch._live`` whose seat's write_globs overlap the committed paths gets
one steer.

Tested against ``dispatch._live`` directly (fake entries, no real
subprocess) — that dict is what the module actually has a pipe to, which is
the whole reason this lives in dispatch rather than being computed from the
database.
"""
from __future__ import annotations

from bgate_core.board import steerbox
from bgate_ui.agents import dispatch


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


def _entry(seat: str, alive: bool = True) -> dict:
    return {"proc": _FakeProc(alive), "seat": seat}


def test_a_running_peer_whose_lane_was_touched_gets_steered(root, monkeypatch):
    monkeypatch.setitem(dispatch._live, 42, _entry("art"))
    dispatch._notify_peer_seats(root, 7, ["game/assets/hero_walk.png"])

    pending = steerbox.pending(root, 42)
    assert pending, "the still-running peer must receive a steer"
    assert "peer #7 landed" in pending[0]["text"]
    assert "hero_walk.png" in pending[0]["text"]


def test_the_landing_item_itself_is_never_steered(root, monkeypatch):
    monkeypatch.setitem(dispatch._live, 7, _entry("art"))
    dispatch._notify_peer_seats(root, 7, ["game/assets/hero_walk.png"])
    assert steerbox.pending(root, 7) == []


def test_a_peer_whose_lane_was_not_touched_is_left_alone(root, monkeypatch):
    monkeypatch.setitem(dispatch._live, 99, _entry("audio"))
    dispatch._notify_peer_seats(root, 7, ["game/assets/hero_walk.png"])
    assert steerbox.pending(root, 99) == []


def test_a_peer_whose_process_already_exited_is_not_steered(root, monkeypatch):
    monkeypatch.setitem(dispatch._live, 55, _entry("art", alive=False))
    dispatch._notify_peer_seats(root, 7, ["game/assets/hero_walk.png"])
    assert steerbox.pending(root, 55) == []
