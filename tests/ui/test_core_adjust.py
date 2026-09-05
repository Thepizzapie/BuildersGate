"""The twelve audit findings, pinned.

Every test here exists because something CLAIMED to be a gate and was not: a
PreToolUse hook that guarded four tools while the agent held Bash, a QA loop with
no counter, a route registry that lost half the API in silence, document stores
that let the second tab erase the first. The bar for each is the same — the gate
must bite, and its failure must be visible.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_cli import hook
from bgate_cli.main import HOOK_COMMAND, HOOK_MATCHER, install_hook
from bgate_core.store import assets, db
from bgate_core.design import bible
from bgate_core.qa import feedback, playtest
from bgate_core.board import iterations, queue
from bgate_core.board import seats as _seats
from bgate_core.store import workspace
from bgate_ui.agents import qa_gate
from bgate_ui import routes as _routes


def bash(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


# ---------------------------------------------------------------------------
# 1. The hook guards Bash, not just the file-edit tools
# ---------------------------------------------------------------------------
class TestBashGuard:
    @pytest.mark.parametrize("command", [
        "echo hi > game/assets/rock.png",
        "echo hi >> game/assets/rock.png",
        "printf x 2> game/assets/rock.png",
        "cp /tmp/rock.png game/assets/rock.png",
        "mv other.png game/assets/rock.png",
        "rm game/assets/rock.png",
        "sed -i s/a/b/ game/assets/rock.png",
        "cat /tmp/x | tee game/assets/rock.png",
        "curl -o game/assets/rock.png http://example.com/x.png",
        "dd if=/dev/zero of=game/assets/rock.png",
        "truncate -s 0 game/assets/rock.png",
    ])
    def test_out_of_lane_bash_write_is_blocked(self, root, command):
        """gameplay does not own game/assets — the same rule Write obeys."""
        code, message = hook.decide(bash(command, str(root)), "gameplay")
        assert code == hook.BLOCK, command
        assert "game/assets/rock.png" in message
        assert "Bash" in message

    def test_in_lane_bash_write_is_allowed(self, root):
        code, _ = hook.decide(
            bash("echo 'extends Node' > game/scripts/player.gd", str(root)),
            "gameplay")
        assert code == hook.ALLOW

    def test_a_locked_binary_blocks_a_bash_write_too(self, root):
        assets.lock(root, "game/assets/shard.blend", "art")
        code, message = hook.decide(
            bash("cp new.blend game/assets/shard.blend", str(root)), "tech")
        assert code == hook.BLOCK
        assert "locked by seat 'art'" in message

    def test_a_leased_path_blocks_another_execution(self, root):
        assets.acquire_path_lease(root, "game/scripts/player.gd", "gameplay",
                                  "item-7", lease_s=600)
        code, message = hook.decide(
            bash("echo x > game/scripts/player.gd", str(root)),
            "gameplay", owner="item-9")
        assert code == hook.BLOCK
        assert "item-7" in message

    @pytest.mark.parametrize("command", [
        "ls game/scripts",
        "cat game/scripts/player.gd",
        "grep -rn extends game",
        "git status --porcelain",
        "git log --oneline -5",
        "python -m pytest tests -q",
        "python -c \"print(open('game/scripts/player.gd').read())\"",
        "rg jump game | head -20",
    ])
    def test_read_only_commands_are_never_in_the_way(self, root, command):
        code, message = hook.decide(bash(command, str(root)), "gameplay")
        assert code == hook.ALLOW, f"{command} -> {message}"

    @pytest.mark.parametrize("command", [
        "python -c \"open('game/assets/x.png','w').write('x')\"",
        "node -e \"require('fs').writeFileSync('x','y')\"",
        "git checkout -- game/assets/rock.png",
        "git apply /tmp/patch.diff",
        "echo 'unbalanced > game/assets/rock.png",
    ])
    def test_unanalysable_writes_fail_closed_inside_a_project(self, root, command):
        code, message = hook.decide(bash(command, str(root)), "gameplay")
        assert code == hook.BLOCK, command
        assert "cannot verify" in message

    def test_unanalysable_writes_outside_a_project_are_not_our_business(
            self, tmp_path):
        code, _ = hook.decide(
            bash("python -c \"open('x','w').write('1')\"", str(tmp_path)),
            "gameplay")
        assert code == hook.ALLOW

    def test_a_write_out_of_the_project_is_ignored(self, root, tmp_path_factory):
        # `root` IS tmp_path, so "elsewhere" has to be a genuinely other tree.
        elsewhere = (tmp_path_factory.mktemp("elsewhere") / "notes.txt").as_posix()
        code, _ = hook.decide(bash(f"echo hi > {elsewhere}", str(root)), "gameplay")
        assert code == hook.ALLOW

    def test_dev_null_is_not_a_file_anyone_owns(self, root):
        code, _ = hook.decide(bash("make build > /dev/null 2>&1", str(root)),
                              "gameplay")
        assert code == hook.ALLOW

    def test_every_segment_of_a_chain_is_checked(self, root):
        code, message = hook.decide(
            bash("ls game && echo x > game/assets/rock.png", str(root)), "gameplay")
        assert code == hook.BLOCK
        assert "game/assets/rock.png" in message

    def test_the_analysis_is_honest_about_what_it_found(self):
        got = hook.analyse_bash("cp a.png game/assets/b.png && ls")
        assert got["writes"] == ["game/assets/b.png"]
        assert got["unclear"] == []


# ---------------------------------------------------------------------------
# 2. Failing open is visible
# ---------------------------------------------------------------------------
class TestObservability:
    def test_a_crashing_hook_still_allows_but_leaves_a_record(
            self, root, monkeypatch, capsys):
        def _boom(*_a, **_k):
            raise RuntimeError("seeded oracle failure")

        monkeypatch.setattr(hook, "decide", _boom)
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setattr(
            hook.sys, "stdin",
            type("S", (), {"read": staticmethod(lambda: json.dumps(
                bash("echo x > game/assets/y.png", str(root))))})())

        assert hook.main([]) == hook.ALLOW  # fail-safe intact
        assert "FAILED OPEN" in capsys.readouterr().err

        logged = hook.recent_failures(str(root))
        assert logged and logged[-1]["event"] == "fail_open"
        assert "seeded oracle failure" in logged[-1]["detail"]

    def test_selftest_proves_enforcement_is_live(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        install_hook(str(root))
        report = hook.selftest(str(root))
        assert report["installed"] is True
        assert report["enforcing"] is True
        assert [p["ok"] for p in report["probes"]] == [True, True, True]

    def test_selftest_says_so_when_nothing_is_enforced(self, root, monkeypatch):
        """THIS USED TO ASSERT that an unset BGATE_SEAT meant nothing was
        enforced, which stopped being true when the seatless session started
        holding the director seat and taking path leases. The property worth
        protecting was never "no seat means inert" — it was "the status command
        must not claim enforcement it does not have". So the assertion moves to
        the case where that is still the situation: mode 'off'.
        """
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "off")
        report = hook.selftest(str(root))
        assert report["enforcing"] is False
        assert "BGATE_SEAT" in report["reason"]

    def test_selftest_reports_the_seatless_session_honestly(self, root, monkeypatch):
        """And the other half: it must not claim LESS than it has either.

        A seatless session is checked now — reporting it as inert would send a
        human looking for a gate that is already there.
        """
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_DIRECTOR_MODE", raising=False)
        report = hook.selftest(str(root))
        assert report["seated"] is False and report["mode"] == "collide"
        assert report["enforcing"] is True
        assert "DIRECTOR" in report["reason"]

    def test_selftest_catches_a_broken_oracle(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setattr(_seats, "can_write",
                            lambda *a, **k: {"allowed": True, "path": "x",
                                             "reason": ""})
        report = hook.selftest(str(root))
        assert report["enforcing"] is False
        assert "NOT trustworthy" in report["reason"]


# ---------------------------------------------------------------------------
# 3. hook-install is portable
# ---------------------------------------------------------------------------
class TestHookInstall:
    def test_no_machine_specific_path_is_committed(self, tmp_path):
        install_hook(str(tmp_path))
        text = (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert HOOK_COMMAND in text
        assert ":" not in json.loads(text)["hooks"]["PreToolUse"][0]["hooks"][0][
            "command"]  # no C:\ or /usr/bin/... drive or scheme
        assert "Bash" in HOOK_MATCHER

    def test_a_stale_entry_is_upgraded_in_place(self, tmp_path):
        stale = {"hooks": {"PreToolUse": [{
            "matcher": "Write|Edit",
            "hooks": [{"type": "command",
                       "command": "C:/someone/else/venv/python.exe -m bgate_cli.hook"}],
        }]}}
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps(stale))

        got = install_hook(str(tmp_path))
        assert got["updated"] is True
        entry = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]
        assert entry["matcher"] == HOOK_MATCHER
        assert entry["hooks"][0]["command"] == HOOK_COMMAND


# ---------------------------------------------------------------------------
# 4. The QA gate cannot loop forever
# ---------------------------------------------------------------------------
class TestQaGateCap:
    @pytest.fixture()
    def dispatched(self, monkeypatch):
        from bgate_ui.agents import dispatch as _dispatch
        calls: list[int] = []
        monkeypatch.setattr(_dispatch, "dispatch",
                            lambda root, item_id, **kw: calls.append(item_id) or
                            {"ok": True, "item_id": item_id})
        return calls

    def _round(self, root, item_id: int, n: int) -> None:
        """One fail -> reopen -> re-done cycle, as the reviewer drives it.

        updated_at is forced forward because SQLite stores whole seconds: a
        same-second re-completion is indistinguishable from the first, and the
        gate would (correctly) treat it as already reviewed.
        """
        for gate in db.connect(root).execute(
                "SELECT id FROM work_item WHERE source = 'qa-gate' "
                "AND source_ref = ? AND status IN ('queued', 'dispatched')",
                (str(item_id),)).fetchall():
            queue.set_status(root, int(gate["id"]), "done",
                             result="VERDICT: FAIL — still not matching the ref")
        queue.reopen(root, item_id, f"still wrong, round {n}")
        queue.set_status(root, item_id, "done", result=f"round {n} attempt")
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET updated_at = ? WHERE id = ?",
                         (f"2100-01-0{n + 1} 00:00:00", item_id))

    def test_the_loop_stops_and_escalates_to_a_human(self, root, dispatched):
        item = queue.add(root, "art", "HUD meter")
        queue.set_status(root, item["id"], "done", result="v1")

        gates = 0
        for n in range(6):
            qa_gate._scan_once(root, "1970-01-01 00:00:00")
            gates = len([r for r in db.connect(root).execute(
                "SELECT 1 FROM work_item WHERE source = 'qa-gate'")])
            self._round(root, item["id"], n)

        assert gates == qa_gate.MAX_ROUNDS, "the gate kept re-reviewing forever"
        escalations = [dict(r) for r in db.connect(root).execute(
            "SELECT * FROM work_item WHERE source = ?",
            (qa_gate.ESCALATION_SOURCE,))]
        assert len(escalations) == 1
        assert escalations[0]["seat"] == "director"
        assert escalations[0]["status"] == "queued"
        # Escalation is for a HUMAN: spending another agent on the same argument
        # is exactly what the cap exists to prevent.
        assert escalations[0]["id"] not in dispatched

    def test_the_escalation_is_filed_once(self, root, dispatched):
        item = queue.add(root, "gameplay", "dash feel")
        queue.set_status(root, item["id"], "done")
        with db.tx(root) as conn:
            conn.execute("UPDATE work_item SET attempts = 9 WHERE id = ?",
                         (item["id"],))
        for _ in range(3):
            qa_gate._scan_once(root, "1970-01-01 00:00:00")
        assert len([r for r in db.connect(root).execute(
            "SELECT 1 FROM work_item WHERE source = ?",
            (qa_gate.ESCALATION_SOURCE,))]) == 1
        assert dispatched == []

# ---------------------------------------------------------------------------
# 5 + 6. The director board is bounded and lineage survives a reload
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app
    return TestClient(app)


class TestOrchestrator:
    def test_the_overview_is_paged_and_brief_free(self, root, client):
        for n in range(12):
            queue.add(root, "art", f"item {n}", brief="x" * 5000)
        got = client.get("/api/orchestrator/overview?limit=5").json()
        shown = [i for column in got["queue"].values() for i in column]
        assert len(shown) == 5
        assert got["page"]["total"] == 12
        assert got["page"]["next_offset"] == 5
        assert got["totals"]["by_seat"]["art"] == 12
        assert all("brief" not in item for item in shown)
        assert shown[0]["brief_len"] == 5000
        assert len(shown[0]["brief_preview"]) == 200

    def test_delegation_lineage_is_persisted_not_remembered(self, root, client,
                                                            monkeypatch):
        from bgate_ui.agents import dispatch as _dispatch
        monkeypatch.setattr(_dispatch, "dispatch",
                            lambda *a, **k: {"ok": True, "pid": 1234})
        monkeypatch.setattr(_dispatch, "status", lambda *a, **k: [])

        source = queue.add(root, "director", "Ship the fight HUD")
        posted = client.post("/api/orchestrator/delegate",
                             json={"item_id": source["id"]}).json()
        delegate_id = posted["delegate_item_id"]

        # What the director agent then does, through queue_add: the brief it was
        # told to write carries the delegation id.
        from bgate_ui.routes.orchestrator import DELEGATED_FROM
        child = queue.add(
            root, "art", "Draw the HUD frame",
            brief=f"{DELEGATED_FROM}{delegate_id} (source #{source['id']})\n\n"
                  "Draw it against concept-fight-hud.",
            source="seat:director")

        # A fresh request — i.e. the page reloaded and remembers nothing.
        tree = client.get(f"/api/orchestrator/lineage/{delegate_id}").json()
        assert tree["parent"]["id"] == source["id"]
        assert [c["id"] for c in tree["children"]] == [child["id"]]

        overview = client.get("/api/orchestrator/overview").json()
        assert overview["lineage"]["parents"][str(child["id"])] == delegate_id

    def test_the_delegate_brief_names_its_own_id(self, root, client, monkeypatch):
        from bgate_ui.agents import dispatch as _dispatch
        monkeypatch.setattr(_dispatch, "dispatch",
                            lambda *a, **k: {"ok": True, "pid": 1})
        source = queue.add(root, "gameplay", "make the dash feel good")
        delegate_id = client.post("/api/orchestrator/delegate",
                                  json={"item_id": source["id"]}).json()[
            "delegate_item_id"]
        from bgate_ui.routes.orchestrator import DELEGATED_FROM
        assert f"{DELEGATED_FROM}{delegate_id}" in queue.get(root, delegate_id)["brief"]


# ---------------------------------------------------------------------------
# 7. refs_upload cannot be talked into writing somewhere else
# ---------------------------------------------------------------------------
class TestRefUpload:
    @pytest.mark.parametrize("name", [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\evil",
        "sub/dir/name",
    ])
    def test_a_traversal_name_is_refused(self, root, client, name):
        got = client.post("/api/refs/upload", json={
            "name": name, "ext": "png",
            "data": "iVBORw0KGgo="})
        assert got.status_code == 400
        assert "path" in got.text

    def test_a_normal_upload_still_works(self, root, client):
        import base64
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
        got = client.post("/api/refs/upload",
                          json={"name": "Concept Fight HUD", "ext": "png",
                                "data": png, "kind": "concept"})
        assert got.status_code == 200, got.text
        assert "concept-fight-hud" in json.dumps(got.json())


# ---------------------------------------------------------------------------
# 8. A route module that fails to import is loud
# ---------------------------------------------------------------------------
class TestRouteRegistry:
    def test_a_failed_import_is_recorded_and_served(self, monkeypatch, client):
        import importlib

        real = importlib.import_module

        def _explode(name, *a, **k):
            if name.endswith(".refs"):
                raise ImportError("no module named python_multipart")
            return real(name, *a, **k)

        from fastapi import FastAPI
        monkeypatch.setattr(importlib, "import_module", _explode)
        registered = _routes.register(FastAPI())
        assert "refs" not in registered
        assert [f["module"] for f in _routes.FAILURES] == ["refs"]

        # And it is visible at runtime, not only in a startup print nobody read.
        body = client.get("/api/routes/status").json()
        assert body["ok"] is False
        assert body["failed"][0]["module"] == "refs"
        assert "python_multipart" in body["failed"][0]["error"]

    def test_strict_mode_refuses_to_start_half_an_api(self, monkeypatch):
        import importlib

        from fastapi import FastAPI
        real = importlib.import_module
        monkeypatch.setenv("BGATE_STRICT_ROUTES", "1")
        monkeypatch.setattr(importlib, "import_module",
                            lambda name, *a, **k: (_ for _ in ()).throw(
                                ImportError("boom")) if name.endswith(".refs")
                            else real(name, *a, **k))
        with pytest.raises(RuntimeError, match="refs"):
            _routes.register(FastAPI())

    def test_a_healthy_registry_says_so(self, client):
        # Re-register cleanly so a previous test's monkeypatched failure is gone.
        from fastapi import FastAPI
        _routes.register(FastAPI())
        body = client.get("/api/routes/status").json()
        assert body["ok"] is True
        assert "orchestrator" in body["registered"]


# ---------------------------------------------------------------------------
# 9. Feedback keeps the END of the remark
# ---------------------------------------------------------------------------
class TestFeedbackSpan:
    def _session(self, root) -> int:
        with db.tx(root) as conn:
            return int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status) "
                "VALUES ('span', 'span', 'ready')").lastrowid)

    def test_the_window_covers_the_whole_spoken_thought(self, root):
        """The complaint runs 0->14s and the jump it is about happens at 13s.
        Anchored to t alone with a 4s window, the evidence is invisible."""
        session = self._session(root)
        with db.tx(root) as conn:
            segments = []
            for start, end, text in ((0.0, 6.0, "so when I jump here"),
                                     (6.4, 14.0, "it just floats forever, "
                                                 "that is not right")):
                segments.append(int(conn.execute(
                    "INSERT INTO playtest_segment "
                    "(session_id, t_start, t_end, text) VALUES (?, ?, ?, ?)",
                    (session, start, end, text)).lastrowid))
            conn.execute(
                "INSERT INTO playtest_item "
                "(session_id, segment_id, t, kind, text, seat) "
                "VALUES (?, ?, 0.0, 'fix', 'so when I jump here it just floats "
                "forever', 'gameplay')", (session, segments[0]))
            conn.execute(
                "INSERT INTO playtest_event (session_id, t, kind, data) "
                "VALUES (?, 13.0, 'jump', '{\"air_time\": 1.4}')", (session,))

        item = playtest.brief(root, session, window_s=4.0)["items"][0]
        assert item["t_end"] == pytest.approx(14.0)
        assert [e["kind"] for e in item["events"]] == ["jump"]

    def test_an_item_with_no_segment_is_still_an_instant(self, root):
        session = self._session(root)
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat) "
                "VALUES (?, 5.0, 'fix', 'hand typed', 'gameplay')", (session,))
            conn.execute(
                "INSERT INTO playtest_event (session_id, t, kind, data) "
                "VALUES (?, 40.0, 'death', '{}')", (session,))
        item = playtest.brief(root, session, window_s=4.0)["items"][0]
        assert item["t_end"] == 5.0
        assert item["events"] == []

    def test_extract_still_reports_the_span_the_insert_must_keep(self):
        items = feedback.extract([
            {"id": 1, "t_start": 0.0, "t_end": 6.0, "text": "the jump is floaty"},
            {"id": 2, "t_start": 6.5, "t_end": 12.0, "text": "really not good at all"},
        ])
        assert items[0]["t"] == 0.0 and items[0]["t_end"] == 12.0


# ---------------------------------------------------------------------------
# 10. Opening a review does not shell out to ffmpeg
# ---------------------------------------------------------------------------
class TestReviewIsNonBlocking:
    def test_the_filmstrip_runs_as_a_job(self, root, monkeypatch, tmp_path):
        video = tmp_path / "session.mp4"
        video.write_bytes(b"not really an mp4")
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_path, "
                "duration_s) VALUES ('s', 's', 'ready', ?, 600)",
                (str(video),)).lastrowid)

        called: list[str] = []

        def _never(*_a, **_k):
            called.append("ffmpeg")
            raise AssertionError("ffmpeg ran inside the request")

        monkeypatch.setattr(playtest, "_ensure_filmstrip", _never)
        # The job body is what may block; the brief itself must not.
        monkeypatch.setattr(playtest, "_filmstrip_job",
                            lambda root_, s: {"state": "extracting", "job_id": 42})

        got = playtest.brief(root, session)
        assert called == []
        assert got["filmstrip"]["state"] == "extracting"
        assert got["filmstrip"]["job_id"] == 42
        assert got["video_frames"] == []

    def test_a_second_open_does_not_start_a_second_extraction(self, root,
                                                              monkeypatch, tmp_path):
        from bgate_core.board import jobs

        video = tmp_path / "s2.mp4"
        video.write_bytes(b"x")
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_path, "
                "duration_s) VALUES ('s2', 's2', 'ready', ?, 60)",
                (str(video),)).lastrowid)
        started: list[int] = []

        def _fake_run(root_, kind, work, request=None, actor=""):
            started.append(1)
            return jobs.create(root_, kind, request=request)

        monkeypatch.setattr(jobs, "run_in_background", _fake_run)
        playtest.brief(root, session)
        playtest.brief(root, session)
        assert started == [1]

    def test_a_failed_extraction_is_reported_not_retried_forever(
            self, root, monkeypatch, tmp_path):
        from bgate_core.board import jobs

        video = tmp_path / "s4.mp4"
        video.write_bytes(b"x")
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_path, "
                "duration_s) VALUES ('s4', 's4', 'ready', ?, 60)",
                (str(video),)).lastrowid)
        job_id = jobs.create(root, playtest.FILMSTRIP_JOB,
                             request={"session_id": session})
        jobs.finish(root, job_id, status="failed", error="ffmpeg not found")

        starts: list[int] = []
        monkeypatch.setattr(jobs, "run_in_background",
                            lambda *a, **k: starts.append(1))
        got = playtest.brief(root, session)["filmstrip"]
        assert got["state"] == "failed"
        assert "ffmpeg" in got["error"]
        assert starts == []

    def test_no_video_means_no_job_at_all(self, root):
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status) "
                "VALUES ('s3', 's3', 'ready')").lastrowid)
        assert playtest.brief(root, session)["filmstrip"]["state"] == "no_video"


# ---------------------------------------------------------------------------
# 11. Documents are not last-write-wins
# ---------------------------------------------------------------------------
class TestOptimisticConcurrency:
    def test_two_tabs_cannot_silently_eat_each_other(self, root):
        workspace.set(root, "narrative", "storyboard", {"beats": ["one"]})
        tab_a = workspace.get(root, "narrative", "storyboard")
        tab_b = workspace.get(root, "narrative", "storyboard")

        tab_a["beats"].append("two")
        workspace.set(root, "narrative", "storyboard", tab_a)

        tab_b["beats"].append("an afternoon of work")
        with pytest.raises(workspace.StaleWrite) as caught:
            workspace.set(root, "narrative", "storyboard", tab_b)
        assert "changed since you loaded it" in str(caught.value)
        # The winner's write survived untouched.
        assert workspace.get(root, "narrative", "storyboard")["beats"] == ["one", "two"]

    def test_the_version_is_metadata_not_content(self, root):
        workspace.set(root, "art", "flow", {"nodes": []})
        doc = workspace.get(root, "art", "flow")
        assert doc[workspace.VERSION_KEY]
        workspace.set(root, "art", "flow", doc)
        stored = db.connect(root).execute(
            "SELECT data_json FROM workspace_doc WHERE seat = 'art'").fetchone()
        assert workspace.VERSION_KEY not in json.loads(stored["data_json"])

    def test_a_first_write_needs_no_version(self, root):
        assert workspace.set(root, "qa", "bots", {"a": 1})["ok"] is True

    def test_bible_update_refuses_a_stale_edit(self, root):
        section = bible.add(root, "pillar", "Tension over spectacle",
                            body="the original")
        editor_a = bible.get(root, section["id"])
        editor_b = bible.get(root, section["id"])

        bible.update(root, section["id"], body="A's careful rewrite",
                     expected_version=editor_a["version"])
        with pytest.raises(bible.StaleWrite):
            bible.update(root, section["id"], body="B's rewrite of the old text",
                         expected_version=editor_b["version"])
        assert bible.get(root, section["id"])["body"] == "A's careful rewrite"

    def test_an_unversioned_bible_edit_still_works(self, root):
        """Existing callers keep working — the check is opt-in per caller, and
        the read-modify-write is now serialised regardless."""
        section = bible.add(root, "loop", "placeholder")
        got = bible.update(root, section["id"], title="Scavenge, craft, survive")
        assert got["title"] == "Scavenge, craft, survive"
        assert got["version"] != bible.version_of(section)


# ---------------------------------------------------------------------------
# 12. The iteration timeline says whether it got better
# ---------------------------------------------------------------------------
class TestIterationProgress:
    def _iteration_with_feedback(self, root, goal: str, problems: int,
                                 resolved: int) -> int:
        iteration = iterations.create(root, goal)
        iteration_id = int(iteration["id"])
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status, iteration_id) "
                "VALUES (?, 'sess', 'ready', ?)", (goal, iteration_id)).lastrowid)
            ids = [int(conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (?, ?, 'fix', ?, 'gameplay', 'promoted')",
                (session, float(n), f"problem {n}")).lastrowid)
                for n in range(problems)]
        for item_id in ids[:resolved]:
            work = queue.add(root, "gameplay", f"fix {item_id}",
                             source="playtest", source_ref=str(item_id))
            queue.set_status(root, work["id"], "done", result="fixed")
        return iteration_id

    def test_it_answers_better_or_worse_not_just_hashes(self, root):
        first = self._iteration_with_feedback(root, "round one", problems=4,
                                              resolved=0)
        iterations.complete_from_playtest(
            root, first, int(db.connect(root).execute(
                "SELECT id FROM playtest_session WHERE iteration_id = ?",
                (first,)).fetchone()[0]))
        second = self._iteration_with_feedback(root, "round two", problems=2,
                                               resolved=2)

        got = iterations.progress(root, second)
        assert got["previous_id"] == first
        assert got["feedback_resolved"] == 2
        assert got["open_problems"] == 0
        assert got["deltas"]["open_problems"] == -4
        assert got["verdict"] == "better"
        assert any("open problems" in r for r in got["reasons"])

    def test_a_growing_backlog_reads_as_worse(self, root):
        self._iteration_with_feedback(root, "round one", problems=1, resolved=1)
        second = self._iteration_with_feedback(root, "round two", problems=5,
                                               resolved=0)
        got = iterations.progress(root, second)
        assert got["verdict"] == "worse"
        assert got["open_problems"] == 5

    def test_the_first_iteration_is_a_baseline_not_a_verdict(self, root):
        first = int(iterations.create(root, "the very first")["id"])
        assert iterations.progress(root, first)["verdict"] == "baseline"

    def test_nothing_measured_is_reported_as_unknown(self, root):
        iterations.create(root, "one")
        second = int(iterations.create(root, "two")["id"])
        got = iterations.progress(root, second)
        assert got["verdict"] == "unknown"
        assert any("nothing measured" in r for r in got["reasons"])

    def test_rework_rides_along(self, root):
        """Rework is a comparable number. Spend is not one this product has:
        the ledger it used to read is gone (db migration 0045)."""
        iteration_id = int(iterations.create(root, "costly")["id"])
        item = queue.add(root, "art", "expensive thing")
        queue.set_status(root, item["id"], "done")
        queue.reopen(root, item["id"], "not good enough")
        queue.set_status(root, item["id"], "done", result="round two")
        got = iterations.progress(root, iteration_id)
        assert got["rework_rounds"] >= 1
        assert "spend_usd" not in got

    def test_get_carries_the_verdict(self, root):
        iteration_id = int(iterations.create(root, "carried")["id"])
        assert "verdict" in iterations.get(root, iteration_id)["progress"]


# ---------------------------------------------------------------------------
# minor: seat_brief is capped
# ---------------------------------------------------------------------------
class TestSeatBriefCap:
    def test_the_brief_every_agent_reads_first_is_bounded(self, root):
        from bgate_core.design import lore
        for n in range(_seats.MAX_CANON + 12):
            lore.add_entity(root, "character", f"Extra {n}", summary="s" * 200,
                            status="canon")

        got = _seats.brief(root, "narrative")
        assert len(got["canon"]) == _seats.MAX_CANON
        assert got["truncated"]["lore_list"]["total"] == _seats.MAX_CANON + 12
        assert "lore_list" in got["truncated"]["lore_list"]["note"]

    def test_a_giant_bible_body_is_trimmed_not_shipped_whole(self, root):
        bible.add(root, "pillar", "Long one", body="x" * 50_000)
        section = _seats.brief(root, "director")["bible"]["pillars"][0]
        assert len(section["body"]) < _seats.BODY_CHARS + 200
        assert "bible_read" in section["body"]


# ---------------------------------------------------------------------------
# coordinator follow-ups: the review overlay's two backend halves
# ---------------------------------------------------------------------------
class TestReviewPayload:
    def test_video_offset_reaches_the_reviewer(self, root):
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status, video_offset_s, "
                "audio_offset_s) VALUES ('o', 'o', 'ready', 1.75, 0.5)").lastrowid)
        got = playtest.brief(root, session)["session"]
        assert got["video_offset_s"] == 1.75
        assert got["audio_offset_s"] == 0.5


class TestUnmerge:
    @pytest.fixture()
    def pair(self, root):
        with db.tx(root) as conn:
            session = int(conn.execute(
                "INSERT INTO playtest_session (name, slug, status) "
                "VALUES ('m', 'm', 'ready')").lastrowid)
            ids = [int(conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat) "
                "VALUES (?, ?, 'fix', ?, 'gameplay')",
                (session, float(n), f"report {n}")).lastrowid) for n in range(2)]
        return ids

    def test_a_misclicked_merge_can_be_undone(self, root, pair):
        source, target = pair
        playtest.merge(root, source, target)
        assert playtest.get_item(root, source)["status"] == "dismissed"

        restored = playtest.unmerge(root, source)
        assert restored["status"] == "new"
        assert restored["merged_into_id"] is None

    def test_unmerge_is_reachable_through_the_patch_route(self, root, pair):
        source, target = pair
        playtest.update_item(root, source, merged_into_id=target)
        assert playtest.get_item(root, source)["merged_into_id"] == target

        playtest.update_item(root, source, merged_into_id=None)
        assert playtest.get_item(root, source)["status"] == "new"

    def test_unmerging_something_that_was_never_merged_is_an_error(self, root, pair):
        with pytest.raises(ValueError, match="not merged"):
            playtest.unmerge(root, pair[0])
