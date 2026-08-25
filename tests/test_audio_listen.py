"""In-game listening: the audio check that file metrics cannot make."""
from __future__ import annotations

import pytest

from bgate_core import audiohooks


def _wire(root):
    """One cue the game asks for and a file that answers it."""
    audio = root / "game" / "assets" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "sfx_melee_hit.wav").write_bytes(b"RIFF0000WAVE")
    scripts = root / "game" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "combat.gd").write_text(
        'func hit():\n\tAudio.sfx("melee_hit")\n', encoding="utf-8")


def _capture(root, name="capture.mp4"):
    path = root / name
    path.write_bytes(b"not really a video")
    return name


class TestListenRecord:
    def test_a_capture_that_does_not_exist(self, root):
        with pytest.raises(ValueError, match="not a file under this project"):
            audiohooks.listen_record(
                root, capture="runs/ghost.mp4", cues=["melee_hit"],
                verdict="pass",
                notes="the hit lands under the music but it reads")

    def test_a_png_is_not_a_capture(self, root):
        (root / "shot.png").write_bytes(b"x")
        with pytest.raises(ValueError, match="not a capture"):
            audiohooks.listen_record(
                root, capture="shot.png", cues=["melee_hit"], verdict="pass",
                notes="the hit lands under the music but it reads")

    def test_a_pass_with_no_cues_covers_nothing(self, root):
        capture = _capture(root)
        with pytest.raises(ValueError, match="covers no cue"):
            audiohooks.listen_record(
                root, capture=capture, cues=[], verdict="pass",
                notes="everything sounded fine to me on the whole")

    def test_say_what_you_heard(self, root):
        capture = _capture(root)
        with pytest.raises(ValueError, match="say what you heard"):
            audiohooks.listen_record(
                root, capture=capture, cues=["melee_hit"], verdict="pass",
                notes="fine")

    def test_only_a_passing_listen_counts_as_coverage(self, root):
        capture = _capture(root)
        audiohooks.listen_record(
            root, capture=capture, cues=["melee_hit"], verdict="fail",
            notes="the hit is three frames late and lands after the flash")
        assert audiohooks.listened(root) == set()

    def test_a_passing_listen_covers_its_cues(self, root):
        capture = _capture(root)
        audiohooks.listen_record(
            root, capture=capture, cues=["melee_hit", "door_open"],
            verdict="pass",
            notes="both read clearly over the room tone at combat volume")
        assert audiohooks.listened(root) == {"melee_hit", "door_open"}

    def test_a_new_capture_does_not_erase_the_old_coverage(self, root):
        first = _capture(root, "run1.mp4")
        second = _capture(root, "run2.mp4")
        audiohooks.listen_record(
            root, capture=first, cues=["melee_hit"], verdict="pass",
            notes="the hit reads clearly over the room tone at combat volume")
        audiohooks.listen_record(
            root, capture=second, cues=["door_open"], verdict="pass",
            notes="the door reads clearly from the far side of the corridor")
        assert audiohooks.listened(root) == {"melee_hit", "door_open"}


class TestUnreviewed:
    def test_a_project_with_no_wired_cues_owes_nothing(self, root):
        assert audiohooks.in_game_unreviewed(root) == []

    def test_a_wired_cue_nobody_has_heard(self, root):
        _wire(root)
        rows = audiohooks.in_game_unreviewed(root)
        assert rows and "never been heard" in rows[0]

    def test_hearing_it_clears_the_row(self, root):
        _wire(root)
        found = audiohooks.scan(root)
        names = [e["event"] for e in found["events"] if e["state"] == "wired"]
        assert names, "the fixture did not wire a cue"
        audiohooks.listen_record(
            root, capture=_capture(root), cues=names, verdict="pass",
            notes="the hit reads clearly over the room tone at combat volume")
        assert audiohooks.in_game_unreviewed(root) == []
