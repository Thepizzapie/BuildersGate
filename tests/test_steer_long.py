"""A long correction can now reach a running agent without killing it.

THE DEAD END. A steer is capped at 2000 characters and a longer one was REFUSED
outright. Refusing rather than truncating is the right call and it stays —
handing an agent half a sentence with no way to know it was cut is worse than
either option. But the cap left no route at all for a genuinely long correction
to reach a RUNNING agent: kill the run and re-pay for it, or watch it carry on
doing the wrong thing.

The fix is the discipline `ask_human` already asks of its callers — cite, do not
paste. Past the cap the text becomes a file and the steer becomes an excerpt plus
a path. What the agent receives is still an interruption: enough to judge whether
to stop now, and a pointer for the rest.

The load-bearing assertion in this file is that what lands in the inbox STILL
FITS THE CAP. A "fix" that posts an over-cap message is the truncation bug
wearing a hat, and it would fail inside the dashboard's delivery rather than here
where someone can see it.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import steerbox

SHORT = "that pose is off-model, use the pinned ref"
LONG = ("Stop and re-read the brief. " + ("x" * 40 + " ") * 120).strip()


def _inbox(root):
    return sorted(steerbox.box(root).glob("*.json"))


def _messages(root):
    return [json.loads(p.read_text(encoding="utf-8")) for p in _inbox(root)]


class TestShortIsUnchanged:
    def test_it_posts_verbatim(self, root):
        steerbox.post_long(root, 7, SHORT, by="director")
        assert _messages(root)[0]["text"] == SHORT

    def test_no_file_is_written_for_a_short_steer(self, root):
        out = steerbox.post_long(root, 7, SHORT, by="director")
        assert out["excerpted"] is False
        assert out["note_path"] == ""
        assert not steerbox.notes_dir(root).exists()

    def test_an_empty_steer_is_still_refused(self, root):
        with pytest.raises(ValueError):
            steerbox.post_long(root, 7, "   ", by="director")


class TestLongIsCited:
    @pytest.fixture()
    def posted(self, root):
        assert len(LONG) > steerbox.MAX_TEXT      # the control: it really is long
        return steerbox.post_long(root, 7, LONG, by="director")

    def test_what_lands_in_the_inbox_fits_the_cap(self, root, posted):
        """THE ONE THAT MATTERS. An over-cap message would fail at delivery
        instead of here, inside the dashboard, out of sight."""
        assert len(_messages(root)[0]["text"]) <= steerbox.MAX_TEXT

    def test_the_full_text_is_on_disk_intact(self, root, posted):
        body = open(posted["note_path"], encoding="utf-8").read()
        assert LONG in body

    def test_the_agent_is_told_where_to_read_it(self, root, posted):
        text = _messages(root)[0]["text"]
        assert posted["note_path"] in text
        assert "READ THAT FILE" in text

    def test_the_excerpt_is_the_opening_not_a_summary(self, root, posted):
        """So the agent can judge whether to stop NOW, before opening anything."""
        assert LONG[:200] in _messages(root)[0]["text"]

    def test_it_says_how_much_was_left_out(self, root, posted):
        """Silence about the remainder is how a truncated steer reads."""
        assert "more characters" in _messages(root)[0]["text"]

    def test_the_note_names_the_item_and_the_sender(self, root, posted):
        body = open(posted["note_path"], encoding="utf-8").read()
        assert "#7" in body
        assert "director" in body

    def test_two_long_steers_do_not_collide(self, root):
        a = steerbox.post_long(root, 7, LONG, by="director")
        b = steerbox.post_long(root, 7, LONG + " again", by="director")
        assert a["note_path"] != b["note_path"]


class TestItIsStillAnInterruption:
    def test_the_note_lives_in_the_steer_box_not_the_brief(self, root):
        """A correction that should outlive the run belongs in queue.update. This
        one dies with the process it was aimed at, which is the honest lifetime
        for 'no, not like that' — so it is filed under .bgate/steer, not on the
        work item."""
        out = steerbox.post_long(root, 7, LONG, by="director")
        assert steerbox.box(root) in __import__("pathlib").Path(
            out["note_path"]).parents
