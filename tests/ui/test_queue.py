"""Work queue + dispatch endpoints + the in-app play route."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db
from bgate_core.board import queue
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


class TestQueueCore:
    def test_lifecycle(self, root):
        item = queue.add(root, "gameplay", "fix the jump", brief="too floaty now")
        assert item["status"] == "queued"
        assert queue.next_for(root, "gameplay")["id"] == item["id"]

        done = queue.set_status(root, item["id"], "done", result="fixed")
        assert done["status"] == "done"
        assert queue.next_for(root, "gameplay") is None

    def test_priority_orders_next(self, root):
        queue.add(root, "art", "low", priority=0)
        high = queue.add(root, "art", "high", priority=5)
        assert queue.next_for(root, "art")["id"] == high["id"]

    def test_unknown_seat_and_empty_title(self, root):
        with pytest.raises(ValueError, match="seat"):
            queue.add(root, "wizard", "x")
        with pytest.raises(ValueError, match="title"):
            queue.add(root, "art", "   ")

    def test_promoted_playtest_items_flow_in_once(self, root):
        with db.tx(root) as conn:
            conn.execute("INSERT INTO playtest_session (id, name, slug, status) "
                         "VALUES (1, 'R', 'r', 'ready')")
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (1, 5.0, 'fix', 'jump is floaty', 'gameplay', 'promoted')")

        first = queue.sync_promoted(root)
        assert first["created"] == 1
        assert "playtest item" in first["items"][0]["brief"]
        assert queue.sync_promoted(root)["created"] == 0  # once, not every poll

    def test_orbit_import_fails_soft(self, root):
        got = queue.import_orbit(root, api_url="http://127.0.0.1:1")  # nothing there
        assert got["created"] == 0
        assert "unreachable" in got["error"]


class TestQueueApi:
    def test_add_and_list(self, client):
        client.post("/api/queue", json={"seat": "tech", "title": "export web build"})
        got = client.get("/api/queue").json()
        assert got["items"][0]["title"] == "export web build"

    def test_dispatch_missing_claude_is_honest(self, client, root, monkeypatch):
        from bgate_ui.agents import dispatch
        monkeypatch.setattr(dispatch, "find_claude", lambda: None)
        item = queue.add(root, "art", "paint")
        got = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert got["ok"] is False
        assert "claude" in got["error"].lower()

    def test_dispatch_spawns_with_seat_env_and_marks(self, client, root, monkeypatch):
        # The dispatch contract is streamed: the CLI starts in stream-json
        # input mode and the task arrives as the FIRST user message on stdin
        # (not as a -p argument) so steer() can inject more turns later.
        import io

        from bgate_ui.agents import dispatch

        captured = {}

        class FakeStdin(io.BytesIO):
            def close(self):  # dispatch closes stdin at EOF; keep it readable
                pass

        class FakeProc:
            pid = 4242
            def __init__(self):
                self.stdin = FakeStdin()
            def poll(self):
                return None
            def kill(self):
                pass
        def fake_popen(args, **kw):
            # dispatch() is not the only thing reaching for Popen. The liveness
            # sweep asks the OS what a pid actually is, and with no psutil (it
            # is a declared dependency of nothing, so CI has none while a dev
            # box usually does) that falls back to `tasklist` via
            # subprocess.run — which IS subprocess.Popen. Patching the module
            # attribute catches both, and last-write-wins meant the assertions
            # below ran against the tasklist argv on precisely the machines
            # without psutil. Capture the spawn under test, by name.
            if not args or args[0] != "claude":
                return FakeProc()
            captured["args"] = args
            captured["env"] = kw["env"]
            captured["cwd"] = kw["cwd"]
            captured["proc"] = FakeProc()
            return captured["proc"]

        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        monkeypatch.setattr(dispatch.subprocess, "Popen", fake_popen)
        dispatch._live.clear()

        item = queue.add(root, "art", "paint the thing")
        got = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert got["ok"] is True
        assert captured["env"]["BGATE_SEAT"] == "art"
        assert captured["cwd"] == str(root)
        assert "--input-format" in captured["args"]  # stdin is the task channel
        first_msg = json.loads(captured["proc"].stdin.getvalue().decode("utf-8"))
        assert first_msg["type"] == "user"
        prompt = first_msg["message"]["content"][0]["text"]
        assert "queue_complete" in prompt and "progress/item-" in prompt
        assert queue.get(root, item["id"])["status"] == "dispatched"
        dispatch._live.clear()

    def test_double_dispatch_refused(self, client, root, monkeypatch):
        import io

        from bgate_ui.agents import dispatch

        class FakeProc:
            pid = 1
            def __init__(self):
                self.stdin = io.BytesIO()
            def poll(self):
                return None
            def kill(self):
                pass
        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **k: FakeProc())
        dispatch._live.clear()

        item = queue.add(root, "qa", "verify")
        assert client.post(f"/api/queue/{item['id']}/dispatch").json()["ok"] is True
        second = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert second["ok"] is False
        dispatch._live.clear()


class TestPlaytestFromApp:
    def test_preflight_endpoint_reports_shape(self, client):
        got = client.get("/api/playtest/preflight").json()
        assert "ready" in got and "checks" in got

    def test_stop_without_recording_is_honest(self, client):
        got = client.post("/api/playtest/stop").json()
        assert got["ok"] is False
        assert "recording" in got["error"]

    def test_stop_processes_and_queues_director_triage(self, client, root, monkeypatch):
        """The routing the app exists for: session -> transcript -> a DIRECTOR
        triage item in the queue, carrying the session id."""
        from bgate_core.qa import playtest as pt
        from bgate_ui import app as ui_app

        with db.tx(root) as conn:
            conn.execute("INSERT INTO playtest_session (id, name, slug, status) "
                         "VALUES (7, 'app session', 'app-session', 'recording')")

        monkeypatch.setattr(pt, "stop", lambda r, sid, **kw: {
            "session_id": sid, "transcript": {"ok": True, "items": 5}})

        got = client.post("/api/playtest/stop").json()
        assert got["ok"] is True and got["session_id"] == 7

        import time
        for _ in range(50):
            if ui_app._pt_processing.get(7) == "ready":
                break
            time.sleep(0.05)
        assert ui_app._pt_processing[7] == "ready"

        items = queue.list_items(root, status="queued", seat="director")
        assert len(items) == 1
        assert items[0]["source"] == "playtest-triage"
        assert items[0]["source_ref"] == "7"
        assert "playtest_brief" in items[0]["brief"]
        assert "session_id=7" in items[0]["brief"]

    def test_failed_transcription_does_not_queue_triage(
            self, client, root, monkeypatch):
        from bgate_core.qa import playtest as pt
        from bgate_ui import app as ui_app

        with db.tx(root) as conn:
            conn.execute("INSERT INTO playtest_session (id, name, slug, status) "
                         "VALUES (8, 'failed session', 'failed-session', 'recording')")
        monkeypatch.setattr(pt, "stop", lambda r, sid, **kw: {
            "session_id": sid,
            "transcript": {"ok": False, "error": "whisper failed"}})

        assert client.post("/api/playtest/stop").json()["ok"] is True
        import time
        for _ in range(50):
            if str(ui_app._pt_processing.get(8, "")).startswith("failed"):
                break
            time.sleep(0.05)
        assert queue.list_items(root, seat="director") == []

    def test_status_endpoint(self, client):
        got = client.get("/api/playtest/status").json()
        assert "recording" in got and "processing" in got

    def test_web_telemetry_and_review_endpoints(self, client, root):
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_session "
                "(id, name, slug, status, started_epoch) "
                "VALUES (9, 'web', 'web', 'recording', 1000)")
            conn.execute(
                "INSERT INTO playtest_item "
                "(id, session_id, t, kind, text, seat) "
                "VALUES (90, 9, 2.0, 'fix', 'jump is floaty', 'gameplay')")

        event = client.post("/api/playtest/9/events", json={
            "ts": 1002.0, "kind": "jump", "data": {"air_time": 0.9}})
        assert event.status_code == 200
        review = client.get("/api/playtest/9").json()
        assert review["counts"]["events"] == 1
        assert review["items"][0]["events"][0]["data"]["air_time"] == 0.9

        promoted = client.post(
            "/api/playtest/items/90/promote",
            json={"seat": "tech", "kind": "fix"}).json()
        assert promoted["status"] == "promoted"
        assert promoted["seat"] == "tech"
        # Promotion marks the moment noteworthy; it must NOT auto-create a work
        # item (that produced blob/fragment tasks -- the director authors work
        # from the full transcript by meaning).
        assert queue.list_items(root) == []


class TestPlayRoute:
    def test_coi_headers_on_every_response(self, client):
        got = client.get("/api/queue")
        assert got.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert got.headers["Cross-Origin-Embedder-Policy"] == "require-corp"

    def test_serves_build_and_guards_escape(self, client, root):
        web = root / "export" / "web"
        web.mkdir(parents=True)
        (web / "index.html").write_text("<html>game</html>", encoding="utf-8")

        assert client.get("/play/").status_code == 200
        assert "game" in client.get("/play/").text
        assert client.get("/play/../../.env").status_code in (403, 404)

    def test_no_build_is_a_clear_404(self, client):
        got = client.get("/play/")
        assert got.status_code == 404
        assert "export" in got.json()["error"]["message"]


class TestStaleBuildGuard:
    """The stale-build bug that wasted a morning: the app served an 11-hour-old
    export while the source had every requested change. status() must catch it."""

    def test_missing_build_reads_stale(self, client, root):
        got = client.get("/api/play/status").json()
        assert got["stale"] is True

    def test_source_newer_than_build_reads_stale(self, client, root):
        import time
        from bgate_core.store import scaffold
        scaffold.new_project(root / "game", "T", kind="2d")
        web = root / "export" / "web"
        web.mkdir(parents=True)
        (web / "index.pck").write_bytes(b"old")
        time.sleep(0.02)
        # touch a script AFTER the build
        (root / "game" / "scripts" / "player.gd").write_text("# changed\n",
                                                             encoding="utf-8")
        got = client.get("/api/play/status").json()
        assert got["built"] is True
        assert got["stale"] is True

    def test_build_newer_than_source_reads_fresh(self, client, root):
        import time
        from bgate_core.store import scaffold
        scaffold.new_project(root / "game", "T", kind="2d")
        time.sleep(0.02)
        web = root / "export" / "web"
        web.mkdir(parents=True)
        (web / "index.pck").write_bytes(b"fresh")  # written after the source
        got = client.get("/api/play/status").json()
        assert got["stale"] is False


class TestReservation:
    """The atomic queued->dispatched transition — two dispatchers, one winner.

    dispatch._spawn and a worker's queue_claim_next live in different
    processes; both used to read 'queued' and proceed, which is one item with
    two agents. The UPDATE's WHERE clause is the guarantee under test.
    """

    def test_reserve_wins_exactly_once(self, root):
        item = queue.add(root, "tech", "wire the door")
        assert queue.reserve(root, item["id"]) is True
        assert queue.reserve(root, item["id"]) is False
        assert queue.get(root, item["id"])["status"] == "dispatched"

    def test_release_undoes_reserve_without_touching_the_result(self, root):
        item = queue.add(root, "tech", "wire the door")
        queue.set_status(root, item["id"], "failed", result="round one notes")
        queue.reopen(root, item["id"], "fix the hinge")
        assert queue.reserve(root, item["id"]) is True
        assert queue.release(root, item["id"]) is True
        after = queue.get(root, item["id"])
        assert after["status"] == "queued"
        # The reopened note survives — release touches status alone. set_status
        # would have blanked it, which is why release exists.
        assert "reopened" in (after["result"] or "")

    def test_release_of_a_non_dispatched_item_is_a_no_op(self, root):
        item = queue.add(root, "tech", "wire the door")
        assert queue.release(root, item["id"]) is False
        assert queue.get(root, item["id"])["status"] == "queued"


class TestClaimNext:
    """The worker pickup loop: claim atomically, honour every hold."""

    def test_claims_the_highest_priority_ready_item_and_stamps_the_actor(self, root):
        queue.add(root, "art", "low", priority=0)
        high = queue.add(root, "art", "high", priority=5)
        got = queue.claim_next(root, "art", actor="agent:item-99")
        assert got["id"] == high["id"]
        assert got["status"] == "dispatched"
        assert got["actor"] == "agent:item-99"

    def test_never_claims_across_seats(self, root):
        queue.add(root, "tech", "not yours")
        assert queue.claim_next(root, "art", actor="agent:item-1") is None

    def test_honours_dependencies(self, root):
        first = queue.add(root, "art", "paint the anchor")
        queue.add(root, "art", "derive the poses", depends_on=first["id"])
        # Claim the ready one, then nothing: the dependent is not ready and the
        # first is now dispatched.
        assert queue.claim_next(root, "art", actor="agent:item-1")["id"] == first["id"]
        assert queue.claim_next(root, "art", actor="agent:item-1") is None
        queue.set_status(root, first["id"], "done", result="anchor landed")
        follow = queue.claim_next(root, "art", actor="agent:item-1")
        assert follow is not None and follow["title"] == "derive the poses"

    def test_holds_what_autodeploy_holds(self, root):
        queue.add(root, "director", "two agents disagreed",
                  source="qa-gate-escalation")
        queue.add(root, "director", "a chat turn", source="chat")
        placeholder = queue.add(root, "director", "coming", brief="x")
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET brief = '(preparing #7)' "
                         "WHERE id = ?", (placeholder["id"],))
        assert queue.claim_next(root, "director", actor="agent:item-1") is None

    def test_requires_an_execution_identity(self, root):
        queue.add(root, "art", "x")
        with pytest.raises(ValueError, match="identity"):
            queue.claim_next(root, "art", actor="  ")

    def test_claimed_open_sees_only_this_actors_claims(self, root):
        mine = queue.add(root, "art", "mine")
        queue.add(root, "art", "ordinary")
        queue.claim_next(root, "art", actor="agent:item-7")  # takes 'mine'
        held = queue.claimed_open(root, "agent:item-7")
        assert [h["id"] for h in held] == [mine["id"]]
        assert queue.claimed_open(root, "agent:item-8") == []


class TestEmitTerminal:
    """The watchdog's kill path finally reaches the bus."""

    def test_a_failed_item_emits_item_failed(self, root):
        from bgate_core.store import events
        item = queue.add(root, "tech", "doomed")
        queue.set_status(root, item["id"], "failed", result="killed: ceiling")
        before = events.head(root)
        queue.emit_terminal(root, item["id"])
        got = [e for e in events.since(root, before)["events"]
               if e["kind"] == "item.failed"]
        assert len(got) == 1
        assert got[0]["payload"]["item"] == item["id"]

    def test_a_non_terminal_status_emits_nothing(self, root):
        from bgate_core.store import events
        item = queue.add(root, "tech", "still going")
        before = events.head(root)
        queue.emit_terminal(root, item["id"])          # status is 'queued'
        assert events.since(root, before)["events"] == []

    def test_a_deleted_item_is_silently_skipped(self, root):
        queue.emit_terminal(root, 424242)              # must not raise


# The MCP-surface behaviour of queue_reopen and queue_claim_next (round
# counting, worker-only claiming) is tested in test_mcp_server.py, through the
# registered handlers the way a client hits them.


class TestAutoCommitBreaksTheDeadlock:
    """Nothing ever committed, and a dirty tree refuses every dispatch — so the
    first agent to write a file blocked the whole board forever."""

    def _repo(self, root):
        import subprocess
        for args in (["init"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True)
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "seed"], cwd=root,
                       capture_output=True)

    def test_commit_paths_takes_only_what_it_is_given(self, root):
        from bgate_core.board import gitwork
        self._repo(root)
        (root / "agent.txt").write_text("agent wrote this\n", encoding="utf-8")
        (root / "human.txt").write_text("human wrote this\n", encoding="utf-8")

        got = gitwork.commit_paths(root, ["agent.txt"], "bgate: item #1")
        assert got["ok"] is True and got["committed"] == ["agent.txt"]
        # The human's file is untouched — and still dirties the tree, which is
        # the original rule working rather than being bypassed.
        after = gitwork.dirty(root)
        assert after["dirty"] is True
        assert after["paths"] == ["human.txt"]

    def test_a_clean_tree_after_the_agents_own_files_are_committed(self, root):
        from bgate_core.board import gitwork
        self._repo(root)
        (root / "a.gd").write_text("1\n", encoding="utf-8")
        (root / "b.gd").write_text("2\n", encoding="utf-8")
        gitwork.commit_paths(root, ["a.gd", "b.gd"], "bgate: item #2")
        assert gitwork.dirty(root)["dirty"] is False   # the next dispatch runs

    def test_nothing_to_commit_is_reported_not_raised(self, root):
        from bgate_core.board import gitwork
        self._repo(root)
        got = gitwork.commit_paths(root, [], "bgate: item #3")
        assert got["ok"] is False and "nothing" in got["reason"]
