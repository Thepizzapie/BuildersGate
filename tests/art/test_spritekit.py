"""The arithmetic half of sprite consistency — registration, palette, motion, layout.

Everything here is deterministic PIL. No model, no API, no Blender: these are the
checks that are supposed to be true of a sheet whoever painted it, so a test that
needed a provider would be testing the wrong thing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bgate_adapters import sprites
from bgate_core.art import animspec, spritekit


def _figure(path, *, arm_box, torso=(200, 100, 300, 550), size=(500, 600),
            colors=((200, 60, 40), (60, 90, 200))):
    """A stick figure: an identical torso, and one arm that moves between frames.

    The torso is the SAME PIXELS in every frame these tests build, so any
    apparent movement of the torso in the assembled output is registration error
    and nothing else.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle(torso, fill=colors[0] + (255,))
    if arm_box:
        draw.rectangle(arm_box, fill=colors[-1] + (255,))
    img.save(path)
    return str(path)


def _torso_centre(path, width=160):
    """Where the torso sits in an assembled cell. None if it is not there."""
    from PIL import Image

    mask = (Image.open(path).convert("RGBA").getchannel("A")
            .point(lambda a: 255 if a >= 60 else 0))
    columns = list(mask.resize((width, 1), Image.BOX).getdata())
    tall = [x for x, value in enumerate(columns) if value > 150]
    return (min(tall) + max(tall)) / 2 if tall else None


class TestRegistration:
    """The claim in spritekit.anchor_x's docstring, kept honest.

    The docstring quotes four numbers for four strategies. If someone changes the
    anchor and the ordering inverts, that table becomes a lie and this fails.
    """

    def _drift(self, tmp_path, monkeypatch, strategy):
        real_anchor = spritekit.anchor_x
        real_place = spritekit.place_offset
        if strategy == "bbox":
            monkeypatch.setattr(spritekit, "anchor_x",
                                lambda img, robust=True: img.width / 2.0)
            monkeypatch.setattr(
                spritekit, "place_offset",
                lambda fw, fh, sw, sh, cx, ground=0, lift=0:
                    ((fw - sw) // 2, fh - ground - sh - lift))
        elif strategy == "centroid":
            monkeypatch.setattr(spritekit, "anchor_x",
                                lambda img, robust=True: real_anchor(img, robust=False))
        # "core" is the shipped default and is left unpatched.

        folded = _figure(tmp_path / f"{strategy}_0.png", arm_box=(160, 200, 300, 240))
        thrown = _figure(tmp_path / f"{strategy}_1.png", arm_box=(300, 200, 440, 240))
        got = sprites.from_pose_images(
            [("a/0", folded), ("a/1", thrown)],
            out_dir=str(tmp_path / f"out_{strategy}"), name="t",
            frame_size=(160, 240))
        assert got["ok"] is True, got
        centres = [_torso_centre(got["frames"][p]) for p in ("a/0", "a/1")]
        assert all(c is not None for c in centres), centres
        monkeypatch.setattr(spritekit, "anchor_x", real_anchor)
        monkeypatch.setattr(spritekit, "place_offset", real_place)
        return abs(centres[0] - centres[1])

    def test_core_column_anchor_beats_every_alternative(self, tmp_path, monkeypatch):
        """An outstretched arm must not move the body it is attached to."""
        box = self._drift(tmp_path, monkeypatch, "bbox")
        centroid = self._drift(tmp_path, monkeypatch, "centroid")
        core = self._drift(tmp_path, monkeypatch, "core")

        # The failure this whole mechanism exists to fix: box-centring slides the
        # torso by a large fraction of the limb's reach.
        assert box > 20, f"the synthetic stopped exercising the bug (box={box})"
        assert centroid < box, (centroid, box)
        assert core < centroid, (core, centroid)
        # And the shipped one has to be good, not merely best.
        assert core <= 2.0, f"registration drifted {core}px"

    def test_the_anchor_ignores_a_limb_but_follows_the_body(self):
        """Both halves matter. An anchor that ignored everything would also
        'ignore the arm', and would be wrong the moment the character leans."""
        from PIL import Image, ImageDraw

        def build(torso_x, arm):
            img = Image.new("RGBA", (300, 400), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rectangle((torso_x, 0, torso_x + 60, 399), fill=(200, 60, 40, 255))
            if arm:
                draw.rectangle(arm, fill=(200, 60, 40, 255))
            return img

        centred = spritekit.anchor_x(build(120, None))
        with_arm = spritekit.anchor_x(build(120, (180, 100, 290, 130)))
        assert abs(centred - with_arm) < 3, "the arm dragged the anchor"

        leaned = spritekit.anchor_x(build(160, None))
        assert leaned - centred == pytest.approx(40, abs=3), \
            "the anchor did not follow the body when the body actually moved"

    def test_the_mean_is_dragged_where_the_median_is_not(self):
        """The reason the default is not the textbook centroid."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (241, 451), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 100, 450), fill=(200, 60, 40, 255))     # torso, centre 50
        draw.rectangle((100, 100, 240, 140), fill=(200, 60, 40, 255))  # arm
        mean = spritekit.anchor_x(img, robust=False)
        core = spritekit.anchor_x(img, robust=True)
        assert mean > 60, mean            # dragged well off the torso
        assert abs(core - 50) < 3, core   # still on it


class TestPaletteLock:
    def test_a_drifted_frame_is_pulled_back_onto_the_reference_palette(self, tmp_path):
        from PIL import Image

        ref = tmp_path / "ref.png"
        Image.new("RGBA", (64, 64), (200, 40, 40, 255)).save(ref)

        drifted = tmp_path / "frame.png"
        Image.new("RGBA", (64, 64), (40, 200, 40, 255)).save(drifted)

        got = spritekit.lock_palette(drifted, spritekit.master_palette(ref, 8))
        assert got["ok"] is True, got
        # The green is not in the reference, so after locking it cannot be there.
        assert Image.open(drifted).convert("RGB").getpixel((0, 0)) == (200, 40, 40)
        assert got["changed"] == 1.0

    def test_locking_leaves_alpha_and_zeroes_rgb_under_it(self, tmp_path):
        """A quantiser has no opinion about invisible pixels and writes a colour
        into every one of them — which is the dirty alpha the chroma audit
        fails on."""
        from PIL import Image

        ref = tmp_path / "ref.png"
        Image.new("RGBA", (32, 32), (10, 120, 200, 255)).save(ref)

        frame = tmp_path / "f.png"
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        img.paste((250, 10, 10, 255), (8, 8, 24, 24))
        img.save(frame)

        spritekit.lock_palette(frame, spritekit.master_palette(ref, 8))
        out = Image.open(frame).convert("RGBA")
        assert out.getpixel((0, 0)) == (0, 0, 0, 0), "colour left under alpha 0"
        assert out.getpixel((16, 16))[3] == 255, "alpha was not preserved"

    def test_flat_art_is_recognised_and_painterly_art_is_not(self, tmp_path):
        """The measurement behind palette_lock='auto'. Locking flat art is free;
        locking a gradient bands it, and that is a downgrade nobody ordered."""
        from PIL import Image

        flat = tmp_path / "flat.png"
        img = Image.new("RGBA", (64, 64), (200, 40, 40, 255))
        img.paste((30, 30, 60, 255), (0, 0, 64, 20))
        img.save(flat)
        assert spritekit.looks_limited_palette(flat) is True

        smooth = tmp_path / "smooth.png"
        grad = Image.new("RGBA", (256, 64))
        for x in range(256):
            for y in range(64):
                grad.putpixel((x, y), (x, 255 - x, (x * 3) % 256, 255))
        grad.save(smooth)
        assert spritekit.looks_limited_palette(smooth) is False

    def test_locking_defringes_the_silhouette(self, tmp_path):
        """The halo bug a human caught on the first real conform: a dim edge
        blend snaps to whichever palette entry is nearest — sometimes a LIGHT
        one — and stray anti-aliasing flecks get dressed in a palette colour
        and become visible. Stray ink evaporates; a lone mismatched edge pixel
        takes its neighbours' colour; the interior is never touched."""
        from PIL import Image

        palette = [(200, 40, 40), (240, 240, 240)]
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        img.paste((200, 40, 40, 255), (8, 8, 24, 24))     # the body
        img.putpixel((2, 2), (20, 20, 25, 255))           # stray AA fleck
        img.putpixel((8, 8), (230, 230, 230, 255))        # halo pixel -> light
        frame = tmp_path / "f.png"
        img.save(frame)

        got = spritekit.lock_palette(frame, palette)
        assert got["ok"] is True
        out = Image.open(frame).convert("RGBA")
        assert out.getpixel((2, 2)) == (0, 0, 0, 0), "stray ink survived"
        assert out.getpixel((8, 8))[:3] == (200, 40, 40), "halo kept its colour"
        assert out.getpixel((16, 16))[:3] == (200, 40, 40), "interior edited"

    def test_no_reference_is_reported_rather_than_guessed(self, tmp_path):
        """Locking to the batch's own colours would average in whatever drifted,
        which is the opposite of the point."""
        files = [("a/0", _figure(tmp_path / "0.png", arm_box=None))]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "o"),
                                       name="t", palette_lock=True)
        assert got["ok"] is True
        assert got["palette"]["ok"] is False
        assert "no reference" in got["palette"]["note"]


class TestParts:
    def test_one_figure_reads_as_one_part(self, tmp_path):
        path = _figure(tmp_path / "one.png", arm_box=(300, 200, 440, 240))
        assert spritekit.parts(path)["parts"] == 1

    def test_a_detached_limb_is_counted(self, tmp_path):
        """The failure every existing audit walks past: the cut is clean, the
        alpha is crisp, nothing is hollow, and a hand is floating in space."""
        path = _figure(tmp_path / "two.png", arm_box=(380, 200, 460, 260))
        assert spritekit.parts(path)["parts"] == 2

    def test_speckles_are_separated_from_parts(self, tmp_path):
        """Confetti from a dirty key is a different problem with a different fix
        than a limb that came off, so it gets a different number."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle((150, 50, 250, 350), fill=(200, 60, 40, 255))
        for x in range(10, 120, 20):
            draw.rectangle((x, x, x + 2, x + 2), fill=(200, 60, 40, 255))
        path = tmp_path / "speckle.png"
        img.save(path)
        got = spritekit.parts(str(path))
        assert got["parts"] == 1
        assert got["speckles"] >= 3


class TestMotionReport:
    def _frames(self, tmp_path, boxes):
        return [(f"walk/{i}", _figure(tmp_path / f"m{i}.png", arm_box=box))
                for i, box in enumerate(boxes)]

    def test_identical_adjacent_frames_are_called_duplicates(self, tmp_path):
        frames = self._frames(tmp_path, [(300, 200, 440, 240)] * 2)
        got = spritekit.motion_report(frames, looping=False)
        assert [f["kind"] for f in got["findings"]] == ["duplicate"]
        assert got["flagged"] is True

    def test_a_cycle_that_does_not_close_is_flagged(self, tmp_path):
        """The fault that is cheap to miss and impossible to unsee: it hitches
        once per repetition, forever."""
        from PIL import Image, ImageDraw

        paths = []
        for i, x in enumerate((200, 230, 260, 500)):
            img = Image.new("RGBA", (700, 400), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle((x, 50, x + 120, 350),
                                          fill=(200, 60, 40, 255))
            p = tmp_path / f"c{i}.png"
            img.save(p)
            paths.append((f"run/{i}", str(p)))
        got = spritekit.motion_report(paths, looping=True)
        assert "open_loop" in [f["kind"] for f in got["findings"]]

    def test_a_one_shot_is_not_marked_for_failing_to_loop(self, tmp_path):
        """A death does not flow back into its first frame and must not be
        blamed for it."""
        from PIL import Image, ImageDraw

        paths = []
        for i, x in enumerate((200, 240, 600)):
            img = Image.new("RGBA", (800, 400), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle((x, 50, x + 120, 350),
                                          fill=(200, 60, 40, 255))
            p = tmp_path / f"d{i}.png"
            img.save(p)
            paths.append((f"death/{i}", str(p)))
        got = spritekit.motion_report(paths, looping=False)
        assert "open_loop" not in [f["kind"] for f in got["findings"]]

    def test_a_healthy_cycle_says_nothing(self, tmp_path):
        from PIL import Image, ImageDraw

        paths = []
        for i, x in enumerate((200, 215, 230, 215)):
            img = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle((x, 50, x + 150, 350),
                                          fill=(200, 60, 40, 255))
            p = tmp_path / f"h{i}.png"
            img.save(p)
            paths.append((f"walk/{i}", str(p)))
        got = spritekit.motion_report(paths, looping=True)
        assert got["findings"] == [], got["findings"]

    def test_a_one_shot_archetype_is_not_asked_why_it_does_not_loop(self, tmp_path):
        """NO_LOOP is a name-based rule and `timing` overrides it per animation.
        attack, hurt, cast and jump are one-shots that are in NO neither list, so
        without the override the loop check asks an attack why its recovery frame
        does not flow back into its wind-up."""
        from PIL import Image, ImageDraw

        # Frames must differ in SHAPE, not position — registration exists to
        # remove position, so four boxes at four x offsets assemble into four
        # identical cells. Three upright stances of slightly different height,
        # then a wide low lunge of about the same ink, so the wrap-around pair
        # overlaps far less than the adjacent ones do.
        boxes = [(190, 60, 310, 440), (190, 80, 310, 440), (190, 100, 310, 440),
                 (100, 300, 400, 440)]
        files = []
        for i, box in enumerate(boxes):
            img = Image.new("RGBA", (900, 500), (0, 0, 0, 0))
            ImageDraw.Draw(img).rectangle(box, fill=(200, 60, 40, 255))
            p = tmp_path / f"a{i}.png"
            img.save(p)
            files.append((f"attack/{i}", str(p)))

        loose = sprites.from_pose_images(files, out_dir=str(tmp_path / "loose"),
                                         name="t")
        assert "open_loop" in [f["kind"] for f
                               in loose["motion"]["animations"]["attack"]["findings"]]

        told = sprites.from_pose_images(files, out_dir=str(tmp_path / "told"),
                                        name="t",
                                        timing={"attack": {"loop": False}})
        assert "open_loop" not in [f["kind"] for f
                                   in told["motion"]["animations"]["attack"]["findings"]]

    def test_the_assembler_reports_motion_per_animation(self, tmp_path):
        files = [("idle/0", _figure(tmp_path / "i0.png", arm_box=(300, 200, 440, 240))),
                 ("idle/1", _figure(tmp_path / "i1.png", arm_box=(300, 200, 440, 240)))]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "o"), name="t")
        assert got["motion"]["flagged"] == ["idle"]
        assert got["motion"]["animations"]["idle"]["findings"][0]["kind"] == "duplicate"


class TestLayout:
    def test_a_short_set_is_still_a_plain_strip(self):
        """Every sheet and every region assertion in this project is a strip. A
        layout change nobody asked for is a re-import of every character."""
        plan = spritekit.layout(3, 160, 240)
        assert plan == {"columns": 3, "rows": 1, "pad": 0,
                        "width": 480, "height": 240}

    def test_a_long_set_wraps_instead_of_exceeding_the_texture_limit(self):
        """A texture over the device limit does not warn. It fails to upload and
        the sprite draws as nothing."""
        plan = spritekit.layout(60, 160, 240)
        assert plan["width"] <= spritekit.MAX_SHEET_PX
        assert plan["rows"] > 1
        assert plan["pad"] >= 1, "a gridded sheet has vertical neighbours to bleed from"

    def test_regions_and_pixels_agree_under_padding(self, tmp_path):
        """The one way a sheet and its .tres can disagree is if either recomputes
        the geometry, so the layout travels from the stitcher to the emitter."""
        from PIL import Image

        files = [(f"a/{i}", _figure(tmp_path / f"p{i}.png",
                                    arm_box=(250 + i * 30, 200, 350 + i * 30, 240)))
                 for i in range(4)]
        got = sprites.from_pose_images(files, out_dir=str(tmp_path / "o"),
                                       name="t", frame_size=(64, 96), pad=2)
        plan = got["layout"]
        assert plan["pad"] == 2
        assert Image.open(got["sheet"]).size == (plan["width"], plan["height"])
        tres = Path(got["tres"]).read_text(encoding="utf-8")
        for i in range(4):
            x, y = spritekit.cell_origin(i, 64, 96, plan)
            assert f"region = Rect2({x}, {y}, 64, 96)" in tres


class TestTiming:
    def test_holds_reach_the_resource(self):
        """Godot has carried per-frame durations all along; this emitter wrote a
        flat 1.0 for every frame ever made."""
        tres = sprites._sprite_frames_tres(
            "s.png", [("attack", 4)], (160, 240), 12.0, "assets/sprites",
            timing={"attack": {"holds": [0.7, 2.0, 1.0, 1.4]}})
        block = tres.split('&"attack"')[0]
        assert '"duration": 0.7' in block
        assert '"duration": 2.0' in block

    def test_ping_pong_replays_frames_without_stuttering_at_the_ends(self):
        tres = sprites._sprite_frames_tres(
            "s.png", [("idle", 3)], (160, 240), 6.0, "assets/sprites",
            timing={"idle": {"order": animspec.pingpong_order(3)}})
        block = tres.split('&"idle"')[0]
        # 0,1,2,1 — four steps from three drawings, and frame 1 appears twice.
        assert block.count('SubResource("atlas_1")') == 2
        assert block.count('SubResource("atlas_0")') == 1
        assert block.count('SubResource("atlas_2")') == 1

    def test_a_timing_loop_flag_overrides_the_name_rule(self):
        tres = sprites._sprite_frames_tres(
            "s.png", [("ko", 2)], (160, 240), 8.0, "assets/sprites",
            timing={"ko": {"loop": True}})
        assert '"loop": true' in tres

    def test_a_zero_hold_cannot_drop_a_frame(self):
        """0 parses fine and then never displays, which reads as a dropped frame
        and looks like an ordinary number in the file."""
        tres = sprites._sprite_frames_tres(
            "s.png", [("a", 1)], (16, 16), 8.0, "d", timing={"a": {"holds": [0]}})
        assert '"duration": 0.05' in tres

    def test_an_order_naming_a_frame_that_does_not_exist_is_dropped(self):
        """Substituting frame 0 for it would emit an animation nobody asked for
        and nothing would say so."""
        tres = sprites._sprite_frames_tres(
            "s.png", [("a", 2)], (16, 16), 8.0, "d",
            timing={"a": {"order": [0, 1, 7]}})
        block = tres.split('&"a"')[0]
        assert block.count('"duration"') == 2

    def test_per_animation_fps_overrides_the_sheet_speed(self):
        """A 6fps idle and a 12fps attack on one sheet is normal; one speed for
        both is a compromise between two right answers."""
        tres = sprites._sprite_frames_tres(
            "s.png", [("idle", 1), ("attack", 1)], (16, 16), 8.0, "d",
            timing={"idle": {"fps": 6.0}, "attack": {"fps": 12.0}})
        assert '"speed": 6.0' in tres and '"speed": 12.0' in tres


class TestAnimspec:
    def test_a_walk_is_built_from_the_four_key_poses(self):
        got = animspec.plan("walk")
        text = " ".join(p["description"].upper() for p in got["poses"])
        for key in ("CONTACT", "DOWN", "PASSING", "UP"):
            assert key in text, f"a walk without {key} is not a walk"
        assert got["generated"] == 8

    def test_an_attack_holds_its_impact_and_rushes_its_wind_up(self):
        """The ratio IS the feeling of a hit. Even holds turn the same four
        drawings into a slideshow."""
        holds = animspec.plan("attack")["timing"]["attack"]["holds"]
        assert holds[0] < 1.0, "the anticipation frame should be quick"
        assert holds[1] >= 2.0, "the impact frame should hold"
        assert holds[1] > holds[0] * 2

    def test_ping_pong_gets_more_playback_than_it_generates(self):
        got = animspec.plan("idle")
        assert got["pingpong"] is True
        assert got["steps"] > got["generated"]
        assert got["timing"]["idle"]["order"] == [0, 1, 2, 1]

    def test_a_one_shot_is_marked_non_looping(self):
        assert animspec.plan("death")["loop"] is False
        assert animspec.plan("walk")["loop"] is True

    def test_aliases_resolve_to_one_canonical_entry(self):
        assert animspec.resolve("punch") == "attack"
        assert animspec.resolve("PING-PONG-NOTHING") is None
        assert animspec.plan("punch", anim="uppercut")["poses"][0]["name"] == "uppercut/0"

    def test_view_is_stated_once_rather_than_eight_times(self):
        got = animspec.plan("walk4", view="side view, facing right")
        assert all(p["description"].startswith("side view, facing right. ")
                   for p in got["poses"])

    def test_several_archetypes_become_one_ordered_set(self):
        got = animspec.plans(["idle", "walk4", "attack"])
        assert got["animations"] == ["idle", "walk4", "attack"]
        names = [p["name"] for p in got["poses"]]
        assert names[0] == "idle/0" and "walk4/0" in names and "attack/3" in names
        assert len(names) == len(set(names)), "duplicate frame names"
        assert set(got["timing"]) == {"idle", "walk4", "attack"}

    def test_the_same_archetype_twice_is_numbered_not_refused(self):
        """Two attacks is a normal thing to want."""
        got = animspec.plans(["attack", "attack"])
        assert got["animations"] == ["attack", "attack2"]

    def test_an_unknown_archetype_names_the_ones_that_exist(self):
        with pytest.raises(ValueError, match="unknown archetype"):
            animspec.plan("moonwalk")

    def test_the_catalog_prices_generation_against_playback(self):
        rows = {row["archetype"]: row for row in animspec.catalog()}
        assert rows["idle"]["steps"] > rows["idle"]["generated"]
        assert rows["walk"]["steps"] == rows["walk"]["generated"]
        assert all(row["why"] for row in rows.values())


class TestFacingReport:
    """The generated-frames facing vote — row_report had one for months while
    the path that buys frames one API call at a time never ran it. Found by a
    human on the first real 8-frame walk: two frames facing camera in a set
    asked to face right, motion report clean."""

    @staticmethod
    def _head(tmp_path, name, eye_side):
        """A figure whose head band carries a dark 'visor' left or right."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((20, 4, 44, 28), fill=(210, 190, 170, 255))    # head
        d.rectangle((24, 30, 40, 90), fill=(120, 130, 150, 255))   # body
        ex = (36, 42) if eye_side > 0 else (22, 28)
        d.rectangle((ex[0], 12, ex[1], 20), fill=(25, 20, 20, 255))
        p = tmp_path / f"{name}.png"
        img.save(p)
        return str(p)

    def _set(self, tmp_path, sides):
        ordered = [f"walk/{i}" for i in range(len(sides))]
        files = {pose: self._head(tmp_path, f"f{i}", side)
                 for i, (pose, side) in enumerate(zip(ordered, sides))}
        return ordered, files

    def test_the_minority_is_the_finding(self, tmp_path):
        ordered, files = self._set(tmp_path, [1, 1, -1, 1, 1, 1])
        got = spritekit.facing_report(ordered, files)
        [finding] = got["findings"]
        assert finding["kind"] == "facing_flip"
        assert finding["frames"] == ["walk/2"]

    def test_a_unanimous_set_is_clean(self, tmp_path):
        ordered, files = self._set(tmp_path, [1, 1, 1, 1])
        assert spritekit.facing_report(ordered, files)["findings"] == []

    def test_a_split_set_is_ambiguous_not_a_verdict(self, tmp_path):
        """Half the frames are wrong either way — pretending to know which
        half helps nobody."""
        ordered, files = self._set(tmp_path, [1, 1, -1, -1])
        got = spritekit.facing_report(ordered, files)
        assert got.get("ambiguous") is True and got["findings"] == []

    def test_sheet_report_carries_the_finding_and_flags_the_anim(self, tmp_path):
        ordered, files = self._set(tmp_path, [1, 1, 1, -1, 1, 1])
        got = spritekit.sheet_report(ordered, files)
        assert "walk" in got["flagged"]
        kinds = {f["kind"] for f in got["animations"]["walk"]["findings"]}
        assert "facing_flip" in kinds
        # Facing now votes PER ANIMATION GROUP (a contract sheet carries
        # several directions, and a back row voting against a front row would
        # flag a correct sheet) — the report is keyed by animation.
        assert got["facing"]["walk"]["voters"] == 6


class TestHeightOutlier:
    """Set-median height check — the adjacent-pair jitter check cannot see a
    first and last frame that are both drawn tall, because they are never a
    pair. Found by a human bracketing a real 8-frame walk."""

    @staticmethod
    def _figure_h(tmp_path, name, height):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 200), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((24, 199 - height, 40, 199), fill=(150, 60, 40, 255))
        p = tmp_path / f"{name}.png"
        img.save(p)
        return str(p)

    def _set(self, tmp_path, heights, anim="walk"):
        ordered = [f"{anim}/{i}" for i in range(len(heights))]
        files = {pose: self._figure_h(tmp_path, f"h{i}", h)
                 for i, (pose, h) in enumerate(zip(ordered, heights))}
        return ordered, files

    def test_a_tall_bracket_is_flagged_against_the_median(self, tmp_path):
        ordered, files = self._set(tmp_path, [160, 144, 145, 146, 144, 158])
        got = spritekit.facing_report(ordered, files)
        [finding] = [f for f in got["findings"] if f["kind"] == "height_outlier"]
        assert set(finding["frames"]) == {"walk/0", "walk/5"}

    def test_stride_variation_under_the_threshold_is_not_a_finding(self, tmp_path):
        ordered, files = self._set(tmp_path, [140, 144, 146, 145, 142, 146])
        got = spritekit.facing_report(ordered, files)
        assert [f for f in got["findings"] if f["kind"] == "height_outlier"] == []

    def test_airborne_animations_do_not_vote(self, tmp_path):
        """A jump tucks its legs — shorter drawn height is the pose, not a
        defect."""
        ordered, files = self._set(tmp_path, [144, 145, 120, 146, 145],
                                   anim="jump")
        got = spritekit.facing_report(ordered, files, airborne=("jump",))
        assert got["findings"] == [] and got["heights"] == {}

    def test_a_fifty_fifty_scale_fork_is_still_a_finding(self, tmp_path):
        """Three tall + three short leaves no minority, and the outlier vote
        rightly refuses to pick a side — but a 33% spread is wrong no matter
        which half is right. Measured on a real nb2 walk."""
        ordered, files = self._set(tmp_path, [154, 154, 103, 103, 154, 102])
        got = spritekit.facing_report(ordered, files)
        [finding] = [f for f in got["findings"] if f["kind"] == "height_split"]
        assert set(finding["frames"]) == set(ordered)
        assert "walk/2" in finding["note"] and "walk/5" in finding["note"]


class TestYawDrift:
    """Same left/right facing, different camera ANGLE — the sign vote cannot
    see it. Found by a human on an RD walk whose last three frames rotated
    toward camera: same-sign skews clustered 0.11-0.14 vs 0.04-0.06."""

    @staticmethod
    def _skewed(tmp_path, name, offset):
        """A figure whose head detail sits `offset` px right of head centre —
        bigger offset reads as more profile."""
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((20, 4, 44, 28), fill=(210, 190, 170, 255))
        d.rectangle((24, 30, 40, 90), fill=(120, 130, 150, 255))
        cx = 32 + offset
        d.rectangle((cx - 2, 12, cx + 2, 20), fill=(25, 20, 20, 255))
        p = tmp_path / f"{name}.png"
        img.save(p)
        return str(p)

    def _set(self, tmp_path, offsets):
        ordered = [f"walk/{i}" for i in range(len(offsets))]
        files = {pose: self._skewed(tmp_path, f"y{i}", off)
                 for i, (pose, off) in enumerate(zip(ordered, offsets))}
        return ordered, files

    def test_a_frontal_cluster_is_flagged(self, tmp_path):
        ordered, files = self._set(tmp_path, [9, 9, 9, 9, 3, 3])
        got = spritekit.facing_report(ordered, files)
        yaw = [f for f in got["findings"] if f["kind"] == "yaw_drift"]
        assert yaw and set(yaw[0]["frames"]) >= {"walk/4", "walk/5"}

    def test_one_band_of_magnitudes_is_clean(self, tmp_path):
        ordered, files = self._set(tmp_path, [8, 9, 8, 9, 8, 9])
        got = spritekit.facing_report(ordered, files)
        assert [f for f in got["findings"] if f["kind"] == "yaw_drift"] == []


class TestNormaliseHeights:
    """The cheap half of a height finding: arithmetic, not a re-roll."""

    def _figure(self, tmp_path, name, height, canvas=(128, 160), bottom=150):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", canvas, (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        w = round(height * 0.4)
        cx = canvas[0] // 2
        d.rectangle((cx - w // 2, bottom - height, cx + w // 2, bottom),
                    fill=(120, 100, 90, 255))
        p = tmp_path / f"{name}.png"
        img.save(p)
        return str(p)

    def _heights(self, files):
        from PIL import Image

        out = {}
        for pose, path in files.items():
            b = Image.open(path).getbbox()
            out[pose] = (b[3] - b[1], b[3])
        return out

    def test_the_oversized_minority_is_scaled_onto_its_feet(self, tmp_path):
        ordered = [f"walk/{i}" for i in range(6)]
        sizes = [100, 100, 100, 100, 140, 142]
        files = {p: self._figure(tmp_path, f"f{i}", s)
                 for i, (p, s) in enumerate(zip(ordered, sizes))}
        got = spritekit.normalise_heights(ordered, files)
        assert set(got["scaled"]) == {"walk/4", "walk/5"}
        # ImageDraw rectangles are inclusive, so drawn height is size+1
        assert got["median"] in (100, 101)
        after = self._heights(files)
        for pose, (h, _bottom) in after.items():
            assert abs(h - got["median"]) <= 6, (pose, h)
        assert len({b for _, b in after.values()}) == 1, "feet moved"

    def test_a_uniform_set_is_untouched_and_the_call_is_idempotent(
            self, tmp_path):
        ordered = [f"walk/{i}" for i in range(6)]
        sizes = [100, 102, 99, 101, 100, 100]
        files = {p: self._figure(tmp_path, f"u{i}", s)
                 for i, (p, s) in enumerate(zip(ordered, sizes))}
        assert spritekit.normalise_heights(ordered, files)["scaled"] == {}
        files2 = {p: self._figure(tmp_path, f"v{i}", s)
                  for i, (p, s) in enumerate(zip(ordered,
                                                 [100] * 4 + [140, 140]))}
        spritekit.normalise_heights(ordered, files2)
        assert spritekit.normalise_heights(ordered, files2)["scaled"] == {}

    def test_a_split_set_has_no_majority_to_trust(self, tmp_path):
        """Half the frames 'wrong' means nobody knows which half — leave it
        for the finding, do not silently rescale half a sheet."""
        ordered = [f"walk/{i}" for i in range(6)]
        sizes = [100, 100, 100, 140, 140, 140]
        files = {p: self._figure(tmp_path, f"s{i}", s)
                 for i, (p, s) in enumerate(zip(ordered, sizes))}
        assert spritekit.normalise_heights(ordered, files)["scaled"] == {}
