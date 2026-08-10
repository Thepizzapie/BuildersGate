"""A pending human decision has to be READABLE, not just drawable.

THE GAP THIS CLOSES. Human approval was dashboard-only by design — the agent
that made a candidate must not be the one to clear it — but "you may not decide
it" was implemented as "you may not SEE it", and those are different rules. A
director could not ask what was blocking its own board: asset_status lists
candidates with no approval state, art_tournament_standings reports only decided
matches, and nothing emitted an event.

The cost is not inconvenience. A blocking gate with no signal looks exactly like
an agent quietly working, so work stalls behind it and the heartbeat says
nothing — the same "silence is not success" failure as a dead agent, through a
different door. Measured in the field: five species candidates generated inside
two minutes, an approval card for each, and zero lines on notify.jsonl.

Two surfaces are tested here because they answer different questions and neither
substitutes for the other — the jsonl is what a shell can tail with no database
and no MCP, the tool is what an agent mid-run can call.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import artifacts, gates, queue as _queue


def _notify_lines(root):
    path = root / ".bgate" / "notify.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _register(root, name="species_red_fox", item_id=None):
    image = root / f"{name}.png"
    image.write_bytes(b"plate-bytes")
    return artifacts.register(root, name, image, producer="image_generate",
                              work_item_id=item_id)


class TestTheHeartbeatCarriesCandidates:
    def test_a_candidate_appends_a_line(self, root):
        before = len(_notify_lines(root))
        _register(root)
        after = _notify_lines(root)
        assert len(after) == before + 1
        assert after[-1]["kind"] == "artifact.candidate"

    def test_the_line_names_what_is_waiting(self, root):
        item = _queue.add(root, "art", title="fox plate", brief="a fox")
        art = _register(root, item_id=item["id"])
        line = _notify_lines(root)[-1]
        assert line["artifact_id"] == art["id"]
        assert line["item_id"] == item["id"]
        assert "species_red_fox" in line["title"]

    def test_the_decision_closing_is_news_too(self, root):
        """Without this a consumer's pending list only grows, and it keeps
        telling the human they owe a decision they already made."""
        art = _register(root)
        artifacts.review(root, art["id"], "approved", actor="Sam")
        line = _notify_lines(root)[-1]
        assert line["kind"] == "artifact.reviewed"
        assert line["status"] in ("approved", "integrated")

    def test_work_item_lines_are_unchanged_for_old_consumers(self, root):
        """Purely additive. A tail that reads {ts,item_id,status,seat,title}
        must see exactly what it saw before — the whole point of putting both
        classes on ONE stream is that nobody has to be told to re-plumb."""
        item = _queue.add(root, "tech", title="scatter", brief="scatter")
        _queue.set_status(root, item["id"], "dispatched")
        line = [ln for ln in _notify_lines(root)
                if ln.get("item_id") == item["id"]][-1]
        for field in ("ts", "item_id", "status", "seat", "title"):
            assert field in line
        assert line["status"] == "dispatched"

    def test_a_broken_stream_never_costs_the_registration(self, root, monkeypatch):
        """The control. Losing a ping must not lose the artifact it was about."""
        monkeypatch.setattr(artifacts, "_notify_line",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError):
            artifacts._notify_line(root, {})       # the fake really does raise
        monkeypatch.setattr(artifacts, "_announce_candidate",
                            lambda *a, **k: None)
        assert _register(root)["id"]


class TestPendingDecisions:
    """The tool that did not exist."""

    @pytest.fixture()
    def call(self, root, monkeypatch):
        import asyncio

        from bgate_mcp import server

        monkeypatch.setenv("BGATE_ROOT", str(root))

        def _go(**kw):
            out = asyncio.run(server.mcp.call_tool("pending_decisions", kw))
            content = out[0] if isinstance(out, tuple) else out
            block = content[0]
            return json.loads(block.text) if hasattr(block, "text") else block
        return _go

    def test_an_idle_project_says_so(self, call):
        got = call()
        assert got["total"] == 0
        assert got["note"] == "nothing is waiting on a human"

    def test_a_candidate_is_listed_with_what_it_hangs_off(self, root, call):
        item = _queue.add(root, "art", title="fox plate", brief="a fox")
        art = _register(root, item_id=item["id"])
        got = call()
        assert [c for c in got["candidates"]
                if c["artifact_id"] == art["id"]
                and c["work_item_id"] == item["id"]]

    def test_a_machine_verdict_is_distinguished_from_a_raw_candidate(self, root,
                                                                     call):
        """A candidate a QA agent already passed is a different ask: the human is
        confirming a check, not performing the first one."""
        art = _register(root)
        artifacts.qa_verdict(root, art["id"], passed=True, note="full body, clean",
                             actor="agent:item-3")
        row = [c for c in call()["candidates"] if c["artifact_id"] == art["id"]][0]
        assert row["machine_verdict"] == "pass"
        assert "full body" in row["machine_note"]

    def test_a_parked_item_is_reported_as_a_stopped_chain(self, root, call):
        gates.set_mode(root, gates.BUILDERS)
        item = _queue.add(root, "tech", title="scatter pass", brief="scatter")
        _queue.complete(root, item["id"], result="scattered 850")
        got = call()
        assert [p for p in got["blocked_chains"] if p["item_id"] == item["id"]]

    def test_it_reports_the_gate_mode(self, root, call):
        gates.set_mode(root, gates.BUILDERS)
        assert call()["gate"]["mode"] == "builders"

    def test_a_decision_pending_under_gate_none_is_called_out(self, root, call):
        """Under 'none' the board is not supposed to be stopping for anyone, so
        a non-empty list is a defect to report rather than a queue to click
        through. The tool says which one the agent is looking at."""
        art = _register(root)
        artifacts.qa_verdict(root, art["id"], passed=True, actor="agent:item-1")
        gates.set_mode(root, gates.NONE)
        got = call()
        assert got["candidates"]                       # left over from before
        assert "approval gate is 'none'" in got["note"]

    def test_an_answered_candidate_drops_off(self, root, call):
        art = _register(root)
        assert call()["total"] == 1
        artifacts.review(root, art["id"], "approved", actor="Sam")
        assert call()["total"] == 0
