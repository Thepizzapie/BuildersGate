"""Humanoid pose and gait authoring, in plain Python — the animation layer the
3D path never had.

WHY THIS EXISTS. Every rigged humanoid out of this pipeline carries Godot's
SkeletonProfileHumanoid bone names, and nothing anywhere could put a clip on
one. An agent asked for "six gameplay animations" wrote a 600-line per-project
bpy script that aimed bones at absolute world directions, assumed the rig
faced +Y with its left on -X, and interpolated nine sparse keys. MEASURED on
the character it was written for: every clip bent and strode BACKWARDS
(the rig sat 180 degrees from the frame the script assumed), the torso never
bent more than 28 degrees (absolute aims do not accumulate down a chain), the
arms hung like a mannequin's, and every automated gate passed it — a moonwalk
still plants its feet and still keys smoothly.

THREE RULES THIS MODULE IS BUILT ON:

  1. THE RIG IS MEASURED, NEVER ASSUMED. `RigFrame` reads forward, up and left
     off the bones it is given (the feet point forward; the two hips span
     left-to-right), so the same clip lands correctly on a rig whose "Left"
     bones happen to sit on +X. Whether the MESH agrees with the skeleton is a
     separate question, answered in Blender by the facing gate in blender.py.

  2. POSES ARE ANATOMICAL, NOT LOCAL-AXIS. `pitch` swings a limb about the
     character's LEFT axis (forward/back), `yaw` about UP, `roll` about
     FORWARD — the axes transported through the parent's own pose, so an
     elbow folds forward whichever way the upper arm has been swung. Nobody
     authoring a clip needs to know a bone's roll, and no clip breaks when
     `bg_roll` changes.

  3. FEET GO WHERE THEY ARE PUT. Every locomotion pose solves two-bone IK for
     each leg against an ankle target, with the knee pushed toward the
     character's front, and the hip height is derived from what the legs can
     reach at full stride — the stride is what the legs allow, not a number
     an author liked.

The module is PURE. `bake_clips` returns per-frame bone-local quaternions and
a Hips translation; `blender.py` splices this file into a bpy script (the same
way `_measured` splices bodymeasure.py) and keys them. A unit test drives the
same code on `canonical_rig()` with no Blender present.

Quaternions here are (w, x, y, z) — Blender's order. Vectors are 3-tuples.
Angles at the API are DEGREES; internally radians.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Tiny linear algebra — enough to compose bones, no numpy in Blender's Python
# ---------------------------------------------------------------------------

Vec = tuple


def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(v_dot(a, a))


def v_norm(a, fallback=(0.0, 0.0, 1.0)):
    n = v_len(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else fallback


def m_identity():
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def m_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3))
                       for j in range(3)) for i in range(3))


def m_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def m_transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def m_col(m, i):
    return (m[0][i], m[1][i], m[2][i])


def m_from_axis_angle(axis, angle):
    x, y, z = v_norm(axis, (1.0, 0.0, 0.0))
    c, s = math.cos(angle), math.sin(angle)
    t = 1.0 - c
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c))


def m_from_to(a, b):
    """The smallest rotation taking unit vector `a` onto unit vector `b`."""
    a, b = v_norm(a), v_norm(b)
    d = max(-1.0, min(1.0, v_dot(a, b)))
    if d > 1.0 - 1e-9:
        return m_identity()
    if d < -1.0 + 1e-9:
        # Antiparallel: any axis perpendicular to `a` does; pick a stable one.
        helper = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        return m_from_axis_angle(v_cross(a, helper), math.pi)
    return m_from_axis_angle(v_cross(a, b), math.acos(d))


def m_to_quat(m):
    """(w, x, y, z) from a rotation matrix. Shepperd's method, sign-stable."""
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        return (0.25 * s, (m[2][1] - m[1][2]) / s,
                (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return ((m[2][1] - m[1][2]) / s, 0.25 * s,
                (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s,
                0.25 * s, (m[1][2] + m[2][1]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s,
            (m[1][2] + m[2][1]) / s, 0.25 * s)


def q_to_m(q):
    w, x, y, z = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def q_norm(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def q_slerp(a, b, t):
    """Shortest-arc slerp on the hemisphere — the double cover is a jump of 2."""
    d = sum(x * y for x, y in zip(a, b))
    if d < 0.0:
        b = tuple(-c for c in b)
        d = -d
    if d > 0.9995:
        return q_norm(tuple(x + (y - x) * t for x, y in zip(a, b)))
    theta = math.acos(max(-1.0, min(1.0, d)))
    sa, sb = math.sin((1.0 - t) * theta), math.sin(t * theta)
    st = math.sin(theta)
    return tuple((x * sa + y * sb) / st for x, y in zip(a, b))


Q_IDENTITY = (1.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------

def ease_inout(u):
    """Smoothstep. The one curve a walking body actually follows between two
    extremes; linear interpolation is what read as a mannequin."""
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)


def ease_linear(u):
    return max(0.0, min(1.0, u))


EASES = {"inout": ease_inout, "linear": ease_linear}


# ---------------------------------------------------------------------------
# The rig, as measured
# ---------------------------------------------------------------------------

LEFT, RIGHT = "Left", "Right"
SIDES = (LEFT, RIGHT)

# Bone roles by Godot humanoid name. Every generator below speaks in these.
TRUNK = ("Hips", "Spine", "Chest", "UpperChest", "Neck", "Head")


class RigFrame:
    """A skeleton's rest pose plus the anatomical axes read off it.

    `bones`: {name: {"parent": str|None, "head": (x,y,z), "tail": (x,y,z),
                     "matrix": 3x3 rest rotation in ARMATURE space, columns
                     = the bone's local X, Y (along the bone), Z}}
    All in metres, armature space, Z up (Blender's convention; glTF is
    converted by the exporter).
    """

    def __init__(self, bones: dict, *, up=(0.0, 0.0, 1.0)):
        self.bones = {n: dict(b) for n, b in bones.items()}
        self.up = v_norm(up)
        self.order = self._topological()
        self.forward = self._read_forward()
        self.left = self._read_left()
        # Re-orthogonalise: forward is trusted, left made perpendicular to it.
        f, u = self.forward, self.up
        l = self.left
        l = v_norm(v_sub(l, v_scale(f, v_dot(l, f))), v_cross(u, f))
        self.left = l
        self.lengths = {n: self.bone_length(n) for n in self.bones}

    # -- reading the rig ----------------------------------------------------
    def _topological(self):
        out, seen = [], set()

        def visit(n):
            if n in seen or n not in self.bones:
                return
            p = self.bones[n].get("parent")
            if p and p in self.bones and p not in seen:
                visit(p)
            seen.add(n)
            out.append(n)
        for n in self.bones:
            visit(n)
        return out

    def _read_forward(self):
        """Forward is where the FEET point. A foot bone runs ankle-to-toe, so
        its horizontal component is the one unambiguous 'front' a skeleton
        carries; the trunk is symmetric and says nothing."""
        acc = (0.0, 0.0, 0.0)
        for side in SIDES:
            for role in ("Foot", "Toes"):
                b = self.bones.get(side + role)
                if not b:
                    continue
                d = v_sub(b["tail"], b["head"])
                d = v_sub(d, v_scale(self.up, v_dot(d, self.up)))
                acc = v_add(acc, d)
        if v_len(acc) < 1e-6:
            return v_norm(v_cross((1.0, 0.0, 0.0), self.up), (0.0, 1.0, 0.0))
        return v_norm(acc)

    def _read_left(self):
        """Left is where the bones NAMED Left are. Measured off the hips and
        shoulders, which is what makes a rig with its Left bones on +X land
        its clips correctly instead of mirrored."""
        acc = (0.0, 0.0, 0.0)
        for role in ("UpperLeg", "UpperArm", "Shoulder"):
            l, r = self.bones.get(LEFT + role), self.bones.get(RIGHT + role)
            if l and r:
                acc = v_add(acc, v_sub(l["head"], r["head"]))
        acc = v_sub(acc, v_scale(self.up, v_dot(acc, self.up)))
        if v_len(acc) < 1e-6:
            return v_cross(self.up, self.forward)
        return v_norm(acc)

    def has(self, *names):
        return all(n in self.bones for n in names)

    def bone_length(self, name):
        """Head to FIRST CHILD'S HEAD where a child exists — the length glTF
        carries — else head to tail."""
        b = self.bones[name]
        kids = [k for k, v in self.bones.items() if v.get("parent") == name]
        if kids:
            # The child that continues the chain: the same side's next bone,
            # or simply the nearest child head.
            return min(v_len(v_sub(self.bones[k]["head"], b["head"]))
                       for k in kids) or v_len(v_sub(b["tail"], b["head"]))
        return v_len(v_sub(b["tail"], b["head"]))

    def rest_dir(self, name):
        b = self.bones[name]
        return v_norm(v_sub(b["tail"], b["head"]))

    def height(self):
        zs = []
        for b in self.bones.values():
            zs.append(v_dot(b["head"], self.up))
            zs.append(v_dot(b["tail"], self.up))
        return (max(zs) - min(zs)) if zs else 1.0

    def leg_length(self, side=LEFT):
        return (self.lengths.get(side + "UpperLeg", 0.0)
                + self.lengths.get(side + "LowerLeg", 0.0))

    def ankle_height(self, side=LEFT):
        return v_dot(self.bones[side + "Foot"]["head"], self.up)

    def hip_height(self, side=LEFT):
        return v_dot(self.bones[side + "UpperLeg"]["head"], self.up)

    def summary(self):
        return {"forward": tuple(round(c, 4) for c in self.forward),
                "left": tuple(round(c, 4) for c in self.left),
                "up": tuple(round(c, 4) for c in self.up),
                "leg_length": round(self.leg_length(), 4) if self.has(
                    "LeftUpperLeg", "LeftLowerLeg") else None,
                "height": round(self.height(), 4), "bones": len(self.bones)}


# ---------------------------------------------------------------------------
# A pose, and forward kinematics over it
# ---------------------------------------------------------------------------

class Pose:
    """Bone-local rotation deltas on top of rest, plus a Hips translation.

    `rot[name]` is the rotation matrix a bone applies IN ITS OWN REST FRAME —
    Blender's `matrix_basis`, glTF's bone-local delta once composed onto rest.
    `hips_offset` is in ARMATURE space, metres, and lands on whichever bone is
    the topmost deforming trunk bone (Hips when present).
    """

    def __init__(self, rig: RigFrame):
        self.rig = rig
        self.rot = {}
        self.hips_offset = (0.0, 0.0, 0.0)
        self._cache = {}

    def copy(self):
        p = Pose(self.rig)
        p.rot = dict(self.rot)
        p.hips_offset = self.hips_offset
        return p

    def _invalidate(self):
        self._cache = {}

    def root_bone(self):
        return "Hips" if "Hips" in self.rig.bones else self.rig.order[0]

    # -- FK -----------------------------------------------------------------
    def world(self, name):
        """(R, head) of a bone in armature space under this pose. R's columns
        are the bone's posed local axes; head is where its head is."""
        if name in self._cache:
            return self._cache[name]
        b = self.rig.bones[name]
        rest = b["matrix"]
        q = self.rot.get(name)
        basis = q if q is not None else m_identity()
        parent = b.get("parent")
        if parent and parent in self.rig.bones:
            rp, hp = self.world(parent)
            rest_p = self.rig.bones[parent]["matrix"]
            # parent-relative rest: rest_p^T @ rest, and the head offset in
            # the parent's rest frame
            rel = m_mul(m_transpose(rest_p), rest)
            off = m_vec(m_transpose(rest_p),
                        v_sub(b["head"], self.rig.bones[parent]["head"]))
            r = m_mul(m_mul(rp, rel), basis)
            head = v_add(hp, m_vec(rp, off))
        else:
            r = m_mul(rest, basis)
            head = b["head"]
        if name == self.root_bone():
            head = v_add(head, self.hips_offset)
        self._cache[name] = (r, head)
        return r, head

    def world_dir(self, name):
        r, _ = self.world(name)
        return m_col(r, 1)

    def world_tail(self, name):
        r, head = self.world(name)
        return v_add(head, v_scale(m_col(r, 1),
                                   v_len(v_sub(self.rig.bones[name]["tail"],
                                               self.rig.bones[name]["head"]))))

    def _unposed_world(self, name):
        """The bone's armature-space rotation with ITS OWN delta removed (the
        parent chain still posed) — the frame a new delta composes onto."""
        b = self.rig.bones[name]
        parent = b.get("parent")
        if parent and parent in self.rig.bones:
            rp, _ = self.world(parent)
            rel = m_mul(m_transpose(self.rig.bones[parent]["matrix"]),
                        b["matrix"])
            return m_mul(rp, rel)
        return b["matrix"]

    # -- authoring primitives -----------------------------------------------
    def set_world_rotation(self, name, r_world):
        """Make the bone's posed armature-space rotation exactly `r_world`."""
        base = self._unposed_world(name)
        self.rot[name] = m_mul(m_transpose(base), r_world)
        self._invalidate()

    def aim(self, name, direction):
        """Point the bone along an armature-space direction, minimal twist,
        composed onto whatever its parents are doing."""
        base = self._unposed_world(name)
        cur = m_col(base, 1)
        delta = m_from_to(cur, v_norm(direction, cur))
        self.set_world_rotation(name, m_mul(delta, base))

    def rotate_about(self, name, axis_world, degrees):
        """Rotate the bone (and everything below it) about an armature-space
        axis, on top of its current pose."""
        if abs(degrees) < 1e-9 or name not in self.rig.bones:
            return
        r_cur, _ = self.world(name)
        delta = m_from_axis_angle(axis_world, math.radians(degrees))
        self.set_world_rotation(name, m_mul(delta, r_cur))

    def transported_axes(self, name):
        """The character's (left, forward, up) carried through the PARENT's
        pose — the anatomical frame a joint bends in."""
        b = self.rig.bones[name]
        parent = b.get("parent")
        if parent and parent in self.rig.bones:
            rp, _ = self.world(parent)
            carry = m_mul(rp, m_transpose(self.rig.bones[parent]["matrix"]))
            return (m_vec(carry, self.rig.left), m_vec(carry, self.rig.forward),
                    m_vec(carry, self.rig.up))
        return self.rig.left, self.rig.forward, self.rig.up

    def anatomical(self, name, pitch=0.0, yaw=0.0, roll=0.0):
        """Bend a joint in its anatomical frame, degrees.

        pitch  positive carries the DISTAL END toward the character's FRONT:
               a leg kicks, an arm reaches, the torso bends over, the head
               nods DOWN. Geometric, not a fixed axis — an up-pointing spine
               and a down-hanging arm flex in OPPOSITE rotation senses about
               the left axis, and a fixed "+pitch about left" sent every arm
               swing backwards while every torso bend went forward.
        yaw    about UP: positive turns toward the character's LEFT.
        roll   positive carries the distal end toward the character's LEFT.
        The frame is carried through the PARENT's pose, so an elbow folds
        forward relative to the upper arm wherever the upper arm is.
        """
        if name not in self.rig.bones:
            return
        left, fwd, up = self.transported_axes(name)
        if pitch:
            self.rotate_about(name, self._toward_axis(name, fwd, left), pitch)
        if yaw:
            self.rotate_about(name, up, yaw)
        if roll:
            self.rotate_about(name, self._toward_axis(name, left, fwd), roll)

    def _toward_axis(self, name, toward, fallback):
        """The axis a positive rotation about carries this bone's tip toward
        `toward`: cross(direction, toward). A bone already lying along
        `toward` (a foot, for forward) has no such axis and gets `fallback`."""
        axis = v_cross(self.world_dir(name), toward)
        return v_norm(axis, fallback) if v_len(axis) > 0.15 else fallback

    def set_hips(self, offset):
        self.hips_offset = tuple(offset)
        self._invalidate()

    # -- IK -----------------------------------------------------------------
    def leg_ik(self, side, ankle, pole=None, foot_dir=None, margin=0.995):
        """Two-bone IK: put the ankle at `ankle` (armature space) with the knee
        toward `pole` (default: forward). Returns the shortfall in metres when
        the target was out of reach — a number, not a silent clamp."""
        up_n, lo_n, foot_n = side + "UpperLeg", side + "LowerLeg", side + "Foot"
        if not self.rig.has(up_n, lo_n):
            return 0.0
        l1, l2 = self.rig.lengths[up_n], self.rig.lengths[lo_n]
        _, hip = self.world(up_n)
        d = v_sub(ankle, hip)
        dist = v_len(d)
        reach = (l1 + l2) * margin
        short = max(0.0, dist - reach)
        dist = max(1e-6, min(dist, reach))
        dn = v_norm(d, v_scale(self.rig.up, -1.0))
        pole = v_norm(pole or self.rig.forward)
        perp = v_sub(pole, v_scale(dn, v_dot(pole, dn)))
        if v_len(perp) < 1e-6:
            perp = self.rig.forward
        perp = v_norm(perp)
        cos_a = (l1 * l1 + dist * dist - l2 * l2) / (2.0 * l1 * dist)
        a = math.acos(max(-1.0, min(1.0, cos_a)))
        thigh = v_add(v_scale(dn, math.cos(a)), v_scale(perp, math.sin(a)))
        self.aim(up_n, thigh)
        knee = v_add(hip, v_scale(thigh, l1))
        self.aim(lo_n, v_sub(ankle, knee))
        if foot_dir is not None and foot_n in self.rig.bones:
            self.aim(foot_n, foot_dir)
        return short

    # -- baking -------------------------------------------------------------
    def quaternions(self):
        return {n: m_to_quat(r) for n, r in self.rot.items()}

    def hips_local(self):
        """The Hips translation in the bone's OWN rest frame — what Blender's
        pose_bone.location and glTF's translation channel want."""
        root = self.root_bone()
        return m_vec(m_transpose(self.rig.bones[root]["matrix"]),
                     self.hips_offset)


# ---------------------------------------------------------------------------
# Standing, the pose every clip starts from
# ---------------------------------------------------------------------------

def stand(rig: RigFrame, *, hips=(0.0, 0.0, 0.0), lean=0.0, twist=0.0,
          head_pitch=0.0, head_yaw=0.0, arm_out=12.0,
          arm_swing=(0.0, 0.0), elbow=(12.0, 12.0), feet=None,
          foot_pitch=(0.0, 0.0), knee_pole=None) -> Pose:
    """A whole-body standing pose in metres and degrees.

    hips        armature-space offset from rest (lateral/forward/up are the
                RIG's axes — pass `frame(rig, l, f, u)` to author in character
                terms).
    lean        forward bend of the torso, degrees, spread up the spine.
    twist       torso yaw toward the left, degrees.
    arm_out     how far the hanging arms sit out from the body, degrees.
    arm_swing   (left, right) forward swing of the upper arms, degrees.
    elbow       (left, right) forearm fold, degrees.
    feet        {side: ankle position (armature space)}; default is each
                foot's rest ankle. Solved by IK so the hips can move freely.
    foot_pitch  (left, right): +toes up (heel strike), -toes down (toe off).
    """
    p = Pose(rig)
    p.set_hips(hips)
    # THE SPINE BENDS CUMULATIVELY. Each bone takes a share of the total and
    # inherits the ones below it, which is what a bend is.
    for name, share in (("Spine", 0.35), ("Chest", 0.35), ("UpperChest", 0.30)):
        p.anatomical(name, pitch=lean * share, yaw=twist * share)
    p.anatomical("Neck", pitch=head_pitch * 0.4, yaw=head_yaw * 0.4)
    p.anatomical("Head", pitch=head_pitch * 0.6, yaw=head_yaw * 0.6)
    for i, side in enumerate(SIDES):
        sgn = 1.0 if side == LEFT else -1.0
        up_n, lo_n, hand = side + "UpperArm", side + "LowerArm", side + "Hand"
        if rig.has(up_n):
            # Hang: straight down, out by `arm_out`, then swing forward.
            down = v_scale(rig.up, -1.0)
            out = v_scale(rig.left, sgn)
            hang = v_norm(v_add(v_scale(down, math.cos(math.radians(arm_out))),
                                v_scale(out, math.sin(math.radians(arm_out)))))
            p.aim(up_n, hang)
            # THE SWING IS IN WORLD, LIKE THE HANG. A pitch in the torso's
            # transported frame rode the lean: a 75-degree reach on a body
            # bent 48 degrees over ended up pointing at the ceiling, and the
            # pickup's hand landed at the chin instead of the floor.
            if arm_swing[i]:
                p.rotate_about(up_n, v_norm(v_cross(hang, rig.forward), rig.left),
                               arm_swing[i])
        if rig.has(lo_n):
            p.aim(lo_n, p.world_dir(up_n) if rig.has(up_n) else rig.up)
            p.anatomical(lo_n, pitch=elbow[i])
        if rig.has(hand):
            p.aim(hand, p.world_dir(lo_n) if rig.has(lo_n) else rig.up)
    for i, side in enumerate(SIDES):
        foot_n = side + "Foot"
        if not rig.has(side + "UpperLeg", side + "LowerLeg", foot_n):
            continue
        target = (feet or {}).get(side) or rig.bones[foot_n]["head"]
        foot_rest = rig.rest_dir(foot_n)
        p.leg_ik(side, target, pole=knee_pole, foot_dir=foot_rest)
        if foot_pitch[i]:
            # +pitch (toes up) is a rotation about LEFT carrying the toe UP,
            # i.e. the opposite sign of anatomical pitch.
            p.rotate_about(foot_n, rig.left, -foot_pitch[i])
    return p


def frame(rig: RigFrame, left=0.0, forward=0.0, up=0.0):
    """Metres in CHARACTER terms -> an armature-space vector."""
    return v_add(v_add(v_scale(rig.left, left), v_scale(rig.forward, forward)),
                 v_scale(rig.up, up))


def rest_ankle(rig: RigFrame, side):
    return rig.bones[side + "Foot"]["head"]


# ---------------------------------------------------------------------------
# Gait
# ---------------------------------------------------------------------------

GAITS = {
    # stance: fraction of the cycle a foot is down. >0.5 overlaps into double
    # support (a walk); <0.5 leaves a flight phase (a run).
    "walk": {"stance": 0.62, "stride": 0.32, "lift": 0.09, "bob": 0.020,
             "sway": 0.018, "pelvis_yaw": 5.0, "lean": 4.0, "arm": 22.0,
             "elbow": (14.0, 26.0), "foot_roll": (14.0, -22.0), "cycle_s": 1.1},
    # lift_power < 1 makes the swing foot leave the ground FAST: a run's
    # flight is the window where the trailing foot has only just lifted, and
    # a sine lift kept it inside the contact band for the whole window —
    # measured as flight_fraction 0.0 on an exported run.
    "run": {"stance": 0.36, "stride": 0.50, "lift": 0.22, "bob": 0.035,
            "sway": 0.012, "pelvis_yaw": 9.0, "lean": 12.0, "arm": 42.0,
            "elbow": (80.0, 95.0), "foot_roll": (6.0, -30.0), "cycle_s": 0.70,
            "lift_power": 0.45},
    "sneak": {"stance": 0.70, "stride": 0.22, "lift": 0.07, "bob": 0.008,
              "sway": 0.010, "pelvis_yaw": 3.0, "lean": 18.0, "arm": 10.0,
              "elbow": (40.0, 50.0), "foot_roll": (4.0, -8.0), "cycle_s": 1.6},
}


def _foot_track(phase, stance, stride, lift, lift_power=1.0):
    """(forward, lift) of one foot at `phase` in [0,1). Stance recedes at ONE
    speed — on an in-place cycle the planted foot IS the ground, and a foot
    that recedes unevenly reads as the floor lurching."""
    if phase < stance:
        u = phase / stance
        return stride * (1.0 - 2.0 * u), 0.0
    u = (phase - stance) / (1.0 - stance)
    return (-stride + 2.0 * stride * ease_inout(u),
            lift * math.sin(math.pi * u) ** lift_power)


def gait_cycle(rig: RigFrame, kind="walk", frames=None, fps=30,
               overrides: Optional[dict] = None) -> tuple[list, dict]:
    """A looping in-place locomotion cycle. Returns (poses, notes).

    Every distance is a fraction of THIS RIG'S leg length, so the same
    parameters walk a 1.2 m child and a 2.2 m brute at their own stride. The
    hip height is DERIVED: the pelvis drops until the longest stride is still
    reachable with a bent knee, and bobs above that at mid-stance.
    """
    g = dict(GAITS[kind])
    g.update(overrides or {})
    leg = rig.leg_length()
    if leg <= 0.0:
        raise ValueError("this rig has no leg chain to walk on")
    n = int(frames or round(g["cycle_s"] * fps))
    stride = g["stride"] * leg          # half-stride: ankle reach fore/aft
    lift = g["lift"] * leg
    bob = g["bob"] * leg
    sway = g["sway"] * leg
    span = rig.hip_height() - rig.ankle_height()
    # Reachability at full split: hip-to-ankle must fit inside 0.96 of the
    # leg with a knee still bent. Whatever drop that needs is the base.
    need = math.sqrt(max(0.0, (0.96 * leg) ** 2 - stride ** 2))
    drop = max(0.0, span - need)
    notes = {"kind": kind, "frames": n, "leg_length": round(leg, 4),
             "half_stride": round(stride, 4), "hip_drop": round(drop, 4),
             "lift": round(lift, 4), "shortfall_frames": 0,
             "worst_shortfall": 0.0}
    poses = []
    hipx = {s: v_dot(v_sub(rig.bones[s + "UpperLeg"]["head"],
                           rig.bones["Hips"]["head"] if rig.has("Hips")
                           else (0.0, 0.0, 0.0)), rig.left) for s in SIDES}
    for i in range(n):
        phi = i / float(n)
        ph = {LEFT: phi, RIGHT: (phi + 0.5) % 1.0}
        # Pelvis: highest at mid-stance (phi=0.25, 0.75), lowest at the split.
        rise = bob * 0.5 * (1.0 - math.cos(4.0 * math.pi * phi))
        lat = sway * math.sin(2.0 * math.pi * phi)   # toward the stance foot
        hips = frame(rig, left=lat, up=-drop + rise)
        # Pelvis yaw leads with the forward leg; the torso counters it.
        pyaw = g["pelvis_yaw"] * math.cos(2.0 * math.pi * phi)
        feet, pitch = {}, []
        for side in SIDES:
            f, l = _foot_track(ph[side], g["stance"], stride, lift,
                               g.get("lift_power", 1.0))
            feet[side] = v_add(frame(rig, left=hipx[side], forward=f,
                                     up=rig.ankle_height() + l),
                               v_scale(rig.forward, 0.0))
            # Heel strike at the front of stance, toe-off at its end.
            p_s = ph[side]
            if p_s < g["stance"] * 0.25:
                pitch.append(g["foot_roll"][0] * (1.0 - p_s / (g["stance"] * 0.25)))
            elif p_s < g["stance"]:
                u = (p_s - g["stance"] * 0.6) / (g["stance"] * 0.4)
                pitch.append(g["foot_roll"][1] * ease_inout(u) if u > 0 else 0.0)
            else:
                u = (p_s - g["stance"]) / (1.0 - g["stance"])
                pitch.append(g["foot_roll"][1] * (1.0 - ease_inout(min(1.0, u * 2.0))))
        # Arms swing opposite their own leg: left foot forward, left arm back.
        swing = g["arm"] * math.cos(2.0 * math.pi * phi)
        arm_swing = (-swing, swing)
        e0, e1 = g["elbow"]
        elbow = (e0 + (e1 - e0) * max(0.0, -math.cos(2.0 * math.pi * phi)),
                 e0 + (e1 - e0) * max(0.0, math.cos(2.0 * math.pi * phi)))
        pose = stand(rig, hips=hips, lean=g["lean"], twist=-pyaw * 0.6,
                     head_yaw=0.0, arm_swing=arm_swing, elbow=elbow,
                     feet=feet, foot_pitch=tuple(pitch), arm_out=10.0)
        # Pelvis yaw is on the Hips bone itself, so the legs ride with it —
        # the feet are re-solved after, so they stay where they were put.
        if rig.has("Hips"):
            pose.rotate_about("Hips", rig.up, pyaw)
            for j, side in enumerate(SIDES):
                short = pose.leg_ik(side, feet[side],
                                    foot_dir=rig.rest_dir(side + "Foot"))
                if pitch[j]:
                    pose.rotate_about(side + "Foot", rig.left, -pitch[j])
                if short > 1e-4:
                    notes["shortfall_frames"] += 1
                    notes["worst_shortfall"] = max(notes["worst_shortfall"],
                                                   round(short, 4))
            # The head stays on the horizon while the pelvis and torso turn.
            pose.anatomical("Head", yaw=pyaw * 0.4)
        poses.append(pose)
    return poses, notes


# ---------------------------------------------------------------------------
# Idle and the keyed clips
# ---------------------------------------------------------------------------

def idle_cycle(rig: RigFrame, frames=90, fps=30, energy=1.0) -> tuple[list, dict]:
    """Breathing, a slow weight shift, a drifting head. Loops exactly."""
    n = int(frames)
    leg = rig.leg_length() or rig.height() * 0.45
    poses = []
    for i in range(n):
        t = i / float(n)
        w = 2.0 * math.pi
        shift = 0.018 * leg * energy * math.sin(w * t)          # one cycle
        breath = 1.6 * energy * math.sin(2.0 * w * t)           # two breaths
        drift = 5.0 * energy * math.sin(w * t + 1.1)
        hips = frame(rig, left=shift, up=-0.004 * leg * (1.0 - math.cos(2 * w * t)))
        pose = stand(rig, hips=hips, lean=1.5 + breath * 0.4, head_yaw=drift,
                     head_pitch=1.0 + breath * 0.3,
                     arm_swing=(1.5 * math.sin(w * t), -1.5 * math.sin(w * t)),
                     elbow=(14.0, 14.0), arm_out=11.0)
        pose.anatomical("Chest", pitch=-breath * 0.5)
        poses.append(pose)
    return poses, {"kind": "idle", "frames": n}


# A keyed clip speaks in these parameters, interpolated between keys and then
# solved into a pose — so an author writes "lean 45, hips down 0.2, left hand
# reaching" and never a bone rotation.
KEY_FIELDS = {
    "hips_left": 0.0, "hips_forward": 0.0, "hips_up": 0.0,   # metres
    "lean": 0.0, "twist": 0.0, "head_pitch": 0.0, "head_yaw": 0.0,
    "arm_out": 12.0, "arm_swing_l": 0.0, "arm_swing_r": 0.0,
    "elbow_l": 12.0, "elbow_r": 12.0,
    "reach_l": 0.0, "reach_r": 0.0,      # raise the upper arm forward-up, deg
    "foot_l_forward": 0.0, "foot_r_forward": 0.0,   # metres from rest
    "foot_l_up": 0.0, "foot_r_up": 0.0,
    "foot_pitch_l": 0.0, "foot_pitch_r": 0.0,
}


def _lerp_fields(a, b, u):
    return {k: a.get(k, KEY_FIELDS[k]) + (b.get(k, KEY_FIELDS[k])
                                          - a.get(k, KEY_FIELDS[k])) * u
            for k in KEY_FIELDS}


def solve_fields(rig: RigFrame, f: dict) -> Pose:
    hips = frame(rig, f["hips_left"], f["hips_forward"], f["hips_up"])
    feet = {}
    for side, tag in ((LEFT, "l"), (RIGHT, "r")):
        if rig.has(side + "Foot"):
            feet[side] = v_add(rest_ankle(rig, side),
                               frame(rig, forward=f["foot_%s_forward" % tag],
                                     up=f["foot_%s_up" % tag]))
    pose = stand(rig, hips=hips, lean=f["lean"], twist=f["twist"],
                 head_pitch=f["head_pitch"], head_yaw=f["head_yaw"],
                 arm_out=f["arm_out"],
                 arm_swing=(f["arm_swing_l"] + f["reach_l"],
                            f["arm_swing_r"] + f["reach_r"]),
                 elbow=(f["elbow_l"], f["elbow_r"]), feet=feet,
                 foot_pitch=(f["foot_pitch_l"], f["foot_pitch_r"]))
    return pose


def keyed_clip(rig: RigFrame, keys: list, fps=30, loop=False,
               ease="inout") -> tuple[list, dict]:
    """Poses from [{"t": seconds, <KEY_FIELDS>...}, ...], eased between keys.

    A looping clip must end where it starts; if the last key's time is the
    clip length and its fields differ from the first's, the first key is
    appended at that time so the loop closes.
    """
    if not keys:
        raise ValueError("a keyed clip needs at least one key")
    keys = sorted(({**KEY_FIELDS, **k} for k in keys), key=lambda k: k["t"])
    length = keys[-1]["t"]
    if loop and any(abs(keys[0][f] - keys[-1][f]) > 1e-9 for f in KEY_FIELDS):
        keys.append({**keys[0], "t": length + (length / max(len(keys) - 1, 1))})
        length = keys[-1]["t"]
    n = max(1, int(round(length * fps)))
    fn = EASES.get(ease, ease_inout)
    poses = []
    for i in range(n if loop else n + 1):
        t = i / float(fps)
        lo = max((k for k in keys if k["t"] <= t), key=lambda k: k["t"])
        hi = min((k for k in keys if k["t"] >= t), key=lambda k: k["t"],
                 default=lo)
        span = hi["t"] - lo["t"]
        u = 0.0 if span <= 1e-9 else fn((t - lo["t"]) / span)
        poses.append(solve_fields(rig, _lerp_fields(lo, hi, u)))
    unknown = sorted({k for key in keys for k in key} - set(KEY_FIELDS) - {"t"})
    return poses, {"kind": "keyed", "frames": len(poses), "keys": len(keys),
                   "ignored_fields": unknown}


# The shipped vocabulary. Each preset is a keyed clip in character terms; the
# numbers are fractions of leg length where they are distances.
def presets(rig: RigFrame) -> dict:
    leg = rig.leg_length() or rig.height() * 0.45
    return {
        "crouch_idle": {"loop": True, "keys": [
            {"t": 0.0, "hips_up": -0.32 * leg, "hips_forward": 0.02 * leg,
             "lean": 22.0, "head_pitch": -8.0, "elbow_l": 55.0, "elbow_r": 55.0,
             "arm_swing_l": 20.0, "arm_swing_r": 20.0, "arm_out": 16.0,
             "foot_l_forward": 0.04 * leg, "foot_r_forward": -0.04 * leg},
            {"t": 1.5, "hips_up": -0.33 * leg, "hips_forward": 0.02 * leg,
             "lean": 24.0, "head_pitch": -6.0, "elbow_l": 57.0, "elbow_r": 57.0,
             "arm_swing_l": 22.0, "arm_swing_r": 22.0, "arm_out": 16.0,
             "foot_l_forward": 0.04 * leg, "foot_r_forward": -0.04 * leg},
            {"t": 3.0, "hips_up": -0.32 * leg, "hips_forward": 0.02 * leg,
             "lean": 22.0, "head_pitch": -8.0, "elbow_l": 55.0, "elbow_r": 55.0,
             "arm_swing_l": 20.0, "arm_swing_r": 20.0, "arm_out": 16.0,
             "foot_l_forward": 0.04 * leg, "foot_r_forward": -0.04 * leg}]},
        # Bends AND squats, both hands to the floor in front of the feet,
        # straightens cradling what it picked up at the chest.
        "pickup": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.55, "hips_up": -0.30 * leg, "hips_forward": -0.04 * leg,
             "lean": 50.0, "head_pitch": 25.0, "reach_r": 40.0, "reach_l": 40.0,
             "elbow_r": 8.0, "elbow_l": 8.0, "arm_out": 14.0,
             "foot_l_forward": 0.08 * leg, "foot_r_forward": -0.06 * leg},
            {"t": 0.85, "hips_up": -0.32 * leg, "hips_forward": -0.04 * leg,
             "lean": 54.0, "head_pitch": 28.0, "reach_r": 44.0, "reach_l": 44.0,
             "elbow_r": 6.0, "elbow_l": 6.0, "arm_out": 14.0,
             "foot_l_forward": 0.08 * leg, "foot_r_forward": -0.06 * leg},
            {"t": 1.25, "hips_up": -0.10 * leg, "lean": 18.0, "head_pitch": 6.0,
             "reach_r": 30.0, "reach_l": 30.0, "elbow_r": 90.0, "elbow_l": 90.0,
             "arm_out": 10.0, "foot_l_forward": 0.04 * leg,
             "foot_r_forward": -0.03 * leg},
            {"t": 1.7, "lean": 3.0, "reach_r": 28.0, "reach_l": 28.0,
             "elbow_r": 100.0, "elbow_l": 100.0, "arm_out": 8.0}]},
        "look_around": {"loop": True, "keys": [
            {"t": 0.0, "lean": 1.0},
            {"t": 0.7, "lean": -2.0, "head_yaw": 45.0, "twist": 12.0,
             "head_pitch": -3.0},
            {"t": 1.3, "lean": -2.0, "head_yaw": 40.0, "twist": 10.0},
            {"t": 2.1, "lean": -1.0, "head_yaw": -40.0, "twist": -10.0,
             "head_pitch": -2.0},
            {"t": 2.7, "lean": -1.0, "head_yaw": -35.0, "twist": -9.0},
            {"t": 3.4, "lean": 1.0}]},
        "wave": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.4, "reach_r": 150.0, "elbow_r": 60.0, "head_yaw": 6.0},
            {"t": 0.7, "reach_r": 155.0, "elbow_r": 95.0, "head_yaw": 6.0},
            {"t": 1.0, "reach_r": 150.0, "elbow_r": 55.0, "head_yaw": 6.0},
            {"t": 1.3, "reach_r": 155.0, "elbow_r": 95.0, "head_yaw": 6.0},
            {"t": 1.9}]},
        "hit": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.12, "lean": -18.0, "hips_forward": -0.06 * leg,
             "head_pitch": -14.0, "arm_swing_l": 25.0, "arm_swing_r": 25.0,
             "elbow_l": 40.0, "elbow_r": 40.0},
            {"t": 0.5, "lean": 6.0, "hips_forward": -0.02 * leg},
            {"t": 0.8}]},
        "jump": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.25, "hips_up": -0.28 * leg, "lean": 20.0,
             "arm_swing_l": -35.0, "arm_swing_r": -35.0},
            {"t": 0.45, "hips_up": 0.25 * leg, "lean": -4.0,
             "arm_swing_l": 60.0, "arm_swing_r": 60.0,
             "foot_l_up": 0.25 * leg, "foot_r_up": 0.25 * leg,
             "foot_l_forward": -0.05 * leg, "foot_r_forward": -0.05 * leg},
            {"t": 0.7, "hips_up": 0.20 * leg, "lean": 2.0,
             "arm_swing_l": 30.0, "arm_swing_r": 30.0,
             "foot_l_up": 0.20 * leg, "foot_r_up": 0.20 * leg},
            {"t": 0.95, "hips_up": -0.22 * leg, "lean": 16.0,
             "arm_swing_l": -10.0, "arm_swing_r": -10.0},
            {"t": 1.3}]},
    }


CLIP_KINDS = ("idle", "walk", "run", "sneak", "crouch_idle", "pickup",
              "look_around", "wave", "hit", "jump", "keyed")


def build_clip(rig: RigFrame, spec: dict, fps=30) -> tuple[list, dict]:
    """One clip from its spec: {"name", "kind", ...}. Returns (poses, notes).

    kind  idle | walk | run | sneak — cycles, parameters under "overrides"
          crouch_idle | pickup | look_around | wave | hit | jump — presets
          keyed — {"keys": [...], "loop": bool, "ease": str}
    """
    kind = spec.get("kind") or spec.get("name")
    if kind in GAITS:
        poses, notes = gait_cycle(rig, kind, frames=spec.get("frames"),
                                  fps=fps, overrides=spec.get("overrides"))
        notes["loop"] = True
    elif kind == "idle":
        poses, notes = idle_cycle(rig, frames=spec.get("frames", 90), fps=fps,
                                  energy=float(spec.get("energy", 1.0)))
        notes["loop"] = True
    elif kind == "keyed":
        poses, notes = keyed_clip(rig, spec.get("keys") or [], fps=fps,
                                  loop=bool(spec.get("loop")),
                                  ease=spec.get("ease", "inout"))
        notes["loop"] = bool(spec.get("loop"))
    elif kind in presets(rig):
        pre = presets(rig)[kind]
        poses, notes = keyed_clip(rig, pre["keys"], fps=fps, loop=pre["loop"])
        notes["kind"] = kind
        notes["loop"] = pre["loop"]
    else:
        raise ValueError("unknown clip kind %r — one of %s"
                         % (kind, ", ".join(CLIP_KINDS)))
    notes["name"] = spec.get("name") or kind
    return poses, notes


def bake(rig: RigFrame, poses: list) -> dict:
    """Per-bone (w,x,y,z) per frame, plus the root's bone-local translation.

    Every frame carries every bone the rig has, identity where the pose said
    nothing — a channel that appears on frame 12 and not on frame 11 is a pop.
    """
    names = [n for n in rig.order]
    rot = {n: [] for n in names}
    loc = []
    for p in poses:
        q = p.quaternions()
        for n in names:
            rot[n].append(q.get(n, Q_IDENTITY))
        loc.append(p.hips_local())
    # Hemisphere-continuous quaternion tracks: q and -q are the same rotation
    # and a sign flip between frames is read as a full turn by anything that
    # differences the samples.
    for n in names:
        track = rot[n]
        for i in range(1, len(track)):
            if sum(a * b for a, b in zip(track[i - 1], track[i])) < 0.0:
                track[i] = tuple(-c for c in track[i])
    return {"bones": names, "rotations": rot, "root": rig.bones and
            ("Hips" if "Hips" in rig.bones else rig.order[0]),
            "root_location": loc, "frames": len(poses)}


def bake_clips(rig_dump: dict, clips: list, fps=30) -> dict:
    """The entry point the Blender script calls: a rig dump in, baked tracks
    out, one per clip spec. Never raises for one bad clip — it is reported."""
    rig = RigFrame(rig_dump["bones"], up=tuple(rig_dump.get("up") or (0, 0, 1)))
    out = {"rig": rig.summary(), "clips": [], "fps": fps}
    for spec in clips:
        try:
            poses, notes = build_clip(rig, spec, fps=fps)
        except Exception as exc:  # one clip's spec must not sink the rest
            out["clips"].append({"name": spec.get("name") or spec.get("kind"),
                                 "ok": False, "error": "%s: %s"
                                 % (type(exc).__name__, exc)})
            continue
        baked = bake(rig, poses)
        out["clips"].append({"name": notes["name"], "ok": True, "notes": notes,
                             "loop": bool(notes.get("loop")), **baked})
    return out


# ---------------------------------------------------------------------------
# The canonical rig, for tests and for authoring without Blender
# ---------------------------------------------------------------------------

def _bone(parent, head, tail, x_axis):
    """A rest matrix from the bone's direction and its local X axis."""
    y = v_norm(v_sub(tail, head))
    x = v_norm(v_sub(x_axis, v_scale(y, v_dot(x_axis, y))))
    z = v_cross(x, y)
    m = ((x[0], y[0], z[0]), (x[1], y[1], z[1]), (x[2], y[2], z[2]))
    return {"parent": parent, "head": head, "tail": tail, "matrix": m}


def canonical_rig() -> dict:
    """The shipped humanoid template's rest pose, transcribed from
    templates/humanoid/humanoid_skeleton.glb (T-pose, faces +Y, left on -X).
    Every axis the generators rely on is MEASURED off this, not assumed."""
    B = {}
    B["Root"] = _bone(None, (0, 0, 0), (0, 0, 0.943), (-1, 0, 0))
    trunk = [("Hips", 0.943, 1.043), ("Spine", 1.119, 1.307),
             ("Chest", 1.307, 1.380), ("UpperChest", 1.380, 1.448),
             ("Neck", 1.448, 1.468), ("Head", 1.469, 1.680)]
    parent = "Root"
    for name, h, t in trunk:
        B[name] = _bone(parent, (0, 0, h), (0, 0, t), (-1, 0, 0))
        parent = name
    for side, s in ((LEFT, -1.0), (RIGHT, 1.0)):
        B[side + "Shoulder"] = _bone("UpperChest", (s * 0.055, 0, 1.433),
                                     (s * 0.144, 0, 1.448), (0, 0, -s))
        B[side + "UpperArm"] = _bone(side + "Shoulder", (s * 0.144, 0, 1.448),
                                     (s * 0.432, 0, 1.448), (0, 0, -s))
        B[side + "LowerArm"] = _bone(side + "UpperArm", (s * 0.432, 0, 1.448),
                                     (s * 0.693, 0, 1.448), (0, 0, -s))
        B[side + "Hand"] = _bone(side + "LowerArm", (s * 0.693, 0, 1.448),
                                 (s * 0.954, 0, 1.448), (0, 0, -s))
        B[side + "UpperLeg"] = _bone("Hips", (s * 0.073, 0, 0.875),
                                     (s * 0.073, 0, 0.435), (1, 0, 0))
        B[side + "LowerLeg"] = _bone(side + "UpperLeg", (s * 0.073, 0, 0.435),
                                     (s * 0.073, 0, 0.068), (1, 0, 0))
        B[side + "Foot"] = _bone(side + "LowerLeg", (s * 0.073, 0, 0.068),
                                 (s * 0.073, 0.112, 0.029), (1, 0, 0))
        B[side + "Toes"] = _bone(side + "Foot", (s * 0.073, 0.112, 0.029),
                                 (s * 0.073, 0.230, 0.012), (1, 0, 0))
    return {"bones": B, "up": (0.0, 0.0, 1.0)}
