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


ROOT = _sandbox()
CAST = ROOT / ".bgate_out" / "art" / "cast"
OUT = CAST / "anim"
FRAMES = CAST / "frames"          # the single drawings, kept: they are the
                                  # expensive part and a restitch must be free

# The same cast description gen_floor_cast_anims.py uses, and it has to STAY the
# same: it is what stops the model swapping a jacket colour and still believing
# it obeyed the reference.
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


def one_frame(name: str, anim: str, i: int, refs: list[str],
              provider: str) -> dict:
    """Buy one drawing, unless it is already on disk."""
    spec = ANIMS[anim]
    out = frame_path(name, anim, i)
    if out.exists():
        return {"ok": True, "skipped": True, "path": str(out)}
    prompt = TEMPLATE.format(who=WHO[name], stance=spec["stance"],
                             beat=spec["beats"][i])
    if len(refs) > 1:
        prompt += CYCLE
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
    cols, rows = ANIMS[anim]["grid"]
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
               for i in range(len(ANIMS[anim]["beats"])))


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

    total = sum(sum(1 for i in range(len(ANIMS[a]["beats"]))
                    if not frame_path(n, a, i).exists()) for n, a in jobs)
    print(f"{len(jobs)} sheets, {total} drawings to buy on {provider}",
          flush=True)

    bad = 0
    for n, a in jobs:
        sheet_ref = str(CAST / f"{n}-sheet.png")
        if not Path(sheet_ref).is_file():
            print(f"FAIL {n}: no model sheet to anchor on", flush=True)
            bad += 1
            continue
        # FRAME 1 FIRST, ALONE, because it is the anchor every other frame of
        # this animation is drawn against. Running the whole cycle in parallel
        # off the model sheet alone is what makes a set instead of a cycle.
        first = one_frame(n, a, 0, [sheet_ref], provider)
        if not first.get("ok"):
            print(f"FAIL {n}-{a} frame 1: {first.get('error')}", flush=True)
            bad += 1
            continue
        print(f"ok   {n}-{a} 1  {first.get('seconds')}s", flush=True)
        refs = [sheet_ref, str(frame_path(n, a, 0))]
        rest = range(1, len(ANIMS[a]["beats"]))
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
    from bgate_core import envfile
    envfile.load_env(str(ROOT))
    sys.exit(main())
