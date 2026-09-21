"""ITEM 36 — board.focus, the one slice the board works before a second opens.

board_focus_set is a director/human call (same authority as decision_add's
state='settled'); queue_add WARNS, never refuses, when a brief does not name
the slice that is set.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.store import project
# MODULE-LEVEL, like tests/board/test_mcp_server.py — the module/seat/engine
# registration gates run once, at import, off whatever BGATE_SEAT the
# environment happens to carry then. Importing during collection pins that to
# before any test's monkeypatch.setenv runs.
from bgate_mcp import server


class TestProjectFocus:
    def test_unset_focus_reads_empty(self, root):
        assert project.focus_of(root) == ""

    def test_set_and_read(self, root):
        project.set_focus(root, "Canal vertical slice")
        assert project.focus_of(root) == "Canal vertical slice"

    def test_clearing_with_empty_string(self, root):
        project.set_focus(root, "Canal vertical slice")
        project.set_focus(root, "")
        assert project.focus_of(root) == ""


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


async def _call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.mark.anyio
class TestBoardFocusSetTool:
    async def test_the_director_may_set_it(self, wired, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        got = await _call("board_focus_set", focus="Truck Stop")
        assert got.get("ok") and got.get("focus") == "Truck Stop"

    async def test_a_dispatched_seat_is_refused(self, wired, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_WORK_ITEM", "1")
        got = await _call("board_focus_set", focus="Truck Stop")
        assert not got.get("ok")


@pytest.mark.anyio
class TestQueueAddFocusWarning:
    async def test_a_brief_off_the_set_focus_is_warned_not_refused(
            self, wired, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        await _call("board_focus_set", focus="Canal vertical slice")
        got = await _call("queue_add", seat="art", title="DMV interior lighting")
        assert got.get("ok", True) is not False    # never refused
        assert "id" in got
        assert "focus_warning" in got
        assert "Canal vertical slice" in got["focus_warning"]

    async def test_a_brief_naming_the_focus_is_not_warned(
            self, wired, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        await _call("board_focus_set", focus="Canal vertical slice")
        got = await _call("queue_add", seat="art",
                          title="Canal vertical slice signage pass")
        assert "focus_warning" not in got

    async def test_no_focus_set_means_no_warning(self, wired, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        got = await _call("queue_add", seat="art", title="anything at all")
        assert "focus_warning" not in got
