"""Where a body's landmarks are, measured from its points and nothing else.

TWO WORLDS, ONE COPY OF THE SOURCE. These functions run inside Blender, spliced
into the rig script by `blender._rig_script()`, and they run in a test process
that has never seen bpy. That is the whole reason the module exists: the
measurement they do is the hardest thing in the fitting path to get right, and
until now it lived inside a triple-quoted string where nothing could import it.
Four variants of the crease finder were built against real meshes at 20-60
seconds an iteration; as pure functions over a synthetic point cloud the same
four are milliseconds, and cheap experiments are the ones that get run.

SO: STANDARD LIBRARY ONLY, AND NO bpy. A point is a plain ``(x, y, z)``. The
caller inside Blender is a three-line adapter that hands over
``[tuple(matrix_world @ v.co) for v in mesh.data.vertices]``; anything that
needs an object, a modifier or a depsgraph belongs on the far side of that
adapter, not in here.

WHAT IS MEASURED, and everything else in the fitting path is still assumed:
floor, crown, height, half-width, and per side a shoulder joint, a fingertip
and the arm between them. The trunk — crotch, waist, chest, neck base — is NOT
measured here yet; it comes from an idealised figure scaled by total height
alone, which is the defect this module was carved out to close next.
"""

ARM_BANDS = 64          # ~2.7 cm bands on a 1.75 m figure
ARM_SPLIT = 0.02        # an empty run this much of body height is a crease
ARM_FLOOR = 3           # points either side of it before it counts as one
ARM_BRIDGE = 2          # bands a crease may vanish for and still be the same one

# HOW CLOSE TO THE MIDLINE COUNTS AS ON IT, as a fraction of body height. 1% is
# 1.75 cm on an adult, and the value is not free: see crotch_height for the
# measurement that fixed it and the density at which it stops mattering.
MIDLINE = 0.010
MIDLINE_FLOOR = 2       # points on the midline before a band counts as filled
MIDLINE_RUN = 3         # consecutive filled bands before the crotch is called
# WHERE A CROTCH CAN BE AT ALL, as a fraction of total height. Derived from
# bg_proportions rather than picked: crotch_z is 0.577 of chin height, so it
# lands at 38% of total on a 3-head chibi and 51% on an 8-head heroic figure.
# The floor is the shortest legs the module will build — `limbs` clamps at 0.4,
# which drags a chibi's crotch to 17% — and the ceiling has the same margin.
CROTCH_BAND = (0.15, 0.70)
# Where to look for the skull, as a fraction of the shoulder-to-crown span.
SKULL_SEARCH = 0.35

# WHAT EACH LANDMARK IS WORTH, as (low, high) error in fractions of TOTAL
# HEIGHT, measured against bg_human figures whose landmarks are known by
# construction across 20 cells — five head counts, two builds, both stances.
# tests/adapters/test_rig_known_figure.py holds the per-cell tables and fails if any of
# these is exceeded, so this is a record of a measurement rather than a claim.
#
# A LANDMARK WITHOUT ITS ERROR BAR IS A GUESS WEARING A NUMBER, and the reason
# to carry these on the rows rather than in a summary is that they are big
# enough to matter: the shoulder's own bar is 0.134 of body height against a
# template_deviation threshold of 0.08, so a bone derived from it can fail that
# gate on measurement error alone. Reading "measured, and this measurement
# cannot tell" off the row is the whole point.
LANDMARK_ENVELOPE = {
    "crown": (0.0, 0.0),
    "neck_base": (-0.003, 0.020),
    "crotch": (-0.111, -0.032),
    "shoulder_line": (-0.134, 0.034),
}

SIDES = (("Left", -1.0), ("Right", 1.0))

# THE TEMPLATE'S OWN TRUNK, as fractions of the crotch-to-shoulder span rather
# than of total height. This is the whole correction: bg_proportions places
# every trunk landmark at a fraction of CHIN HEIGHT, so a character with a big
# head or short legs gets its spine and chest at an adult's absolute heights.
# Measured against the same numbers as a proportion of the span between two
# landmarks that CAN be measured, the same canon adapts to the body.
#
#   crotch 0.577  waist 0.738  chest 0.862  upperchest 0.910  shoulder 0.955
#   hips head = crotch + 0.045          span = 0.955 - 0.577 = 0.378
TRUNK_SPAN = {
    "hips": 0.045 / 0.378,
    "waist": (0.738 - 0.577) / 0.378,
    "chest": (0.862 - 0.577) / 0.378,
    "upperchest": (0.910 - 0.577) / 0.378,
    "shoulder": 1.0,
}
# And the head, as fractions of the shoulder-to-crown span. Both ends of THAT
# are measured too, so the head chain rides on the same footing as the trunk.
HEAD_SPAN = {"neck": 0.068, "crown_inset": 0.046}


def _mean(points):
    """Centroid of a list of (x, y, z), as a list."""
    count = float(len(points))
    return [sum(p[0] for p in points) / count,
            sum(p[1] for p in points) / count,
            sum(p[2] for p in points) / count]


def side_creases(points, sgn, lo, h, bands=ARM_BANDS):
    """Per horizontal band on one side, where an arm's surface stops being the
    torso's surface.

    THE ONE QUESTION A POINT CLOUD ANSWERS RELIABLY. Vertex spacing is whatever
    the decimator left behind, which makes the cloud a poor ruler for a WIDTH —
    measured on a real generated character, per-band clustering resolved a
    torso column in 23 of 175 bands. It is a fine ruler for "is there a run of
    empty x here", and that is all this asks of it.

    Sorting one band's |x| and splitting wherever the spacing exceeds ARM_SPLIT
    gives that band's segments; two or more mean the arm's surface and the
    body's are still distinct at this height, even where the two solids touch.
    That distinction is the whole point. Rays cast through this character find
    arm and torso fused into one span from z=0.936 up, while the crease between
    them stays legible in the vertices to z=1.27 — and 1.27 is the shoulder,
    0.936 only the armpit.

    Returns (creases, reach). creases[band] is (body_edge, arm_edge) in |x|,
    the two sides of the crease; reach[band] is the furthest anything on this
    side gets in that band.
    """
    step = h / float(bands)
    if step <= 0.0:
        return {}, {}
    rows = {}
    for p in points:
        if sgn * p[0] <= 0.0:
            continue
        rows.setdefault(min(bands - 1, int((p[2] - lo) / step)), []).append(
            abs(p[0]))
    creases, reach = {}, {}
    for band, xs in rows.items():
        xs.sort()
        reach[band] = xs[-1]
        runs, first, last, count = [], xs[0], xs[0], 1
        for x in xs[1:]:
            if x - last > h * ARM_SPLIT:
                runs.append((first, last, count))
                first, count = x, 1
            else:
                count += 1
            last = x
        runs.append((first, last, count))
        solid = [r for r in runs if r[2] >= ARM_FLOOR]
        if len(solid) >= 2 and solid[0][0] < solid[-1][0]:
            creases[band] = (solid[0][1], solid[-1][0])
    return creases, reach


def arm_landmarks(points, sgn, lo, h):
    """One side's shoulder joint and fingertip, or why neither could be found.

    THE ARM IS WHAT THE TORSO IS NOT TOUCHING. The rule this replaced —
    everything past 55% of the half-width is arm — reads that half-width off
    the FINGERTIPS, so on the A-pose this pipeline rigs by default it selects
    hand and forearm and calls their innermost slice a shoulder. Measured on a
    real character: the joint landed at x=0.44, z=0.925, which is the bicep.
    The clamp meant to catch exactly that sampled torso width at a fixed 62% of
    height, and 62% of height on an A-pose is mid-forearm — 0.956 across — so
    it never bit. Both Shoulder bones came out 0.643 m long, 36.7% of body
    height against the template's 5.2%, each running from beside the neck
    diagonally across the whole ribcage to the bicep; nearest-segment weighting
    then handed the pair 16.9% of the body while all four trunk bones together
    held 2.1%. The comment this replaced claimed the fix was in. It was in for
    x, and it was never in for z.

    So: no fraction of height anywhere. The arm is the material outboard of the
    crease; the arm run is the stack of creased bands climbing from the
    fingertips; and the joint sits IN the crease at the top of that stack,
    which is the one place on this mesh where arm and body demonstrably meet.

    UPWARD FROM THE TIP, and the direction is load-bearing. Legs crease too,
    and a run allowed to walk down from the fingertips joins the two into one
    limb: measured, that dragged the run from band 21 to band 8 and put the
    left shoulder 9 cm from where it put the right one. The fingertips are the
    far end of an arm in every stance this rigs.

    KNOWN ACCURACY, because a landmark without an error bar is a guess wearing
    a number. Against `bg_human` figures whose shoulder is known by
    construction, at 5 head counts and both stances, this lands within 3.5% of
    body height — 6.1 cm on a 1.75 m figure — and the error is method-limited,
    not sampling-limited: it converges as density rises rather than shrinking.
    On adult proportions it tracks the arm's own radius, because the crease
    runs out at the TOP of the arm while the joint centre sits a radius inside
    it. See tests/adapters/test_rig_known_figure.py for the matrix.
    """
    step = h / float(ARM_BANDS)
    creases, reach = side_creases(points, sgn, lo, h)
    if not creases or not reach:
        return {"why": "no band separates an arm from the torso"}
    tip_band = max(reach, key=lambda b: reach[b])
    if tip_band not in creases:
        return {"why": "the outermost point's band is not creased"}
    run, band = {tip_band}, tip_band
    while True:
        # A crease may vanish for a band or two on a decimated surface without
        # the arm having ended: measured on one character's own 4002-vertex
        # adopt, two bands of the run came up empty and a strict walk stopped
        # at the bicep. It may not vanish for longer, which is what keeps a
        # pair of ears 8 bands up out of the arm.
        ahead = [b for b in range(band + 1, band + 2 + ARM_BRIDGE)
                 if b in creases]
        if not ahead:
            break
        band = ahead[0]
        run.add(band)
    top = max(run)
    arm = []
    for p in points:
        if sgn * p[0] <= 0.0:
            continue
        band = min(ARM_BANDS - 1, int((p[2] - lo) / step))
        if band in run and abs(p[0]) >= creases[band][1]:
            arm.append(p)
    if len(arm) < 8:
        return {"why": "too little material outboard of the creases"}
    tip = max(arm, key=lambda p: sgn * p[0])
    # THE JOINT IS IN THE CREASE, not on either surface of it: between where
    # the body stops and where the arm starts, at the height the crease runs
    # out. y and z come from the arm's own material in the top two bands rather
    # than from the band centre, because a band is 2.7 cm of slack and the arm
    # inside it is not evenly spread through that.
    cap = [p for p in arm
           if min(ARM_BANDS - 1, int((p[2] - lo) / step)) >= top - 1]
    body_edge, arm_edge = creases[top]
    shoulder = _mean(cap)
    shoulder[0] = sgn * (body_edge + arm_edge) * 0.5
    return {"shoulder": shoulder, "tip": list(tip), "torso_w": body_edge,
            "arm_verts": len(arm), "shoulder_z": shoulder[2],
            "armpit_z": lo + step * (min(run) + 0.5)}


def crotch_height(points, lo, h, bands=ARM_BANDS):
    """Where two legs become one body.

    MIDLINE OCCUPANCY, and it is the one question a sparse cloud answers
    without argument. Between the legs there is no material near the body's
    centre line; above the crotch there is. Counting points in a narrow band
    about x=0 is robust exactly where measuring a width is not, and it agrees
    with two other methods on the character this was built against: rays cast
    through the mesh put the leg split at 0.516-0.534, an independent check by
    another agent watched the midline fill between 0.40 and 0.55, and this
    returns 0.506.

    THE WINDOW IS 1% OF BODY HEIGHT AND THAT NUMBER WAS MEASURED, not picked.
    On the dense 23,998-point cloud the answer is 0.506 for every window from
    0.4% to 1.2% — the choice is invisible. On the same character after
    bg_adopt welds it to 4,002 points it reads 0.506 at 1.0% and above, 0.615
    at 0.6%, and 1.326 at 0.4%, because a narrow window on a thinned surface
    stops finding enough points to clear the floor and the search walks up past
    the pelvis into the chest. 1% is where the sparse cloud agrees with the
    dense one. Anything tuned tighter is tuned to a mesh nobody ships.

    REFUSES WHEN THE LEGS TOUCH, and that case is not exotic: bg_human's own
    figure at its default build has thighs wider than their separation
    (hip_x 0.048 of body against thigh_r 0.062), so it has no crotch gap
    anywhere and this correctly declines to invent one. A skirt, a robe or a
    coat does the same thing to a generated character.

    Returns (height or None, why).
    """
    step = h / float(bands)
    if step <= 0.0:
        return None, "the body has no height to band"
    rows = {}
    for p in points:
        rows.setdefault(min(bands - 1, int((p[2] - lo) / step)), []).append(p)
    near = h * MIDLINE
    filled = {band: sum(1 for p in pts if abs(p[0]) < near)
              for band, pts in rows.items()}
    present = sorted(filled)
    # CONSECUTIVE PRESENT BANDS, not consecutive indices. A decimated surface
    # leaves whole bands empty, and treating a hole as "the midline is clear
    # here" restarts the search above the crotch every time one falls in it.
    for index, band in enumerate(present):
        window = present[index:index + MIDLINE_RUN]
        if len(window) < MIDLINE_RUN:
            break
        if all(filled[b] >= MIDLINE_FLOOR for b in window):
            if band == present[0] and filled[band] >= MIDLINE_FLOOR:
                return None, "midline occupied in the lowest band: thighs touch"
            found = lo + step * (band + 0.5)
            where = (found - lo) / h
            if not CROTCH_BAND[0] <= where <= CROTCH_BAND[1]:
                return None, ("midline fills at %.0f%% of height, not a "
                              "crotch (a calf)" % (100 * where))
            return found, ""
    return None, "the midline never fills: no two legs become one"


def neck_height(points, lo, h, above, bands=ARM_BANDS):
    """The narrowest place between the shoulders and the skull.

    ABOVE THE ARMS THERE IS NOTHING BUT NECK AND HEAD, so the body's own reach
    in x is the width of one or the other and no limb contaminates it. The
    profile going up reads narrow, then wide, then narrow again — neck, skull,
    crown — and the neck is the minimum BELOW the skull's maximum. Taking the
    global minimum instead finds the crown, which is a point.

    WHY IT IS WORTH MEASURING RATHER THAN DERIVING. The head chain was first
    hung on the template's own ratio between the shoulder line and the crown,
    and that ratio only holds for a figure whose head is a seventh of it. On a
    4.44-head character the same ratio put the Neck bone 13 cm below the neck
    and left UpperChest owning no vertices at all — the bones bunched at the
    top of the trunk while the Head bone took 21.9% of the body. A ratio
    between two measured anchors is still an assumption about everything
    between them.

    REFUSES when the arms reach into the neck's own bands, which is what a
    T-pose does: the arm spans the shoulder line, its reach swamps the neck's,
    and there is no minimum to find that means anything.

    Returns (height or None, why).
    """
    step = h / float(bands)
    rows = {}
    for p in points:
        band = min(bands - 1, int((p[2] - lo) / step))
        if lo + step * (band + 0.5) <= above:
            continue
        rows.setdefault(band, []).append(abs(p[0]))
    widths = {band: max(xs) for band, xs in rows.items()
              if len(xs) >= ARM_FLOOR}
    if len(widths) < 3:
        return None, "fewer than three bands above the shoulder line"
    # THE SKULL IS LOOKED FOR IN THE TOP OF WHAT IS LEFT, because the widest
    # band above a measured shoulder line is usually the shoulder itself: the
    # line is a joint, and the deltoid sits above it. On the character this was
    # built against the band just above the shoulder reads 0.283 against a
    # skull of 0.171, so an unrestricted maximum finds the shoulder, leaves
    # nothing below it, and refuses. Every head bg_proportions builds has its
    # widest point above the middle of the head, so the top 65% of the
    # shoulder-to-crown span is a bound that cannot exclude a skull.
    crown = max(lo + step * (b + 0.5) for b in widths)
    floor_z = above + (crown - above) * SKULL_SEARCH
    upper = {b: w for b, w in widths.items()
             if lo + step * (b + 0.5) >= floor_z}
    if not upper:
        return None, "nothing in the top of the shoulder-to-crown span"
    skull = max(upper, key=lambda b: upper[b])
    below = {b: w for b, w in widths.items() if b < skull}
    if not below:
        return None, "widest band above the shoulder is also the lowest"
    band = min(below, key=lambda b: below[b])
    if below[band] >= widths[skull] * 0.92:
        return None, ("narrowest band is %.0f%% of the widest: not a neck"
                      % (100 * below[band] / widths[skull]))
    return lo + step * (band + 0.5), ""


def body_landmarks(points):
    """Where the body actually is, measured off its points instead of assumed.

    THESE ARE NOT THE POINTS IN THE FILE, and it is the most expensive thing to
    re-learn. `bg_adopt` runs FIRST and welds: on the character this was
    written against, 23,998 vertices in the .glb became 4,002 by the time
    `fit_bones` called this. Anything tuned on the file's own density is tuned
    on six times the points it will get — the first build of the crease finder
    worked on the dense mesh and put the shoulder back on the bicep on the
    welded one, because a crease that is solid at 24k has holes in it at 4k.
    Prototype against the post-adopt cloud, not against the .glb.
    """
    zs = [p[2] for p in points]
    lo, hi = min(zs), max(zs)
    h = hi - lo
    half_w = max(abs(p[0]) for p in points)
    out = {"floor": lo, "top": hi, "height": h, "half_width": half_w}
    for side, sgn in SIDES:
        out[side] = arm_landmarks(points, sgn, lo, h)
    # LEGS: below the hip line, split by side; the foot is the lowest cluster.
    hip_z = lo + h * 0.52
    for side, sgn in SIDES:
        leg = [p for p in points if p[2] < hip_z and sgn * p[0] > 0.01]
        if not leg:
            continue
        foot = [p for p in leg if p[2] < lo + h * 0.06]
        centre = _mean(foot) if foot else list(min(leg, key=lambda p: p[2]))
        out.setdefault(side, {})["foot"] = centre
        out[side]["hip_x"] = sum(abs(p[0]) for p in leg) / len(leg) * 0.55
    out["trunk"] = trunk_anchors(points, out)
    return out


def trunk_anchors(points, marks):
    """The two heights the trunk chain hangs between, and whether they are real.

    THE TRUNK USED TO BE ASSUMED END TO END. `fit_bones` moved arms, shoulders,
    legs, feet and toes onto measured landmarks and never touched Hips, Spine,
    Chest, UpperChest, Neck or Head; those came from an idealised 7.5-head
    figure scaled by TOTAL HEIGHT ALONE. Measured on a real character — a
    4.44-head figure 1.75 m tall — that put the crotch 34 cm above the one its
    mesh has, the thigh bones 37.6 cm above their own joint, and the Neck and
    Head bones inside the skull. Every rig gate stayed green, because each of
    them compares the trunk against the same template that placed it.

    So the chain gets hung between two things this module can actually find:
    the crotch below and the shoulder line above, with the crown for the head.
    The template's own proportions are then applied WITHIN that measured span
    (see TRUNK_SPAN), which keeps the canon and drops the assumption that the
    span itself is an adult's.

    EVERY VALUE CARRIES WHETHER IT WAS MEASURED. A landmark that could not be
    found returns `measured: False` with a reason, and the caller is expected
    to fall back and SAY it fell back — a trunk that was assumed must not be
    reportable as a trunk that was measured.
    """
    crotch, why = crotch_height(points, marks["floor"], marks["height"])
    shoulders = [marks[side]["shoulder"][2] for side, _sgn in SIDES
                 if "shoulder" in (marks.get(side) or {})]
    out = {
        "crotch": {"value": crotch, "measured": crotch is not None,
                   "why": why},
        "crown": {"value": marks["top"], "measured": True, "why": ""},
        "shoulder_line": {
            "value": sum(shoulders) / len(shoulders) if shoulders else None,
            "measured": bool(shoulders),
            "sides": len(shoulders),
            "why": "" if shoulders else "neither shoulder was measured",
        },
    }
    for name, row in out.items():
        low, high = LANDMARK_ENVELOPE.get(name, (0.0, 0.0))
        row["envelope"] = [low, high]
        row["worst_error"] = max(abs(low), abs(high))
    if out["shoulder_line"]["measured"]:
        neck, neck_why = neck_height(points, marks["floor"], marks["height"],
                                     out["shoulder_line"]["value"])
    else:
        neck, neck_why = None, "no shoulder line to look above"
    low, high = LANDMARK_ENVELOPE["neck_base"]
    out["neck_base"] = {"value": neck, "measured": neck is not None,
                        "why": neck_why, "envelope": [low, high],
                        "worst_error": max(abs(low), abs(high))}
    usable = out["crotch"]["measured"] and out["shoulder_line"]["measured"]
    if usable and out["shoulder_line"]["value"] <= crotch:
        usable = False
        out["shoulder_line"]["why"] = (
            "the shoulder reads at or below the crotch: not a body")
    out["fitted"] = usable
    return out


# THE ONLY AXES A CHARACTER MAY LEGITIMATELY VARY ALONG, and the list is not
# ours to extend: bg_proportions exposes height, heads, build, limbs and
# shoulders, and that signature is this project's own declaration of what a
# character can be. Variation ALONG these is style. Variation OFF them is a fit
# fault, which is the only thing a rig gate should ever fire on.
#
# TWO OF THE FIVE ARE DELIBERATELY NOT DERIVED, and the reasons are different:
#
#   build      changes no length this is ever compared against. Read
#              bg_proportions: every height (shoulder, chest, waist, crotch,
#              knee, ankle) and every limb length (upper_arm_l, lower_arm_l,
#              hand_l, foot_l) is a fraction of chin height with no `build`
#              term. It scales radii and half-widths only. Deriving it would
#              feed noise into a comparison it cannot move.
#
#   shoulders  changes exactly ONE compared length — the Shoulder bone, whose
#              tail is the arm pivot at shoulder_x = 0.095 * body * shoulders.
#              Every other arm length is a difference between two points that
#              both shift with it. Deriving `shoulders` would therefore be
#              fitting a parameter to the single row it decides, which is
#              solving, not deriving. It stays at 1.0 and the Shoulder rows
#              stay honest.
DERIVED_AXES = ("height", "heads", "limbs")
FIXED_AXES = {"build": 1.0, "shoulders": 1.0}

# bg_proportions' own constants, needed to invert its arithmetic. Kept here as
# a quotation with the source named rather than re-derived from taste.
CANON_ANKLE = 0.045     # ankle_z as a fraction of chin height
CANON_CROTCH = 0.577    # crotch_z at limbs=1.0, same units
LIMBS_CLAMP = (0.4, 1.6)
HEADS_CLAMP = (1.06, 12.0)


def derive_parameters(marks, heads_pin=None):
    """The character's own proportions, DERIVED from independent measurements.

    NEVER SOLVED FOR. Every value here comes from a landmark this module
    measured off the mesh — the crown, the neck base, the crotch — and none of
    them from whatever number would minimise a gate's deviation. That
    distinction is the whole safety property: a parameter fitted to make a gate
    pass is a gate that passes everything, and it would look exactly like
    success.

    The structural guarantee that makes it hold is worth stating plainly. These
    parameters are derived from the MESH. The gate that uses them compares
    BONES. So moving a bone — the whole of the damage a fit fault is — cannot
    move the reference it is judged against, and no amount of breaking a
    skeleton can talk this into calling it a different body.

    heads_pin, when given, beats the measurement. A project whose character IS
    adult canon says so and gets the strict comparison; the default is measure.

    Each value carries what it was derived FROM and its own error, because two
    of the three inherit a landmark's bar. Returns a dict of rows plus
    `derived` / `from_measurements` counts for the report.
    """
    height = marks.get("height") or 0.0
    crown = marks.get("top")
    trunk = marks.get("trunk") or {}
    neck = trunk.get("neck_base") or {}
    crotch = trunk.get("crotch") or {}
    out = {"height": {"value": height, "measured": height > 0.0,
                      "from": "crown minus floor", "why": ""}}

    if heads_pin:
        out["heads"] = {"value": float(heads_pin), "measured": False,
                        "from": "pinned by the project", "why": ""}
    elif neck.get("measured") and crown is not None:
        head_len = crown - neck["value"]
        count = height / head_len if head_len > 1e-6 else 0.0
        inside = HEADS_CLAMP[0] < count <= HEADS_CLAMP[1]
        out["heads"] = {
            "value": count if inside else None, "measured": inside,
            "from": "total height over neck-base-to-crown",
            "why": "" if inside else
                   "%.2f heads, which bg_proportions will not build" % count}
    else:
        out["heads"] = {"value": None, "measured": False,
                        "from": "neck base", "why": neck.get("why")
                        or "no neck base was measured"}

    # limbs inverts bg_proportions' own leg arithmetic:
    #   crotch = ankle0 + (crotch0 - ankle0) * limbs, both as fractions of body
    if out["heads"].get("value") and crotch.get("measured"):
        body = height - height / out["heads"]["value"]
        floor_z = marks.get("floor") or 0.0
        reach = (CANON_CROTCH - CANON_ANKLE) * body
        ratio = (((crotch["value"] - floor_z) - CANON_ANKLE * body) / reach
                 if reach > 1e-9 else 0.0)
        inside = LIMBS_CLAMP[0] <= ratio <= LIMBS_CLAMP[1]
        out["limbs"] = {
            "value": ratio if inside else None, "measured": inside,
            "from": "measured crotch height against the canon's leg span",
            "why": "" if inside else
                   "limbs=%.2f, outside %.1f-%.1f" % (ratio, *LIMBS_CLAMP)}
    else:
        out["limbs"] = {"value": None, "measured": False,
                        "from": "measured crotch and head count",
                        "why": crotch.get("why") or out["heads"]["why"]
                        or "no crotch was measured"}

    for axis, value in FIXED_AXES.items():
        out[axis] = {"value": value, "measured": False,
                     "from": "NOT derived, held at the canon's default",
                     "why": "changes no length this gate compares"
                            if axis == "build" else
                            "changes only the one row it would be fitted to"}
    out["derived"] = sum(1 for a in DERIVED_AXES if out[a]["measured"])
    out["from_measurements"] = sum(
        1 for row in (marks.get("trunk") or {}).values()
        if isinstance(row, dict) and row.get("measured"))
    out["complete"] = all(out[a]["measured"] or out[a].get("value")
                          for a in DERIVED_AXES)
    return out
