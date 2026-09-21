"""Do not spawn an agent that will be refused at its first write.

THE LEASE IS ENFORCED IN THE RIGHT PLACE AND CHARGED IN THE WORST ONE. The
PreToolUse hook refuses a write to a file another run holds, which is correct
for consistency and useless for cost: by then the agent is spawned, has read
its brief and half the repo, and burns its whole runtime being told no.

MEASURED on one file, scenes/graybox_house.tscn: items #72, #78, #79, #84, #87
and #88 all queued against it. #88 alone spent two full rounds and $3.49
discovering a collision the board already knew about — and its own brief said
"check asset_status before you start", which changed nothing, because an
instruction to an agent cannot un-spawn it.

The controls are the whole test. A check that defers too eagerly starves the
board, and a check that never defers is the bug back again.
"""
from __future__ import annotations

import pytest

from bgate_ui.agents import autodeploy


class _Assets:
    """Stands in for bgate_core.store.assets with a fixed lease table."""

    def __init__(self, leases):
        self._leases = leases

    def list_path_leases(self, root):
        return self._leases


@pytest.fixture
def leases(monkeypatch):
    rows = [
        {"path": "scenes/graybox_house.tscn", "owner": "item-87",
         "seat": "gameplay", "expires_at": "2026-09-02 17:50:39"},
        {"path": "scripts/cat.gd", "owner": "item-68",
         "seat": "gameplay", "expires_at": "2026-09-02 17:50:53"},
    ]
    import bgate_core.store.assets as real
    monkeypatch.setattr(real, "list_path_leases", lambda root: rows)
    return rows


def _item(item_id, brief, title=""):
    return {"id": item_id, "title": title, "brief": brief}


def test_an_item_holding_its_own_lease_is_not_blocked_by_it(leases):
    """The deadlock this must never cause. A reopened item keeps its lease
    across attempts, so blocking it on its own claim would park it forever."""
    assert not autodeploy._leased_path_in_brief(
        ".", _item(68, "edit scripts/cat.gd for the climb"))


def test_an_item_touching_nothing_leased_spawns(leases):
    """The control. Without this the check could return a reason always and
    every other test here would still pass while the board starved."""
    assert not autodeploy._leased_path_in_brief(
        ".", _item(99, "wire assets/audio/cat/purr_loop.wav into the mixer"))


def test_an_unreadable_lease_store_lets_the_board_run(monkeypatch):
    """Fail OPEN, deliberately, and for the same reason the hook does: a
    dispatcher that stops on its own inability to check is worse than one that
    occasionally spawns into a collision — which is the behaviour this
    replaces, not a new risk it introduces."""
    import bgate_core.store.assets as real

    def boom(root):
        raise RuntimeError("db locked")

    monkeypatch.setattr(real, "list_path_leases", boom)
    assert autodeploy._leased_path_in_brief(
        ".", _item(88, "scenes/graybox_house.tscn")) == ""


def test_no_leases_at_all_blocks_nothing(monkeypatch):
    import bgate_core.store.assets as real
    monkeypatch.setattr(real, "list_path_leases", lambda root: [])
    assert autodeploy._leased_path_in_brief(
        ".", _item(88, "scenes/graybox_house.tscn")) == ""


def test_file_leased_is_not_a_floor_code():
    """One item waiting on a file must not stop the whole tick — the other
    seats have work that does not touch it. FLOOR_CODES ends the tick; this
    one only cools the item."""
    assert "file_leased" not in autodeploy.FLOOR_CODES
