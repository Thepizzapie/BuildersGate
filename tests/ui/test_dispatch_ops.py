"""The four gates a dispatch has to pass, and the trail it has to leave.

Reopen/edit/cancel used to exist only as MCP tools, cost was never persisted,
the kill clock only started after an item was already finished, and nobody
could see what an agent changed. Each class below is one of those.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_core.board import gitwork, queue, runlimits
from bgate_ui import api
from bgate_ui.agents import dispatch
from bgate_ui.app import app

needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="git is not installed")


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    dispatch._live.clear()
    # Present the dashboard token: the guard decides whether it is enforcing at
    # import time, so a mutating test cannot rely on the suite's opt-out fixture.
    yield TestClient(app, headers={"x-bgate-token": api.ensure_token(root)})
    dispatch._live.clear()


class FakeStdin(io.BytesIO):
    def close(self):  # dispatch closes stdin at EOF; keep it readable
        pass


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.stdin = FakeStdin()

    def poll(self):
        return None

    def kill(self):
        pass

    def terminate(self):
        pass


def _fake_spawn(monkeypatch, procs):
    """Fake the claude spawn and NOTHING else.

    dispatch.subprocess is the subprocess module itself, so a blanket Popen
    patch also decapitates the git calls the dispatcher makes on the way — real
    git commands pass straight through.
    """
    real_popen = subprocess.Popen
    monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
    monkeypatch.setattr(dispatch, "_watch_completion", lambda *a, **k: None)

    def fake_popen(args, **kw):
        if args and str(args[0]) == "git":
            return real_popen(args, **kw)
        proc = FakeProc(pid=1000 + len(procs))
        procs.append(proc)
        return proc

    monkeypatch.setattr(dispatch.subprocess, "Popen", fake_popen)


@pytest.fixture()
def fake_claude(monkeypatch):
    """A spawnable 'claude' that never runs anything."""
    procs = []
    _fake_spawn(monkeypatch, procs)
    return procs


def _git_repo(path):
    """A real repo with one commit — the boundary every diff test needs."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    env = {**os.environ, **env}
    for args in (["init", "-q"], ["add", "-A"],
                 ["commit", "-qm", "base", "--no-gpg-sign"]):
        subprocess.run(["git", *args], cwd=str(path), env=env,
                       capture_output=True, check=False)


class TestItemRoutes:
    def test_get_and_missing(self, client, root):
        item = queue.add(root, "art", "paint")
        got = client.get(f"/api/queue/{item['id']}").json()
        assert got["ok"] is True and got["data"]["title"] == "paint"

        missing = client.get("/api/queue/9999")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

    def test_wait_route_still_resolves(self, client):
        """/api/queue/wait must not be swallowed by /api/queue/{item_id}."""
        got = client.get("/api/queue/wait?ids=&timeout_s=5")
        assert got.status_code == 200
        assert "ids" in got.json()["error"]

    def test_patch_edits_and_validates(self, client, root):
        item = queue.add(root, "art", "typo'd breif")
        got = client.patch(f"/api/queue/{item['id']}",
                           json={"title": "fixed brief", "priority": 3}).json()
        assert got["data"]["title"] == "fixed brief"
        assert got["data"]["priority"] == 3

        bad = client.patch(f"/api/queue/{item['id']}", json={"seat": "wizard"})
        assert bad.status_code == 400
        assert "seat" in bad.json()["error"]["message"]

        empty = client.patch(f"/api/queue/{item['id']}", json={})
        assert empty.status_code == 400

    def test_reopen_requires_a_finished_item_and_a_reason(self, client, root):
        item = queue.add(root, "gameplay", "fix the jump")
        still_queued = client.post(f"/api/queue/{item['id']}/reopen",
                                   json={"reason": "again"})
        assert still_queued.status_code == 400

        queue.set_status(root, item["id"], "failed", result="agent died")
        no_reason = client.post(f"/api/queue/{item['id']}/reopen", json={})
        assert no_reason.status_code == 400

        got = client.post(f"/api/queue/{item['id']}/reopen",
                          json={"reason": "the hitbox is still 4px off"}).json()
        assert got["data"]["status"] == "queued"
        assert got["data"]["attempts"] == 1
        assert "hitbox is still 4px off" in got["data"]["brief"]

    def test_cancel_sets_status_and_stops_the_agent(self, client, root,
                                                    fake_claude):
        item = queue.add(root, "qa", "verify")
        assert client.post(f"/api/queue/{item['id']}/dispatch").json()["ok"] is True
        got = client.post(f"/api/queue/{item['id']}/cancel",
                          json={"reason": "not needed"}).json()
        assert got["data"]["status"] == "cancelled"
        assert got["agent_stopped"] is True
        # Cancelled is a real status the DB accepts and the list sinks to the end.
        queue.add(root, "qa", "next thing")
        assert [i["status"] for i in queue.list_items(root)][-1] == "cancelled"

    def test_cancelled_item_can_be_reopened(self, client, root):
        item = queue.add(root, "tech", "export build")
        queue.set_status(root, item["id"], "cancelled")
        got = client.post(f"/api/queue/{item['id']}/reopen",
                          json={"reason": "we do want it"}).json()
        assert got["data"]["status"] == "queued"


class TestRunAccounting:
    """What a run cost lands on the run's own row and is summed nowhere else.

    There is no ledger and no ceiling behind these numbers: this product does
    not meter money, and the only balance that exists is the one on the user's
    own provider account.
    """

    def test_completion_persists_cost_and_turns(self, client, root):
        item = queue.add(root, "art", "paint")
        log_dir = root / ".bgate" / "agents"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"item-{item['id']}.log"
        log.write_text(
            json.dumps({"type": "bgate_run_start", "item_id": item["id"]}) + "\n" +
            json.dumps({"type": "result", "subtype": "success", "result": "done",
                        "total_cost_usd": 1.25, "num_turns": 9}) + "\n",
            encoding="utf-8")

        dispatch._finalize(str(root), item["id"], {"log": str(log)})
        row = queue.get(root, item["id"])
        assert row["total_cost_usd"] == pytest.approx(1.25)
        assert row["num_turns"] == 9

    def test_dispatch_is_not_refused_for_money(self, client, root, fake_claude):
        """The budget gate is gone. A dispatch is refused for a dirty tree, a
        chain that has not landed, or the concurrency cap - never for spend."""
        item = queue.add(root, "art", "expensive")
        assert client.post(f"/api/queue/{item['id']}/dispatch").json()["ok"] is True

    def test_there_is_no_spend_endpoint(self, client):
        """/api/spend and /api/spend/budget served the ledger and the ceilings.
        Both are gone; a 404 is the honest answer, not an empty total."""
        assert client.get("/api/spend").status_code == 404
        assert client.patch("/api/spend/budget",
                            json={"per_day_usd": 9.0}).status_code == 404


class TestConcurrencyCap:
    def test_overflow_is_refused(self, client, root, fake_claude):
        runlimits.set_limits(root, max_concurrent=1)
        first = queue.add(root, "art", "one")
        second = queue.add(root, "gameplay", "two")
        assert client.post(f"/api/queue/{first['id']}/dispatch").json()["ok"] is True

        got = client.post(f"/api/queue/{second['id']}/dispatch").json()
        assert got["ok"] is False
        assert got["code"] == "concurrency_limit"
        assert got["detail"]["max_concurrent"] == 1
        assert queue.get(root, second["id"])["status"] == "queued"


class TestWatchdog:
    @pytest.mark.parametrize("status,should_kill", [
        ("dispatched", False), ("done", True), ("failed", True),
    ])
    def test_codex_prompt_eof_is_not_completion(self, root, monkeypatch,
                                               status, should_kill):
        item = queue.add(root, "art", "Codex EOF regression")
        queue.set_status(root, item["id"], status)
        entry = self._entry("unused", runner="codex", started_at=0,
                            max_runtime_s=1800)
        dispatch._live[item["id"]] = entry
        clock = [0]
        killed = []

        def tick(_):
            clock[0] += 100
            if clock[0] > 300:
                dispatch._live.pop(item["id"], None)

        monkeypatch.setattr(dispatch.time, "sleep", tick)
        monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(dispatch, "_last_output_age_s", lambda *a: 0)
        monkeypatch.setattr(dispatch, "_kill_tree", killed.append)
        monkeypatch.setattr(dispatch._assets, "heartbeat", lambda *a: None)
        dispatch._watch_completion(str(root), item["id"], exit_grace_s=90)
        assert bool(killed) is should_kill
        if not should_kill:
            assert "eof_at" not in entry

    def _entry(self, log, **over):
        entry = {"proc": FakeProc(pid=777), "log": str(log),
                 "started_at": time.monotonic(), "run_start_pos": 0,
                 "stdin": FakeStdin(), "stdin_closed": True}
        entry.update(over)
        return entry

    def test_the_runtime_limit_kills_a_wedged_agent(self, root, monkeypatch):
        item = queue.add(root, "art", "wedged")
        queue.set_status(root, item["id"], "dispatched")
        log = root / ".bgate" / "agents" / f"item-{item['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("", encoding="utf-8")

        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))
        # Already past its ceiling when the watchdog first looks.
        dispatch._live[item["id"]] = self._entry(
            log, started_at=time.monotonic() - 120, max_runtime_s=60)
        dispatch._watch_completion(str(root), item["id"], poll_s=0.01)

        assert killed == [777]
        row = queue.get(root, item["id"])
        assert row["status"] == "failed"
        assert "runtime limit" in row["result"]
        dispatch._live.clear()

    def test_healthy_agent_is_left_alone(self, root, monkeypatch):
        item = queue.add(root, "art", "fine")
        queue.set_status(root, item["id"], "dispatched")
        log = root / ".bgate" / "agents" / f"item-{item['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("", encoding="utf-8")

        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))
        dispatch._live[item["id"]] = self._entry(log, max_runtime_s=3600)
        # The watchdog loops until the entry disappears; let it poll a few times
        # against a healthy agent, then drop the entry to end the thread.
        import threading
        t = threading.Thread(target=dispatch._watch_completion,
                             args=(str(root), item["id"], 0.01), daemon=True)
        t.start()
        time.sleep(0.15)
        dispatch._live.clear()
        t.join(timeout=2)

        assert killed == []
        assert queue.get(root, item["id"])["status"] == "dispatched"


@needs_git
class TestGitSurface:
    def test_initialize_creates_a_standalone_repo_with_a_baseline(self, root):
        (root / "project.godot").write_text("[application]\n", encoding="utf-8")

        got = gitwork.initialize(root)

        assert got["available"] is True
        assert got["created"] is True
        assert Path(got["toplevel"]).resolve() == root.resolve()
        assert gitwork.head(root)

    def test_chaos_worktree_is_committed_then_merged(self, root):
        (root / "game.txt").write_text("base\n", encoding="utf-8")
        _git_repo(root)
        base = gitwork.head(root)
        made = gitwork.make_worktree(root, 77, base=base)
        assert made["available"]
        worktree = Path(made["worktree"])
        (worktree / "game.txt").write_text("chaos\n", encoding="utf-8")

        prepared = gitwork.prepare_worktree(
            root, 77, worktree, base, seat="gameplay")
        assert prepared["pending"] is True
        assert gitwork.integration(root, 77)["commit"] == prepared["commit"]

        merged = gitwork.merge_worktree(root, 77)
        assert merged["integrated"] is True
        assert (root / "game.txt").read_text(encoding="utf-8") == "chaos\n"
        assert gitwork.integration(root, 77)["pending"] is False

    def test_conflicting_branch_is_abandoned_after_the_ceiling(self, root):
        """A branch that cannot merge is offered a bounded number of times,
        then fails — not re-asked every five minutes for the life of the
        server. A dirty working branch does not count against it."""
        (root / "game.txt").write_text("base\n", encoding="utf-8")
        _git_repo(root)
        base = gitwork.head(root)
        made = gitwork.make_worktree(root, 78, base=base)
        worktree = Path(made["worktree"])
        (worktree / "game.txt").write_text("theirs\n", encoding="utf-8")
        assert gitwork.prepare_worktree(root, 78, worktree, base)["pending"]
        (root / "game.txt").write_text("ours\n", encoding="utf-8")

        held = gitwork.merge_worktree(root, 78)
        assert held["pending"] is True and "dirty" in held["reason"]
        assert gitwork.integration(root, 78)["attempts"] == 0

        _git_repo(root)                       # commit "ours" -> real conflict
        for attempt in range(1, gitwork.MAX_INTEGRATION_ATTEMPTS):
            got = gitwork.merge_worktree(root, 78)
            assert got["integrated"] is False and got["pending"] is True
            assert got["attempts"] == attempt
        last = gitwork.merge_worktree(root, 78)
        assert last["pending"] is False and last["failed"] is True
        assert gitwork.integrations(root, pending=True) == []
        assert gitwork.dirty(root)["dirty"] is False   # merge was aborted

    def test_director_prompts_are_counted_and_capped(self, root):
        (root / "game.txt").write_text("base\n", encoding="utf-8")
        _git_repo(root)
        base = gitwork.head(root)
        made = gitwork.make_worktree(root, 79, base=base)
        (Path(made["worktree"]) / "game.txt").write_text("x\n", encoding="utf-8")
        gitwork.prepare_worktree(root, 79, Path(made["worktree"]), base)
        for n in range(1, gitwork.MAX_INTEGRATION_ATTEMPTS + 1):
            assert gitwork.note_integration_prompt(root, 79)["prompts"] == n
        final = gitwork.note_integration_prompt(root, 79)
        assert final["failed"] is True and final["pending"] is False

    def test_no_repo_degrades_instead_of_raising(self, root):
        got = gitwork.probe(root)
        assert got["available"] is False and got["reason"]
        assert gitwork.diff(root, "HEAD")["available"] is False
        assert gitwork.revert(root, "HEAD")["available"] is False
        assert gitwork.dirty(root)["dirty"] is False

    def test_diff_reports_edits_new_files_and_binaries(self, root):
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "player.gd").write_text("var speed = 1\n", encoding="utf-8")
        (root / "game" / "hero.png").write_bytes(b"\x89PNG\x00" + b"a" * 100)
        _git_repo(root)
        base = gitwork.head(root)
        assert base

        (root / "game" / "player.gd").write_text("var speed = 2\n", encoding="utf-8")
        (root / "game" / "hero.png").write_bytes(b"\x89PNG\x00" + b"a" * 300)
        (root / "game" / "enemy.gd").write_text("var hp = 3\n", encoding="utf-8")

        got = gitwork.diff(root, base)
        assert got["available"] is True
        by_path = {f["path"]: f for f in got["files"]}
        assert "+var speed = 2" in by_path["game/player.gd"]["diff"]
        assert by_path["game/hero.png"]["binary"] is True
        assert by_path["game/hero.png"]["size_delta"] == 200
        assert by_path["game/hero.png"]["diff"] == ""      # bytes are not a review
        assert by_path["game/enemy.gd"]["status"] == "added"
        assert "+var hp = 3" in by_path["game/enemy.gd"]["diff"]
        # The dashboard's own state is never presented as the agent's work.
        assert not any(f["path"].startswith(".bgate") for f in got["files"])

    def test_revert_is_scoped_and_refuses_foreign_edits(self, root):
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        (root / "b.txt").write_text("keep\n", encoding="utf-8")
        _git_repo(root)
        base = gitwork.head(root)

        # The "run": edits a.txt, creates c.txt. b.txt is touched by a human.
        (root / "a.txt").write_text("agent\n", encoding="utf-8")
        (root / "c.txt").write_text("new\n", encoding="utf-8")
        expect = gitwork.fingerprint(root, ["a.txt", "c.txt"])
        (root / "b.txt").write_text("human edit\n", encoding="utf-8")

        got = gitwork.revert(root, base, expect=expect)
        assert got["conflicts"] == []
        assert set(got["reverted"]) == {"a.txt", "c.txt"}
        assert (root / "a.txt").read_text(encoding="utf-8") == "one\n"
        assert not (root / "c.txt").exists()
        assert (root / "b.txt").read_text(encoding="utf-8") == "human edit\n"

    def test_revert_refuses_when_the_run_paths_moved_on(self, root):
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        _git_repo(root)
        base = gitwork.head(root)

        (root / "a.txt").write_text("agent\n", encoding="utf-8")
        expect = gitwork.fingerprint(root, ["a.txt"])
        (root / "a.txt").write_text("agent + a human's later fix\n", encoding="utf-8")

        got = gitwork.revert(root, base, expect=expect)
        assert got["conflicts"] == ["a.txt"]
        assert got["reverted"] == []
        assert "someone else" in got["reason"]
        assert (root / "a.txt").read_text(encoding="utf-8").endswith("fix\n")

    def test_dirty_tree_refuses_dispatch_unless_allowed(self, root, monkeypatch):
        # The repo is built BEFORE the spawn is faked: patching Popen also
        # patches the subprocess.run that git calls go through.
        _git_repo(root)
        (root / "wip.txt").write_text("half a thought\n", encoding="utf-8")
        _fake_spawn(monkeypatch, [])
        item = queue.add(root, "art", "paint")

        got = dispatch.dispatch(str(root), item["id"])
        assert got["ok"] is False and got["code"] == "dirty_tree"

        allowed = dispatch.dispatch(str(root), item["id"], allow_dirty=True)
        assert allowed["ok"] is True
        assert allowed["base_commit"] == gitwork.head(root)
        assert queue.get(root, item["id"])["base_commit"] == allowed["base_commit"]
        dispatch._live.clear()

    def test_dispatch_route_carries_allow_dirty(self, client, root, monkeypatch):
        """The refusal told the operator to "dispatch with allow_dirty" and the
        route then dropped the field, so there was no way to do that from the
        browser — env var and a restart, or nothing. static/dirtygate.js turns
        the refusal into a dialog and retries through here."""
        _git_repo(root)
        (root / "wip.txt").write_text("half a thought\n", encoding="utf-8")
        _fake_spawn(monkeypatch, [])
        item = queue.add(root, "art", "paint")
        url = f"/api/queue/{item['id']}/dispatch"

        refused = client.post(url, json={}).json()
        assert refused["ok"] is False and refused["code"] == "dirty_tree"
        # The dialog lists these, so an empty list would make it useless.
        assert refused["detail"]["paths"]

        allowed = client.post(url, json={"allow_dirty": True}).json()
        assert allowed["ok"] is True
        dispatch._live.clear()

    def test_dispatch_route_allow_dirty_false_still_refuses(self, client, root,
                                                            monkeypatch):
        """None and False are different: unspecified defers to
        BGATE_ALLOW_DIRTY, an explicit false does not."""
        _git_repo(root)
        (root / "wip.txt").write_text("half a thought\n", encoding="utf-8")
        _fake_spawn(monkeypatch, [])
        monkeypatch.setenv("BGATE_ALLOW_DIRTY", "1")
        item = queue.add(root, "art", "paint")
        url = f"/api/queue/{item['id']}/dispatch"

        assert client.post(url, json={"allow_dirty": False}).json()["code"] == "dirty_tree"
        assert client.post(url, json={}).json()["ok"] is True   # env decides
        dispatch._live.clear()

    def test_diff_route_reads_the_items_base_commit(self, client, root):
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        _git_repo(root)
        item = queue.add(root, "art", "paint")
        queue.set_run_fields(root, item["id"], base_commit=gitwork.head(root))
        (root / "a.txt").write_text("two\n", encoding="utf-8")

        got = client.get(f"/api/queue/{item['id']}/diff").json()["data"]
        assert got["available"] is True
        assert [f["path"] for f in got["files"]] == ["a.txt"]

    def test_revert_route_conflicts_with_409(self, client, root):
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        _git_repo(root)
        item = queue.add(root, "art", "paint")
        base = gitwork.head(root)
        queue.set_run_fields(root, item["id"], base_commit=base)
        (root / "a.txt").write_text("agent\n", encoding="utf-8")
        record_path = dispatch.run_record_path(str(root), item["id"])
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps({"base_commit": base,
                        "paths": gitwork.fingerprint(root, ["a.txt"])}),
            encoding="utf-8")
        (root / "a.txt").write_text("a human, later\n", encoding="utf-8")

        clash = client.post(f"/api/queue/{item['id']}/revert", json={})
        assert clash.status_code == 409
        assert clash.json()["error"]["detail"]["conflicts"] == ["a.txt"]

        forced = client.post(f"/api/queue/{item['id']}/revert",
                             json={"force": True}).json()
        assert forced["data"]["reverted"] == ["a.txt"]
        assert (root / "a.txt").read_text(encoding="utf-8") == "one\n"


class TestNoBaseCommit:
    def test_diff_without_a_base_is_explained_not_500(self, client, root):
        item = queue.add(root, "art", "paint")
        got = client.get(f"/api/queue/{item['id']}/diff").json()
        assert got["ok"] is True
        assert got["data"]["available"] is False
        assert "base commit" in got["data"]["reason"]

    def test_revert_without_a_base_is_a_400(self, client, root):
        item = queue.add(root, "art", "paint")
        got = client.post(f"/api/queue/{item['id']}/revert", json={})
        assert got.status_code == 400


class TestTheLogCarriesItsOwnClock:
    """A step's time has to be written by whoever received the line.

    The runner's stream-json carries no wall clock, so every reader dated a
    step by when it PARSED the line. That is within milliseconds for the
    dashboard's incremental feed and a fabrication for anything re-reading a
    finished log: agent_activity reported all four steps of a twenty-minute
    run as having happened in the same second, which answers "is it stuck"
    with a confident no, every time.
    """

    def _pumped(self, tmp_path, body: str):
        import subprocess
        import sys

        script = tmp_path / "fake_agent.py"
        script.write_text(body, encoding="utf-8")
        log = tmp_path / "item-1.log"
        handle = open(log, "ab")
        proc = subprocess.Popen([sys.executable, str(script)],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        thread = dispatch._pump_stamped(proc.stdout, handle, 1)
        proc.wait()
        thread.join(10)
        handle.close()
        return log.read_text(encoding="utf-8").splitlines()

    def test_every_json_line_gets_the_writers_clock(self, tmp_path):
        import json
        import time

        before = time.time()
        lines = self._pumped(tmp_path, "import json\n"
                                       "for i in range(3):\n"
                                       "    print(json.dumps({'type': 'assistant', 'n': i}),"
                                       " flush=True)\n")
        after = time.time()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            event = json.loads(line)
            assert before <= event["bgate_ts"] <= after
            # The payload survives the splice byte for byte - these lines are
            # large and are read by things that are not this module.
            assert event["type"] == "assistant" and event["n"] == i

    def test_a_line_that_is_not_json_is_passed_through_untouched(self, tmp_path):
        # A log that drops what it cannot classify is worse than one holding an
        # unstamped line: stderr is where the reason a run died shows up.
        lines = self._pumped(tmp_path,
                             "import sys\n"
                             "print('not json at all', flush=True)\n"
                             "sys.stderr.write('Traceback (most recent call"
                             " last)' + chr(10))\n")
        assert "not json at all" in lines
        assert any("Traceback" in line for line in lines)

    def test_a_stamped_log_gives_agent_activity_real_per_step_times(self, tmp_path):
        import json
        import time

        from bgate_core.board import agentlog

        old = time.time() - 900
        state = {"steps": [], "final": None, "session_id": "", "step_count": 0}
        agentlog.fold_line(state, json.dumps(
            {"bgate_ts": old, "type": "assistant",
             "message": {"content": [{"type": "text", "text": "started"}]}}))
        step = agentlog._stamped(state["steps"][0])
        assert step["ts_exact"] is True
        assert step["age_s"] >= 890, (
            "a fifteen-minute-old step must read as fifteen minutes old, or "
            "the feed cannot answer the only question anyone asks of it")

    def test_an_unstamped_log_gets_no_time_rather_than_a_wrong_one(self, tmp_path):
        import json

        from bgate_core.board import agentlog

        state = {"steps": [], "final": None, "session_id": "", "step_count": 0}
        agentlog.fold_line(state, json.dumps(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "legacy"}]}}))
        step = agentlog._stamped(state["steps"][0])
        assert step["ts_exact"] is False
        assert "at" not in step and "age_s" not in step, (
            "a log written before the stamp existed must not be dated from "
            "the moment it was read")
