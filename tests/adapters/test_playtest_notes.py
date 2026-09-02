"""The playtest notepad — typed evidence on the recorder's clock.

The feature's whole value rests on one property: a note has to land on the SAME
axis as the transcript, the frames and the telemetry (seconds from session
start). If it does not, the note is decorative — it floats next to a video it
does not line up with. So the first test here is the alignment one, and it is
written against a spoken segment rather than against a constant, because
matching a constant would still pass if both sides were wrong.

The second thing under test is survival. transcribe_session clears the last
transcription pass before writing a new one, and a typed note lives in exactly
the two tables it clears — so stopping the session, the next thing anyone does
after taking notes, is what would have destroyed them.
"""
from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest

from bgate_core.store import db
from bgate_core.qa import playtest

# The smallest thing that is really a PNG: 1x1, so the decode path, the suffix
# mapping and the write are all exercised without a fixture file on disk.
def _png_bytes() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()


@pytest.fixture()
def live(root, tmp_path):
    """A session that is recording, with a real clock origin and a frames dir."""
    frames = tmp_path / "frames"
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, started_epoch, "
            "frames_dir, audio_path) VALUES ('Run 1', 'run-1', 'recording', "
            "1000.0, ?, ?)", (str(frames), str(root / "a.wav")))
        return int(cur.lastrowid)


class TestTheSharedClock:
    def test_a_note_lands_on_the_same_axis_as_a_transcript_segment(
            self, root, live, monkeypatch):
        """The join that makes the whole feature worth having.

        A remark SPOKEN 12.5s into the session and a note TYPED 12.5s into the
        session must end up at the same t. They arrive by completely different
        routes — whisper's wav-relative stamp plus audio_offset_s for one, a
        browser wall clock minus started_epoch for the other — and this is the
        only place those two routes are checked against each other.
        """
        from bgate_adapters import transcribe

        # Spoken: the mic stream began 1.5s after the session, and whisper
        # reports 11.0s into the WAV. 11.0 + 1.5 = 12.5 on the session clock.
        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 11.0, "t_end": 13.0,
                                      "text": "the jump feels really floaty"}]})

        # Typed: started_epoch is 1000.0, and the browser stamped the note at
        # wall-clock 1012.5.
        note = playtest.add_note(root, live, "the jump feels really floaty",
                                 ts=1012.5)
        playtest.transcribe_session(root, live, audio_offset_s=1.5)

        spoken = db.connect(root).execute(
            "SELECT t FROM playtest_item WHERE session_id = ? AND source <> 'typed'",
            (live,)).fetchone()
        assert note["t"] == 12.5
        assert spoken["t"] == 12.5

    def test_no_started_epoch_refuses_rather_than_stamping_zero(self, root):
        """A note at 0.0 would sit on the first second of the recording forever
        and look exactly like a real one."""
        with db.tx(root) as conn:
            cur = conn.execute(
                "INSERT INTO playtest_session (name, slug, status) "
                "VALUES ('old', 'old', 'ready')")
            old = int(cur.lastrowid)
        with pytest.raises(ValueError, match="no session clock"):
            playtest.add_note(root, old, "something happened")

    def test_an_explicit_t_is_taken_as_session_seconds(self, root, live):
        """What the review UI sends when you type against the video playhead."""
        note = playtest.add_note(root, live, "boss hitbox is wrong here", t=42.25)
        assert note["t"] == 42.25
        assert note["clock"] == "00:42.25"

    def test_a_note_is_an_instant_not_a_span(self, root, live):
        """t_end == t on purpose: brief() joins telemetry over [t, t_end], and
        stretching that to the moment you stopped typing would drag in events
        from well after the thing the note is about."""
        note = playtest.add_note(root, live, "the door does not open", ts=1005.0)
        assert note["t_end"] == note["t"]


class TestItWritesTheTranscriptToo:
    def test_a_note_appears_in_the_transcript_marked_as_typed(self, root, live):
        playtest.add_note(root, live, "armour 4 should be 40", ts=1009.0)
        brief = playtest.brief(root, live, include_transcript=True)
        typed = [line for line in brief["transcript"] if line["source"] == "typed"]
        assert len(typed) == 1
        assert typed[0]["text"] == "armour 4 should be 40"
        assert typed[0]["t_start"] == 9.0
        # confidence is whisper's certainty about what it HEARD. There is
        # nothing uncertain about text somebody typed.
        assert typed[0]["confidence"] is None

    def test_the_item_is_classified_and_routed_like_speech(self, root, live):
        note = playtest.add_note(root, live, "the enemies are way too fast", ts=1003.0)
        assert note["kind"] == "change"
        assert note["seat"] == "gameplay"

    def test_kind_and_seat_can_be_overridden(self, root, live):
        note = playtest.add_note(root, live, "look at this", ts=1003.0,
                                 kind="fix", seat="art")
        assert (note["kind"], note["seat"]) == ("fix", "art")

    def test_a_bad_seat_is_refused(self, root, live):
        with pytest.raises(ValueError, match="seat must be one of"):
            playtest.add_note(root, live, "x y z", ts=1003.0, seat="marketing")

    def test_an_empty_note_is_refused(self, root, live):
        with pytest.raises(ValueError, match="cannot be empty"):
            playtest.add_note(root, live, "   \n  ", ts=1003.0)


class TestSurvivingTranscription:
    def test_stopping_the_session_does_not_erase_the_notes(
            self, root, live, monkeypatch):
        """THE bug this feature would have shipped with.

        transcribe_session clears the previous pass with `DELETE FROM
        playtest_segment WHERE session_id = ?` and `DELETE FROM playtest_item
        WHERE session_id = ? AND status = 'new'`. A typed note is a row in both
        tables and is 'new' by definition, so without the source guard every
        note taken during a session was destroyed by the act of ending it.
        """
        from bgate_adapters import transcribe

        note = playtest.add_note(root, live, "armour 4 should be 40", ts=1009.0)
        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 20.0, "t_end": 22.0,
                                      "text": "this bit is really good fun"}]})
        playtest.transcribe_session(root, live)

        survivors = db.connect(root).execute(
            "SELECT text FROM playtest_item WHERE id = ?", (note["id"],)).fetchone()
        assert survivors is not None, "the note was deleted by transcription"
        assert survivors["text"] == "armour 4 should be 40"
        segment = db.connect(root).execute(
            "SELECT text FROM playtest_segment WHERE session_id = ? AND source = 'typed'",
            (live,)).fetchone()
        assert segment["text"] == "armour 4 should be 40"

    def test_re_transcribing_twice_does_not_duplicate_a_note(
            self, root, live, monkeypatch):
        from bgate_adapters import transcribe

        playtest.add_note(root, live, "armour 4 should be 40", ts=1009.0)
        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 20.0, "t_end": 22.0,
                                      "text": "this bit is really good fun"}]})
        playtest.transcribe_session(root, live)
        playtest.transcribe_session(root, live)
        count = db.connect(root).execute(
            "SELECT count(*) FROM playtest_item WHERE session_id = ? AND source = 'typed'",
            (live,)).fetchone()[0]
        assert count == 1

    def test_a_typed_note_does_not_widen_a_spoken_item_span(
            self, root, live, monkeypatch):
        """_item_spans regroups segments to rebuild a thought's end time. A note
        dropped in the middle of someone talking is not part of what they were
        saying, and fusing it would point the telemetry join at a window the
        complaint never covered."""
        from bgate_adapters import transcribe

        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 5.0, "t_end": 6.0,
                                      "text": "the jump feels really floaty"}]})
        playtest.transcribe_session(root, live)
        # Typed 0.2s after the speaker stopped — inside group_thoughts' 1.0s gap.
        playtest.add_note(root, live, "and the landing is mushy too", ts=1006.2)

        conn = db.connect(root)
        spoken = conn.execute(
            "SELECT id FROM playtest_item WHERE session_id = ? AND source <> 'typed'",
            (live,)).fetchone()
        # Zero the stored t_end so the span has to be RECONSTRUCTED — which is
        # the path that reads segments and could otherwise swallow the note.
        with db.tx(root) as tx:
            tx.execute("UPDATE playtest_item SET t_end = 0 WHERE id = ?",
                       (int(spoken["id"]),))
        spans = playtest._item_spans(db.connect(root), live)
        assert spans[int(spoken["id"])] == 6.0


class TestFrameAttachment:
    def test_a_captured_frame_becomes_a_real_file_under_the_session(
            self, root, live):
        note = playtest.add_note(root, live, "this sprite is wrong", ts=1004.0,
                                 frame=PNG_DATA_URL)
        path = Path(note["frame_path"])
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG")
        # It has to live where the session's other stills do, or the report zip
        # and /api/preview cannot reach it.
        frames = Path(playtest.get(root, live)["frames_dir"]).resolve()
        assert path.resolve().parent == frames

    def test_the_frame_name_comes_from_us_and_not_from_the_caller(
            self, root, live):
        """Nothing the browser sends reaches the filename. The only caller-
        controlled part of the write is the image bytes."""
        note = playtest.add_note(root, live, "a", ts=1004.0, frame=PNG_DATA_URL)
        assert Path(note["frame_path"]).name == f"note{note['id']:05d}.png"

    def test_the_written_path_never_leaves_the_frames_directory(
            self, root, live, tmp_path):
        """The invariant the guard in _write_note_frame protects.

        Nothing the browser sends is interpolated into the filename today, so
        the guard cannot be tripped from outside — which is exactly why it is
        asserted here rather than trusted: the day someone puts a caller-
        supplied label in that format string, this fails instead of quietly
        turning the dashboard into a file writer."""
        frames = tmp_path / "frames"
        for item_id in (1, 42, 99999):
            written = Path(playtest._write_note_frame(frames, item_id, PNG_DATA_URL))
            assert written.resolve().parent == frames.resolve()

    def test_a_frame_escaping_its_directory_is_refused(self, root, tmp_path,
                                                       monkeypatch):
        """And the guard itself fires when the constructed name WOULD escape."""
        monkeypatch.setitem(playtest._NOTE_FRAME_SUFFIX, "png", "/../../loose.png")
        with pytest.raises(ValueError, match="escapes"):
            playtest._write_note_frame(tmp_path / "frames", 7, PNG_DATA_URL)

    def test_a_non_image_payload_is_refused(self, root, live):
        with pytest.raises(ValueError, match="base64 data URL"):
            playtest._write_note_frame(Path(root), 1, "data:text/html;base64,PGI+")

    def test_an_oversized_frame_is_refused(self, root, live, monkeypatch):
        monkeypatch.setattr(playtest, "NOTE_FRAME_MAX_BYTES", 4)
        with pytest.raises(ValueError, match="the limit is"):
            playtest._write_note_frame(Path(root), 1, PNG_DATA_URL)

    def test_a_rejected_frame_keeps_the_words(self, root, live):
        """The note is the evidence; the picture is a bonus. Nobody can retype
        from memory what they watched happen a minute ago."""
        note = playtest.add_note(root, live, "the boss T-posed on death",
                                 ts=1004.0, frame="data:image/png;base64,!!!!")
        assert note["text"] == "the boss T-posed on death"
        assert not note["frame_path"]
        assert "base64" in note["frame_error"]

    def test_a_frameless_note_is_backfilled_from_the_video_at_stop(
            self, root, live, monkeypatch):
        """A note taken against a NATIVE Godot window has no canvas to grab, so
        it is saved bare and filled in when the mp4 becomes seekable."""
        from bgate_adapters import recorder, transcribe

        video = Path(root) / "session.mp4"
        video.write_bytes(b"not really an mp4")
        with db.tx(root) as conn:
            conn.execute("UPDATE playtest_session SET video_path = ?, "
                         "video_offset_s = 0.75 WHERE id = ?", (str(video), live))
        note = playtest.add_note(root, live, "the lift never arrives", ts=1010.0)
        assert not note["frame_path"]

        seeks = []

        def fake_extract(_video, t, out):
            seeks.append(t)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"jpeg")
            return {"ok": True, "t": t, "path": out}

        monkeypatch.setattr(recorder, "extract_frame", fake_extract)
        monkeypatch.setattr(transcribe, "transcribe",
                            lambda *a, **k: {"ok": True, "segments": []})
        playtest.transcribe_session(root, live)

        filled = playtest.get_item(root, note["id"])
        assert Path(filled["frame_path"]).is_file()
        # Sought on VIDEO time — the note's session t minus the capture offset.
        assert seeks == [pytest.approx(9.25)]


class TestItIsAnOrdinaryFeedbackItem:
    def test_a_note_can_be_promoted_and_dismissed(self, root, live):
        note = playtest.add_note(root, live, "the shop UI overlaps the map",
                                 ts=1007.0)
        promoted = playtest.promote(root, note["id"], seat="art", kind="fix")
        assert promoted["status"] == "promoted"
        assert promoted["seat"] == "art"
        dismissed = playtest.dismiss(root, note["id"])
        assert dismissed["status"] == "dismissed"

    def test_a_note_can_be_merged_into_a_spoken_item(self, root, live, monkeypatch):
        from bgate_adapters import transcribe

        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 5.0, "t_end": 7.0,
                                      "text": "the jump feels really floaty"}]})
        playtest.transcribe_session(root, live)
        spoken = db.connect(root).execute(
            "SELECT id FROM playtest_item WHERE session_id = ? AND source <> 'typed'",
            (live,)).fetchone()
        note = playtest.add_note(root, live, "jump again — still floaty", ts=1030.0)

        merged = playtest.merge(root, note["id"], int(spoken["id"]))
        assert merged["merged_into_id"] == int(spoken["id"])
        assert playtest.unmerge(root, note["id"])["status"] == "new"

    def test_a_promoted_note_reaches_the_bug_report_marked_as_typed(
            self, root, live):
        note = playtest.add_note(root, live, "the crusher hitbox kills through walls",
                                 ts=1015.0, frame=PNG_DATA_URL)
        playtest.promote(root, note["id"], seat="qa", kind="fix")

        report = playtest.report(root, live)
        assert note["id"] in report["items"]
        assert "the crusher hitbox kills through walls" in report["markdown"]
        assert "**typed note**" in report["markdown"]
        # Not "Said during play" — the report must not claim a recording exists
        # that says these words.
        assert "Typed during play (verbatim)" in report["markdown"]
        assert "00:15.00" in report["markdown"]
        # The frame travels with it, so the zip has something to attach.
        assert [f["item_id"] for f in report["frames"]] == [note["id"]]

    def test_notes_show_up_in_the_qa_queue_flagged(self, root, live):
        playtest.add_note(root, live, "the save prompt fires twice", ts=1020.0)
        queue = playtest.qa_queue(root)
        typed = [i for i in queue["untriaged"] if i["typed"]]
        assert len(typed) == 1
        assert typed[0]["clock"] == "00:20.00"

    def test_list_notes_returns_only_typed_ones_in_clock_order(
            self, root, live, monkeypatch):
        from bgate_adapters import transcribe

        monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: {
            "ok": True, "segments": [{"t_start": 4.0, "t_end": 6.0,
                                      "text": "the jump feels really floaty"}]})
        playtest.transcribe_session(root, live)
        playtest.add_note(root, live, "second thing typed", ts=1030.0)
        playtest.add_note(root, live, "first thing typed", ts=1010.0)

        got = playtest.list_notes(root, live)
        assert [n["text"] for n in got["notes"]] == ["first thing typed",
                                                     "second thing typed"]
