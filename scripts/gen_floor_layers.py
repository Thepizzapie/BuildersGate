"""Split each painted room into a FLOOR layer and a PROPS layer.

WHY, IN ONE SENTENCE: a room bought as a single flat picture can never let a
character stand behind a desk, so every depth cue had to be faked, and faking
them is what made the floor look cheap.

The longer version is worth writing down because it took four wrong turns to
reach. gen_floor_rooms.py buys each room as one composed drawing, which fixed
what it set out to fix - the rooms are coherent, on-palette and match the
concept. But one image is one layer. A door had to be a dark rectangle stamped
over it, which read as a hole punched in a finished drawing. A character always
draws in front of everything, because there is no "behind" to draw into. Light
had to be a gradient laid over the top. Each fake needed another fake to cover
its edges.

TWO LAYERS IS ALL IT TAKES.

  floor.png   the room EMPTY - its floor, its walls, and a real doorway drawn
              into the wall that needs one. Opaque, and baked once.
  props.png   the furniture and everything mounted on the walls, in exactly the
              same places, on a keyable field so everything else is transparent.

The renderer bakes the floor, then draws the props layer as horizontal strips
inside the same y-sort the characters are in. A character standing at a desk is
then drawn after the strips north of them and before the strips south of them,
which is real occlusion falling out of the sort that already existed rather than
another special case.

REGISTRATION COMES FROM A SHARED ANCHOR, NOT FROM THE PROMPT. Both layers are
generated FROM the finished room image, and both are told not to move or rescale
anything. Two layers generated independently from the concept would not line up
and no wording would make them; generated from the same picture, "same place,
same size" is a thing the model can actually see.

THE DOORWAY IS DRAWN BY WHOEVER DRAWS THE ROOM. It goes in the floor layer, in
the wall CLEAR says needs one, composed with the wall around it. The renderer
does not illustrate doors any more - it stopped after the punched version, and
the note where that code used to live says why.

Run:
    python scripts/gen_floor_layers.py              # every room, both layers
    python scripts/gen_floor_layers.py audio        # one room
    python scripts/gen_floor_layers.py --force qa
"""
from __future__ import annotations

import concurrent.futures as futures
from collections import deque
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _floorpaths import sandbox  # noqa: E402

from PIL import Image  # noqa: E402

from bgate_adapters import kie  # noqa: E402
from gen_floor_rooms import AT, DEST, RATIO  # noqa: E402
from slice_floor_cast import despeckle  # noqa: E402

ART_ROOT = sandbox()
# The finished single-layer rooms. These are the anchor for both layers and are
# not thrown away: a room whose split fails still has one to fall back to.
WHOLE = ROOT / "frontend" / "public" / "img" / "floor" / "rooms"
OUT = WHOLE
RAW = ART_ROOT / ".bgate_out" / "art" / "floor-layers"

MODEL = "nano-banana-2"
TIMEOUT = 300

# THE CHROMA KEY'S TWO THRESHOLDS, in units of `min(r, b) - g`.
# Pure #FF00FF scores 255 and a dark plum shadow of it still scores well over a
# hundred, so hi can sit low enough to catch the whole lit field. lo is what
# protects the pinks that are genuinely IN these rooms - the art room's task
# lamp is the tightest case at around 40 - so the soft band runs above it.
KEY_HI = 96
KEY_LO = 46
# The flood's own test, far looser than the key's because it is guarded by
# connectivity rather than by colour alone.
KEY_LOOSE = 14
# A props layer is mostly field: the furniture in these rooms covers well under
# half the frame. Below this the model kept the floor in, and the layer would be
# an opaque copy of the room drawn over itself.
MIN_FIELD = 0.35

# WHERE THE DOOR GOES, worded for the floor layer. Only rooms whose door is in a
# wall the picture draws appear here - a south door needs nothing, because the
# camera looks from the south and there is no near wall in the picture at all.
# DOORS GO IN BACK WALLS ONLY, and the side-wall entries were removed rather
# than reworded. A back wall is a broad horizontal band with room for a frame,
# a threshold and the floor running through it, and a door drawn there reads as
# a door. A side wall at this camera is a narrow vertical strip a few pixels
# across: the same instruction produced a full doorframe standing proud of the
# wall in Gameplay, which read as a wardrobe in the middle of the room, and a
# flat black slab in QA. Those rooms keep their doors in the PLAN - routing and
# the wall gaps are unchanged - the picture just does not try to draw them.
# EMPTY, AND THE MACHINERY BELOW IT IS KEPT ON PURPOSE.
#
# Doors were drawn into the rooms' back walls for several rounds and every
# version was worse than none. The last one was geometrically perfect - exactly
# one, exactly centred, level to the pixel, seam error under three - and it
# still read as a glowing panel, because at a camera looking down at 70 to 75
# degrees a doorway in the FAR wall is seen almost edge on. There is no drawing
# of it that is both visible and not a bright rectangle; the concept solves this
# by not attempting it, and putting small door slabs on the corridor-side walls
# between rooms instead.
#
# add_door and its wall-base measurement stay because the corridor-side version
# needs both, and because a table that is empty says "this was decided" where a
# deleted one says nothing.
DOOR_WALL: dict[str, str] = {
    # Art and Tech, because their back walls are otherwise solid and a room with
    # no way in reads as a mistake. Drawn as a patch and composited at the plan's
    # own door coordinates - see add_door - so exactly one door lands, centred
    # and level, whatever the model does with the picture.
    #
    # THE OPENING IS DARK. The first cut of these asked for a warm spill across
    # the threshold, on the theory that a black rectangle reads as a hole rather
    # than a way out. It does - and the cure was worse: a lit doorway is the
    # brightest thing in a dim room, so all three read as glowing panels. A
    # recessed dark opening with a lit FRAME is the version that reads as a door
    # without competing with the room for attention.
    "art": "the BACK wall, in the middle of the horizontal wall across the top",
    "tech": "the BACK wall, in the middle of the horizontal wall across the top",
}



FLOOR_PROMPT = """Here is a room interior. Redraw it COMPLETELY EMPTY.

Keep the room itself exactly as it is: the same floor material and colour, the
same walls, the same lighting, the same pixel-art style, and above all the same
framing - the walls stay in the same places and the picture is not zoomed,
cropped, shifted or rescaled in any way.

REMOVE EVERYTHING THAT STANDS UP. No furniture, no desks, no chairs, no
shelves, no equipment, no plants. Nothing standing on the floor and nothing
hung on, leaning against or mounted to the walls. Where something used to be,
draw the plain floor or the plain wall that was behind it.

BUT KEEP EVERYTHING THAT LIES FLAT ON THE GROUND. Rugs, mats, floor markings,
cable runs taped to the floor, and the soft shadows the furniture casts onto the
floor all STAY exactly where they are. They are part of the floor.

The result is the room with its furniture taken out and its floor left as it
was.

EVERY WALL IS SOLID. Do not draw a door, a doorway, an archway or an opening of
any kind in any wall. The doorway is added afterwards at an exact position, and
one drawn here would be a second door beside it.

NO PEOPLE, no characters, no animals. NO TEXT of any kind."""

# THE DOORWAY IS COMPOSITED, NOT PROMPTED, and this is the third approach to it.
#
# First the renderer punched a dark rectangle through the finished art, which
# read as a hole rather than a door. Then the floor layer was asked to draw one,
# which looks far better and is the right instinct - but a door has to satisfy
# two things at once, and a language model reliably delivers only the first. It
# must LOOK hand-drawn, in this room's own wall material, and it must be
# GEOMETRICALLY EXACT: exactly one of them, exactly where the plan puts its door
# gap, and exactly level. Asked three times in increasingly explicit words, the
# office kept coming back with a symmetric pair and the art room's frame kept
# leaning.
#
# So the two halves are split at the point where they differ. The model draws a
# PATCH of wall with a doorway in it, which is the part that has to look drawn;
# this file pastes that patch into the wall at the plan's own coordinates, which
# is the part that has to be exact. Level is then not something anyone has to
# get right - it is a rectangle blitted on an integer row.
#
# THE PATCH IS OPAQUE AND CARRIES ITS OWN WALL, rather than being keyed to a
# frame on transparency. Keying puts the seam at the frame's silhouette, where
# any mismatch in the wall behind it shows as a halo; an opaque patch puts the
# seam at the door's outer edge, which is where a frame's edge belongs anyway.
DOOR_PROMPT = """Draw a piece of wall with a single DOORWAY in it.

The reference is a room. Use ITS back wall - the horizontal wall across the top
of the reference - as the wall in this drawing: the same material, the same
colour, the same pixel-art style, the same lighting. What you draw is a small
rectangular crop of exactly that wall, with a doorway cut through the middle of
it.

The doorway fills most of the width and runs from the top of the picture down to
the floor at the bottom. It has a proper frame: two straight vertical jambs of
equal width down the sides and one straight horizontal lintel across the top,
all the same thickness and all square to the edges of the picture.

Through the opening, show a DARK recess - a few steps of dim grey corridor
floor falling away into shadow, and the faint suggestion of a far wall. Keep it
DARK and UNLIT: this is a doorway in a dim room, not a lit panel, and anything
bright here becomes the brightest thing in the room and stops reading as a door.
No glow, no warm spill, no light pooling on the floor.

The FRAME is what catches the light instead: a thin highlight along the top edge
of the lintel and down the lit side of the near jamb, exactly as bright as the
rest of the wall's edges and no brighter.

FILL THE WHOLE PICTURE. Wall to the left of the frame, wall to the right of it,
the doorway between them, and the floor line at the very bottom. No margin, no
background, no magenta, no transparency, no drop shadow.

Draw it square on and flat. No perspective, no tilt, no lean. ONE doorway only.
NO PEOPLE and NO TEXT."""

PROPS_PROMPT = """Here is a room interior. Keep ONLY the objects in it.

Replace the FLOOR and the WALLS with a completely flat, solid, uniform bright
magenta, #FF00FF. Every surface of the room itself becomes that one magenta:
the floor, the walls, the skirting, the corners. Flat magenta with no shading,
no gradient and no texture.

EVERYTHING THAT STANDS UP STAYS EXACTLY WHERE IT IS. The furniture, the
equipment, the screens, the shelves, the plants, and everything mounted on or
leaning against the walls - all of it stays, in exactly the same position, at exactly
the same size, in exactly the same colours, in exactly the same framing. Do not
move anything. Do not resize anything. Do not add anything. Do not remove
anything. Do not redraw the picture from a different angle or distance.

DOORWAYS ARE PART OF THE WALL. A doorway, its frame, and the dark opening
through it all become magenta along with the wall they are cut into. Do not keep
the opening as a dark shape - the floor layer draws the doorway, and a dark
rectangle kept here is drawn as a solid black slab on top of it.

ANYTHING LYING FLAT ON THE GROUND GOES WITH THE FLOOR. Rugs, mats, floor
markings, cables taped down, and every shadow cast onto the floor become
magenta along with it. Only things that stand up, or hang on a wall, are kept.

This is not fussiness. A rug is drawn at floor level, so a person standing on
one must be drawn IN FRONT of it - and this layer is sorted against the cast by
depth, which would put the near half of the rug over their legs. Flat things
belong to the floor for the same reason a person's own shadow does.

Think of it as the same picture with the room's own surfaces turned magenta and
the standing objects left untouched. They keep their own shading and any shadow
they cast onto ANOTHER OBJECT, but nothing is drawn onto the magenta.

NO PEOPLE, no characters, no animals. NO TEXT of any kind."""

_URL: dict[str, str] = {}
_LOCK = threading.Lock()


def url_for(path: Path) -> str:
    """A kie URL for a local file, uploaded at most once per run.

    The lock spans the upload rather than only the dict write: a room's two
    layers run in parallel and both want the same anchor, so releasing between
    the check and the upload would mint two URLs for one file.
    """
    key = str(path)
    with _LOCK:
        if key not in _URL:
            _URL[key] = kie.upload_file(key, root=str(ART_ROOT))["url"]
        return _URL[key]


def key_props(src: Path, dst: Path) -> tuple[int, int]:
    """Chroma-key the magenta out, suppress its spill, then downscale.

    A COLOUR-DISTANCE TEST IS NOT ENOUGH HERE, and the first version was one.
    "Within N of #FF00FF" assumes the field is the colour we asked for, and the
    model does not deliver it that way: told to paint the floor flat magenta it
    paints a LIT magenta floor, running from pure at the back to a dark plum in
    the shadowed foreground. Half the field then sits outside any tolerance
    tight enough to spare the furniture, so the bottom of the layer came back as
    an opaque magenta wash with a magenta halo around every object.

    So this is the key a compositor would use. Magenta-ness is `min(r, b) - g`,
    which is large for magenta at ANY brightness - dark plum keys exactly as
    readily as pure - and at or below zero for the greys, browns and blues
    everything in these rooms is actually made of.

    THE SOFT BAND BETWEEN lo AND hi IS THE HALO FIX. An edge pixel is a blend of
    object and field, so it is partly magenta and belongs at partial alpha; a
    hard threshold has to choose, and either choice leaves a fringe. Alpha ramps
    across the band instead.

    AND THE SPILL COMES OFF WHAT SURVIVES. A pixel that is 40% field still
    carries the field's colour, which shows as a pink rim once it is composited
    over a dark floor. Pulling green up to min(r, b) neutralises exactly the
    magenta cast and leaves everything that was not magenta alone.
    """
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    out = im.convert("RGBA")
    op = out.load()
    cleared = 0.0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            m = min(r, b) - g
            if m >= KEY_HI:
                op[x, y] = (0, 0, 0, 0)
                cleared += 1
                continue
            if m <= KEY_LO:
                continue
            a = 1.0 - (m - KEY_LO) / float(KEY_HI - KEY_LO)
            cleared += 1.0 - a
            # De-spill: lift green to where it would sit without the cast.
            op[x, y] = (r, min(r, b), b, int(round(a * 255)))
    # THE TAIL THE THRESHOLD CANNOT REACH, taken by connectivity instead.
    #
    # The field is LIT, and where it falls into the deepest foreground shadow it
    # desaturates toward grey - `min(r, b) - g` drops under twenty, which is
    # below any threshold that still spares the art room's pink lamp. Colour
    # alone therefore cannot separate the darkest quarter of the field from the
    # props, and that is what left a plum wash across the bottom of the layer.
    #
    # Connectivity can. The field is one region touching the frame's border and
    # no prop is: the whole point of the composition is that objects sit inside
    # the room. So this floods inward from the border through anything still
    # faintly magenta, which walks the length of the dark tail and stops dead at
    # the first pixel of furniture. A lamp cannot be reached because the flood
    # never travels through a pixel that is not the field.
    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        q.append((x, 0)); q.append((x, h - 1))
    for y in range(h):
        q.append((0, y)); q.append((w - 1, y))
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        r, g, b = px[x, y]
        if op[x, y][3] != 0 and min(r, b) - g < KEY_LOOSE:
            continue
        seen[i] = 1
        if op[x, y][3] != 0:
            op[x, y] = (0, 0, 0, 0)
            cleared += 1
        if x: q.append((x - 1, y))
        if x < w - 1: q.append((x + 1, y))
        if y: q.append((x, y - 1))
        if y < h - 1: q.append((x, y + 1))

    share = cleared / float(w * h)
    if share < MIN_FIELD:
        raise ValueError(
            f"only {share:.0%} keyed - the floor and walls were not turned "
            "magenta, so this layer is an opaque copy of the room")

    out = despeckle(out)
    # LANCZOS: the model returns a large soft drawing rather than true pixel
    # art, so this is a photographic downscale. Alpha resamples with it, which
    # is what keeps the keyed edges from stair-stepping.
    out = out.resize(DEST, Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return out.size


def one(room: str, layer: str, force: bool) -> dict:
    whole = WHOLE / f"{room}.png"
    if not whole.exists():
        return {"room": room, "layer": layer, "ok": False,
                "error": "no finished room to split - run gen_floor_rooms first"}

    dst = OUT / f"{room}-{layer}.png"
    if dst.exists() and not force:
        return {"room": room, "layer": layer, "ok": True, "skipped": True}

    raw = RAW / f"{room}-{layer}.png"
    started = time.monotonic()
    if not raw.exists() or force:
        raw.parent.mkdir(parents=True, exist_ok=True)
        prompt = FLOOR_PROMPT if layer == "floor" else PROPS_PROMPT
        res = kie.generate_image(
            prompt, str(raw), model=MODEL, aspect_ratio=RATIO, timeout=TIMEOUT,
            root=str(ART_ROOT), logical_name=f"floor-{room}-{layer}",
            image_urls=[url_for(whole)])
        if not res.get("ok", True) or not raw.exists():
            return {"room": room, "layer": layer, "ok": False,
                    "error": res.get("error", "no file came back")}

    try:
        if layer == "props":
            size = key_props(raw, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            im = Image.open(raw).convert("RGB").resize(DEST, Image.LANCZOS)
            im.save(dst)
            size = im.size
    except Exception as exc:                                      # noqa: BLE001
        return {"room": room, "layer": layer, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"room": room, "layer": layer, "ok": True, "size": size,
            "seconds": round(time.monotonic() - started, 1)}



# ── the doorway patch ──────────────────────────────────────────────────────
# Where the plan puts a back-wall door, in cells, from floorplan.craftRoom:
# `doorAt = rect.w / 2 - DOOR_LEN / 2` with ROOM_W 8 and DOOR_LEN 2.5, so the
# gap runs 2.75..5.25 and is centred on the room. Read from there rather than
# re-derived by eye, because the whole point of compositing is that this number
# is the same one the walls and the router already use.
DOOR_X0, DOOR_X1 = 2.75, 5.25
CELL_PX = DEST[0] // 8


def wall_base(im: Image.Image, x0: int, x1: int) -> int:
    """The row where the back wall meets the floor, across a span of columns.

    MEASURED, NOT ASSUMED. The nine rooms do not agree on how tall their back
    wall is - the office panelling runs deeper than the tech room's - and a
    constant would float the doorway above the floor in some rooms and bury it
    in others. The junction is the strongest horizontal edge in the band where a
    wall base can actually be: wall and floor are different materials, so the
    row-to-row difference spikes there.

    THE SEARCH STARTS WELL BELOW THE TOP EDGE, and that is not caution. Scanning
    from row four, the strongest edge in the whole upper half is the picture's
    own top border - the dark line the room is drawn inside - so every room
    reported a wall six to eight pixels tall and the doorway patches came back
    as 120x8 slivers. The band below is where a back wall's base can be: no room
    has one shallower than a fifth of a cell or deeper than two cells.
    """
    px = im.convert("RGB").load()
    lo = max(6, int(im.height * 0.10))
    hi = min(im.height - 2, int(im.height * 0.45))
    best, at = -1.0, im.height // 5
    for y in range(lo, hi):
        d = 0
        for x in range(x0, x1, 3):
            a, b = px[x, y], px[x, y + 1]
            d += abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
        if d > best:
            best, at = d, y + 1
    return at


# The depth a back wall should run to, in cells. Not an average of what the nine
# rooms came back with - several of those are wrong - but the depth at which a
# room reads as a room seen from above: enough wall to carry what hangs on it,
# little enough that the floor is the subject. audio and art landed here on
# their own, which is the only reason to believe the number.
WALL_TARGET = 2.0


def level_wall(room: str) -> dict:
    """Slide a room's layers so its back wall meets the floor at WALL_TARGET.

    WHY THIS IS SAFE TO DO TO A FINISHED DRAWING, which is the only real
    question. The picture is square on - the whole prompt is built around that -
    so the wall is a band across the top, the floor is everything below it, and
    both are uniform in the direction being changed. Sliding the image up by N
    rows therefore costs the top N rows of WALL, which is the least informative
    strip in the frame, and needs N rows of FLOOR at the bottom, which is the
    most repeatable. Neither is stretched: nothing is rescaled, so the panelling
    keeps its proportions and the trophies keep their shapes.

    THE TWO LAYERS MOVE TOGETHER OR NOT AT ALL. They are registered to each
    other by construction, and a shift applied to one would put every piece of
    furniture N pixels off the floor it stands on.
    """
    floor_p = OUT / f"{room}-floor.png"
    props_p = OUT / f"{room}-props.png"
    if not floor_p.exists():
        return {"room": room, "ok": False, "error": "no floor layer"}

    base = Image.open(floor_p).convert("RGB")
    at = wall_base(base, int(2.75 * CELL_PX), int(5.25 * CELL_PX))
    want = int(round(WALL_TARGET * CELL_PX))
    dy = at - want
    if abs(dy) < CELL_PX * 0.15:
        return {"room": room, "ok": True, "was": at, "now": at, "moved": 0}
    if dy < 0:
        # A wall SHALLOWER than the target would need wall invented above it,
        # and inventing wall is how a seam appears. Left alone: too much floor
        # is a much smaller error than a fabricated band across the top.
        return {"room": room, "ok": True, "was": at, "now": at, "moved": 0,
                "why": "wall is already shallower than the target"}

    w, h = base.size
    # The floor rows to repeat: taken from just above the bottom rather than the
    # very last row, which carries the picture's own dark border.
    strip = base.crop((0, h - 8 - dy, w, h - 8))
    out = Image.new("RGB", (w, h))
    out.paste(base.crop((0, dy, w, h)), (0, 0))
    out.paste(strip, (0, h - dy))
    out.save(floor_p)

    if props_p.exists():
        pr = Image.open(props_p).convert("RGBA")
        moved = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        moved.paste(pr.crop((0, dy, w, h)), (0, 0))
        moved.save(props_p)

    return {"room": room, "ok": True, "was": at, "now": want, "moved": dy}


def add_door(room: str, force: bool) -> dict:
    """Buy a patch of this room's wall with a doorway in it, and blit it in."""
    floor = OUT / f"{room}-floor.png"
    if not floor.exists():
        return {"room": room, "layer": "door", "ok": False,
                "error": "no floor layer to cut a door into"}

    base = Image.open(floor).convert("RGB")
    x0 = int(DOOR_X0 * CELL_PX)
    x1 = int(DOOR_X1 * CELL_PX)
    y1 = wall_base(base, x0, x1)
    w, h = x1 - x0, y1

    raw = RAW / f"{room}-door.png"
    started = time.monotonic()
    if not raw.exists() or force:
        raw.parent.mkdir(parents=True, exist_ok=True)
        res = kie.generate_image(
            DOOR_PROMPT, str(raw), model=MODEL,
            aspect_ratio=_nearest_ratio(w, h), timeout=TIMEOUT,
            root=str(ART_ROOT), logical_name=f"floor-{room}-door",
            image_urls=[url_for(floor)])
        if not res.get("ok", True) or not raw.exists():
            return {"room": room, "layer": "door", "ok": False,
                    "error": res.get("error", "no file came back")}

    patch = Image.open(raw).convert("RGB").resize((w, h), Image.LANCZOS)

    # ── MATCH THE PATCH'S WALL TO THE WALL IT LANDS IN ─────────────────────
    # The patch is drawn from the room, so its wall is the right MATERIAL, and
    # it is drawn separately, so it is not quite the right VALUE - the office's
    # came back a stop lighter than its own panelling. Pasted, that is a pale
    # rectangle around the door: the geometry is finally right and the join
    # gives it away.
    #
    # The correction is per channel and driven by the join itself. Both edges of
    # the patch are wall, and so is the wall they land beside, so the ratio of
    # those two means is exactly the gain that makes them agree. Applied to the
    # whole patch it also lifts the doorway, which is correct - a doorway in a
    # darker wall is a darker doorway.
    bp, pp = base.load(), patch.load()
    dst_sum = [0.0, 0.0, 0.0]
    src_sum = [0.0, 0.0, 0.0]
    n = 0
    for y in range(0, h, 2):
        for dx in (-3, -2, 2, 3):
            xa = x0 + dx if dx < 0 else x1 + dx - 1
            xb = dx + 3 if dx < 0 else w - 1 - (3 - (dx - 2))
            if not (0 <= xa < base.width and 0 <= xb < w):
                continue
            a, b = bp[xa, y], pp[xb, y]
            for k in range(3):
                dst_sum[k] += a[k]
                src_sum[k] += b[k]
            n += 1
    if n:
        # Clamped: a wildly off patch is a bad generation, and stretching its
        # levels far enough to hide that would only produce a smooth join
        # between a wall and something that is not one.
        gain = [min(1.6, max(0.6, dst_sum[k] / max(1.0, src_sum[k])))
                for k in range(3)]
        lut = [min(255, int(round(v * gain[k]))) for k in range(3)
               for v in range(256)]
        patch = patch.point(lut)
        pp = patch.load()

    # ── AND FEATHER WHAT IS LEFT ──────────────────────────────────────────
    # A gain match cannot fix texture: the wall's panel lines and tile grout do
    # not line up across the join, so a hard edge still draws a vertical rule
    # down each side. Three columns of cross-fade is enough to lose it at this
    # pixel size, and it is three columns of WALL on both sides - the frame is
    # inboard of it - so nothing structural is blurred.
    FEATHER = 3
    for y in range(h):
        for i in range(FEATHER):
            t = (i + 1) / float(FEATHER + 1)
            for xa, xb in ((x0 + i, i), (x1 - 1 - i, w - 1 - i)):
                if not (0 <= xa < base.width):
                    continue
                a, b = bp[xa, y], pp[xb, y]
                pp[xb, y] = tuple(int(round(a[k] * (1 - t) + b[k] * t))
                                  for k in range(3))

    # THE SEAM, MEASURED BEFORE IT IS TRUSTED. The patch carries its own wall,
    # so the join is only invisible if that wall matches the one it lands in.
    # Comparing the column just outside the paste against the patch's own edge
    # column turns "does it blend" into a number, which is the difference
    # between checking this and hoping.
    bp, pp = base.load(), patch.load()
    seam = 0
    for y in range(0, h, 2):
        for xa, xb in ((x0 - 2, 0), (x1 + 1, w - 1)):
            if 0 <= xa < base.width:
                a, b = bp[xa, y], pp[xb, y]
                seam += (abs(a[0] - b[0]) + abs(a[1] - b[1])
                         + abs(a[2] - b[2])) / 3.0
    seam /= max(1, len(range(0, h, 2)) * 2)

    base.paste(patch, (x0, 0))
    base.save(floor)
    return {"room": room, "layer": "door", "ok": True, "size": (w, h),
            "seam": round(seam, 1), "base": y1,
            "seconds": round(time.monotonic() - started, 1)}


def _nearest_ratio(w: int, h: int) -> str:
    """The supported aspect closest to the slot, so the patch is not stretched."""
    want = w / float(h)
    have = {"1:1": 1.0, "5:4": 1.25, "4:3": 4 / 3, "3:2": 1.5, "16:9": 16 / 9,
            "2:3": 2 / 3, "3:4": 0.75, "4:5": 0.8}
    return min(have, key=lambda k: abs(have[k] - want))


def main(argv: list[str]) -> int:
    force = "--force" in argv
    rooms = [a for a in argv if not a.startswith("--")] or list(AT)
    for r in rooms:
        if r not in AT:
            raise SystemExit(f"unknown room: {r} (have {', '.join(AT)})")

    jobs = [(r, layer) for r in rooms for layer in ("floor", "props")]
    print(f"{len(jobs)} layer(s) -> {OUT}")
    bad = 0
    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        for f in futures.as_completed(
                [pool.submit(one, r, layer, force) for r, layer in jobs]):
            d = f.result()
            tag = f"{d['room']}-{d['layer']}"
            if d.get("skipped"):
                print(f"  ·  {tag:<20} already on disk")
            elif d["ok"]:
                print(f"  ok {tag:<20} {d['size'][0]}x{d['size'][1]}"
                      f"  {d['seconds']}s")
            else:
                bad += 1
                print(f"  XX {tag:<20} {d['error']}")

    # AFTER the floor layers, never beside them: this edits the file the floor
    # pass writes, so running the two concurrently would race on it.
    for r in rooms:
        if r not in DOOR_WALL:
            continue
        d = add_door(r, force)
        if d["ok"]:
            print(f"  ok {r + '-door':<20} {d['size'][0]}x{d['size'][1]} "
                  f"at wall base {d['base']}  seam {d['seam']}  {d['seconds']}s")
        else:
            bad += 1
            print(f"  XX {r + '-door':<20} {d['error']}")
    print(f"done, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    from bgate_core.store import envfile
    envfile.load_env(str(ART_ROOT))
    sys.exit(main(sys.argv[1:]))
