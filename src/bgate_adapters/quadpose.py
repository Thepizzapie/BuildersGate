"""Quadruped poses and gaits — the four-legged half of humanpose.

PURE PYTHON, NO bpy, and spliced into the Blender script BY SOURCE after
humanpose.py (see blender._with_humanpose), which is why the import below is
guarded: inside Blender the humanpose names are already globals.

THE SAME RULES AS humanpose, RESTATED FOR FOUR LEGS:
  * The rig is MEASURED. Forward runs from the pelvis to the head; left is
    where the bones named Left are. A skeleton whose Left legs sit on +X
    animates correctly.
  * Every distance is a fraction of THAT LEG's length, so a 0.3 m cat and a
    1.6 m horse stride at their own scale.
  * Legs are solved by two-bone IK (humanpose.Pose.chain_ik) with the middle
    joint sent the way the animal bends: the ELBOW points BACKWARD, the
    STIFLE points FORWARD. Feet stay where they are put.
  * Gaits are footfall TIMING tables, which is what separates a walk from a
    trot from a gallop — not the speed. walk: lateral sequence, four beats,
    three feet down at once. trot: diagonal pairs, two beats, a suspension
    between them. gallop: transverse, a flight phase after the leading fore.

BONE NAMES (blender.QUADRUPED_BONES): Root, Hips, Spine, Chest, Neck, Head,
Tail1, Tail2, and per leg <Left|Right><Front|Back>{UpperLeg, LowerLeg, Foot}.
No engine profile names these — Godot has no quadruped SkeletonProfile — so
they are chosen to read, and animation_contacts takes them as `feet`.
"""
from __future__ import annotations

import math
from typing import Optional

try:
    from bgate_adapters.humanpose import (  # noqa: F401
        Pose, RigFrame, Q_IDENTITY, bake, ease_inout, m_to_quat,
        v_add, v_cross, v_dot, v_len, v_norm, v_scale, v_sub, _bone,
    )
except ImportError:  # spliced after humanpose's source inside Blender
    pass

LEGS = ("LeftFront", "RightFront", "LeftBack", "RightBack")
FRONT = ("LeftFront", "RightFront")
BACK = ("LeftBack", "RightBack")
QUAD_TRUNK = ("Hips", "Spine", "Chest", "Neck", "Head")
QUAD_TAIL = ("Tail1", "Tail2")


def is_quadruped(bones: dict) -> bool:
    """A rig with front legs is a quadruped; the humanoid path never names
    one. Decided by the bones, not by a caller's word."""
    return any(leg + "UpperLeg" in bones for leg in FRONT)


class QuadFrame(RigFrame):
    """A quadruped skeleton's rest pose and its measured axes."""

    def _read_forward(self):
        """Forward runs from the pelvis to the head. A quadruped's feet all
        point the same way but a paw bone is short and a hoof is a nub; the
        trunk is long and unambiguous."""
        tail = self.bones.get("Hips")
        nose = self.bones.get("Head") or self.bones.get("Neck")
        if tail is None or nose is None:
            fronts = [self.bones[l + "UpperLeg"]["head"] for l in FRONT
                      if l + "UpperLeg" in self.bones]
            backs = [self.bones[l + "UpperLeg"]["head"] for l in BACK
                     if l + "UpperLeg" in self.bones]
            if not fronts or not backs:
                return v_norm(v_cross((1.0, 0.0, 0.0), self.up), (0.0, 1.0, 0.0))
            a = v_scale(fronts[0], 1.0 / len(fronts))
            for p in fronts[1:]:
                a = v_add(a, v_scale(p, 1.0 / len(fronts)))
            b = v_scale(backs[0], 1.0 / len(backs))
            for p in backs[1:]:
                b = v_add(b, v_scale(p, 1.0 / len(backs)))
            d = v_sub(a, b)
        else:
            d = v_sub(nose["head"], tail["head"])
        d = v_sub(d, v_scale(self.up, v_dot(d, self.up)))
        if v_len(d) < 1e-6:
            return v_norm(v_cross((1.0, 0.0, 0.0), self.up), (0.0, 1.0, 0.0))
        return v_norm(d)

    def _read_left(self):
        acc = (0.0, 0.0, 0.0)
        for pair in (("LeftFront", "RightFront"), ("LeftBack", "RightBack")):
            l = self.bones.get(pair[0] + "UpperLeg")
            r = self.bones.get(pair[1] + "UpperLeg")
            if l and r:
                acc = v_add(acc, v_sub(l["head"], r["head"]))
        acc = v_sub(acc, v_scale(self.up, v_dot(acc, self.up)))
        if v_len(acc) < 1e-6:
            return v_cross(self.up, self.forward)
        return v_norm(acc)

    def legs(self):
        return [l for l in LEGS if self.has(l + "UpperLeg", l + "LowerLeg")]

    def leg_length(self, leg="LeftFront"):
        return (self.lengths.get(leg + "UpperLeg", 0.0)
                + self.lengths.get(leg + "LowerLeg", 0.0))

    def paw(self, leg):
        """Where the foot bone starts at rest — the IK target's home."""
        foot = self.bones.get(leg + "Foot")
        if foot is not None:
            return foot["head"]
        return self.bones[leg + "LowerLeg"]["tail"]

    def paw_height(self, leg):
        return v_dot(self.paw(leg), self.up)

    def shoulder_height(self, leg):
        return v_dot(self.bones[leg + "UpperLeg"]["head"], self.up)

    def summary(self):
        legs = self.legs()
        return {"forward": tuple(round(c, 4) for c in self.forward),
                "left": tuple(round(c, 4) for c in self.left),
                "up": tuple(round(c, 4) for c in self.up),
                "kind": "quadruped", "legs": legs,
                "leg_length": {l: round(self.leg_length(l), 4) for l in legs},
                "height": round(self.height(), 4), "bones": len(self.bones)}


def frame(rig, left=0.0, forward=0.0, up=0.0):
    return v_add(v_add(v_scale(rig.left, left), v_scale(rig.forward, forward)),
                 v_scale(rig.up, up))


def pole_for(rig: QuadFrame, leg: str):
    """The way the middle joint bends: elbows back, stifles forward."""
    return v_scale(rig.forward, -1.0) if leg in FRONT else rig.forward


# ---------------------------------------------------------------------------
# Standing
# ---------------------------------------------------------------------------

def quad_stand(rig: QuadFrame, *, hips=(0.0, 0.0, 0.0), body_pitch=0.0,
               spine_pitch=0.0, neck_pitch=0.0, head_pitch=0.0, head_yaw=0.0,
               tail_pitch=0.0, tail_yaw=0.0, feet=None, foot_pitch=None) -> Pose:
    """A whole-body standing pose in metres and degrees.

    body_pitch  the whole trunk about the pelvis (+ nose down), degrees.
    spine_pitch flex of the back between pelvis and chest (+ arches down).
    neck/head   + carries the nose down; head_yaw + turns toward the left.
    tail        + carries the tail tip down / toward the left.
    feet        {leg: paw position (armature space)}; default rest.
    foot_pitch  {leg: degrees, + toes up}.
    """
    p = Pose(rig)
    p.set_hips(hips)
    if body_pitch and rig.has("Hips"):
        p.anatomical("Hips", pitch=body_pitch)
    for name, share in (("Spine", 0.55), ("Chest", 0.45)):
        if spine_pitch:
            p.anatomical(name, pitch=spine_pitch * share)
    p.anatomical("Neck", pitch=neck_pitch, yaw=head_yaw * 0.4)
    p.anatomical("Head", pitch=head_pitch, yaw=head_yaw * 0.6)
    for name, share in (("Tail1", 0.5), ("Tail2", 0.5)):
        if tail_pitch or tail_yaw:
            p.anatomical(name, pitch=tail_pitch * share, yaw=tail_yaw * share)
    for leg in rig.legs():
        target = (feet or {}).get(leg) or rig.paw(leg)
        foot_n = leg + "Foot"
        foot_dir = rig.rest_dir(foot_n) if foot_n in rig.bones else None
        p.chain_ik(leg + "UpperLeg", leg + "LowerLeg", target,
                   pole=pole_for(rig, leg), end=foot_n, end_dir=foot_dir)
        pitch = (foot_pitch or {}).get(leg, 0.0)
        if pitch and foot_n in rig.bones:
            p.rotate_about(foot_n, rig.left, -pitch)
    return p


# ---------------------------------------------------------------------------
# Gaits
# ---------------------------------------------------------------------------

# phase: when each foot STRIKES, as a fraction of the cycle. stance: fraction
# of the cycle a foot is down. Footfall order is the identity of the gait.
QUAD_GAITS = {
    # Lateral-sequence walk: LB, LF, RB, RF a quarter cycle apart; each foot
    # down ~70% so three feet are on the ground most of the time.
    "walk": {"phase": {"LeftBack": 0.0, "LeftFront": 0.25,
                       "RightBack": 0.5, "RightFront": 0.75},
             "stance": 0.70, "stride": 0.30, "lift": 0.10, "bob": 0.015,
             "sway": 0.02, "pitch": 1.5, "spine": 0.0, "head_bob": 3.0,
             "tail": 6.0, "cycle_s": 1.1},
    # Trot: diagonal pairs (LF+RB, RF+LB) half a cycle apart, each down under
    # half the cycle so there is a suspension between beats.
    "trot": {"phase": {"LeftFront": 0.0, "RightBack": 0.0,
                       "RightFront": 0.5, "LeftBack": 0.5},
             "stance": 0.44, "stride": 0.40, "lift": 0.16, "bob": 0.03,
             "sway": 0.0, "pitch": 0.0, "spine": 0.0, "head_bob": 2.0,
             "tail": 3.0, "cycle_s": 0.6, "lift_power": 0.6},
    # Transverse gallop: hind pair strikes first, close together, then the
    # fore pair; flight after the leading fore leaves. The back flexes and
    # extends with it.
    "gallop": {"phase": {"LeftBack": 0.0, "RightBack": 0.10,
                         "LeftFront": 0.40, "RightFront": 0.50},
               "stance": 0.32, "stride": 0.55, "lift": 0.22, "bob": 0.05,
               "sway": 0.0, "pitch": 5.0, "spine": 14.0, "head_bob": 6.0,
               "tail": 4.0, "cycle_s": 0.5, "lift_power": 0.5},
}


def _foot_track(phase, stance, stride, lift, lift_power=1.0):
    """(forward, lift) of one foot at `phase` in [0,1). Stance recedes at one
    speed — on an in-place cycle the planted foot IS the ground."""
    if phase < stance:
        u = phase / stance
        return stride * (1.0 - 2.0 * u), 0.0
    u = (phase - stance) / (1.0 - stance)
    return (-stride + 2.0 * stride * ease_inout(u),
            lift * math.sin(math.pi * u) ** lift_power)


def quad_gait_cycle(rig: QuadFrame, kind="walk", frames=None, fps=30,
                    overrides: Optional[dict] = None) -> tuple[list, dict]:
    """A looping in-place cycle. Returns (poses, notes).

    The body height is DERIVED per gait: the pelvis and shoulders drop until
    the longest stride is reachable with the joint still bent, then bob above
    that. Front and back legs each use their own length.
    """
    g = dict(QUAD_GAITS[kind])
    g.update(overrides or {})
    legs = rig.legs()
    if not legs:
        raise ValueError("this rig has no leg chains to walk on")
    n = int(frames or round(g["cycle_s"] * fps))
    notes = {"kind": kind, "frames": n, "shortfall_frames": 0,
             "worst_shortfall": 0.0, "legs": legs}
    # Per-leg scale: strides and lifts are fractions of that leg.
    stride = {l: g["stride"] * rig.leg_length(l) for l in legs}
    lift = {l: g["lift"] * rig.leg_length(l) for l in legs}
    ref = max(rig.leg_length(l) for l in legs)
    bob = g["bob"] * ref
    sway = g["sway"] * ref
    notes["body_drop"] = 0.0
    poses = []
    for i in range(n):
        phi = i / float(n)
        rise = bob * 0.5 * (1.0 - math.cos(4.0 * math.pi * phi))
        lat = sway * math.sin(2.0 * math.pi * phi)
        feet, foot_pitch = {}, {}
        for l in legs:
            ph = (phi - g["phase"].get(l, 0.0)) % 1.0
            f, up = _foot_track(ph, g["stance"], stride[l], lift[l],
                                g.get("lift_power", 1.0))
            home = rig.paw(l)
            feet[l] = v_add(home, frame(rig, forward=f, up=up))
            if ph >= g["stance"]:
                u = (ph - g["stance"]) / (1.0 - g["stance"])
                foot_pitch[l] = -18.0 * math.sin(math.pi * u)   # toes trail
        # Gallop: the back arches when the hind legs gather and extends as
        # the fore legs reach.
        spine = g["spine"] * math.cos(2.0 * math.pi * phi)
        pitch = g["pitch"] * math.sin(2.0 * math.pi * phi)
        head = -g["head_bob"] * math.cos(2.0 * math.pi * (phi + 0.1))
        tail = g["tail"] * math.sin(2.0 * math.pi * phi)
        trunk = dict(body_pitch=pitch, spine_pitch=spine,
                     neck_pitch=head * 0.5, head_pitch=head * 0.5,
                     tail_yaw=tail, tail_pitch=-abs(tail) * 0.3)
        pose = quad_stand(rig, hips=frame(rig, left=lat, up=rise), **trunk)
        # THE BODY HEIGHT IS DERIVED PER FRAME, AFTER THE TRUNK HAS MOVED. A
        # pitched or arched back carries the shoulders up and away from the
        # paws; measured on the canonical rig, a 5-degree gallop pitch put
        # the front paws 8.7 cm out of reach on 12 of 15 frames and they
        # never touched the ground. So: pose the trunk, read where each leg
        # now starts, and drop the whole body until the furthest paw fits
        # inside 0.96 of its leg with the joint still bent.
        drop = 0.0
        for l in legs:
            _, top = pose.world(l + "UpperLeg")
            d = v_sub(feet[l], top)
            vert = v_dot(d, rig.up)
            horiz = v_len(v_sub(d, v_scale(rig.up, vert)))
            reach = 0.96 * rig.leg_length(l)
            need = math.sqrt(max(0.0, reach * reach - horiz * horiz))
            drop = max(drop, -vert - need)
        if drop > 0.0:
            pose.set_hips(frame(rig, left=lat, up=rise - drop))
        notes["body_drop"] = max(notes["body_drop"], round(drop, 4))
        for l in legs:
            foot_n = l + "Foot"
            short = pose.chain_ik(l + "UpperLeg", l + "LowerLeg", feet[l],
                                  pole=pole_for(rig, l), end=foot_n,
                                  end_dir=rig.rest_dir(foot_n)
                                  if foot_n in rig.bones else None)
            if foot_pitch.get(l) and foot_n in rig.bones:
                pose.rotate_about(foot_n, rig.left, -foot_pitch[l])
            if short > 1e-4:
                notes["shortfall_frames"] += 1
                notes["worst_shortfall"] = max(notes["worst_shortfall"],
                                               round(short, 4))
        poses.append(pose)
    return poses, notes


def quad_idle_cycle(rig: QuadFrame, frames=90, fps=30, energy=1.0):
    """Breathing, a weight shift, an ear-less head drift, a tail swish."""
    n = int(frames)
    ref = max([rig.leg_length(l) for l in rig.legs()] or [rig.height() * 0.5])
    poses = []
    for i in range(n):
        t = i / float(n)
        w = 2.0 * math.pi
        breath = 1.2 * energy * math.sin(2.0 * w * t)
        shift = 0.012 * ref * energy * math.sin(w * t)
        hips = frame(rig, left=shift, up=-0.003 * ref * (1.0 - math.cos(2 * w * t)))
        pose = quad_stand(rig, hips=hips, spine_pitch=-breath * 0.6,
                          neck_pitch=1.0 + breath * 0.4,
                          head_yaw=6.0 * energy * math.sin(w * t + 0.9),
                          head_pitch=2.0 * energy * math.sin(w * t + 2.0),
                          tail_yaw=12.0 * energy * math.sin(3.0 * w * t),
                          tail_pitch=-4.0 * energy)
        poses.append(pose)
    return poses, {"kind": "idle", "frames": n}


def quad_presets(rig: QuadFrame) -> dict:
    """Keyed clips in character terms: sit, lie, alert, pounce."""
    ref = max([rig.leg_length(l) for l in rig.legs()] or [rig.height() * 0.5])
    return {
        "sit": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.6, "hips_up": -0.25 * ref, "body_pitch": -18.0,
             "spine_pitch": -8.0, "neck_pitch": -8.0,
             "back_forward": 0.35 * ref},
            {"t": 1.2, "hips_up": -0.42 * ref, "body_pitch": -30.0,
             "spine_pitch": -12.0, "neck_pitch": -12.0,
             "back_forward": 0.55 * ref}]},
        "alert": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.25, "neck_pitch": -14.0, "head_pitch": -6.0,
             "tail_pitch": -25.0, "hips_up": 0.03 * ref},
            {"t": 0.9, "neck_pitch": -14.0, "head_pitch": -6.0,
             "head_yaw": 18.0, "tail_pitch": -25.0, "hips_up": 0.03 * ref},
            {"t": 1.4, "neck_pitch": -14.0, "head_pitch": -6.0,
             "head_yaw": -18.0, "tail_pitch": -25.0, "hips_up": 0.03 * ref},
            {"t": 1.8}]},
        "pounce": {"loop": False, "keys": [
            {"t": 0.0},
            {"t": 0.3, "hips_up": -0.2 * ref, "body_pitch": 6.0,
             "spine_pitch": 10.0, "neck_pitch": 8.0},
            {"t": 0.5, "hips_up": 0.35 * ref, "body_pitch": -18.0,
             "spine_pitch": -10.0, "neck_pitch": -6.0,
             "front_forward": 0.5 * ref, "front_up": 0.3 * ref,
             "back_up": 0.25 * ref},
            {"t": 0.75, "hips_up": -0.05 * ref, "body_pitch": 4.0,
             "front_forward": 0.35 * ref, "back_forward": -0.2 * ref},
            {"t": 1.1}]},
    }


QUAD_KEY_FIELDS = {
    "hips_left": 0.0, "hips_forward": 0.0, "hips_up": 0.0,
    "body_pitch": 0.0, "spine_pitch": 0.0, "neck_pitch": 0.0,
    "head_pitch": 0.0, "head_yaw": 0.0, "tail_pitch": 0.0, "tail_yaw": 0.0,
    # metres from rest, applied to both feet of that pair
    "front_forward": 0.0, "front_up": 0.0, "back_forward": 0.0, "back_up": 0.0,
}


def _lerp(a, b, u):
    return {k: a.get(k, QUAD_KEY_FIELDS[k]) + (b.get(k, QUAD_KEY_FIELDS[k])
                                              - a.get(k, QUAD_KEY_FIELDS[k])) * u
            for k in QUAD_KEY_FIELDS}


def solve_quad_fields(rig: QuadFrame, f: dict) -> Pose:
    feet = {}
    for l in rig.legs():
        pair = "front" if l in FRONT else "back"
        feet[l] = v_add(rig.paw(l), frame(rig, forward=f[pair + "_forward"],
                                          up=f[pair + "_up"]))
    return quad_stand(rig, hips=frame(rig, f["hips_left"], f["hips_forward"],
                                      f["hips_up"]),
                      body_pitch=f["body_pitch"], spine_pitch=f["spine_pitch"],
                      neck_pitch=f["neck_pitch"], head_pitch=f["head_pitch"],
                      head_yaw=f["head_yaw"], tail_pitch=f["tail_pitch"],
                      tail_yaw=f["tail_yaw"], feet=feet)


def quad_keyed_clip(rig: QuadFrame, keys: list, fps=30, loop=False):
    if not keys:
        raise ValueError("a keyed clip needs at least one key")
    keys = sorted(({"t": 0.0, **k} for k in keys), key=lambda k: k["t"])
    end = keys[-1]["t"] if len(keys) > 1 else 1.0 / fps
    n = max(2, int(round(end * fps)) + 1)
    poses = []
    for i in range(n):
        t = i / float(fps)
        j = 0
        while j + 1 < len(keys) and keys[j + 1]["t"] <= t:
            j += 1
        a = keys[j]
        b = keys[min(j + 1, len(keys) - 1)]
        span = b["t"] - a["t"]
        u = ease_inout(min(1.0, max(0.0, (t - a["t"]) / span))) if span > 1e-9 else 0.0
        poses.append(solve_quad_fields(rig, _lerp(a, b, u)))
    return poses, {"kind": "keyed", "frames": n, "keys": len(keys), "loop": loop}


QUAD_CLIP_KINDS = ("idle", "walk", "trot", "gallop", "sit", "alert", "pounce",
                   "keyed")


def build_quad_clip(rig: QuadFrame, spec: dict, fps=30) -> tuple[list, dict]:
    kind = spec.get("kind") or spec.get("name")
    if kind in QUAD_GAITS:
        poses, notes = quad_gait_cycle(rig, kind, frames=spec.get("frames"),
                                       fps=fps, overrides=spec.get("overrides"))
        notes["loop"] = True
    elif kind == "idle":
        poses, notes = quad_idle_cycle(rig, frames=spec.get("frames", 90),
                                       fps=fps, energy=float(spec.get("energy", 1.0)))
        notes["loop"] = True
    elif kind == "keyed":
        poses, notes = quad_keyed_clip(rig, spec.get("keys") or [], fps=fps,
                                       loop=bool(spec.get("loop")))
    elif kind in quad_presets(rig):
        pre = quad_presets(rig)[kind]
        poses, notes = quad_keyed_clip(rig, pre["keys"], fps=fps, loop=pre["loop"])
        notes["kind"] = kind
    else:
        raise ValueError("unknown quadruped clip kind %r — one of %s"
                         % (kind, ", ".join(QUAD_CLIP_KINDS)))
    notes["name"] = spec.get("name") or kind
    return poses, notes


def bake_quad_clips(rig_dump: dict, clips: list, fps=30) -> dict:
    """The quadruped twin of humanpose.bake_clips: same record shape out."""
    rig = QuadFrame(rig_dump["bones"], up=tuple(rig_dump.get("up") or (0, 0, 1)))
    out = {"rig": rig.summary(), "clips": [], "fps": fps}
    for spec in clips:
        try:
            poses, notes = build_quad_clip(rig, spec, fps=fps)
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
# The canonical quadruped, for tests and for authoring without Blender
# ---------------------------------------------------------------------------

def canonical_quadruped_rig(length=1.0, shoulder=0.55, width=0.16) -> dict:
    """A dog-sized quadruped: faces +Y, left on -X, Z up. Shoulders and
    hips at `shoulder` height, body `length` from hip to shoulder, paws on
    the floor. Elbows sit BACK of the shoulder line and stifles FORWARD of
    the hip line, so a two-bone IK with the right pole reproduces the rest."""
    B = {}
    hx = width * 0.5
    hip_y, sh_y = -length * 0.5, length * 0.5
    B["Root"] = _bone(None, (0, hip_y, 0), (0, hip_y, shoulder), (-1, 0, 0))
    B["Hips"] = _bone("Root", (0, hip_y, shoulder), (0, hip_y + length * 0.3, shoulder * 1.02), (-1, 0, 0))
    B["Spine"] = _bone("Hips", (0, hip_y + length * 0.3, shoulder * 1.02),
                       (0, hip_y + length * 0.65, shoulder * 1.05), (-1, 0, 0))
    B["Chest"] = _bone("Spine", (0, hip_y + length * 0.65, shoulder * 1.05),
                       (0, sh_y, shoulder * 1.05), (-1, 0, 0))
    B["Neck"] = _bone("Chest", (0, sh_y, shoulder * 1.05),
                      (0, sh_y + length * 0.2, shoulder * 1.35), (-1, 0, 0))
    B["Head"] = _bone("Neck", (0, sh_y + length * 0.2, shoulder * 1.35),
                      (0, sh_y + length * 0.42, shoulder * 1.38), (-1, 0, 0))
    B["Tail1"] = _bone("Hips", (0, hip_y, shoulder), (0, hip_y - length * 0.2, shoulder * 0.95), (-1, 0, 0))
    B["Tail2"] = _bone("Tail1", (0, hip_y - length * 0.2, shoulder * 0.95),
                       (0, hip_y - length * 0.4, shoulder * 0.8), (-1, 0, 0))
    paw_z = shoulder * 0.06
    for side, s in (("Left", -1.0), ("Right", 1.0)):
        x = s * hx
        # Front: shoulder -> elbow (back) -> paw
        top = (x, sh_y, shoulder)
        elbow = (x, sh_y - length * 0.08, shoulder * 0.5)
        paw = (x, sh_y, paw_z)
        toe = (x, sh_y + length * 0.08, 0.0)
        B[side + "FrontUpperLeg"] = _bone("Chest", top, elbow, (1, 0, 0))
        B[side + "FrontLowerLeg"] = _bone(side + "FrontUpperLeg", elbow, paw, (1, 0, 0))
        B[side + "FrontFoot"] = _bone(side + "FrontLowerLeg", paw, toe, (1, 0, 0))
        # Back: hip -> stifle (forward) -> paw
        top = (x, hip_y, shoulder)
        knee = (x, hip_y + length * 0.10, shoulder * 0.5)
        paw = (x, hip_y, paw_z)
        toe = (x, hip_y + length * 0.08, 0.0)
        B[side + "BackUpperLeg"] = _bone("Hips", top, knee, (1, 0, 0))
        B[side + "BackLowerLeg"] = _bone(side + "BackUpperLeg", knee, paw, (1, 0, 0))
        B[side + "BackFoot"] = _bone(side + "BackLowerLeg", paw, toe, (1, 0, 0))
    return {"bones": B, "up": (0.0, 0.0, 1.0)}
