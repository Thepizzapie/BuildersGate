"""Milestone QA: one reviewer per completed human checkpoint, not one per item.

The default ``qa.gate`` scope. Measured on cat-cafe (2026-10-27): per-item QA
was 125 of 266 items and 49% of run spend, while the only judge who ever played
the game was never asked. A finished maker item now waits for the next
checkpoint; the checkpoint gets one round covering everything since the last
one, and the verdict goes to the human signing it off.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue
from bgate_core.store import db, settings
from bgate_ui.agents import dispatch as _dispatch
from bgate_ui.agents import followup
from bgate_ui.agents import qa_gate

EPOCH = "1970-01-01 00:00:00"


@pytest.fixture()
def dispatched(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(_dispatch, "dispatch",
                        lambda root, item_id, **kw: calls.append(item_id) or
                        {"ok": True, "item_id": item_id})
    return calls


def _gates(root) -> list[dict]:
    return [dict(r) for r in db.connect(root).execute(
        "SELECT * FROM work_item WHERE source = 'qa-gate' ORDER BY id")]


def _milestone(root) -> tuple[dict, dict]:
    """A maker item and the checkpoint that waits on it, both completed."""
    sprite = queue.add(root, "art", "keeper sprite")
    cp = queue.add(root, "gameplay", "first rescue playable",
                   depends_on=int(sprite["id"]), checkpoint=True,
                   checkpoint_note="rescue the grey cat")
    queue.set_status(root, int(sprite["id"]), "done", result="drew it")
    cp = queue.complete(root, int(cp["id"]), result="rescue works end to end")
    return queue.get(root, int(sprite["id"])), cp


def test_milestones_is_the_default_scope(root):
    assert qa_gate.gate_scope(root) == "milestones"
    assert followup.load_settings(root)["qa_gate"] == "milestones"


def test_a_done_maker_item_gets_no_round_of_its_own(root, dispatched):
    item = queue.add(root, "tech", "a script")
    queue.set_status(root, int(item["id"]), "done", result="wired it")
    qa_gate._scan_once(str(root), EPOCH)
    assert _gates(root) == []
    assert dispatched == []


def test_a_completed_checkpoint_gets_one_round_for_the_milestone(root, dispatched):
    sprite, cp = _milestone(root)
    assert cp["status"] == "review"
    qa_gate._scan_once(str(root), EPOCH)
    gates = _gates(root)
    assert len(gates) == 1
    gate = gates[0]
    assert gate["seat"] == "qa"
    assert gate["source_ref"] == str(cp["id"])
    assert f"#{sprite['id']}" in gate["brief"]
    assert "rescue the grey cat" in gate["brief"]
    assert dispatched == [int(gate["id"])]
    # The per-item gate stays off: the sprite got no round of its own.
    assert not [g for g in gates if g["source_ref"] == str(sprite["id"])]


def test_rescanning_does_not_file_a_second_round(root, dispatched):
    _milestone(root)
    qa_gate._scan_once(str(root), EPOCH)
    qa_gate._scan_once(str(root), EPOCH)
    assert len(_gates(root)) == 1
    assert len(dispatched) == 1


def test_the_router_action_files_and_dispatches_the_round(root, dispatched):
    _, cp = _milestone(root)
    got = followup._do_qa_milestone(root, {"item": int(cp["id"]), "why": "x"})
    assert got.get("ok") and got.get("dispatched")
    again = followup._do_qa_milestone(root, {"item": int(cp["id"]), "why": "x"})
    assert not again.get("ok")
    assert len(_gates(root)) == 1


def test_a_signed_off_checkpoint_is_not_reviewed(root, dispatched):
    _, cp = _milestone(root)
    queue.approve(root, int(cp["id"]), by="adrian")
    got = followup._do_qa_milestone(root, {"item": int(cp["id"]), "why": "x"})
    assert not got.get("ok")
    assert _gates(root) == []


def test_the_per_item_gate_is_still_one_setting_away(root, dispatched):
    settings.set(root, "qa.gate", "all")
    item = queue.add(root, "tech", "a script")
    queue.set_status(root, int(item["id"]), "done", result="wired it")
    qa_gate._scan_once(str(root), EPOCH)
    assert [g["source_ref"] for g in _gates(root)] == [str(item["id"])]
