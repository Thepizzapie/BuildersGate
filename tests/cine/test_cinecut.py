"""Post-production: captions, transitions, sound, continuity, delivery.

The half that turns a folder of clips into a cutscene. Each piece here was
missing from the first build, and one of them — sound — was worse than missing:
three documents described "the audio seat scores it over the top" while every
assembled cut shipped silent. These tests exist so that cannot recur quietly.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from bgate_core.cine import cinecut, cinematic

HAVE_THEORA = cinematic.ffmpeg_status()["ok"]
needs_theora = pytest.mark.skipif(
    not HAVE_THEORA, reason="this ffmpeg cannot write Ogg Theora")

FF = shutil.which("ffmpeg")


def _clip(path, *, seconds=1, size="320x240", source="testsrc", args=""):
    """A real H.264 .mp4 — what a video model hands back, in miniature.

    lavfi separates a filter's FIRST argument with `=` and every one after it
    with `:`, so `args` carries any leading options ("c=black:") rather than
    being pasted on with another `=`, which parses as a filter nobody has.

    SKIPS rather than crashes when there is no ffmpeg. `needs_theora` guards the
    tests that ENCODE, but a few only need a file to exist, and on a runner with
    no ffmpeg those died with a TypeError from `subprocess.run(None, ...)` — a
    red CI reporting the wrong problem. Guarding the helper covers every test
    that uses it, including the ones nobody has written yet.
    """
    if not FF:
        pytest.skip("ffmpeg is not on PATH, so there is no clip to make")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FF, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"{source}={args}size={size}:rate=15:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


def _tone(path, *, seconds=4, freq=220):
    if not FF:
        pytest.skip("ffmpeg is not on PATH, so there is no tone to make")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FF, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}", str(path)],
        check=True, capture_output=True)
    return path


def _shots(*specs):
    """(duration, transition, transition_s, dialogue) tuples -> shot dicts."""
    out = []
    for i, (dur, kind, hold, line) in enumerate(specs, start=1):
        out.append({"idx": i, "slug": f"shot{i:02d}", "duration": dur,
                    "transition": kind, "transition_s": hold,
                    "dialogue": line})
    return out


class TestCaptionTiming:
    def test_a_line_sits_inside_its_shot(self):
        lines = cinecut.captions(_shots((5, "cut", 0, "hello")))
        assert lines[0]["start"] > 0 and lines[0]["end"] < 5

    def test_shots_with_no_dialogue_produce_no_caption(self):
        assert cinecut.captions(_shots((5, "cut", 0, ""))) == []

    def test_a_transition_shortens_the_cut(self):
        """A 1s dissolve is not 1s of extra runtime — both shots are on screen
        at once, so the cut is a second SHORTER than the sum of its parts."""
        shots = _shots((5, "cut", 0, ""), (5, "dissolve", 1.0, ""))
        assert cinecut.runtime_of(shots) == 9.0

    def test_timing_follows_the_transition_not_the_naive_sum(self):
        """Timing off the sum drifts later and later through a sequence — the
        classic subtitle bug, invisible until the last line of a long scene."""
        shots = _shots((5, "cut", 0, "one"), (5, "dissolve", 1.0, "two"))
        lines = cinecut.captions(shots)
        # Shot 2 begins at 4.0, not 5.0.
        assert 4.0 <= lines[1]["start"] < 4.5

    def test_captions_never_overlap(self):
        """MEASURED on the first sequence assembled with a transition: line 1
        ran to 4.85 while line 2 began at 4.15 — seven tenths of a second with
        two subtitles stacked, and the player showing the OLD line over the NEW
        shot."""
        shots = _shots((5, "cut", 0, "one"), (5, "dissolve", 1.0, "two"))
        lines = cinecut.captions(shots)
        assert lines[0]["end"] <= lines[1]["start"], lines

    def test_a_line_on_too_short_a_shot_is_flagged_not_dropped(self):
        """A line the writer put in the scene should not vanish because the
        shot is brief; an unreadable caption is better evidence of a pacing
        problem than silence."""
        lines = cinecut.captions(_shots((4, "cut", 0, "a very long line here")))
        short = cinecut.captions([{"idx": 1, "duration": 0.5,
                                   "dialogue": "too fast"}])
        assert lines[0]["short"] is False
        assert short[0]["short"] is True

    def test_srt_is_well_formed_and_ordered(self):
        shots = _shots((5, "cut", 0, "one"), (5, "dissolve", 1.0, "two"))
        srt = cinecut.to_srt(cinecut.captions(shots))
        assert srt.startswith("1\n00:00:")
        assert "-->" in srt and "two" in srt
        # SubRip uses a comma before the milliseconds; a dot is the other
        # format and silently fails to parse in some tools.
        assert "," in srt.split("-->")[0]

    def test_the_srt_clock_rounds_without_producing_1000ms(self):
        assert cinecut._srt_clock(1.9999) == "00:00:02,000"
        assert cinecut._srt_clock(3661.5) == "01:01:01,500"


class TestThePlan:
    def test_cuts_only_take_the_cheap_path(self):
        """A filter graph decodes every shot in full. Nothing pays for one it
        does not use."""
        plan = cinecut.picture_plan(_shots((5, "cut", 0, ""), (5, "cut", 0, "")))
        assert plan["filtered"] is False

    def test_a_transition_or_a_fade_forces_the_graph(self):
        assert cinecut.picture_plan(
            _shots((5, "cut", 0, ""), (5, "dissolve", 1, "")))["filtered"]
        assert cinecut.picture_plan(
            _shots((5, "cut", 0, "")), fade_in=0.5)["filtered"]

    def test_an_unknown_transition_is_refused(self):
        with pytest.raises(cinecut.CutError, match="unknown transition"):
            cinecut.picture_plan(_shots((5, "cut", 0, ""), (5, "swirl", 1, "")))

    def test_the_xfade_offset_is_measured_on_the_output_so_far(self):
        """xfade chains pairwise and its offset is on the output built so far,
        not the input being added. Get it wrong and ffmpeg still exits 0 with a
        shot missing."""
        graph, out = cinecut.xfade_graph(
            _shots((5, "cut", 0, ""), (5, "dissolve", 1.0, ""),
                   (5, "dissolve", 1.0, "")))
        assert "offset=4.000" in graph      # 5 - 1
        assert "offset=8.000" in graph      # (5 + 5 - 1) - 1
        assert out


class TestTheJoin:
    @needs_theora
    def test_cuts_join_into_one_theora_file(self, tmp_path):
        srcs = [_clip(tmp_path / f"{i}.mp4", seconds=2) for i in range(2)]
        out = cinecut.build_picture(srcs, _shots((2, "cut", 0, ""),
                                                 (2, "cut", 0, "")),
                                    tmp_path / "cut.ogv")
        assert out["filtered"] is False
        assert (tmp_path / "cut.ogv").is_file()

    @needs_theora
    def test_a_dissolve_actually_shortens_the_file(self, tmp_path):
        """The measurement that proves the transition happened rather than the
        clips being butt-joined."""
        srcs = [_clip(tmp_path / f"{i}.mp4", seconds=3) for i in range(2)]
        out = cinecut.build_picture(
            srcs, _shots((3, "cut", 0, ""), (3, "dissolve", 1.0, "")),
            tmp_path / "cut.ogv")
        assert out["filtered"] is True
        assert 4.4 <= out["measured_s"] <= 5.6, out    # 3 + 3 - 1

    @needs_theora
    def test_a_plan_that_disagrees_with_the_file_is_reported(self, tmp_path):
        """Caption timing is built from the plan, so a disagreement means the
        subtitles are wrong too — and that is invisible until the last line."""
        srcs = [_clip(tmp_path / "a.mp4", seconds=1)]
        out = cinecut.build_picture(srcs, _shots((9, "cut", 0, "")),
                                    tmp_path / "cut.ogv")
        assert "timing_warning" in out


class TestSound:
    @needs_theora
    def test_a_bed_is_muxed_and_the_video_is_not_re_encoded(self, tmp_path):
        """The picture has been through Theora once; a second pass to attach an
        audio stream would be generation loss for nothing."""
        picture = tmp_path / "p.ogv"
        cinecut.build_picture([_clip(tmp_path / "a.mp4", seconds=3)],
                              _shots((3, "cut", 0, "")), picture)
        out = cinecut.mix_audio(picture, tmp_path / "scored.ogv",
                                bed=str(_tone(tmp_path / "bed.mp3", seconds=3)))
        assert (tmp_path / "scored.ogv").is_file()
        probe = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "csv=p=0",
             str(tmp_path / "scored.ogv")], capture_output=True, text=True)
        assert "theora" in probe.stdout and "vorbis" in probe.stdout
        assert out["picture_s"] > 0

    @needs_theora
    def test_a_short_bed_pads_with_silence_and_says_so(self, tmp_path):
        """-shortest would silently truncate the CUTSCENE to the length of the
        track, which is the wrong thing to shorten."""
        picture = tmp_path / "p.ogv"
        cinecut.build_picture([_clip(tmp_path / "a.mp4", seconds=4)],
                              _shots((4, "cut", 0, "")), picture)
        out = cinecut.mix_audio(picture, tmp_path / "scored.ogv",
                                bed=str(_tone(tmp_path / "bed.mp3", seconds=1)))
        assert out["padded_with_silence_s"] > 2
        assert "shorter than the cut" in out["note"]
        # And the picture kept its full length.
        assert cinecut.duration_of(tmp_path / "scored.ogv") > 3.5

    def test_a_missing_bed_is_named(self, tmp_path):
        with pytest.raises(cinecut.CutError, match="audio bed not found"):
            cinecut.mix_audio(_clip(tmp_path / "a.mp4"), tmp_path / "o.ogv",
                              bed=str(tmp_path / "ghost.mp3"))


class TestContinuity:
    @needs_theora
    def test_matching_shots_pass(self, tmp_path):
        clips = [_clip(tmp_path / f"{i}.mp4", seconds=2) for i in range(2)]
        joins = cinecut.continuity(clips, work_dir=tmp_path / "w")
        assert len(joins) == 1
        assert joins[0]["ok"] is True, joins

    @needs_theora
    def test_a_black_shot_against_a_bright_one_is_flagged(self, tmp_path):
        """The check reads the real frames, because the whole reason a cut
        fails is that the model did something other than what was asked."""
        dark = _clip(tmp_path / "dark.mp4", seconds=2, source="color", args="c=black:")
        light = _clip(tmp_path / "light.mp4", seconds=2, source="color", args="c=white:")
        joins = cinecut.continuity([dark, light], work_dir=tmp_path / "w")
        assert joins[0]["ok"] is False
        assert joins[0]["luma_delta"] > cinecut.LUMA_JUMP
        assert any("brightness" in f for f in joins[0]["flags"])

    @needs_theora
    def test_a_finding_describes_rather_than_condemns(self, tmp_path):
        """A cut from a cellar to a snowfield SHOULD jump. The verdict is a
        human's."""
        dark = _clip(tmp_path / "dark.mp4", seconds=2, source="color", args="c=black:")
        light = _clip(tmp_path / "light.mp4", seconds=2, source="color", args="c=white:")
        flags = cinecut.continuity([dark, light],
                                   work_dir=tmp_path / "w")[0]["flags"]
        assert any("if they are not, this is fine" in f for f in flags)


class TestTheDeliveredScene:
    def test_the_root_is_a_canvaslayer_over_everything(self):
        """A cutscene draws over whatever is rendering — 2D, 3D or the HUD. A
        Control would inherit its parent's transform and land wherever that is."""
        text = cinecut.cutscene_scene_text("res://a.ogv", "res://a.gd")
        assert 'type="CanvasLayer"' in text
        assert "layer = 100" in text

    def test_the_video_and_script_are_external_resources(self):
        text = cinecut.cutscene_scene_text("res://cine/x.ogv", "res://cine/x.gd")
        assert 'type="VideoStream" path="res://cine/x.ogv"' in text
        assert 'type="Script" path="res://cine/x.gd"' in text
        # Declared once each (lowercase header) and USED once each (ExtResource
        # call). A declaration nothing references loads the file for nothing;
        # a reference with no declaration does not parse.
        assert text.count("[ext_resource") == 2
        assert text.count("ExtResource(") == 2

    def test_the_caption_label_has_an_outline(self):
        """Subtitle text over an arbitrary frame is unreadable without one."""
        text = cinecut.cutscene_scene_text("res://a.ogv", "res://a.gd")
        assert "outline_size" in text

    def test_load_steps_covers_every_resource(self):
        """Godot uses load_steps to size its resource table; too low and the
        scene loads with a missing resource and no error worth reading."""
        text = cinecut.cutscene_scene_text("res://a.ogv", "res://a.gd")
        declared = int(text.split("load_steps=")[1].split()[0])
        actual = text.count("[ext_resource") + text.count("[sub_resource") + 1
        assert declared == actual, text

    def test_the_script_emits_one_signal_for_both_endings(self):
        """Every caller wants the same thing next; branching on whether it was
        skipped is how a game ends up stuck on a black screen."""
        gd = cinecut.CUTSCENE_GD
        assert "signal finished(skipped: bool)" in gd
        assert gd.count("finished.emit") == 1

    def test_the_script_is_skippable_and_marks_the_input_handled(self):
        gd = cinecut.CUTSCENE_GD
        assert "ui_cancel" in gd and "ui_accept" in gd
        # Without this the same press also reaches the game underneath.
        assert "set_input_as_handled" in gd

    def test_captions_are_driven_off_the_video_clock(self):
        """A separate timer drifts the moment the video stalls on a slow disk."""
        assert "stream_position" in cinecut.CUTSCENE_GD

class TestWrittenCaptionFiles:
    def test_both_files_are_written_from_one_source(self, tmp_path):
        lines = cinecut.captions(_shots((5, "cut", 0, "hello there")))
        out = cinecut.write_captions(tmp_path, tmp_path / "c", "scene", lines)
        assert out["lines"] == 1
        srt = (tmp_path / "c" / "scene.srt").read_text(encoding="utf-8")
        data = json.loads((tmp_path / "c" / "scene_captions.json").read_text())
        assert "hello there" in srt
        assert data[0]["text"] == "hello there"
        # The engine file carries only what the player needs.
        assert set(data[0]) == {"start", "end", "text"}


class TestWindowsSafety:
    """Windows is the supported platform (CLAUDE.md), and the two hazards here
    are both invisible on Linux, which is where this was written."""

    def test_the_concat_list_uses_forward_slashes(self, tmp_path):
        """Inside a quoted concat entry the demuxer treats `\\` as an ESCAPE
        character. A project at C:\\Users\\nina\\new-game feeds it `\\n`, and the
        entry silently becomes a filename with a newline in it."""
        clips = [_clip(tmp_path / "a.mp4"), _clip(tmp_path / "b.mp4")]
        out = tmp_path / "cut.ogv"
        listing_text = {}

        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            for i, part in enumerate(cmd):
                if part == "-i" and str(cmd[i + 1]).endswith("_concat.txt"):
                    listing_text["body"] = pathlib.Path(cmd[i + 1]).read_text(
                        encoding="utf-8")
            return real_run(cmd, *a, **kw)

        import bgate_core.cine.cinecut as mod
        mod.subprocess.run = spy
        try:
            mod.build_picture(clips, _shots((1, "cut", 0, ""), (1, "cut", 0, "")),
                              out)
        finally:
            mod.subprocess.run = real_run

        body = listing_text.get("body", "")
        assert body, "the concat list was never written"
        assert "\\" not in body, body

    def test_every_subprocess_call_suppresses_the_console_window(self):
        """Five other modules carry _NO_WINDOW; this one spawns MORE binaries
        than any of them — one per shot for continuity, one per join, one per
        mix — and under a stdio MCP server each one would flash a window."""
        import ast
        import inspect

        for module in ("bgate_core.cine.cinecut", "bgate_core.cine.cinematic"):
            mod = __import__(module, fromlist=["x"])
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", None)
                if name != "run":
                    continue
                if getattr(getattr(func, "value", None), "id", "") != "subprocess":
                    continue
                kwargs = {k.arg for k in node.keywords}
                assert "creationflags" in kwargs, (
                    f"{module}: a subprocess.run at line {node.lineno} does not "
                    "pass creationflags=_NO_WINDOW")
                assert "stdin" in kwargs, (
                    f"{module}: a subprocess.run at line {node.lineno} does not "
                    "close stdin — see docs/gotchas.md on stdio MCP servers")
