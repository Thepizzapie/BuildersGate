"""The process lifecycle, exercised end-to-end against a FAKE claude binary.

dispatch.py spawns a real subprocess, keeps its stdin open as a steering
channel, watches for self-report, closes the pipe, kills the tree if the child
lingers, and sweeps orphans a previous server left behind. That is where the
scariest bugs in this repo have lived (the doom-loop zombie, the stale-run
activity feed, steers marked consumed by a PREVIOUS run's echoes) — and none of
it had a test, because testing it seemed to require the real agent.

It does not. The CLI contract is small: argv flags, cwd, env, a stream-json
user turn on stdin, NDJSON events on stdout, exit on EOF. This module writes a
~40-line python program that honours exactly that contract and points
``dispatch.find_claude`` at it, so dispatch -> steer -> complete -> reap ->
orphan-sweep runs deterministically, offline, in about a second.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bgate_core.board import agentreg, queue
from bgate_core.store import db
from bgate_ui.agents import dispatch


def _open_row(root, pid, item_id, *, started_at=0.0, proc_started=None,
              proc_name="") -> None:
    """A run row left open by a previous server run."""
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO agent_runs (project, item_id, pid, proc_started, "
            "proc_name, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(root), item_id, pid, proc_started, proc_name, started_at))


def _open_pids(root) -> list[int]:
    return [int(r["pid"]) for r in agentreg.open_runs(str(root))]

# --- the fake CLI -----------------------------------------------------------
# Mirrors the real `claude -p --input-format stream-json --output-format
# stream-json --verbose --replay-user-messages` wire shape: one JSON object per
# line, user turns echoed back, and a terminal `result` event on stdin EOF.
FAKE_CLI = r'''
import json, os, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

# Record how we were invoked so the test can assert the contract.
probe = os.environ.get("BGATE_FAKE_PROBE")
if probe:
    with open(probe, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(),
                   "seat": os.environ.get("BGATE_SEAT"),
                   "root": os.environ.get("BGATE_ROOT"),
                   "item": os.environ.get("BGATE_WORK_ITEM"),
                   "lock_owner": os.environ.get("BGATE_LOCK_OWNER"),
                   "image_model": os.environ.get("BGATE_IMAGE_MODEL")}, fh)

emit({"type": "system", "subtype": "init", "model": "fake-cli"})
turns = 0
while True:
    line = sys.stdin.readline()
    if not line:  # EOF: the dashboard closed our stdin -> wrap up like the CLI
        break
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    blocks = ev.get("message", {}).get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    turns += 1
    emit(ev)  # --replay-user-messages
    if turns == 1:
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "taking the item"},
            {"type": "tool_use", "name": "mcp__builders-gate__seat_brief",
             "input": {"role": "gameplay"}}]}})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "seat brief delivered"}]}})
    else:
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "acking steer: " + text[-40:]}]}})
emit({"type": "result", "subtype": "success", "result": "fake run complete",
      "total_cost_usd": 0.0, "num_turns": turns})
'''


@pytest.fixture()
def fake_claude(tmp_path_factory, monkeypatch):
    """A real executable on disk that speaks the CLI's protocol."""
    d = tmp_path_factory.mktemp("fakecli")
    script = d / "fake_claude.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    probe = d / "probe.json"
    if sys.platform == "win32":
        exe = d / "claude.bat"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                       encoding="utf-8")
    else:
        exe = d / "claude"
        exe.write_text(f"#!{sys.executable}\n{FAKE_CLI}", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("BGATE_FAKE_PROBE", str(probe))
    monkeypatch.setattr(dispatch, "find_claude", lambda: str(exe))
    return {"exe": exe, "probe": probe}


@pytest.fixture(autouse=True)
def no_stray_processes():
    """Whatever the test does, nothing survives it — a leaked fake agent would
    hold the tmp dir open on Windows and wedge the next test."""
    yield
    with dispatch._lock:
        entries = list(dispatch._live.items())
        dispatch._live.clear()
    for _item_id, entry in entries:
        proc = entry["proc"]
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
        for key in ("stdin", "handle"):
            try:
                entry[key].close()
            except Exception:
                pass


def _wait(predicate, timeout=20.0, interval=0.1, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {what}")


def _log_text(root, item_id) -> str:
    p = Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


class TestDispatchContract:
    def test_spawn_passes_seat_env_cwd_and_stream_flags(self, root, fake_claude):
        item = queue.add(root, "gameplay", "fix the jump", brief="too floaty")
        res = dispatch.dispatch(root, item["id"])
        assert res["ok"], res
        assert res["pid"]
        assert queue.get(root, item["id"])["status"] == "dispatched"

        probe = _wait(lambda: fake_claude["probe"].is_file() and
                      fake_claude["probe"].read_text(encoding="utf-8") or None,
                      what="fake CLI to record its invocation")
        info = json.loads(probe)
        argv = info["argv"]
        for flag in ("-p", "--input-format", "stream-json", "--output-format",
                     "--verbose", "--replay-user-messages", "--permission-mode"):
            assert flag in argv, argv
        assert Path(info["cwd"]).resolve() == Path(root).resolve()
        assert info["seat"] == "gameplay"
        assert info["item"] == str(item["id"])
        assert info["lock_owner"] == f"item-{item['id']}"
        assert info["image_model"] == "gpt-image-1"

        # The prompt went in as the FIRST streamed user turn, not as argv.
        _wait(lambda: "fix the jump" in _log_text(root, item["id"]),
              what="the prompt to reach the agent")
        assert "fix the jump" not in " ".join(argv)

    def test_second_dispatch_of_a_live_item_is_refused(self, root, fake_claude):
        item = queue.add(root, "art", "paint it")
        assert dispatch.dispatch(root, item["id"])["ok"]
        again = dispatch.dispatch(root, item["id"])
        assert again["ok"] is False
        # It is 'dispatched' now, so the status guard fires first — either
        # refusal is correct, neither may spawn a second process.
        assert "already" in again["error"] or "not queued" in again["error"]

    def test_dispatch_refuses_when_no_cli_is_installed(self, root, monkeypatch):
        """The refusal now NAMES the runner, because there is more than one.

        It also moved: which CLI to look for depends on the seat, so the check
        cannot happen before the item is read. The two things that matter are
        unchanged — no process, and the item is still queued for a later try.
        """
        monkeypatch.setattr(dispatch, "find_claude", lambda: None)
        item = queue.add(root, "art", "no cli here")
        res = dispatch.dispatch(root, item["id"])
        assert res["ok"] is False
        assert res["error"] == "claude CLI not found on PATH"
        assert res["code"] == "runner_unavailable"
        assert res["detail"] == {"runner": "claude"}
        assert queue.get(root, item["id"])["status"] == "queued"


class TestSteer:
    def test_steer_lands_and_is_marked_consumed_by_its_echo(self, root, fake_claude):
        item = queue.add(root, "gameplay", "tune the dash")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent's first turn")

        res = dispatch.steer(root, item["id"], "use 0.2s not 0.4s")
        assert res["ok"] and res["steers"] == 1

        # status() is the dashboard poll; it scans the log for the replay echo.
        row = _wait(lambda: next(
            (r for r in dispatch.status(root)
             if r["item_id"] == item["id"] and r.get("steers_pending") == 0),
            None), what="the steer echo to be observed")
        assert row["steers"] == 1
        assert row["steer_latency_s"] and row["steer_latency_s"][0] >= 0

        feed = dispatch.read_activity(root, item["id"])
        assert feed["running"] is True
        kinds = {s["kind"] for s in feed["steps"]}
        assert {"say", "tool", "steer"} <= kinds, feed["steps"]
        steer_step = next(s for s in feed["steps"] if s["kind"] == "steer")
        assert steer_step["text"] == "use 0.2s not 0.4s"

    def test_steer_without_a_live_agent_is_refused(self, root, fake_claude):
        item = queue.add(root, "art", "nobody home")
        assert dispatch.steer(root, item["id"], "hello")["ok"] is False
        assert dispatch.steer(root, item["id"], "   ")["ok"] is False

    def test_steer_is_refused_once_the_channel_closed(self, root, fake_claude):
        item = queue.add(root, "gameplay", "closing time")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent to start")
        queue.set_status(root, item["id"], "done", result="self-reported")
        _wait(lambda: any(r["item_id"] == item["id"] and
                          dispatch._live.get(item["id"], {}).get("stdin_closed")
                          for r in dispatch.status(root)) or
                      item["id"] not in dispatch._live,
              what="stdin to be closed after self-report")
        res = dispatch.steer(root, item["id"], "too late")
        assert res["ok"] is False


class TestCompletionAndReap:
    def test_self_report_closes_stdin_and_the_process_is_reaped(
            self, root, fake_claude):
        item = queue.add(root, "gameplay", "ship it")
        res = dispatch.dispatch(root, item["id"])
        pid = res["pid"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent to start")
        assert pid in _open_pids(root)

        # The agent self-reports via queue_complete -> status() closes stdin ->
        # the CLI hits EOF and exits -> the next status() reaps it.
        queue.set_status(root, item["id"], "done", result="jump fixed")
        exited = _wait(lambda: next(
            (r for r in dispatch.status(root)
             if r["item_id"] == item["id"] and r["state"] == "exited"), None),
            what="the agent process to exit on EOF")
        assert exited["code"] == 0
        assert item["id"] not in dispatch._live
        # A clean exit must not overwrite the agent's own result.
        assert queue.get(root, item["id"])["result"] == "jump fixed"
        # A reaped run's row is closed, so the next server run has nothing to
        # sweep for this agent - and the row still carries what it banked.
        assert pid not in _open_pids(root)
        run = agentreg.last_run(str(root), item["id"])
        assert run["ended_at"] and run["status"] == "done"
        assert run["result"]["code"] == 0

        final = dispatch.read_activity(root, item["id"])["final"]
        assert final and final["subtype"] == "success"

    def test_stop_terminates_a_live_agent(self, root, fake_claude):
        item = queue.add(root, "art", "kill me")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent to start")
        assert dispatch.stop(item["id"])["ok"] is True
        _wait(lambda: next((r for r in dispatch.status(root)
                            if r["item_id"] == item["id"]
                            and r["state"] == "exited"), None),
              what="the terminated agent to be reaped")
        # It never self-reported, so a nonzero exit marks the item failed.
        assert queue.get(root, item["id"])["status"] in ("failed", "done")
        assert dispatch.stop(item["id"])["ok"] is False

    def test_a_re_dispatch_starts_a_fresh_activity_run(self, root, fake_claude):
        """The log appends across runs; the feed must show only THIS one."""
        item = queue.add(root, "gameplay", "round one")
        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="run 1")
        queue.set_status(root, item["id"], "done", result="r1")
        _wait(lambda: next((r for r in dispatch.status(root)
                            if r["item_id"] == item["id"]
                            and r["state"] == "exited"), None), what="run 1 exit")
        queue.set_status(root, item["id"], "queued")

        assert dispatch.dispatch(root, item["id"])["ok"]
        _wait(lambda: _log_text(root, item["id"]).count("taking the item") == 2,
              what="run 2")
        feed = dispatch.read_activity(root, item["id"])
        # Exactly one run's worth of steps, and no stale final from run 1.
        assert [s["kind"] for s in feed["steps"]].count("say") == 1
        assert feed["final"] is None


class TestOrphanSweep:
    def test_no_ledger_is_a_no_op(self, tmp_path):
        assert dispatch.reap_orphans(str(tmp_path)) == {"killed": [], "cleared": []}

    def test_live_agents_are_never_swept(self, root, fake_claude):
        item = queue.add(root, "gameplay", "still working")
        pid = dispatch.dispatch(root, item["id"])["pid"]
        _wait(lambda: "taking the item" in _log_text(root, item["id"]),
              what="the agent to start")
        swept = dispatch.reap_orphans(root)
        assert pid not in [k["pid"] for k in swept["killed"]]
        assert pid not in swept["cleared"]
        assert dispatch.status(root)[0]["state"] == "running"

    def test_dead_pids_are_cleared_without_killing_anything(self, root):
        """A pid from a previous server run that is gone (or belongs to some
        unrelated process now) must have its row closed, never be killed."""
        _open_row(root, 999999999, 1)
        # A pid that is not a claude process: this very interpreter.
        _open_row(root, os.getpid(), 2)
        swept = dispatch.reap_orphans(root)
        assert swept["killed"] == []
        assert set(swept["cleared"]) == {999999999, os.getpid()}
        assert _open_pids(root) == []
        assert os.getpid()  # still alive, obviously — nothing was killed

    @pytest.mark.skipif(sys.platform != "win32",
                        reason="the sweep identifies processes with tasklist")
    def test_an_orphaned_claude_tree_is_killed(self, root, fake_claude, tmp_path):
        """A survivor of a previous server run — in the ledger, not in _live —
        gets its tree killed. The stand-in is a copy of the fake CLI named
        claude.exe, so the tasklist name check (the pid-reuse guard) sees the
        real thing."""
        shim = tmp_path / "claude.exe"
        # waitfor.exe is a plain, relocatable, long-blocking Windows binary —
        # unlike sys.executable, which is often a Store alias that cannot be
        # copied at all, and unlike py.exe, which breaks when moved.
        src = shutil.which("waitfor")
        if not src:  # pragma: no cover - hostile environment
            pytest.skip("no relocatable binary to stage a claude.exe stand-in")
        try:
            shutil.copy2(src, shim)
        except OSError as exc:  # pragma: no cover - hostile filesystem
            pytest.skip(f"cannot stage a claude.exe stand-in: {exc}")
        proc = subprocess.Popen([str(shim), "BgateOrphanSweepTest", "/t", "120"],
                                creationflags=dispatch._NO_WINDOW)
        try:
            _open_row(root, proc.pid, 7)

            swept = dispatch.reap_orphans(root)
            assert [k["pid"] for k in swept["killed"]] == [proc.pid]
            assert proc.wait(timeout=20) is not None
            assert _open_pids(root) == []
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
