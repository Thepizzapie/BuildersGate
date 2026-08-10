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

import pytest
from fastapi.testclient import TestClient

from bgate_core import gitwork, queue, spend
from bgate_ui import api
from bgate_ui import dispatch
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
                           json={"title": "fixed brief", "priority": 3,
                                 "max_cost_usd": 12.5}).json()
        assert got["data"]["title"] == "fixed brief"
        assert got["data"]["priority"] == 3
        assert got["data"]["max_cost_usd"] == 12.5

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


class TestSpendCeiling:
    def test_dispatch_refused_when_the_day_budget_is_spent(self, client, root,
                                                           fake_claude):
        spend.set_budget(root, per_day_usd=2, per_item_usd=5, enforced=1)
        # kind="image", not "agent": the day ceiling measures REAL money, and
        # an agent session on a subscription is not any. See TestBillingSplit.
        spend.record(root, 1.5, kind="image", detail="earlier tonight")
        item = queue.add(root, "art", "expensive")

        got = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert got["ok"] is False
        assert got["code"] == "budget_exceeded"
        assert "daily budget" in got["error"]
        assert queue.get(root, item["id"])["status"] == "queued"  # never spawned

    def test_dispatch_allowed_under_the_ceiling(self, client, root, fake_claude):
        spend.set_budget(root, per_day_usd=100, per_item_usd=5)
        item = queue.add(root, "art", "cheap")
        assert client.post(f"/api/queue/{item['id']}/dispatch").json()["ok"] is True

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
        # The notional price lands on the SUBSCRIPTION side. project_usd is
        # real money and an agent session is not any of it.
        assert spend.totals(root)["subscription"]["usd"] == pytest.approx(1.25)
        assert spend.totals(root)["project_usd"] == pytest.approx(0)

        # Idempotent: reaping twice must not bill twice.
        entry = {"log": str(log), "finalized": True}
        dispatch._finalize(str(root), item["id"], entry)
        assert spend.totals(root)["subscription"]["usd"] == pytest.approx(1.25)

    def test_spend_endpoints(self, client, root):
        spend.record(root, 3.0, kind="image", logical_name="hero")
        got = client.get("/api/spend").json()
        assert got["data"]["project_usd"] == pytest.approx(3.0)
        assert got["data"]["by_kind"]["image"] == pytest.approx(3.0)

        patched = client.patch("/api/spend/budget",
                               json={"per_day_usd": 9.0}).json()
        assert patched["data"]["per_day_usd"] == 9.0
        assert client.patch("/api/spend/budget",
                            json={"max_concurrent": 0}).status_code == 400


class TestBillingSplit:
    """Two bills, and the ledger used to add them together.

    An image generation is invoiced by a vendor. An agent session on a
    subscription reports what it WOULD have cost on the API and is charged to
    nobody. Summing them gave a project total matching no statement, and made
    an evening of uncharged agent work refuse a purchase that costs money.
    """

    def test_agent_spend_is_not_real_money(self, root):
        spend.record(root, 12.0, kind="agent", detail="a long session")
        totals = spend.totals(root)
        assert totals["project_usd"] == pytest.approx(0)
        assert totals["subscription"]["usd"] == pytest.approx(12.0)

    def test_image_spend_is(self, root):
        spend.record(root, 3.0, kind="image", logical_name="hero")
        assert spend.totals(root)["project_usd"] == pytest.approx(3.0)

    def test_agent_spend_cannot_lock_out_a_purchase(self, root):
        """The regression that motivated the split: $400 of subscription agent
        work against a $25 day ceiling refused every image generation after
        it, while doing nothing at all to slow the agents down."""
        spend.set_budget(root, per_day_usd=25, enforced=1)
        spend.record(root, 400.0, kind="agent", detail="a busy night")
        assert spend.check(root, projected_usd=1.0)["allowed"] is True
        # Real money still bites.
        spend.record(root, 24.5, kind="image", logical_name="hero")
        assert spend.check(root, projected_usd=1.0)["allowed"] is False

    def test_tokens_are_recorded_because_dollars_do_not_meter_a_subscription(
            self, root):
        spend.record(root, 0.0, kind="agent", model="claude-opus-5[1m]",
                     tokens={"input": 164, "output": 72_911,
                             "cache_read": 13_793_062, "cache_write": 200_780})
        sub = spend.totals(root)["subscription"]
        assert sub["cache_read_tokens"] == 13_793_062
        assert sub["input_side_tokens"] == 164 + 13_793_062 + 200_780
        # A priced-at-zero run is still a run. Dropping it because usd was 0
        # would hide exactly the plans where tokens are the only signal.
        assert sub["runs"] == 1

    def test_a_run_with_neither_dollars_nor_tokens_is_not_a_row(self, root):
        spend.record(root, 0.0, kind="agent")
        assert spend.totals(root)["subscription"]["runs"] == 0


class TestConcurrencyCap:
    def test_overflow_is_refused(self, client, root, fake_claude):
        spend.set_budget(root, max_concurrent=1)
        first = queue.add(root, "art", "one")
        second = queue.add(root, "gameplay", "two")
        assert client.post(f"/api/queue/{first['id']}/dispatch").json()["ok"] is True

        got = client.post(f"/api/queue/{second['id']}/dispatch").json()
        assert got["ok"] is False
        assert got["code"] == "concurrency_limit"
        assert got["detail"]["max_concurrent"] == 1
        assert queue.get(root, second["id"])["status"] == "queued"


class TestWatchdog:
    def _entry(self, log, **over):
        entry = {"proc": FakeProc(pid=777), "log": str(log),
                 "started_at": time.monotonic(), "run_start_pos": 0,
                 "stdin": FakeStdin(), "stdin_closed": True}
        entry.update(over)
        return entry

    def test_runtime_budget_kills_a_wedged_agent(self, root, monkeypatch):
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
        assert "runtime budget" in row["result"]
        dispatch._live.clear()

    def test_cost_budget_kills_mid_run(self, root, monkeypatch):
        item = queue.add(root, "art", "spendy")
        queue.set_status(root, item["id"], "dispatched")
        log = root / ".bgate" / "agents" / f"item-{item['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"type": "result", "total_cost_usd": 9.5}) + "\n",
                       encoding="utf-8")

        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))
        dispatch._live[item["id"]] = self._entry(log, max_cost_usd=1.0)
        dispatch._watch_completion(str(root), item["id"], poll_s=0.01)

        assert killed == [777]
        assert "$9.50" in queue.get(root, item["id"])["result"]
        dispatch._live.clear()

    def test_healthy_agent_is_left_alone(self, root, monkeypatch):
        item = queue.add(root, "art", "fine")
        queue.set_status(root, item["id"], "dispatched")
        log = root / ".bgate" / "agents" / f"item-{item['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("", encoding="utf-8")

        killed = []
        monkeypatch.setattr(dispatch, "_kill_tree", lambda pid: killed.append(pid))
        dispatch._live[item["id"]] = self._entry(log, max_runtime_s=3600,
                                                 max_cost_usd=100.0)
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
