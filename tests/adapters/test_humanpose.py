"""The pose and gait library, driven on the canonical rig with no Blender.

Every assertion here is a MEASUREMENT off forward kinematics, not a check that
a function returned. The defects this module was written against were all
invisible to the checks that existed: a walk that strode backwards planted
its feet, a torso that never bent past 28 degrees keyed smoothly, arms that
hung like a mannequin's had clean curves. So the tests ask where the hand IS,
which way the foot POINTS, whether the knee went FORWARD.
"""
from __future__ import annotations

import math

import pytest

from bgate_adapters import humanpose as hp


@pytest.fixture
def rig():
    return hp.RigFrame(**hp.canonical_rig())


def _dist(a, b):
    return hp.v_len(hp.v_sub(a, b))


class TestTheRigIsRead:
    def test_the_canonical_rig_faces_plus_y_with_its_left_on_minus_x(self, rig):
        """Both MEASURED off the bones. A rig with its Left bones on +X must
        read the other way — that is the whole reason nothing is assumed."""
        assert rig.forward == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)
        assert rig.left == pytest.approx((-1.0, 0.0, 0.0), abs=1e-6)

    def test_a_mirrored_rig_reads_its_own_axes(self):
        """The rig that walked a character backwards: feet pointing -Y, Left
        bones on +X. Read correctly, its clips land correctly."""
        dump = hp.canonical_rig()
        flipped = {}
        for name, b in dump["bones"].items():
            flipped[name] = dict(b, head=(-b["head"][0], -b["head"][1], b["head"][2]),
                                 tail=(-b["tail"][0], -b["tail"][1], b["tail"][2]),
                                 matrix=tuple((-r[0], -r[1], r[2]) if i < 2 else r
                                              for i, r in enumerate(b["matrix"])))
        rig = hp.RigFrame(flipped)
        assert rig.forward == pytest.approx((0.0, -1.0, 0.0), abs=1e-6)
        assert rig.left == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)

    def test_bone_length_is_head_to_first_child_head(self, rig):
        """What glTF carries — a tail is authorship, a child's head is data."""
        assert rig.lengths["LeftUpperLeg"] == pytest.approx(0.44, abs=1e-6)
        assert rig.leg_length() == pytest.approx(0.44 + 0.367, abs=1e-6)


class TestForwardKinematics:
    def test_rest_reproduces_the_rest_tails(self, rig):
        pose = hp.Pose(rig)
        for name in rig.order:
            assert _dist(pose.world_tail(name), rig.bones[name]["tail"]) < 1e-9

    def test_aim_points_the_bone_and_carries_its_children(self, rig):
        pose = hp.Pose(rig)
        pose.aim("LeftUpperArm", (0.0, 0.0, -1.0))
        assert pose.world_dir("LeftUpperArm") == pytest.approx((0, 0, -1), abs=1e-6)
        # The hand hangs below the elbow now, on the same side.
        hand = pose.world("LeftHand")[1]
        assert hand[2] < rig.bones["LeftUpperArm"]["head"][2] - 0.5
        assert hand[0] < 0.0

    def test_anatomical_pitch_carries_the_distal_end_forward(self, rig):
        """+pitch on a hanging arm = the hand goes toward the character's
        FRONT. The sign every gait relies on, pinned."""
        pose = hp.Pose(rig)
        pose.aim("LeftUpperArm", (0.0, 0.0, -1.0))
        before = pose.world("LeftHand")[1]
        pose.anatomical("LeftUpperArm", pitch=45.0)
        after = pose.world("LeftHand")[1]
        assert hp.v_dot(hp.v_sub(after, before), rig.forward) > 0.2

    def test_anatomical_axes_follow_the_parent(self, rig):
        """An elbow folds FORWARD relative to the upper arm wherever the upper
        arm is — the transported frame, not the world one."""
        pose = hp.Pose(rig)
        pose.aim("LeftUpperArm", (0.0, 0.0, -1.0))
        pose.anatomical("LeftUpperArm", pitch=90.0)          # arm straight out front
        pose.aim("LeftLowerArm", pose.world_dir("LeftUpperArm"))
        pose.anatomical("LeftLowerArm", pitch=90.0)          # forearm folds
        # Forearm now points UP (forward rotated by +90 about left → up).
        assert pose.world_dir("LeftLowerArm") == pytest.approx((0, 0, 1), abs=1e-6)

    def test_the_spine_bend_accumulates(self, rig):
        """The failure that made every crouch stand up straight: absolute aims
        do not stack. A 60 degree lean has to put the shoulders 60 degrees off
        vertical, not 21."""
        pose = hp.stand(rig, lean=60.0)
        d = pose.world_dir("UpperChest")
        angle = math.degrees(math.acos(max(-1.0, min(1.0, hp.v_dot(d, rig.up)))))
        assert angle == pytest.approx(60.0, abs=1.0)
        assert hp.v_dot(d, rig.forward) > 0.8          # forward, not back


class TestLegIK:
    def test_reaches_a_target_with_the_knee_forward(self, rig):
        pose = hp.Pose(rig)
        rest = rig.bones["LeftFoot"]["head"]
        target = hp.v_add(rest, hp.frame(rig, forward=0.15, up=0.05))
        short = pose.leg_ik("Left", target, foot_dir=rig.rest_dir("LeftFoot"))
        assert short == 0.0
        assert _dist(pose.world("LeftFoot")[1], target) < 1e-6
        knee = pose.world("LeftLowerLeg")[1]
        assert hp.v_dot(knee, rig.forward) > 0.05

    def test_an_unreachable_target_reports_its_shortfall(self, rig):
        """Not a silent clamp: the walk that floated 8 cm off the floor was a
        clamp nobody could see."""
        pose = hp.Pose(rig)
        target = hp.v_add(rig.bones["LeftFoot"]["head"], hp.frame(rig, forward=1.5))
        short = pose.leg_ik("Left", target)
        assert short > 0.5

    def test_a_dropped_hip_keeps_the_feet_planted(self, rig):
        pose = hp.stand(rig, hips=hp.frame(rig, up=-0.25))
        for side in hp.SIDES:
            assert _dist(pose.world(side + "Foot")[1],
                         rig.bones[side + "Foot"]["head"]) < 1e-6
        # and the hips actually dropped
        assert pose.world("Hips")[1][2] < rig.bones["Hips"]["head"][2] - 0.2


class TestTheWalk:
    def test_a_foot_is_down_on_every_frame_and_none_goes_under(self, rig):
        poses, notes = hp.gait_cycle(rig, "walk")
        floor = rig.ankle_height()
        for pose in poses:
            zs = [pose.world(s + "Foot")[1][2] for s in hp.SIDES]
            assert min(zs) <= floor + 0.005
            assert min(zs) >= floor - 1e-6
        assert notes["shortfall_frames"] == 0

    def test_it_strides_forward_and_the_arms_counter_swing(self, rig):
        """Frame 0 is the left foot's heel strike: left foot front, right foot
        back, left arm BACK, right arm FORWARD — or it is a moonwalk."""
        poses, _ = hp.gait_cycle(rig, "walk")
        p0 = poses[0]
        f = rig.forward
        assert hp.v_dot(p0.world("LeftFoot")[1], f) > 0.15
        assert hp.v_dot(p0.world("RightFoot")[1], f) < -0.1
        assert hp.v_dot(p0.world("LeftHand")[1], f) < hp.v_dot(p0.world("RightHand")[1], f)

    def test_the_run_has_flight_and_the_walk_does_not(self, rig):
        def flight(kind):
            poses, _ = hp.gait_cycle(rig, kind)
            floor = rig.ankle_height()
            return sum(1 for p in poses
                       if min(p.world(s + "Foot")[1][2] for s in hp.SIDES) > floor + 0.01)
        assert flight("walk") == 0
        assert flight("run") > 0

    def test_the_stride_scales_with_the_legs(self):
        """The same parameters on a rig twice the size stride twice as far —
        distances are fractions of leg length, never metres an author liked."""
        small = hp.RigFrame(**hp.canonical_rig())
        big_dump = hp.canonical_rig()
        for b in big_dump["bones"].values():
            b["head"] = hp.v_scale(b["head"], 2.0)
            b["tail"] = hp.v_scale(b["tail"], 2.0)
        big = hp.RigFrame(**big_dump)
        _, a = hp.gait_cycle(small, "walk")
        _, b = hp.gait_cycle(big, "walk")
        assert b["half_stride"] == pytest.approx(2 * a["half_stride"], rel=1e-3)

    def test_the_head_stays_level_while_the_pelvis_turns(self, rig):
        poses, _ = hp.gait_cycle(rig, "walk")
        for pose in poses:
            d = pose.world_dir("Head")
            assert hp.v_dot(d, rig.up) > 0.98


class TestBaking:
    def test_every_bone_has_a_track_on_every_frame_and_no_sign_flips(self, rig):
        poses, _ = hp.gait_cycle(rig, "walk")
        baked = hp.bake(rig, poses)
        assert set(baked["bones"]) == set(rig.bones)
        for name in baked["bones"]:
            track = baked["rotations"][name]
            assert len(track) == len(poses)
            for i in range(1, len(track)):
                assert sum(a * b for a, b in zip(track[i - 1], track[i])) >= 0.0
            assert all(math.isfinite(c) for q in track for c in q)

    def test_the_loop_closes(self, rig):
        """The last frame is one step from the first, not a jump."""
        poses, _ = hp.gait_cycle(rig, "walk")
        baked = hp.bake(rig, poses)
        for name in baked["bones"]:
            first, last = baked["rotations"][name][0], baked["rotations"][name][-1]
            assert abs(sum(a * b for a, b in zip(first, last))) > 0.98

    def test_bake_clips_reports_a_bad_spec_and_keeps_the_rest(self):
        out = hp.bake_clips(hp.canonical_rig(),
                            [{"kind": "walk"}, {"kind": "nothing_like_this"},
                             {"name": "custom", "kind": "keyed", "loop": True,
                              "keys": [{"t": 0.0}, {"t": 1.0, "lean": 40.0}]}])
        by = {c["name"]: c for c in out["clips"]}
        assert by["walk"]["ok"] and by["custom"]["ok"]
        assert by["nothing_like_this"]["ok"] is False
        assert "unknown clip kind" in by["nothing_like_this"]["error"]
        # A looping keyed clip that ends elsewhere gets its first key appended.
        assert by["custom"]["notes"]["keys"] == 3

    def test_every_preset_solves_on_the_canonical_rig(self, rig):
        for kind in hp.presets(rig):
            poses, notes = hp.build_clip(rig, {"kind": kind})
            assert poses and notes["ignored_fields"] == []
            for pose in poses:
                for q in pose.quaternions().values():
                    assert all(math.isfinite(c) for c in q)


class TestTheHipsTranslation:
    def test_root_location_is_in_the_bone_frame(self, rig):
        """Blender's pose_bone.location is bone-local. Hips' rest X axis is
        -X (roll 180), so a +X world nudge is a -X local one."""
        pose = hp.Pose(rig)
        pose.set_hips((0.1, 0.0, 0.0))
        assert pose.hips_local() == pytest.approx((-0.1, 0.0, 0.0), abs=1e-9)
