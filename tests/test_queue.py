"""Work queue + dispatch endpoints + the in-app play route."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, queue
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
        from bgate_ui import dispatch
        monkeypatch.setattr(dispatch, "find_claude", lambda: None)
        item = queue.add(root, "art", "paint")
        got = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert got["ok"] is False
        assert "claude" in got["error"].lower()

    def test_dispatch_spawns_with_seat_env_and_marks(self, client, root, monkeypatch):
        from bgate_ui import dispatch

        captured = {}

        class FakeProc:
            pid = 4242
            def poll(self):
                return None
        def fake_popen(args, **kw):
            captured["args"] = args
            captured["env"] = kw["env"]
            captured["cwd"] = kw["cwd"]
            return FakeProc()

        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        monkeypatch.setattr(dispatch.subprocess, "Popen", fake_popen)
        dispatch._live.clear()

        item = queue.add(root, "art", "paint the thing")
        got = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert got["ok"] is True
        assert captured["env"]["BGATE_SEAT"] == "art"
        assert captured["cwd"] == str(root)
        assert "-p" in captured["args"]
        prompt = captured["args"][captured["args"].index("-p") + 1]
        assert "queue_complete" in prompt and "progress/item-" in prompt
        assert queue.get(root, item["id"])["status"] == "dispatched"
        dispatch._live.clear()

    def test_double_dispatch_refused(self, client, root, monkeypatch):
        from bgate_ui import dispatch

        class FakeProc:
            pid = 1
            def poll(self):
                return None
        monkeypatch.setattr(dispatch, "find_claude", lambda: "claude")
        monkeypatch.setattr(dispatch.subprocess, "Popen", lambda *a, **k: FakeProc())
        dispatch._live.clear()

        item = queue.add(root, "qa", "verify")
        assert client.post(f"/api/queue/{item['id']}/dispatch").json()["ok"] is True
        second = client.post(f"/api/queue/{item['id']}/dispatch").json()
        assert second["ok"] is False
        dispatch._live.clear()


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
        assert "export" in got.json()["detail"]
