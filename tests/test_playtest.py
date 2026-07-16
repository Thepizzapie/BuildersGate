"""Playtest lifecycle — DB, clock alignment, telemetry join, promotion.

The recorder is faked here (no mic/ffmpeg in CI), but everything that carries
real risk is exercised for real: the audio-offset correction, the telemetry
join window, and the promotion gate. See test_transcribe.py for real whisper.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core import db, playtest


@pytest.fixture()
def session(root):
    """A finished session row, without touching hardware."""
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, duration_s, "
            "telemetry_path, frames_dir) VALUES ('Run 1', 'run-1', 'ready', 60.0, ?, ?)",
            (str(root / "tel.jsonl"), str(root / "frames")),
        )
        return int(cur.lastrowid)


class TestLifecycle:
    def test_only_one_session_records_at_a_time(self, root, monkeypatch):
        """Two ffmpeg captures would fight over the same window."""
        with db.tx(root) as conn:
            conn.execute("INSERT INTO playtest_session (name, slug, status) "
                         "VALUES ('live', 'live', 'recording')")
        with pytest.raises(RuntimeError, match="already recording"):
            playtest.start(root, "second")

    def test_failed_start_marks_session_failed_not_orphaned(self, root, monkeypatch):
        from bgate_adapters import recorder

        def boom(*a, **k):
            raise recorder.RecorderError("mic preflight failed: silent")

        monkeypatch.setattr(recorder, "start", boom)
        with pytest.raises(recorder.RecorderError):
            playtest.start(root, "doomed")

        rows = playtest.list_sessions(root)
        assert rows[0]["status"] == "failed"
        assert "silent" in rows[0]["error"]

    def test_stop_without_live_recorder_fails_loudly(self, root, session):
        """Server restarted mid-session: say so, don't silently produce nothing."""
        with db.tx(root) as conn:
            conn.execute("UPDATE playtest_session SET status = 'recording' WHERE id = ?",
                         (session,))
        with pytest.raises(RuntimeError, match="no live recorder"):
            playtest.stop(root, session)
        assert playtest.get(root, session)["status"] == "failed"

    def test_stop_with_no_recording_session(self, root):
        with pytest.raises(LookupError, match="no session is currently recording"):
            playtest.stop(root)


class TestClockAlignment:
    def test_whisper_timestamps_are_shifted_onto_the_session_clock(
            self, root, session, monkeypatch):
        """The mic starts a beat after the session; correct once, on ingest.

        Without this, every frame is grabbed from the wrong moment and telemetry
        joins to the wrong events — a silent, pervasive misalignment.
        """
        from bgate_adapters import transcribe

        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "language": "en",
            "segments": [{"t_start": 10.0, "t_end": 12.0,
                          "text": "the jump feels really floaty", "confidence": -0.2}],
        })
        with db.tx(root) as conn:
            conn.execute("UPDATE playtest_session SET audio_path = ? WHERE id = ?",
                         (str(root / "a.wav"), session))

        playtest.transcribe_session(root, session, audio_offset_s=1.5)

        conn = db.connect(root)
        seg = conn.execute("SELECT * FROM playtest_segment WHERE session_id = ?",
                           (session,)).fetchone()
        assert seg["t_start"] == 11.5  # 10.0 + 1.5
        item = conn.execute("SELECT * FROM playtest_item WHERE session_id = ?",
                            (session,)).fetchone()
        assert item["t"] == 11.5
        assert item["kind"] == "fix"


class TestTelemetry:
    def test_ingests_jsonl_and_skips_bad_lines(self, root, session):
        Path(root / "tel.jsonl").write_text(
            '{"t": 1.0, "kind": "jump", "data": {"air_time": 0.9}}\n'
            "not json at all\n"
            '{"t": 2.0, "kind": "death"}\n'
            "\n"
            '{"missing": "t"}\n',
            encoding="utf-8")
        got = playtest.ingest_telemetry(root, session)
        assert got["ingested"] == 2
        assert got["skipped"] == 2

    def test_missing_telemetry_is_not_an_error(self, root, session):
        got = playtest.ingest_telemetry(root, session)
        assert got["ingested"] == 0
        assert "emitted nothing" in got["note"]

    def test_reingest_replaces_rather_than_duplicates(self, root, session):
        Path(root / "tel.jsonl").write_text('{"t": 1.0, "kind": "jump"}\n', encoding="utf-8")
        playtest.ingest_telemetry(root, session)
        playtest.ingest_telemetry(root, session)
        count = db.connect(root).execute(
            "SELECT count(*) FROM playtest_event WHERE session_id = ?", (session,)).fetchone()[0]
        assert count == 1


class TestBrief:
    @pytest.fixture()
    def loaded(self, root, session):
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat) "
                "VALUES (?, 12.5, 'fix', 'the jump feels floaty', 'gameplay')", (session,))
            for t, kind, data in [
                (2.0, "jump", '{"air_time": 0.4}'),      # far away — excluded
                (11.8, "jump", '{"air_time": 0.92}'),    # in window — the smoking gun
                (13.0, "land", '{"impact": 3.1}'),       # in window
                (40.0, "death", "{}"),                   # far away — excluded
            ]:
                conn.execute("INSERT INTO playtest_event (session_id, t, kind, data) "
                             "VALUES (?, ?, ?, ?)", (session, t, kind, data))
        return session

    def test_joins_telemetry_to_the_moment_of_feedback(self, root, loaded):
        """This join is the entire point: a vibe becomes a number."""
        brief = playtest.brief(root, loaded, window_s=4.0)
        item = brief["items"][0]
        kinds = [e["kind"] for e in item["events"]]
        assert kinds == ["jump", "land"]
        assert item["events"][0]["data"]["air_time"] == 0.92

    def test_window_bounds_are_respected(self, root, loaded):
        assert playtest.brief(root, loaded, window_s=0.1)["items"][0]["events"] == []

    def test_transcript_is_opt_in(self, root, loaded):
        assert "transcript" not in playtest.brief(root, loaded)
        assert "transcript" in playtest.brief(root, loaded, include_transcript=True)

    def test_brief_warns_agents_they_cannot_watch_video(self, root, loaded):
        assert "cannot watch" in playtest.brief(root, loaded)["note"]


class TestPromotion:
    @pytest.fixture()
    def item(self, root, session):
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_item (session_id, t, kind, text, seat) "
                "VALUES (?, 5.0, 'fix', 'jump is floaty', 'gameplay')", (session,))
            return int(cur.lastrowid)

    def test_items_start_as_new(self, root, item):
        assert db.connect(root).execute(
            "SELECT status FROM playtest_item WHERE id = ?", (item,)).fetchone()[0] == "new"

    def test_promote_marks_and_can_reroute(self, root, item):
        got = playtest.promote(root, item, seat="tech", ref="ORBI-1")
        assert got["status"] == "promoted"
        assert got["seat"] == "tech"
        assert got["promoted_ref"] == "ORBI-1"

    def test_promote_rejects_unknown_seat(self, root, item):
        with pytest.raises(ValueError, match="seat"):
            playtest.promote(root, item, seat="wizard")

    def test_dismiss(self, root, item):
        assert playtest.dismiss(root, item)["status"] == "dismissed"

    def test_promote_missing_item(self, root):
        with pytest.raises(LookupError):
            playtest.promote(root, 999)


class TestContract:
    def test_telemetry_contract_is_self_explaining(self):
        contract = playtest.telemetry_contract()
        assert "t" in contract["required"] and "kind" in contract["required"]
        json.loads(contract["example"])  # the example must actually parse
