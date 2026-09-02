"""The from-anywhere activity reader (bgate_core.board.agentlog).

The dashboard's reader lives in bgate_ui and needs the dashboard's process
state; this one is what the MCP server's agent_activity tool serves from any
session. The properties worth pinning: it parses the claude stream-json
shapes into the same step kinds the dashboard shows, a re-dispatch marker
resets the feed, a missing log is an answer rather than an error, and
liveness comes from the on-disk registry rather than a Popen handle.
"""
from __future__ import annotations

import json

from bgate_core.board import agentlog, agentreg


def _write_log(root, item_id: int, events: list[dict]) -> None:
    path = agentlog.log_path(root, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n",
                    encoding="utf-8")


def _say(text):
    return {"type": "assistant", "session_id": "sess-1",
            "message": {"content": [{"type": "text", "text": text}]}}


def _tool(name, **inp):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name,
                                     "input": inp}]}}


def _tool_result(text):
    return {"type": "user",
            "message": {"content": [{"type": "tool_result", "content": text}]}}


class TestTail:
    def test_no_log_is_an_answer_not_an_error(self, root, monkeypatch):
        monkeypatch.setattr(agentreg, "live", list)
        got = agentlog.tail(root, 99)
        assert got["steps"] == [] and got["final"] is None
        assert "never been dispatched" in got["note"]
        assert got["running"] is False

    def test_steps_and_final_parse_into_the_dashboard_shapes(self, root,
                                                             monkeypatch):
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 7, [
            _say("starting on the HUD"),
            _tool("mcp__builders-gate__seat_brief", role="art"),
            _tool("Bash", command="godot --headless"),
            _tool_result("ok, 0 failures"),
            {"type": "result", "subtype": "success",
             "result": "shipped the HUD", "total_cost_usd": 0.42,
             "num_turns": 9},
        ])
        got = agentlog.tail(root, 7)
        kinds = [(s["kind"], s.get("name", "")) for s in got["steps"]]
        assert kinds == [("say", ""), ("tool", "seat_brief"),
                         ("tool", "Bash"), ("result", "")]
        # The mcp prefix is stripped and the hint names the subject.
        assert got["steps"][2]["hint"] == "godot --headless"
        assert got["final"]["subtype"] == "success"
        assert got["final"]["cost"] == 0.42
        assert got["session_id"] == "sess-1"
        assert got["step_count"] == 4 and got["truncated"] is False

    def test_a_redispatch_marker_resets_the_feed(self, root, monkeypatch):
        """The log appends across runs; run 1's steps must not read as run 2's
        current state — same rule as the dashboard's reader."""
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 8, [
            _say("old run"),
            {"type": "result", "subtype": "success", "result": "old result"},
            {"type": "bgate_run_start"},
            _say("new run"),
        ])
        got = agentlog.tail(root, 8)
        assert [s["text"] for s in got["steps"]] == ["new run"]
        assert got["final"] is None

    def test_limit_windows_from_the_newest(self, root, monkeypatch):
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 9, [_say(f"step {n}") for n in range(10)])
        got = agentlog.tail(root, 9, limit=3)
        assert [s["text"] for s in got["steps"]] == \
            ["step 7", "step 8", "step 9"]
        assert got["step_count"] == 10 and got["truncated"] is True

    def test_liveness_reads_the_registry_for_this_item_and_root(
            self, root, monkeypatch):
        _write_log(root, 11, [_say("working")])
        monkeypatch.setattr(agentreg, "live", lambda: [
            {"pid": 4242, "item_id": 11, "root": str(root), "seat": "art",
             "started_at": 1.0},
            {"pid": 4343, "item_id": 11, "root": r"C:\somewhere\else",
             "seat": "art", "started_at": 1.0},
        ])
        got = agentlog.tail(root, 11)
        assert got["running"] is True and got["pid"] == 4242
        # Same item number in a DIFFERENT project is not this item.
        monkeypatch.setattr(agentreg, "live", lambda: [
            {"pid": 4343, "item_id": 11, "root": r"C:\somewhere\else"}])
        assert agentlog.tail(root, 11)["running"] is False

    def test_an_unknown_codex_item_leaves_a_trace(self, root, monkeypatch):
        """A codex capability this version has never seen must look
        unreadable, not idle — same rule as the dashboard feed."""
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 12, [
            {"type": "item.completed",
             "item": {"type": "web_search", "text": "godot tilemap"}},
        ])
        got = agentlog.tail(root, 12)
        assert got["steps"][0]["kind"] == "tool"
        assert got["steps"][0]["name"] == "web_search"

    def test_codex_thread_start_resets_like_a_redispatch(self, root,
                                                         monkeypatch):
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 13, [
            _say("old codex run"),
            {"type": "thread.started"},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "fresh run"}},
        ])
        got = agentlog.tail(root, 13)
        assert [s["text"] for s in got["steps"]] == ["fresh run"]
        # codex has no separate result event; the last say IS the final.
        assert got["final"]["text"] == "fresh run"

    def test_the_marker_and_prefix_agree_with_their_writers(self):
        """The constants exist to be shared; drift here is the bug this
        module was created to end."""
        from bgate_ui.agents import dispatch as _dispatch
        from bgate_ui.agents import runners as _runners
        assert _dispatch.STEER_MARKER is agentlog.STEER_MARKER
        assert agentlog.MCP_TOOL_PREFIX == \
            f"mcp__{_runners.MCP_SERVER_NAME}__"

    def test_a_steer_turn_is_a_steer_step(self, root, monkeypatch):
        monkeypatch.setattr(agentreg, "live", list)
        _write_log(root, 14, [
            {"type": "user", "message": {"content": [
                {"type": "text",
                 "text": agentlog.STEER_MARKER + "use the pinned ref"}]}},
        ])
        got = agentlog.tail(root, 14)
        assert got["steps"] == [got["steps"][0]]
        assert got["steps"][0]["kind"] == "steer"
        assert got["steps"][0]["text"] == "use the pinned ref"
