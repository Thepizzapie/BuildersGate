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

import json
import pathlib
import shutil
import subprocess

import pytest

from pathlib import Path

from bgate_core import artifacts, cinematic, db, seats

pytestmark = pytest.mark.usefixtures("root")

HAVE_THEORA = cinematic.ffmpeg_status()["ok"]
needs_theora = pytest.mark.skipif(
    not HAVE_THEORA, reason="this ffmpeg cannot write Ogg Theora")


def _clip(path, *, seconds=1, size="320x240"):
    """A real, tiny H.264 .mp4 — what a video model hands back, in miniature.

    SKIPS rather than crashes with no ffmpeg — see the twin in test_cinecut.py.
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not on PATH, so there is no clip to make")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [shutil.which("ffmpeg"), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:d={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


class _Done:
    """A finished subprocess.run, for the stubs below. The encoder's exit code
    is the thing under test in half of them, so it has to be settable."""

    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def _decode_noise(count):
    """What a broken libtheora's output looks like coming back through the
    decoder. The real message, because the real one is what a reader greps."""
    return "\n".join(f"[theora @ 0x1] error in unpack_block_qpis frame {i}"
                      for i in range(count))


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
    """The single most important behaviour in the module.

    Exercised through `install_to_engine=True` because a SHOT is not what the
    game loads — see TestWhatReachesTheEngineProject. The transcode is the same
    code either way; what differs is which revisions get it by default.
    """

    @needs_theora
    def test_keeping_writes_ogv_not_mp4(self, root):
        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        art = _register_fake(root, seq, 1)

        out = cinematic.keep(root, art["id"], actor="human",
                             install_to_engine=True)
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
        out = cinematic.keep(root, art["id"], actor="human",
                             install_to_engine=True)

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
        cinematic.keep(root, first["id"], actor="human", install_to_engine=True)
        second = _register_fake(root, seq, 1, take=2)
        cinematic.keep(root, second["id"], actor="human", install_to_engine=True)

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
        """A form that retypes '4 to 15 seconds' lies the day kie changes it.

        Read in INTENT terms — `seconds`, not `duration` — because that is what
        a caller planning a sequence needs and the field name behind it differs
        per model."""
        from bgate_adapters import kie

        opts = cinematic.options(root)
        lo, hi = kie.MODELS["seedance-2"]["ranges"]["duration"]
        assert opts["models"]["seedance-2"]["options"]["seconds"] == [lo, hi]


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

    def test_generation_refuses_a_missing_anchor_before_spending(self, root,
                                                                 monkeypatch):
        # The encoder gate legitimately runs BEFORE this one — it is the check
        # that stops a machine with no libtheora buying a sequence it could
        # never deliver. Stubbed so this test measures the anchor gate on any
        # machine rather than passing only where ffmpeg happens to be.
        monkeypatch.setattr(cinematic, "ffmpeg_status",
                            lambda: {"ok": True, "reason": "", "probed": True})
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
        # Intent names, not Seedance's — the pipeline must not speak one
        # model's dialect or it can never drive a second.
        assert calls[0]["seconds"] == 5
        assert calls[0]["prompt"].startswith("slow push in.")

    @needs_theora
    def test_audio_is_off_unless_asked_for(self, root, monkeypatch):
        """Generated audio is baked in and cannot be separated afterwards."""
        calls = self._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        cinematic.generate_shot(root, "seq", 1)
        # SENT EXPLICITLY, not omitted: Seedance's generate_audio defaults to
        # TRUE upstream, so "off" is a thing that has to be said.
        assert calls[0]["audio"] is False

        cinematic.generate_shot(root, "seq", 1, generate_audio=True)
        assert calls[1]["audio"] is True

    @needs_theora
    def test_two_shots_do_not_share_a_file(self, root, monkeypatch):
        """slugify("") returns "unnamed", which is TRUTHY — so every unnamed
        shot in a sequence once got the same slug, the same logical name and the
        same path, and shot 2 silently overwrote the clip shot 1 was paid for."""
        self._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(2))
        # previs_ok: these buy several shots to test GENERATION mechanics,
        # not the previs gate, which TestPrevisIsWatchedBeforeItIsBought owns.
        first = cinematic.generate_shot(root, "seq", 1, previs_ok=True)
        second = cinematic.generate_shot(root, "seq", 2, previs_ok=True)
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


class TestStyle:
    """"Cutscenes in whatever style" — three levers, and the one that matters
    most is that ALL of them reach EVERY shot without anyone remembering to."""

    def test_the_style_is_actually_in_the_prompt(self, root):
        """The first cut of this module stored a `style` column that nothing
        read: a sequence could be given a look and every shot was generated
        without it. This is the test that would have caught that."""
        cinematic.plan(root, "seq", _shots(2), style="noir")
        for shot in cinematic.sequence(root, "seq")["shots"]:
            assert "film noir" in shot["prompt"]

    def test_style_trails_the_prompt_rather_than_leading_it(self, root):
        """Trailing style applies to the whole prompt. Leading it styles only
        the noun it sits next to — "a cel-shaded knight in a ruined hall"
        returns a cel-shaded knight in a photoreal hall."""
        text = cinematic.prompt_for({"action": "she turns", "camera": "wide"},
                                    "watercolour and ink wash")
        assert text.index("she turns") < text.index("watercolour")

    def test_free_prose_is_a_style(self, root):
        """An unlisted word is not refused — whatever style means whatever."""
        look = cinematic.resolve_style("like a rain-soaked 90s music video")
        assert look["is_preset"] is False
        assert "rain-soaked" in look["text"]
        seq = cinematic.plan(root, "seq", _shots(1),
                             style="like a rain-soaked 90s music video")
        assert "rain-soaked" in seq["shots"][0]["prompt"]

    def test_a_style_note_is_appended_to_the_preset(self, root):
        look = cinematic.resolve_style("anime", "faded seaside palette")
        assert "anime" in look["text"] and "faded seaside" in look["text"]

    def test_naming_no_style_is_a_reported_choice(self, root):
        """A model with no stylistic instruction uses its own house look, which
        differs per model and per version — so an unstyled sequence is one
        nobody chose and nobody can reproduce."""
        seq = cinematic.plan(root, "seq", _shots(1))
        assert any("NO STYLE WAS NAMED" in w for w in seq["warnings"])
        assert seq["style_resolved"]["matched"] == cinematic.STYLE_FALLBACK

    def test_each_preset_carries_its_trap(self, root):
        """A preset table without the caveats is a list of nice words. `pixel`
        in particular is the WEAKEST fit for generated video."""
        table = cinematic.styles()
        assert all(spec["note"] for spec in table.values())
        assert "pixel art" in table["pixel"]["note"].lower()
        seq = cinematic.plan(root, "seq", _shots(1), style="pixel")
        assert any("shimmer" in w for w in seq["warnings"])

    def test_style_refs_ride_behind_identity_refs(self, root):
        """A model weights the front of a reference array more heavily, and
        identity is the thing that must not drift."""
        (root / "art").mkdir(exist_ok=True)
        for n in ("hero.png", "look.png"):
            (root / "art" / n).write_bytes(b"\x89PNG")
        frames = cinematic.keyframes_for(
            root, {"refs": ["art/hero.png"]}, style_refs=["art/look.png"])
        assert frames["refs"][0].endswith("hero.png")
        assert frames["refs"][1].endswith("look.png")

    def test_mixing_style_and_identity_refs_is_warned_about(self, root):
        """The art seat's rule 4: at equal strength the style ref transfers the
        SUBJECT and the whole cast comes back as one person."""
        (root / "art").mkdir(exist_ok=True)
        (root / "art" / "look.png").write_bytes(b"\x89PNG")
        seq = cinematic.plan(root, "seq", _shots(1, refs=["art/hero.png"]),
                             style="anime", style_refs=["art/look.png"])
        assert any("CANNOT SHARE A WEIGHT" in w for w in seq["warnings"])

    def test_a_style_ref_that_is_not_on_disk_is_reported_not_raised(self, root):
        """plan() must stay free and non-refusing — writing a shot list before
        the keyframes exist is the normal order of work."""
        seq = cinematic.plan(root, "seq", _shots(1), style_refs=["art/ghost.png"])
        assert any("not on disk" in w for w in seq["warnings"])
        assert seq["style_refs"] == []

    @needs_theora
    def test_changing_the_style_resets_generated_shots(self, root, monkeypatch):
        """A clip bought under the old look is not a rendering of the new one.
        Carrying it would leave a sequence half noir and half anime, with the
        seam findable only by watching the whole cut."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1), style="noir")
        cinematic.generate_shot(root, "seq", 1)
        assert cinematic.sequence(root, "seq")["shots"][0]["status"] == "generated"

        again = cinematic.plan(root, "seq", _shots(1), style="anime")
        assert again["shots"][0]["status"] == "planned"
        assert again["restyled"]["shots"] == 1

    @needs_theora
    def test_an_unchanged_style_keeps_the_shots(self, root, monkeypatch):
        """The reset must not fire on every replan, or fixing a logline throws
        away everything already paid for."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1), style="noir")
        cinematic.generate_shot(root, "seq", 1)
        again = cinematic.plan(root, "seq", _shots(1), style="noir",
                               logline="a new logline")
        assert again["shots"][0]["status"] == "generated"
        assert "restyled" not in again


class TestMultipleModels:
    """The pipeline must not speak one model's dialect. kie's own catalogue
    disagrees with itself: Sora 2 counts `n_frames` and spells its shape
    "landscape" where Seedance takes `duration` and "16:9"."""

    @pytest.fixture(autouse=True)
    def _no_leak(self):
        """register_video_model mutates a module-level table for the life of the
        process. Left alone, a model registered in one test is visible in every
        later one — which under random ordering is a failure that reproduces
        only sometimes, and blames the wrong test when it does."""
        from bgate_adapters import kie

        before = dict(kie.MODELS)
        yield
        kie.MODELS.clear()
        kie.MODELS.update(before)
        kie._refresh_model_kinds()

    def _sora_like(self):
        from bgate_adapters import kie

        return kie.register_video_model("sora-like", {
            "model": "sora-2-text-to-video",
            "label": "Sora-shaped model",
            "intent": {"seconds": "n_frames", "shape": "aspect_ratio"},
            "enums": {"aspect_ratio": ("landscape", "portrait")},
            "ranges": {"n_frames": (40, 300)},
            "intent_values": {"shape": {"16:9": "landscape",
                                        "9:16": "portrait"}},
            "intent_scale": {"seconds": 20},
        })

    def test_one_intent_becomes_two_different_payloads(self, root):
        from bgate_adapters import kie

        self._sora_like()
        assert kie.video_input("seedance-2", seconds=6, shape="16:9") == {
            "duration": 6, "aspect_ratio": "16:9"}
        assert kie.video_input("sora-like", seconds=6, shape="16:9") == {
            "n_frames": 120, "aspect_ratio": "landscape"}

    def test_an_intent_a_model_cannot_do_is_refused_not_dropped(self, root):
        """A silently dropped parameter still bills you and hands back the
        default, with nothing saying why the setting did not apply."""
        from bgate_adapters import kie

        self._sora_like()
        with pytest.raises(kie.KieError, match="no parameter for 'audio'"):
            kie.video_input("sora-like", audio=False)

    def test_a_registered_model_is_stamped_as_such(self, root):
        """Nothing may confuse a user's entry for a verified one."""
        assert self._sora_like()["source"] == "registered"
        from bgate_adapters import kie
        assert kie.video_capabilities("seedance-2")["source"] == "built-in"

    def test_a_spec_without_an_intent_map_is_refused(self, root):
        from bgate_adapters import kie

        with pytest.raises(kie.KieError, match="without intent"):
            kie.register_video_model("guess", {"model": "who/knows"})

    def test_the_model_lives_on_the_sequence(self, root):
        """A cutscene generated half on one model does not cut together."""
        self._sora_like()
        seq = cinematic.plan(root, "seq", _shots(1), model="sora-like")
        assert seq["model"] == "sora-like"

    def test_an_unregistered_model_is_refused_at_plan_time(self, root):
        """Before money moves, and naming the ones that exist."""
        with pytest.raises(cinematic.CinematicError, match="not a registered"):
            cinematic.plan(root, "seq", _shots(1), model="veo-9")

    def test_a_shot_the_model_cannot_generate_is_refused_before_spending(
            self, root, monkeypatch):
        """Seedance does 4-15s. A sora-like model at 20fps with a 40-frame
        floor cannot do 1 second, and that must surface before the upload."""
        self._sora_like()
        cinematic.plan(root, "seq", [{"action": "x", "duration": 15}],
                       model="sora-like")
        # 15s * 20fps = 300 frames, the ceiling — legal.
        from bgate_adapters import kie
        assert kie.video_input("sora-like", seconds=15)["n_frames"] == 300
        with pytest.raises(kie.KieError, match="must be 40"):
            kie.build_input("sora-like", prompt="a long enough prompt",
                            **kie.video_input("sora-like", seconds=1))


class TestRecovery:
    """A generation is charged at SUBMIT. Everything after — the poll loop, the
    download, this process surviving ten minutes — can fail while the provider
    holds a finished clip that has already been billed."""

    def test_a_shot_with_no_task_id_says_so_rather_than_guessing(self, root):
        cinematic.plan(root, "seq", _shots(1))
        with pytest.raises(cinematic.CinematicError, match="no task id"):
            cinematic.recover_shot(root, "seq", 1)

    @needs_theora
    def test_recovery_registers_the_clip_without_claiming_a_cost(
            self, root, monkeypatch):
        from bgate_adapters import kie

        cinematic.plan(root, "seq", _shots(1))
        seq = cinematic.sequence(root, "seq")
        cinematic._set_shot(root, seq["shots"][0]["id"], status="failed",
                            task_id="task-7")

        monkeypatch.setattr(kie, "poll", lambda *a, **k: {"state": "success"})
        monkeypatch.setattr(kie, "result_urls",
                            lambda rec: ["https://kie.test/v/7.mp4"])
        monkeypatch.setattr(kie, "download",
                            lambda url, out, **k: _clip(pathlib.Path(out)).stat().st_size)

        out = cinematic.recover_shot(root, "seq", 1)
        assert out["ok"] is True and out["task_id"] == "task-7"
        art = artifacts.get(root, out["artifact_id"])
        # No cost is claimed: the charge happened at submit, so a delta measured
        # now would be meaningless.
        assert art["metadata"]["credits_consumed"] is None
        assert cinematic.sequence(root, "seq")["shots"][0]["status"] == "generated"


class TestAudioAcrossModels:
    """Asking a model to turn OFF something it cannot do is not an error, and
    getting that wrong made every picture-only model unusable the moment audio
    defaulted to off. Caught end to end, not by a unit test."""

    @pytest.fixture(autouse=True)
    def _no_leak(self):
        from bgate_adapters import kie

        before = dict(kie.MODELS)
        yield
        kie.MODELS.clear()
        kie.MODELS.update(before)
        kie._refresh_model_kinds()

    def _mute_model(self):
        from bgate_adapters import kie

        return kie.register_video_model("mute", {
            "model": "vendor/mute-video",
            "intent": {"seconds": "duration"},
            "ranges": {"duration": (4, 10)},
        })

    @needs_theora
    def test_audio_off_is_satisfied_by_a_model_that_makes_none(
            self, root, monkeypatch):
        calls = TestGeneratingAShot()._stub(monkeypatch)
        self._mute_model()
        cinematic.plan(root, "seq", _shots(1), model="mute")
        out = cinematic.generate_shot(root, "seq", 1)
        assert out["ok"] is True, out.get("error")
        # Not sent at all — there is no field, and "do not make audio" is
        # already true of a model that makes none.
        assert "audio" not in calls[0]
        # Advisory settings this model cannot express are dropped and REPORTED,
        # never silently swallowed.
        assert set(out["unsupported"]["dropped"]) == {"quality", "shape"}

    def test_asking_FOR_audio_a_model_cannot_make_is_refused(self, root,
                                                             monkeypatch):
        """The asymmetry: this IS something the caller wanted and will not get."""
        monkeypatch.setattr(cinematic, "ffmpeg_status",
                            lambda: {"ok": True, "reason": "", "probed": True})
        self._mute_model()
        cinematic.plan(root, "seq", _shots(1), model="mute")
        out = cinematic.generate_shot(root, "seq", 1, generate_audio=True)
        assert out["ok"] is False and out["stage"] == "model"
        assert "cannot generate audio" in out["error"]
        assert "Nothing has been charged" in out["error"]


class TestProvenance:
    @needs_theora
    def test_the_recorded_prompt_is_the_one_that_was_sent(self, root,
                                                          monkeypatch):
        """A revision whose stored prompt does not reproduce the clip beside it
        is a lie, and the prompt is the only record of what was asked for."""
        calls = TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1), style="noir")
        out = cinematic.generate_shot(root, "seq", 1)
        art = artifacts.get(root, out["artifact_id"])
        assert art["prompt"] == calls[0]["prompt"]
        assert "film noir" in art["prompt"]

    @needs_theora
    def test_the_style_and_model_are_on_the_revision(self, root, monkeypatch):
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1), style="vhs")
        out = cinematic.generate_shot(root, "seq", 1)
        meta = artifacts.get(root, out["artifact_id"])["metadata"]
        assert meta["sequence"] == "seq" and meta["shot_idx"] == 1


class TestPostProduction:
    """The five things that turn a folder of clips into a cutscene, wired into
    the sequence rather than tested standalone (that is test_cinecut.py)."""

    def _bed(self, root, seconds=8):
        path = root / "game" / "assets" / "audio" / "music" / "theme.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=220:duration={seconds}", str(path)],
            check=True, capture_output=True)
        return "game/assets/audio/music/theme.mp3"

    def _generated(self, root, monkeypatch, **plan):
        TestGeneratingAShot()._stub(monkeypatch)
        seq = cinematic.plan(root, "seq", **plan)
        for shot in seq["shots"]:
            out = cinematic.generate_shot(root, "seq", shot["idx"],
                                          previs_ok=True)
            cinematic.keep(root, out["artifact_id"], actor="human")
        return cinematic.sequence(root, "seq")

    def test_a_transition_is_stored_and_validated(self, root):
        seq = cinematic.plan(root, "seq", [
            {"action": "a", "duration": 5},
            {"action": "b", "duration": 5, "transition": "dissolve",
             "transition_s": 1.0}])
        assert [s["transition"] for s in seq["shots"]] == ["cut", "dissolve"]

    def test_an_unknown_transition_is_refused_at_plan_time(self, root):
        with pytest.raises(cinematic.CinematicError, match="swirl"):
            cinematic.plan(root, "seq", [{"action": "a", "duration": 5,
                                          "transition": "swirl"}])

    def test_a_handle_longer_than_its_shot_is_refused(self, root):
        """xfade's offset goes negative and ffmpeg still exits 0, having
        dropped a beat."""
        with pytest.raises(cinematic.CinematicError, match="cannot be as long"):
            cinematic.plan(root, "seq", [
                {"action": "a", "duration": 5},
                {"action": "b", "duration": 4, "transition": "fade",
                 "transition_s": 4}])

    @needs_theora
    def test_a_cut_with_no_bed_says_it_is_silent(self, root, monkeypatch):
        """Three documents described "the audio seat scores it over the top"
        while every assembled cut shipped mute. Never silent about silence."""
        self._generated(root, monkeypatch, shots=_shots(1))
        out = cinematic.assemble(root, "seq")
        assert "silent" in out["audio"]["note"].lower()

    @needs_theora
    def test_a_bed_is_laid_under_the_picture(self, root, monkeypatch):
        bed = self._bed(root)
        self._generated(root, monkeypatch, shots=_shots(1), audio_track=bed,
                        audio_gain_db=-6)
        out = cinematic.assemble(root, "seq")
        assert out["audio"]["track"] == bed
        probe = subprocess.run(
            [shutil.which("ffprobe"), "-v", "error", "-show_entries",
             "stream=codec_name", "-of", "csv=p=0", str(root / out["path"])],
            capture_output=True, text=True, check=True)
        assert "vorbis" in probe.stdout and "theora" in probe.stdout

    @needs_theora
    def test_a_bed_that_is_not_on_disk_refuses_rather_than_shipping_mute(
            self, root, monkeypatch):
        self._generated(root, monkeypatch, shots=_shots(1),
                        audio_track="game/assets/audio/music/ghost.mp3")
        with pytest.raises(cinematic.CinematicError, match="not on disk"):
            cinematic.assemble(root, "seq")

    @needs_theora
    def test_dialogue_becomes_caption_files(self, root, monkeypatch):
        self._generated(root, monkeypatch, shots=[
            {"action": "a", "duration": 5, "dialogue": "We are the first."},
            {"action": "b", "duration": 5, "dialogue": "Nobody has been here."}])
        out = cinematic.assemble(root, "seq")
        assert out["captions"]["lines"] == 2
        srt = (root / out["captions"]["srt"]).read_text(encoding="utf-8")
        assert "We are the first." in srt
        data = json.loads((root / out["captions"]["json"]).read_text())
        assert data[0]["end"] <= data[1]["start"]      # never stacked

    @needs_theora
    def test_the_recorded_runtime_is_measured_not_summed(self, root,
                                                         monkeypatch):
        """A transition overlaps both shots, so the sum of the durations is
        longer than the cut."""
        self._generated(root, monkeypatch, shots=[
            {"action": "a", "duration": 5},
            {"action": "b", "duration": 5, "transition": "dissolve",
             "transition_s": 1.0}])
        out = cinematic.assemble(root, "seq")
        art = artifacts.get(root, out["artifact_id"])
        assert art["metadata"]["planned_runtime_s"] == 9.0
        assert art["metadata"]["transitions"] == ["dissolve"]

    @needs_theora
    def test_continuity_needs_two_shots_and_then_measures(self, root,
                                                          monkeypatch):
        cinematic.plan(root, "seq", _shots(1))
        assert "no join to check" in cinematic.check_continuity(root, "seq")["note"]
        self._generated(root, monkeypatch, shots=_shots(2))
        got = cinematic.check_continuity(root, "seq")
        assert len(got["joins"]) == 1
        assert "luma_delta" in got["joins"][0]


class TestDelivery:
    """An .ogv in the project is a file. A cutscene is a scene, a script and a
    contract with the caller."""

    def _assembled(self, root, monkeypatch, **plan):
        TestGeneratingAShot()._stub(monkeypatch)
        seq = cinematic.plan(root, "seq", **plan)
        for shot in seq["shots"]:
            out = cinematic.generate_shot(root, "seq", shot["idx"],
                                          previs_ok=True)
            cinematic.keep(root, out["artifact_id"], actor="human")
        cut = cinematic.assemble(root, "seq")
        cinematic.keep(root, cut["artifact_id"], actor="human")
        return cut

    def test_delivery_before_assembly_says_so(self, root):
        cinematic.plan(root, "seq", _shots(1))
        with pytest.raises(cinematic.CinematicError, match="not been assembled"):
            cinematic.deliver(root, "seq")

    @needs_theora
    def test_delivery_before_keeping_says_so(self, root, monkeypatch):
        """A scene pointing at a file that is not in the project would not load."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        cinematic.keep(root, out["artifact_id"], actor="human")
        cinematic.assemble(root, "seq")
        with pytest.raises(cinematic.CinematicError, match="has not been kept"):
            cinematic.deliver(root, "seq")

    @needs_theora
    def test_the_scene_script_and_captions_land_in_the_project(
            self, root, monkeypatch):
        self._assembled(root, monkeypatch, shots=[
            {"action": "a", "duration": 5, "dialogue": "hello"}])
        got = cinematic.deliver(root, "seq", actor="human")
        for key in ("scene", "script", "captions", "video"):
            assert (root / got[key]).is_file(), (key, got[key])
        # The translator's file travels too — left in a gitignored scratch dir
        # is how localisation finds out the captions were never versioned.
        assert (root / got["scene"]).with_suffix(".srt").is_file()

    @needs_theora
    def test_the_scene_points_at_the_real_installed_video(self, root,
                                                          monkeypatch):
        self._assembled(root, monkeypatch, shots=_shots(1))
        got = cinematic.deliver(root, "seq")
        text = (root / got["scene"]).read_text(encoding="utf-8")
        assert got["video"].endswith(".ogv")
        assert Path(got["video"]).name in text

    @needs_theora
    def test_it_hands_gameplay_three_lines_that_name_the_real_scene(
            self, root, monkeypatch):
        self._assembled(root, monkeypatch, shots=_shots(1))
        got = cinematic.deliver(root, "seq")
        assert got["scene_res"] in got["usage"]
        assert "await cut.finished" in got["usage"]

    @needs_theora
    def test_a_hand_edited_script_is_not_overwritten(self, root, monkeypatch):
        """The .gd is meant to be edited. A second delivery silently reverting
        those edits would be this product destroying a user's work."""
        self._assembled(root, monkeypatch, shots=_shots(1))
        got = cinematic.deliver(root, "seq")
        script = root / got["script"]
        script.write_text("# MINE\nextends CanvasLayer\n", encoding="utf-8")

        again = cinematic.deliver(root, "seq")
        assert again["script_kept"] is True
        assert script.read_text(encoding="utf-8").startswith("# MINE")

        forced = cinematic.deliver(root, "seq", force=True)
        assert forced["script_kept"] is False
        assert cinematic.GENERATED_MARK in script.read_text(encoding="utf-8")

    @needs_theora
    def test_a_generated_script_is_refreshed_without_force(self, root,
                                                           monkeypatch):
        """Only HAND edits are protected — an untouched generated file must
        pick up a corrected path."""
        self._assembled(root, monkeypatch, shots=_shots(1))
        cinematic.deliver(root, "seq")
        again = cinematic.deliver(root, "seq")
        assert again["script_kept"] is False

    @needs_theora
    def test_the_node_name_is_a_legal_godot_identifier(self, root, monkeypatch):
        """Godot node names take no dots, slashes or colons, and a slug has
        hyphens."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "The Long Fall", _shots(1))
        out = cinematic.generate_shot(root, "The Long Fall", 1)
        cinematic.keep(root, out["artifact_id"], actor="human")
        cut = cinematic.assemble(root, "The Long Fall")
        cinematic.keep(root, cut["artifact_id"], actor="human")
        got = cinematic.deliver(root, "The Long Fall")
        text = (root / got["scene"]).read_text(encoding="utf-8")
        assert '[node name="TheLongFall" type="CanvasLayer"]' in text


class TestWhatReachesTheEngineProject:
    """The first build transcoded EVERY kept shot into the game. Nothing
    references those — the game loads the assembled cut, and assemble() reads
    the .mp4 candidates directly — so it was a Theora encode per shot and, at
    1080p, tens of megabytes each of files nobody asked for."""

    @needs_theora
    def test_a_kept_shot_stays_out_of_the_engine_project(self, root,
                                                         monkeypatch):
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        kept = cinematic.keep(root, out["artifact_id"], actor="human")

        assert kept["installed_to_engine"] is False
        assert kept["install"] == {}
        installed = root / "game" / "assets" / "cinematics"
        assert not installed.exists() or not list(installed.glob("*.ogv"))

    @needs_theora
    def test_the_assembled_cut_does_reach_the_engine_project(self, root,
                                                             monkeypatch):
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(2))
        for idx in (1, 2):
            out = cinematic.generate_shot(root, "seq", idx,
                                          previs_ok=True)
            cinematic.keep(root, out["artifact_id"], actor="human")
        cut = cinematic.assemble(root, "seq")
        kept = cinematic.keep(root, cut["artifact_id"], actor="human")

        assert kept["installed_to_engine"] is True
        assert kept["install"]["path"].endswith(".ogv")
        # Exactly one .ogv in the project: the cutscene, not the intermediates.
        found = sorted((root / "game" / "assets" / "cinematics").glob("*.ogv"))
        assert [f.name for f in found] == ["seq.ogv"], found

    @needs_theora
    def test_a_shot_can_still_be_installed_deliberately(self, root,
                                                        monkeypatch):
        """One real case: a single clip used alone as an attract loop or a
        sting, with no cut around it."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        kept = cinematic.keep(root, out["artifact_id"], actor="human",
                              install_to_engine=True)
        assert kept["installed_to_engine"] is True
        assert (root / kept["install"]["path"]).is_file()

    @needs_theora
    def test_a_shot_is_still_approved_and_assemblable(self, root, monkeypatch):
        """Not installing must not mean not kept — assemble refuses on any
        shot that is not kept."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        cinematic.keep(root, out["artifact_id"], actor="human")
        assert cinematic.sequence(root, "seq")["shots"][0]["status"] == "kept"
        assert cinematic.assemble(root, "seq")["ok"] is True


class TestPathContainment:
    """CodeQL called this "uncontrolled data used in path expression", HIGH, and
    it was right — but the severity is worse than the label suggests.

    These paths are not merely READ. A conditioning frame is handed to
    kie.upload_file, which reads the bytes and POSTs them to a third party, so a
    path escaping the project root is exfiltration off the machine rather than
    local file disclosure. And the callers are the dashboard body and the MCP
    tool arguments — which means an AGENT, and constraining what an agent can
    reach is the whole premise of the seat and lane system.
    """

    ESCAPE = "../../../../../../etc/passwd"

    def test_a_traversal_in_a_style_ref_is_refused(self, root):
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.plan(root, "leak", _shots(1), style_refs=[self.ESCAPE])

    @pytest.mark.parametrize("field", ["first_frame", "last_frame", "vo"])
    def test_a_traversal_on_a_shot_is_refused_at_plan_time(self, root, field):
        """The earliest refusal is the useful one — the shot list is what a
        human reviews, and a path that can never work should not survive review
        looking legitimate."""
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.plan(root, "leak",
                           [{"action": "a", "duration": 5, field: self.ESCAPE}])

    def test_a_traversal_in_refs_is_refused(self, root):
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.plan(root, "leak",
                           [{"action": "a", "duration": 5,
                             "refs": [self.ESCAPE]}])

    def test_an_absolute_path_is_refused(self, root):
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.project_path(root, "/etc/passwd")

    def test_keyframes_for_refuses_too_as_the_last_gate(self, root):
        """A shot row can be written by something other than plan(), and this is
        the last gate before an upload."""
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.keyframes_for(root, {"first_frame": self.ESCAPE,
                                           "refs": []})

    def test_an_escaping_audio_bed_is_refused_at_plan_time(self, root):
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.plan(root, "seq", _shots(1), audio_track=self.ESCAPE)

    @needs_theora
    def test_an_escaping_audio_bed_is_refused_at_assembly_too(self, root,
                                                              monkeypatch):
        """The last gate before ffmpeg reads it — a sequence row can be written
        by something other than plan()."""
        TestGeneratingAShot()._stub(monkeypatch)
        cinematic.plan(root, "seq", _shots(1))
        out = cinematic.generate_shot(root, "seq", 1)
        cinematic.keep(root, out["artifact_id"], actor="human")
        with db.tx(root) as conn:
            conn.execute("UPDATE cine_sequence SET audio_track = ?",
                         (self.ESCAPE,))
        with pytest.raises(cinematic.CinematicError, match="outside the project"):
            cinematic.assemble(root, "seq")

    def test_legitimate_paths_still_work(self, root):
        """The point is containment, not refusing everything with a slash."""
        (root / "art").mkdir(exist_ok=True)
        (root / "art" / "hero.png").write_bytes(b"\x89PNG")
        assert cinematic.project_path(root, "art/hero.png") == "art/hero.png"
        assert cinematic.project_path(
            root, "https://x.test/a.png") == "https://x.test/a.png"
        assert cinematic.project_path(root, "") == ""
        seq = cinematic.plan(root, "fine", _shots(1, refs=["art/hero.png"]))
        assert seq["shots"][0]["refs"] == ["art/hero.png"]

    def test_a_path_that_merely_looks_like_a_traversal_but_stays_inside_is_kept(
            self, root):
        """`game/../art/x.png` resolves inside the project and is legal — a
        check that refused on the presence of `..` would be refusing shape
        rather than destination."""
        (root / "art").mkdir(exist_ok=True)
        (root / "art" / "hero.png").write_bytes(b"\x89PNG")
        assert cinematic.project_path(
            root, "game/../art/hero.png") == "art/hero.png"


class TestTheEncoderIsWorkingNotMerelyPresent:
    """The check that was missing, and the defect it was missing FOR.

    Every cutscene this product ever shipped was a structurally corrupt Ogg
    Theora file: the installed clip of a real game decoded 14 of its 193 frames
    and the other 179 threw `error in unpack_block_qpis`, extracting as flat
    green rectangles. The build was Gyan.FFmpeg 8.1.1 from winget, whose
    libtheora writes malformed bitstreams (GyanD/codexffmpeg#200); the same
    version from BtbN is fine.

    Nothing here could notice, because ffmpeg_status() decided the build was
    usable with `"libtheora" in listed` — presence, never function. So these
    tests pin FUNCTION, and every subprocess is stubbed on purpose: a test that
    read the real ffmpeg on this machine would pass for the wrong reason today
    and silently stop testing anything the day somebody reinstalls it.
    """

    EXE = "/fake/bin/ffmpeg"

    def _ffmpeg(self, monkeypatch, *, exe="", decode_errors=0,
                encoders="libtheora libvorbis", encode_ok=True):
        """Stand in for a whole ffmpeg build, and count what got asked of it.

        ``decode_errors`` is the number of stderr lines the DECODE reports,
        which is the entire signal: a healthy build round-trips in silence.
        """
        exe = exe or self.EXE
        calls = []
        monkeypatch.setattr(cinematic, "_ROUNDTRIP", {})
        monkeypatch.setattr(shutil, "which", lambda name: exe)
        # AND NO ~/.bgate/bin, WHICH OUTRANKS PATH. `ffmpegbin.resolve` prefers
        # a binary deliberately placed there, and it finds it by reading the
        # filesystem rather than through shutil.which — so on a machine that has
        # one (this feature's whole purpose is that developers WILL have one)
        # the stub above would be silently outranked and every assertion here
        # would be about the developer's real ffmpeg instead of the fake.
        monkeypatch.setattr(cinematic._ffmpegbin, "local_bin", lambda: None)

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            if "-encoders" in cmd:
                return _Done(0, encoders)
            if "null" in cmd:
                return _Done(0, "", _decode_noise(decode_errors))
            if not encode_ok:
                return _Done(1, "", "Unknown encoder 'libtheora'")
            Path(cmd[-1]).write_bytes(b"OggS-not-really")
            return _Done(0)

        monkeypatch.setattr(cinematic.subprocess, "run", fake_run)
        return calls

    def _probes(self, calls):
        return [c for c in calls if "lavfi" in " ".join(c)]

    def test_a_working_build_passes(self, monkeypatch):
        """The round trip must not fail a healthy encoder — a check that cries
        wolf gets stubbed out by the first person it inconveniences."""
        self._ffmpeg(monkeypatch, decode_errors=0)
        status = cinematic.ffmpeg_status()
        assert status["ok"] is True
        assert status["theora"] is True and status["probed"] is True
        assert status["roundtrip"]["ok"] is True
        assert not status["reason"]

    def test_a_build_whose_output_will_not_decode_is_refused(self, monkeypatch):
        """THE DEFECT. libtheora is listed, the encode exits 0, and what comes
        out cannot be read back. `-encoders` cannot see this, which is why the
        product shipped green rectangles under a green doctor."""
        self._ffmpeg(monkeypatch, decode_errors=35)
        status = cinematic.ffmpeg_status()
        assert status["ok"] is False
        # The encoder IS there. Reporting this as "no libtheora" would send the
        # reader to install something they already have.
        assert status["theora"] is True and status["probed"] is True
        assert status["roundtrip"]["errors"] == 35

    def test_the_refusal_names_the_build_and_the_remedy_not_a_setting(
            self, monkeypatch):
        """Quality, frame size and encoder threading were all ruled out by
        experiment. An error that leaves that open costs the reader an evening
        of tuning knobs that cannot help."""
        self._ffmpeg(monkeypatch, decode_errors=35)
        reason = cinematic.ffmpeg_status()["reason"]
        assert "35" in reason
        assert "#200" in reason
        assert "BtbN" in reason
        assert "not a settings problem" in reason.lower()

    def test_the_three_states_read_differently(self, monkeypatch):
        """No ffmpeg / no libtheora / libtheora that lies are three different
        situations wanting three different sentences."""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(cinematic._ffmpegbin, "local_bin", lambda: None)
        absent = cinematic.ffmpeg_status()["reason"]

        self._ffmpeg(monkeypatch, encoders="libvorbis libx264")
        unbuilt = cinematic.ffmpeg_status()["reason"]

        self._ffmpeg(monkeypatch, decode_errors=35)
        broken = cinematic.ffmpeg_status()["reason"]

        assert "not found on PATH" in absent
        assert "without libtheora" in unbuilt
        assert "broken files" in broken
        assert len({absent, unbuilt, broken}) == 3

    def test_a_build_that_cannot_encode_at_all_is_not_called_corrupt(
            self, monkeypatch):
        """A failed probe encode is the OLD failure (no usable libtheora), not
        the new one, and it keeps its own branch rather than being reported as a
        build that writes files nothing can read."""
        self._ffmpeg(monkeypatch, encode_ok=False)
        status = cinematic.ffmpeg_status()
        assert status["ok"] is False
        assert status["roundtrip"]["errors"] == 0

    def test_the_probe_is_cached_per_process(self, monkeypatch):
        """It costs an encode and a decode of a second of video (~256ms). Run on
        every ffmpeg_status() it would be paid by options(), by every doctor
        sweep and before every generation."""
        calls = self._ffmpeg(monkeypatch, decode_errors=0)
        for _ in range(4):
            assert cinematic.ffmpeg_status()["ok"] is True
        assert len(self._probes(calls)) == 1

    def test_the_cache_is_keyed_on_the_executable_not_global(self, monkeypatch):
        """A global flag would be wrong the moment somebody follows the remedy
        this module prints, puts a working build ahead of the broken one on
        PATH, and asks the running dashboard again."""
        broken = self._ffmpeg(monkeypatch, exe="/fake/broken/ffmpeg",
                              decode_errors=35)
        assert cinematic.ffmpeg_status()["ok"] is False
        assert len(self._probes(broken)) == 1

        # A NEW PATH. The previous binary's answer is left in the cache, still
        # true of that binary, and the new one is probed on its own account.
        good = []
        monkeypatch.setattr(shutil, "which", lambda name: "/fake/good/ffmpeg")
        monkeypatch.setattr(cinematic._ffmpegbin, "local_bin", lambda: None)

        def fake_run(cmd, **kw):
            good.append(list(cmd))
            if "-encoders" in cmd:
                return _Done(0, "libtheora libvorbis")
            if "null" in cmd:
                return _Done(0)
            Path(cmd[-1]).write_bytes(b"OggS")
            return _Done(0)

        monkeypatch.setattr(cinematic.subprocess, "run", fake_run)
        assert cinematic.ffmpeg_status()["ok"] is True
        assert len(self._probes(good)) == 1
        assert cinematic._ROUNDTRIP["/fake/broken/ffmpeg"]["ok"] is False
        assert cinematic._ROUNDTRIP["/fake/good/ffmpeg"]["ok"] is True


class TestTranscodeVerifiesItsOwnOutput:
    """The build-level probe is not enough by itself: it asks one question about
    the BUILD, once, on synthetic video. transcode() writes the file that is
    actually installed into somebody's game, and an encoder's exit code is only
    its opinion of its own work."""

    def _source(self, root):
        src = Path(root) / ".bgate_out" / "shot.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"not really an mp4")
        return src

    def _ffmpeg(self, monkeypatch, *, decode_errors=0):
        monkeypatch.setattr(cinematic, "ffmpeg_status",
                            lambda: {"ok": True, "reason": "", "probed": True,
                                     "theora": True, "ffmpeg": "/fake/ffmpeg"})

        def fake_run(cmd, **kw):
            if "null" in cmd:
                return _Done(0, "", _decode_noise(decode_errors))
            Path(cmd[-1]).write_bytes(b"OggS-output")
            return _Done(0)

        monkeypatch.setattr(cinematic.subprocess, "run", fake_run)

    def test_a_clip_that_decodes_is_returned_and_says_it_was_verified(
            self, root, monkeypatch):
        self._ffmpeg(monkeypatch, decode_errors=0)
        out = cinematic.transcode(self._source(root), root / "out.ogv")
        assert out["verified"] == {"decoded": True, "errors": 0}
        assert (root / "out.ogv").is_file()

    def test_a_corrupt_output_raises_rather_than_being_returned(
            self, root, monkeypatch):
        """ffmpeg exits 0 and writes a plausible file. Returning it is how 179
        unreadable frames reached a shipped game."""
        self._ffmpeg(monkeypatch, decode_errors=179)
        with pytest.raises(cinematic.CinematicError) as exc:
            cinematic.transcode(self._source(root), root / "out.ogv")
        text = str(exc.value)
        # What was measured, and what it means for the delivery.
        assert "179" in text
        assert "NOT installed" in text

    def test_the_corrupt_file_is_not_left_looking_like_a_deliverable(
            self, root, monkeypatch):
        """A half-written .ogv sitting at the destination path is what a later
        run — or a human reading the directory — takes for a converted clip."""
        self._ffmpeg(monkeypatch, decode_errors=12)
        with pytest.raises(cinematic.CinematicError):
            cinematic.transcode(self._source(root), root / "out.ogv")
        assert not (root / "out.ogv").exists()


class TestPrevisIsWatchedBeforeItIsBought:
    """The stage that had nothing in it.

    plan() wrote a shot list and generate_shot bought a clip, so the first time
    anyone saw the EDIT was after paying for all of it. These pin the gate that
    closes that, including the half that actually matters: an animatic built
    before the shot list changed describes a scene nobody is buying.
    """

    def _seq(self, root, shots=2):
        cinematic.plan(root, "scene", [
            {"action": f"beat {i}", "duration": 5} for i in range(shots)])
        return cinematic.sequence(root, "scene")

    def test_generation_is_refused_before_any_animatic_exists(self, root):
        got = cinematic.generate_shot(root, "scene", 1) if self._seq(root) else None
        assert got["ok"] is False
        assert got["stage"] == "previs"
        assert "cinematic_animatic" in got["error"]
        # The refusal has to say the money is safe, like every other refusal in
        # this module — a caller that thinks it was charged re-checks instead of
        # acting.
        assert "Nothing has been charged" in got["error"]

    def test_a_one_shot_sequence_is_exempt(self, root):
        """There is no edit in one shot, and an audit that fires on everything
        is one somebody switches off."""
        state = cinematic.previs_state(root, self._seq(root, shots=1))
        assert state["ok"] is True and state["built"] is False

    def test_an_animatic_older_than_the_shot_list_does_not_count(
            self, root, tmp_path, monkeypatch):
        """THE HALF THAT MATTERS. 'An animatic exists' is a weak claim: one
        built before three shots were re-ordered is worse than none, because it
        is the reason somebody believes the edit was checked."""
        import datetime as dt

        seq = self._seq(root)
        reel = root / "design" / "cinematics" / "animatics" / "scene.mp4"
        reel.parent.mkdir(parents=True, exist_ok=True)
        reel.write_bytes(b"reel")

        fresh = cinematic.previs_state(root, seq)
        assert fresh["ok"] is True and fresh["stale"] is False

        # The shot list is edited AFTER the reel was cut.
        later = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                 + dt.timedelta(hours=1)).isoformat(" ", "seconds")
        seq = {**seq, "updated_at": later}
        stale = cinematic.previs_state(root, seq)
        assert stale["stale"] is True and stale["ok"] is False
        assert "since been changed" in stale["note"]

    def test_previs_ok_is_the_deliberate_override(self, root):
        """A re-roll of one shot in a watched sequence should not have to
        rebuild the reel — but skipping previs must be something typed."""
        self._seq(root)
        got = cinematic.generate_shot(root, "scene", 1, previs_ok=True)
        assert got.get("stage") != "previs"

    def test_an_unparseable_timestamp_does_not_block_spending(self, root):
        """Refusing to generate over a date format would be a worse failure
        than the one this guards."""
        seq = {**self._seq(root), "updated_at": "not a date"}
        reel = root / "design" / "cinematics" / "animatics" / "scene.mp4"
        reel.parent.mkdir(parents=True, exist_ok=True)
        reel.write_bytes(b"reel")
        assert cinematic.previs_state(root, seq)["ok"] is True


class TestTheSetReferenceEviction:
    """Seedance takes an anchor frame OR reference images, never both.

    _fit_intent resolves that by keeping first_frame, which is right for the
    CAST and exactly wrong for the SET: `refs` is the only slot a location plate
    can occupy, so the better a shot is anchored the more completely it loses
    the ability to be told what room it is in.
    """

    def test_a_first_frame_evicts_the_plates(self):
        wanted = {"seconds": 5, "first_frame": "/x/still.png",
                  "last_frame": None, "refs": ["/x/plate.png"], "audio": False}
        intent, dropped, refusal = cinematic._fit_intent("seedance-2", wanted)
        assert not refusal
        assert dropped == ["refs"]
        assert not intent.get("refs")
        assert intent.get("first_frame")

    def test_without_an_anchor_the_plates_survive(self):
        wanted = {"seconds": 5, "first_frame": None, "last_frame": None,
                  "refs": ["/x/plate.png"], "audio": False}
        intent, dropped, _ = cinematic._fit_intent("seedance-2", wanted)
        assert dropped == [] and intent.get("refs")
