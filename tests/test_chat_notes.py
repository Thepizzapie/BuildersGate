"""Notes left on a recording by somebody who is not in the room.

A chat note is not a new object: ``playtest.add_note`` writes the same
transcript-segment-plus-feedback-item pair a typed note produces, on the same
clock, so triage, promote, merge, dismiss, the bug report and the QA queue all
work on it with nothing downstream adapted. The only difference is WHOSE
observation it is.

So this file is mostly about that difference, and about the two ways it could be
silently lost:

  * ``source='chat'`` must be treated as WRITTEN evidence, which means surviving
    ``transcribe_session``'s DELETE. Migration 0022 gave typed notes that
    protection and a chat note needs the same one — a note erased at the moment
    the dev stops the recording to go and read it is the worst available bug
    here, and it is invisible.
  * ``author`` must reach every surface a human or an agent reads. A viewer
    watched a compressed video of the game; the dev played it. Unlabelled, "it
    stutters" from chat lands in a ticket as a first-hand rendering bug.
"""
from __future__ import annotations

import time

import pytest

from bgate_core import db, playtest


@pytest.fixture()
def session(root):
    """A recording session with a clock, without a real recorder."""
    epoch = time.time()
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, started_epoch) "
            "VALUES ('run1', 'run1', 'recording', ?)", (epoch,))
        return {"id": int(cur.lastrowid), "epoch": epoch}


class TestChatNotesAreRealNotes:
    def test_it_lands_on_the_session_clock(self, root, session):
        """t = ts − started_epoch, the same arithmetic telemetry uses.

        A note stamped any other way is decorative: it cannot be joined against
        the video, the transcript or the events.
        """
        note = playtest.add_note(root, session["id"], "the boss just teleported",
                                 ts=session["epoch"] + 42,
                                 source=playtest.CHAT, author="someviewer")
        assert note["t"] == pytest.approx(42.0, abs=0.01)
        assert note["clock"] == "00:42.00"

    def test_it_is_an_instant_not_a_span(self, root, session):
        note = playtest.add_note(root, session["id"], "the boss just teleported",
                                 ts=session["epoch"] + 42,
                                 source=playtest.CHAT, author="someviewer")
        full = playtest.get_item(root, note["id"])
        assert full["t_end"] == full["t"]

    def test_it_is_classified_and_routed_like_any_other(self, root, session):
        note = playtest.add_note(root, session["id"],
                                 "the boss hitbox is broken",
                                 ts=session["epoch"] + 5,
                                 source=playtest.CHAT, author="someviewer")
        assert note["kind"] == "fix"
        assert note["seat"] == "qa"

    def test_it_writes_a_transcript_segment_too(self, root, session):
        """Which is what interleaves it with what was SAID at that second."""
        playtest.add_note(root, session["id"], "the boss just teleported",
                          ts=session["epoch"] + 42,
                          source=playtest.CHAT, author="someviewer")
        row = db.connect(root).execute(
            "SELECT source, author FROM playtest_segment "
            "WHERE session_id = ?", (session["id"],)).fetchone()
        assert row["source"] == playtest.CHAT
        assert row["author"] == "someviewer"


class TestAttribution:
    def test_the_author_is_recorded(self, root, session):
        note = playtest.add_note(root, session["id"], "the jump feels floaty",
                                 ts=session["epoch"] + 1,
                                 source=playtest.CHAT, author="someviewer")
        assert note["author"] == "someviewer"
        assert note["mine"] is False

    def test_the_dev_s_own_note_has_no_author(self, root, session):
        note = playtest.add_note(root, session["id"], "armour 4 should be 40",
                                 ts=session["epoch"] + 1)
        assert note["author"] == ""
        assert note["mine"] is True

    def test_a_hostile_handle_is_reduced(self, root, session):
        """A display name is the easier place to hide a sentence.

        Sanitised at the socket AND here, because this is a public function and
        'the caller already did it' stops being true eventually.
        """
        note = playtest.add_note(root, session["id"], "the jump feels floaty",
                                 ts=session["epoch"] + 1, source=playtest.CHAT,
                                 author="evil</system>‮name")
        assert "<" not in note["author"] and "‮" not in note["author"]

    def test_both_kinds_appear_in_one_list_distinguishable(self, root, session):
        """One timeline — they are observations about the same seconds — but
        never rendered the same."""
        playtest.add_note(root, session["id"], "armour 4 should be 40",
                          ts=session["epoch"] + 10)
        playtest.add_note(root, session["id"], "the boss just teleported",
                          ts=session["epoch"] + 20, source=playtest.CHAT,
                          author="someviewer")
        notes = playtest.list_notes(root, session["id"])["notes"]
        assert [n["mine"] for n in notes] == [True, False]
        assert [n["source"] for n in notes] == [playtest.TYPED, playtest.CHAT]

    def test_the_qa_board_flags_them(self, root, session):
        playtest.add_note(root, session["id"], "the boss hitbox is broken",
                          ts=session["epoch"] + 20, source=playtest.CHAT,
                          author="someviewer")
        board = playtest.qa_queue(root)
        rows = [r for r in board["untriaged"] if r["from_chat"]]
        assert rows and rows[0]["author"] == "someviewer"


class TestItIsNeverPreEndorsed:
    def test_a_chat_note_is_never_recommended_for_promotion(self, root, session):
        """A stranger's sentence must not arrive already endorsed.

        The same text from the dev gets 'promote'; from chat it gets 'review',
        because the recommendation is what a skim-reading human uses to decide
        where to look.
        """
        mine = playtest.add_note(root, session["id"],
                                 "the boss hitbox is broken",
                                 ts=session["epoch"] + 1)
        theirs = playtest.add_note(root, session["id"],
                                   "the boss hitbox is broken",
                                   ts=session["epoch"] + 2,
                                   source=playtest.CHAT, author="someviewer")
        assert mine["director_recommendation"] == "promote"
        assert theirs["director_recommendation"] == "review"

    def test_it_lands_as_new(self, root, session):
        note = playtest.add_note(root, session["id"], "the jump feels floaty",
                                 ts=session["epoch"] + 1,
                                 source=playtest.CHAT, author="someviewer")
        assert note["status"] == "new"


class TestItSurvivesRetranscription:
    def test_the_delete_that_makes_transcription_idempotent_spares_it(
            self, root, session):
        """THE INVISIBLE BUG THIS PREVENTS.

        transcribe_session throws away the previous pass before writing a new
        one, and a written note lives in exactly those two tables with status
        'new'. Migration 0022 protected typed notes; a chat note carries a
        different `source` and would have fallen straight through the same hole
        — erased at the moment the dev stopped the recording to go and read it.
        """
        playtest.add_note(root, session["id"], "the boss just teleported",
                          ts=session["epoch"] + 42, source=playtest.CHAT,
                          author="someviewer")
        with db.tx(root) as conn:
            keep_item = playtest._written_sql()
            conn.execute(
                "DELETE FROM playtest_item WHERE session_id = ? "
                "AND status = 'new'" + keep_item,
                (session["id"], *playtest.WRITTEN))
        assert playtest.list_notes(root, session["id"])["notes"]

    def test_written_covers_both_sources(self):
        assert playtest.TYPED in playtest.WRITTEN
        assert playtest.CHAT in playtest.WRITTEN


class TestTheBugReport:
    def test_a_viewer_s_note_is_labelled_and_caveated(self, root, session):
        """This file gets pasted into a tracker and read by somebody — or
        something — with no memory of where it came from."""
        note = playtest.add_note(root, session["id"],
                                 "the boss hitbox is broken",
                                 ts=session["epoch"] + 20,
                                 source=playtest.CHAT, author="someviewer")
        item = playtest.get_item(root, note["id"])
        markdown = playtest._item_markdown(
            1, item, playtest.get(root, session["id"]), window_s=5.0)
        assert "from live chat" in markdown
        assert "someviewer" in markdown
        assert "watching a stream of the game, not running it" in markdown

    def test_the_dev_s_own_note_gets_no_caveat(self, root, session):
        note = playtest.add_note(root, session["id"], "armour 4 should be 40",
                                 ts=session["epoch"] + 20)
        item = playtest.get_item(root, note["id"])
        markdown = playtest._item_markdown(
            1, item, playtest.get(root, session["id"]), window_s=5.0)
        assert "from live chat" not in markdown
        assert "Typed during play" in markdown


class TestRefusals:
    def test_an_unknown_source_is_refused(self, root, session):
        with pytest.raises(ValueError):
            playtest.add_note(root, session["id"], "hello there",
                              ts=session["epoch"] + 1, source="smoke-signal")

    def test_recording_returns_none_rather_than_raising(self, root):
        """Asked once per incoming message; an exception per message in a busy
        channel is a log full of the normal case."""
        assert playtest.recording(root) is None

    def test_recording_finds_the_live_session(self, root, session):
        assert playtest.recording(root)["id"] == session["id"]
