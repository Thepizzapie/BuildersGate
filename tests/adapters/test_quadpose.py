"""The quadruped pose and gait library, driven on the canonical rig, no Blender.

Every assertion is a MEASUREMENT off forward kinematics: where the paw IS,
which way the elbow bends, whether the footfalls come in the order the gait
is named after. A gait that keys smoothly with every foot in the wrong order
is what a curve gate cannot see.
"""
from __future__ import annotations

import pytest

from bgate_adapters import humanpose as hp
from bgate_adapters import quadpose as qp


@pytest.fixture
def rig():
    return qp.QuadFrame(**qp.canonical_quadruped_rig())


def _contacts(rig, poses, band=0.01):
    """Per frame, which legs are within `band` of their rest paw height."""
    out = []
    for pose in poses:
        down = set()
        for leg in rig.legs():
            _, head = pose.world(leg + "Foot")
            if hp.v_dot(head, rig.up) < rig.paw_height(leg) + band:
                down.add(leg)
        out.append(down)
    return out


class TestTheRigIsRead:
    def test_forward_runs_from_hips_to_head_and_left_is_where_left_legs_are(self, rig):
        assert rig.forward == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)
        assert rig.left == pytest.approx((-1.0, 0.0, 0.0), abs=1e-6)
        assert rig.legs() == list(qp.LEGS)

    def test_a_mirrored_rig_reads_its_own_axes(self):
        dump = qp.canonical_quadruped_rig()
        flipped = {}
        for name, b in dump["bones"].items():
            flipped[name] = dict(b, head=(-b["head"][0], -b["head"][1], b["head"][2]),
                                 tail=(-b["tail"][0], -b["tail"][1], b["tail"][2]),
                                 matrix=tuple((-r[0], -r[1], r[2]) if i < 2 else r
                                              for i, r in enumerate(b["matrix"])))
        rig = qp.QuadFrame(flipped)
        assert rig.forward == pytest.approx((0.0, -1.0, 0.0), abs=1e-6)
        assert rig.left == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)

    def test_is_quadruped_is_decided_by_front_legs(self, rig):
        assert qp.is_quadruped(rig.bones)
        assert not qp.is_quadruped(hp.canonical_rig()["bones"])


class TestStanding:
    def test_the_rest_stand_leaves_every_paw_where_it_was(self, rig):
        pose = qp.quad_stand(rig)
        for leg in rig.legs():
            _, head = pose.world(leg + "Foot")
            assert head == pytest.approx(rig.paw(leg), abs=2e-3)

    def test_elbows_bend_back_and_stifles_bend_forward(self, rig):
        """The one thing that separates an animal from a table: the middle
        joint of a front leg sits BEHIND the shoulder line, of a hind leg
        AHEAD of the hip line - solved by IK with that pole, not authored."""
        pose = qp.quad_stand(rig, feet={leg: hp.v_add(rig.paw(leg), (0, 0, 0.06))
                                        for leg in rig.legs()})
        for leg in qp.FRONT:
            _, mid = pose.world(leg + "LowerLeg")
            assert hp.v_dot(mid, rig.forward) < hp.v_dot(rig.bones[leg + "UpperLeg"]["head"], rig.forward)
        for leg in qp.BACK:
            _, mid = pose.world(leg + "LowerLeg")
            assert hp.v_dot(mid, rig.forward) > hp.v_dot(rig.bones[leg + "UpperLeg"]["head"], rig.forward)

    def test_head_pitch_carries_the_nose_down(self, rig):
        up = qp.quad_stand(rig)
        down = qp.quad_stand(rig, neck_pitch=20.0, head_pitch=20.0)
        assert hp.v_dot(down.world_tail("Head"), rig.up) < hp.v_dot(up.world_tail("Head"), rig.up) - 0.05


class TestGaits:
    def test_the_walk_keeps_feet_down_and_never_flies(self, rig):
        poses, notes = qp.quad_gait_cycle(rig, "walk", fps=30)
        contacts = _contacts(rig, poses)
        assert all(len(c) >= 2 for c in contacts), "a walk has at least two feet down"
        assert notes["shortfall_frames"] == 0

    def test_the_walk_footfalls_come_in_lateral_sequence(self, rig):
        """LB, LF, RB, RF - the order the gait is named after."""
        poses, _ = qp.quad_gait_cycle(rig, "walk", frames=40)
        contacts = _contacts(rig, poses)
        strikes = {}
        for i, down in enumerate(contacts):
            prev = contacts[i - 1]
            for leg in down - prev:
                strikes.setdefault(leg, i)
        assert set(strikes) == set(qp.LEGS)
        order = sorted(strikes, key=strikes.get)
        start = order.index("LeftBack")
        assert order[start:] + order[:start] == ["LeftBack", "LeftFront", "RightBack", "RightFront"]

    def test_the_trot_pairs_diagonals(self, rig):
        poses, _ = qp.quad_gait_cycle(rig, "trot", frames=40)
        for down in _contacts(rig, poses):
            if len(down) == 2:
                assert down in ({"LeftFront", "RightBack"}, {"RightFront", "LeftBack"})

    def test_the_gallop_has_a_flight_phase_and_no_shortfall(self, rig):
        poses, notes = qp.quad_gait_cycle(rig, "gallop", fps=30)
        contacts = _contacts(rig, poses)
        assert any(len(c) == 0 for c in contacts), "a gallop leaves the ground"
        assert notes["shortfall_frames"] == 0
        assert notes["body_drop"] > 0.0, "the body dropped to keep the paws in reach"

    def test_every_distance_scales_with_the_leg(self):
        small = qp.QuadFrame(**qp.canonical_quadruped_rig(length=0.4, shoulder=0.22, width=0.07))
        big = qp.QuadFrame(**qp.canonical_quadruped_rig(length=2.0, shoulder=1.2, width=0.4))
        _, n_small = qp.quad_gait_cycle(small, "walk")
        _, n_big = qp.quad_gait_cycle(big, "walk")
        assert n_small["shortfall_frames"] == 0 and n_big["shortfall_frames"] == 0


class TestClipsAndBaking:
    def test_every_kind_builds_and_a_bad_kind_is_reported_not_raised(self):
        dump = qp.canonical_quadruped_rig()
        specs = [{"name": k, "kind": k} for k in qp.QUAD_CLIP_KINDS if k != "keyed"]
        specs.append({"name": "nope", "kind": "nope"})
        out = qp.bake_quad_clips(dump, specs)
        by = {c["name"]: c for c in out["clips"]}
        for k in qp.QUAD_CLIP_KINDS:
            if k != "keyed":
                assert by[k]["ok"], by[k]
                assert by[k]["frames"] == len(by[k]["rotations"]["Hips"])
        assert by["nope"]["ok"] is False and "unknown" in by["nope"]["error"]
        assert out["rig"]["kind"] == "quadruped"

    def test_a_keyed_clip_speaks_in_character_terms(self, rig):
        poses, notes = qp.quad_keyed_clip(rig, [{"t": 0.0}, {"t": 0.5, "hips_up": -0.2}], fps=30)
        assert notes["frames"] == 16
        _, first = poses[0].world("Hips")
        _, last = poses[-1].world("Hips")
        assert hp.v_dot(last, rig.up) == pytest.approx(hp.v_dot(first, rig.up) - 0.2, abs=1e-6)
