"""Redraw each room of the studio floor from the user's concept, one room per call.

WHY THIS REPLACED A PROP GENERATOR. The first attempt at "make the floor less
flat" bought individual furniture: a mixing console, a bookcase, a server rack,
fifty-odd sprites, each generated alone on a keyable field and then scattered by
this repo at coordinates written by hand. Two things were wrong with it and only
one of them was fixable by tuning.

The fixable one was detail. A prop drawn at 2048 square and shown eighty pixels
wide is noise; the console came back with two hundred individually drawn faders
and resolved to mush.

The unfixable one is COHERENCE. What makes the concept work is that it is ONE
DRAWING. Its rooms share a palette, a pixel grid, a light angle and a sense of
how densely a surface gets dressed, because one pass drew all of them. Fifty-five
independent generations do not share any of that no matter how the prompt is
worded, and the composition - console under the wall gear, plant tucked in the
corner - would still have been this file's guesswork rather than the artist's.

SO THE UNIT IS THE ROOM. Each room is cropped out of the concept, handed back as
the reference, and redrawn at full resolution as one composed picture. Nine calls
rather than fifty-five, every one of them internally consistent, and every one of
them faithful to a layout the user already approved. The renderer blits the
result as the room's backdrop and draws the cast on top, which is how the concept
reads in the first place.

THE CONCEPT IS ON THE PLAN'S OWN GRID, which is what makes the crop exact rather
than eyeballed: it is 540x490 for a floor that is 30x27 cells, so one cell is 18
pixels and a room is a 144x126 box at a multiple of it. Verified by cropping
audio and looking at it, not assumed from the arithmetic.

ROOMS ARE ADDRESSED BY NAME, NOT BY POSITION. The Director and Narrative swapped
places when the office moved to the top row so its window could face outside;
the concept still shows the old arrangement. AT below is where each room sits IN
THE CONCEPT, and the renderer places what comes back by name, so the swap costs
nothing here.

NOTHING LIVING IS DRAWN INTO A BACKDROP. Characters, the nameplate and the radio
are drawn by the renderer every frame - the cast moves, the plate is generated
from the seat table, and the radio has two states. A backdrop that baked any of
them in would show a second stationary copy.

Run:
    python scripts/gen_floor_rooms.py              # every room missing
    python scripts/gen_floor_rooms.py audio qa     # named rooms
    python scripts/gen_floor_rooms.py --force art  # re-buy
"""
from __future__ import annotations

import concurrent.futures as futures
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _floorpaths import sandbox, FLOOR_IMG  # noqa: E402

from PIL import Image  # noqa: E402

from bgate_adapters import kie  # noqa: E402

ART_ROOT = sandbox()
CONCEPT = ART_ROOT / ".bgate" / "refs" / "floor-concept.r1.png"

# public/, not static/: the build copies public -> bgate_ui/static, so art
# written straight to static disappears on the next `npm run build`.
OUT = FLOOR_IMG / "rooms"
RAW = ART_ROOT / ".bgate_out" / "art" / "floor-rooms"
CROP = ART_ROOT / ".bgate_out" / "art" / "floor-crops"

MODEL = "nano-banana-2"
TIMEOUT = 300

CELL = 18          # concept pixels per plan cell - see the memo above
ROOM_W = 8         # in cells, from floorplan.ts
ROOM_H = 7

# STORED AT 48 PIXELS PER CELL, about twice what the pane draws a cell at on a
# large display. Enough that the backdrop never softens when the rail is dragged
# wide, and small enough that nine of them are a few hundred kilobytes.
SCALE = 48
DEST = (ROOM_W * SCALE, ROOM_H * SCALE)      # 384 x 336

# 8:7 is 1.143 and nano-banana-2 offers 5:4 or 1:1 either side of it. 5:4 is the
# nearer, and the 9% squash back to 8:7 is invisible on furniture; generating
# square and cropping would clip whatever sits against the side walls.
RATIO = "5:4"

# Where each room sits in the CONCEPT, in cells. Pre-swap: narrative top-centre,
# director bottom-centre.
AT: dict[str, tuple[int, int]] = {
    "audio": (0, 0),    "narrative": (11, 0),  "cinematic": (22, 0),
    "gameplay": (0, 10), "lounge": (11, 10),   "qa": (22, 10),
    "art": (0, 20),     "director": (11, 20),  "tech": (22, 20),
}

# One line per room, naming what the room IS. The crop carries the layout and
# the palette; this only has to stop the model reinterpreting the subject.
WHAT: dict[str, str] = {
    "audio": "a sound studio: mixing console, wall of rack gear, waveform "
             "screens, a synthesizer and microphone stands",
    "narrative": "a writers' room: bookcases, a writing desk under a corkboard "
                 "of pinned index cards, a reading chair",
    "cinematic": "a video studio: a bright green screen backdrop, a camera on "
                 "a tripod, studio lights and an editing bay",
    "gameplay": "a playtest room: a large television on a stand, a sofa, a low "
                "table with controllers and shelves of games",
    "lounge": "a staff lounge with a warm wooden floor: sofas around a low "
              "table, a record player, a kitchenette",
    "qa": "a test lab: a bench of phones and tablets in stands, a wall of "
          "monitors showing charts, racks of paper reports",
    "art": "an artist's studio: an easel with a canvas, a drawing desk with a "
           "tablet and a pink lamp, a rack of colour swatches, a pinboard",
    # NO WINDOW. The office sits in the BOTTOM row, so the wall the picture
    # draws backs onto a corridor - a skyline in it is a view into a hallway,
    # which reads as a mistake before it reads as a view. The concept has one
    # there, so the prompt has to take it out explicitly rather than just not
    # ask for it. What replaces it is what the concept already puts either side:
    # the ego wall.
    "director": "an executive office with NO WINDOWS AT ALL: a big executive "
                "desk facing the room, and behind it a solid interior back "
                "wall of dark wood panelling carrying shelves of gold "
                "trophies, framed awards and a globe. Where the reference "
                "shows a window onto a city skyline, draw plain panelled "
                "interior wall instead - this room is in the middle of the "
                "building and has no view out",
    "tech": "a server room: tall racks of blinking servers, a desk of code "
            "monitors, cable trays and a patch panel",
}

# WHICH WALL HAS THE DOOR IN IT, phrased for the prompt.
#
# THE RENDERER PUNCHES THE DOORWAY, THIS ONLY RESERVES THE WALL FOR IT. The
# plan owns where every door is and draws the opening itself, which is what
# keeps a doorway correct when a room moves; what the plan cannot do is stop the
# model parking a bookcase exactly there. Narrative, Art and Tech all came back
# with their back wall fully dressed, so the punched opening cut a hole through
# shelving and read as damage rather than as a door.
#
# Only rooms whose door is in a wall the PAINTING supplies appear here. A door
# in the south wall needs no clearance: the camera looks from the south, so the
# picture has no near wall to keep clear.
CLEAR: dict[str, str] = {
    "director": "BACK wall (the horizontal wall across the top)",
    "art": "BACK wall (the horizontal wall across the top)",
    "tech": "BACK wall (the horizontal wall across the top)",
}


# THE CLAUSE GOES IN TWICE, AT THE TOP AND AT THE BOTTOM. Once at the end was
# not enough: Art came back with its desk squarely in the reserved stretch, and
# the punched doorway cut a hole through it. Early placement is where a long
# prompt is actually weighted, and the repeat at the end is cheap.
DOORWAY_FIRST = """

BEFORE ANYTHING ELSE - THE DOORWAY. The middle third of the {clear} must be
completely bare: plain empty wall with NOTHING hung on it, and NOTHING standing
against it or in front of it. No desk, no shelf, no bookcase, no cabinet, no
board, no picture, no plant, no equipment. This is where the door goes. If the
reference has furniture there, move that furniture along the wall to one side.
Leave the floor in front of that stretch clear too, so there is a way in.
"""

DOORWAY_LAST = """

FINALLY, CHECK THE DOORWAY AGAIN: the middle third of the {clear} is bare wall
with clear floor in front of it, and nothing is standing there."""

# THE BACK WALL'S DEPTH IS CORRECTED AFTER THE FACT, NOT ASKED FOR.
#
# The model picks it and picks differently every time - across the nine rooms
# the wall base landed anywhere from 0.7 cells to 3.1 - and a room whose wall
# runs three cells deep has half the floor of its neighbour and a horizon well
# below theirs, so a row of them reads as though the camera moved. Asking for a
# shallower wall in words was tried on the office and changed nothing.
#
# gen_floor_layers.level_wall does it by measurement instead: it finds where the
# wall actually meets the floor and slides the picture up to put that line where
# it belongs. See there for why that is safe to do to a finished drawing.
TEMPLATE = """Redraw this room interior at high resolution.

The reference image IS this room. Keep it: the same furniture, in the same
places, at the same sizes, in the same colours. This is {what}.

Draw it larger and cleaner, not different. Same limited dark palette, same
chunky pixel-art style, same soft interior lighting with the light coming from
the upper left. The second reference shows the whole floor this room belongs to;
match its style exactly.

THE LAYOUT IS FLAT AND SQUARE ON, AND THIS MATTERS MORE THAN ANY OTHER
INSTRUCTION. The back wall is one straight horizontal band across the TOP of the
image. The left and right walls are straight vertical bands at the LEFT and
RIGHT edges. The floor is a plain rectangle filling everything between them, and
its edges are parallel to the edges of the image. The camera looks down at about
70 to 75 degrees and is centred on the room.

Do NOT draw the room in perspective. Do NOT let the walls converge, tilt, taper
or vanish toward a point. Do NOT rotate the room or view it from a corner. Do
NOT draw it as a 45-degree isometric box. Every wall meets the floor in a line
that is either exactly horizontal or exactly vertical. Furniture stands on that
floor seen from above and slightly in front, exactly as in the reference.

Fill in detail the small version could not hold - the grain of the floor, the
edges of the furniture, what is on the walls - but do not add new furniture, do
not rearrange anything, and do not change the room's colour.

LEAVE IT EMPTY OF PEOPLE. No characters, no figures, no animals, no one seated
or standing anywhere in the room.

NO CEILING. Do not draw a ceiling light, a skylight, a hanging lamp, a glowing
panel, a window in the ceiling or any bright fixture across the top of the
room. The top of this image is a WALL, not a ceiling. The building's ceiling
lights are drawn separately and one baked into this room would be a second
stationary copy sitting over the wall.

NO TEXT ANYWHERE. No room label, no sign, no lettering on the walls, no
watermark, no caption, no border.

Draw only the room itself, filling the whole frame edge to edge, exactly as the
reference is framed: the floor, the furniture, and the walls at the top and the
sides. No margin, no drop shadow, no background outside the room."""

_URL: dict[str, str] = {}
_LOCK = threading.Lock()


def url_for(path: Path) -> str:
    """A kie URL for a local file, uploaded at most once per run.

    The lock spans the upload rather than only the dict write: every worker
    wants the same concept anchor, and a check-then-upload that released in
    between would put all nine into their own upload of it.
    """
    key = str(path)
    with _LOCK:
        if key not in _URL:
            _URL[key] = kie.upload_file(key, root=str(ART_ROOT))["url"]
        return _URL[key]


def crop_for(room: str) -> Path:
    """This room, cut out of the concept and enlarged for the model.

    Enlarged with NEAREST and not sent at its native 144x126: an upload that
    small is below what the model treats as a real reference, and NEAREST is
    what keeps the pixel grid legible instead of handing it a blurred hint.
    """
    cx, cy = AT[room]
    dst = CROP / f"{room}.png"
    if dst.exists():
        return dst
    con = Image.open(CONCEPT).convert("RGB")
    box = (cx * CELL, cy * CELL, (cx + ROOM_W) * CELL, (cy + ROOM_H) * CELL)
    piece = con.crop(box).resize((ROOM_W * CELL * 6, ROOM_H * CELL * 6),
                                 Image.NEAREST)
    dst.parent.mkdir(parents=True, exist_ok=True)
    piece.save(dst)
    return dst


def _prompt(room: str) -> str:
    """The room's prompt, with the doorway reservation wrapped around it."""
    body = TEMPLATE.format(what=WHAT[room])
    if room not in CLEAR:
        return body
    where = CLEAR[room]
    return (DOORWAY_FIRST.format(clear=where).lstrip("\n") + "\n" + body
            + DOORWAY_LAST.format(clear=where))


def one(room: str, force: bool) -> dict:
    dst = OUT / f"{room}.png"
    if dst.exists() and not force:
        return {"room": room, "ok": True, "skipped": True}

    raw = RAW / f"{room}.png"
    started = time.monotonic()
    if not raw.exists() or force:
        raw.parent.mkdir(parents=True, exist_ok=True)
        res = kie.generate_image(
            _prompt(room),
            str(raw), model=MODEL,
            aspect_ratio=RATIO, timeout=TIMEOUT, root=str(ART_ROOT),
            logical_name=f"floor-room-{room}",
            # The room's own crop FIRST. It is the subject; the whole-floor
            # concept behind it is only there to hold the style steady across
            # nine separately-bought rooms.
            image_urls=[url_for(crop_for(room)), url_for(CONCEPT)])
        if not res.get("ok", True) or not raw.exists():
            return {"room": room, "ok": False,
                    "error": res.get("error", "no file came back")}

    im = Image.open(raw).convert("RGB")
    # LANCZOS down, not NEAREST. The model returns a large soft-edged drawing
    # rather than true pixel art, so this is a photographic downscale to the
    # stored size; NEAREST here drops every other row and aliases the grain.
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.resize(DEST, Image.LANCZOS).save(dst)
    return {"room": room, "ok": True, "src": im.size, "dest": DEST,
            "seconds": round(time.monotonic() - started, 1)}


def main(argv: list[str]) -> int:
    force = "--force" in argv
    rooms = [a for a in argv if not a.startswith("--")] or list(AT)
    for r in rooms:
        if r not in AT:
            raise SystemExit(f"unknown room: {r} (have {', '.join(AT)})")
    if not CONCEPT.exists():
        raise SystemExit(f"the concept anchor is missing: {CONCEPT}")

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{len(rooms)} room(s) -> {OUT}")
    bad = 0
    with futures.ThreadPoolExecutor(max_workers=5) as pool:
        for res in futures.as_completed(
                [pool.submit(one, r, force) for r in rooms]):
            d = res.result()
            if d.get("skipped"):
                print(f"  ·  {d['room']:<12} already on disk")
            elif d["ok"]:
                print(f"  ok {d['room']:<12} {d['src'][0]}x{d['src'][1]}"
                      f" -> {d['dest'][0]}x{d['dest'][1]}  {d['seconds']}s")
            else:
                bad += 1
                print(f"  XX {d['room']:<12} {d['error']}")
    print(f"done, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    from bgate_core.store import envfile
    envfile.load_env(str(ART_ROOT))
    sys.exit(main(sys.argv[1:]))
