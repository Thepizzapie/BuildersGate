"""Cut the studio cast's per-animation sheets into one STRIP per animation.

WHAT CHANGED AND WHY. The first version of this script cut a single 3x2 grid of
six POSES into six one-frame PNGs, and the floor pane cross-faded two of them to
suggest a walk. Six stills is not an animation, and the repo already had the
right convention sitting next to it: frontend/public/img/agents is one sheet per
animation and agent-stage.html steps through it with CSS. This does the same for
the floor: one sheet per animation per character, sliced into a horizontal strip
whose length is the motion's rather than the pose grid's: every looping strip is
eight frames and every handoff is four, and no cell of a strip repeats another.

THE FRAME COUNT IS PER ANIMATION AND IT IS NOT A CONSTANT. That is the whole
reason the fixed 3x2 had to go. GRID below is the shape the SOURCE sheets were
bought at, TARGET_FRAMES is the length they are installed at, and the number the
RENDERER needs is written out by this script (see CAST_FRAMES_TS) rather than
typed into the stylesheet a second time - a frame count that lives in two files
is a frame count that will disagree with itself the first time a sheet is
regenerated at a different length.

WHY THERE IS AN ALLOW LIST. These sheets come from an image model, and a model
handed "draw an 8-frame walk cycle in a 4x2 grid" alongside a reference that is
itself a 6-pose model sheet sometimes draws the MODEL SHEET again: the audio
walk's second row is a VR headset, an arcade cabinet, the words "game over" and a
trophy. That is not a bug this script can detect - every frame is a plausible
picture of the right character - so a sheet is not installed until somebody has
looked at it. PASS names the ones that were looked at and were right; RETRY names
the ones that were looked at and were wrong, with the reason, because "regenerate
these four" is the useful form of that knowledge.

AND A SHEET THAT IS NOT IN PASS STILL DRAWS, out of the ORIGINAL model sheet.
Never dropping a live agent because nobody drew it is this pane's standing rule,
so the fallback is a strip cut from the six poses that already exist.

THE FALLBACK IS NOT AUTOMATICALLY ONE FRAME, and that is the part worth reading.
The model sheet is not six unrelated pictures: cells 2 and 3 are the SAME walk
half a stride apart, and cells 4 and 1 are the same seated character with its
hands on the keys and with its hands down. Those are two-frame cycles - the
oldest ones in the medium - and they were being thrown away in favour of a still
because the fallback was hardcoded to one cell per animation. FALLBACK_CELLS says
which cells make each animation, so a character with no bought walk sheet still
MOVES ITS LEGS when it crosses the floor, at no cost and with no new art. Where
the model sheet genuinely holds only one drawing of the pose - a standing breath,
a page held out - that one drawing is still all the DRAWING there is: the strip
is filled out by deforming it along the movement it is in the middle of, never by
alternating it with an unrelated pose, which would animate a flap rather than a
cycle.

EVERY LOOPING STRIP IS EIGHT FRAMES AND EVERY HANDOFF IS FOUR, and no two frames
of a strip are the same picture. That is TARGET_FRAMES, it is the contract the
renderer is graded against, and it is why this pass exists: the previous one
declared eight and then filled the tail of the sheet with repeats. Two ways it
was doing that, both fixed below.

  - A COSINE SAMPLED AT EIGHT POINTS ONLY HAS FIVE VALUES. The breath walked
    (1 - cos)/2, which is symmetric about the top of the breath, so frames 1 and
    7, 2 and 6, 3 and 5 got the SAME chest lift and, after rounding to whole
    pixels, came out byte-identical. Nine of the installed strips were 1024px
    wide and held six distinct drawings. The fix is that a breath now carries a
    second component in QUADRATURE with the first - a pixel or two of lateral
    weight shift, which is antisymmetric where the lift is symmetric - so the
    inhale and the exhale are different pictures, as they are in any hand-drawn
    idle. The two halves of a breath are not the same moment played backwards.

  - A CYCLE SHORTER THAN THE TARGET WAS INSTALLED AT ITS OWN LENGTH. A bought
    six-frame idle stayed six, and the two-frame walk and two-frame typing cut
    out of the model sheet stayed two - which is a flicker, not an animation.
    Those are now stepped up to eight by IN-BETWEENING: the output frame at
    phase p shows the nearest real drawing carrying the part of the motion that
    falls between it and the next one. For a breath that in-between is the chest
    itself, signed, so a frame that lands on the exhale COMPRESSES rather than
    lifting (see `lift`, which takes a signed travel). For a stride and for
    typing it is the body's
    own bounce and lean over the beat, applied as whole-pixel offsets of the
    finished cell (see `beat_cycle`) - which is what secondary animation is at
    this size, and is not the same thing as holding a drawing for two frames.

WHAT IS NOT SYNTHESISED IS THE DRAWING. Nothing below invents a pose. Every
output frame is a real bought or hand-drawn cell with a few pixels of chest,
bounce or sway on it, and where a real cell exists at that phase it is passed
through untouched. A strip that already holds eight distinct drawings - the five
bought walks - is placed and left exactly alone, which `beat_cycle` gets for free
because its in-between amount is zero when the counts match.

AND WHERE THE MODEL SHEET HOLDS ONLY ONE DRAWING, THE CYCLE IS SYNTHESISED. See
`breath_cycle`. A standing breath and a seated breath are not a set of poses,
they are ONE pose deformed by a few pixels of chest - the generator's own
prompts for these two animations say exactly that, beat by beat ("the chest and
the shoulders have lifted very slightly"). So where there is no bought sheet and
the model sheet holds a single standing or seated drawing, the frames are made by lifting that
drawing's chest and head off its own hips, on a cosine, and the strip is a real
cycle rather than a still.

THAT IS NOT THE THING FALLBACK_CELLS REFUSES TO DO, and the difference is the
whole reason it is allowed here. The refusal below is against alternating two
UNRELATED drawings - a standing pose and a page-offering pose - which animates a
character flapping a page in and out of frame. A breath is the same drawing at
two moments of one movement, which is what every hand-drawn idle at this pixel
size is: the chest band is stretched and the head above it is translated, the
hips and the feet do not move, and nothing is invented that the drawing does not
already contain. It is applied ONLY to idle and sitting - see BREATHES - because
those are the two animations that ARE a breath.

THE HANDOFF IS NOT A BREATH, AND IT IS NO LONGER A STILL EITHER. The old
reasoning was half right: it is a raise, an extend and a hold, and no lifting of
the chest produces the raise, so it does not go through `breath_cycle`. But the
extend IS in the drawing. The offering pose reaches an arm and a page out past the
silhouette the same character has standing at rest, and the difference between
those two silhouettes is the travel, measured rather than invented. So
`handoff_cycle` slides exactly that region - the ink outboard of the standing
body, which is the forearm and the page and nothing else - back in along the axis
it came out on, and the four frames are the page coming up out of the body and
reaching. The last frame is the drawing untouched, and the stylesheet plays the
strip once and holds there, so the pose a delivery ends on is still the pose that
was drawn. Three characters had a one-frame handoff and now have four; the six
with a bought four-frame sheet are untouched.

ALL THREE PROVIDERS WERE RE-PROBED ON 2026-08-15 AND ALL THREE ARE STILL SHUT -
openai answers credit_balance_exhausted, krea answers no API credit, kie answers
"unusual account activity" on its credit endpoint. Buying the missing sheets is
the better fix and it is not available; when one of them is funded again,
scripts/gen_floor_cast_frames.py buys them and a bought sheet wins over the
synthesis automatically, because the synthesis only runs on a one-frame result.

AND A SHEET CAN BE PARTLY RIGHT. See SALVAGE: the audio walk came back with a
correct four-frame cycle along its top row and a VR headset, an arcade cabinet,
the words "game over" and a trophy along the bottom. Half a sheet is not a
failure to be replaced later, it is four real frames that were paid for; the
salvage list names the cells that were looked at and were right.

THE BACKGROUND COMES OFF THE SAME WAY IT ALWAYS DID. A flood fill in from each
cell's border, accepting pixels close to the colour sampled at that cell's own
corner - per CELL and not per SHEET, because the model does not paint the same
navy in every cell and one sheet came back with a visibly different block behind
each frame. The ground ellipse is deliberately LEFT IN, for the reason the first
version recorded: a rule wide enough to swallow the shadow also swallowed dark
denim, and the ink outline has enough gaps at this size for the fill to get
inside a trouser leg and eat the character from within.

WHAT IS NEW IN THE KEYING is the despeckle. These sheets have JPEG noise (krea
returns JPEG) and some carry a faint diagonal texture in the field, both of which
survive the flood as loose specks; and a frame that went off-script can carry
free-floating furniture. Dropping every opaque island under a fraction of the
biggest one removes all three. It is deliberately a LOW fraction: the page in the
handoff pose is a separate small island whenever the outline closes around it,
and losing the page would make the delivery unreadable.

REGISTRATION IS THE PART THAT MAKES steps() HONEST. The model was asked to keep
one scale and one baseline across a grid and it does not, quite - cells drift by
a few pixels and sometimes by a few percent of scale. Cut naively, the character
lurches sideways every frame and the "animation" reads as a fault. So every frame
is placed on a common cell: its ink is scaled to a target height, its FEET are
centred horizontally, and its lowest ink sits on one baseline. The feet rather
than the whole ink, because an arm held out to offer a page drags the full-ink
centre sideways and would swing the whole body to compensate.

THE TARGET HEIGHT PER ANIMATION IS MEASURED, NOT GUESSED. A seated character is
genuinely shorter than a standing one, so normalising every animation to one
height would inflate the sitting frames. The ORIGINAL model sheet already holds
that character standing AND sitting at one consistent scale, so the ratio between
them is a fact about the drawing and is read off it.

Run it from anywhere; the paths are absolute.
"""
from __future__ import annotations

import json
import math
import sys
from collections import deque
from pathlib import Path

from PIL import Image

# Where the sheets are generated (the sandbox) and where they are installed.
REPO = Path(__file__).resolve().parent.parent

# WHERE THE SANDBOX IS, ASKED FOR RATHER THAN HARDCODED.
#
# This was an absolute path to one machine's Desktop, which is three separate
# problems in one line: it only ran for the person who wrote it, it put a home
# directory and an account name into a public repository, and the leak test that
# guards against exactly that (tests/test_streamer.py) failed on main because of
# it.
#
# BGATE_CAST_PROJECT is the env var, --project is the flag, and the default is
# a sibling `bg-testbed` beside this checkout - which is where it actually lives
# for the person who wrote it, so the convenience is kept without the address.
def _sandbox() -> Path:
    from os import environ
    asked = environ.get("BGATE_CAST_PROJECT", "").strip()
    if asked:
        return Path(asked).expanduser().resolve()
    return (REPO.parent / "bg-testbed").resolve()


CAST = _sandbox() / ".bgate_out" / "art" / "cast"
ANIM = CAST / "anim"
INSTALL = REPO / "frontend" / "public" / "img" / "floor"
CAST_FRAMES_TS = REPO / "frontend" / "src" / "shell" / "agents" / "castFrames.ts"

NAMES = ["art", "audio", "narrative", "gameplay", "qa", "cinematic", "tech",
         "director", "generic"]

# The grid each animation's sheet was generated as, and therefore the number of
# frames its strip has. (cols, rows).
#
# THIS IS THE FALLBACK NOW, NOT THE AUTHORITY. A sheet stitched by
# scripts/gen_floor_cast_frames.py writes a `.grid` sidecar next to itself, and
# `grid_for` prefers it. The reason is that a grid mismatch between the thing
# that WRITES a sheet and the thing that CUTS it is silent: it does not raise,
# it produces sliced heads and half-bodies that look like bad art. The two
# tables lived in two files with nothing holding them together until the frame
# counts changed and they disagreed for real. The numbers below are the shape of
# the sheets bought by the older grid generator, which carry no sidecar.
GRID = {
    "idle": (3, 2),        # 6 - a breath cycle
    "sitting": (2, 2),     # 4 - a seated breath
    "walk": (4, 2),        # 8 - the full contact/down/passing/up cycle, twice
    "working": (3, 2),     # 6 - hands alternating on the keys
    "handoff": (2, 2),     # 4 - raise, extend, hold
}


# HOW MANY FRAMES EACH INSTALLED STRIP HAS, whatever its source held. This is
# the contract, not an observation: a looping animation is eight frames and a
# handoff is four, for every character, and the strip is stepped up to it by
# in-betweening (see `breath_cycle` and `beat_cycle`) where the drawings that
# were bought or cut are fewer.
#
# SIXTEEN BECAUSE EIGHT WAS VISIBLY CHOPPY, and the arithmetic says where. The
# renderer (floorRender.ANIM_SPEED) runs each cycle over a FIXED duration and
# steps the whole strip across it, so the frame count IS the frame rate:
#
#   idle    2800ms / 8  = 350ms/frame = 2.9fps      / 16 = 5.7fps
#   sitting 3200ms / 8  = 400ms/frame = 2.5fps      / 16 = 5.0fps
#   working 1200ms / 8  = 150ms/frame = 6.7fps      / 16 = 13fps
#   walk     720ms / 8  =  90ms/frame =  11fps      / 16 = 22fps
#
# Idle and sitting were the worst two and they are what most of the floor is
# doing most of the time - a standing agent updating under three times a second
# reads as a stutter rather than as a breath. Doubling the count halves the
# frame time at the same cycle duration.
#
# IT COSTS NO GENERATIONS. The count is not the number of drawings: `cycle_for`
# steps whatever drawings exist up to this contract by in-betweening (see
# `breath_cycle`, `beat_cycle`, `handoff_cycle`), and the five bought eight-frame
# walks are still the only real drawings in their strips - they now carry one
# in-between apiece rather than none.
#
# THE HANDOFF STAYS AT FOUR, AND IT WAS TRIED AT EIGHT FIRST. Every looping
# strip can be stepped up because its in-between is a deformation the drawings
# already contain - a chest, a bounce, a lean. The handoff's in-between is the
# arm and the page SLIDING BACK IN along the axis they came out on, which only
# exists where two consecutive drawings differ in how far the ink reaches past
# the body. Measured on the six bought sheets (`outboard`, per segment, in
# installed-cell pixels):
#
#   audio      8, none,   34, none
#   narrative  6,    3,   33, none
#   gameplay  11,   18,   13, none
#   tech       7, none,   34, none
#   qa         6,   24, none,    9
#   cinematic  7,   14,   14,    7
#
# Four of the six were bought with their last two drawings at the same reach -
# the extend and the hold are the same silhouette - so there is no travel
# between them to put a frame on, and audio and tech have a second such pair in
# the middle. At eight those seats came out with frame 7 identical to frame 6
# and `check_distinct` failed the run, which is the correct outcome: the fix is
# to buy eight handoff drawings, not to invent an arm the sheet does not
# contain. Only cinematic could hold eight, and one seat animating at twice the
# rate of the other eight is worse than all nine agreeing.
#
# It is also the cheapest thing to leave short. The handoff plays ONCE and holds
# on its last frame, so it is on screen for a fraction of the time a breath is.
TARGET_FRAMES = {"idle": 16, "sitting": 16, "walk": 16, "working": 16,
                 "handoff": 4}


def grid_for(name: str, anim: str) -> tuple[int, int]:
    """The grid THIS sheet was written at, from its sidecar where it has one.

    A malformed sidecar falls back rather than raising: the sheet next to it is
    still cuttable at the table's grid, and refusing to draw a live agent
    because a text file is corrupt is the one thing this pane must never do.
    """
    side = ANIM / f"{name}-{anim}.grid"
    try:
        cols, rows = side.read_text(encoding="utf-8").strip().lower().split("x")
        return int(cols), int(rows)
    except Exception:
        return GRID[anim]

# The ORIGINAL six-pose model sheet, which is still the fallback and is also what
# every generated sheet was conditioned on. Cell index within its 3x2 grid.
#
#   0 standing at rest   1 seated, hands in the lap   2 walking, one stride
#   3 walking, the other stride   4 seated, hands up on the keys   5 offering a page
POSE_CELL = {"idle": 0, "sitting": 1, "walk": 2, "working": 4, "handoff": 5}
POSE_GRID = (3, 2)

# WHICH MODEL-SHEET CELLS MAKE THE FALLBACK STRIP FOR EACH ANIMATION, in play
# order. Only listed where the six poses genuinely contain a cycle:
#
#   walk     cells 2 and 3 are one stride apart, which is the two-frame walk
#            every side-scroller shipped for a decade. It matters more than any
#            other entry here because a character SLIDING across the floor with
#            its legs frozen is the one failure a reader spots without looking.
#   working  cell 4 has both hands up on the keys and cell 1 has them down in
#            the lap, at the same seat and the same scale: hands down, hands up,
#            which is what typing is.
#
# idle, sitting and handoff are single cells because the sheet holds exactly one
# drawing of each. Alternating the standing pose with the page-offering pose
# would not be a handoff cycle, it would be a character waving a page in and out
# of frame, and a wrong animation is worse than a still - the still at least
# never claims anything the board did not say.
FALLBACK_CELLS = {
    "idle": (0,),
    "sitting": (1,),
    "walk": (2, 3),
    "working": (4, 1),
    "handoff": (5,),
}

# SHEETS THAT WERE LOOKED AT AND WERE RIGHT. Anything absent falls back to a
# strip cut from the model sheet - see the module docstring.
PASS = {
    "idle": {"art", "audio", "cinematic", "gameplay", "qa", "tech"},
    "sitting": {"cinematic", "narrative", "qa"},
    "walk": {"art", "cinematic", "gameplay", "narrative", "qa"},
    # EVERY SEAT, because every seat's working was REBOUGHT on the craft pass.
    # The three that were here before (audio, gameplay, narrative) were passed
    # for a generic keyboard mime that every seat shared, which was the bug: the
    # rooms are drawn per craft and the cast was not, so the art room and the
    # audio room were the same drawing in different clothes. The typing sheets
    # they were passed for are kept under anim/superseded/ rather than in
    # rejected/ - they were right for what they were asked, and what was asked
    # changed.
    #
    # GENERIC IS IN THE LIST TOO, though its craft did not change - it has no
    # craft, and the stock typing was always right for it. It was bought
    # because raising the loops to sixteen frames left it the last strip
    # in-betweening from the model sheet's two typing cells, and a one-pixel
    # bounce over sixteen phases quantises to the same offset twice (frames 3
    # and 2, 6 and 5, 11 and 10, 14 and 13 came out identical on every run that
    # still fell back). Eight real drawings is the fix that does not involve
    # inflating a motion that is correctly tiny.
    "working": {"art", "audio", "narrative", "gameplay", "qa", "cinematic",
                "tech", "director", "generic"},
    "handoff": {"audio", "cinematic", "gameplay", "narrative", "qa", "tech"},
}

# SHEETS THAT ARE PARTLY RIGHT, and WHICH CELLS of the sheet's own grid to keep,
# in play order. A salvaged sheet is cut at its declared grid like any other and
# then only these cells are kept, so the frames are the ones that were bought and
# the ones that were looked at.
SALVAGE = {
    # The top row is a clean contact/down/passing/up. The bottom row is a VR
    # headset, an arcade cabinet, the words "game over" and a trophy - the model
    # answered the second half of the grid with a poster about the character
    # instead of the rest of its stride. Four real frames beat the one-frame
    # still this was falling back to, and beat the two-frame model-sheet walk
    # the other unbought characters get.
    "audio-walk": (0, 1, 2, 3),
}

# SHEETS THAT WERE LOOKED AT AND WERE WRONG, and what to fix on the retry. Kept
# in the file because the next person to hold a funded API key needs the list,
# and a list of failures that lives in a chat log does not survive the session.
#
# ALL THREE PROVIDERS ARE SHUT as of this pass, which is why the list is still
# here rather than spent: krea answers "no API credit", kie answers "unusual
# account activity" on both its job and its upload endpoint, and openai answers
# "credit_balance_exhausted". scripts/gen_floor_cast_frames.py is the per-frame
# path these sheets should be rebought through when one of them is funded again -
# it is the fix gen_floor_cast_anims.py's own docstring points at for a sheet
# that has failed once, which every entry below now has.
RETRY = {
    "narrative-idle": "drew the six-pose model sheet again instead of a breath "
                      "cycle - two of the six are seated and one is offering a "
                      "page. Looked at again on the second pass and it is not "
                      "salvageable: no four of the six are one continuous "
                      "standing movement",
    "audio-sitting": "frame 2 is a back view, 3 holds a laptop, 4 holds a mug - "
                     "three different scenes, not one cycle. Only frame 1 is "
                     "usable and it is the pose the model sheet already has",
    "gameplay-sitting": "ignored the 2x2 and drew a 3x2 pose sheet. Its two "
                        "seated cells are a rest and a hands-up, which is the "
                        "same pair the model sheet holds, so there is nothing "
                        "here the fallback does not already give",
}

# SHEETS THAT WERE ON THE RETRY LIST AND CAME OFF IT, with what settled it. A
# rejection that is overturned has to say why in the same file that recorded it,
# or the next pass re-rejects it from the same note.
#
# qa-idle was rejected for its trousers flickering "between dark grey and olive"
# between frames. Measured on the keyed cells rather than eyeballed on the JPEG:
# the mean colour of the trouser band is (61,73,61), (62,75,62), (62,75,63),
# (65,79,64), (61,74,62), (61,75,62) - a spread of four levels out of 255, on a
# sprite that is drawn about 48px wide. That is under this sheet's own JPEG
# noise. Six real frames of the right cycle, installed.
#
# cinematic-working and qa-working came off the list on the craft pass, and not
# because the rejections were wrong - they were right, and both were rejected
# for a fault the PROMPT caused rather than the model. cinematic-working's prop
# changed between cells because the old prompt named no prop at all, so each
# cell invented one; qa-working drew its own desk because the old stance said
# "as if typing on a keyboard that is not drawn" and said nothing about the
# desk. The rebought sheets are drawn from per-seat stances that name exactly
# one prop and forbid the furniture by name (gen_floor_cast_frames.WORKING), and
# they are anchored one drawing at a time on frame 1 of their own cycle, which
# is what stops a prop drifting between cells in the first place.

# WHICH SEATS' `working` IS A STANDING POSE. Everything else about the installed
# height is measured off the model sheet, and the model sheet only holds ONE
# working pose: the character seated with its hands on the keys. That was true
# of every seat until the craft pass, and now it is false for three - a painter
# at an easel, a tester at a bench and a camera operator behind a tripod are all
# on their feet. Scaling those to the seated ruler shrinks them by the seated
# ratio (about four fifths), so the character would visibly SHRINK the moment it
# sat down to work in the art room and grow again when it stood up to walk.
# These three are measured against the standing pose instead.
WORKING_STANDS = {"art", "qa", "cinematic"}

# HOW ALIKE TWO CELLS' SILHOUETTES MUST BE TO COUNT AS THE SAME FACING.
#
# Measured, not chosen. Pairwise silhouette IoU across the bought working
# sheets puts same-facing cells at 0.77-0.97 (different arm and shoulder
# positions of one seated 3/4 view) and cross-facing pairs at 0.57-0.76 (a
# front 3/4 against a back 3/4 of the same character). 0.77 is the floor of
# the first band and above the ceiling of the second on every sheet
# measured.
SAME_FACING_IOU = 0.77

# THE INSTALLED CELL. 128x160 is the size the repo's other cast already uses
# (frontend/public/img/agents, 8 frames of 128x160), and matching it means the
# two renderers can be read against each other.
CELL_W, CELL_H = 128, 160
# How tall a STANDING character is inside that cell, in pixels. The rest is
# headroom and a few pixels under the feet, so a frame where the model drew the
# hair a little higher does not clip at the top of the cell.
STAND_H = 132
FOOT_PAD = 5           # pixels of cell left below the lowest ink
# Pixels of guaranteed empty cell on each side. Not decoration: `place` rounds
# the composite to whole pixels, so a scale that fits to the last pixel lands ON
# the edge about half the time, and the renderer steps the strip by a fractional
# device-pixel amount that can round the other way again. Two pixels of nothing
# is what makes a bleed arithmetically impossible rather than usually absent.
#
# IT IS FOUR RATHER THAN TWO BECAUSE THE SWAY LIVES IN IT. `nudge` shifts a
# finished cell by up to MOTION_DX pixels sideways, and a cell fitted to within
# two pixels of its edge has nowhere to be shifted TO - `nudge` would clamp the
# shift away and hand back the frame it started with, which is the duplicate
# this pass exists to remove. Reserving the motion's own budget in the fit is
# what keeps the clamp from ever binding on a frame that needs to move.
MOTION_DX = 2          # the widest lateral offset any cycle below asks for
SIDE_PAD = 2 + MOTION_DX

BG_TOLERANCE = 40      # unchanged from the first version, and load-bearing
SPECK = 0.02           # drop opaque islands under this fraction of the biggest
FOOT_BAND = 0.22       # the bottom slice of the ink whose centre is "the feet"

# WHICH ANIMATIONS ARE A BREATH: how far the chest travels at the top of the
# breath, and how far the body's weight shifts sideways, both in pixels OF THE
# INSTALLED 128x160 CELL. The frame count is TARGET_FRAMES', not this table's -
# a bought sheet and a synthesised one are stepped to the same length.
#
# THE SWAY IS THE HALF THAT MAKES EIGHT FRAMES EIGHT PICTURES. The chest travel
# is symmetric about the top of the breath, so on its own it draws the exhale as
# the inhale played backwards and the strip holds five distinct frames in eight
# cells. The sway runs in QUADRATURE with it - fastest where the chest is
# stationary - so the two halves differ. It is also true of the pose: a person
# standing still for three seconds shifts their weight, and two cell pixels is
# under one screen pixel of it at the size this renders.
#
# THE SEATED SWAY WAS HALF THE STANDING ONE AND COULD NOT STAY THERE. The
# reasoning for one pixel was sound - a seated body is braced against a chair -
# but a one-pixel quadrature term can only ever take three values after whole-
# pixel rounding, and sixteen phases need more resolution than that. Measured on
# the files at TARGET_FRAMES 16, every one of the nine casts came out with
# sitting frames 1 and 0, 8 and 7, 9 and 7, and 15 and 0 byte-identical: the
# chest curve is symmetric about the top of the breath, so the sway is the only
# thing separating the inhale from the exhale, and where it rounded to zero the
# two halves collapsed onto each other. Two pixels gives it five values and all
# sixteen frames come out distinct. It is the same two pixels the standing sway
# uses, whose own note is the argument for why that is still not much: at
# --cell * 2.7 it is under one screen pixel. The seated breath stays smaller
# than the standing one - that part was never the problem.
#
# THE TRAVEL IS DELIBERATELY SMALL. The floor renders the cell at --cell * 2.7,
# about 59px at the default 22px cell, so four cell pixels is between one and
# two pixels on screen - which is what a breath is at this size. Twice this
# reads as a shrug and half of it rounds away to nothing.
#
# THE SEATED BREATH IS SMALLER THAN THE STANDING ONE, and that is a fix rather
# than a taste call: a seated torso is resting against a chair back, so it
# genuinely moves less - and the chair back is drawn at the same height as the
# shoulders, so it sits inside the chest band and rises with it. Three pixels
# of that reads as the whole upper body settling into the seat; four read as
# the furniture breathing too.
#
# walk, working and handoff are absent on purpose: none of the three IS a
# breath. The first two are beats of a body moving and go through `beat_cycle`,
# which bounces and leans the whole figure between the drawings it has; the
# handoff is a gesture with a travel of its own and goes through
# `handoff_cycle`.
BREATHES = {"idle": (4.0, 2.0), "sitting": (3.0, 2.0)}

# THE BOUNCE AND THE LEAN, per animation that is a set of BEATS rather than a
# breath: peak vertical travel and peak lateral travel of the whole figure, in
# installed-cell pixels, applied between one drawn beat and the next.
#
# A WALK BOUNCES BECAUSE A WALK BOUNCES. The body is lowest just after the foot
# lands and highest at the push-off, which is the single thing that separates a
# character walking from a character sliding, and the two-frame stride cut from
# the model sheet has no way to show it. Two pixels of it here is what an
# in-between frame IS: the same contact drawing, dropped onto its own bent knee.
#
# TYPING BARELY MOVES, and that is the point of it being smaller. The beats it
# has are hands up on the keys and hands down; the in-between is a shoulder
# dropping towards the desk, and one pixel of that at a frame every 0.15s reads
# as somebody working rather than as somebody rocking.
BEATS = {"walk": (2.0, 1.0), "working": (1.0, 1.0)}
# Where the body is cut, as fractions down the INK's own bounding box. Above
# SHOULDER is head and neck and it TRANSLATES; between SHOULDER and HIP is the
# chest and it STRETCHES; below HIP is hips, legs and feet, or the chair, and it
# does not move at all - that is what keeps the character standing on the floor
# rather than hovering over it.
BREATH_SHOULDER = 0.30
BREATH_HIP = 0.55


def cells(sheet: Image.Image, cols: int, rows: int):
    """Split into cols x rows on exact boundaries.

    Rounded from the true fraction rather than floor-divided: 1376/4 is exact but
    1264/3 is not, and a floor-divided grid drops the rightmost four pixels of
    every sheet whose width does not divide - which is where a swinging arm is.
    """
    w, h = sheet.size
    for r in range(rows):
        for c in range(cols):
            yield sheet.crop((round(c * w / cols), round(r * h / rows),
                              round((c + 1) * w / cols), round((r + 1) * h / rows)))


def key_cell(cell: Image.Image) -> Image.Image:
    """Flood the field out to alpha 0, sampling the background per CELL."""
    im = cell.convert("RGB")
    w, h = im.size
    px = im.load()
    # The four corners, not one: a cell whose character's hair reaches the top
    # left would otherwise sample the character and key the whole frame away.
    corners = [px[2, 2], px[w - 3, 2], px[2, h - 3], px[w - 3, h - 3]]
    bg = min(corners, key=lambda c: sum(abs(c[i] - sum(x[i] for x in corners) / 4)
                                        for i in range(3)))

    seen = bytearray(w * h)
    q = deque()

    def ok(c):
        # BACKGROUND ONLY. A wider rule that also swallowed the ground shadow was
        # tried and ate the characters' dark denim with it - the ellipse and a
        # navy trouser leg are the same colour to within a few levels, and the
        # ink outline has enough gaps at this pixel size for the fill to get
        # inside. The shadow stays; the floor pane composites it onto its own
        # dark carpet, where it does not show.
        return (abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2])
                < BG_TOLERANCE)

    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y))
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i] or not ok(px[x, y]):
            continue
        seen[i] = 1
        if x > 0:
            q.append((x - 1, y))
        if x < w - 1:
            q.append((x + 1, y))
        if y > 0:
            q.append((x, y - 1))
        if y < h - 1:
            q.append((x, y + 1))

    out = im.convert("RGBA")
    op = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if seen[row + x]:
                op[x, y] = (0, 0, 0, 0)
    return despeckle(out)


def despeckle(im: Image.Image) -> Image.Image:
    """Drop opaque islands far smaller than the biggest one.

    JPEG ringing around the figure, the faint diagonal texture some sheets have
    in the field, and any furniture a frame invented all survive the flood as
    free-floating blobs. SPECK is deliberately small: the offered page is its own
    island whenever the outline closes around the hand, and a floor where the
    handoff carries nothing is a floor where a delivery reads as wandering off.
    """
    w, h = im.size
    a = im.load()
    label = [0] * (w * h)
    sizes = [0]
    nxt = 1
    for sy in range(h):
        for sx in range(w):
            if a[sx, sy][3] == 0 or label[sy * w + sx]:
                continue
            q = deque([(sx, sy)])
            label[sy * w + sx] = nxt
            n = 0
            while q:
                x, y = q.popleft()
                n += 1
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    u, v = x + dx, y + dy
                    if 0 <= u < w and 0 <= v < h and not label[v * w + u] \
                            and a[u, v][3] != 0:
                        label[v * w + u] = nxt
                        q.append((u, v))
            sizes.append(n)
            nxt += 1
    if len(sizes) <= 2:
        return im
    floor = max(sizes) * SPECK
    doomed = {i for i, n in enumerate(sizes) if i and n < floor}
    if not doomed:
        return im
    for y in range(h):
        row = y * w
        for x in range(w):
            if label[row + x] in doomed:
                a[x, y] = (0, 0, 0, 0)
    return im


def foot_centre(im: Image.Image, box) -> float:
    """The x of the character's FEET, not of all its ink.

    An arm extended to offer a page pulls the full-ink centre sideways by a good
    fraction of the body's width. Registering on that would swing the whole
    character across the cell every time it reached out. The bottom band is the
    boots, or the base of the chair, and it is what stands still.
    """
    x0, y0, x1, y1 = box
    band = max(1, int((y1 - y0) * FOOT_BAND))
    a = im.load()
    lo, hi = x1, x0
    for y in range(y1 - band, y1):
        for x in range(x0, x1):
            if a[x, y][3] > 8:
                lo = min(lo, x)
                hi = max(hi, x)
    if hi < lo:                       # nothing in the band; fall back to the ink
        return (x0 + x1) / 2
    return (lo + hi + 1) / 2


def fit_scale(frames: list[Image.Image], wanted: float) -> float:
    """The wanted scale, reduced until no frame overflows the cell.

    THE HEIGHT TARGET IS NOT THE ONLY CONSTRAINT and pretending it was cost the
    Director its page: the handoff pose reaches an arm out sideways, so scaling
    it to the same height as the standing pose pushed the hand past the right
    edge of the 128px cell, where it was cropped. In a strip that is worse than a
    crop - the overflow lands in the NEXT frame, so the character animates with a
    piece of its previous self stuck to it.

    Both half-widths are measured from the FOOT CENTRE rather than from the ink's
    own middle, because the foot centre is what `place` puts on the cell's centre
    line - a frame is only safe if it fits around the point it will be pinned at.

    Height is capped too, against the cell minus the pad the feet stand on. It
    binds far less often, but the frame that would hit it is exactly the frame
    where the model drew the hair or a raised arm well above the rest of the set.
    """
    s = wanted
    for f in frames:
        box = f.getbbox()
        if not box:
            continue
        x0, y0, x1, y1 = box
        cx = foot_centre(f, box)
        reach = max(cx - x0, x1 - cx)
        if reach > 0:
            s = min(s, (CELL_W / 2 - SIDE_PAD) / reach)
        if y1 - y0 > 0:
            s = min(s, (CELL_H - FOOT_PAD) / (y1 - y0))
    return s


def place(frame: Image.Image, scale: float) -> Image.Image:
    """Scale one keyed frame and set it on the common cell, feet registered."""
    box = frame.getbbox()
    if not box:
        return Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    cx = foot_centre(frame, box)
    x0, y0, x1, y1 = box
    w = max(1, round((x1 - x0) * scale))
    h = max(1, round((y1 - y0) * scale))
    # NEAREST, not a smooth filter: these are pixel art and the pane renders them
    # with image-rendering:pixelated. A resample that invents intermediate values
    # puts a soft halo on every edge, and then the flood's clean alpha boundary
    # comes back as a fringe the moment it is drawn over a light room.
    ink = frame.crop(box).resize((w, h), Image.NEAREST)
    cell = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    # The feet land on the cell's centre line and on one baseline. Both are
    # rounded to whole pixels: a half-pixel offset on a nearest-neighbour sprite
    # is a column of the character that shifts and one that does not.
    left = round(CELL_W / 2 - (cx - x0) * scale)
    top = CELL_H - FOOT_PAD - h
    cell.alpha_composite(ink, (left, top))
    return cell


def iround(x: float) -> int:
    """Round half AWAY from zero, which `round` does not.

    Python rounds halves to even, so round(0.5) is 0 and round(1.5) is 2. Every
    number below is a few pixels of travel, so a half that collapses to zero is
    a frame that does not move - and a frame that does not move is a duplicate
    of the one beside it. This is the difference between a one-pixel sway and no
    sway at all.
    """
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def lift(im: Image.Image, y_sh: int, y_hip: int, dy: int) -> Image.Image:
    """One drawing with its chest expanded by `dy` pixels, hips down unmoved.

    Three bands, and the split between them is what makes this a breath rather
    than a bob. Everything below the hip line is copied where it was, so the
    feet stay planted - a whole-body translation would read as the character
    hopping. The chest band is STRETCHED by dy with its bottom pinned to the hip
    line, because a chest filling with air gets taller without its bottom
    moving. The head and neck above it are TRANSLATED by the same dy and not
    stretched, because a head does not change shape when its owner inhales.

    The two moved bands stay contiguous by construction: the stretched chest
    ends up occupying [y_sh - dy, y_hip) and the translated head [0, y_sh - dy),
    which is why this can never open a transparent seam across the torso the way
    lifting the top half as one block would.

    dy IS SIGNED NOW, AND THE NEGATIVE HALF IS NOT DECORATION. A bought breath
    sheet is stepped up to eight frames by asking this for the DIFFERENCE
    between the chest the output frame wants and the chest the nearest bought
    drawing already has - and half of those differences fall on the exhale,
    where the wanted chest is emptier than the drawn one. With dy clamped at
    zero the way it used to be, every one of those frames came back as an
    untouched copy, which is exactly the duplicate this is here to remove. A
    negative dy compresses the chest band and settles the head onto it, which is
    the same movement run the other way.
    """
    if dy == 0:
        return im.copy()
    w, h = im.size
    ch = y_hip - y_sh + dy
    if ch < 1:                        # a chest cannot be compressed to nothing
        return im.copy()
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    # Below the hip: hips, legs, feet, or the chair and its castors.
    out.paste(im.crop((0, y_hip, w, h)), (0, y_hip))
    chest = im.crop((0, y_sh, w, y_hip))
    # NEAREST for the same reason `place` uses it: these are pixel art drawn
    # with a hard ink outline, and a smooth resample here would soften the
    # outline on the stretched band only - so the character's chest would go
    # blurry at the top of every breath and sharp again at the bottom.
    out.alpha_composite(chest.resize((w, ch), Image.NEAREST), (0, y_hip - ch))
    # The head, moved by dy: for a lift, dropping dy rows off the top of the
    # crop (they are empty, guaranteed by the clamp in `chest_span`); for a
    # compression, pasting the whole head that many rows lower. Either way it
    # lands flush on top of the resized chest band and no seam opens.
    if dy > 0:
        out.alpha_composite(im.crop((0, dy, w, y_sh)), (0, 0))
    else:
        out.alpha_composite(im.crop((0, 0, w, y_sh)), (0, -dy))
    return out


def chest_span(pose: Image.Image, travel_px: float):
    """(shoulder line, hip line, how far this drawing's chest may travel).

    The cap is what stops a breath cropping a head: the lift moves the head up
    by taking rows from above the ink, so it can never exceed the empty margin
    the drawing happens to have at the top of its cell, nor the height of the
    chest band itself. A cell where the model drew the character right up to the
    top edge breathes by however much room it has, including not at all.
    """
    box = pose.getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    ink = y1 - y0
    y_sh = y0 + round(ink * BREATH_SHOULDER)
    y_hip = y0 + round(ink * BREATH_HIP)
    cap = min(int(travel_px), y0, max(0, y_hip - y_sh - 1))
    return y_sh, y_hip, cap


def nudge(cell: Image.Image, dx: int, dy: int) -> Image.Image:
    """A finished 128x160 cell, shifted whole, and never off its own edge.

    THE SHIFT IS THE SECONDARY MOTION and it happens HERE rather than on the
    source drawing on purpose. `place` registers every frame's feet on one
    baseline and one centre line, so a translation applied before it is measured
    away again - the registration is the whole reason the strip does not lurch,
    and it cannot tell a deliberate bounce from the model's drift. Shifting
    afterwards is the only place a bounce survives.

    THE CLAMP IS A GUARD, NOT A BUDGET. SIDE_PAD already reserves MOTION_DX
    pixels of empty cell on each side, so this should never bite; if a frame
    turns up whose ink is closer to the edge than that, one pixel of margin is
    kept rather than letting a limb bleed into the neighbouring frame of the
    strip, where it would animate as a piece of the character's previous self.
    """
    box = cell.getbbox()
    if box and dx:
        room_l, room_r = box[0] - 1, CELL_W - box[2] - 1
        dx = max(-room_l, min(room_r, dx))
    if box and dy:
        room_up, room_dn = box[1], CELL_H - box[3]
        dy = max(-room_up, min(room_dn, dy))
    if not dx and not dy:
        return cell
    out = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    out.paste(cell, (dx, dy))
    return out


def breath_cycle(keyed: list[Image.Image], n: int, scale: float,
                 travel: float, sway: float) -> list[Image.Image]:
    """`n` frames of one breath, out of however many drawings there are.

    ONE RULE COVERS BOTH THE BOUGHT SHEET AND THE SINGLE POSE, and that is the
    point of it. The breath is defined as a curve - chest fullness against phase
    - and the output frame at phase p is the drawing NEAREST that phase carrying
    the difference between the chest the curve wants there and the chest that
    drawing was drawn with. Where a drawing sits exactly on its phase the
    difference is zero and the bought frame is passed through untouched; where
    the strip is being stepped up from six drawings to eight, the two frames
    with no drawing of their own are the neighbouring drawing with a few pixels
    of chest on it, in the direction the breath is actually going at that
    moment. A one-drawing fallback is the same code with the drawn chest at
    zero, which is what the single-pose synthesis always did.

    THE SWAY IS WHAT MAKES THE COUNT HONEST. The chest curve is symmetric, so
    frames 1 and 7 want the same chest and, on a single-drawing fallback, would
    be the same picture. The sway is a sine against the chest's cosine, so it is
    equal and OPPOSITE at those two frames. Its amplitude is small enough to be
    under a screen pixel at the size this renders, and it is a real thing a
    standing body does.

    The deformation is done at SOURCE resolution and the sway on the finished
    cell, which is not an inconsistency: a stretched band at source resolution
    duplicates one row in several hundred and the downscale absorbs it, where
    the same stretch on the installed cell would duplicate one row in ten across
    the chest and show as banding. A translation has no such cost at either
    resolution, and on the finished cell it survives `place`.
    """
    k = len(keyed)
    span = chest_span(keyed[0], travel / scale)
    deformed = []
    for i in range(n):
        p = i / n
        want = (1 - math.cos(2 * math.pi * p)) / 2
        idx = int(p * k + 0.5) % k
        drawn = (1 - math.cos(2 * math.pi * idx / k)) / 2 if k > 1 else 0.0
        src = keyed[idx]
        if span is None or span[2] <= 0:
            deformed.append(src.copy())
            continue
        y_sh, y_hip, cap = span if k == 1 else (chest_span(src, travel / scale)
                                                or span)
        deformed.append(lift(src, y_sh, y_hip, iround(cap * (want - drawn))))
    # Re-clamped, not re-measured: a lifted frame is taller than the drawing it
    # came from and `fit_scale` only ever shrinks, so this proves the whole set
    # still fits the cell without undoing the scale the animation was measured
    # at - which is what keeps a breathing character the same height as the same
    # character walking.
    scale = fit_scale(deformed, scale)
    return [nudge(place(f, scale),
                  iround(sway * math.sin(2 * math.pi * i / n)), 0)
            for i, f in enumerate(deformed)]


def beat_cycle(keyed: list[Image.Image], n: int, scale: float,
               bounce: float, lean: float) -> list[Image.Image]:
    """`n` frames out of `k` drawn BEATS, in-between by the body's own bounce.

    A walk and a typing loop are not one pose deformed, they are a short series
    of drawn positions - contact, down, passing, up; hands up, hands down - and
    the honest way to run four of them at eight frames is not to hold each for
    two. What happens between two beats of a walk is that the body drops onto
    the leading knee and rises off the back toe, and what happens between two
    beats of typing is a shoulder dipping towards the desk; both are movements
    of the WHOLE figure, which is exactly what survives being applied to the
    finished cell.

    So the output frame at phase p takes the beat it is in and offsets it by how
    far through that beat it is: a raised cosine down-and-back-up for the
    bounce, a sine for the lean, which are in quadrature and therefore never
    both zero except on the beat itself. Two frames of the same beat land on
    different offsets, so the strip holds `n` different pictures.

    WHERE THE COUNTS ALREADY MATCH THIS DOES NOTHING, and that is deliberate
    rather than a happy accident: every in-between amount is zero when k == n,
    so the five bought eight-frame walks are placed and installed exactly as
    they were cut, with no invented motion on top of drawings that already carry
    their own.
    """
    k = len(keyed)
    out = []
    # WHAT EACH FRAME ALREADY IS, so no two come out the same picture.
    #
    # The offsets are whole pixels (a half-pixel nudge is not a thing a
    # sprite can hold), and the working beat is one pixel of bounce and one
    # of lean - so at sixteen frames the phases either side of a turning
    # point BOTH round to zero and two frames of the same beat come out
    # pixel-identical. That is what check_distinct was reporting on five
    # casts, and it is a rounding artifact rather than a missing drawing:
    # the motion is real, it is just finer than the grid it lands on.
    #
    # So a collision is pushed to the nearest free offset instead of being
    # shipped as a repeat. One pixel, on a strip whose whole vocabulary is
    # one pixel, is the smallest honest way to say "this frame is further
    # through the beat than the last one".
    seen: set[tuple[int, int, int]] = set()
    for i in range(n):
        t = i * k / n
        idx = int(t) % k
        r = t - int(t)
        dy = iround(bounce * (1 - math.cos(2 * math.pi * r)) / 2)
        dx = iround(lean * math.sin(2 * math.pi * r))
        if (idx, dx, dy) in seen:
            # Along the bounce first: it is the axis the eye reads as the
            # body working, where the lean reads as the body turning.
            for ddy, ddx in ((1, 0), (-1, 0), (0, 1), (0, -1),
                             (2, 0), (-2, 0)):
                if (idx, dx + ddx, dy + ddy) not in seen:
                    dx, dy = dx + ddx, dy + ddy
                    break
        seen.add((idx, dx, dy))
        out.append(nudge(place(keyed[idx], scale), dx, dy))
    return out


# THE SMALLEST OUTBOARD INK THAT COUNTS AS A REACH, in installed-cell pixels.
#
# IT IS THREE RATHER THAN THE SIX IT WAS, because what is measured changed. Six
# was calibrated when the only measurement was the whole gesture against the
# standing pose - a whole arm and a page, tens of pixels - and six was safely
# below it while still refusing a page drawn in front of the chest. The handoff
# now measures each drawing against the one BEFORE it, and one step of a
# four-drawing raise is a fraction of that: audio, narrative and tech all came
# out with handoff frames 3 and 2 identical because a real five-pixel step was
# being read as no reach at all. Three is still above the pixel or two of
# silhouette wobble that keying and rescaling leave behind.
MIN_REACH = 3


def outboard(offer: Image.Image, behind: Image.Image):
    """(which side the arm reaches, how far past `behind` it gets, where to cut).

    Both cells are already placed at one scale with their feet on one line, so
    the only way the offering pose differs from the one behind it is ink out
    past the side of that body - the forearm and the page. Everything inboard is
    the same character standing in the same place.

    None where there is nothing measurably outboard: the model drew the page in
    front of the chest, or these two drawings are the same moment twice. The
    caller must not invent a travel there - an arm that is not in the drawing
    cannot be animated out of it.
    """
    ob, bb = offer.getbbox(), behind.getbbox()
    if not ob or not bb:
        return None
    out_r, out_l = ob[2] - bb[2], bb[0] - ob[0]
    if max(out_r, out_l) < MIN_REACH:
        return None
    right = out_r >= out_l
    return right, (out_r if right else out_l), (bb[2] if right else bb[0])


def slide_in(cell: Image.Image, right: bool, split: int, back: int,
             drop: int) -> Image.Image:
    """One placed cell with its outboard arm slid `back` px in and `drop` down.

    The arm goes on TOP of the body, because that is where it is: drawn back
    against the chest it is in front of the character, not behind it.
    """
    out = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    inboard = (0, 0, split, CELL_H) if right else (split, 0, CELL_W, CELL_H)
    arm = (split, 0, CELL_W, CELL_H) if right else (0, 0, split, CELL_H)
    out.alpha_composite(cell.crop(inboard), (inboard[0], 0))
    moved = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    moved.paste(cell.crop(arm), (arm[0] + (-back if right else back), drop))
    out.alpha_composite(moved)
    return out


def handoff_cycle(keyed: list[Image.Image], rest: Image.Image, n: int,
                  scale: float) -> list[Image.Image]:
    """`n` frames of a page being raised and offered, out of `k` drawings.

    IT TAKES THE WHOLE SET NOW, NOT JUST THE FIRST DRAWING, and that is a fix
    rather than a generalisation for its own sake. It used to be handed
    `keyed[0]` because the six bought four-frame handoff sheets never reached it
    - at TARGET_FRAMES 4 they were already long enough and were installed as
    cut. Raising the contract to eight put them through here, and a function
    that reads one drawing would have thrown three PAID drawings away per seat
    and synthesised the whole gesture from the first.

    THE DRAWINGS ARE THE KEYFRAMES OF THE REACH. The gesture is a single
    monotonic extension - raise, extend, hold - so drawn frame j sits at
    extension e_j along the ease below, and an output frame that lands between
    two of them takes the LATER drawing with its outboard ink slid back towards
    where the earlier one has it. Later rather than earlier on purpose: sliding
    ink IN is subtracting reach the drawing already contains, where sliding it
    out would be inventing reach it does not.

    THE TRAVEL IS MEASURED OFF THE TWO SILHOUETTES, not invented. Placed at one
    scale with their feet on one line, the standing pose and the offering pose
    differ in exactly one way: the offering one has ink out past the side of the
    body, and that ink is the forearm and the page. Everything inboard of the
    standing silhouette's edge is the same character standing in the same place.
    So the gesture is that outboard region sliding in along the axis it came out
    on and settling as it goes, and the frames are the page coming up out of the
    body and reaching. The last frame is the drawing untouched, which matters
    because the stylesheet plays this once and HOLDS there: the pose a delivery
    ends on is the pose that was drawn, not something this function made.

    EACH DRAWING IS MEASURED AGAINST THE ONE BEHIND IT IN THE GESTURE - the
    previous drawing, or the standing rest pose for the first - so what slides
    is only the reach that drawing ADDED, never the whole extension. Measuring
    every drawn frame against the rest pose instead would slide the hold frame's
    arm all the way back to the hip to make its in-between, which is a second
    copy of the raise rather than the moment between an extend and a hold.

    THE TRAVEL IS MEASURED OFF THE SILHOUETTES, not invented. See `outboard`.
    The last frame is the last drawing untouched, which matters because the
    stylesheet plays this once and HOLDS there: the pose a delivery ends on is
    the pose that was drawn, not something this function made.

    IT REFUSES RATHER THAN GUESSES. Where a pair has no measurable outboard ink -
    the model drew the page in front of the chest, or two drawings are the same
    moment twice - that segment's in-betweens are the drawing itself rather than
    an invented slide, and where NO pair has any, the caller gets a single frame
    back and installs the still. That is what shipped before this existed and is
    honest about having nothing.
    """
    placed = [place(f, scale) for f in keyed]
    k = len(placed)
    # The measurement basis for each drawing: the one before it, and the
    # standing pose before the first. `rest` is the model sheet's own standing
    # cell, so "the body" is this character's own width and not a constant that
    # would be wrong for the cat in the suit.
    span = [outboard(placed[j], placed[j - 1] if j else place(rest, scale))
            for j in range(k)]
    if all(s is None for s in span):
        return [placed[-1]]
    # Which drawing each output frame is on or approaching, and how far through
    # that drawing's own segment it is. u is in (0, 1]; u == 1 is the drawing
    # itself, untouched, and every segment ends on one.
    phase = []
    for i in range(n):
        p = (i + 1) / n
        j = min(k - 1, max(0, math.ceil(p * k - 1e-9) - 1))
        phase.append((j, p * k - j))

    # HOW FAR THE ARM IS SLID BACK, PER FRAME, AND WHY IT IS NOT JUST THE CURVE.
    # The gesture EASES OUT rather than running at a constant speed: an arm
    # extending decelerates into the offer, and a linear ramp reads as the page
    # being shoved. Squared, which for a single drawing (k == 1, u == p) is
    # exactly the 1 - (1 - p)**2 ease this used before it took a set.
    #
    # BUT AN EASE-OUT SAMPLED ON A WHOLE-PIXEL GRID CRUSHES ITS OWN TAIL. The
    # last two frames of an eight-frame gesture sit at (1/8)**2 and 0 of the
    # travel, which is under half a pixel apart on any reach this cast has, so
    # both rounded to zero and the strip ended on the same picture twice - art,
    # audio, gameplay, narrative and tech all reported handoff frame 7 identical
    # to frame 6. So the rounded travel is walked back from the end of each
    # segment and forced to strictly increase, one pixel at a time, up to the
    # reach that was actually MEASURED and never past it. Inside the tail that
    # is a pixel of real arm per frame instead of none; everywhere else the
    # curve is already more than a pixel apart and the pass changes nothing.
    #
    # WHERE THE MEASURED REACH RUNS OUT, IT STOPS. A segment with fewer pixels
    # of travel than it has frames cannot show a different arm in each of them,
    # and inventing the difference would be drawing an arm the sheet does not
    # contain. The duplicate survives and `check_distinct` names it, which is
    # the honest outcome and the one that says which sheet to rebuy.
    back = [0] * n
    for i in range(n - 1, -1, -1):
        j, u = phase[i]
        s = span[j]
        if s is None:
            continue
        raw = iround(s[1] * (1 - u) ** 2)
        nxt = phase[i + 1] if i + 1 < n else None
        if nxt and nxt[0] == j and raw <= back[i + 1]:
            raw = back[i + 1] + 1
        back[i] = min(raw, s[1])

    # A retracted arm hangs lower than a held-out one, and the drop is tied to
    # HOW FAR THIS FRAME WAS SLID rather than to where it sits in the gesture.
    # That is what keeps a drawn frame untouched: a drawing that is already at
    # its own phase has been slid nowhere, so it is dropped nowhere either, and
    # the paid pose survives into the strip exactly as it was drawn. For a
    # single drawing the two definitions coincide, which is the case this had
    # before it took a set.
    total = sum(s[1] for s in span if s) or 1
    frames = []
    for i in range(n):
        j = phase[i][0]
        s = span[j]
        if s is None or not back[i]:
            frames.append(placed[j])
            continue
        frames.append(slide_in(placed[j], s[0], s[2], back[i],
                               iround(3 * back[i] / total)))
    return frames


def strip(frames: list[Image.Image]) -> Image.Image:
    """One row of uniform cells. This shape is the contract with steps()."""
    out = Image.new("RGBA", (CELL_W * len(frames), CELL_H), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        out.paste(f, (i * CELL_W, 0), f)
    return out


def ink_heights(frames: list[Image.Image]) -> list[int]:
    return [(b[3] - b[1]) for b in (f.getbbox() for f in frames) if b]


def median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def reject_scale_outliers(frames: list[Image.Image], tol: float = 0.4) \
        -> list[Image.Image]:
    """Drop any cell whose ink is wildly off the sheet's own scale.

    A BOUGHT SHEET IS EIGHT DRAWINGS FROM ONE PROMPT, NOT EIGHT DRAWINGS AT
    ONE SCALE - the model draws each cell independently and nothing enforces
    that the character comes out the same size twice. `fit_scale` measures
    ONE scale off the group's median height, so a cell whose own character
    was drawn small is not caught there - it is scaled by the SAME
    multiplier as every other cell and comes out small on screen where the
    others come out right (and the reverse for one drawn big). Six working
    sheets shipped exactly this - every cast but art and audio, most of them
    2-3 of 8 cells at roughly half the others' height - and every one of
    them read on the floor as the character breathing in and out between
    frames.

    DROPPED, not swapped for a neighbour: the first version of this function
    duplicated the nearest good cell into the bad one's slot, which passed
    `fit_scale` clean but failed `check_distinct` on six casts at once - the
    module's own hard rule is that no two frames of a strip are the same
    picture, and a duplicated cell is exactly that picture twice. `beat_cycle`
    derives its phase purely from how many keys it is GIVEN (`k = len(keyed)`
    at call time, not any original position in an 8-cell grid), so simply
    shortening the list costs a little pose variety and buys back every
    surviving cell being both correctly scaled AND, via the per-phase
    bounce/lean nudge, still a distinct frame.
    """
    heights = [(b[3] - b[1]) if (b := f.getbbox()) else 0 for f in frames]
    live = [i for i, h in enumerate(heights) if h > 0]
    if len(live) < 3:
        return frames
    # THE LARGEST GROUP THAT AGREES WITH ITSELF, found as a window over the
    # SORTED heights. Two earlier versions of this got it wrong in two
    # different ways and both shipped a strip that still pulsed:
    #
    #   * measuring every cell against the MEDIAN assumes the good cells
    #     outnumber the bad ones by enough to hold it. On a sheet drawn half
    #     at one size and half at another (audio's working: four cells at
    #     1241px of ink and four at 620px) the median lands in the gap and
    #     calls both groups fine.
    #   * grouping by "everything within tol of cell i" is not transitive.
    #     gameplay's heights run 671..1486 as a continuum, and seeded on the
    #     middle cell (976) every other cell is within 40% of it - so the
    #     group was all eight and nothing was dropped, while the two ends of
    #     it differ from each other by more than half.
    #
    # A window over sorted heights cannot do either: every member is within
    # tol of every other by construction, because the extremes are.
    order = sorted(live, key=lambda i: heights[i])
    best: list[int] = []
    for a in range(len(order)):
        b = a
        while (b + 1 < len(order)
               and heights[order[b + 1]] <= heights[order[a]] * (1 + tol)):
            b += 1
        window = order[a:b + 1]
        # Ties go to the taller group: both are internally consistent and
        # fit_scale normalises whichever wins to the same target height, so
        # the only thing left to choose on is which drawings carry more
        # pixels into that downscale.
        if (len(window) > len(best)
                or (len(window) == len(best) and best
                    and heights[window[0]] > heights[best[0]])):
            best = window
    keep = sorted(best)
    return [frames[j] for j in keep] if len(keep) >= 2 else frames


def equalise_heights(frames: list[Image.Image]) -> list[Image.Image]:
    """Resize every cell so they all carry the same height of ink.

    THE LAST OF THE PULSE, and it only makes sense for a strip whose pose is
    meant to hold still. `working` is a character seated at a station moving
    its hands: nothing about it should change the character's height, so any
    height difference between two of its cells is the generator drawing the
    same person at two sizes and nothing else. Dropping the gross outliers
    leaves a group that agrees to within a fifth, which is still four or
    five pixels of breathing on a 129px figure - visible, because the eye
    reads a silhouette edge moving against a static room behind it.

    Scaled about the FEET, which `place` pins to the cell's floor line: a
    character resized about its centre would sink into or float above the
    floor it is standing on, trading one visible fault for a worse one.
    """
    heights = [(b[3] - b[1]) if (b := f.getbbox()) else 0 for f in frames]
    live = [h for h in heights if h > 0]
    if len(live) < 2:
        return frames
    want = median(live)
    out = []
    for f, h in zip(frames, heights):
        if not h or abs(h - want) <= 1:
            out.append(f)
            continue
        box = f.getbbox()
        ink = f.crop(box)
        k = want / h
        ink = ink.resize((max(1, round(ink.width * k)),
                          max(1, round(ink.height * k))), Image.NEAREST)
        cell = Image.new("RGBA", f.size, (0, 0, 0, 0))
        # Bottom-aligned on the ink's own baseline, centred on its own
        # horizontal middle, so the feet stay where they were.
        cx = (box[0] + box[2]) / 2
        cell.alpha_composite(ink, (max(0, round(cx - ink.width / 2)),
                                   max(0, box[3] - ink.height)))
        out.append(cell)
    return out


def _silhouette(frame: Image.Image, size=(64, 80)) -> list[bool]:
    """The frame's ink as a normalised binary mask.

    Cropped to its own ink and resized to a common box first, so the
    comparison is of SHAPE and not of how big the model happened to draw
    this cell - scale is the other filter's job and conflating the two
    would let a correctly-facing small cell read as a different facing.
    """
    box = frame.getbbox()
    crop = frame.crop(box) if box else frame
    small = crop.resize(size, Image.LANCZOS)
    return [p > 8 for p in small.getchannel("A").getdata()]


def _iou(a: list[bool], b: list[bool]) -> float:
    inter = sum(1 for x, y in zip(a, b) if x and y)
    union = sum(1 for x, y in zip(a, b) if x or y)
    return inter / union if union else 0.0


def reject_facing_outliers(frames: list[Image.Image]) -> list[Image.Image]:
    """Keep the largest group of cells that share one facing.

    THE OTHER HALF OF "THE CHARACTER KEEPS SWAPPING ROUND". A bought sheet
    is one prompt, and nothing in this script has ever told the generator
    which way the character faces - so several sheets came back holding a
    front 3/4 for some cells and a back 3/4 for others. Played at eight
    frames a second that is not a working loop, it is a person spinning on
    the spot, and it is the thing a reader notices before anything else on
    the floor.

    Silhouette IoU separates the two cleanly (see SAME_FACING_IOU), so the
    dominant facing is the largest set of cells that all agree with one
    another. Everything outside it is dropped, exactly as a scale outlier
    is: `beat_cycle` re-derives its phase from however many keys it is
    handed, so a shorter list costs pose variety and nothing else, and the
    strip that comes out holds one character facing one way.
    """
    if len(frames) < 3:
        return frames
    sils = [_silhouette(f) for f in frames]
    best: list[int] = []
    for i in range(len(frames)):
        group = [j for j in range(len(frames))
                 if _iou(sils[i], sils[j]) >= SAME_FACING_IOU]
        if len(group) > len(best):
            best = group
    return [frames[j] for j in best] if len(best) >= 2 else frames


def pose_frames(name: str) -> dict[str, list[Image.Image]]:
    """The original model sheet, keyed, as the fallback strip per animation.

    Two jobs, and they read off DIFFERENT cells now. As the FALLBACK it hands
    back every cell FALLBACK_CELLS names for the animation, in play order, so a
    walk with no bought sheet is two frames rather than one. As the RULER - the
    only place the true ratio between a standing height and a seated one exists,
    because it is the one drawing where the same artist drew both at one scale -
    it is measured on POSE_CELL, the single cell that IS the pose. The two must
    not be conflated: the median height of a two-frame fallback is not the height
    of the pose it is standing in for.
    """
    sheet = Image.open(CAST / f"{name}-sheet.png").convert("RGB")
    cut = [key_cell(c) for c in cells(sheet, *POSE_GRID)]
    return {anim: [cut[i] for i in idx] for anim, idx in FALLBACK_CELLS.items()}


def cycle_for(anim: str, keyed: list[Image.Image], scale: float,
              rest: Image.Image) -> list[Image.Image]:
    """The installed strip for one animation: TARGET_FRAMES cells, all different.

    THE ONE PLACE THE FRAME COUNT IS DECIDED, and it does not ask where the
    drawings came from. That is the fix: the count used to be whatever the
    source happened to hold - eight for a bought walk, six for a bought idle,
    two for a stride cut out of the model sheet - so the length of a strip
    recorded a provider's balance rather than the motion. Both paths hand their
    drawings here and get the contract back.
    """
    n = TARGET_FRAMES[anim]
    if len(keyed) >= n:
        # Already at or past the contract: place and install, no invented
        # motion on top of drawings that carry their own. Past it only if a
        # future sheet is bought longer than this asks for, in which case the
        # extra drawings are dropped rather than the strip being installed at a
        # length the renderer's steps() would walk unevenly.
        return [place(f, scale) for f in keyed[:n]]
    if anim in BREATHES:
        return breath_cycle(keyed, n, scale, *BREATHES[anim])
    if anim == "handoff":
        return handoff_cycle(keyed, rest, n, scale)
    return beat_cycle(keyed, n, scale, *BEATS[anim])


def build(name: str) -> dict[str, int]:
    sheet = Image.open(CAST / f"{name}-sheet.png").convert("RGB")
    ruler = {a: key_cell(list(cells(sheet, *POSE_GRID))[i])
             for a, i in POSE_CELL.items()}
    poses = pose_frames(name)
    ref_h = {a: (im.getbbox()[3] - im.getbbox()[1]) if im.getbbox() else 0
             for a, im in ruler.items()}
    if not ref_h.get("idle"):
        print(f"{name}: model sheet has no standing pose - skipped")
        return {}

    dest = INSTALL / name
    dest.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for anim in GRID:
        cols, rows = grid_for(name, anim)
        src = ANIM / f"{name}-{anim}.png"
        # The height this animation should come out at, in the installed cell:
        # the standing target, times how tall this pose is RELATIVE to standing
        # on the model sheet. A seated frame is genuinely shorter and must stay
        # shorter, or the character grows every time it sits down.
        #
        # WHICH MODEL-SHEET POSE IS THE RULER IS NOT ALWAYS THE ANIMATION'S OWN.
        # The sheet holds one working pose and it is seated, so it is the wrong
        # ruler for the three seats whose craft is done standing - see
        # WORKING_STANDS.
        ruler_anim = ("idle" if anim == "working" and name in WORKING_STANDS
                      else anim)
        target = STAND_H * (ref_h.get(ruler_anim, ref_h["idle"])
                            / ref_h["idle"])

        # WHERE THE DRAWINGS COME FROM - a bought sheet, the passed cells of a
        # partly-right one, or the model sheet - and then ONE path installs
        # them. It used to be two paths that each decided their own length, and
        # a length decided beside the source is a length that records which
        # sheets a provider happened to sell.
        keyed: list[Image.Image] = []
        origin = ""
        keep = SALVAGE.get(f"{name}-{anim}")
        if (name in PASS.get(anim, ()) or keep) and src.exists():
            cut = [key_cell(c) for c in cells(
                Image.open(src).convert("RGB"), cols, rows)]
            want = cols * rows
            if keep:
                # CUT THE WHOLE GRID FIRST, THEN KEEP. The salvage list indexes
                # the sheet's own cells, so it has to be applied after the cut
                # and before anything drops an empty one - an index into a list
                # that has already had its blanks removed points at the wrong
                # frame, silently.
                cut = [cut[i] for i in keep]
                want = len(keep)
            cut = [k for k in cut if k.getbbox()]
            if len(cut) != want or not ink_heights(cut):
                print(f"{name}/{anim}: {len(cut)}/{want} cells survived "
                      "keying - falling back to the model sheet")
            else:
                fixed = reject_scale_outliers(cut)
                if len(fixed) != len(cut):
                    dropped = len(cut) - len(fixed)
                    print(f"{name}/{anim}: {dropped}/{len(cut)} cells were "
                          "off the sheet's own scale - dropped")
                # FACING SECOND, on what survived the scale cut. Order
                # matters: a cell the generator drew at half size distorts
                # its own silhouette enough to read as a different facing,
                # so measuring facing first would let a scale fault pick
                # which facing wins.
                #
                # ONLY THE ANIMATIONS WHOSE SILHOUETTE IS MEANT TO HOLD
                # STILL. A walk is a silhouette that changes on purpose -
                # legs apart on the stride, together on the passing frame -
                # and a handoff is an arm leaving the body's outline, which
                # is the entire gesture. Run over those, this filter reads
                # the motion itself as disagreement and throws away half the
                # cycle: measured, 4/8 of narrative's and qa's walks and 2/4
                # of four handoffs. `working` is the one strip that is a
                # fixed pose with small hand movement, so it is the one
                # strip where a silhouette that disagrees means the
                # character turned round.
                turned = (reject_facing_outliers(fixed)
                          if anim == "working" else fixed)
                if len(turned) != len(fixed):
                    dropped = len(fixed) - len(turned)
                    print(f"{name}/{anim}: {dropped}/{len(fixed)} cells face "
                          "a different way from the rest - dropped")
                # LAST, on the survivors: the two filters above remove the
                # cells that are wrong, this one settles the small
                # disagreement left between the ones that are right.
                keyed = equalise_heights(turned) if anim == "working" else turned
                origin = "salvaged sheet" if keep else "bought sheet"

        if not keyed:
            # THE FALLBACK, and it is a CYCLE rather than a still wherever the
            # model sheet holds one - see FALLBACK_CELLS.
            keyed = [p for p in poses[anim] if p.getbbox()]
            # THE RETRY NOTE WINS OVER "not generated". A rejected sheet is
            # moved out of ANIM (that is how it gets bought again when a
            # provider is funded), so asking the filesystem whether it exists
            # reports a sheet that was bought, looked at and turned down as one
            # nobody ever tried - which is the one thing this list exists to
            # stop happening twice.
            origin = "model sheet: " + RETRY.get(
                f"{name}-{anim}",
                "not generated" if not src.exists() else "not passed")
        if not keyed:
            print(f"{name}/{anim}: no sheet and no pose - nothing written")
            continue

        # THE SCALE IS MEASURED ON THE DRAWINGS AS CUT, and then only ever
        # reduced. One scale for the whole animation, off the median frame: per
        # frame would be worse than doing nothing, because it would erase the
        # crouch out of the walk's down frame and the lift out of its up frame,
        # which are the two frames the cycle is made of. Measuring it on the
        # DEFORMED frames instead would let a breath's own lift push the median
        # height up and shrink the character to compensate, so a breathing
        # character would stand shorter than the same character walking - which
        # reads as the camera moving every time an agent stops.
        hs = ink_heights(keyed)
        scale = fit_scale(keyed, target / median(hs))
        frames = cycle_for(anim, keyed, scale, ruler["idle"])
        strip(frames).save(dest / f"{anim}.png")
        counts[anim] = len(frames)
        made = ("as cut" if len(keyed) >= len(frames)
                else f"{len(keyed)} drawn -> {len(frames)} in-betweened")
        print(f"{name}/{anim}: {len(frames)} frames  {made}  ({origin})  "
              f"scale {scale:.2f}  ink {median(hs):.0f}px -> {target:.0f}px")

    # The old two-frame walk. Left behind by the first version of this script and
    # now dead: the walk is one strip and the renderer mirrors it in CSS rather
    # than loading a second set of images. Removed here so a stale file cannot go
    # on being served by a build that no longer references it.
    for dead in ("walk-left-a", "walk-left-b", "walk-right-a", "walk-right-b",
                 "walk-a", "walk-b"):
        (dest / f"{dead}.png").unlink(missing_ok=True)
    return counts


def write_frame_table(counts: dict[str, dict[str, int]]) -> None:
    """Hand the frame counts to the renderer as a generated module.

    NOT a JSON file fetched at runtime, and not a table typed into the
    stylesheet. A fetch would make the cast's timing depend on a request that can
    fail, on a pane whose whole claim is that what it draws is something it was
    actually told; and a hand-kept copy in CSS is a number that has to be edited
    in two places every time a sheet is regenerated at a new length, which is
    exactly the pair that drifts.
    """
    lines = [
        "/* GENERATED BY scripts/slice_floor_cast.py - DO NOT EDIT.",
        " *",
        " * How many frames each of the floor cast's animation strips actually",
        " * has. It is written by the slicer because the slicer is the only",
        " * thing that knows: the count is a property of the sheet that was",
        " * generated and installed, not a constant somebody chose.",
        " *",
        " * EVERY LOOPING STRIP IS EIGHT AND EVERY HANDOFF IS FOUR, and no two",
        " * frames of a strip are the same picture. That is a contract the",
        " * slicer holds to (TARGET_FRAMES) rather than a count that fell out",
        " * of what a provider sold: where fewer drawings exist than the",
        " * contract asks for, the frames between them are the neighbouring",
        " * drawing carrying the part of the motion that belongs at that phase",
        " * - a few pixels of chest for a breath, of bounce and lean for a",
        " * stride or a keystroke. Nothing here holds one drawing for two",
        " * frames, which is what the counts recorded before.",
        " *",
        " * The renderer reads it for two things that have to agree: the",
        " * background-size that lays the strip across the sprite box, and the",
        " * steps() count that walks it. Getting them from one number is what",
        " * stops a strip being stepped through at the wrong width. */",
        "export const CAST_FRAMES: Record<string, Record<string, number>> = {",
    ]
    for name in NAMES:
        got = counts.get(name)
        if not got:
            continue
        inner = ", ".join(f"{a}: {n}" for a, n in got.items())
        lines.append(f"  {name}: {{ {inner} }},")
    lines += [
        "};",
        "",
        "/** How many frames <seat>'s <anim> strip has.",
        " *",
        " *  ONE for an unknown seat rather than zero: a project can invent a",
        " *  seat, it falls back to the `generic` drawing, and a zero here would",
        " *  divide the sprite box by nothing. */",
        "export function castFrames(seat: string, anim: string): number {",
        "  return CAST_FRAMES[seat]?.[anim] ?? 1;",
        "}",
        "",
    ]
    CAST_FRAMES_TS.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {CAST_FRAMES_TS}")


def check_margins(counts: dict[str, dict[str, int]]) -> None:
    """Prove no cell's ink reaches its own edge, on the finished files.

    THE ONE FAULT THAT LOOKS LIKE ART. Everything else that can go wrong here
    announces itself - a bad key leaves a navy block, a bad grid leaves a sliced
    head. Ink touching a cell edge does not: it reads as a character standing a
    bit close to the next one, right up until the strip is stepped and the
    neighbour's elbow travels along with it. `fit_scale` is supposed to make it
    impossible, so this is the assertion that it did, measured on what was
    actually written rather than on what was intended.

    It also covers the half-pixel case the renderer cannot avoid. The sprite box
    is a multiple of `--cell` and lands on fractional device pixels, so the
    browser rounds each step and can pull a column from the neighbouring cell.
    A margin of a pixel or more means that column is transparent.
    """
    tight = None
    for name, anims in counts.items():
        for anim in anims:
            p = INSTALL / name / f"{anim}.png"
            im = Image.open(p).convert("RGBA")
            n = im.size[0] // CELL_W
            for i in range(n):
                box = im.crop((i * CELL_W, 0, (i + 1) * CELL_W, CELL_H)).getbbox()
                if not box:
                    print(f"!! {name}/{anim} frame {i} is empty")
                    continue
                m = min(box[0], CELL_W - box[2])
                if tight is None or m < tight[0]:
                    tight = (m, f"{name}/{anim} frame {i}")
    if tight is None:
        return
    if tight[0] < 1:
        print(f"\n!! INK TOUCHES A CELL EDGE: {tight[1]} has {tight[0]}px of "
              "margin - it will bleed into the next frame")
    else:
        print(f"\ntightest side margin in any cell: {tight[0]}px ({tight[1]})")


def check_counts(counts: dict[str, dict[str, int]]) -> int:
    """Prove every installed strip is the length TARGET_FRAMES promises.

    MEASURED ON THE FILES, not on the dictionary this run built. The renderer
    reads the frame count out of castFrames.ts and lays the strip across the
    sprite box by it, so a file whose width disagrees with its count is not a
    short animation, it is a sprite stepped through fractions of frames. The
    only way to catch that is to ask the PNG how wide it is.
    """
    bad = 0
    for name, anims in counts.items():
        for anim, n in anims.items():
            wide = Image.open(INSTALL / name / f"{anim}.png").size[0] // CELL_W
            want = TARGET_FRAMES[anim]
            if wide != n or n != want:
                print(f"!! {name}/{anim}: {wide} frames on disk, {n} declared, "
                      f"{want} required")
                bad += 1
    return bad


def check_distinct(counts: dict[str, dict[str, int]]) -> int:
    """Prove no strip pads itself out with a repeat of a frame it already has.

    THE FAULT THIS CATCHES DOES NOT LOOK LIKE A FAULT. A 1024px strip that holds
    six drawings and two copies passes every other check here - the width is
    right, the count is right, the margins are right - and on screen it is an
    animation that stalls twice a lap. It is also the exact shape of the failure
    this pass was opened on, so it is checked rather than reasoned about: cells
    are compared byte for byte, and any pair that matches is named.
    """
    bad = 0
    for name, anims in counts.items():
        for anim in anims:
            im = Image.open(INSTALL / name / f"{anim}.png").convert("RGBA")
            n = im.size[0] // CELL_W
            seen: dict[bytes, int] = {}
            for i in range(n):
                b = im.crop((i * CELL_W, 0, (i + 1) * CELL_W, CELL_H)).tobytes()
                if b in seen:
                    print(f"!! {name}/{anim}: frame {i} is identical to frame "
                          f"{seen[b]}")
                    bad += 1
                else:
                    seen[b] = i
    return bad


def main() -> int:
    counts: dict[str, dict[str, int]] = {}
    for name in NAMES:
        if not (CAST / f"{name}-sheet.png").exists():
            print(f"{name}: no model sheet - skipped")
            continue
        got = build(name)
        if got:
            counts[name] = got
    write_frame_table(counts)
    print(json.dumps(counts, indent=1))
    check_margins(counts)
    frames = [n for c in counts.values() for n in c.values()]
    print(f"\n{sum(frames)} frames across {len(frames)} animations, "
          f"{sum(frames) / len(frames):.1f} per animation")
    # THE RUN FAILS ON EITHER OF THESE. A short strip and a padded one are both
    # invisible to a build - the files are there and the app renders - so the
    # only place they can be caught is the thing that wrote them.
    bad = check_counts(counts) + check_distinct(counts)
    if bad:
        print(f"\n{bad} strips are short or padded - see above")
        return 1
    print("\nevery strip is TARGET_FRAMES long and holds no repeated frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
