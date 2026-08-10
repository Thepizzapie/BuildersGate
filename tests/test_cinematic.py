"""The cutscene pipeline, and mostly the four places video is not like music.

The interesting tests here are not "does a row get written". They are:

  * a kept clip is TRANSCODED, not copied, because Godot cannot play an .mp4
    and the failure mode is silent — no import error, a blank rectangle;
  * a sequence with an unkept shot refuses to assemble, because assembling
    around a missing beat ships a story that does not make sense;
  * shots of differing sizes refuse to join, because ffmpeg joins them into a
    broken file and reports SUCCESS;
  * a replan preserves clips already paid for.

Anything needing a real encode is skipped where ffmpeg has no libtheora, which
is a real machine state (several distributions ship one) rather than a
hypothetical — and is exactly what ffmpeg_status() exists to report.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from bgate_core import artifacts, cinematic, seats

pytestmark = pytest.mark.usefixtures("root")

HAVE_THEORA = cinematic.ffmpeg_status()["ok"]
needs_theora = pytest.mark.skipif(
    not HAVE_THEORA, reason="this ffmpeg cannot write Ogg Theora")


def _clip(path, *, seconds=1, size="320x240"):
    """A real, tiny H.264 .mp4 — what a video model hands back, in miniature."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


def _shots(n=2, **over):
    return [{"action": f"beat {i}", "duration": 5, **over} for i in range(1, n + 1)]


class TestTheSeat:
    def test_cinematic_is_a_seat(self):
        assert "cinematic" in seats.ROLES

    def test_it_cannot_write_the_art_seat_s_lane(self, root):
        """The argument for an eighth seat rather than widening art: a .ogv does
        not merge, and art must not be locking video it did not make."""
        assert not seats.can_write(
            root, "cinematic", "game/assets/sprites/hero.png")["allowed"]
        assert seats.can_write(
            root, "cinematic", "game/assets/cinematics/intro.ogv")["allowed"]

    def test_art_cannot_write_cinematics(self, root):
        assert not seats.can_write(
            root, "art", "cinematics/intro.ogv")["allowed"]

    def test_the_brief_carries_the_format_trap(self, root):
        """The .mp4-is-silently-ignored trap is the one that costs a whole
        sequence, so it must reach the seat that generates them."""
        brief = seats.brief(root, "cinematic")
        assert any("no import error" in t.lower() for t in brief["traps"])
        assert "shot list" in brief["workflow"].lower()


class TestPlanning:
    def test_a_plan_costs_nothing_and_orders_the_shots(self, root):
        seq = cinematic.plan(root, "Intro Scene", _shots(3), logline="a start")
        assert seq["name"] == "intro-scene"
        assert [s["idx"] for s in seq["shots"]] == [1, 2, 3]
        assert seq["runtime_s"] == 15
        assert seq["kept"] == 0

    def test_a_shot_with_no_action_is_refused(self, root):
        with pytest.raises(cinematic.CinematicError, match="no action"):
            cinematic.plan(root, "bad", [{"camera": "wide"}])

    def test_a_duration_no_model_will_generate_is_refused(self, root):
        with pytest.raises(cinematic.CinematicError, match="two shots"):
            cinematic.plan(root, "long", [{"action": "x", "duration": 40}])

    def test_an_unanchored_sequence_is_warned_about(self, root):
        """The most expensive warning in the product to ignore: every shot
        invents the cast fresh, so no two agree on a face."""
        seq = cinematic.plan(root, "drifty", _shots(2))
        assert any("NOT ONE SHOT IS ANCHORED" in w for w in seq["warnings"])

    def test_an_anchored_sequence_is_not_warned_about(self, root):
        seq = cinematic.plan(root, "anchored", _shots(2, refs=["art/hero.png"]))
        assert not any("ANCHORED" in w for w in seq.get("warnings", []))

    def test_a_long_shot_warns_but_is_planned(self, root):
        seq = cinematic.plan(root, "slow", [{"action": "x", "duration": 14}])
        assert any("drift" in w for w in seq["warnings"])
        assert seq["shots"][0]["duration"] == 14

    def test_the_prompt_leads_with_the_camera(self, root):
        """A camera instruction buried after two sentences of action gets
        averaged away by the model."""
        text = cinematic.prompt_for(
            {"action": "she turns", "camera": "slow push in",
             "dialogue": "we are late"})
        assert text.startswith("slow push in.")
        assert '"we are late"' in text

    def test_replanning_keeps_shots_already_paid_for(self, root):
        cinematic.plan(root, "seq", _shots(2))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        cinematic._set_shot(root, seq["shots"][0]["id"], status="generated",
                            artifact_id=art["id"])

        # Shot 1's action is untouched; shot 2's is rewritten.
        again = cinematic.plan(root, "seq", [
            {"action": "beat 1", "duration": 5},
            {"action": "a completely different beat", "duration": 5}])
        assert again["shots"][0]["status"] == "generated"
        assert again["shots"][0]["artifact_id"] == art["id"]
        assert again["shots"][1]["status"] == "planned"
        assert again["carried"]["shots"] == 1

    def test_a_missing_sequence_names_the_ones_that_exist(self, root):
        cinematic.plan(root, "real", _shots(1))
        with pytest.raises(cinematic.CinematicError, match="real"):
            cinematic.sequence(root, "imaginary")


def _register_fake(root, seq, idx, *, size="320x240", suffix=".mp4", take=1):
    """A registered candidate shot with a real file behind it.

    `take` varies the clip's CONTENT, not just its name. Two takes encoded from
    identical ffmpeg arguments are byte-identical and therefore hash-identical,
    which silently defeats any test about which revision's bytes are installed.
    """
    shot = seq["shots"][idx - 1]
    stem = f"{seq['name']}_{shot['slug']}"
    path = root / cinematic.CANDIDATE_DIR / seq["name"] / f"{stem}_take{take}{suffix}"
    _clip(path, size=size, seconds=take)
    return artifacts.register(root, stem, path, producer=cinematic.PRODUCER,
                              metadata={"sequence": seq["name"],
                                        "shot_idx": idx, "kind": "shot"})


class TestKeepingTranscodes:
    """The single most important behaviour in the module."""

    @needs_theora
    def test_keeping_writes_ogv_not_mp4(self, root):
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)

        out = cinematic.keep(root, art["id"], actor="human")
        installed = out["install"]["path"]
        assert installed.endswith(".ogv"), installed
        assert out["install"]["transcoded"] is True
        assert (root / installed).is_file()

    @needs_theora
    def test_the_installed_file_is_really_theora(self, root):
        """A test that asserts on the extension alone would pass for a renamed
        .mp4, which is precisely the bug — Godot reads the container, not the
        name."""
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        out = cinematic.keep(root, art["id"], actor="human")

        probe = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
             str(root / out["install"]["path"])],
            capture_output=True, text=True, check=True)
        assert probe.stdout.strip() == "theora"

    @needs_theora
    def test_the_shot_row_is_marked_kept(self, root):
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        cinematic._set_shot(root, seq["shots"][0]["id"], artifact_id=art["id"],
                            status="generated")
        cinematic.keep(root, art["id"], actor="human")
        assert cinematic.sequence(root, "seq")["shots"][0]["status"] == "kept"

    @needs_theora
    def test_installed_is_false_once_a_later_take_overwrites(self, root):
        """Two takes install to one destination. A card that says 'installed'
        for the loser is a card claiming to be the clip in the game."""
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        first = _register_fake(root, seq, 1, take=1)
        cinematic.keep(root, first["id"], actor="human")
        second = _register_fake(root, seq, 1, take=2)
        cinematic.keep(root, second["id"], actor="human")

        by_id = {c["artifact_id"]: c for c in cinematic.kept(root)}
        assert by_id[second["id"]]["installed"] is True
        assert by_id[first["id"]]["installed"] is False

    def test_a_non_video_artifact_is_refused(self, root):
        note = root / "design" / "note.txt"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("not a clip", encoding="utf-8")
        art = artifacts.register(root, "note", note, producer=cinematic.PRODUCER)
        with pytest.raises(cinematic.CinematicError, match="not video"):
            cinematic.keep(root, art["id"], actor="human")

    @needs_theora
    def test_discarding_returns_the_shot_to_planned(self, root):
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        cinematic._set_shot(root, seq["shots"][0]["id"], artifact_id=art["id"],
                            status="generated")
        cinematic.discard(root, art["id"], note="the face is wrong")
        again = cinematic.sequence(root, "seq")["shots"][0]
        assert again["status"] == "planned" and again["artifact_id"] is None


class TestAssembly:
    @needs_theora
    def test_an_unkept_shot_refuses_the_cut(self, root):
        cinematic.plan(root, "seq", _shots(2))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        cinematic._set_shot(root, seq["shots"][0]["id"], artifact_id=art["id"],
                            status="generated")
        cinematic.keep(root, art["id"], actor="human")
        with pytest.raises(cinematic.CinematicError, match=r"\[2\]"):
            cinematic.assemble(root, "seq")

    @needs_theora
    def test_a_kept_pair_assembles_into_one_ogv(self, root):
        cinematic.plan(root, "seq", _shots(2))
        seq = cinematic.sequence(root, "seq")
        for idx in (1, 2):
            art = _register_fake(root, seq, idx)
            cinematic._set_shot(root, seq["shots"][idx - 1]["id"],
                                artifact_id=art["id"], status="generated")
            cinematic.keep(root, art["id"], actor="human")

        out = cinematic.assemble(root, "seq")
        assert out["shots"] == 2
        cut = root / out["path"]
        assert cut.suffix == ".ogv" and cut.is_file()
        assert cinematic.sequence(root, "seq")["status"] == "assembled"

    @needs_theora
    def test_mismatched_sizes_refuse_rather_than_join_badly(self, root):
        """ffmpeg's concat demuxer does not scale, produces a broken file, and
        frequently exits ZERO — so this cannot be left to the return code."""
        cinematic.plan(root, "seq", _shots(2))
        seq = cinematic.sequence(root, "seq")
        for idx, size in ((1, "320x240"), (2, "640x480")):
            art = _register_fake(root, seq, idx, size=size)
            cinematic._set_shot(root, seq["shots"][idx - 1]["id"],
                                artifact_id=art["id"], status="generated")
            cinematic.keep(root, art["id"], actor="human")
        with pytest.raises(cinematic.CinematicError, match="not all the same size"):
            cinematic.assemble(root, "seq")

    @needs_theora
    def test_keeping_an_assembled_cut_copies_rather_than_re_encodes(self, root):
        """It is already Theora. Re-encoding would be a second generation of
        loss for nothing."""
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)
        cinematic._set_shot(root, seq["shots"][0]["id"], artifact_id=art["id"],
                            status="generated")
        cinematic.keep(root, art["id"], actor="human")
        cut = cinematic.assemble(root, "seq")

        out = cinematic.keep(root, cut["artifact_id"], actor="human")
        assert out["install"]["transcoded"] is False
        assert out["install"]["path"].endswith(".ogv")


class TestTranscode:
    @needs_theora
    def test_the_gop_is_godot_s_not_libtheora_s_default(self, root):
        """libtheora defaults to a 12-frame GOP, which the engine docs call
        insufficient and which inflates a cutscene for nothing."""
        src = _clip(root / ".bgate_out" / "in.mp4", seconds=2)
        out = cinematic.transcode(src, root / ".bgate_out" / "out.ogv")
        assert out["gop"] == 64
        assert out["bytes"] > 0

    @needs_theora
    def test_downscaling_is_available_for_an_oversized_source(self, root):
        src = _clip(root / ".bgate_out" / "big.mp4", size="640x480")
        cinematic.transcode(src, root / ".bgate_out" / "small.ogv",
                            scale_height=240)
        probe = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0",
             str(root / ".bgate_out" / "small.ogv")],
            capture_output=True, text=True, check=True)
        assert probe.stdout.strip() == "240"

    def test_a_missing_source_is_named(self, root):
        with pytest.raises(cinematic.CinematicError, match="nothing on disk"):
            cinematic.transcode(root / "gone.mp4", root / "out.ogv")


class TestOptions:
    def test_options_report_both_availabilities_separately(self, root):
        """A key without libtheora buys a whole sequence and delivers none of
        it, so 'can I generate' and 'can I deliver' are two answers."""
        opts = cinematic.options(root)
        assert "provider_available" in opts and "encoder" in opts
        assert opts["engine_format"]["suffix"] == ".ogv"

    def test_audio_is_off_by_default_and_says_why(self, root):
        opts = cinematic.options(root)
        assert opts["audio_default"] is False
        assert "baked into the clip" in opts["audio_note"]

    def test_the_shot_ceiling_comes_from_the_adapter(self, root):
        """A form that retypes '4 to 15 seconds' lies the day kie changes it."""
        from bgate_adapters import kie

        opts = cinematic.options(root)
        assert (opts["models"]["seedance-2"]["ranges"]["duration"]
                == kie.MODELS["seedance-2"]["ranges"]["duration"])


class TestAnchors:
    def test_a_named_frame_that_is_not_on_disk_is_reported(self, root):
        frames = cinematic.keyframes_for(
            root, {"first_frame": "art/nope.png", "refs": []})
        assert frames["missing"] == ["art/nope.png"]

    def test_a_url_passes_through_untouched(self, root):
        frames = cinematic.keyframes_for(
            root, {"first_frame": "https://example.com/a.png", "refs": []})
        assert frames["first"] == "https://example.com/a.png"
        assert not frames["missing"]

    def test_a_local_frame_is_handed_over_as_a_path_for_upload(self, root):
        plate = root / "art" / "plate.png"
        plate.parent.mkdir(parents=True, exist_ok=True)
        plate.write_bytes(b"\x89PNG\r\n\x1a\n")
        frames = cinematic.keyframes_for(root, {"first_frame": "art/plate.png",
                                                "refs": []})
        assert frames["first"] == str(plate)
        assert not frames["missing"]

    def test_generation_refuses_a_missing_anchor_before_spending(self, root):
        cinematic.plan(root, "seq", [{"action": "x", "duration": 5,
                                      "first_frame": "art/ghost.png"}])
        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False and out["stage"] == "anchors"
        assert "Nothing has been charged" in out["error"]


class TestGeneratingAShot:
    """The generate path with the provider stubbed — everything except the
    money: the pre-spend gates, the registration, and the shot row's state."""

    def _stub(self, monkeypatch, **over):
        from bgate_adapters import kie

        calls = []

        def fake(prompt, out_path, **kw):
            calls.append({"prompt": prompt, **kw})
            _clip(pathlib.Path(out_path))
            return {"ok": True, "model": "bytedance/seedance-2",
                    "path": str(out_path), "task_id": "task-1",
                    "url": "https://kie.test/v/1.mp4",
                    "uploads": [], "credits_consumed": 120, **over}

        monkeypatch.setattr(kie, "generate_video", fake)
        return calls

    @needs_theora
    def test_a_generated_shot_is_registered_and_linked(self, root, monkeypatch):
        calls = self._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1, camera="slow push in"))

        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is True
        shot = cinematic.sequence(root, "seq")["shots"][0]
        assert shot["status"] == "generated"
        assert shot["artifact_id"] == out["artifact_id"]
        assert shot["task_id"] == "task-1"

        art = artifacts.get(root, out["artifact_id"])
        assert art["metadata"]["sequence"] == "seq"
        assert art["metadata"]["shot_idx"] == 1
        # Provenance, not a location — stamped so nothing refetches it later.
        assert art["metadata"]["source_url"] == "https://kie.test/v/1.mp4"
        assert calls[0]["duration"] == 5
        assert calls[0]["prompt"].startswith("slow push in.")

    @needs_theora
    def test_audio_is_off_unless_asked_for(self, root, monkeypatch):
        """Generated audio is baked in and cannot be separated afterwards."""
        calls = self._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        cinematic.generate_shot(root, "seq", 1)
        assert calls[0]["generate_audio"] is False

        cinematic.generate_shot(root, "seq", 1, generate_audio=True)
        assert calls[1]["generate_audio"] is True

    @needs_theora
    def test_two_shots_do_not_share_a_file(self, root, monkeypatch):
        """slugify("") returns "unnamed", which is TRUTHY — so every unnamed
        shot in a sequence once got the same slug, the same logical name and the
        same path, and shot 2 silently overwrote the clip shot 1 was paid for."""
        self._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(2))
        first = cinematic.generate_shot(root, "seq", 1)
        second = cinematic.generate_shot(root, "seq", 2)
        assert first["path"] != second["path"]
        assert (root / first["path"]).is_file()
        assert (root / second["path"]).is_file()

    def test_duplicate_slugs_are_disambiguated_not_refused(self, root):
        """Two shots called "wide" is an ordinary authoring slip with the same
        catastrophic result. A shot list is not worth rejecting over a name."""
        seq = cinematic.plan(root, "seq", [
            {"action": "a", "slug": "wide"}, {"action": "b", "slug": "wide"}])
        slugs = [s["slug"] for s in seq["shots"]]
        assert len(set(slugs)) == 2, slugs

    @needs_theora
    def test_a_failed_generation_marks_the_shot_and_keeps_the_task_id(
            self, root, monkeypatch):
        """The task may be finished and already charged; losing its id loses the
        only handle on work that was paid for."""
        from bgate_adapters import kie

        monkeypatch.setattr(kie, "generate_video", lambda *a, **k: {
            "ok": False, "error": "kie refused", "task_id": "task-9"})
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False
        shot = cinematic.sequence(root, "seq")["shots"][0]
        assert shot["status"] == "failed" and shot["task_id"] == "task-9"

    def test_no_encoder_refuses_before_spending(self, root, monkeypatch):
        """A key without libtheora buys a whole sequence and delivers none of
        it. That discovery belongs before the first dollar, not at the keep."""
        monkeypatch.setattr(cinematic, "ffmpeg_status", lambda: {
            "ok": False, "reason": "no libtheora here"})
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is False and out["stage"] == "encoder"
        assert "nothing has been charged" in out["error"].lower()

    def test_an_unknown_shot_index_names_the_ones_that_exist(self, root):
        cinematic.plan(root, "seq", _shots(2))
        with pytest.raises(cinematic.CinematicError, match=r"\[1, 2\]"):
            cinematic.generate_shot(root, "seq", 7)
