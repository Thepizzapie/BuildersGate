"""Four-legged landmarks off a point cloud, and the skeleton built on them.

The synthetic dog here is the shape a generator hands back after adopt: a
trunk, four leg columns, a head that rises above the back, a thin tail.
"""
from __future__ import annotations

import random

import pytest

from bgate_adapters import bodymeasure as bm
from bgate_adapters import humanpose as hp
from bgate_adapters import quadpose as qp


def _dog(seed=1, head=True, tail=True, flip=False):
    rnd = random.Random(seed)
    pts = []

    def box(cx, cy, cz, sx, sy, sz, n):
        for _ in range(n):
            pts.append((cx + rnd.uniform(-sx, sx), cy + rnd.uniform(-sy, sy),
                        cz + rnd.uniform(-sz, sz)))
    box(0, 0, 0.55, 0.15, 0.5, 0.15, 3000)
    for x in (-0.09, 0.09):
        for y in (-0.42, 0.42):
            box(x, y, 0.22, 0.04, 0.05, 0.22, 500)
    if head:
        box(0, 0.75, 0.8, 0.1, 0.15, 0.1, 800)
    if tail:
        box(0, -0.7, 0.6, 0.02, 0.15, 0.04, 200)
    if flip:
        pts = [(x, -y, z) for x, y, z in pts]
    return pts


class TestFrontSign:
    def test_the_head_end_reads_as_the_front(self):
        assert bm.quadruped_front_sign(_dog())["sign"] == 1
        assert bm.quadruped_front_sign(_dog(flip=True))["sign"] == -1

    def test_a_headless_body_refuses_rather_than_guessing(self):
        read = bm.quadruped_front_sign(_dog(head=False, tail=False))
        assert read["sign"] == 0
        assert "same mass" in read["why"]


class TestLandmarks:
    def test_four_legs_a_neck_and_a_tail_are_measured(self):
        m = bm.quadruped_landmarks(_dog())
        assert m["why"] == []
        assert m["measured"] == 6
        for leg, entry in m["legs"].items():
            assert entry["paw"][2] < 0.05
            assert "Front" in leg and entry["paw"][1] > 0.3 or "Back" in leg and entry["paw"][1] < -0.3
            assert ("Left" in leg) == (entry["paw"][0] < 0)
            assert 0.4 < entry["top"][2] < 0.62
        assert m["neck"]["measured"] and m["tail"]["measured"]
        assert m["nose"][1] > 0.85
        assert m["tail"]["tip"][1] < -0.7

    def test_no_head_is_placed_from_the_trunk_and_says_so(self):
        m = bm.quadruped_landmarks(_dog(head=False, tail=False))
        assert m["neck"]["measured"] is False
        assert m["tail"]["measured"] is False
        assert any("ahead of the shoulders" in w for w in m["why"])
        assert m["measured"] == 4

    def test_a_body_with_no_legs_refuses(self):
        pts = [p for p in _dog() if p[2] > 0.42]
        m = bm.quadruped_landmarks(pts)
        assert "hips" not in m
        assert m["why"]


class TestTheChain:
    def test_the_skeleton_walks(self):
        rows = bm.quadruped_chain(bm.quadruped_landmarks(_dog()))
        names = [r[0] for r in rows]
        assert names[:8] == ["Root", "Hips", "Spine", "Chest", "Neck", "Head", "Tail1", "Tail2"]
        assert len(names) == 20
        bones = {}
        for name, head, tail, parent, _roll in rows:
            bones[name] = hp._bone(parent, head, tail,
                                   (1, 0, 0) if name.endswith("Leg") else (-1, 0, 0))
        rig = qp.QuadFrame(bones)
        assert rig.forward == pytest.approx((0.0, 1.0, 0.0), abs=1e-3)
        _, notes = qp.quad_gait_cycle(rig, "walk")
        assert notes["shortfall_frames"] == 0

    def test_no_tail_means_no_tail_bones(self):
        rows = bm.quadruped_chain(bm.quadruped_landmarks(_dog(tail=False)))
        assert "Tail1" not in [r[0] for r in rows]

    def test_unmeasured_trunk_raises(self):
        with pytest.raises(ValueError):
            bm.quadruped_chain({"why": ["nothing"]})
