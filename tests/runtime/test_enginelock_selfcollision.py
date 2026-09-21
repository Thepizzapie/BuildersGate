"""ITEM #19c: a refusal from the SAME work item is self_collision, not
'another Godot process is running' — the sentence that sent an agent
looking for somebody else's process when the process was its own.
"""
from __future__ import annotations

import os

import pytest

from bgate_core.runtime import enginelock


@pytest.fixture(autouse=True)
def _clean_work_item(monkeypatch):
    monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
    yield


def test_a_stranger_holding_the_lock_is_not_self_collision(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_WORK_ITEM", "42")
    path = enginelock.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"pid": 999, "what": "godot_run", "work_item": "7", '
        f'"expires_at": {__import__("time").time() + 900}}}',
        encoding="utf-8")
    with pytest.raises(enginelock.EngineBusy) as exc:
        with enginelock.hold(tmp_path, "godot_evidence", wait_s=0.01):
            pass
    assert exc.value.self_collision is False
    assert "another Godot" in str(exc.value)


def test_the_same_work_item_is_reported_as_self_collision(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_WORK_ITEM", "42")
    path = enginelock.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"pid": 999, "what": "godot_run", "work_item": "42", '
        f'"expires_at": {__import__("time").time() + 900}}}',
        encoding="utf-8")
    with pytest.raises(enginelock.EngineBusy) as exc:
        with enginelock.hold(tmp_path, "godot_evidence", wait_s=0.01):
            pass
    assert exc.value.self_collision is True
    assert exc.value.holder_pid == 999
    assert "YOUR OWN driver" in str(exc.value)
    assert "stop your own" in str(exc.value)


def test_no_work_item_at_all_is_never_self_collision(tmp_path, monkeypatch):
    monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
    path = enginelock.lock_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"pid": 999, "what": "godot_run", "work_item": "", '
        f'"expires_at": {__import__("time").time() + 900}}}',
        encoding="utf-8")
    with pytest.raises(enginelock.EngineBusy) as exc:
        with enginelock.hold(tmp_path, "godot_evidence", wait_s=0.01):
            pass
    assert exc.value.self_collision is False


def test_a_claimed_lock_records_the_owning_work_item(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_WORK_ITEM", "99")
    with enginelock.hold(tmp_path, "godot_run"):
        held = enginelock.holder(tmp_path)
        assert held.get("work_item") == "99"
