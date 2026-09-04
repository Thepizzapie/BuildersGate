"""The dashboard backend's second audit pass.

Nine findings, all in bgate_ui/app.py: the failure paths that still hand-rolled
their own shape, a POST that 500'd on a missing field and silently dropped the
provenance the director's badge renders, a poll loop that re-hashed every
tracked binary, a long-poll that pinned a threadpool worker for half an hour, a
recording whose ffmpeg outlived the server, a fan-out that reported success for
work that failed, and a raw log that spliced two agent runs together.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import assets, db
from bgate_core.qa import playtest
from bgate_core.board import queue as _queue
from bgate_ui import app as ui_app
from bgate_ui.agents import dispatch as _dispatch
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    ui_app._verify_cache.clear()
    ui_app._verify_refreshing.clear()
    return TestClient(app)


# ---------------------------------------------------------------------------
# 3. POST /api/queue — validation and provenance
# ---------------------------------------------------------------------------
class TestQueueAdd:
    def test_missing_seat_is_400_not_500(self, client):
        got = client.post("/api/queue", json={"title": "no seat here"})
        assert got.status_code == 400
        body = got.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "bad_request"
        assert "seat" in body["error"]["message"]

    def test_missing_title_is_400(self, client):
        got = client.post("/api/queue", json={"seat": "tech"})
        assert got.status_code == 400
        assert "title" in got.json()["error"]["message"]

    def test_empty_body_names_both_fields(self, client):
        got = client.post("/api/queue", json={})
        assert got.status_code == 400
        assert got.json()["error"]["detail"]["missing"] == ["seat", "title"]

    def test_unknown_seat_is_400_in_the_envelope(self, client):
        got = client.post("/api/queue", json={"seat": "wizard", "title": "x"})
        assert got.status_code == 400
        assert "wizard" in got.json()["error"]["message"]

    def test_unparseable_priority_is_400(self, client):
        got = client.post("/api/queue",
                          json={"seat": "tech", "title": "x", "priority": "soon"})
        assert got.status_code == 400
        assert "priority" in got.json()["error"]["message"]

    def test_source_and_source_ref_round_trip(self, client, root):
        got = client.post("/api/queue", json={
            "seat": "gameplay", "title": "fix the facing bug",
            "brief": "seen at 04:12", "priority": 3,
            "source": "playtest", "source_ref": "42"}).json()
        assert got["source"] == "playtest"
        assert got["source_ref"] == "42"
        assert got["priority"] == 3
        stored = _queue.get(root, got["id"])
        assert (stored["source"], stored["source_ref"]) == ("playtest", "42")

    def test_source_defaults_to_manual(self, client):
        got = client.post("/api/queue",
                          json={"seat": "tech", "title": "hand-filed"}).json()
        assert got["source"] == "manual"
        assert got["source_ref"] == ""


# ---------------------------------------------------------------------------
# 5. queue_wait — bounded, and holding no worker
# ---------------------------------------------------------------------------
class TestQueueWaitBounding:
    def test_the_cap_is_minutes_not_half_an_hour(self):
        assert ui_app._WAIT_MAX_S <= 120

    def test_a_thirty_minute_request_is_clamped_to_the_cap(self, client, root,
                                                           monkeypatch):
        """A caller asking for 30 minutes gets the cap, not 30 minutes."""
        monkeypatch.setattr(ui_app, "_WAIT_MAX_S", 5)  # the same clamp, faster
        item = _queue.add(root, "tech", "still queued")
        started = time.monotonic()
        got = client.get(f"/api/queue/wait?ids={item['id']}&timeout_s=1800").json()
        assert got["waited_budget_s"] == 5
        assert got["timed_out"] is True
        assert time.monotonic() - started < 30

    def test_a_finished_item_returns_immediately(self, client, root):
        item = _queue.add(root, "tech", "done already")
        _queue.set_status(root, item["id"], "done", result="shipped")
        started = time.monotonic()
        got = client.get(f"/api/queue/wait?ids={item['id']}&timeout_s=120").json()
        assert got["finished"] == {str(item["id"]): "done"} or \
               list(got["finished"].values()) == ["done"]
        assert got["timed_out"] is False
        assert time.monotonic() - started < 5

    def test_a_short_timeout_is_honoured_and_says_so(self, client, root):
        item = _queue.add(root, "tech", "never finishes")
        started = time.monotonic()
        got = client.get(f"/api/queue/wait?ids={item['id']}&timeout_s=5").json()
        assert got["timed_out"] is True
        assert got["waited_budget_s"] == 5
        assert time.monotonic() - started < 20

    def test_the_handler_is_async_so_it_holds_no_threadpool_worker(self):
        import inspect
        assert inspect.iscoroutinefunction(ui_app.queue_wait)

    def test_bad_ids_still_answer_in_prose(self, client):
        got = client.get("/api/queue/wait?ids=&timeout_s=5")
        assert got.status_code == 200
        body = got.json()
        assert body["ok"] is False and "ids" in body["error"]


# ---------------------------------------------------------------------------
# 4. /api/state must not hash on the poll thread
# ---------------------------------------------------------------------------
class TestVerifyOffThePollPath:
    def test_first_poll_computes_and_is_truthful(self, client, root):
        blend = root / "b.blend"
        blend.write_bytes(b"v1")
        assets.track(root, blend)
        blend.write_bytes(b"stomped")
        verify = client.get("/api/state").json()["verify"]
        assert verify["ok"] is False
        assert verify["modified"][0]["path"] == "b.blend"
        assert verify["stale"] is False

    def test_subsequent_polls_never_hash(self, client, root, monkeypatch):
        client.get("/api/state")  # warm

        def _explode(*_a, **_kw):
            raise AssertionError("the poll path re-hashed every tracked binary")

        monkeypatch.setattr(assets, "verify", _explode)
        for _ in range(5):
            assert "verify" in client.get("/api/state").json()

    def test_a_stale_snapshot_is_served_and_refreshed_behind_the_poll(
            self, client, root, monkeypatch):
        client.get("/api/state")
        key = str(root.resolve())
        stamp, cached = ui_app._verify_cache[key]
        # Age the cache past the TTL without waiting for it.
        ui_app._verify_cache[key] = (stamp - ui_app._VERIFY_TTL - 1, cached)
        calls: list[float] = []
        real = assets.verify

        def _counted(target):
            calls.append(time.monotonic())
            return real(target)

        monkeypatch.setattr(assets, "verify", _counted)
        got = client.get("/api/state").json()["verify"]
        assert got["stale"] is True          # answered from the old snapshot
        for _ in range(50):                  # ...and refreshed off-thread
            if calls:
                break
            time.sleep(0.05)
        assert calls, "the stale snapshot was never refreshed in the background"

    def test_on_demand_verify_still_hashes_now(self, client, root):
        client.get("/api/state")
        blend = root / "c.blend"
        blend.write_bytes(b"v1")
        assets.track(root, blend)
        blend.write_bytes(b"stomped after the cache was warm")
        got = client.post("/api/assets/verify").json()
        assert got["modified"][0]["path"] == "c.blend"
        assert got["stale"] is False


# ---------------------------------------------------------------------------
# 7. A restart mid-recording
# ---------------------------------------------------------------------------
class TestOrphanedRecording:
    def _recording(self, root, *, with_video: bool = True) -> int:
        video = ""
        if with_video:
            store = root / ".bgate" / "playtests"
            store.mkdir(parents=True, exist_ok=True)
            path = store / "session.mp4"
            path.write_bytes(b"\x00unfinalised mp4 with no moov atom")
            video = str(path)
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_path) "
                "VALUES ('crashed', 'crashed', 'recording', ?)", (video,))
            return int(cur.lastrowid)

    def test_the_session_is_closed_out_not_left_recording(
            self, root, monkeypatch):
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [])
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        sid = self._recording(root)
        assert ui_app._repair_orphan_recordings(root)[0]["session_id"] == sid
        session = playtest.get(root, sid)
        assert session["status"] == "failed"
        assert session["processing_stage"] == "orphaned"
        assert "restarted" in session["processing_error"]

    def test_an_unplayable_file_is_not_offered_as_a_video(
            self, client, root, monkeypatch):
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [])
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        sid = self._recording(root)
        ui_app._repair_orphan_recordings(root)
        got = client.get(f"/api/playtest/{sid}/video")
        assert got.status_code == 404
        assert "no video yet" in got.json()["error"]["message"]

    def test_a_salvaged_file_keeps_its_video(self, client, root, monkeypatch):
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [])
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: True)
        sid = self._recording(root)
        ui_app._repair_orphan_recordings(root)
        assert playtest.get(root, sid)["video_path"]
        assert client.get(f"/api/playtest/{sid}/video").status_code == 200

    def test_the_orphaned_capture_is_killed(self, root, monkeypatch):
        killed: list[int] = []
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [4242])
        monkeypatch.setattr(ui_app, "_kill_ffmpeg", killed.append)
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        self._recording(root)
        assert ui_app._repair_orphan_recordings(root)[0]["killed"] == [4242]
        assert killed == [4242]

    def test_a_session_with_no_path_is_still_closed(self, root, monkeypatch):
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        sid = self._recording(root, with_video=False)
        ui_app._repair_orphan_recordings(root)
        assert playtest.get(root, sid)["status"] == "failed"

    def test_recording_again_is_possible_afterwards(self, root, monkeypatch):
        """The point of all this: a stuck row blocked every future session."""
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [])
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        self._recording(root)
        ui_app._repair_orphan_recordings(root)
        assert db.connect(root).execute(
            "SELECT count(*) FROM playtest_session WHERE status = 'recording'"
        ).fetchone()[0] == 0

    def test_startup_runs_the_sweep(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setattr(ui_app, "_ffmpeg_pids_for", lambda _n: [])
        monkeypatch.setattr(ui_app, "_finalise_orphan_video", lambda _p: False)
        sid = self._recording(root)
        with TestClient(app):        # lifespan: startup handlers actually run
            pass
        assert playtest.get(root, sid)["status"] == "failed"


# ---------------------------------------------------------------------------
# Playtest start response and event
# ---------------------------------------------------------------------------
class TestPlaytestStart:
    def test_success_returns_the_session_and_emits_its_identity(
            self, client, monkeypatch):
        started = {
            "session_id": 42,
            "name": "controller pass",
            "recording": True,
        }
        emitted = []
        monkeypatch.setattr(playtest, "start", lambda *a, **k: started)
        monkeypatch.setattr(ui_app._events, "emit",
                            lambda *a, **k: emitted.append((a, k)))

        got = client.post("/api/playtest/start", json={
            "name": "controller pass",
        }).json()

        assert got == started
        assert emitted[0][1]["ref"] == "42"
        assert emitted[0][1]["payload"] == {"name": "controller pass"}


# ---------------------------------------------------------------------------
# 8. artifact_react reports what actually happened
# ---------------------------------------------------------------------------
class TestArtifactReact:
    def _artifact(self, root) -> int:
        from bgate_core.store import artifacts

        image = root / "hero.png"
        image.write_bytes(b"hero")
        return int(artifacts.register(root, "hero", image,
                                      producer="image_generate")["id"])

    def test_all_three_effects_report_individually(self, client, root):
        art = self._artifact(root)
        got = client.post(f"/api/artifacts/{art}/react",
                          json={"verdict": "like", "note": "on model"}).json()
        assert got["ok"] is True
        assert got["effects"]["disposition"] == {
            "attempted": True, "ok": True, "status": "approved"}
        assert got["effects"]["seat_note"]["ok"] is True
        assert got["effects"]["steer"]["attempted"] is False
        assert got["failed"] == []

    def test_a_failed_effect_is_not_reported_as_success(self, client, root,
                                                       monkeypatch):
        from bgate_core.store import artifacts

        art = self._artifact(root)
        monkeypatch.setattr(artifacts, "review", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("db is locked")))
        got = client.post(f"/api/artifacts/{art}/react",
                          json={"verdict": "dislike", "note": "off model"})
        body = got.json()
        assert body["ok"] is False              # THE finding: it used to say true
        assert body["failed"] == ["disposition"]
        assert "db is locked" in body["effects"]["disposition"]["error"]
        assert body["effects"]["seat_note"]["ok"] is True

    def test_a_steer_with_no_live_agent_is_a_failure_not_a_shrug(
            self, client, root):
        art = self._artifact(root)
        _dispatch._live.clear()
        item = _queue.add(root, "art", "paint the hero")
        got = client.post(f"/api/artifacts/{art}/react",
                          json={"verdict": "dislike", "note": "hands",
                                "item_id": item["id"]}).json()
        assert got["effects"]["steer"]["attempted"] is True
        assert got["effects"]["steer"]["ok"] is False
        assert "no live agent" in got["effects"]["steer"]["error"]
        assert got["ok"] is False and got["failed"] == ["steer"]
        # ...while the parts that DID work still say so.
        assert got["effects"]["disposition"]["ok"] is True
        assert got["steered"] is False          # legacy key the dashboard reads

    def test_a_bare_like_deliberately_does_not_steer(self, client, root):
        art = self._artifact(root)
        item = _queue.add(root, "art", "paint")
        got = client.post(f"/api/artifacts/{art}/react",
                          json={"verdict": "like", "item_id": item["id"]}).json()
        assert got["effects"]["steer"]["attempted"] is False
        assert "bare like" in got["effects"]["steer"]["reason"]
        assert got["ok"] is True

    def test_an_unknown_verdict_is_refused(self, client, root):
        art = self._artifact(root)
        got = client.post(f"/api/artifacts/{art}/react", json={"verdict": "meh"})
        assert got.status_code == 400
        assert got.json()["error"]["code"] == "bad_request"

    def test_a_missing_artifact_is_a_clean_404(self, client, root):
        got = client.post("/api/artifacts/99999/react", json={"verdict": "like"})
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# 9. The raw log knows where a run starts
# ---------------------------------------------------------------------------
class TestRawLogIsRunAware:
    def _log(self, root, item_id: int = 1) -> None:
        agents = root / ".bgate" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        lines = []
        for run in (1, 2):
            lines.append(json.dumps({"type": "bgate_run_start",
                                     "item_id": item_id, "run": run}))
            lines.append(json.dumps({"type": "assistant", "run": run}))
        (agents / f"item-{item_id}.log").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")

    def test_only_the_current_run_is_returned(self, client, root):
        self._log(root)
        got = client.get("/api/agent-log/1?tail=100").json()
        assert got["runs"] == 2
        assert len(got["lines"]) == 2                    # marker + its one event
        assert all('"run": 1' not in line for line in got["lines"])
        assert json.loads(got["lines"][1])["run"] == 2

    def test_a_tail_no_longer_splices_two_runs_together(self, client, root):
        self._log(root)
        got = client.get("/api/agent-log/1?tail=3").json()
        # tail 3 spans the boundary in the raw file; run 1 must not bleed in.
        assert all('"run": 1' not in line for line in got["lines"])

    def test_all_runs_are_available_with_a_separator(self, client, root):
        self._log(root)
        got = client.get("/api/agent-log/1?tail=100&all_runs=true").json()
        assert got["runs"] == 2
        seps = [line for line in got["lines"] if line.startswith("─")]
        assert len(seps) == 2
        assert "run 1 of 2" in seps[0] and "run 2 of 2" in seps[1]

    def test_a_log_with_no_marker_still_reads(self, client, root):
        agents = root / ".bgate" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "item-5.log").write_text("plain\nlines\n", encoding="utf-8")
        got = client.get("/api/agent-log/5").json()
        assert got["lines"] == ["plain", "lines"]
        assert got["runs"] == 0

    def test_a_missing_log_is_empty_not_an_error(self, client):
        got = client.get("/api/agent-log/404")
        assert got.status_code == 200
        assert got.json()["lines"] == []


# ---------------------------------------------------------------------------
# 1. One error convention on the backend
# ---------------------------------------------------------------------------
class TestOneErrorConvention:
    @pytest.mark.parametrize("method,path,body", [
        ("POST", "/api/queue", {"title": "no seat"}),
        ("POST", "/api/artifacts/99999/react", {"verdict": "like"}),
        ("POST", "/api/artifacts/99999/review", {"status": "approved"}),
        ("POST", "/api/artifacts/99999/restore", None),
        ("POST", "/api/artifacts/99999/regenerate", {}),
        ("POST", "/api/playtest/items/99999/dismiss", None),
        ("POST", "/api/playtest/items/99999/merge", {"target_id": 1}),
        ("POST", "/api/playtest/items/99999/promote", {}),
        ("GET", "/api/playtest/99999", None),
        ("GET", "/api/preview?rel=nope.png", None),
    ])
    def test_every_failure_is_the_shared_envelope(self, client, method, path,
                                                  body):
        got = client.request(method, path, json=body)
        assert 400 <= got.status_code < 500, got.text[:200]
        envelope = got.json()
        assert envelope["ok"] is False
        assert set(envelope["error"]) >= {"code", "message", "detail"}
        assert isinstance(envelope["error"]["message"], str)

    def test_no_handler_hand_rolls_a_flat_error_string(self, client, root):
        """The two that deliberately still do are documented as sentence+code."""
        import inspect

        source = inspect.getsource(ui_app)
        # Every remaining {"ok": False, "error": ...} literal carries a "code".
        for chunk in source.split('{"ok": False,')[1:]:
            assert '"code"' in chunk[:200], chunk[:200]

    def test_the_sentence_convention_carries_a_code(self, client):
        got = client.post("/api/playtest/stop").json()
        assert got["ok"] is False
        assert isinstance(got["error"], str)     # prose, as the record UI reads
        assert got["code"] == "not_recording"


# ---------------------------------------------------------------------------
# 2 and 6 — verifying the previous pass, not redoing it
# ---------------------------------------------------------------------------
class TestAlreadyFixed:
    def test_serve_prints_the_url_and_the_project(self, root, monkeypatch,
                                                  capsys):
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
        ui_app.serve(port=7799)
        printed = capsys.readouterr().out
        assert "http://127.0.0.1:7799" in printed
        assert str(root) in printed

    def test_video_of_an_unknown_session_is_404_not_500(self, client):
        got = client.get("/api/playtest/99999/video")
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"

    def test_a_session_with_no_video_is_not_a_security_error(self, client, root):
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_path) "
                "VALUES ('pending', 'pending', 'processing', '')")
            sid = int(cur.lastrowid)
        message = client.get(f"/api/playtest/{sid}/video").json()["error"]["message"]
        assert "no video yet" in message and "escape" not in message
