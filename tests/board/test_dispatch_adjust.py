"""The ten dispatch findings from the QA audit, each pinned by a test.

The theme running through them: dispatch.py knew things it never wrote down.
A run's outcome lived in an exit code that lies, its existence lived in a dict
that dies with the server, its result lived for exactly one dashboard poll, and
the process it spawned was identified by nothing more than a name prefix.

The process-level cases reuse the fake `claude` from test_dispatch_lifecycle —
a real executable speaking the real stream-json contract. Everything else is
faster and sharper against a hand-built _live entry, because what is under test
is the bookkeeping, not the pipe.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from bgate_core.store import project
from bgate_core.board import agentreg, queue
from bgate_ui.agents import dispatch

# The fake CLI harness. Imported, never rebuilt.
from test_dispatch_lifecycle import _log_text, _wait, fake_claude  # noqa: F401


@pytest.fixture(autouse=True)
def clean_module_tables():
    """Every table in dispatch.py is module-level and keyed by item id or
    project path, so one test's leftovers are the next one's ghosts."""
    def reset():
        entries = list(dispatch._live.values())
        dispatch._live.clear()
        for entry in entries:
            proc = entry.get("proc")
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
            except Exception:
                pass
            for key in ("stdin", "handle"):
                try:
                    entry[key].close()
                except Exception:
                    pass
        dispatch._activity.clear()
        dispatch._reconciled.clear()
        dispatch._starting.clear()

    reset()
    yield
    reset()


class FakeProc:
    """A process whose exit code the test dictates. ``terminate`` is recorded
    but never expected — stop() must kill the TREE."""

    def __init__(self, code=None, pid=54321):
        self.pid = pid
        self.code = code
        self.terminated = False
        self.stdin = _NullStream()

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.code = -9


class _NullStream:
    closed = False

    def close(self):
        self.closed = True

    def write(self, _data):
        return 0

    def flush(self):
        pass


def _log(root, item_id, *events, append=False) -> Path:
    path = Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return path


def _entry(root, item_id, *, code=None, pid=54321, **over) -> dict:
    entry = {"proc": FakeProc(code=code, pid=pid), "root": str(root),
             "log": str(Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"),
             "stdin": _NullStream(), "handle": _NullStream(),
             "started_at": time.monotonic(), "run_start_pos": 0, "steers": []}
    entry.update(over)
    return entry


def _dispatched(root, seat="art", title="work") -> dict:
    item = queue.add(root, seat, title)
    queue.set_status(root, item["id"], "dispatched")
    return item


# --- 1. exit 0 is not a result ---------------------------------------------
class TestExitZeroIsNotDone:
    def test_clean_exit_with_nothing_reported_is_a_failure(self, root):
        item = _dispatched(root)
        _log(root, item["id"], {"type": "bgate_run_start", "item_id": item["id"]})
        dispatch._reap(str(root), item["id"], _entry(root, item["id"], code=0), 0)

        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "without ever reporting a result" in row["result"]

    def test_clean_exit_that_reported_success_is_done(self, root):
        item = _dispatched(root)
        _log(root, item["id"],
             {"type": "bgate_run_start", "item_id": item["id"]},
             {"type": "result", "subtype": "success", "result": "hitbox fixed",
              "total_cost_usd": 0.4, "num_turns": 6})
        dispatch._reap(str(root), item["id"], _entry(root, item["id"], code=0), 0)

        row = queue.get(root, item["id"])
        assert row["status"] == "done"
        assert "hitbox fixed" in row["result"]

    def test_clean_exit_reporting_an_error_is_a_failure(self, root):
        item = _dispatched(root)
        _log(root, item["id"],
             {"type": "result", "subtype": "error_during_execution",
              "result": "the MCP server died"})
        dispatch._reap(str(root), item["id"], _entry(root, item["id"], code=0), 0)

        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "error_during_execution" in row["result"]

    def test_an_agents_own_report_is_never_overwritten(self, root):
        item = queue.add(root, "art", "self reported")
        queue.set_status(root, item["id"], "done", result="I painted it")
        dispatch._reap(str(root), item["id"], _entry(root, item["id"], code=0), 0)
        assert queue.get(root, item["id"])["result"] == "I painted it"


# --- 2. a restart must not strand items -------------------------------------
class TestRestartReconciliation:
    def test_a_stranded_item_is_settled_not_left_dispatched(self, root):
        item = _dispatched(root, title="mid-flight when the server died")
        # _live is empty: this is what a fresh process sees after a restart.
        settled = dispatch.reconcile(str(root))

        assert settled["settled"] == [{"item_id": item["id"], "status": "failed"}]
        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "restart" in row["result"]

    def test_a_run_that_finished_before_the_crash_is_settled_as_done(self, root):
        item = _dispatched(root)
        _log(root, item["id"],
             {"type": "result", "subtype": "success", "result": "landed the fix"})
        dispatch.reconcile(str(root))

        row = queue.get(root, item["id"])
        assert row["status"] == "done"
        assert "landed the fix" in row["result"]

    def test_this_servers_own_live_runs_are_left_alone(self, root):
        item = _dispatched(root)
        dispatch._live[item["id"]] = _entry(root, item["id"])
        dispatch.reconcile(str(root))
        assert queue.get(root, item["id"])["status"] == "dispatched"

    def test_the_startup_sweep_reconciles_and_keeps_its_contract(self, root):
        """app.py calls reap_orphans at startup; that is where a restart is
        noticed. Its return shape is relied on elsewhere and must not grow."""
        item = _dispatched(root)
        got = dispatch.reap_orphans(str(root))
        assert got == {"killed": [], "cleared": []}
        assert queue.get(root, item["id"])["status"] == "failed"

    def test_the_first_sweep_of_a_project_reconciles_once(self, root):
        item = _dispatched(root)
        dispatch.sweep(str(root))
        assert queue.get(root, item["id"])["status"] == "failed"

        # Idempotent: a later sweep must not re-settle a fresh dispatch.
        queue.set_status(root, item["id"], "queued")
        queue.set_status(root, item["id"], "dispatched")
        dispatch.sweep(str(root))
        assert queue.get(root, item["id"])["status"] == "dispatched"

    def test_an_item_stranded_after_the_first_sweep_is_eventually_settled(
            self, root):
        """The once-per-process guard is an age gate now: an item stranded
        AFTER the startup sweep used to stay 'dispatched' until the next
        server restart, parking its whole chain behind it."""
        dispatch.sweep(str(root))                     # the startup pass
        item = _dispatched(root, title="stranded after the first sweep")
        dispatch.sweep(str(root))                     # inside the gate window
        assert queue.get(root, item["id"])["status"] == "dispatched"

        project = dispatch._pkey(str(root))
        dispatch._reconciled[project] -= dispatch.RECONCILE_EVERY_S + 1
        dispatch.sweep(str(root))                     # the gate has aged out
        assert queue.get(root, item["id"])["status"] == "failed"

    def test_reconcile_leaves_a_dispatch_in_flight_alone(self, root):
        """Between queue.reserve() and the Popen an item is 'dispatched' with
        no _live entry — which is what stranded looks like, except this
        process is mid-spawn on it. A re-runnable reconcile must not fail it."""
        item = _dispatched(root)
        dispatch._starting.add(item["id"])
        try:
            got = dispatch.reconcile(str(root))
        finally:
            dispatch._starting.discard(item["id"])
        assert got["settled"] == []
        assert queue.get(root, item["id"])["status"] == "dispatched"


# --- 3. the read handler must not write -------------------------------------
class TestStatusIsAPureRead:
    def test_status_does_not_settle_a_dead_run(self, root):
        item = _dispatched(root)
        entry = _entry(root, item["id"], code=0)
        dispatch._live[item["id"]] = entry

        rows = dispatch.status(str(root))
        assert rows == []                                   # nothing to show yet
        assert queue.get(root, item["id"])["status"] == "dispatched"
        assert item["id"] in dispatch._live                 # untouched
        assert entry["handle"].closed is False

    def test_sweep_is_what_settles_it(self, root):
        item = _dispatched(root)
        dispatch._live[item["id"]] = _entry(root, item["id"], code=0)

        got = dispatch.sweep(str(root))
        assert got["reaped"] == [item["id"]]
        assert queue.get(root, item["id"])["status"] == "failed"
        assert item["id"] not in dispatch._live
        assert [r["state"] for r in dispatch.status(str(root))] == ["exited"]

    def test_the_agents_endpoint_reads_without_writing(self, root, monkeypatch):
        from fastapi.testclient import TestClient
        from bgate_ui.app import app

        monkeypatch.setenv("BGATE_ROOT", str(root))
        item = _dispatched(root)
        dispatch._live[item["id"]] = _entry(root, item["id"], code=0)

        got = TestClient(app).get("/api/agents")
        assert got.status_code == 200
        assert queue.get(root, item["id"])["status"] == "dispatched"


# --- 4. a stop is a stop -----------------------------------------------------
class TestStopKillsTheTree:
    def test_stop_kills_the_whole_tree_and_names_who_did_it(self, root, monkeypatch):
        item = _dispatched(root)
        entry = _entry(root, item["id"], pid=9911)
        dispatch._live[item["id"]] = entry
        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))

        got = dispatch.stop(item["id"], actor="director@desk")
        assert got["ok"] is True and got["actor"] == "director@desk"
        assert killed == [9911]                    # the tree, not just the parent
        assert entry["proc"].terminated is False   # terminate() left MCP children

        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "stopped by director@desk" in row["result"]
        assert "without self-reporting" not in row["result"]

    def test_the_reap_does_not_retell_a_stop_as_a_crash(self, root, monkeypatch):
        item = _dispatched(root)
        entry = _entry(root, item["id"])
        dispatch._live[item["id"]] = entry
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: None)
        dispatch.stop(item["id"], actor="me")

        entry["proc"].code = 1                     # the tree is gone now
        row = dispatch._reap(str(root), item["id"], entry, 1)
        assert "stopped by me" in row["result"]
        assert "stopped by me" in queue.get(root, item["id"])["result"]

    def test_stop_on_a_real_agent_records_the_stop(self, root, fake_claude):
        item = queue.add(root, "art", "kill me")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent to start")

        assert dispatch.stop(item["id"])["ok"] is True
        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "stopped by" in row["result"]


# --- 5. the activity feed reads forward, once -------------------------------
class TestActivityByteCursor:
    def _state(self, root, item_id):
        return dispatch._activity[(dispatch._pkey(root), item_id)]

    def test_a_poll_reads_only_the_new_bytes(self, root):
        item_id = 5
        path = _log(root, item_id,
                    {"type": "bgate_run_start", "item_id": item_id},
                    *[{"type": "assistant", "message": {"content": [
                        {"type": "text", "text": f"step {i} " + "x" * 200}]}}
                      for i in range(50)])
        first = path.stat().st_size

        dispatch.read_activity(str(root), item_id)
        assert self._state(root, item_id)["bytes_read"] == first

        _log(root, item_id, {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "one more"}]}}, append=True)
        grew = path.stat().st_size - first

        dispatch.read_activity(str(root), item_id)
        # The whole 10KB log is NOT re-read to show the newest step.
        assert self._state(root, item_id)["bytes_read"] == first + grew
        assert grew < first

    def test_a_partial_last_line_is_not_lost(self, root):
        item_id = 6
        path = _log(root, item_id, {"type": "bgate_run_start"})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", "message": {"content": [{"type": ')
        assert dispatch.read_activity(str(root), item_id)["steps"] == []
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('"text", "text": "finished writing"}]}}\n')

        steps = dispatch.read_activity(str(root), item_id)["steps"]
        assert [s["text"] for s in steps] == ["finished writing"]

    def test_a_replaced_log_resets_the_cursor(self, root):
        item_id = 7
        _log(root, item_id, *[{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "old run " + "y" * 300}]}} for _ in range(20)])
        dispatch.read_activity(str(root), item_id)

        _log(root, item_id, {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "brand new"}]}})   # shorter file, no append
        feed = dispatch.read_activity(str(root), item_id)
        assert [s["text"] for s in feed["steps"]] == ["brand new"]

    def test_truncation_is_reported_and_pageable(self, root):
        item_id = 8
        _log(root, item_id, *[{"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"step {i}"}]}} for i in range(100)])

        feed = dispatch.read_activity(str(root), item_id, limit=40)
        assert len(feed["steps"]) == 40
        assert feed["step_count"] == 100
        assert feed["truncated"] is True            # no longer a silent cut
        assert feed["steps"][-1]["text"] == "step 99"

        older = dispatch.read_activity(str(root), item_id, limit=40, offset=40)
        assert older["steps"][-1]["text"] == "step 59"
        assert len(dispatch.read_activity(str(root), item_id, limit=0)["steps"]) == 100

    def test_the_ring_is_bounded_and_says_what_it_dropped(self, root, monkeypatch):
        monkeypatch.setattr(dispatch, "MAX_STEPS", 10)
        item_id = 9
        _log(root, item_id, *[{"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"step {i}"}]}} for i in range(30)])

        feed = dispatch.read_activity(str(root), item_id, limit=0)
        assert len(feed["steps"]) == 10
        assert feed["step_count"] == 30 and feed["dropped"] == 20

    def test_a_re_dispatch_marker_clears_the_previous_run(self, root):
        item_id = 10
        _log(root, item_id,
             {"type": "assistant", "message": {"content": [
                 {"type": "text", "text": "run one"}]}},
             {"type": "result", "subtype": "success", "result": "r1"})
        dispatch.read_activity(str(root), item_id)

        _log(root, item_id, {"type": "bgate_run_start", "item_id": item_id},
             {"type": "assistant", "message": {"content": [
                 {"type": "text", "text": "run two"}]}}, append=True)
        feed = dispatch.read_activity(str(root), item_id)
        assert [s["text"] for s in feed["steps"]] == ["run two"]
        assert feed["final"] is None


# --- 6. a finished run stays readable ---------------------------------------
class TestFinishedRunRetention:
    def test_the_result_survives_the_reap(self, root):
        item = _dispatched(root)
        _log(root, item["id"], {"type": "result", "subtype": "success",
                                "result": "the dash is 0.2s now"})
        dispatch._reap(str(root), item["id"], _entry(root, item["id"], code=0), 0)

        rows = dispatch.status(str(root))
        assert len(rows) == 1
        row = rows[0]
        assert row["state"] == "exited" and row["outcome"] == "done"
        assert row["final"]["text"] == "the dash is 0.2s now"
        # Still there on the NEXT poll — the old table dropped it immediately.
        assert dispatch.status(str(root))[0]["item_id"] == item["id"]

    def test_retention_is_bounded(self, root):
        for item_id in range(1, dispatch.RETAIN_RUNS + 4):
            dispatch._reap(str(root), item_id, _entry(root, item_id, code=0), 0)
        assert len(dispatch.status(str(root))) == dispatch.RETAIN_RUNS

    def test_stale_runs_expire(self, root, monkeypatch):
        dispatch._reap(str(root), 1, _entry(root, 1, code=0), 0)
        assert dispatch.status(str(root))
        monkeypatch.setattr(dispatch, "RETAIN_S", -1)
        assert dispatch.status(str(root)) == []

    def test_another_projects_runs_are_not_shown(self, root, tmp_path):
        dispatch._reap(str(root), 1, _entry(root, 1, code=0), 0)
        assert dispatch.status(str(tmp_path / "elsewhere")) == []


# --- 7. house rules belong to the project -----------------------------------
class TestSeatRulesAreProjectScoped:
    def test_no_other_games_assets_are_shipped_in_the_builtins(self):
        blob = " ".join(dispatch.SEAT_RULES.values()).lower()
        for foreign in ("tommy", "scoville", "fight_test"):
            assert foreign not in blob

    def test_the_verification_step_is_derived_from_this_project(self, root):
        item = queue.add(root, "gameplay", "tune the dash")
        prompt = dispatch._prompt_for(str(root), item)
        assert "fight_test.gd" not in prompt

        tests_dir = Path(root) / "game" / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "combat_test.gd").write_text("# test", encoding="utf-8")
        prompt = dispatch._prompt_for(str(root), item)
        assert "game/tests/combat_test.gd" in prompt

    def test_a_project_without_an_engine_is_not_told_to_run_godot(self, root):
        project.init(root, "Test Game", engine="none")
        item = queue.add(root, "narrative", "write a bark")
        prompt = dispatch._prompt_for(str(root), item)
        assert "godot_check_project" not in prompt
        # The eyes rule survives in the engine-less branch: no godot tools,
        # but the agent is still told the render is the check.
        assert "LOOK AT IT" in prompt

    def test_a_seat_rule_can_be_overridden_and_turned_off(self, root):
        (Path(root) / ".bgate").mkdir(parents=True, exist_ok=True)
        (Path(root) / ".bgate" / dispatch.SEAT_RULES_FILENAME).write_text(
            json.dumps({"art": "ART HOUSE RULE — everything ships as .aseprite.",
                        "narrative": ""}), encoding="utf-8")

        # A project override replaces that seat's CRAFT rules. The two
        # board-wide rules (ownership, production routing) are not craft and are
        # not overridable per seat - a project that turns a seat's rules off
        # must not silently turn off the doctrine that stopped every sound
        # effect shipping twice. It states its own in the bible instead.
        assert "aseprite" in dispatch.seat_rules(str(root), "art")
        narrative = dispatch.seat_rules(str(root), "narrative")
        assert "NO FIRST-THOUGHT JOKES" not in narrative     # off, not default
        assert "OWNING ITS WIRE" in narrative
        assert dispatch.seat_rules(str(root), "audio").endswith(
            dispatch.SEAT_RULES["audio"])

        item = queue.add(root, "art", "paint")
        prompt = dispatch._prompt_for(str(root), item)
        assert "aseprite" in prompt
        assert "SINGLE-GEN SHEETS" not in prompt

    def test_a_broken_override_file_falls_back_to_the_builtins(self, root):
        (Path(root) / ".bgate").mkdir(parents=True, exist_ok=True)
        (Path(root) / ".bgate" / dispatch.SEAT_RULES_FILENAME).write_text(
            "{not json", encoding="utf-8")
        assert dispatch.seat_rules(str(root), "art").endswith(
            dispatch.SEAT_RULES["art"])


# --- 8. the feed shows the agent, not the plumbing --------------------------
class TestActivityShowsTheRightThing:
    def test_the_seat_identity_prompt_is_never_a_step(self, root, fake_claude):
        item = queue.add(root, "gameplay", "fix the jump")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent's first turn")

        feed = dispatch.read_activity(root, item["id"], limit=0)
        blob = json.dumps(feed["steps"])
        assert "YOU ARE A SPAWNED SEAT WORKER" not in blob
        assert "Protocol, in order" not in blob
        assert any(s["kind"] == "say" for s in feed["steps"])

    def test_the_result_is_not_cut_to_a_preview(self, root):
        item_id = 11
        long_result = "R" * 1200
        _log(root, item_id,
             {"type": "user", "message": {"content": [
                 {"type": "tool_result", "content": "T" * 500}]}},
             {"type": "result", "subtype": "success", "result": long_result})

        feed = dispatch.read_activity(str(root), item_id, limit=0)
        assert feed["final"]["text"] == long_result
        assert len([s for s in feed["steps"] if s["kind"] == "result"]) == 1
        assert len(feed["steps"][0]["text"]) == 500   # was chopped at 160

    def test_a_string_content_turn_does_not_break_the_feed(self, root):
        """Some CLI builds send message.content as a bare string."""
        item_id = 12
        _log(root, item_id, {"type": "assistant", "message": {"content": "hi"}},
             {"type": "assistant", "message": {"content": [
                 {"type": "text", "text": "still parsing"}]}})
        feed = dispatch.read_activity(str(root), item_id)
        assert [s["text"] for s in feed["steps"]] == ["still parsing"]


# --- 9. a missing item is an answer, not a stack ----------------------------
def test_dispatching_a_missing_item_is_a_clean_refusal(root, monkeypatch):
    monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
    got = dispatch.dispatch(str(root), 9999)
    assert got["ok"] is False
    assert got["code"] == "not_found"
    assert "9999" in got["error"]
    assert got["detail"]["item_id"] == 9999


# --- 9b. every reservation is released or becomes a live process ------------
class TestReservationNeverStrands:
    def test_a_raise_after_the_reservation_releases_the_item(
            self, root, monkeypatch):
        """queue.reserve() happens first; an unanticipated raise anywhere
        after it (here: _git.dirty blowing up) must put the item back to
        'queued' rather than stranding it 'dispatched' with no process."""
        item = queue.add(root, "art", "strand me not")

        def boom(_root):
            raise RuntimeError("git blew up")

        monkeypatch.setattr(dispatch._git, "dirty", boom)
        with pytest.raises(RuntimeError):
            dispatch.dispatch(str(root), item["id"])
        assert queue.get(root, item["id"])["status"] == "queued"
        assert item["id"] not in dispatch._live
        assert item["id"] not in dispatch._starting

    def test_a_prompt_failure_spawns_nothing_and_releases(
            self, root, fake_claude, monkeypatch):
        """The prompt is built BEFORE the Popen: a prompt builder that raises
        used to leave a live claude tree that _live never heard of, while the
        conditional release put the item back on the board under it."""
        item = queue.add(root, "art", "no prompt for you")

        def boom(*a, **kw):
            raise RuntimeError("prompt builder blew up")

        monkeypatch.setattr(dispatch, "_prompt_for", boom)
        with pytest.raises(RuntimeError):
            dispatch.dispatch(str(root), item["id"])
        assert queue.get(root, item["id"])["status"] == "queued"
        assert item["id"] not in dispatch._live
        # No process was created, so no run row was opened for it.
        assert agentreg.open_runs(str(root)) == []


# --- 10. never kill a process we cannot identify ----------------------------
class TestOrphanIdentity:
    def _ledger(self, root, pid, spawned_at=0.0, name="", started=None):
        """An open run row left by a previous server run."""
        from bgate_core.store import db
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO agent_runs (project, item_id, pid, proc_started, "
                "proc_name, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(root), 3, pid, started, name, spawned_at))

    def test_a_claude_that_is_not_ours_is_spared(self, root, monkeypatch):
        """The user's own claude session, sitting on a recycled pid."""
        self._ledger(root, 4242, spawned_at=100.0, name="claude.exe",
                     started=100.0)
        monkeypatch.setattr(dispatch, "_proc_identity",
                            lambda pid: {"name": "claude.exe", "started": 90000.0})
        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))

        swept = dispatch.reap_orphans(str(root))
        assert killed == [] and swept["killed"] == []
        assert swept["cleared"] == [4242]        # dropped from the ledger, alive

    def test_our_own_orphan_is_killed(self, root, monkeypatch):
        self._ledger(root, 4242, spawned_at=100.0, name="claude.exe",
                     started=100.0)
        monkeypatch.setattr(dispatch, "_proc_identity",
                            lambda pid: {"name": "claude.exe", "started": 100.0})
        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))

        assert [k["pid"] for k in dispatch.reap_orphans(str(root))["killed"]] == [4242]
        assert killed == [4242]

    def test_a_different_program_on_that_pid_is_spared(self, root, monkeypatch):
        self._ledger(root, 4242, spawned_at=100.0, started=100.0)
        monkeypatch.setattr(dispatch, "_proc_identity",
                            lambda pid: {"name": "python.exe", "started": 100.0})
        monkeypatch.setattr(dispatch, "_kill_tree",
                            lambda pid: pytest.fail("killed a stranger"))
        assert dispatch.reap_orphans(str(root))["killed"] == []

    def test_an_unknowable_process_is_left_alone(self, root, monkeypatch):
        self._ledger(root, 4242, spawned_at=100.0)
        monkeypatch.setattr(dispatch, "_proc_identity", lambda pid: {})
        monkeypatch.setattr(dispatch, "_kill_tree",
                            lambda pid: pytest.fail("killed an unknown process"))
        assert dispatch.reap_orphans(str(root))["killed"] == []

    def test_the_ledger_records_an_identity_to_check_against(self, root):
        if not dispatch._proc_identity(os.getpid()).get("started"):
            pytest.skip("this host cannot read process start times")
        agentreg.record(os.getpid(), item_id=3, root=str(root),
                        name=dispatch._proc_identity(os.getpid()).get("name", ""))
        meta = agentreg.open_runs(str(root))[0]
        assert meta["proc_started"] and meta["proc_name"]
        assert dispatch._is_recorded_agent(os.getpid(), meta) is False  # not claude
