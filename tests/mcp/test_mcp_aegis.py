"""Containment on the MCP side of the fence - the door the hook cannot watch.

tests/board/test_aegis.py covers the decision function and tests/cli/test_hook.py covers
the PreToolUse gate. This covers the hole both of them left: every tool in
bgate_mcp.server takes a `project_dir`, resolves the project itself and goes
straight to sqlite in the server process. No Write, no Bash, no PreToolUse
event - so an agent dispatched for Ember could file work, lock assets and edit
scenes in Hollow, and the containment story in the README was true only of the
tools that happen to touch files through Claude Code.

The two properties worth pinning here, because both are easy to regress:

  * the SPLIT. A refusal is decided by which tool is calling, and the default
    for an unlisted tool is "writes". A future tool added without a thought
    must arrive contained, not exempt.
  * the ORDER. The check runs before `_keys`, so a refused call never loads the
    other project's .env into this process. A containment gate that hands over
    the API keys first and refuses afterwards has already lost the thing worth
    protecting.

Projects here are the marker file and nothing else where the decision is all
that matters, exactly as in test_aegis - aegis asks the filesystem whether
`.bgate/game.db` exists and asks nothing else.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest

from bgate_cli import hook
from bgate_core.board import aegis
from bgate_mcp import server


def make_project(path: Path) -> Path:
    (path / aegis.PROJECT_DIRNAME).mkdir(parents=True, exist_ok=True)
    (path / aegis.PROJECT_DIRNAME / aegis.PROJECT_DBNAME).write_bytes(b"")
    return path


@pytest.fixture
def ember(tmp_path: Path) -> Path:
    return make_project(tmp_path / "ember")


@pytest.fixture
def hollow(tmp_path: Path) -> Path:
    return make_project(tmp_path / "hollow")


@pytest.fixture
def seated(monkeypatch, ember: Path):
    """A dispatched agent: a seat, and a root pinned to Ember at spawn."""
    monkeypatch.setenv("BGATE_SEAT", "gameplay")
    monkeypatch.setenv("BGATE_ROOT", str(ember))
    monkeypatch.setenv("BGATE_AEGIS", "block")
    return ember


def call(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


@contextlib.contextmanager
def calling(name: str):
    """Stand in for the `_tool` wrapper, which is what binds the calling tool's
    name for the gate. Reset on the way out for the same reason the wrapper
    does it in a finally."""
    token = server._CALL_TOOL.set(name)
    try:
        yield
    finally:
        server._CALL_TOOL.reset(token)


def refused(payload) -> bool:
    """The one predicate. Tools that catch their own exceptions turn the raise
    into `_fail`'s "ContainmentRefused: ..." string and tools that do not get
    the wrapper's structured payload, so the marker is checked in both shapes."""
    if not isinstance(payload, dict):
        return False
    return (payload.get("refused") == "containment"
            or "ContainmentRefused" in str(payload.get("error", "")))


def log_lines(root: Path) -> list[dict]:
    path = root / ".bgate" / "hook.log"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class TestLadder:
    """One dial for both enforcers. The hook and the server disagreeing about
    what BGATE_AEGIS=block means is not a policy, it is a coin flip."""

    def test_default_is_block(self, monkeypatch):
        # warn -> block on 2026-08-19: the project boundary IS what a seat
        # enforces now that lanes are advisory.
        monkeypatch.delenv("BGATE_AEGIS", raising=False)
        assert aegis.mode() == "block" == aegis.DEFAULT_MODE

    @pytest.mark.parametrize("mode", aegis.MODES)
    def test_hook_and_server_read_the_same_ladder(self, monkeypatch, mode):
        monkeypatch.setenv("BGATE_AEGIS", mode.upper())
        assert hook.aegis_mode() == aegis.mode() == mode

    def test_nonsense_falls_back_rather_than_disabling_the_gate(self, monkeypatch):
        monkeypatch.setenv("BGATE_AEGIS", "blcok")
        assert aegis.mode() == aegis.DEFAULT_MODE


class TestSplit:
    def test_every_read_only_name_is_a_real_tool(self):
        """A typo here silently CONTAINS a read-only tool, which is a refusal
        nobody can explain by reading the set."""
        registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert server._READ_ONLY_TOOLS <= registered

    @pytest.mark.parametrize("name", [
        "queue_add", "asset_lock", "scene_set_property", "bible_add",
        "image_generate", "godot_deliver_asset", "sprite_sheet_check",
        "queue_claim_next", "art_qa_verdict", "consistency_check",
    ])
    def test_writers_are_not_on_the_read_only_set(self, name):
        assert name not in server._READ_ONLY_TOOLS

    def test_an_unknown_tool_is_treated_as_writing(self, seated, hollow):
        """The default direction, asserted directly: a name nobody has
        classified is contained rather than exempt."""
        with calling("tool_from_2027"), pytest.raises(server.ContainmentRefused):
            server._contained(str(hollow))


class TestSeatedAgent:
    def test_write_tool_against_another_project_is_refused(self, seated, hollow):
        out = call(server.queue_add, seat="gameplay", title="drift",
                   brief="file work in somebody else's game",
                   project_dir=str(hollow))
        assert refused(out)

    def test_the_refusal_names_both_projects_and_the_seat(self, seated, hollow):
        out = call(server.queue_add, seat="gameplay", title="drift", brief="x",
                   project_dir=str(hollow))
        message = str(out.get("error", ""))
        assert str(seated) in message and str(hollow) in message
        assert "gameplay" in message

    def test_same_project_is_untouched(self, seated):
        assert server._contained(str(seated)) == str(seated)

    def test_a_subdirectory_of_the_pinned_root_is_untouched(self, seated):
        inside = str(seated / ".bgate" / "work" / "item-7")
        assert server._contained(inside) == inside

    def test_read_only_tool_may_still_look_at_another_project(self, seated,
                                                              hollow):
        """project_status only counts rows. Refusing it costs a human a dead
        agent and protects nothing that a write was not already protecting."""
        with calling("project_status"):
            assert server._contained(str(hollow)) == str(hollow)


class TestWhoIsExempt:
    def test_a_seatless_session_is_not_contained(self, monkeypatch, ember,
                                                 hollow):
        """A human-started director reading one game while planning another is
        ordinary top-level work, not a containment failure."""
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setenv("BGATE_ROOT", str(ember))
        monkeypatch.setenv("BGATE_AEGIS", "block")
        assert server._contained(str(hollow)) == str(hollow)

    def test_a_seated_session_nobody_pinned_is_not_contained(self, monkeypatch,
                                                             hollow):
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.delenv("BGATE_ROOT", raising=False)
        monkeypatch.setenv("BGATE_AEGIS", "block")
        assert server._contained(str(hollow)) == str(hollow)

    def test_off_checks_nothing(self, seated, hollow, monkeypatch):
        monkeypatch.setenv("BGATE_AEGIS", "off")
        with calling("queue_add"):
            assert server._contained(str(hollow)) == str(hollow)


class TestWarnMode:
    def test_warn_allows_the_call_and_records_it(self, monkeypatch, ember,
                                                 hollow):
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_ROOT", str(ember))
        monkeypatch.setenv("BGATE_AEGIS", "warn")
        with calling("queue_add"):
            assert server._contained(str(hollow)) == str(hollow)

        rows = [r for r in log_lines(ember) if r.get("event") == "containment"]
        assert len(rows) == 1
        assert rows[0]["surface"] == "mcp"
        assert rows[0]["verdict"] == aegis.DENY
        assert rows[0]["tool"] == "queue_add"
        assert rows[0]["enforced"] is False
        assert str(hollow) in rows[0]["target"]

    def test_the_log_goes_in_the_pinned_project_not_the_named_one(
            self, monkeypatch, ember, hollow):
        """Writing the audit trail into the other game would BE the crossing
        this line exists to report."""
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_ROOT", str(ember))
        monkeypatch.setenv("BGATE_AEGIS", "warn")
        with calling("queue_add"):
            server._contained(str(hollow))
        assert log_lines(ember) and not log_lines(hollow)

    def test_ordinary_in_project_work_is_not_logged(self, seated):
        """Every call a working agent makes lands here; logging them would bury
        the handful of lines the audit is for."""
        server._contained(str(seated))
        assert not log_lines(seated)


class TestCredentialsAreNotHandedOverFirst:
    def test_a_refused_call_never_loads_the_other_project_env(self, seated,
                                                              hollow):
        """THE ORDERING GUARANTEE. `_keys` loads the named project's .env into
        this process, so a check placed after it would give a seated agent
        another game's API key and then tell it it was not allowed to be
        there."""
        (hollow / ".env").write_text("BGATE_TEST_LEAKED_KEY=sk-not-yours\n",
                                     encoding="utf-8")
        os.environ.pop("BGATE_TEST_LEAKED_KEY", None)
        out = call(server.queue_add, seat="gameplay", title="drift", brief="x",
                   project_dir=str(hollow))
        assert refused(out)
        assert "BGATE_TEST_LEAKED_KEY" not in os.environ


class TestWrapperShape:
    def test_a_refusal_carries_the_same_failure_predicate_as_everything_else(
            self, seated, hollow):
        out = call(server.queue_add, seat="gameplay", title="drift", brief="x",
                   project_dir=str(hollow))
        assert out["ok"] is False and out["error"]

    def test_the_tool_name_does_not_survive_the_call(self, seated, hollow):
        """The contextvar is reset in a finally. A leaked name would judge the
        NEXT call on this thread by the last tool's classification."""
        call(server.queue_add, seat="gameplay", title="drift", brief="x",
             project_dir=str(hollow))
        assert server._CALL_TOOL.get() == ""
