"""Generate the floor cast's animation frames ONE FRAME PER CALL, and stitch
each set back into the grid sheet that scripts/slice_floor_cast.py already eats.

WHY THIS EXISTS BESIDE gen_floor_cast_anims.py. That script buys a whole cycle as
one grid image, which is cheap and fails about a third of the time in a way no
check can catch: handed a grid to fill and a reference that is itself a six-pose
model sheet, the model sometimes redraws the MODEL SHEET. One walk came back with
a correct top row and a VR headset, an arcade cabinet, the words "game over" and
a trophy along the bottom. Its own docstring names the fix and says to reach for
it when a sheet fails twice - one generation per frame, each anchored on the same
reference, stitched afterwards. Seven sheets have now failed, sixteen more were
never bought at all, and both of the providers that serve the grid path are shut:
krea's API balance is spent and kie answers "unusual account activity" on both
its job and upload endpoints. So this is that fix, on the third leg, openai.

WHY ONE FRAME PER CALL IS THE RULE AND NOT A PREFERENCE. It is enforced in the
adapter - bgate_adapters.imagegen._reject_multi_pose refuses a prompt that asks
for a sheet, a row or a count of frames - because a multi-pose generation is
where the character comes apart. Nothing in here asks for more than one drawing,
which is also why none of the prompts below may say "sprite sheet", "frames" or
"poses": the guard reads the prompt, not the intent.

THE CYCLE IS ANCHORED TWICE, AND THE SECOND ANCHOR IS THE POINT. Frame 1 of an
animation is drawn against the character's model sheet alone. Every LATER frame
is drawn against the model sheet AND against frame 1, because independent
generations of "the same person breathing" agree on the person and disagree about
everything the cycle is made of - which way the body is turned, where the weight
is, how far away the camera is. Anchoring the tail of the cycle on its own first
frame is what makes the set a cycle rather than a set. It costs one round trip of
latency per animation and nothing per frame.

REGISTRATION IS STILL THE SLICER'S JOB. Frames arrive at slightly different
scales and offsets no matter how the prompt is worded; slice_floor_cast.py
already scales each frame's ink to a measured target height and pins its FEET to
one baseline, so this script does not try to solve on the prompt what is solved
downstream by measurement.

THE OUTPUT IS A GRID SHEET, NOT A STRIP. It writes exactly the file the grid
generator would have written, at the grid in slice_floor_cast.GRID, so the slicer
is unchanged and a sheet from either source is cut by the same code. The frames
are pasted at a common size onto a flat field of the SAME navy the prompts ask
for, so the slicer's per-cell flood keys them out the way it keys a bought sheet.

EVERY ANIMATION IS EIGHT FRAMES NOW, AND THAT IS THE POINT OF THIS PASS. The
previous cast shipped walk 8, idle 6, sitting 4, working 2 and handoff 1, which
reads as a budget being rationed and was really three shut providers: the grid
generator only ever landed 25 of its 45 sheets, and the rest fell back to cells
cut out of the six-pose model sheet. Two frames of typing is a flicker rather
than an animation. kie is funded again, so the counts here are what the motion
needs and not what survived: eight is enough to carry a breath through its top
and bottom without the turn reading as a jump, and it is the count the repo's
other cast (frontend/public/img/agents) already uses.

THE PROVIDER IS kie AND IT IS CALLED DIRECTLY, NOT THROUGH chroma. That is a
correctness fix, not a preference. chroma.generate appends the project's art
direction whenever it is handed a `root`, and bg-testbed's bible says "angled 3/4
isometric view, 2:1 tile geometry, never flat top-down" - which CONTRADICTS the
70 to 75 degree camera this cast is drawn to and that the floor pane
counter-rotates by. Every kie sheet the grid generator bought carried that
contradiction in its prompt. Going straight at the adapter is what keeps the
camera clause the only camera clause in the prompt; `root` is still passed, so
the spend still lands in the ledger.

THE REFERENCE UPLOAD IS CACHED, because kie takes reference images as URLs
rather than inline bytes. A model sheet re-uploaded once per frame would be 360
uploads of nine files, each with its own three-day expiry, for no gain. It is
uploaded once per character per run and the URL is reused by every frame that
anchors on it.

Run: python scripts/gen_floor_cast_frames.py [--provider kie|openai] [name|anim|name-anim ...]
No arguments means every sheet that is missing. A sheet that already exists is
skipped; delete it to force a regeneration. Individual FRAMES are skipped the
same way, so a run that died halfway resumes for the price of what it missed.
"""
from __future__ import annotations

import concurrent.futures as futures
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from bgate_adapters import imagegen, kie  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _floorpaths import sandbox, FLOOR_IMG  # noqa: E402

ROOT = sandbox()
CAST = ROOT / ".bgate_out" / "art" / "cast"
OUT = CAST / "anim"
# ROOT is the ART project (bg-testbed); the rooms are art installed into the
# HARNESS checkout, which is this file's own repo.
ROOMS = FLOOR_IMG / "rooms"
FRAMES = CAST / "frames"          # the single drawings, kept: they are the
                                  # expensive part and a restitch must be free

# The same cast description gen_floor_cast_anims.py uses, and it has to STAY the
# same: it is what stops the model swapping a jacket colour and still believing
# it obeyed the reference.
# WHICH WAY A WORKING FIGURE IS TURNED, AND WHY IT IS A PROMPT PROBLEM.
#
# The user marked every seat's station on the rendered floor with a circle and
# drew an arrow for what it should be looking at. Four of those arrows point UP
# - the audio console, the QA bench, the art canvas and the video edit bay are
# all NORTH of where their seat stands - and two point DOWN.
#
# Mirroring cannot express that. The sprite is one drawing turned three-quarters
# towards the camera, and a horizontal flip buys left and right and nothing
# else, so every seat came back working with its back to the thing it was
# working on. That is the first thing a reader notices and no amount of
# repositioning fixes it.
#
# So the orientation is DRAWN. An arrow pointing away from the camera means the
# figure is seen FROM BEHIND, over its shoulder, which is what a person bent
# over a desk on the far side of a room actually looks like from a camera
# angled down at seventy-odd degrees. An arrow pointing towards the camera
# means the figure faces the viewer, which is the stock orientation.
#
# art IS DELIBERATELY ABSENT. Its painter was generated front-on, the user
# looked at it and kept it, and re-cutting art to satisfy a rule it is already
# excepted from would be spending to make something worse.
ORIENT = {
    "audio": "seen FROM BEHIND, its back to the viewer and its head bent to the "
             "work in front of it, so the face is hidden and the shoulders and "
             "the back of the head are what the camera sees",
    "qa": "seen FROM BEHIND, its back to the viewer and its head bent to the "
          "work in front of it, so the face is hidden",
    "cinematic": "seen FROM BEHIND and turned slightly to ITS OWN LEFT, its back "
                 "to the viewer, so the camera sees its shoulders and the back "
                 "of its head",
    "gameplay": "seen FROM BEHIND and turned slightly to ITS OWN LEFT, its back "
                "to the viewer, slouched low",
    "tech": "seen FROM BEHIND and turned slightly to ITS OWN LEFT, its back to "
            "the viewer, bent over the bench in front of it",
    "narrative": "FACING THE VIEWER, turned three-quarters towards the camera, "
                 "head bent to the work in front of it",
    "director": "FACING THE VIEWER, turned three-quarters towards the camera",
}


WHO = {
    "art": "a woman with black hair tied up in a bun with a pink hair tie, a "
           "pink long-sleeved top, a paint-spattered cream apron, dark blue "
           "jeans and dark boots",
    "audio": "a dark-skinned man wearing large mint-green over-ear headphones, "
             "a mint-green zip jacket over a dark shirt, dark grey trousers and "
             "dark shoes",
    "narrative": "a woman with long straight dark hair and glasses, in a lilac "
                 "purple open coat over a grey scarf and top, dark trousers and "
                 "brown boots",
    "gameplay": "a man in a red-orange baseball cap worn forwards, a red "
                "quilted body-warmer over a dark long-sleeved top, grey jeans "
                "and dark shoes",
    "qa": "a person with clear goggles pushed up onto the forehead, a black "
          "jacket under a hi-vis yellow-green safety vest with a small badge, "
          "grey trousers and dark boots",
    "cinematic": "a bearded man in an orange knitted beanie and glasses, an "
                 "amber-orange padded jacket, dark trousers and brown boots",
    "tech": "a person in a blue hooded top with the hood up over a blue jacket, "
            "blue jeans and dark shoes, with a backpack strap over the shoulder",
    "director": "an anthropomorphic orange tabby CAT standing upright on two "
                "legs, in a mustard-gold suit jacket, dark trousers and dark "
                "shoes, with a long striped tail",
    "generic": "a man with short brown hair in a plain grey polo shirt with a "
               "lanyard badge, dark trousers and dark shoes",
}

# WHAT THE CHARACTER IS DOING IN EACH SINGLE DRAWING.
#
# The grid must match slice_floor_cast.GRID or the slicer cuts the sheet on the
# wrong boundaries - it reads the grid from its own table, not from the file, so
# a mismatch here is silent and produces sliced heads.
#
# `stance` is the sentence every frame of the animation repeats, and it is what
# holds the set together: it names the camera-facing, the chair, the desk and the
# page, so a frame cannot quietly wander into a different scene. `beats` is the
# one clause that differs, and each one is deliberately SMALL - a breath is two
# or three pixels of chest, and a prompt that asks for a big change gets a
# different drawing rather than the next frame of the same one.
ANIMS: dict[str, dict] = {
    "idle": {
        "grid": (4, 2),
        "size": "1024x1536",
        "stance": ("standing upright and still, facing the viewer, both feet "
                   "flat on the ground and level with each other, arms hanging "
                   "relaxed at the sides"),
        "beats": [
            "at rest, the chest neutral and the shoulders level",
            "beginning to breathe in: the chest has just started to fill and "
            "the shoulders have barely begun to rise",
            "breathing in: the chest and the shoulders have lifted very "
            "slightly and the head sits a little higher",
            "near the top of the breath: the chest is almost at its fullest and "
            "the shoulders are almost at their highest",
            "at the top of the breath: the chest is at its fullest and the "
            "shoulders are at their highest",
            "beginning to breathe out: the shoulders have started to drop back "
            "down from their highest",
            "breathing out: the chest is settling and the shoulders are low",
            "at the bottom of the breath: the shoulders are at their lowest and "
            "the chest is at its emptiest, about to rise again",
        ],
    },
    "sitting": {
        "grid": (4, 2),
        "size": "1024x1024",
        "stance": ("seated on a plain dark office swivel chair with a five-star "
                   "base on castors, turned three-quarters towards the viewer, "
                   "both hands resting in the lap, both feet on the ground. "
                   "There is no desk and no table anywhere in the picture"),
        "beats": [
            "settled in the chair at rest, the torso neutral",
            "beginning to breathe in: the torso has just started to lift off "
            "the chair back",
            "breathing in: the torso has lifted very slightly out of the chair "
            "back",
            "near the top of the breath: the chest is almost full and the head "
            "has almost finished rising",
            "at the top of the breath: the chest is fullest and the head sits a "
            "little higher",
            "beginning to breathe out: the chest has just started to empty and "
            "the shoulders are dropping",
            "breathing out: the torso is settling back down towards neutral",
            "at the bottom of the breath: the torso has settled fully into the "
            "chair back, about to rise again",
        ],
    },
    "walk": {
        "grid": (4, 2),
        "size": "1024x1536",
        "stance": ("walking on the spot towards the viewer's LEFT, the body "
                   "turned three-quarters towards the viewer, the arms swinging "
                   "opposite the legs"),
        "beats": [
            "contact: the LEFT leg is forward with the heel just landing and "
            "the RIGHT leg is stretched back with the toe still down; the right "
            "arm is forward and the left arm is back",
            "down: the weight has sunk onto the front leg, that knee is bent "
            "and the whole body is at its LOWEST point of the stride",
            "passing: the rear leg has swung through directly under the body "
            "and the legs are close together; the body is rising",
            "up: the body is at its HIGHEST point, pushing off the back toe, "
            "the back leg straight behind",
            "contact on the other side: the RIGHT leg is forward with the heel "
            "just landing and the LEFT leg is stretched back with the toe still "
            "down; the left arm is forward and the right arm is back",
            "down on the other side: the weight has sunk onto the forward right "
            "leg and the body is at its LOWEST point again",
            "passing on the other side: the other leg has swung through under "
            "the body, the legs close together, the body rising",
            "up on the other side: the body is at its HIGHEST point again, "
            "pushing off the back toe",
        ],
    },
    "working": {
        "grid": (4, 2),
        "size": "1024x1024",
        "stance": ("seated on a plain dark office swivel chair, turned "
                   "three-quarters towards the viewer, both hands raised in "
                   "front of the body at waist height as if typing on a "
                   "keyboard that is not drawn. There is NO desk, NO table, NO "
                   "keyboard and NO monitor anywhere in the picture - only the "
                   "character and the chair"),
        "beats": [
            "the LEFT hand is down at the bottom of its stroke and the RIGHT "
            "hand is raised above it",
            "the hands are crossing: the left is lifting off the keys and the "
            "right is dropping towards them",
            "the RIGHT hand is down at the bottom of its stroke and the LEFT "
            "hand is raised above it",
            "the hands are crossing back and the torso leans in very slightly",
            "the LEFT hand is down at the bottom of its stroke again and the "
            "RIGHT hand is raised higher than before",
            "the hands are crossing and the head dips very slightly towards "
            "the work",
            "the RIGHT hand is down at the bottom of its stroke again and the "
            "LEFT hand is raised above it",
            "the hands are crossing back and the torso is returning to upright",
        ],
    },
    "handoff": {
        "grid": (4, 2),
        "size": "1024x1536",
        "stance": ("standing upright facing the viewer, both feet flat on the "
                   "ground, holding one small plain WHITE sheet of paper. The "
                   "page is the same size and the same flat white every time"),
        # EIGHT, THOUGH THE BRIEF ONLY ASKS FOR FOUR. The extra frames all go
        # into the TRAVEL, not into the hold: the arm coming up is the part a
        # reader watches, and four frames of it is the difference between an arm
        # that lifts and a page that teleports to chest height. The last two are
        # the settled hold, which is where the stylesheet parks the animation
        # when it plays this once and stops.
        "beats": [
            "the page is held down at the side, the arm straight and relaxed",
            "the page has just left the side, that elbow beginning to bend",
            "the page is being raised, the elbow bent, the body beginning to "
            "turn towards the viewer",
            "the page is up at waist height, the forearm swinging forward, the "
            "body turned further towards the viewer",
            "the arm is unfolding forward and the page is up at chest height "
            "but not yet reaching out",
            "the arm is extended forward and the page is offered out at chest "
            "height towards the viewer",
            "the arm is at full reach, the page held out towards the viewer, "
            "the weight settling onto the front foot",
            "the arm is still extended and the page is still held out at chest "
            "height, the weight settled, waiting",
        ],
    },
}

# WHAT EACH SEAT IS ACTUALLY DOING WHEN IT IS WORKING.
#
# ONE GENERIC `working` FOR NINE SEATS WAS THE BUG. Every character on the floor
# mimed the same keyboard, so an agent in the art room and an agent in the audio
# room were the same drawing in different clothes, and the one thing the floor
# pane exists to show - which craft is busy - was the one thing the sprite did
# not say. The rooms are already drawn per craft; the cast was not.
#
# THESE OVERLAY ANIMS["working"], they do not replace the table. `grid` is not
# restated - it comes from ANIMS and therefore still agrees with the sidecar the
# stitch writes - and a seat with no entry here (generic) keeps the typing that
# was always right for it.
#
# THE PROP IS HELD; THE FURNITURE IS NOT DRAWN. The easel, the mixing console,
# the desk, the workbench and the camera on its tripod are already painted into
# the room art at the coordinate the sprite stands on, so a second copy drawn
# into the sprite sits on top of the real one - qa-working was rejected on the
# last pass for exactly that ("drew its own desk into every cell"). What the
# character can carry off the floor with them - a brush, a pen, a controller, a
# handheld, a sheaf of pages - is drawn, because that is on their person. What
# they cannot is mimed, which is what the stock `working` stance already did
# with its keyboard.
#
# ONE PROP PER SEAT AND IT NEVER CHANGES. cinematic-working was rejected on the
# last pass because its prop changed between cells - empty hands, a loose page,
# a closed book, an open book - and stepped, the book appeared and disappeared
# in his hands. Every stance below names exactly one object and every beat
# leaves it in the same hand.
#
# THE AMPLITUDES ARE THE SAME ONES THE STOCK BEATS USE, for the same reason: the
# floor draws this cell about 59px tall, so a brush stroke is a few pixels of
# hand travel and a shoulder shift. A beat that asks for a swing gets a
# different drawing rather than the next frame of the same one.
NO_FURNITURE = ("There is NO desk, NO table, NO bench, NO easel, NO canvas, NO "
                "monitor, NO screen and NO furniture of any kind anywhere in "
                "the picture - only the character")
WORKING: dict[str, dict] = {
    "art": {
        # PORTRAIT, because this pose STANDS. The stock working size is square
        # for a seated body; a standing painter in a square frame comes back
        # smaller to fit, and the slicer then scales it up from less ink.
        "size": "1024x1536",
        "stance": ("standing upright at work, the body turned three-quarters "
                   "towards the viewer, both feet flat on the ground, one arm "
                   "raised in front of the chest holding a single small "
                   "paintbrush, the other arm bent at the waist holding a "
                   "small oval palette. " + NO_FURNITURE + ", the brush and "
                   "the palette"),
        "beats": [
            "the brush is at the top of its stroke, that arm raised highest "
            "and that shoulder lifted",
            "the brush has started down, the elbow just beginning to open",
            "the brush is halfway down the stroke, the wrist level with the "
            "shoulder",
            "the brush is at the bottom of the stroke, that arm lowest and the "
            "torso leaned in very slightly towards the work",
            "the brush has lifted a little off the work and is beginning to "
            "travel back up",
            "the brush is partway back up, the elbow folding, the head dipped "
            "very slightly towards the work",
            "the brush is near the top of its return, the shoulder rising and "
            "the head coming back up",
            "the brush is back at the top of its stroke, the arm raised, about "
            "to start down again",
        ],
    },
    "audio": {
        "stance": (ORIENT["audio"] + ", " + "seated on a plain dark office swivel chair, turned three-quarter "
                   "s towards the viewer, leaning slightly forward with both hands o "
                   "ut in front at waist height working the faders of a mixing desk  "
                   "that is NOT drawn, wearing over-ear headphones "
                   + ". " + NO_FURNITURE),
        "beats": [
            "both hands level on the faders, the head down to the desk, nodding to the track",
            "the left hand has pushed one fader up a little and the shoulder lifted with it",
            "the left hand holds there while the right hand starts across the desk",
            "the right hand has moved a little to the side and the head follows it",
            "the right hand is at its furthest across, both shoulders square again",
            "the right hand is coming back and the head begins to lift",
            "the head is up, listening, both hands almost back at rest",
            "the head lowers to the desk again and the hands settle level",
        ],
    },
    "narrative": {
        "stance": (ORIENT["narrative"] + ", " + "seated on a plain dark office swivel chair, turned three-quarter "
                   "s towards the viewer, leaning forward over a table that is NOT d "
                   "rawn, one hand holding a SHORT SLIM PEN at waist height and movi "
                   "ng it as if writing, the other forearm resting beside it "
                   + ". " + NO_FURNITURE),
        "beats": [
            "the pen is at the start of the line, the head down to the work",
            "the pen has moved a little along and the wrist has rolled slightly",
            "the pen is halfway along the line, the forearm beginning to open",
            "the pen is near the end of the line, the forearm extended further",
            "the pen is at the end of the line, that hand at its furthest forward",
            "the pen has lifted clear and the hand travels back, the head raised to think",
            "the head is still raised and the hand is nearly back at the start",
            "the pen comes back down and the head lowers to the work again",
        ],
    },
    "gameplay": {
        "stance": (ORIENT["gameplay"] + ", " + "seated low and slouched back as if on a couch, knees forward, tu "
                   "rned three-quarters towards the viewer, both hands together in f "
                   "ront at chest height holding a small game controller, thumbs wor "
                   "king "
                   + ". " + NO_FURNITURE),
        "beats": [
            "slouched back, both thumbs on the sticks, the head level to the screen",
            "the thumbs press in and the shoulders tense very slightly",
            "the whole body leans a little to the left with the action",
            "the lean is at its furthest left, the hands following the tilt",
            "the body is coming back through centre, the thumbs still working",
            "the body leans a little to the right, the head following",
            "the lean is at its furthest right and the shoulders begin to drop",
            "the body settles back to centre and the shoulders relax",
        ],
    },
    "qa": {
        "size": "1024x1536",
        "stance": (ORIENT["qa"] + ", " + "seated on a plain dark office swivel chair, turned three-quarter "
                   "s towards the viewer, leaning forward with both hands out in fro "
                   "nt at waist height typing on a laptop that is NOT drawn, checkin "
                   "g something "
                   + ". " + NO_FURNITURE),
        "beats": [
            "both hands on the keys, the head down to the screen",
            "the right hand taps and lifts, the head still down",
            "the left hand taps in turn and the shoulder dips",
            "both hands pause flat and the head tilts, reading",
            "the head is still tilted, one hand lifted clear",
            "that hand comes back down and the head straightens",
            "both hands typing again, the shoulders square",
            "the hands settle and the head lowers back to the screen",
        ],
    },
    "cinematic": {
        "stance": (ORIENT["cinematic"] + ", " + "seated on a plain dark office swivel chair, turned three-quarter "
                   "s towards the viewer, leaning slightly forward with both hands o "
                   "ut in front at waist height on a keyboard and mouse that are NOT "
                   " drawn, reviewing a cut "
                   + ". " + NO_FURNITURE),
        "beats": [
            "both hands settled and level, the head down to the screen",
            "the left hand presses down and the shoulder dips with it",
            "the left hand rises as the right hand begins to move across",
            "the right hand has travelled a little to the side, the head following",
            "the right hand is at its furthest across and has stopped",
            "the right hand comes back and the head levels",
            "both hands almost at rest, the head lifting to take in the whole cut",
            "the hands settle level and the head lowers to the screen",
        ],
    },
    "tech": {
        "stance": (ORIENT["tech"] + ", " + "standing upright at a workbench that is NOT drawn, the body turn "
                   "ed three-quarters towards the viewer and leaning forward over th "
                   "e work, one hand holding a SMALL HAND TOOL down in front at wais "
                   "t height and the other steadying the piece being worked on "
                   + ". " + NO_FURNITURE),
        "beats": [
            "leaning over the work, the tool down to it, both shoulders forward",
            "the tool hand presses in a little and the head lowers with it",
            "the tool hand eases back and the steadying hand adjusts its grip",
            "the tool has moved a little across the work, the head following",
            "the tool is at its furthest across and has stopped",
            "the tool hand lifts clear and the head rises to inspect",
            "the head is still up, the tool held clear, the other hand turning the piece",
            "the tool comes back down and the head lowers to the work again",
        ],
    },
    "director": {
        "stance": (ORIENT["director"] + ", " + "seated on a high-backed office chair, turned three-quarters towa "
                   "rds the viewer, leaning slightly forward with both hands out in  "
                   "front at waist height on a keyboard that is NOT drawn, occasiona "
                   "lly gesturing while talking "
                   + ". " + NO_FURNITURE),
        "beats": [
            "both hands on the keys, the head level to the screen",
            "the right hand lifts off into a small gesture at chest height",
            "the gesture opens a little further, the head turning with it",
            "the gesture is at its widest, the shoulders open",
            "the hand begins to come back down, the head levelling",
            "the hand is nearly back at the keys, the head returning to the screen",
            "both hands on the keys again, typing",
            "the hands settle level and the shoulders drop",
        ],
    },
}


def spec_for(name: str, anim: str) -> dict:
    """The ANIMS entry for this animation, with this seat's craft laid over it.

    OVERLAY RATHER THAN A SECOND TABLE. `grid` and `size` still come from ANIMS
    unless a seat says otherwise, so the sidecar the stitch writes and the cut
    the slicer makes cannot drift apart per seat - which is the silent failure
    (sliced heads, not an error) the sidecar was added to end.
    """
    spec = ANIMS[anim]
    if anim == "working" and name in WORKING:
        return {**spec, **WORKING[name]}
    return spec


# THE FIXED HALF OF EVERY PROMPT, and both halves of it are load-bearing rather
# than style notes. The CAMERA is the one the cast is already drawn to and the
# one the floor pane counter-rotates the sprites by, so a frame drawn isometric
# would have the camera applied to it twice. The flat navy FIELD is what the
# slicer floods out to alpha, so a gradient, a floor or a cast shadow against a
# wall would key into holes in the character.
#
# It says ONE character and one drawing in as many ways as it can without using
# any of the words the adapter's multi-pose guard refuses: this prompt asking for
# a sheet is the exact failure that put this file here.
TEMPLATE = """A single pixel-art character drawing: ONE character, alone, drawn once, \
filling the picture.

THE CHARACTER IS THE ONE IN THE REFERENCE IMAGE AND MUST NOT CHANGE. It is {who}. \
Same face, same hair, same clothing, same colours, same proportions, same pixel-art rendering, \
same outline weight, same palette as the reference. Do not restyle, do not redesign, do not \
reproportion, do not age. This is the SAME character as the reference, drawn once more.

CAMERA: the identical high three-quarter top-down view used in the reference image, roughly 70 to \
75 degrees, looking down at the character from above and in front. Copy the reference's camera \
exactly. Not a flat overhead plan view, not a straight-on front view, not isometric.

BACKGROUND: one flat solid dark slate-navy fill, RGB 34 44 53, edge to edge. No gradient, no \
vignette, no floor, no tiles, no walls, no props other than the ones named below, no border \
lines, no grid lines, no separators, no panels, no text, no numbers, no labels, no watermark.

FRAMING: the character is horizontally centred, standing on the lower part of the picture with a \
small margin of empty background below the feet and clear empty background on the left and the \
right. Nothing is cropped by any edge.

THE CHARACTER IS {stance}.

IN THIS PARTICULAR DRAWING, {beat}.
"""

# THE SECOND ANCHOR'S SENTENCE, appended for every frame after the first. It
# names what must be identical rather than saying "match the second reference",
# because "match it" is also satisfied by redrawing it.
CYCLE = """
THE SECOND REFERENCE IMAGE IS THE SAME CHARACTER IN THE SAME SITUATION, one moment earlier in the \
same continuous movement. Match it exactly for camera distance, character size in the picture, \
which way the body is turned, where the feet are on the ground, and the lighting. This drawing is \
the SAME moment continued, differing only as described above.
"""

# The navy the prompts ask for. The stitched sheet is laid on it so the slicer's
# per-cell flood has the field it expects even in the margin around a frame that
# came back a different shape.
FIELD = (34, 44, 53)

QUALITY = "high"     # the frames are downsampled to a 128x160 cell and every
                     # pixel of ink that survives is one the reader sees stepped
                     # eight times a second; this is the one knob that buys
                     # cleaner ink, and the budget for this cast is not the
                     # constraint. openai leg only - kie has no quality knob.
TIMEOUT = 600.0
WORKERS = 6
KIE_MODEL = "nano-banana-2"   # the one kie image model that takes reference
                              # images AND enough of them (14) to anchor a frame
                              # on both the model sheet and its own frame 1

# Uploaded reference URLs, keyed by local path. kie takes references as URLs, so
# every anchor has to be POSTed to its file store first; without this memo the
# model sheet would be uploaded once per FRAME - 360 uploads of nine files.
_URLS: dict[str, str] = {}
_URL_LOCK = threading.Lock()


def kie_url(path: str) -> str:
    """The kie URL for a local reference, uploading it at most once per run.

    THE LOCK IS HELD ACROSS THE UPLOAD, not just around the dict. The frames of
    one animation run in parallel and all of them want the same two anchors, so
    a check-then-upload that released between the two would put every worker
    into its own upload of the same file - which is the exact cost this memo
    exists to avoid, and it would also mint several URLs for one anchor.
    """
    with _URL_LOCK:
        if path not in _URLS:
            _URLS[path] = kie.upload_file(path, root=str(ROOT))["url"]
        return _URLS[path]


def frame_path(name: str, anim: str, i: int) -> Path:
    return FRAMES / f"{name}-{anim}-{i + 1}.png"


def anchor_frame(name: str) -> Path | None:
    """This character's own IDLE frame, on the flat field, as a single drawing.

    THE RIGHT ANCHOR WAS ON DISK THE WHOLE TIME. Frame 1 of a working cycle was
    being drawn against the six-pose MODEL SHEET, which settles who the person
    is and settles nothing else - not the camera, not the field, not the scale -
    so every one of those had to be re-argued in words for every seat. Then the
    ROOM was added to argue the facing, and it fixed the facing and painted the
    room in behind the figure, and because frame 1 anchors the cycle the bleed
    reached all eight frames.

    An installed idle frame has all of it already: the same character, the same
    70-75 degree camera, the same pixel scale, the same flat navy field, drawn
    and accepted. Anchoring on THAT leaves the prompt one job - turn them and
    put them to work - which is the job it can actually do.

    Cut from the installed strip rather than the frames directory: the strip is
    what shipped, and the per-frame drawings for idle were bought long enough
    ago that they are not all still on disk.
    """
    strip = FLOOR_IMG / name / "idle.png"
    if not strip.is_file():
        return None
    out = FRAMES / f"{name}-anchor.png"
    if out.exists():
        return out
    im = Image.open(strip).convert("RGBA")
    cell = im.height * 4 // 5          # the strip is 128x160 cells laid across
    one = im.crop((0, 0, cell, im.height))
    flat = Image.new("RGBA", one.size, FIELD + (255,))
    flat.alpha_composite(one)
    out.parent.mkdir(parents=True, exist_ok=True)
    flat.convert("RGB").save(out)
    return out


def one_frame(name: str, anim: str, i: int, refs: list[str],
              provider: str) -> dict:
    """Buy one drawing, unless it is already on disk."""
    spec = spec_for(name, anim)
    out = frame_path(name, anim, i)
    if out.exists():
        return {"ok": True, "skipped": True, "path": str(out)}
    prompt = TEMPLATE.format(who=WHO[name], stance=spec["stance"],
                             beat=spec["beats"][i])
    if len(refs) > 1:
        prompt += CYCLE
    if any(Path(r).name == f"{name}.png" and Path(r).parent.name == "rooms"
           for r in refs):
        prompt += (
            "\n\nThe LAST reference image is the ROOM this character works in, "
            "seen from the same camera. It shows the workstation they are "
            "standing or sitting at. Pose them exactly as somebody using THAT "
            "station would be posed, and turn their body the way that station "
            "requires - if the station is at the TOP of that picture, the "
            "character is working AWAY from the camera and is drawn from "
            "behind. Do not draw the room, the station or any of its furniture "
            "into this picture: only the character."
            # NOT A REPEAT - THE LAST WORD. The room reference does its job on
            # the facing and then keeps going: five of audio's eight frames came
            # back with the studio's floor, wall and orange downlights painted
            # in behind the figure. The slicer keys the background out by
            # flooding a FLAT field, so a frame carrying a room is a frame that
            # cannot be cut. Saying it once at the top was not enough against a
            # reference image that is nothing but room, so the field is restated
            # after the room clause, where it is read last.
            "\n\nTHE BACKGROUND OF THIS PICTURE IS STILL A COMPLETELY FLAT, "
            "PLAIN, EMPTY FIELD OF ONE SINGLE COLOUR, exactly as described "
            "above. Do NOT copy the room's floor, walls, lighting, shadows or "
            "any object from the reference into the background. The reference "
            "is there ONLY to show how the body should be turned. Nothing but "
            "the character and the flat field.")
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if provider == "kie":
        # STRAIGHT AT THE ADAPTER, not through chroma.generate. chroma appends
        # the project's art direction whenever it is given a root, and
        # bg-testbed's bible says "angled 3/4 isometric, never flat top-down" -
        # which argues with the 70 to 75 degree camera TEMPLATE just asked for,
        # in the same prompt. The root still goes to the adapter, so the spend
        # is still on the ledger; it is only the bible that is skipped, and it
        # is skipped because this cast's camera is already settled.
        res = kie.generate_image(prompt, str(out), model=KIE_MODEL,
                                 size=spec["size"],
                                 image_urls=[kie_url(r) for r in refs],
                                 timeout=TIMEOUT, root=str(ROOT),
                                 logical_name=f"{name}-{anim}-{i + 1}")
    else:
        res = imagegen.edit(prompt, refs, str(out), size=spec["size"],
                            quality=QUALITY, timeout=TIMEOUT, root=str(ROOT),
                            logical_name=f"{name}-{anim}-{i + 1}")
    res["seconds"] = round(time.monotonic() - started, 1)
    return res


def stitch(name: str, anim: str) -> bool:
    """Lay the finished drawings into the grid sheet the slicer reads.

    ONE CELL SIZE FOR THE WHOLE SHEET, taken from the largest drawing, with every
    frame centred in its cell on the flat field. The slicer cuts on exact grid
    boundaries and floods each cell from its own border, so a cell that is mostly
    margin is harmless and a cell that is a different SIZE from its neighbours is
    not - which is what a naive paste of mixed-size drawings would produce.
    """
    cols, rows = spec_for(name, anim)["grid"]
    n = cols * rows
    paths = [frame_path(name, anim, i) for i in range(n)]
    if not all(p.exists() for p in paths):
        return False
    ims = [Image.open(p).convert("RGB") for p in paths]
    cw = max(im.size[0] for im in ims)
    ch = max(im.size[1] for im in ims)
    sheet = Image.new("RGB", (cw * cols, ch * rows), FIELD)
    for i, im in enumerate(ims):
        c, r = i % cols, i // cols
        sheet.paste(im, (c * cw + (cw - im.size[0]) // 2,
                         r * ch + (ch - im.size[1]) // 2))
    OUT.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT / f"{name}-{anim}.png")
    # THE GRID TRAVELS WITH THE SHEET. The slicer reads its cut from its own
    # table, so a sheet stitched at one grid and cut at another is a SILENT
    # fault - it produces sliced heads and half-bodies, not an error - and the
    # two tables sat in two files with nothing holding them together. This pass
    # made them disagree for real: the drawings here became eight frames at 4x2
    # while the slicer still cut idle at 3x2, because the sheets already on disk
    # are the old ones. The sidecar ends the argument by letting the sheet state
    # its own shape; the slicer prefers it and falls back to its table for the
    # older sheets that have none.
    (OUT / f"{name}-{anim}.grid").write_text(f"{cols}x{rows}", encoding="utf-8")
    return True


def complete(name: str, anim: str) -> bool:
    """Are all of this animation's drawings already on disk?"""
    return all(frame_path(name, anim, i).exists()
               for i in range(len(spec_for(name, anim)["beats"])))


def main() -> int:
    argv = sys.argv[1:]
    provider = "kie"
    if "--provider" in argv:
        i = argv.index("--provider")
        provider = argv[i + 1]
        del argv[i:i + 2]
    jobs = [(n, a) for n in WHO for a in ANIMS]
    if argv:
        jobs = [(n, a) for n, a in jobs if n in argv or a in argv
                or f"{n}-{a}" in argv]

    # RESTITCH WHAT IS ALREADY BOUGHT, THEN DROP IT. The old test here was
    # whether the SHEET existed, which was wrong in both directions after the
    # frame counts changed: a sheet left over from the six-pose grid generator
    # is the wrong length and would have been kept, and a complete set of
    # drawings whose stitch died would have been bought a second time. The
    # drawings are the expensive artifact, so they decide, and a restitch is
    # free.
    live = []
    for n, a in jobs:
        if complete(n, a):
            stitch(n, a)
            print(f"stitched {n}-{a} from drawings already on disk", flush=True)
        else:
            live.append((n, a))
    jobs = live
    if not jobs:
        print("every drawing is already on disk - nothing to buy")
        return 0

    total = sum(sum(1 for i in range(len(spec_for(n, a)["beats"]))
                    if not frame_path(n, a, i).exists()) for n, a in jobs)
    print(f"{len(jobs)} sheets, {total} drawings to buy on {provider}",
          flush=True)

    bad = 0
    for n, a in jobs:
        sheet_ref = str(CAST / f"{n}-sheet.png")
        # THE ROOM ITSELF IS A REFERENCE, AND NOT USING IT WAS THE MISTAKE.
        #
        # A `working` pose is only correct relative to the thing it is working
        # at, and this script had been told about that thing in prose: "a mixing
        # desk that is NOT drawn", "a laptop that is NOT drawn". The model was
        # being asked to infer a station's height, its distance, and above all
        # WHICH WAY THE BODY FACES, from a sentence - and it got the facing
        # wrong for every seat whose station is north of it, because nothing in
        # the words said the desk was behind the character rather than in front.
        #
        # The room is a finished picture sitting on disk at
        # img/floor/rooms/<seat>.png. Handing it over costs one upload and lets
        # the model see the console, the bench, the couch and the easel it is
        # posing someone at. Only for `working`: idle, walk and handoff happen
        # anywhere on the floor and a room would just pull them towards it.
        room = ROOMS / f"{n}.png"
        # THE ROOM IMAGE IS OFF, AND THE ORIENTATION IS CARRIED BY ORIENT.
        #
        # Handing the room over does fix the facing and it cannot be made to
        # stop bleeding: told six ways not to, the model still paints the room
        # in behind the figure, and because frame 1 is the cycle anchor the
        # bleed then propagates into all eight. Audio came back with a slab of
        # its own equipment rack welded to every frame.
        #
        # ORIENT already says the thing the room was being used to say - "seen
        # FROM BEHIND, its back to the viewer" - in words, which cost nothing
        # and cannot leak a background. Set BGATE_CAST_ROOM_REF=1 to put it back
        # for a seat whose facing words alone cannot settle.
        from os import environ
        want_room = environ.get("BGATE_CAST_ROOM_REF", "") == "1"
        extra = [str(room)] if want_room and a == "working" and room.is_file() else []
        if not Path(sheet_ref).is_file():
            print(f"FAIL {n}: no model sheet to anchor on", flush=True)
            bad += 1
            continue
        # FRAME 1 FIRST, ALONE, because it is the anchor every other frame of
        # this animation is drawn against. Running the whole cycle in parallel
        # off the model sheet alone is what makes a set instead of a cycle.
        # THE ROOM ANCHORS FRAME 1 ONLY, AND THAT IS THE WHOLE TRICK.
        #
        # It is needed to settle which way the body faces, and it costs
        # background: handed a reference that is nothing but room, the model
        # paints the room in behind the figure, and five of audio's eight frames
        # came back on a hazy studio floor instead of the flat field the slicer
        # floods. Frame 1 only pays that once. Every later frame anchors on
        # frame 1 instead, which is ALREADY turned the right way - the cycle
        # anchor this script was built around was always the thing carrying
        # orientation forward, so the room has nothing left to add after it.
        # The character's own idle frame if there is one, the model sheet if
        # not. See anchor_frame: a finished frame carries the camera, the scale
        # and the field, and a model sheet carries none of them.
        base = anchor_frame(n) if a == "working" else None
        first = one_frame(n, a, 0, [str(base) if base else sheet_ref] + extra,
                          provider)
        if not first.get("ok"):
            print(f"FAIL {n}-{a} frame 1: {first.get('error')}", flush=True)
            bad += 1
            continue
        print(f"ok   {n}-{a} 1  {first.get('seconds')}s", flush=True)
        refs = [sheet_ref, str(frame_path(n, a, 0))]
        rest = range(1, len(spec_for(n, a)["beats"]))
        with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            fut = {pool.submit(one_frame, n, a, i, refs, provider): i
                   for i in rest}
            for f in futures.as_completed(fut):
                i = fut[f]
                try:
                    r = f.result()
                except Exception as exc:                       # noqa: BLE001
                    bad += 1
                    print(f"FAIL {n}-{a} frame {i + 1}: "
                          f"{type(exc).__name__}: {exc}", flush=True)
                    continue
                if r.get("ok"):
                    print(f"ok   {n}-{a} {i + 1}  {r.get('seconds')}s",
                          flush=True)
                else:
                    bad += 1
                    print(f"FAIL {n}-{a} frame {i + 1}: {r.get('error')}",
                          flush=True)
        if stitch(n, a):
            print(f"SHEET {n}-{a} stitched", flush=True)
        else:
            print(f"-- {n}-{a} incomplete, not stitched", flush=True)
    print(f"done, {bad} drawings failed")
    return 1 if bad else 0


if __name__ == "__main__":
    # THE KEYS ARE LOADED BEFORE ANY THREAD STARTS - the grid generator lost
    # three sheets to a race on exactly this, workers reaching the adapter while
    # the project's .env was still being read.
    from bgate_core.store import envfile
    envfile.load_env(str(ROOT))
    sys.exit(main())
