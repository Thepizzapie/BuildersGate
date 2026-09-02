"""The animatic: the stage between a shot list and money.

WHAT IS WORTH PINNING HERE. The module is ffmpeg plumbing, and testing plumbing
line by line mostly restates it. What earns a test is every place the reel could
LIE about the edit, because the entire value of previs is that the numbers it
reports are the numbers you will pay for:

  * a panel's planned duration reaching the actual file, so the reel is the
    scene's real length rather than a slideshow at some default rate
  * a beat with no still producing a slate that HOLDS ITS LENGTH — skipping it
    would hand back a reel that is shorter than the scene and reads as finished,
    which is the one failure that makes previs worse than nothing
  * the reported runtime agreeing with what ffprobe measures on the file, since
    caption timing is derived from the report and inherits any drift
  * a clean, actionable refusal when ffmpeg is absent, because there is no model
    to fall back to here and a stack trace would read as a broken feature rather
    than a missing binary

Nothing here calls a provider, because nothing in the module can.
"""
from __future__ import annotations

import shutil

import pytest

from bgate_core.cine import animatic, cinematic, storyboard

FF = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(
    not FF, reason="an animatic is ffmpeg over PNGs; there is no ffmpeg here")


def still(root, name="a", size=(320, 180), colour=(80, 110, 160)):
    """A REAL png, not a stub with a PNG header.

    The panel renderer opens these with Pillow, so the eight-byte fake other
    storyboard tests use (attach only checks existence) would be reported as an
    unreadable image and silently become a slate — which would make the missing
    frame test pass for the wrong reason.
    """
    from PIL import Image

    out = root / "design" / "cinematics" / "plates"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    Image.new("RGB", size, colour).save(path)
    return f"design/cinematics/plates/{name}.png"


@pytest.fixture()
def board(root):
    """Three beats, two of them drawn. The middle one is the hole."""
    storyboard.plan(root, "Atrium Ambush", [
        {"beat": "Wide on the empty atrium", "camera": "wide", "duration": 2,
         "image_path": still(root, "one")},
        {"beat": "Ledger steps out of the lift", "camera": "medium",
         "duration": 3},
        {"beat": "The lights cut", "camera": "close", "duration": 2,
         "dialogue": "Not again.", "image_path": still(root, "three",
                                                       colour=(30, 30, 40))},
    ], premise="A layoff goes wrong")
    return "atrium-ambush"


@pytest.fixture()
def planned(root):
    """A sequence with stills wired in, which is what promotion produces."""
    # 4s is the shortest shot the default video model will generate, so these
    # are the shortest durations a real planned sequence can hold.
    cinematic.plan(root, "Atrium", [
        {"action": "Wide on the empty atrium", "duration": 4,
         "first_frame": still(root, "seq1")},
        {"action": "Ledger steps out of the lift", "duration": 5,
         "first_frame": still(root, "seq2", colour=(160, 90, 60))},
    ])
    return "atrium"


class TestSource:
    def test_a_sequence_is_preferred_over_a_board_of_the_same_name(
            self, root, board, planned):
        """Auto reads the row money is spent against.

        A board can be edited after promotion; the sequence is what the next
        generate_shot call will buy, so it is what the reel has to describe.
        """
        storyboard.plan(root, "atrium", [{"beat": "something else"}])
        assert animatic.resolve(root, "atrium")["source"] == "sequence"

    def test_an_unpromoted_board_can_still_be_cut(self, root, board):
        out = animatic.resolve(root, board)
        assert out["source"] == "board"
        assert [s["idx"] for s in out["shots"]] == [1, 2, 3]

    def test_a_name_that_is_neither_says_so_rather_than_crashing(self, root):
        with pytest.raises(animatic.AnimaticError, match="nothing named"):
            animatic.resolve(root, "no-such-scene")


class TestDurations:
    @needs_ffmpeg
    def test_panel_durations_survive_into_the_output(self, root, planned):
        """4s + 5s is a 9s reel, on the file, not just in the report.

        The failure this pins is a reel encoded at some default length per
        panel: the edit would look right and its rhythm — the only thing previs
        exists to show — would be fiction.
        """
        out = animatic.build(root, planned)
        assert out["runtime_s"] == pytest.approx(9.0, abs=0.01)
        assert out["measured_s"] == pytest.approx(9.0, abs=0.35)
        assert [s["duration_s"] for s in out["shots"]] == [4.0, 5.0]

    @needs_ffmpeg
    def test_the_reported_runtime_matches_the_actual_file(self, root, board):
        from bgate_core.cine import cinecut

        out = animatic.build(root, board)
        measured = cinecut.duration_of(root / out["path"])
        assert measured == pytest.approx(out["runtime_s"], abs=0.35)
        assert measured == pytest.approx(out["measured_s"], abs=0.01)
        assert not [w for w in out["warnings"] if "measures" in w]

    def test_a_shot_with_no_duration_is_held_and_flagged(self, root):
        """Untimed is not zero-length and not an error — it is the default,
        said out loud. Silently taking 5s is how a 30s scene becomes 50s.

        Both tables default `duration` to 5 in the schema, so this is pinned on
        the normaliser rather than through a board: the path that has to survive
        is a row arriving with nothing usable in that column, from a hand-edited
        database or a future source that does not default it.
        """
        untimed = animatic._normalise(root, {"idx": 1, "duration": None},
                                      still="", label="one")
        zero = animatic._normalise(root, {"idx": 2, "duration": 0},
                                   still="", label="two")
        timed = animatic._normalise(root, {"idx": 3, "duration": 4},
                                    still="", label="three")
        assert untimed["duration"] == animatic.DEFAULT_DURATION_S
        assert untimed["duration_defaulted"] is True
        assert zero["duration_defaulted"] is True
        assert timed["duration_defaulted"] is False

    def test_a_transition_shortens_the_reel_the_way_it_shortens_the_cut(
            self, root):
        """Timing comes from cinecut, so a dissolve overlaps here exactly as it
        will in the finished cut. Two implementations of shot timing would
        disagree the first time somebody changed a handle."""
        cinematic.plan(root, "dissolved", [
            {"action": "one", "duration": 4},
            {"action": "two", "duration": 4, "transition": "dissolve",
             "transition_s": 1.0},
        ])
        shots = animatic.resolve(root, "dissolved")["shots"]
        from bgate_core.cine import cinecut

        assert cinecut.runtime_of(shots) == pytest.approx(7.0, abs=0.01)

    @needs_ffmpeg
    def test_a_planned_transition_is_actually_rendered(self, root):
        """The xfade path, end to end. A reel that quietly hard-cut every join
        would show a rhythm the finished cut is not going to have, and the
        runtime would be a second long per dissolve."""
        cinematic.plan(root, "dissolved", [
            {"action": "one", "duration": 4, "first_frame": still(root, "d1")},
            {"action": "two", "duration": 4, "transition": "dissolve",
             "transition_s": 1.0,
             "first_frame": still(root, "d2", colour=(200, 60, 60))},
        ])
        out = animatic.build(root, "dissolved")
        assert out["transitions"] == ["dissolve"]
        assert out["runtime_s"] == pytest.approx(7.0, abs=0.01)
        assert out["measured_s"] == pytest.approx(7.0, abs=0.35)


class TestMissingFrames:
    @needs_ffmpeg
    def test_a_missing_frame_is_a_slate_not_a_shorter_cut(self, root, board):
        """The one failure that would make previs harmful.

        Skipping the undrawn beat gives a 4s reel of a 7s scene that reads as
        complete. The slate holds its 3s and is counted, so the hole is in the
        edit where somebody can see it.
        """
        out = animatic.build(root, board)
        assert out["placeholder_idx"] == [2]
        assert out["panels"] == 3
        assert out["runtime_s"] == pytest.approx(7.0, abs=0.01)
        assert out["measured_s"] == pytest.approx(7.0, abs=0.35)
        assert [s["placeholder"] for s in out["shots"]] == [False, True, False]

    @needs_ffmpeg
    def test_the_slate_is_named_in_the_warnings(self, root, board):
        out = animatic.build(root, board)
        assert any("slates" in w for w in out["warnings"])

    def test_a_still_whose_file_is_gone_slates_rather_than_raising(
            self, root, board, tmp_path):
        """A path that no longer resolves is the same hole as no path at all —
        a board copied between projects produces a page of them, and refusing
        the whole reel would leave nobody able to look at the scene."""
        (root / "design/cinematics/plates/one.png").unlink()
        shots = animatic.resolve(root, board, source="board")["shots"]
        assert shots[0]["still"] == ""
        assert shots[0]["still_rel"].endswith("one.png")

    @needs_ffmpeg
    def test_a_board_of_one_frame_still_builds(self, root):
        """One panel is a held frame, not an edit, and the report says so
        instead of reporting an average shot length as if it meant something."""
        storyboard.plan(root, "single", [
            {"beat": "the only beat", "duration": 3,
             "image_path": still(root, "solo")}])
        out = animatic.build(root, "single", source="board")
        assert out["panels"] == 1
        assert out["measured_s"] == pytest.approx(3.0, abs=0.35)
        assert any("one panel" in w for w in out["warnings"])


class TestReport:
    @needs_ffmpeg
    def test_it_costs_nothing_and_says_so(self, root, planned):
        assert animatic.build(root, planned)["estimated_usd"] == 0.0

    @needs_ffmpeg
    def test_the_reel_is_not_ogg_theora(self, root, planned):
        """Deliberate: the shipped cut is .ogv because Godot plays Theora, and
        the common Windows ffmpeg writes CORRUPT Theora. A previs artifact the
        reviewer cannot watch defeats the entire stage."""
        out = animatic.build(root, planned)
        assert out["path"].endswith(".mp4")
        assert out["codec"] != "libtheora"
        assert (root / out["path"]).stat().st_size > 0

    def test_average_shot_length_is_reported_against_the_readable_window(self):
        fast = {"panels": 4, "average_shot_s": 1.5, "placeholders": 0,
                "placeholder_idx": [], "runtime_s": 6.0, "measured_s": 6.0,
                "shots": [{"idx": i, "duration_defaulted": False}
                          for i in range(1, 5)]}
        assert any("montage" in w for w in animatic.report_notes(fast, []))
        slow = {**fast, "average_shot_s": 11.0}
        assert any("slideshow" in w or "freeze" in w
                   for w in animatic.report_notes(slow, []))

    def test_two_shots_describing_the_same_beat_are_flagged(self, root):
        """The cheapest possible moment to notice you are about to buy one
        picture twice."""
        cinematic.plan(root, "repeats", [
            {"action": "Ledger stares at the lift", "duration": 5},
            {"action": "Ledger stares at the lift", "duration": 5},
        ])
        shots = animatic.resolve(root, "repeats")["shots"]
        notes = animatic.report_notes(
            {"panels": 2, "average_shot_s": 5.0, "placeholders": 0,
             "placeholder_idx": [], "runtime_s": 10.0, "measured_s": 10.0,
             "shots": [{"idx": s["idx"], "duration_defaulted": False}
                       for s in shots]}, shots)
        assert any("same beat" in n for n in notes)

    @needs_ffmpeg
    def test_the_caption_sidecar_is_written_from_the_same_arithmetic(
            self, root, board):
        """Written beside the reel as well as burned into the panels, so a
        timing disagreement between previs and the finished cut shows up while
        it is still free to fix."""
        out = animatic.build(root, board)
        srt = root / out["captions"]["srt"]
        assert srt.exists() and "Not again." in srt.read_text(encoding="utf-8")


class TestRefusals:
    def test_it_refuses_cleanly_when_ffmpeg_is_missing(self, root, board,
                                                       monkeypatch):
        """No model to fall back on, so the message has to be the fix.

        A bare FileNotFoundError out of subprocess reads as a broken feature.
        This names the binary, how to supply one, and the fact that nothing was
        spent — which is the first thing a caller wonders about.

        IT ALSO MUST NOT NAME Gyan.FFmpeg. The first version of this message
        told Windows users to `winget install Gyan.FFmpeg`, and that build's
        libtheora encodes without error into files the decoder cannot read — it
        shipped a cutscene of green rectangles. A help string that sends people
        at a known-broken build is worse than no help string, and the only thing
        stopping it coming back is a test that says so.
        """
        monkeypatch.setattr(animatic._ffmpegbin, "resolve",
                            lambda *_a, **_k: None)
        with pytest.raises(animatic.AnimaticError) as caught:
            animatic.build(root, board)
        message = str(caught.value)
        assert "ffmpeg" in message and "PATH" in message
        assert "BGATE_FFMPEG" in message
        assert "Gyan" not in message.replace("DO NOT install Gyan.FFmpeg", "")
        assert "spent" in message or "charged" in message

    def test_an_unknown_source_is_named(self, root, board):
        with pytest.raises(animatic.AnimaticError, match="not one of"):
            animatic.resolve(root, board, source="vibes")

    def test_a_board_with_every_frame_cut_refuses(self, root, board):
        for idx in (1, 2, 3):
            storyboard.frame_cut(root, board, idx)
        with pytest.raises(animatic.AnimaticError, match="no live shots"):
            animatic.build(root, board, source="board")


class TestTheFfmpegOverride:
    """BGATE_FFMPEG, because the machine's own ffmpeg was the bug.

    Six modules independently called shutil.which("ffmpeg"), so there was no way
    to use a different binary short of uninstalling software — and the binary on
    PATH was producing unreadable Ogg Theora. These pin the escape hatch.
    """

    def test_the_variable_beats_path(self, tmp_path, monkeypatch):
        from bgate_core.runtime import ffmpegbin

        good = tmp_path / "good.exe"
        good.write_text("", encoding="utf-8")
        monkeypatch.setattr(ffmpegbin.shutil, "which",
                            lambda *_a, **_k: r"C:\on\path\ffmpeg.exe")
        monkeypatch.setenv(ffmpegbin.ENV_VAR, str(good))
        assert ffmpegbin.resolve() == str(good)
        assert ffmpegbin.source(str(good)) == ffmpegbin.ENV_VAR

    def test_a_variable_pointing_nowhere_does_not_fall_back(self, monkeypatch):
        """The whole point is to AVOID what is on PATH.

        Falling through would hand back the exact binary the override existed to
        escape, and report success while doing it.
        """
        from bgate_core.runtime import ffmpegbin

        monkeypatch.setattr(ffmpegbin.shutil, "which",
                            lambda name, *_a, **_k: None if name != "ffmpeg"
                            else r"C:\on\path\ffmpeg.exe")
        monkeypatch.setenv(ffmpegbin.ENV_VAR, r"C:\nope\missing.exe")
        assert ffmpegbin.resolve() is None
        assert "not there" in ffmpegbin.source()

    def test_unset_falls_through_to_path(self, monkeypatch):
        """...but only when there is no deliberately-placed binary either.

        `local_bin` is stubbed rather than left alone: the developing machine
        HAS a ~/.bgate/bin/ffmpeg.exe, so a test that did not stub it would
        pass or fail depending on whose desktop it ran on.
        """
        from bgate_core.runtime import ffmpegbin

        monkeypatch.delenv(ffmpegbin.ENV_VAR, raising=False)
        monkeypatch.setattr(ffmpegbin, "local_bin", lambda: None)
        monkeypatch.setattr(ffmpegbin.shutil, "which",
                            lambda *_a, **_k: r"C:\on\path\ffmpeg.exe")
        assert ffmpegbin.resolve() == r"C:\on\path\ffmpeg.exe"
        assert ffmpegbin.source(r"C:\on\path\ffmpeg.exe") == "PATH"

    def test_a_deliberately_placed_binary_beats_path(self, monkeypatch):
        """~/.bgate/bin is a choice; PATH is usually an accident.

        This is the whole escape hatch for the bug that started this. The
        ffmpeg a package manager had put on PATH was the one producing
        unreadable Theora, and the remedy must not require uninstalling it.
        """
        from bgate_core.runtime import ffmpegbin

        monkeypatch.delenv(ffmpegbin.ENV_VAR, raising=False)
        monkeypatch.setattr(ffmpegbin, "local_bin",
                            lambda: r"C:\Users\x\.bgate\bin\ffmpeg.exe")
        monkeypatch.setattr(ffmpegbin.shutil, "which",
                            lambda *_a, **_k: r"C:\broken\ffmpeg.exe")
        assert ffmpegbin.resolve() == r"C:\Users\x\.bgate\bin\ffmpeg.exe"
        assert ffmpegbin.source() == "~/.bgate/bin"

    def test_an_explicit_argument_beats_the_variable(self, tmp_path, monkeypatch):
        from bgate_core.runtime import ffmpegbin

        a, b = tmp_path / "a.exe", tmp_path / "b.exe"
        a.write_text("", encoding="utf-8"); b.write_text("", encoding="utf-8")
        monkeypatch.setenv(ffmpegbin.ENV_VAR, str(b))
        assert ffmpegbin.resolve(str(a)) == str(a)
