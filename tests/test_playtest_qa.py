"""The QA leg of a playtest: writable evidence, filable reports, a live meter.

These cover the four things the audit said were missing, and they cover them at
the level where they can actually break: the report is asserted on its CONTENT
(the quote, the build, the smoking-gun telemetry event) rather than on it being
non-empty, and the zip is asserted on its NAMELIST, because a report whose
images do not travel with it is exactly the failure being fixed.

Hardware is never touched — no mic, no ffmpeg, no whisper.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, playtest


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app

    return TestClient(app)


@pytest.fixture()
def session(root):
    """A finished session with a build identity, as stop() would leave it."""
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, duration_s, "
            "build_ref, telemetry_path, frames_dir) "
            "VALUES ('Boss fight pass', 'boss-fight-pass', 'ready', 124.0, "
            "'a1b2c3d4e5f6+dirty', ?, ?)",
            (str(root / "tel.jsonl"), str(root / "frames")))
        return int(cur.lastrowid)


@pytest.fixture()
def item(root, session):
    """One promoted bug with a captured frame and telemetry around it."""
    frame = root / "frames" / "t0012.50.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8\xff jpeg-ish")
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_item (session_id, t, kind, text, seat, "
            "frame_path, status) VALUES (?, 12.5, 'fix', "
            "'the jump feels floaty when I land on the boss platform', "
            "'gameplay', ?, 'promoted')", (session, str(frame)))
        item_id = int(cur.lastrowid)
        for t, kind, data in [
            (11.8, "jump", '{"air_time": 0.92}'),   # the smoking gun
            (12.4, "fps", '{"fps": 59.4}'),         # heartbeat — must be dropped
            (13.0, "land", '{"impact": 3.1}'),
            (90.0, "death", "{}"),                  # far away — out of window
        ]:
            conn.execute("INSERT INTO playtest_event (session_id, t, kind, data) "
                         "VALUES (?, ?, ?, ?)", (session, t, kind, data))
    return item_id


class TestNotesAndRepro:
    def test_notes_and_repro_round_trip_through_core(self, root, item):
        playtest.update_item(
            root, item,
            notes="only on the second phase, never the first",
            repro_steps="1. start the boss fight\n2. jump onto the left platform")
        got = playtest.get_item(root, item)
        assert got["notes"].startswith("only on the second phase")
        assert "left platform" in got["repro_steps"]

    def test_columns_default_empty_not_null(self, root, item):
        got = playtest.get_item(root, item)
        assert got["notes"] == ""
        assert got["repro_steps"] == ""

    def test_partial_update_leaves_the_other_fields_alone(self, root, item):
        playtest.update_item(root, item, repro_steps="1. jump")
        playtest.update_item(root, item, seat="qa")
        got = playtest.get_item(root, item)
        assert got["repro_steps"] == "1. jump"     # not clobbered by the reroute
        assert got["seat"] == "qa"

    def test_unknown_field_is_refused(self, root, item):
        with pytest.raises(ValueError, match="status"):
            playtest.update_item(root, item, status="promoted")

    def test_bad_seat_is_refused(self, root, item):
        with pytest.raises(ValueError, match="seat"):
            playtest.update_item(root, item, seat="wizard")

    def test_missing_item_is_a_lookup_error(self, root):
        with pytest.raises(LookupError):
            playtest.update_item(root, 999, notes="x")

    def test_patch_endpoint(self, client, root, item):
        got = client.patch(f"/api/playtest/items/{item}",
                           json={"repro_steps": "1. launch\n2. jump twice",
                                 "notes": "reproduced 3/5"})
        assert got.status_code == 200
        body = got.json()
        assert body["ok"] is True
        assert body["data"]["repro_steps"].endswith("jump twice")
        assert playtest.get_item(root, item)["notes"] == "reproduced 3/5"

    def test_patch_with_nothing_to_set_is_a_400(self, client, item):
        got = client.patch(f"/api/playtest/items/{item}", json={})
        assert got.status_code == 400
        assert got.json()["error"]["code"] == "bad_request"

    def test_patch_missing_item_is_a_404(self, client):
        got = client.patch("/api/playtest/items/4242", json={"notes": "x"})
        assert got.status_code == 404

    def test_brief_carries_notes_and_repro_to_the_director(self, root, session, item):
        playtest.update_item(root, item, notes="phase two only",
                             repro_steps="1. start the fight")
        got = playtest.brief(root, session)["items"][0]
        assert got["notes"] == "phase two only"
        assert got["repro_steps"] == "1. start the fight"


class TestBugReport:
    def test_markdown_carries_the_whole_evidence_chain(self, root, session, item):
        playtest.update_item(
            root, item, notes="only in phase two",
            repro_steps="1. start the boss fight\n2. jump onto the platform")
        md = playtest.report(root, session)["markdown"]

        assert "a1b2c3d4e5f6+dirty" in md                  # build identity
        assert "the jump feels floaty" in md               # the verbatim quote
        assert "00:12.50" in md                            # session-clock stamp
        assert "1. start the boss fight" in md             # repro steps
        assert "only in phase two" in md                   # notes
        assert "air_time" in md and "`jump`" in md         # nearby telemetry
        assert "t0012.50.jpg" in md                        # the frame, linked

    def test_fps_heartbeats_are_kept_out_of_the_report(self, root, session, item):
        """A ticket full of fps ticks buries the one event that explains it."""
        md = playtest.report(root, session)["markdown"]
        assert "`fps`" not in md
        assert "`land`" in md

    def test_only_promoted_items_are_filed_by_default(self, root, session, item):
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (?, 40.0, 'note', 'huh, weird', 'unassigned', 'new')",
                (session,))
        built = playtest.report(root, session)
        assert built["items"] == [item]
        assert "huh, weird" not in built["markdown"]

    def test_missing_repro_steps_say_so_instead_of_looking_complete(
            self, root, session, item):
        md = playtest.report(root, session)["markdown"]
        assert "nobody has reproduced this" in md

    def test_per_item_report_covers_one_item_whatever_its_status(
            self, root, session, item):
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (?, 40.0, 'fix', 'the boss music cuts out', 'audio', 'new')",
                (session,))
            other = int(cur.lastrowid)
        built = playtest.item_report(root, other)
        assert built["items"] == [other]
        assert "the boss music cuts out" in built["markdown"]
        assert "the jump feels floaty" not in built["markdown"]

    def test_zip_carries_the_markdown_and_the_frames_it_links(
            self, root, session, item):
        built = playtest.report_zip(root, session)
        with zipfile.ZipFile(io.BytesIO(built["bytes"])) as zf:
            names = zf.namelist()
            assert "report.md" in names
            assert "frames/t0012.50.jpg" in names
            body = zf.read("report.md").decode("utf-8")
            # Every image the markdown links must be inside the archive, or the
            # attachment is a bug report with no evidence in it.
            assert "frames/t0012.50.jpg" in body
            assert zf.read("frames/t0012.50.jpg").startswith(b"\xff\xd8\xff")
        assert built["filename"].endswith(".zip")

    def test_zip_survives_a_frame_that_vanished_off_disk(self, root, session, item):
        (root / "frames" / "t0012.50.jpg").unlink()
        built = playtest.report_zip(root, session)
        with zipfile.ZipFile(io.BytesIO(built["bytes"])) as zf:
            assert zf.namelist() == ["report.md"]

    def test_report_endpoint_returns_markdown(self, client, session, item):
        got = client.get(f"/api/playtest/{session}/report")
        assert got.status_code == 200
        assert got.headers["content-type"].startswith("text/markdown")
        assert "the jump feels floaty" in got.text

    def test_report_endpoint_zip_is_attachable(self, client, session, item):
        got = client.get(f"/api/playtest/{session}/report", params={"format": "zip"})
        assert got.status_code == 200
        assert got.headers["content-type"] == "application/zip"
        assert "attachment;" in got.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
            assert "report.md" in zf.namelist()

    def test_item_report_endpoint(self, client, item):
        got = client.get(f"/api/playtest/items/{item}/report")
        assert got.status_code == 200
        assert "the jump feels floaty" in got.text

    def test_report_for_an_unknown_session_is_a_404(self, client):
        assert client.get("/api/playtest/4242/report").status_code == 404

    def test_bad_format_is_a_400(self, client, session, item):
        got = client.get(f"/api/playtest/{session}/report",
                         params={"format": "pdf"})
        assert got.status_code == 400


class TestQaQueue:
    def test_queue_shape(self, root, session, item):
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat, status) "
                "VALUES (?, 40.0, 'fix', 'the hitbox is way too big', 'gameplay', "
                "'new')", (session,))
        got = playtest.qa_queue(root)

        assert got["sessions"][0]["id"] == session
        assert got["sessions"][0]["items"] == 2
        assert got["sessions"][0]["untriaged"] == 1
        assert got["sessions"][0]["telemetry_events"] == 4

        assert [i["text"] for i in got["untriaged"]] == ["the hitbox is way too big"]
        assert got["untriaged"][0]["clock"] == "00:40.00"
        assert got["untriaged"][0]["has_repro"] is False

        # The promoted bug with nothing written down IS the QA backlog.
        assert [i["id"] for i in got["needs_repro"]] == [item]
        assert got["counts"]["untriaged"] == 1

    def test_written_repro_clears_the_backlog(self, root, item):
        playtest.update_item(root, item, repro_steps="1. start the fight")
        got = playtest.qa_queue(root)
        assert got["needs_repro"] == []
        assert got["counts"]["needs_repro"] == 0

    def test_qa_queue_endpoint(self, client, session, item):
        got = client.get("/api/playtest/qa-queue")
        assert got.status_code == 200
        body = got.json()
        assert body["ok"] is True
        assert body["data"]["sessions"][0]["id"] == session
        assert body["data"]["counts"]["needs_repro"] == 1


class TestReproChecks:
    def test_promote_can_queue_a_parallel_qa_confirm_item(self, root, session):
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat) "
                "VALUES (?, 5.0, 'fix', 'falling through the floor', 'gameplay')",
                (session,))
            new_item = int(cur.lastrowid)

        got = playtest.promote(root, new_item, seat="gameplay", qa_confirm=True)
        assert got["status"] == "promoted"
        assert got["qa_item"]["seat"] == "qa"
        assert "falling through the floor" in got["qa_item"]["title"]
        assert str(new_item) in got["qa_item"]["brief"]

    def test_promote_without_the_flag_authors_no_work(self, root, item):
        playtest.promote(root, item)
        count = db.connect(root).execute(
            "SELECT count(*) FROM work_item WHERE source = 'playtest-repro'"
        ).fetchone()[0]
        assert count == 0

    def test_repro_check_is_idempotent(self, root, item):
        first = playtest.queue_repro_check(root, item)
        second = playtest.queue_repro_check(root, item)
        assert first["existing"] is False
        assert second["existing"] is True
        assert second["id"] == first["id"]

    def test_repro_check_endpoint_surfaces_in_the_queue(self, client, root, item):
        got = client.post(f"/api/playtest/items/{item}/repro-check")
        assert got.status_code == 200
        assert got.json()["data"]["seat"] == "qa"
        queue = playtest.qa_queue(root)
        assert queue["counts"]["open_repro_checks"] == 1


class TestCaptureTarget:
    def test_a_stale_window_title_refuses_rather_than_grabbing_the_desktop(
            self, monkeypatch):
        """Silently recording the whole desktop is the bug being fixed."""
        from bgate_adapters import recorder

        monkeypatch.setattr(recorder.sys, "platform", "win32")
        monkeypatch.setattr(recorder, "list_windows", lambda *a, **k: [
            {"pid": 1, "process": "chrome", "title": "Inbox (413)"}])
        with pytest.raises(recorder.RecorderError, match="no visible window"):
            recorder.resolve_window("Boss Fight")

    def test_the_project_name_targets_the_game_window_unattended(self, monkeypatch):
        from bgate_adapters import recorder

        monkeypatch.setattr(recorder.sys, "platform", "win32")
        monkeypatch.setattr(recorder, "list_windows", lambda *a, **k: [
            {"pid": 1, "process": "chrome", "title": "Inbox (413)"},
            {"pid": 2, "process": "godot", "title": "Test Game (DEBUG)"}])
        got = recorder.resolve_window(None, hints=["Test Game"])
        assert got["title"] == "Test Game (DEBUG)"
        assert got["whole_desktop"] is False

    def test_the_desktop_fallback_announces_itself(self, monkeypatch):
        from bgate_adapters import recorder

        monkeypatch.setattr(recorder.sys, "platform", "win32")
        monkeypatch.setattr(recorder, "list_windows", lambda *a, **k: [])
        got = recorder.resolve_window(None, hints=["Test Game"])
        assert got["whole_desktop"] is True
        assert "WHOLE DESKTOP" in got["note"]

    def test_window_hints_come_from_the_godot_project(self, root):
        game = root / "game"
        game.mkdir()
        (game / "project.godot").write_text(
            '[application]\n\nconfig/name="Ash & Ember"\n', encoding="utf-8")
        assert playtest.game_window_hints(root) == ["Ash & Ember"]

    def test_no_godot_project_means_no_hints(self, root):
        assert playtest.game_window_hints(root) == []

    def test_windows_endpoint(self, client, root, monkeypatch):
        from bgate_adapters import recorder

        game = root / "game"
        game.mkdir()
        (game / "project.godot").write_text(
            '[application]\n\nconfig/name="Test Game"\n', encoding="utf-8")
        monkeypatch.setattr(recorder.sys, "platform", "win32")
        monkeypatch.setattr(recorder, "list_windows", lambda *a, **k: [
            {"pid": 2, "process": "godot", "title": "Test Game (DEBUG)"}])
        body = client.get("/api/playtest/windows").json()
        assert body["ok"] is True
        assert body["data"]["windows"][0]["title"] == "Test Game (DEBUG)"
        assert body["data"]["suggested"] == "Test Game (DEBUG)"


class TestLiveLevel:
    def test_level_reports_silence_with_a_duration(self, monkeypatch):
        """Twenty minutes of digital silence must be visible in second one."""
        import time as _time

        from bgate_adapters import recorder

        rec = recorder.Recording(out_dir=None)
        rec.started_at = rec.audio_started_at = _time.time() - 30
        rec._last_signal_at = rec.audio_started_at
        rec._peaks.extend([0.0] * 8)
        rec._rmss.extend([0.0] * 8)
        rec._samples = recorder.MIC_RATE * 30

        got = recorder.level(rec)
        assert got["signal"] is False
        assert got["captured_s"] == 30.0
        assert got["silent_for_s"] >= 29
        assert "digital silence" in got["warning"]

    def test_level_reports_a_live_mic(self):
        import time as _time

        from bgate_adapters import recorder

        rec = recorder.Recording(out_dir=None)
        rec.started_at = rec.audio_started_at = _time.time() - 2
        rec._last_signal_at = _time.time()
        rec._peaks.extend([0.31, 0.28])
        rec._rmss.extend([0.05, 0.04])
        rec._samples = recorder.MIC_RATE * 2

        got = recorder.level(rec)
        assert got["signal"] is True
        assert got["peak"] == 0.31
        assert got["silent_for_s"] < 1
        assert "warning" not in got

    def test_level_endpoint_when_nothing_is_recording(self, client):
        body = client.get("/api/playtest/level").json()
        assert body["ok"] is True
        assert body["data"]["recording"] is False
        assert "no session" in body["data"]["reason"]

    def test_level_endpoint_reports_a_dead_recorder_rather_than_lying(
            self, client, root, session):
        with db.tx(root) as conn:
            conn.execute("UPDATE playtest_session SET status = 'recording' "
                         "WHERE id = ?", (session,))
        body = client.get("/api/playtest/level").json()["data"]
        assert body["recording"] is False
        assert "server restarted" in body["reason"]

    def test_level_endpoint_reads_the_live_recorder(self, client, root, session):
        import time as _time

        from bgate_adapters import recorder

        with db.tx(root) as conn:
            conn.execute("UPDATE playtest_session SET status = 'recording' "
                         "WHERE id = ?", (session,))
        rec = recorder.Recording(out_dir=None)
        rec.started_at = rec.audio_started_at = _time.time() - 5
        rec._last_signal_at = _time.time()
        rec._peaks.append(0.2)
        rec._rmss.append(0.03)
        rec._samples = recorder.MIC_RATE * 5
        playtest._LIVE[session] = rec
        try:
            body = client.get("/api/playtest/level").json()["data"]
            assert body["recording"] is True
            assert body["session_id"] == session
            assert body["signal"] is True
        finally:
            playtest._LIVE.pop(session, None)
