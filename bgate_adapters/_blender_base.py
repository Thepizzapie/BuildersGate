"""bpy source for the BASE MESH LIBRARY — the second half of the modelling floor.

NOT imported by Builders Gate at runtime except through ``_blender_kit.KIT``,
which splices ``BASE`` in after the primitive helpers. Everything here runs
inside Blender, inside the agent's own script.

WHY IT EXISTS. The kit gave an agent clean primitives and a worked example. An
example is something an agent may or may not imitate; measured on real runs it
mostly does not, and what comes back is a figure whose every coordinate was
invented from nothing. The vocabulary was box/cylinder/sphere plus mirror and
taper, and the honest ceiling of that vocabulary is a snowman.

This module moves the starting point. ``bg_human()`` returns a correctly
proportioned, closed, unwrapped, weight-ready body plus a named skeleton plus a
dictionary of LANDMARKS, so the agent's job stops being "invent a person" and
becomes "fit clothing, hair and gear onto this person" — which is a job an
agent is actually good at, because the numbers it needs are handed to it.

THREE THINGS THIS FIXES, each of them a measured failure:

  * PROPORTION. Every dimension is derived from one measurement — head height —
    and the head-count is a parameter, so 7.5 heads (adult), 5 (stylised) and 3
    (chibi) are the same body maths and not three different guesses.

  * THE SHARED FRAME. Layers are authored in isolated empty scenes, so a cap is
    modelled at a head height the agent guessed and lands floating or sunk.
    ``bg_marks()`` publishes where the head, the wrists, the waist and the soles
    ARE, and ``bg_fit()`` puts a layer there.

  * SCALE. Nothing in this pipeline declared a unit, and the turnaround renders
    normalise scale away, so two characters from two runs are arbitrarily
    different sizes and both look perfect. ``BG_UNIT`` declares metres,
    ``bg_unit_check()`` is the assertion nobody could previously write.

DEFENSIVE, EXCEPT WHERE LOUD IS THE POINT. Helpers swallow their problems the
way the rest of the kit does. The exceptions are deliberate and match
``bg_bone_chain``: nonsense proportions, an unknown landmark name and a failed
unit assertion all raise, because each one produces an asset that looks built
and is wrong in a way no later check catches.
"""

BASE = r'''
# --- Builders Gate base mesh library (injected) -----------------------------
from mathutils import Matrix


# ---------------------------------------------------------------------------
# THE UNIT CONVENTION. Declared once, here, because nothing else in the
# pipeline ever did.
# ---------------------------------------------------------------------------
BG_UNIT = "metre"
BG_UNITS_PER_METRE = 1.0
BG_HUMAN_HEIGHT = 1.8          # the default adult. glTF is metres; Godot agrees.
BG_UP = (0.0, 0.0, 1.0)
# THE FACING CONVENTION, AND WHY IT IS NOT BLENDER'S. Blender's own front view
# looks along +Y, so a Blender-native base faces -Y and every rig tutorial says
# so. But the glTF exporter rotates -90 about X on the way out — Blender +Y
# becomes glTF -Z — and -Z is what Godot, three.js and every glTF consumer call
# forward. A base authored the Blender way therefore arrives in the engine
# facing the camera it should be walking away from, and the only fixes are a
# compensating rotation hidden somewhere in the export path or a 180 degree
# turn every consumer has to remember. So the base faces +Y HERE and is correct
# EVERYWHERE ELSE, and `turnaround` carries the cost: its "front" is 180.
BG_FORWARD = (0.0, 1.0, 0.0)   # the character FACES +Y in Blender -> -Z in glTF
BG_LEFT = (-1.0, 0.0, 0.0)     # BG_UP x BG_FORWARD: the figure's OWN left
# Which side of X each name lives on. Not decoration: LeftUpperArm is a
# SkeletonProfileHumanoid name an engine retargets on, so a base whose "Left"
# bones sit on its anatomical right mirrors every animation ever bound to it,
# silently. Turning the figure to face +Y turns its left to -X with it.
BG_SIDES = (("L", -1.0), ("R", 1.0))
BG_GROUND = 0.0                # soles sit on z = 0, always

# Head-counts worth naming. Everything between them works too.
BG_HEADS_HEROIC = 8.0
BG_HEADS_ADULT = 7.5
BG_HEADS_STYLISED = 5.0
BG_HEADS_CHIBI = 3.0

# Recognised unit mistakes, as a ratio of what came back to what was expected.
_BG_UNIT_MISTAKES = ((1000.0, "millimetres"), (100.0, "centimetres"),
                     (39.3701, "inches"), (3.28084, "feet"),
                     (0.001, "kilometres"), (0.01, "hundredths"))


def bg_unit_check(thing, expect=BG_HUMAN_HEIGHT, tol=0.5, label="asset"):
    """Is this thing about the size it claims to be? IN METRES.

    THE CHECK NOBODY COULD WRITE BEFORE. `blender_turnaround` frames the camera
    to the subject's own bounding box, so a 180 m character and a 0.018 m one
    come back as identical, perfect-looking renders. Scale is the one defect in
    this pipeline that is completely invisible downstream and completely fatal
    in the engine.

    `thing` is an object, a dims triple, or a plain height. Returns
    ok / height / expect / ratio / unit_guess / scale_to_fix / verdict.
    It does NOT raise — see `bg_unit_assert` for the version that does.
    """
    try:
        if hasattr(thing, "type"):
            height = float(bg_bounds(thing)["dims"][2])
        elif isinstance(thing, (int, float)):
            height = float(thing)
        else:
            height = float(max(thing))
    except Exception:
        height = 0.0
    expect = float(expect) or 1.0
    ratio = (height / expect) if expect else 0.0
    ok = bool(height > 0.0 and (1.0 / (1.0 + tol)) <= ratio <= (1.0 + tol))
    guess = ""
    if not ok and ratio:
        for factor, name in _BG_UNIT_MISTAKES:
            if abs(ratio - factor) <= factor * 0.25:
                guess = name
                break
    fix = (expect / height) if height > 1e-12 else 0.0
    if ok:
        verdict = "%s is %.3f %s — within tolerance of %.3f" % (
            label, height, BG_UNIT, expect)
    elif height <= 0.0:
        verdict = "%s has no height at all — nothing measurable in the scene" % label
    else:
        verdict = ("%s is %.4f %s but should be about %.3f (%.4gx out%s). "
                   "Scale by %.6g." % (label, height, BG_UNIT, expect, ratio,
                                       " — this looks like " + guess if guess else "",
                                       fix))
    return {"ok": ok, "height": height, "expect": expect, "ratio": ratio,
            "unit": BG_UNIT, "unit_guess": guess, "scale_to_fix": fix,
            "verdict": verdict}


def bg_unit_assert(thing, expect=BG_HUMAN_HEIGHT, tol=0.5, label="asset"):
    """bg_unit_check, but LOUD. Raises on a wrong-scale asset.

    Deliberately one of the few things in the kit that stops the run: an asset
    at the wrong scale assembles, exports and renders perfectly, and is only
    ever discovered by a human dropping it into a level.
    """
    report = bg_unit_check(thing, expect=expect, tol=tol, label=label)
    if not report["ok"]:
        raise ValueError("bg_unit_assert: " + report["verdict"])
    return report


def bg_rescale(obj, height=BG_HUMAN_HEIGHT, axis=2, others=()):
    """Scale obj uniformly so its size on `axis` is `height`, and drop it back on
    the ground. `others` (an armature, usually) is scaled by the same factor
    about the same origin, so a rig does not come apart from its body."""
    if obj is None:
        return obj
    dims = bg_bounds(obj)["dims"]
    if dims[axis] <= 1e-9:
        return obj
    factor = float(height) / dims[axis]
    for target in [obj] + [o for o in others if o is not None]:
        target.scale = tuple(s * factor for s in target.scale)
        target.location = tuple(v * factor for v in target.location)
    bpy.context.view_layer.update()
    low = bg_bounds(obj)["min"][2]
    for target in [obj] + [o for o in others if o is not None]:
        target.location = (target.location[0], target.location[1],
                           target.location[2] - low + BG_GROUND)
    bpy.context.view_layer.update()
    return obj


# ---------------------------------------------------------------------------
# Lofted shells. Every part of every base mesh is one of these.
# ---------------------------------------------------------------------------
# A shell is a stack of rings perpendicular to one axis, bridged with quads and
# closed at both ends. CLOSED AND MANIFOLD BY CONSTRUCTION is the whole reason
# this exists rather than more primitives: bone-heat weighting refuses doubled,
# loose or degenerate geometry, and a booleaned-together pile of cylinders is
# exactly that. Normals are recalculated outward on the way out, so bg_stats
# reports 0 flipped on everything this module builds.

_BG_RING_AXES = {0: (1, 2), 1: (0, 2), 2: (0, 1)}


def _bg_ring_verts(bm, centre, rx, ry, segs, axis):
    """One ring of `segs` verts, or a single pole vertex if it has no radius."""
    a, b = _BG_RING_AXES[axis]
    if rx <= 1e-7 and ry <= 1e-7:
        return [bm.verts.new((centre[0], centre[1], centre[2]))]
    ring = []
    for i in range(segs):
        angle = 2.0 * math.pi * i / segs
        co = [centre[0], centre[1], centre[2]]
        co[a] += rx * math.cos(angle)
        co[b] += ry * math.sin(angle)
        ring.append(bm.verts.new(co))
    return ring


def _bg_bridge(bm, lower, upper):
    if len(lower) == 1 and len(upper) == 1:
        return
    if len(lower) == 1:
        for i in range(len(upper)):
            bm.faces.new((lower[0], upper[i], upper[(i + 1) % len(upper)]))
    elif len(upper) == 1:
        for i in range(len(lower)):
            bm.faces.new((lower[i], lower[(i + 1) % len(lower)], upper[0]))
    else:
        count = min(len(lower), len(upper))
        for i in range(count):
            j = (i + 1) % count
            bm.faces.new((lower[i], lower[j], upper[j], upper[i]))


def _bg_cap(bm, ring, centre, axis):
    """Close an open end with a triangle fan to a new centre vertex.

    A FAN, NOT AN NGON. An n-gon cap survives every check here and then
    triangulates unpredictably on glTF export, which is where a flat sole picks
    up a crease. Tris cost nothing at this vertex count.
    """
    if len(ring) < 3:
        return
    hub = bm.verts.new((centre[0], centre[1], centre[2]))
    for i in range(len(ring)):
        bm.faces.new((ring[i], ring[(i + 1) % len(ring)], hub))


def bg_shell(name, rings, segs=12, axis=2, cap_start=True, cap_end=True):
    """A closed lofted shell. `rings` is [(centre_xyz, rx, ry), ...] ordered
    along `axis`; a ring with both radii ~0 becomes a pole vertex.

    For axis=2 (Z) rx runs along X and ry along Y; for axis=0 (X) rx runs along
    Y and ry along Z; for axis=1 (Y) rx runs along X and ry along Z.
    """
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    rows = [r for r in (rings or [])]
    if len(rows) < 2:
        return obj
    bm = bmesh.new()
    try:
        loops = [_bg_ring_verts(bm, c, float(rx), float(ry), int(segs), axis)
                 for c, rx, ry in rows]
        for lower, upper in zip(loops, loops[1:]):
            _bg_bridge(bm, lower, upper)
        if cap_start and len(loops[0]) > 1:
            _bg_cap(bm, list(reversed(loops[0])), rows[0][0], axis)
        if cap_end and len(loops[-1]) > 1:
            _bg_cap(bm, loops[-1], rows[-1][0], axis)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return obj


def _bg_lerp(a, b, t):
    return a + (b - a) * t


def _bg_profile(points, t):
    """Interpolate a [(t, value), ...] profile, clamped at both ends."""
    if not points:
        return 0.0
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            span = (t1 - t0) or 1.0
            return _bg_lerp(v0, v1, (t - t0) / span)
    return points[-1][1]


def bg_ellipse_girth(rx, ry):
    """Ramanujan's ellipse perimeter — the number a belt or a hatband needs."""
    a, b = max(rx, ry), min(rx, ry)
    if a <= 0:
        return 0.0
    h = ((a - b) ** 2) / ((a + b) ** 2) if (a + b) else 0.0
    return math.pi * (a + b) * (1.0 + (3.0 * h) / (10.0 + math.sqrt(4.0 - 3.0 * h)))


# ---------------------------------------------------------------------------
# THE PROPORTION FRAME. Pure arithmetic — no bpy, no mesh, callable before
# anything is built and callable by a layer script that is not building a body
# at all but needs to know where the body's wrists are.
# ---------------------------------------------------------------------------

def bg_proportions(height=BG_HUMAN_HEIGHT, heads=BG_HEADS_ADULT, build=1.0,
                   limbs=1.0, shoulders=1.0):
    """Every measurement of a humanoid, derived from ONE number.

    height   total, in metres, crown to sole
    heads    how many head-heights tall: 7.5 adult, 8 heroic, 5 stylised, 3 chibi
    build    heft — scales every width and limb radius, not any length
    limbs    leg and arm length as a ratio of the default; the torso takes up
             the slack, so `height` and `heads` stay exactly what was asked for
    shoulders  extra width on the shoulder line only

    THE HEAD IS THE UNIT AND THE BODY TAKES WHAT IS LEFT. head = height/heads,
    and every landmark below the chin is a fraction of the chin height. That is
    why chibi works: a 3-head figure is not a shrunken adult, it is an adult
    with two-thirds of its body replaced by head, which is exactly what the
    style is.

    RAISES on values that cannot make a body. A silently clamped 0-head figure
    is worse than a stopped run.
    """
    height = float(height)
    heads = float(heads)
    if height <= 0.0:
        raise ValueError("bg_proportions: height must be positive, got %r" % height)
    if heads <= 1.05:
        raise ValueError(
            "bg_proportions: heads=%r leaves no body below the chin (the head "
            "alone would be %.0f%% of the figure). Use 3 for chibi, 5 for "
            "stylised, 7.5 for an adult." % (heads, 100.0 / max(heads, 1e-6)))
    if heads > 12.0:
        raise ValueError(
            "bg_proportions: heads=%r is not a proportion, it is a stick — "
            "8 is already heroic." % heads)
    build = max(float(build), 0.15)
    limbs = min(max(float(limbs), 0.4), 1.6)
    shoulders = min(max(float(shoulders), 0.5), 1.8)

    head = height / heads
    body = height - head                 # chin height: the reference for ALL
                                         # lengths below the neck
    crown = height
    chin = body

    # Heights, as fractions of the chin height. Classic figure-drawing canon
    # converted out of head-counts so it survives restyling.
    shoulder = 0.955 * body
    upperchest = 0.910 * body
    chest = 0.862 * body
    waist = 0.738 * body
    ankle0 = 0.045 * body
    crotch0 = 0.577 * body               # half the total height on an adult
    # `limbs` stretches the legs about the ankle and lets the torso absorb it.
    crotch = ankle0 + (crotch0 - ankle0) * limbs
    crotch = min(crotch, waist - 0.06 * body)
    ankle = ankle0
    knee = ankle + (crotch - ankle) * 0.455

    # Half-widths and radii, all times `build`.
    w = build
    shoulder_hw = 0.133 * body * w * shoulders
    chest_hw, chest_hd = 0.105 * body * w, 0.070 * body * w
    waist_hw, waist_hd = 0.088 * body * w, 0.058 * body * w
    hip_hw, hip_hd = 0.105 * body * w, 0.072 * body * w
    neck_r = 0.0455 * body * w

    shoulder_x = 0.095 * body * shoulders
    hip_x = 0.048 * body

    upper_arm_r = 0.0345 * body * w
    elbow_r = 0.0280 * body * w
    wrist_r = 0.0210 * body * w
    thigh_r = 0.0620 * body * w
    knee_r = 0.0460 * body * w
    ankle_r = 0.0300 * body * w

    # Arm lengths chosen so fingertip-to-fingertip equals the total height —
    # the one proportion check a person can do on a turnaround without a ruler,
    # and the reason these are not four independently plausible numbers.
    upper_arm_l = 0.190 * body * limbs
    lower_arm_l = 0.172 * body * limbs
    hand_l = 0.120 * body
    elbow_x = shoulder_x + upper_arm_l
    wrist_x = elbow_x + lower_arm_l
    fingertip_x = wrist_x + hand_l

    foot_l = 0.165 * body
    foot_hw = 0.031 * body * w
    toe_y = foot_l * 0.72                # the character faces +Y
    heel_y = -foot_l * 0.28

    head_hw = 0.330 * head * w ** 0.35
    head_hd = 0.400 * head * w ** 0.35
    head_centre_z = chin + head * 0.5

    return {
        "unit": BG_UNIT, "height": height, "heads": heads, "head": head,
        "body": body, "build": build, "limbs": limbs, "shoulders": shoulders,
        "crown": crown, "chin": chin, "shoulder_z": shoulder,
        "upperchest_z": upperchest, "chest_z": chest, "waist_z": waist,
        "crotch_z": crotch, "knee_z": knee, "ankle_z": ankle, "ground": BG_GROUND,
        "shoulder_hw": shoulder_hw, "chest_hw": chest_hw, "chest_hd": chest_hd,
        "waist_hw": waist_hw, "waist_hd": waist_hd, "hip_hw": hip_hw,
        "hip_hd": hip_hd, "neck_r": neck_r,
        "shoulder_x": shoulder_x, "hip_x": hip_x,
        "upper_arm_r": upper_arm_r, "elbow_r": elbow_r, "wrist_r": wrist_r,
        "thigh_r": thigh_r, "knee_r": knee_r, "ankle_r": ankle_r,
        "elbow_x": elbow_x, "wrist_x": wrist_x, "fingertip_x": fingertip_x,
        "hand_l": hand_l, "foot_l": foot_l, "foot_hw": foot_hw,
        "toe_y": toe_y, "heel_y": heel_y,
        "head_hw": head_hw, "head_hd": head_hd, "head_centre_z": head_centre_z,
    }


# ---------------------------------------------------------------------------
# LANDMARKS. Where a layer goes, published as numbers.
# ---------------------------------------------------------------------------

def _bg_mark(pos, radius=0.0, size=None, direction=(0.0, 0.0, 1.0), note=""):
    rx = radius if isinstance(radius, (int, float)) else radius[0]
    ry = radius if isinstance(radius, (int, float)) else radius[1]
    return {"pos": tuple(float(v) for v in pos),
            "radius": float(max(rx, ry)),
            "girth": bg_ellipse_girth(float(rx), float(ry)),
            "size": tuple(float(v) for v in (size or (rx * 2, ry * 2, rx * 2))),
            "dir": tuple(float(v) for v in direction),
            "note": note}


def bg_human_marks(P):
    """The landmark table for a set of proportions. Names are the contract.

    LAYERS ARE AUTHORED IN ISOLATED EMPTY SCENES. Nothing in the pipeline puts
    two of them side by side until they are already combined, so "is the cap at
    head height" has never been a question anyone could answer while it was
    still cheap to fix. These are the answers: position, radius and the girth
    around it, in metres, in the same world the body is built in.
    """
    marks = {
        "ground": _bg_mark((0, 0, BG_GROUND), P["foot_hw"] * 2,
                           note="the plane the soles stand on"),
        "crotch": _bg_mark((0, 0, P["crotch_z"]), (P["hip_hw"], P["hip_hd"])),
        "hips": _bg_mark((0, 0, P["crotch_z"] + 0.04 * P["body"]),
                         (P["hip_hw"], P["hip_hd"]), note="belt line"),
        "waist": _bg_mark((0, 0, P["waist_z"]), (P["waist_hw"], P["waist_hd"]),
                          note="narrowest of the torso"),
        "chest": _bg_mark((0, 0, P["chest_z"]), (P["chest_hw"], P["chest_hd"])),
        "shoulder_line": _bg_mark((0, 0, P["shoulder_z"]),
                                  (P["shoulder_hw"], P["chest_hd"]),
                                  note="collar height, full shoulder span"),
        "neck": _bg_mark((0, 0, P["shoulder_z"] + 0.35 * (P["chin"] - P["shoulder_z"])),
                         P["neck_r"], note="collar sits here"),
        "chin": _bg_mark((0, 0, P["chin"]), P["head_hw"] * 0.5),
        # The cranium overhangs BEHIND the spine, which is -Y now the figure
        # faces +Y. This offset and the two face marks below are the whole
        # reason a head landmark is not just "on the axis at head height".
        "head": _bg_mark((0, -P["head_hd"] * 0.06, P["head_centre_z"]),
                         (P["head_hw"], P["head_hd"]),
                         size=(P["head_hw"] * 2, P["head_hd"] * 2, P["head"]),
                         note="head centre; girth is the hatband"),
        "head_top": _bg_mark((0, -P["head_hd"] * 0.06, P["crown"]),
                             (P["head_hw"] * 0.55, P["head_hd"] * 0.55),
                             note="crown — a hat SITS here, it is not centred here"),
        "eye_line": _bg_mark((0, P["head_hd"] * 0.9,
                              P["chin"] + P["head"] * 0.52),
                             P["head_hw"] * 0.8,
                             direction=BG_FORWARD, note="brow/eye height, front of face"),
        "face": _bg_mark((0, P["head_hd"] * 0.95, P["chin"] + P["head"] * 0.45),
                         P["head_hw"] * 0.8, direction=BG_FORWARD),
    }
    for tag, sign in BG_SIDES:
        marks["shoulder." + tag] = _bg_mark(
            (sign * P["shoulder_x"], 0, P["shoulder_z"]), P["upper_arm_r"] * 1.3,
            direction=(sign, 0, 0), note="shoulder joint")
        marks["elbow." + tag] = _bg_mark(
            (sign * P["elbow_x"], 0, P["shoulder_z"]), P["elbow_r"],
            direction=(sign, 0, 0))
        marks["wrist." + tag] = _bg_mark(
            (sign * P["wrist_x"], 0, P["shoulder_z"]), P["wrist_r"],
            direction=(sign, 0, 0), note="cuff/glove opening")
        marks["hand." + tag] = _bg_mark(
            (sign * (P["wrist_x"] + P["hand_l"] * 0.5), 0, P["shoulder_z"]),
            (P["wrist_r"] * 1.6, P["wrist_r"] * 0.75),
            size=(P["hand_l"], P["wrist_r"] * 3.2, P["wrist_r"] * 1.5),
            direction=(sign, 0, 0), note="grip point — a held prop goes here")
        marks["hip." + tag] = _bg_mark(
            (sign * P["hip_x"], 0, P["crotch_z"]), P["thigh_r"],
            direction=(0, 0, -1))
        marks["knee." + tag] = _bg_mark(
            (sign * P["hip_x"], 0, P["knee_z"]), P["knee_r"], direction=(0, 0, -1))
        marks["ankle." + tag] = _bg_mark(
            (sign * P["hip_x"], 0, P["ankle_z"]), P["ankle_r"], direction=(0, 0, -1))
        marks["foot." + tag] = _bg_mark(
            (sign * P["hip_x"], (P["toe_y"] + P["heel_y"]) * 0.5, P["ankle_z"] * 0.5),
            (P["foot_hw"], P["ankle_z"] * 0.5),
            size=(P["foot_hw"] * 2, P["foot_l"], P["ankle_z"] * 1.7),
            direction=BG_FORWARD, note="the whole foot — a boot's box")
        marks["sole." + tag] = _bg_mark(
            (sign * P["hip_x"], (P["toe_y"] + P["heel_y"]) * 0.5, BG_GROUND),
            (P["foot_hw"], P["foot_l"] * 0.5), direction=(0, 0, -1))
    return marks


def bg_mark(base, name):
    """One landmark, by name. RAISES on a name that is not there.

    Deliberately loud: a mis-typed landmark returning an empty dict puts the
    layer at the world origin, between the character's feet, which reads on a
    turnaround as "the hat did not generate" and costs another run to find.
    """
    marks = base.get("marks", base) if isinstance(base, dict) else {}
    if name in marks:
        return marks[name]
    raise KeyError(
        "bg_mark: no landmark %r. Known: %s" % (name, ", ".join(sorted(marks))))


def bg_fit(obj, mark, mode="around", clearance=0.0, scale=True, axis=2):
    """Put a layer ONTO a landmark instead of beside it.

    mode="at"      centre the object on the landmark (a belt on the waist)
    mode="on"      sit the object's underside on the landmark (a hat on a crown)
    mode="around"  wrap it: scale to the landmark's girth, then centre on it
    mode="in"      shrink it INSIDE the landmark's radius (an eye in a socket)

    `scale=False` moves without resizing — use it when the layer was already
    modelled to size and only needs placing.

    Returns the fit report: the object's bounds afterwards, the scale factor
    applied, and the landmark it was fitted to. Move-only, never destructive:
    the transform is left unapplied so a caller can undo it by hand.
    """
    if obj is None or not isinstance(mark, dict) or "pos" not in mark:
        return {"ok": False, "verdict": "nothing to fit"}
    bpy.context.view_layer.update()
    box = bg_bounds(obj)
    if not any(box["dims"]):
        return {"ok": False, "verdict": "the object has no size — already joined away?"}
    factor = 1.0
    if scale and mode in ("around", "in"):
        want = (mark["radius"] + clearance) * 2.0
        if mode == "in":
            want = max(mark["radius"] - clearance, 1e-4) * 2.0
        wide = max(box["dims"][0], box["dims"][1])
        if wide > 1e-9:
            factor = want / wide
            obj.scale = tuple(s * factor for s in obj.scale)
            bpy.context.view_layer.update()
            box = bg_bounds(obj)
    target = list(mark["pos"])
    centre = list(box["centre"])
    if mode == "on":
        # The object's BOTTOM meets the landmark, not its middle. A hat centred
        # on the crown is a hat halfway through the skull.
        centre[axis] = box["min"][axis] - clearance
    delta = [target[i] - centre[i] for i in range(3)]
    obj.location = tuple(obj.location[i] + delta[i] for i in range(3))
    bpy.context.view_layer.update()
    after = bg_bounds(obj)
    return {"ok": True, "mode": mode, "scale": factor, "at": tuple(target),
            "dims": after["dims"], "min": after["min"], "max": after["max"],
            "verdict": "fitted %s at (%.3f, %.3f, %.3f), scaled %.3gx"
                       % (mode, target[0], target[1], target[2], factor)}


# ---------------------------------------------------------------------------
# THE CANONICAL SKELETON. The bone names ARE the contract.
# ---------------------------------------------------------------------------
# combine(bind='bone:Head') matches on a string. An armature of Bone.001 makes
# every rigid layer a guess and no humanoid retarget can read it. These are
# Godot's SkeletonProfileHumanoid names, so a Godot import maps them with no
# hand-written bone table; "blender" gives the Rigify/.L-.R spelling instead.

BG_BONE_NAMES = {
    "godot": {
        "root": "Root", "hips": "Hips", "spine": "Spine", "chest": "Chest",
        "upperchest": "UpperChest", "neck": "Neck", "head": "Head",
        "shoulder.L": "LeftShoulder", "upperarm.L": "LeftUpperArm",
        "lowerarm.L": "LeftLowerArm", "hand.L": "LeftHand",
        "shoulder.R": "RightShoulder", "upperarm.R": "RightUpperArm",
        "lowerarm.R": "RightLowerArm", "hand.R": "RightHand",
        "upperleg.L": "LeftUpperLeg", "lowerleg.L": "LeftLowerLeg",
        "foot.L": "LeftFoot", "toes.L": "LeftToes",
        "upperleg.R": "RightUpperLeg", "lowerleg.R": "RightLowerLeg",
        "foot.R": "RightFoot", "toes.R": "RightToes",
    },
    "blender": {
        "root": "Root", "hips": "Hips", "spine": "Spine", "chest": "Chest",
        "upperchest": "UpperChest", "neck": "Neck", "head": "Head",
        "shoulder.L": "Shoulder.L", "upperarm.L": "UpperArm.L",
        "lowerarm.L": "LowerArm.L", "hand.L": "Hand.L",
        "shoulder.R": "Shoulder.R", "upperarm.R": "UpperArm.R",
        "lowerarm.R": "LowerArm.R", "hand.R": "Hand.R",
        "upperleg.L": "UpperLeg.L", "lowerleg.L": "LowerLeg.L",
        "foot.L": "Foot.L", "toes.L": "Toes.L",
        "upperleg.R": "UpperLeg.R", "lowerleg.R": "LowerLeg.R",
        "foot.R": "Foot.R", "toes.R": "Toes.R",
    },
}
BG_BONE_CONVENTION = "godot"


def bg_bone(base, key, convention=None):
    """The real bone name for a role. Use it, do not spell bones by hand.

        combine(parts=[{"path": cap, "bind": "bone:" + bg_bone(base, "head")}])

    RAISES on an unknown role, for the same reason bg_mark does.
    """
    if convention is None:
        convention = (base.get("convention") if isinstance(base, dict) else None) \
            or BG_BONE_CONVENTION
    table = BG_BONE_NAMES.get(convention)
    if table is None:
        raise KeyError("bg_bone: no bone convention %r. Known: %s"
                       % (convention, ", ".join(sorted(BG_BONE_NAMES))))
    if key in table:
        return table[key]
    if key in table.values():
        return key
    raise KeyError("bg_bone: no bone role %r in the %r convention. Known: %s"
                   % (key, convention, ", ".join(sorted(table))))


def bg_roll(head, tail, up=BG_FORWARD):
    """The roll, IN DEGREES, that points a bone's local Z at `up`.

    ROLL IS THE DIFFERENCE BETWEEN A RIG AND A PILE OF BONES. With every roll
    left at 0 there is no shared twist axis: each elbow bends about whatever
    axis fell out of its head/tail direction, and a Godot or Mixamo humanoid
    retarget produces the twisted-forearm look that reads as a broken
    animation. The rule this module uses everywhere: local Z faces the
    character's FRONT, so every hinge turns the same way.

    Falls back to world up when the bone runs along `up` (a toe bone does),
    because there is no roll that can align Z to the bone's own direction.
    """
    try:
        y = (Vector(tail) - Vector(head))
        if y.length < 1e-9:
            return 0.0
        y.normalize()
        reference = Vector(up)
        if reference.length < 1e-9 or abs(y.dot(reference.normalized())) > 0.985:
            reference = Vector(BG_UP)
            if abs(y.dot(reference.normalized())) > 0.985:
                # Last resort: a SIDE vector. It used to be (0, 1, 0), which is
                # BG_FORWARD itself now the figure faces +Y — a fallback that
                # can land back on the reference it was called in to replace.
                reference = Vector((1.0, 0.0, 0.0))
        z = reference - y * reference.dot(y)
        if z.length < 1e-9:
            return 0.0
        z.normalize()
        x = y.cross(z)
        matrix = Matrix(((x.x, y.x, z.x), (x.y, y.y, z.y), (x.z, y.z, z.z)))
        _axis, roll = bpy.types.Bone.AxisRollFromMatrix(matrix)
        return math.degrees(roll)
    except Exception:
        return 0.0


BG_A_POSE_DROP = 0.42          # radians the arms swing down for pose="a"


def _bg_swing(point, pivot, angle):
    """Rotate a point about a pivot in the XZ plane — the arm swing.

    ONE function, used by both the mesh and the bone list. They used to be a
    rotation and a shear written separately, which is the classic way a bind
    pose ends up not matching the geometry it binds: the arms look posed, the
    rest pose is somewhere else, and every animation starts from a lie.
    """
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    dx, dz = point[0] - pivot[0], point[2] - pivot[2]
    return (pivot[0] + dx * cos_a - dz * sin_a, point[1],
            pivot[2] + dx * sin_a + dz * cos_a)


def bg_human_chain(P, convention=BG_BONE_CONVENTION, pose="t"):
    """The bone list for a set of proportions — ready for bg_bone_chain.

    Returned rather than built, so an agent can print it, edit one bone, or
    hand it to bg_bone_chain itself. Rolls are computed, not guessed.
    """
    name = lambda key: bg_bone({}, key, convention)
    hips_z = P["crotch_z"] + 0.045 * P["body"]
    neck_z = P["shoulder_z"] + 0.30 * (P["chin"] - P["shoulder_z"])
    rows = [
        (name("root"), (0, 0, 0), (0, 0, 0.1 * P["body"]), None),
        (name("hips"), (0, 0, hips_z), (0, 0, P["waist_z"]), name("root")),
        (name("spine"), (0, 0, P["waist_z"]), (0, 0, P["chest_z"]), name("hips")),
        (name("chest"), (0, 0, P["chest_z"]), (0, 0, P["upperchest_z"]), name("spine")),
        (name("upperchest"), (0, 0, P["upperchest_z"]), (0, 0, P["shoulder_z"]),
         name("chest")),
        (name("neck"), (0, 0, P["shoulder_z"]), (0, 0, neck_z), name("upperchest")),
        (name("head"), (0, 0, neck_z), (0, 0, P["crown"] - P["head"] * 0.06),
         name("neck")),
    ]
    drop = 0.0 if pose == "t" else BG_A_POSE_DROP
    for tag, sign in BG_SIDES:
        sz = P["shoulder_z"]
        pivot = (sign * P["shoulder_x"], 0.0, sz)
        swing = lambda x: _bg_swing((sign * x, 0.0, sz), pivot, -drop * sign)
        elbow, wrist, tip = (swing(P["elbow_x"]), swing(P["wrist_x"]),
                             swing(P["fingertip_x"]))
        rows += [
            (name("shoulder." + tag), (sign * P["neck_r"] * 0.8, 0, sz - 0.01 * P["body"]),
             pivot, name("upperchest")),
            (name("upperarm." + tag), pivot, elbow, name("shoulder." + tag)),
            (name("lowerarm." + tag), elbow, wrist, name("upperarm." + tag)),
            (name("hand." + tag), wrist, tip, name("lowerarm." + tag)),
            (name("upperleg." + tag), (sign * P["hip_x"], 0, P["crotch_z"]),
             (sign * P["hip_x"], 0, P["knee_z"]), name("hips")),
            (name("lowerleg." + tag), (sign * P["hip_x"], 0, P["knee_z"]),
             (sign * P["hip_x"], 0, P["ankle_z"]), name("upperleg." + tag)),
            (name("foot." + tag), (sign * P["hip_x"], 0, P["ankle_z"]),
             (sign * P["hip_x"], P["toe_y"] * 0.62, P["ankle_z"] * 0.42),
             name("lowerleg." + tag)),
            (name("toes." + tag),
             (sign * P["hip_x"], P["toe_y"] * 0.62, P["ankle_z"] * 0.42),
             (sign * P["hip_x"], P["toe_y"] * 0.95, P["ankle_z"] * 0.30),
             name("foot." + tag)),
        ]
    feet = (name("foot.L"), name("foot.R"), name("toes.L"), name("toes.R"))
    chain = []
    for bone_name, head, tail, parent in rows:
        up = BG_UP if bone_name in feet else BG_FORWARD
        chain.append((bone_name, head, tail, parent, bg_roll(head, tail, up)))
    return chain


def bg_human_skeleton(P, name="Skeleton", convention=BG_BONE_CONVENTION, pose="t"):
    """Build the canonical armature. Root does not deform — it is the transform
    handle an engine parents to, and a deforming root drags the whole mesh."""
    rig = bg_bone_chain(name, bg_human_chain(P, convention=convention, pose=pose))
    root = bg_bone({}, "root", convention)
    if root in rig.data.bones:
        rig.data.bones[root].use_deform = False
    return rig


# ---------------------------------------------------------------------------
# THE BASE MESHES
# ---------------------------------------------------------------------------

_BG_HEAD_PROFILE = ((0.00, 0.00), (0.07, 0.44), (0.20, 0.70), (0.36, 0.88),
                    (0.55, 1.00), (0.72, 0.98), (0.86, 0.83), (0.95, 0.52),
                    (1.00, 0.00))


def _bg_head_shell(P, segs, rings, name="Head"):
    top = P["crown"]
    bottom = P["chin"] - P["head"] * 0.06
    span = top - bottom
    y0 = -P["head_hd"] * 0.06          # the cranium sits BACK of the spine (-Y)
    rows = []
    for i in range(rings + 1):
        t = i / float(rings)
        factor = _bg_profile(_BG_HEAD_PROFILE, t)
        # The jaw sits forward of the cranium; nudge the lower rings toward the
        # face — +Y — so the profile is a head and not an egg.
        y = y0 + P["head_hd"] * 0.10 * max(0.0, 0.45 - t)
        rows.append(((0.0, y, bottom + span * t),
                     P["head_hw"] * factor, P["head_hd"] * factor))
    return bg_shell(name, rows, segs=segs, axis=2)


def _bg_torso_shell(P, segs, name="Torso"):
    hip_z, waist_z = P["crotch_z"], P["waist_z"]
    chest_z, upper_z = P["chest_z"], P["upperchest_z"]
    sh_z = P["shoulder_z"]
    top_hw = P["shoulder_x"] + P["upper_arm_r"] * 0.85
    rows = [
        ((0, 0, hip_z - 0.045 * P["body"]), P["hip_hw"] * 0.74, P["hip_hd"] * 0.80),
        ((0, 0, hip_z + 0.010 * P["body"]), P["hip_hw"] * 0.99, P["hip_hd"] * 0.99),
        ((0, 0, hip_z + 0.045 * P["body"]), P["hip_hw"], P["hip_hd"]),
        ((0, 0, (hip_z + waist_z) * 0.5),
         (P["hip_hw"] + P["waist_hw"]) * 0.5, (P["hip_hd"] + P["waist_hd"]) * 0.5),
        ((0, 0, waist_z), P["waist_hw"], P["waist_hd"]),
        ((0, 0, (waist_z + chest_z) * 0.5),
         (P["waist_hw"] + P["chest_hw"]) * 0.5, (P["waist_hd"] + P["chest_hd"]) * 0.5),
        ((0, 0, chest_z), P["chest_hw"], P["chest_hd"]),
        ((0, 0, upper_z), P["chest_hw"] * 1.02, P["chest_hd"] * 0.99),
        ((0, 0, sh_z), top_hw, P["chest_hd"] * 0.88),
        ((0, 0, sh_z + 0.028 * P["body"]), top_hw * 0.62, P["chest_hd"] * 0.66),
    ]
    return bg_shell(name, rows, segs=segs, axis=2)


def _bg_neck_shell(P, segs, name="Neck"):
    low = P["shoulder_z"] - 0.02 * P["body"]
    high = P["chin"] + P["head"] * 0.22
    rows = [
        ((0, 0, low), P["neck_r"] * 1.22, P["neck_r"] * 1.28),
        ((0, 0, low + (high - low) * 0.35), P["neck_r"], P["neck_r"] * 1.06),
        ((0, 0, high), P["neck_r"] * 0.92, P["neck_r"] * 0.98),
    ]
    return bg_shell(name, rows, segs=segs, axis=2)


def _bg_arm_shell(P, sign, segs, name="Arm"):
    sz = P["shoulder_z"]
    sx, ex, wx = P["shoulder_x"], P["elbow_x"], P["wrist_x"]
    ua, el, wr = P["upper_arm_r"], P["elbow_r"], P["wrist_r"]
    hand_end = wx + P["hand_l"]
    rows = [
        (sx - 0.045 * P["body"], ua * 1.05, ua * 1.15),
        (sx, ua * 1.30, ua * 1.30),
        (sx + (ex - sx) * 0.45, ua, ua),
        (ex, el, el),
        (ex + (wx - ex) * 0.45, el * 0.88, el * 0.88),
        (wx, wr, wr * 1.05),
        (wx + P["hand_l"] * 0.18, wr * 1.55, wr * 0.80),
        (wx + P["hand_l"] * 0.72, wr * 1.62, wr * 0.72),
        (hand_end, wr * 0.85, wr * 0.42),
    ]
    if sign < 0:
        rows = [(-x, rx, ry) for x, rx, ry in rows][::-1]
    return bg_shell(name, [((x, 0.0, sz), rx, ry) for x, rx, ry in rows],
                    segs=segs, axis=0)


def _bg_leg_shell(P, sign, segs, name="Leg"):
    x = sign * P["hip_x"]
    ankle_z, knee_z, crotch_z = P["ankle_z"], P["knee_z"], P["crotch_z"]
    rows = [
        ((x, 0, ankle_z - 0.012 * P["body"]), P["ankle_r"] * 0.88, P["ankle_r"] * 0.88),
        ((x, 0, ankle_z), P["ankle_r"], P["ankle_r"] * 1.10),
        # The calf bulges BEHIND the shin, which is -Y now the figure faces +Y.
        ((x, -0.012 * P["body"], ankle_z + (knee_z - ankle_z) * 0.42),
         P["knee_r"] * 1.14, P["knee_r"] * 1.22),
        ((x, 0, knee_z), P["knee_r"], P["knee_r"]),
        ((x, 0, knee_z + (crotch_z - knee_z) * 0.40), P["thigh_r"] * 0.93,
         P["thigh_r"] * 0.93),
        ((x, 0, crotch_z + 0.030 * P["body"]), P["thigh_r"], P["thigh_r"]),
        ((x, 0, crotch_z + 0.085 * P["body"]), P["thigh_r"] * 0.80,
         P["thigh_r"] * 0.80),
    ]
    return bg_shell(name, rows, segs=segs, axis=2)


def _bg_foot_shell(P, sign, segs, name="Foot"):
    x = sign * P["hip_x"]
    az, fw, hw = P["ankle_z"], P["foot_hw"], P["foot_hw"]
    toe, heel = P["toe_y"], P["heel_y"]
    rows = [
        ((x, toe, az * 0.30), hw * 0.70, az * 0.30),
        ((x, toe * 0.78, az * 0.44), hw * 1.00, az * 0.44),
        ((x, toe * 0.32, az * 0.62), hw * 0.98, az * 0.62),
        ((x, 0.0, az * 0.85), hw * 0.88, az * 0.85),
        ((x, heel * 0.62, az * 0.80), hw * 0.76, az * 0.80),
        ((x, heel, az * 0.58), hw * 0.52, az * 0.58),
    ]
    return bg_shell(name, rows, segs=segs, axis=1)


def bg_human(height=BG_HUMAN_HEIGHT, heads=BG_HEADS_ADULT, build=1.0, limbs=1.0,
             shoulders=1.0, detail=1, name="Body", rig=True, pose="t",
             convention=BG_BONE_CONVENTION, colour=(0.62, 0.47, 0.38),
             material="skin", finish=True, smooth=True):
    """A correctly proportioned, closed, unwrapped, weight-ready humanoid.

    THIS IS THE STARTING POINT, NOT THE ANSWER. What comes back is a body — a
    nude, sexless, featureless one, at the right size, standing on the ground
    plane, with a skeleton whose bones a retarget can read and a table saying
    where its wrists and crown are. The character is what an agent adds ONTO
    it: hair, clothing, gear, face, silhouette. Model those as their own layers
    and place them with bg_fit(obj, bg_mark(base, "head_top"), "on").

    Returns {"obj", "rig", "marks", "props", "convention", "parts"}.
    `parts` is empty after the join — bg_join consumes what it joins — and is
    kept only so a caller can see what went in.

    detail  0 cheap (about 400 verts), 1 default (about 900), 2 dense
    pose    "t" or "a" — the arms, and the rig, move together
    """
    P = bg_proportions(height=height, heads=heads, build=build, limbs=limbs,
                       shoulders=shoulders)
    detail = int(max(0, min(int(detail), 3)))
    body_segs = (10, 16, 22, 28)[detail]
    limb_segs = (8, 12, 16, 20)[detail]
    head_segs = (12, 18, 24, 30)[detail]
    head_rings = (8, 12, 16, 20)[detail]

    parts = [_bg_torso_shell(P, body_segs), _bg_neck_shell(P, limb_segs),
             _bg_head_shell(P, head_segs, head_rings)]
    for tag, sign in BG_SIDES:
        parts.append(_bg_arm_shell(P, sign, limb_segs, "Arm." + tag))
        parts.append(_bg_leg_shell(P, sign, limb_segs, "Leg." + tag))
        parts.append(_bg_foot_shell(P, sign, limb_segs, "Foot." + tag))

    body = bg_join(parts, name)
    if pose == "a" and body is not None:
        _bg_pose_arms(body, P, drop=BG_A_POSE_DROP)
    if smooth and body is not None:
        bg_only(body)
        bpy.ops.object.shade_smooth()
    if finish and body is not None:
        bg_finish(body, colour=colour, material=material)

    armature = bg_human_skeleton(P, name=name + "Skeleton",
                                 convention=convention, pose=pose) if rig else None
    return {"obj": body, "rig": armature, "props": P,
            "marks": bg_human_marks(P), "convention": convention,
            "pose": pose, "parts": []}


def _bg_pose_arms(obj, P, drop=BG_A_POSE_DROP):
    """Swing the arm verts down into an A-pose about each shoulder joint.

    Done on the MESH, before weighting, so the bind pose and the geometry
    agree — the bones use the same `_bg_swing` about the same pivot, so the
    armature lands inside the arm rather than beside it.

    FULL ROTATION FROM THE JOINT OUTWARD, blended in over the deltoid where the
    arm shell and the torso shell overlap. A hard cut there leaves a step in
    the surface at the shoulder, which survives every check in the kit and
    shows up as a crease on the first render anybody looks at.
    """
    if obj is None or obj.type != "MESH":
        return obj
    def _smooth(t):
        t = min(max(t, 0.0), 1.0)
        return t * t * (3.0 - 2.0 * t)

    for sign in (1.0, -1.0):
        pivot = (sign * P["shoulder_x"], 0.0, P["shoulder_z"])
        blend = P["upper_arm_r"] * 1.6
        # A HEIGHT WINDOW AS WELL AS A REACH ONE. The torso is wider at the hip
        # than the shoulder joint is far out, so a reach-only test swings the
        # pelvis and the outside of the thighs along with the arm — which reads
        # as a figure melting sideways and passes every geometry check there is.
        near, far = P["upper_arm_r"] * 1.5, P["upper_arm_r"] * 3.2
        for vert in obj.data.vertices:
            reach = sign * vert.co.x - sign * pivot[0]
            rise = abs(vert.co.z - pivot[2])
            if reach <= -blend or rise >= far:
                continue
            weight = _smooth((reach + blend) / blend if reach < 0.0 else 1.0)
            weight *= _smooth((far - rise) / (far - near))
            if weight <= 0.0:
                continue
            moved = _bg_swing(tuple(vert.co), pivot, -drop * sign * weight)
            vert.co = Vector(moved)
    obj.data.update()
    return obj


# --- quadruped --------------------------------------------------------------

def bg_quadruped_proportions(length=1.2, height=0.75, build=1.0, legs=1.0,
                             neck=1.0, tail=1.0):
    """A generic four-legged body: dog, wolf, horse, deer, cat, at any size.

    `height` is the OVERALL standing height and `length` the OVERALL nose-to-
    tail length, both in metres, both exact — the mesh is solved to hit them,
    not scaled toward them and hoped over. A cat is (0.55, 0.30), a wolf
    (1.4, 0.80), a horse (2.6, 1.7).

    `build` thickens the animal across its width only, so a heavier wolf is
    still exactly as tall as it was asked to be. That is the whole point of
    declaring a unit: a parameter that quietly changes the size of the asset is
    the same defect as an undeclared unit, in a smaller package.
    """
    length, height = float(length), float(height)
    if length <= 0 or height <= 0:
        raise ValueError("bg_quadruped_proportions: length and height must be "
                         "positive, got %r and %r" % (length, height))
    build = max(float(build), 0.15)
    legs = min(max(float(legs), 0.4), 1.6)

    # Nominal shape, in arbitrary units. Vertical/lateral values and along-the-
    # body values are kept apart, because they get two different normalising
    # factors below and mixing one into the other is how a "1.2 m wolf" ends up
    # 1.54 m long.
    belly_z = 0.62 * legs
    vertical = {"belly_z": belly_z, "chest_rz": 0.200, "waist_rz": 0.165,
                "hip_rz": 0.195, "neck_rz": 0.105, "head_rz": 0.115,
                "upper_leg_rz": 0.062, "lower_leg_rz": 0.040, "paw_rz": 0.052,
                "tail_rz": 0.035, "leg_x": 0.130, "knee_z": belly_z * 0.48}
    along = {"body_l": 1.0, "neck_l": 0.42 * float(neck), "head_l": 0.38,
             "tail_l": 0.65 * float(tail), "paw_ry": 0.075}
    # NOSE AT +Y, TAIL AT -Y — the same facing as bg_human, for the same
    # reason: whatever this animal is, it should walk into the engine's forward
    # and not out of it.
    along["shoulder_y"] = along["body_l"] * 0.42
    along["hip_y"] = -along["body_l"] * 0.44

    # The analytic silhouette of the shells built below — solve it, do not
    # measure it afterwards, so the numbers are queryable before anything is
    # built and a layer script can plan against them.
    z, cr = vertical["belly_z"], vertical["chest_rz"]
    top = max(z + cr * 1.05,
              z + cr * 1.50 + vertical["head_rz"],
              z + cr * 1.35 + vertical["neck_rz"] * 0.95)
    front = along["shoulder_y"] + along["neck_l"] + along["head_l"] * 1.02
    back = -(along["body_l"] * 0.58 + along["tail_l"])
    fz = height / top
    fy = length / (front - back)

    Q = {"unit": BG_UNIT, "length": length, "height": height, "build": build,
         "legs": legs}
    for key, value in vertical.items():
        Q[key] = value * fz
    for key, value in along.items():
        Q[key] = value * fy
    # Widths carry `build`; heights never do.
    for key in ("chest", "waist", "hip", "neck", "head", "upper_leg",
                "lower_leg", "paw", "tail"):
        Q[key + "_rx"] = Q[key + "_rz"] * build
    Q["leg_x"] *= build
    Q["withers_z"] = Q["belly_z"] + Q["chest_rz"]
    return Q


# Which keys are lateral/vertical (X and Z) rather than along the body (Y).
# bg_quadruped trims these after measuring, so `height` comes out exact: the
# solve above is exact for an ideal ellipse and the ring polygon INSCRIBES it,
# which left a 0.75 m wolf measuring 0.7459 — small, invisible, and still a
# declared number that was not true.
_BG_Q_VERTICAL = ("belly_z", "chest_rz", "waist_rz", "hip_rz", "neck_rz",
                  "head_rz", "upper_leg_rz", "lower_leg_rz", "paw_rz",
                  "tail_rz", "leg_x", "knee_z", "withers_z",
                  "chest_rx", "waist_rx", "hip_rx", "neck_rx", "head_rx",
                  "upper_leg_rx", "lower_leg_rx", "paw_rx", "tail_rx")


def bg_quadruped_marks(Q):
    z, cr = Q["belly_z"], Q["chest_rz"]
    neck_y = Q["shoulder_y"] + Q["neck_l"]
    marks = {
        "ground": _bg_mark((0, 0, BG_GROUND), Q["leg_x"] * 2),
        "withers": _bg_mark((0, Q["shoulder_y"], z + cr), (Q["chest_rx"], cr),
                            note="collar/harness front"),
        "back": _bg_mark((0, 0, z + Q["waist_rz"]), (Q["waist_rx"], Q["waist_rz"]),
                         note="the saddle sits here"),
        "rump": _bg_mark((0, Q["hip_y"], z + Q["hip_rz"]),
                         (Q["hip_rx"], Q["hip_rz"])),
        "belly": _bg_mark((0, 0, z - Q["waist_rz"]),
                          (Q["waist_rx"], Q["waist_rz"]), direction=(0, 0, -1)),
        "chest": _bg_mark((0, Q["shoulder_y"], z), (Q["chest_rx"], cr)),
        "neck": _bg_mark((0, Q["shoulder_y"] + Q["neck_l"] * 0.5, z + cr * 1.0),
                         (Q["neck_rx"], Q["neck_rz"]), note="collar goes here"),
        "head": _bg_mark((0, neck_y + Q["head_l"] * 0.4, z + cr * 1.5),
                         (Q["head_rx"], Q["head_rz"])),
        "muzzle": _bg_mark((0, neck_y + Q["head_l"] * 1.02, z + cr * 1.38),
                           (Q["head_rx"] * 0.3, Q["head_rz"] * 0.28),
                           direction=BG_FORWARD),
        "tail": _bg_mark((0, -Q["body_l"] * 0.58, z + Q["hip_rz"] * 0.70),
                         (Q["tail_rx"], Q["tail_rz"])),
    }
    for tag, sign in BG_SIDES:
        for end, y in (("front", Q["shoulder_y"]), ("back", Q["hip_y"])):
            marks[end + "_paw." + tag] = _bg_mark(
                (sign * Q["leg_x"], y + Q["paw_ry"] * 0.2, Q["paw_rz"] * 0.5),
                (Q["paw_rx"], Q["paw_ry"]), direction=(0, 0, -1),
                note="a boot or hoof goes here")
            marks[end + "_knee." + tag] = _bg_mark(
                (sign * Q["leg_x"], y, Q["knee_z"]),
                (Q["lower_leg_rx"] * 1.2, Q["lower_leg_rz"] * 1.2))
    return marks


def bg_quadruped(length=1.2, height=0.75, build=1.0, legs=1.0, neck=1.0,
                 tail=1.0, detail=1, name="Body", rig=True,
                 colour=(0.42, 0.34, 0.26), material="hide", finish=True,
                 smooth=True):
    """A clean four-legged base: barrel, neck, head, four legs, a tail.

    Same contract as bg_human — closed shells, one joined mesh, named bones,
    landmarks. Bone names are BG's own (Spine/Chest/Neck/Head/FrontUpperLeg.L
    …): no engine ships a canonical quadruped profile, so there is nothing to
    match and the names only have to be stable and readable.
    """
    Q = bg_quadruped_proportions(length, height, build, legs, neck, tail)
    detail = int(max(0, min(int(detail), 3)))
    body_segs = (10, 14, 20, 26)[detail]
    limb_segs = (8, 10, 14, 18)[detail]

    bl, z, cr = Q["body_l"], Q["belly_z"], Q["chest_rz"]
    # Rings run nose-end (+Y) to tail-end (-Y): descending along the loft axis,
    # which bg_shell bridges the same either way and recalc_face_normals then
    # turns outward regardless.
    barrel = bg_shell("Barrel", [
        ((0, bl * 0.60, z + cr * 0.30), Q["chest_rx"] * 0.42, cr * 0.48),
        ((0, Q["shoulder_y"], z + cr * 0.05), Q["chest_rx"] * 0.95, cr),
        ((0, 0, z + Q["waist_rz"] * 0.10), Q["waist_rx"] * 0.92, Q["waist_rz"]),
        ((0, Q["hip_y"], z + Q["hip_rz"] * 0.05), Q["hip_rx"] * 0.95, Q["hip_rz"]),
        ((0, -bl * 0.62, z + Q["hip_rz"] * 0.35), Q["hip_rx"] * 0.45, Q["hip_rz"] * 0.48),
    ], segs=body_segs, axis=1)

    neck_y0 = Q["shoulder_y"] + Q["neck_l"] * 0.05
    neck_y1 = Q["shoulder_y"] + Q["neck_l"]
    neck_obj = bg_shell("Neck", [
        ((0, neck_y0, z + cr * 0.60), Q["neck_rx"] * 1.25, Q["neck_rz"] * 1.35),
        ((0, (neck_y0 + neck_y1) * 0.5, z + cr * 1.00), Q["neck_rx"], Q["neck_rz"] * 1.1),
        ((0, neck_y1, z + cr * 1.35), Q["neck_rx"] * 0.88, Q["neck_rz"] * 0.95),
    ], segs=limb_segs, axis=1)

    head_y, hx, hz = neck_y1, Q["head_rx"], Q["head_rz"]
    head_obj = bg_shell("Head", [
        ((0, head_y - Q["head_l"] * 0.30, z + cr * 1.42), hx * 0.35, hz * 0.40),
        ((0, head_y, z + cr * 1.50), hx, hz),
        ((0, head_y + Q["head_l"] * 0.45, z + cr * 1.46), hx * 0.80, hz * 0.82),
        ((0, head_y + Q["head_l"] * 0.90, z + cr * 1.40), hx * 0.52, hz * 0.50),
        ((0, head_y + Q["head_l"] * 1.02, z + cr * 1.38), hx * 0.30, hz * 0.28),
    ], segs=limb_segs, axis=1)

    tail_obj = bg_shell("Tail", [
        ((0, -bl * 0.58, z + Q["hip_rz"] * 0.70), Q["tail_rx"] * 1.3, Q["tail_rz"] * 1.3),
        ((0, -(bl * 0.58 + Q["tail_l"] * 0.55), z + Q["hip_rz"] * 0.40),
         Q["tail_rx"] * 0.8, Q["tail_rz"] * 0.8),
        ((0, -(bl * 0.58 + Q["tail_l"]), z + Q["hip_rz"] * 0.05),
         Q["tail_rx"] * 0.35, Q["tail_rz"] * 0.35),
    ], segs=max(6, limb_segs // 2), axis=1)

    parts = [barrel, neck_obj, head_obj, tail_obj]
    for tag, sign in BG_SIDES:
        for end, y in (("Front", Q["shoulder_y"]), ("Back", Q["hip_y"])):
            x = sign * Q["leg_x"]
            # THE PAW STARTS AT z = 0. A four-legged base whose feet stop 9 mm
            # short of the ground looks correct from every angle and hovers in
            # the engine, which is the same defect as a floating humanoid and
            # is exactly as invisible on a turnaround.
            parts.append(bg_shell(end + "Leg." + tag, [
                ((x, y + Q["paw_ry"] * 0.20, BG_GROUND),
                 Q["paw_rx"] * 0.55, Q["paw_ry"] * 0.55),
                ((x, y + Q["paw_ry"] * 0.10, Q["paw_rz"] * 0.55),
                 Q["paw_rx"] * 0.85, Q["paw_ry"] * 0.90),
                ((x, y, Q["knee_z"]), Q["lower_leg_rx"], Q["paw_ry"] * 0.60),
                ((x, y, z * 0.88), Q["upper_leg_rx"] * 0.92, Q["paw_ry"] * 0.80),
                ((x, y, z + cr * 0.30), Q["upper_leg_rx"] * 0.80, Q["paw_ry"] * 0.70),
            ], segs=limb_segs, axis=2))

    body = bg_join(parts, name)
    if body is not None:
        # Trim X and Z (never Y — `length` is already exact) so the animal
        # measures the height it was asked for, and carry the same factor into
        # the proportions the rig and the landmarks are built from. Correcting
        # the mesh and not the numbers would be worse than not correcting.
        measured = bg_bounds(body)["dims"][2]
        if measured > 1e-9:
            trim = Q["height"] / measured
            if abs(trim - 1.0) > 1e-6:
                body.scale = (body.scale[0] * trim, body.scale[1],
                              body.scale[2] * trim)
                bpy.context.view_layer.update()
                for key in _BG_Q_VERTICAL:
                    if key in Q:
                        Q[key] *= trim
    if smooth and body is not None:
        bg_only(body)
        bpy.ops.object.shade_smooth()
    if finish and body is not None:
        bg_finish(body, colour=colour, material=material)
    elif body is not None:
        bg_apply(body)

    z, cr = Q["belly_z"], Q["chest_rz"]
    armature = None
    if rig:
        chain = [
            ("Root", (0, 0, 0), (0, 0, Q["height"] * 0.12), None),
            ("Hips", (0, Q["hip_y"], z + Q["hip_rz"] * 0.5),
             (0, 0, z + Q["waist_rz"] * 0.6), "Root"),
            ("Spine", (0, 0, z + Q["waist_rz"] * 0.6),
             (0, Q["shoulder_y"], z + cr * 0.6), "Hips"),
            ("Chest", (0, Q["shoulder_y"], z + cr * 0.6),
             (0, neck_y0, z + cr * 0.75), "Spine"),
            ("Neck", (0, neck_y0, z + cr * 0.75),
             (0, neck_y1, z + cr * 1.35), "Chest"),
            ("Head", (0, neck_y1, z + cr * 1.35),
             (0, neck_y1 + Q["head_l"] * 0.95, z + cr * 1.42), "Neck"),
            ("Tail", (0, -bl * 0.58, z + Q["hip_rz"] * 0.70),
             (0, -(bl * 0.58 + Q["tail_l"]), z + Q["hip_rz"] * 0.05), "Hips"),
        ]
        for tag, sign in BG_SIDES:
            for end, y, parent in (("Front", Q["shoulder_y"], "Chest"),
                                   ("Back", Q["hip_y"], "Hips")):
                x = sign * Q["leg_x"]
                chain += [
                    (end + "UpperLeg." + tag, (x, y, z + cr * 0.15),
                     (x, y, Q["knee_z"]), parent),
                    (end + "LowerLeg." + tag, (x, y, Q["knee_z"]),
                     (x, y, Q["paw_rz"] * 0.55), end + "UpperLeg." + tag),
                    (end + "Paw." + tag, (x, y, Q["paw_rz"] * 0.55),
                     (x, y + Q["paw_ry"] * 0.5, Q["paw_rz"] * 0.20),
                     end + "LowerLeg." + tag),
                ]
        rows = [(n, h, t, p, bg_roll(h, t, BG_UP if "Paw" in n else BG_FORWARD))
                for n, h, t, p in chain]
        armature = bg_bone_chain(name + "Skeleton", rows)
        if "Root" in armature.data.bones:
            armature.data.bones["Root"].use_deform = False
    return {"obj": body, "rig": armature, "props": Q,
            "marks": bg_quadruped_marks(Q), "convention": "bgate-quadruped",
            "pose": "stand", "parts": []}


# --- prop frame -------------------------------------------------------------

def bg_prop_frame(size=(0.4, 0.4, 0.6), name="Prop", bevel=0.06, detail=1,
                  rig=True, colour=(0.55, 0.55, 0.58), material="prop",
                  finish=True, smooth=False, grip=None):
    """A generic prop body: a bevelled box shell, on the ground, with anchors.

    A PROP IS NOT A CHARACTER AND STILL NEEDS THE SAME THREE THINGS — a
    declared size, a place things attach, and a bone to bind to. Anchors:
    base / top / front / back / left / right / centre / grip. `grip` overrides
    where a held prop meets a hand; it defaults to the lower third of the
    front face, which is where a handle is.
    """
    w, d, h = (float(v) for v in size)
    if min(w, d, h) <= 0.0:
        raise ValueError("bg_prop_frame: every dimension must be positive, got %r"
                         % (size,))
    bevel = min(max(float(bevel), 0.0), 0.45)
    segs = (4, 8, 12, 16)[int(max(0, min(int(detail), 3)))]
    hw, hd = w * 0.5, d * 0.5
    inset = 1.0 - bevel
    # THE END RINGS SIT AT z=0 AND z=h EXACTLY. Insetting them vertically as
    # well as horizontally made a "0.6 m crate" measure 0.564 and stand 18 mm
    # off the floor — a 6% error that no render shows and that bg_unit_check
    # will not catch either, because 6% is inside any sane tolerance.
    rows = [
        ((0, 0, BG_GROUND), hw * inset, hd * inset),
        ((0, 0, h * bevel), hw, hd),
        ((0, 0, h * (1.0 - bevel)), hw, hd),
        ((0, 0, h), hw * inset, hd * inset),
    ]
    obj = bg_shell(name, rows, segs=segs, axis=2)
    if smooth and obj is not None:
        bg_only(obj)
        bpy.ops.object.shade_smooth()
    if finish and obj is not None:
        bg_finish(obj, colour=colour, material=material)

    marks = {
        "base": _bg_mark((0, 0, BG_GROUND), (hw, hd), size=(w, d, 0.0),
                         direction=(0, 0, -1)),
        "centre": _bg_mark((0, 0, h * 0.5), (hw, hd), size=(w, d, h)),
        "top": _bg_mark((0, 0, h), (hw, hd), size=(w, d, 0.0)),
        # A PROP'S FRONT IS THE SAME FRONT A CHARACTER HAS. A crate whose
        # "front" faced the opposite way from the figure carrying it would put
        # every decal on its back the moment either one was right.
        "front": _bg_mark((0, hd, h * 0.5), (hw, h * 0.5),
                          size=(w, 0.0, h), direction=BG_FORWARD),
        "back": _bg_mark((0, -hd, h * 0.5), (hw, h * 0.5), size=(w, 0.0, h),
                         direction=(0, -1, 0)),
        "left": _bg_mark((-hw, 0, h * 0.5), (hd, h * 0.5), direction=BG_LEFT),
        "right": _bg_mark((hw, 0, h * 0.5), (hd, h * 0.5), direction=(1, 0, 0)),
        "grip": _bg_mark(tuple(grip) if grip else (0, hd * 0.9, h * 0.33),
                         min(w, d) * 0.22, direction=BG_FORWARD,
                         note="where a hand holds it — align to hand.L/hand.R"),
    }
    armature = None
    if rig:
        armature = bg_bone_chain(name + "Skeleton", [
            ("Root", (0, 0, 0), (0, 0, h * 0.18), None,
             bg_roll((0, 0, 0), (0, 0, h * 0.18), BG_FORWARD)),
            ("Body", (0, 0, 0), (0, 0, h), "Root",
             bg_roll((0, 0, 0), (0, 0, h), BG_FORWARD)),
        ])
        if "Root" in armature.data.bones:
            armature.data.bones["Root"].use_deform = False
    return {"obj": obj, "rig": armature, "marks": marks,
            "props": {"unit": BG_UNIT, "size": (w, d, h), "bevel": bevel},
            "convention": "bgate-prop", "pose": "rest", "parts": []}


# ---------------------------------------------------------------------------
# Weighting and the self-check
# ---------------------------------------------------------------------------

def bg_weight(obj, rig, kind="ARMATURE_AUTO"):
    """Bind a mesh to an armature and REPORT WHAT IS STILL UNWEIGHTED.

    THE ONE MEASURED FAILURE MODE OF THIS PIPELINE. bpy.ops does not raise when
    bone-heat gives up; it leaves vertices with no group, which reads in-engine
    as the mesh tearing at the rest pose and reads in Blender as nothing at
    all. Count them here, while the fix is still a bg_clean away.

    Returns bound / unweighted / total / bones / verdict.
    """
    if obj is None or rig is None or obj.type != "MESH":
        return {"bound": False, "unweighted": 0, "total": 0, "bones": 0,
                "verdict": "nothing to bind"}
    deform = {b.name for b in rig.data.bones if b.use_deform}
    try:
        bg_deselect()
        obj.select_set(True)
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.object.parent_set(type=kind)
    except Exception as exc:
        return {"bound": False, "unweighted": len(obj.data.vertices),
                "total": len(obj.data.vertices), "bones": len(deform),
                "verdict": "weighting refused this mesh: %s" % exc}
    groups = obj.vertex_groups
    loose = 0
    for vert in obj.data.vertices:
        if not any(g.weight > 0.0 and groups[g.group].name in deform
                   for g in vert.groups):
            loose += 1
    total = len(obj.data.vertices)
    return {"bound": True, "unweighted": loose, "total": total,
            "bones": len(deform),
            "verdict": ("every vertex weighted (%d verts, %d deform bones)"
                        % (total, len(deform))) if not loose else
                       ("%d of %d vertices carry no deform weight — they will "
                        "stay at the rest pose while the rest of the mesh moves"
                        % (loose, total))}


def bg_base_report(base, expect_height=None):
    """Everything worth knowing about a base, in one dict, for one print.

    verts/faces/loose/nonmanifold/flipped, the measured dims against the asked-
    for height, the bone names, and the unit verdict. Print this and a reviewer
    can see whether the base is sound without opening Blender.
    """
    obj = base.get("obj") if isinstance(base, dict) else base
    rig = base.get("rig") if isinstance(base, dict) else None
    props = base.get("props", {}) if isinstance(base, dict) else {}
    stats = bg_stats(obj) or {}
    if expect_height is None:
        expect_height = (props.get("height")
                         or (props.get("size") or (0, 0, 0))[2]
                         or stats.get("dims", (0, 0, 0))[2])
    unit = bg_unit_check(obj, expect=expect_height or BG_HUMAN_HEIGHT,
                         label=(obj.name if obj else "base"))
    return {"verts": stats.get("verts", 0), "faces": stats.get("faces", 0),
            "loose": stats.get("loose", 0),
            "nonmanifold": stats.get("nonmanifold", 0),
            "flipped": stats.get("flipped", 0), "ngons": stats.get("ngons", 0),
            "dims": stats.get("dims", (0, 0, 0)),
            "min": stats.get("min", (0, 0, 0)),
            "bones": sorted(b.name for b in rig.data.bones) if rig else [],
            "deform_bones": sorted(b.name for b in rig.data.bones
                                   if b.use_deform) if rig else [],
            "unit": unit, "marks": sorted((base or {}).get("marks", {}))}


def bg_base_assert(base, expect_height=None):
    """The four things a base must be true about, LOUDLY.

    On the ground, the right size, closed, and not inside-out. Every one of
    these passes a visual check and fails in the engine, which is why they are
    an assertion and not a note.
    """
    report = bg_base_report(base, expect_height=expect_height)
    problems = []
    if report["loose"]:
        problems.append("%d loose vertices — bone-heat weighting will refuse them"
                        % report["loose"])
    if report["nonmanifold"]:
        problems.append("%d non-manifold edges — the shell is open"
                        % report["nonmanifold"])
    if report["flipped"]:
        problems.append("%d inverted faces — call bg_clean(obj, recalc=True)"
                        % report["flipped"])
    if not report["unit"]["ok"]:
        problems.append(report["unit"]["verdict"])
    if abs(report["min"][2] - BG_GROUND) > max(0.01, report["dims"][2] * 0.02):
        problems.append("the soles are at z=%.4f, not on the ground plane"
                        % report["min"][2])
    if problems:
        raise ValueError("bg_base_assert: " + "; ".join(problems))
    return report




def bg_weld(obj, *, fraction=0.0006, merge=0.0):
    """Weld a generated mesh gently, and report whether it can be decimated.

    GENTLY. The obvious move is to merge hard until the confetti becomes one
    shell, and it is wrong — MEASURED on a generated mannequin, chasing one
    shell took non-manifold edges from 3 to 20,285, and the decimator then
    could not reach an 8,000 triangle budget at all, stalling at 49,261 no
    matter how many passes it was given. The same mesh at a sixth of that merge
    distance sat in 4 shells with 3 non-manifold edges and decimated to 7,999.

    So NON-MANIFOLD COUNT is what predicts decimatability, not shell count.
    Collapse will not cross a non-manifold junction, and over-merging
    manufactures them faster than it removes shells.

    Distance is a fraction of the object's own bounding-box diagonal, so one
    setting serves a 0.2 m cap and a 2 m figure. Pass `merge` to override with
    an absolute distance.
    """
    diag = max((sum(d * d for d in obj.dimensions)) ** 0.5, 1e-6)
    used = float(merge) if merge else diag * float(fraction)
    bg_clean(obj, merge=used)
    shells, nonmanifold = bg_shells(obj), bg_nonmanifold(obj)
    tris = sum(len(p.vertices) - 2 for p in obj.data.polygons)
    thin = nonmanifold <= max(64, tris * 0.005)
    return {"merge": round(used, 6), "fraction": round(used / diag, 5),
            "shells": shells, "nonmanifold": nonmanifold, "tris": tris,
            "decimatable": thin,
            "verdict": ("%d shell(s), %d non-manifold — decimates cleanly"
                        % (shells, nonmanifold)) if thin else
                       ("%d non-manifold edges on %d triangles — collapse will "
                        "stall well above any low budget, and merging harder "
                        "makes this worse, not better" % (nonmanifold, tris))}


def bg_nonmanifold(obj):
    """Edges that are not shared by exactly two faces. The decimation blocker."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    count = sum(1 for edge in bm.edges if not edge.is_manifold)
    bm.free()
    return count


def bg_shells(obj):
    """How many disconnected pieces this mesh is in. 1 is a surface."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    seen, count = set(), 0
    for vert in bm.verts:
        if vert.index in seen:
            continue
        count += 1
        stack = [vert]
        while stack:
            here = stack.pop()
            if here.index in seen:
                continue
            seen.add(here.index)
            for edge in here.link_edges:
                stack.append(edge.other_vert(here))
    bm.free()
    return count


def bg_axes(obj, *, kind="humanoid", up=2):
    """Which way is this mesh's lateral, forward and up — for a KNOWN kind.

    THERE IS NO UNIVERSAL RULE HERE and the first version of this pretended
    there was. MEASURED: "up is the tallest axis" put a cap's up along its brim,
    because a cap lying flat is longer front-to-back than it is tall; and
    "widest horizontal is lateral" is a T-POSE rule that reads a cap exactly
    backwards, since a cap's longest horizontal IS its forward.

    So `up` is world up unless you say otherwise, and the lateral/forward split
    is chosen per kind:

      humanoid  arms span side to side, so lateral is the wider horizontal
      long      the subject is longer than it is wide along its own forward —
                a cap, a shoe, a vehicle, a fish
      none      no opinion; forward is Y and nothing will be rotated

    `certainty` is how lopsided the two horizontals are. Near zero they are the
    same width and the split is a coin toss whatever the kind says.
    """
    dims = [max(d, 1e-9) for d in obj.dimensions]
    up = int(up) % 3
    flat = [i for i in (0, 1, 2) if i != up]
    wide, narrow = (flat if dims[flat[0]] >= dims[flat[1]]
                    else [flat[1], flat[0]])
    if kind == "long":
        lateral, forward = narrow, wide
    else:
        lateral, forward = wide, narrow
    span = max(dims[wide], 1e-9)
    return {"up": up, "lateral": lateral, "forward": forward, "kind": kind,
            "dims": tuple(round(d, 4) for d in dims),
            "certainty": round(abs(dims[wide] - dims[narrow]) / span, 3)}


def bg_facing(obj, axes=None):
    """Which way along the forward axis is the FRONT. Reads the lowest slab.

    Toes are the most reliable asymmetry an upright figure has: a foot reaches
    much further forward of the ankle than the heel does behind it, so the
    bottom of a standing mesh has its centre of mass on the toe side. A face is
    no use — a generated head is often featureless, and ours is an egg by
    design.

    THIS ONLY MEANS ANYTHING FOR SOMETHING THAT STANDS ON FEET. Asked about a
    cap it answered "toes lead by 152.8%", which is not a fact about a cap.
    `confident` is the field to read; the sign without it is noise.
    """
    axes = axes or bg_axes(obj)
    up, forward = axes["up"], axes["forward"]
    world = [obj.matrix_world @ v.co for v in obj.data.vertices]
    if not world:
        return {"sign": 1, "strength": 0.0, "confident": False,
                "verdict": "no geometry to read"}
    lows = [p[up] for p in world]
    floor, ceiling = min(lows), max(lows)
    cut = floor + (ceiling - floor) * 0.06
    slab = [p[forward] for p in world if p[up] <= cut] or [p[forward] for p in world]
    body = sum(p[forward] for p in world) / len(world)

    # REACH, not centroid. A foot is not shifted forward so much as it EXTENDS
    # forward: the toe reaches much further from the ankle than the heel does.
    # MEASURED, and the reason this is not the obvious one-liner: by centroid
    # offset a mannequin scored 2.7% and a CRATE scored 2.8%, so the signal did
    # not discriminate at all and the mannequin was simply lucky.
    ahead, behind = max(slab) - body, body - min(slab)
    bigger, smaller = max(ahead, behind), max(min(ahead, behind), 1e-9)
    ratio = bigger / smaller
    ok = ratio >= 1.25 and axes.get("kind") == "humanoid"
    return {"sign": 1 if ahead >= behind else -1, "strength": round(ratio, 3),
            "confident": ok,
            "verdict": ("toes reach %.2fx further than the heel" % ratio) if ok
                       else ("no readable front — the two sides reach %.2fx, "
                             "which is not a foot" % ratio)}


def bg_orient(obj, *, kind="humanoid", forward=None, assume=None, up=2):
    """Turn a mesh so it faces the project's forward. REFUSES TO GUESS.

    This is the step a generated layer cannot skip. A wrong scale is obvious in
    a render and a wrong position is obvious in a check; facing the wrong way
    is invisible in a symmetric silhouette and then everything downstream is
    quietly mirrored — the turnaround's labels, a bone-parented cap, Godot's
    -Z convention, the collider.

    But it only rotates when it can justify it: a confident reading, or an
    explicit `assume` of +1/-1. MEASURED on real generated output, guessing
    turned a crate 90 degrees onto a different footprint for no reason. An
    asset with no front is not a problem to be solved, so `kind="none"` and
    unreadable geometry both leave the mesh exactly where it was.

    Rotation is in 90-degree steps about the up axis, which is what a generated
    mesh actually needs — it comes out roughly axis-aligned and turned by a
    quarter or a half. A mesh tilted off-axis is not corrected; that is what
    bg_axes' `certainty` is for.
    """
    import math
    target = tuple(forward if forward is not None else BG_FORWARD)
    axes = bg_axes(obj, kind=kind, up=up)
    read = bg_facing(obj, axes)
    forced = assume in (1, -1)
    if kind == "none" or not (read["confident"] or forced):
        return {"turned_deg": 0, "was": read, "axes": axes, "confident": False,
                "note": "left alone — " + ("this kind has no front"
                        if kind == "none" else read["verdict"] +
                        "; pass assume=+1/-1 if you know which way it faces")}

    sign = assume if forced else read["sign"]
    want = 0 if abs(target[0]) > abs(target[1]) else 1
    want_sign = 1 if (target[want] or 1) > 0 else -1

    turns = 1 if axes["forward"] != want else 0
    if sign != want_sign:
        turns += 2
    turns %= 4
    if turns:
        bg_only(obj)
        obj.rotation_mode = "XYZ"
        obj.rotation_euler.rotate_axis("Z", math.radians(90.0 * turns))
        bg_apply(obj, location=False, rotation=True, scale=False)
    bpy.context.view_layer.update()
    return {"turned_deg": 90 * turns, "was": read, "axes": axes,
            "confident": True, "note": ""}


def bg_adopt(obj, *, kind="humanoid", height=None, ground=True, orient=True,
             assume=None, merge=0.0015, budget=0):
    """Take a generated mesh and make it something this pipeline can use.

    One call for everything a generation arrives WITHOUT: it is a pile of
    disconnected shells at an arbitrary scale, facing an arbitrary direction,
    floating at an arbitrary height. MEASURED on real Krea output: a crate came
    back as 20,748 shells and 495,061 tris; a cap as 628 shells; a mannequin as
    604 shells, 21,796 non-manifold edges and 95,232 tris that cleaned down to
    4 shells and 7,928 tris and scaled to 1.800 m exactly.

    Order matters and it is not obvious: clean first so the decimator has
    welded geometry to work on, then scale, then orient, then drop to the
    ground — orienting before scaling is fine, but grounding before scaling
    leaves the mesh floating by whatever the scale factor was.
    """
    report = {"before": bg_stats(obj)}
    # WELD BEFORE DECIMATING. Not a nicety of ordering — the decimator cannot
    # cross a shell boundary, so on unwelded confetti a low budget is simply
    # unreachable and comes back silently over.
    report["weld"] = bg_weld(obj, merge=merge) if merge else {}
    if budget:
        tris = sum(len(p.vertices) - 2 for p in obj.data.polygons)
        if tris > budget:
            mod = obj.modifiers.new("BGateDecimate", "DECIMATE")
            mod.ratio = max(0.005, float(budget) / float(tris))
            bg_only(obj)
            bpy.ops.object.modifier_apply(modifier=mod.name)
            bg_clean(obj, merge=merge)
        got = sum(len(p.vertices) - 2 for p in obj.data.polygons)
        # SAY SO WHEN THE BUDGET WAS NOT MET. Returning a mesh 50x over budget
        # with an ok-looking report is the failure this whole pass exists to
        # stop.
        report["budget"] = {"asked": budget, "got": got, "met": got <= budget * 1.1,
                            "reason": "" if got <= budget * 1.1 else
                            "collapse stalled at %d — %s" % (
                                got, report.get("weld", {}).get("verdict", ""))}
    if height:
        tall = max(obj.dimensions[2], 1e-9)
        factor = float(height) / tall
        obj.scale = (factor, factor, factor)
        bg_apply(obj, location=False, rotation=False, scale=True)
    if orient:
        report["orient"] = bg_orient(obj, kind=kind, assume=assume)
    if ground:
        bpy.context.view_layer.update()
        low = min((obj.matrix_world @ v.co).z for v in obj.data.vertices)
        obj.location.z -= (low - BG_GROUND)
        bpy.context.view_layer.update()
    report["after"] = bg_stats(obj)
    report["quality"] = bg_collapse_ok(report["after"])
    report["ok"] = bool(report["quality"]["ok"]
                        and report.get("budget", {"met": True})["met"])
    return report


def bg_collapse_ok(stats):
    """Did the collapse leave an ASSET, or a triangle count that happens to fit?

    THE HOLE THIS FILLS, measured: a generated head decimated from 143,534 to
    39,803 faces reported budget met=True with no complaint, and was ruined —
    every hair strand a ribbon, the face shredded. The number that said so was
    already in the same report: 20,799 flipped faces out of 39,803, i.e. 52% of
    the surface inside out. A budget check counts triangles; it cannot see that
    the thing those triangles describe was destroyed.

    Thresholds come from the runs either side of that failure, not from taste.
    Clean adopts of the same generator measured 0.5-0.9% flipped and 8-11%
    non-manifold; the ruined one measured 52% and 33%. 5% and 20% sit in the gap
    with room on both sides.

    A NEGATIVE COUNT IS UNKNOWN AND FAILS CLOSED — see bg_flipped, which used to
    answer 0 both when it found nothing and when it broke. A gate that cannot
    tell those apart is the failure this exists to stop.
    """
    faces = max(int(stats.get("faces", 0)), 1)
    flipped, nonmanifold = stats.get("flipped", -1), stats.get("nonmanifold", -1)
    reasons = []
    if flipped < 0 or nonmanifold < 0:
        reasons.append("mesh checks did not complete — treating as unverified")
    else:
        if flipped > faces * 0.05:
            reasons.append("%d of %d faces inside out (%.0f%%) — the collapse "
                           "turned the surface inside out rather than "
                           "simplifying it"
                           % (flipped, faces, 100.0 * flipped / faces))
        if nonmanifold > faces * 0.20:
            reasons.append("%d non-manifold edges on %d faces (%.0f%%) — the "
                           "surface is shredded rather than welded"
                           % (nonmanifold, faces, 100.0 * nonmanifold / faces))
    return {"ok": not reasons, "faces": faces, "flipped": flipped,
            "nonmanifold": nonmanifold,
            "flipped_ratio": round(flipped / faces, 4) if flipped >= 0 else None,
            "nonmanifold_ratio": (round(nonmanifold / faces, 4)
                                  if nonmanifold >= 0 else None),
            "reason": "; ".join(reasons),
            "advice": ("raise the budget or skip decimation — this mesh does "
                       "not survive it" if reasons else "")}


def bg_base_help():
    """Print the worked base-mesh script. Read it before you model a character."""
    print(BG_BASE_EXAMPLE)
    return BG_BASE_EXAMPLE
'''


# ---------------------------------------------------------------------------
# The worked base-mesh script. It runs as written.
# ---------------------------------------------------------------------------

BASE_EXAMPLE = r'''
# --- Builders Gate reference: a character built ON a base, not from nothing ---
# START FROM THE BASE. bg_human() is a correct body; your job is the character
# on top of it. Every number you need comes out of bg_mark(), so nothing below
# is a guess about where the head is.

bg_wipe()

base = bg_human(height=1.8, heads=7.5, build=1.0)   # metres, always
body, rig, marks = base["obj"], base["rig"], base["marks"]

# The base checks itself. These four failures are invisible on a render and
# fatal in an engine, so they raise.
print("base", bg_base_report(base))
bg_base_assert(base)

# ---- which way is forward --------------------------------------------------
# THE BASE FACES +Y. Blender's own front view looks along +Y, so this is NOT
# the convention a Blender tutorial teaches — but the glTF exporter turns
# Blender +Y into glTF -Z, and -Z is what Godot calls forward. Author anything
# with a front — a face, a visor, a chest emblem, a muzzle — on the +Y side,
# and it arrives in the engine facing the way the character walks.
face = bg_mark(base, "face")
assert face["pos"][1] > 0.0 and tuple(face["dir"]) == BG_FORWARD, face
assert base["props"]["toe_y"] > 0.0 > base["props"]["heel_y"]   # toes lead

# ---- a layer, authored ONTO the body ---------------------------------------
# A cap is not "a sphere at 1.7 m". It is "a sphere the width of THIS head,
# sitting on THIS crown" — and both numbers are published.
head = bg_mark(base, "head")
crown = bg_mark(base, "head_top")

cap = bg_ball("Cap", radius=0.5)                     # any size; bg_fit resizes
bg_fit(cap, head, mode="around", clearance=0.006)    # hatband girth, centred
bg_fit(cap, crown, mode="on", clearance=-0.02)       # then dropped onto the crown

# The check that could not be written before there was a shared frame.
sit = bg_overlap(cap, body)
assert sit["intersects"], "the cap is floating off the head: " + sit["verdict"]
assert sit["fraction"] < 0.6, "the cap is inside the skull: " + sit["verdict"]

# A held prop finds the hand the same way.
sword = bg_box("Sword", size=(0.05, 0.05, 0.9))
bg_fit(sword, bg_mark(base, "hand.R"), mode="at", scale=False)

bg_finish(cap, colour=(0.15, 0.22, 0.45), material="cloth")
bg_finish(sword, colour=(0.72, 0.74, 0.78), material="steel")

# ---- prove the rig actually takes the body ---------------------------------
# Not "it looks rigged" — the number of vertices that would stay behind.
bind = bg_weight(body, rig)
assert bind["unweighted"] == 0, bind["verdict"]

# ---- what to hand the pipeline ---------------------------------------------
# Export body and rig as one layer, the cap as its own, and bind the cap to the
# bone by NAME — never by index, never by guessing the spelling.
print("cap binds to bone:", bg_bone(base, "head"))     # -> "Head"
print("sword binds to bone:", bg_bone(base, "hand.R"))  # -> "RightHand"
print("unit:", BG_UNIT, "height:", round(bg_bounds(body)["dims"][2], 3))
# --- end reference script ---------------------------------------------------
'''
