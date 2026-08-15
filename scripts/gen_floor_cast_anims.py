"""Generate one sheet PER ANIMATION per floor-cast character, in the bg-testbed
sandbox, on nano-banana-2 conditioned on the character's EXISTING model sheet.

The other half of scripts/slice_floor_cast.py: this buys the sheets, that one
cuts them into the strips the floor pane steps through. Run it, LOOK at what came
back, put the good ones in that script's PASS set and the bad ones in its RETRY
map with the reason, then run the slicer.

WHICH ACCOUNT SERVES THE MODEL. nano-banana-2 is reachable through both kie and
krea and the model is the same; which one answers is an account fact, not a
quality one, so `--provider` picks and the default is krea. As of the run that
produced the current cast: kie was BLOCKED - both /api/v1/jobs/createTask and the
file-upload endpoint answer "unusual account activity" (500 and 401) while
kie_status still reports available, because that check only proves the key
parses - and krea's API balance ran out 27 sheets in. openai is the third leg and
it refuses this shape by design (see the multi-pose guard in
bgate_adapters.imagegen), which is worth knowing before reaching for it.

WHY THE ADAPTER DIRECTLY AND NOT THE image_generate TOOL. Two reasons, both about
this being a 45-call batch:
  - the tool's timeout is a hardcoded 300s and a cold job lands at ~310s, so the
    call "fails" after the job has already been paid for. The spend ledger is
    still written either way, because krea.generate is what writes it.
  - the tool appends the project's art-direction clause, which for bg-testbed
    reads "angled 3/4 isometric view, 2:1 tile geometry". That CONTRADICTS the
    camera the cast is already drawn to and that the floor pane counter-rotates
    by. The reference won that argument in the probe, but a prompt that argues
    with itself is not something to buy forty-five times.

WHY THE REFERENCE IS THE OLD SHEET AND NOT A SINGLE POSE. The 3x2 sheet is a
model sheet: it shows the same person standing, sitting, walking and holding a
page. Handing all six poses over as one reference is what keeps the identity from
drifting when the new sheet asks for a stance none of them contains.

WHAT THIS APPROACH GETS WRONG, because the next person will hit it. Asking for a
whole cycle as one grid is cheap - one call per animation instead of one per
frame - and it fails about a third of the time, in a way no check here can catch:
handed a 3x2 grid to fill and a reference that IS a 3x2 pose sheet, the model
sometimes redraws the POSE SHEET. One walk came back with a correct top row and a
VR headset, an arcade cabinet, the words "game over" and a trophy along the
bottom. Every frame is a plausible picture of the right character, so only a
human looking at it knows. The expensive fix is one generation PER FRAME, each
anchored on the same reference and stitched afterwards, which is what
image_sprites already does and what imagegen's multi-pose guard is pointing at.
Reach for it when a sheet fails twice.
"""
from __future__ import annotations

import concurrent.futures as futures
import sys
import time
from pathlib import Path

# The repo this script lives in, so `bgate_adapters` imports without the
# caller having installed anything.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bgate_adapters import krea  # noqa: E402

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

# The cast, in the order the contact sheet reads. Each line is what must NOT
# change between the reference and the new frames - written from looking at the
# existing sheets, because "same as the reference" alone lets a model quietly
# swap a jacket colour and still believe it obeyed.
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

# FRAME COUNTS ARE PER ANIMATION AND THEY ARE NOT ALL EIGHT. Eight is right for
# the walk, which is the one cycle a reader can see stepping wrong. Past about
# eight cells the model starts losing the grid itself - cells drift in size and
# the figure changes scale between them - and a sheet that does not divide
# cleanly is worse than a shorter one, because steps() slices on arithmetic and
# has no way to notice. So each animation gets the most frames its motion
# actually needs, in a grid whose cells come out near-square.
#
# (cols, rows, size handed to krea, what the animation does)
ANIMS = {
    "idle": (3, 2, "1536x1024", """A 6-FRAME BREATHING IDLE, standing still and facing the viewer, in this order:
1 neutral stance, chest neutral, arms relaxed at the sides
2 breathing in, chest and shoulders lifting slightly, head a pixel higher
3 top of the breath, chest fullest, shoulders highest
4 beginning to breathe out, shoulders starting to drop
5 breathing out, chest settling, shoulders low
6 bottom of the breath, the lowest point, about to rise again

The feet do not move at all. Nothing changes but the chest, the shoulders, the
head height and a small sway of the hair and clothing. The whole movement is
two or three pixels. Frame 6 must lead cleanly back into frame 1 so the cycle
loops seamlessly."""),

    "sitting": (2, 2, "1024x1024", """A 4-FRAME SITTING IDLE. The character is seated on a dark office swivel chair,
turned three-quarters towards the viewer, hands resting in the lap, exactly as
in the seated pose of the reference sheet. In this order:
1 settled in the chair, neutral
2 breathing in, torso lifting slightly
3 top of the breath, head a pixel higher
4 breathing out, torso settling back to neutral

The chair does not move and the feet do not move. Only the torso, head and hair
shift, by two or three pixels. Frame 4 must lead cleanly back into frame 1 so
the cycle loops seamlessly."""),

    "walk": (4, 2, "1792x1024", """AN 8-FRAME WALK CYCLE MOVING TO THE VIEWER'S LEFT, walking on the spot, in this
order:
1 contact, left leg forward with the heel down, right leg back, arms opposed
2 down, weight sinking onto the front leg, body at its lowest
3 passing, the rear leg swinging through under the body, body rising
4 up, body at its highest, the back leg pushing off the toe
5 contact mirrored, right leg forward with the heel down, left leg back
6 down mirrored, body at its lowest again
7 passing mirrored, the other leg swinging through
8 up mirrored, body at its highest again

The arms swing opposite the legs throughout. The clothing and hair shift with
the stride. The body bobs up and down by only a few pixels. Frame 8 must lead
cleanly back into frame 1 so the cycle loops seamlessly."""),

    "working": (3, 2, "1536x1024", """A 6-FRAME WORKING CYCLE. The character is seated on a dark office swivel chair
at a desk, turned three-quarters towards the viewer, both hands raised in front
and TYPING on a keyboard, exactly the seated working pose in the reference
sheet. In this order:
1 left hand down on the keys, right hand raised
2 both hands mid-travel, torso leaning in slightly
3 right hand down on the keys, left hand raised
4 both hands mid-travel, head dipping slightly towards the work
5 left hand down on the keys again, right hand raised higher
6 both hands mid-travel, torso returning to neutral

The chair, the legs and the feet do not move. Only the hands, forearms, head and
torso move, and only by a few pixels. Frame 6 must lead cleanly back into frame 1
so the cycle loops seamlessly."""),

    "handoff": (2, 2, "1024x1024", """A 4-FRAME HANDOFF. The character stands facing the viewer holding a small white
sheet of paper, and offers it forward, exactly the pose in the reference sheet.
In this order:
1 standing, the page held down at the side
2 raising the page, arm bending up, beginning to turn towards the viewer
3 arm extended forward, offering the page out at chest height
4 arm still extended, the page held out, weight settled, waiting

The feet do not move. Frame 4 is the hold, so it must read as a pose that can
sit on screen. The page stays the same size and the same white in all four
frames."""),
}

# THE FIXED HALF OF EVERY PROMPT. The camera and the background are both
# LOAD-BEARING and neither is a style note: the cast is already drawn to a high
# three-quarter camera and the floor pane counter-rotates the sprites by that
# same angle, so a frame drawn isometric would be drawn with the camera applied
# twice; and the flat navy field is what the slicer floods out to alpha, so a
# gradient or a floor texture would key into holes.
TEMPLATE = """A pixel-art SPRITE SHEET laid out as a strict {cols}-column by {rows}-row grid: \
{frames} frames total, read left to right along the top row then left to right along the bottom row.

THE CHARACTER IS THE ONE IN THE REFERENCE IMAGE AND MUST NOT CHANGE. It is {who}. \
Same face, same hair, same clothing, same colours, same proportions, same pixel-art rendering, \
same outline weight, same palette as the reference. Do not restyle, do not redesign, do not \
reproportion, do not age. This is the SAME character as the reference, drawn in new frames.

CAMERA: the identical high three-quarter top-down view used in the reference image, roughly 70 to \
75 degrees, looking down at the character from above and in front. Copy the reference's camera \
exactly. Not a flat overhead plan view, not a straight-on front view, not isometric.

BACKGROUND: one flat solid dark slate-navy fill, RGB 34 44 53, edge to edge, exactly as in the \
reference. No gradient, no vignette, no floor, no tiles, no walls, no props other than the ones \
named below, no border lines, no grid lines, no separators or gutters between cells, no text, no \
numbers, no labels, no watermark.

LAYOUT: all {frames} cells are exactly the same size and fill the image edge to edge with no \
margin. In every cell the character is at the SAME scale, horizontally centred, with the feet at \
the SAME height from the bottom of the cell. The character must not drift, grow, shrink or slide \
between cells.

{action}
"""

WORKERS = 4          # enough to keep the batch under an hour, gentle enough
                     # that neither provider has rate-limited it
TIMEOUT = 900.0      # a warm job takes ~14s and a cold one ~310s, which is what
                     # made the 300s default fail jobs that had already been paid


def one(name: str, anim: str, provider: str) -> dict:
    cols, rows, size, action = ANIMS[anim]
    out = OUT / f"{name}-{anim}.png"
    if out.exists():
        # SKIP RATHER THAN OVERWRITE, so a rerun after a provider ran out of
        # credit costs nothing for the sheets that already landed. Delete the
        # file to force a regeneration - which is also how a sheet on the
        # slicer's RETRY list gets another go.
        return {"ok": True, "skipped": True, "path": str(out)}
    prompt = TEMPLATE.format(cols=cols, rows=rows, frames=cols * rows,
                             who=WHO[name], action=action)
    refs = [str(CAST / f"{name}-sheet.png")]
    started = time.monotonic()
    if provider == "kie":
        from bgate_core import chroma
        # Through chroma rather than the kie adapter, because kie's reference
        # field is a URI and chroma is what uploads a local anchor to get one.
        res = chroma.generate(prompt, str(out), provider="kie",
                              model="nano-banana-2", size=size,
                              ref_paths=refs, timeout=TIMEOUT, root=str(ROOT),
                              logical_name=f"{name}-{anim}")
    else:
        res = krea.generate(prompt, str(out), model="nano-banana-2", size=size,
                            ref_paths=refs, ref_strength=0.85,
                            timeout=TIMEOUT, root=str(ROOT))
    res["seconds"] = round(time.monotonic() - started, 1)
    return res


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    argv = sys.argv[1:]
    provider = "krea"
    if "--provider" in argv:
        i = argv.index("--provider")
        provider = argv[i + 1]
        del argv[i:i + 2]
    jobs = [(n, a) for n in WHO for a in ANIMS]
    if argv:
        jobs = [(n, a) for n, a in jobs if n in argv or a in argv
                or f"{n}-{a}" in argv]
    print(f"{len(jobs)} sheets on {provider}", flush=True)
    bad = 0
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fut = {pool.submit(one, n, a, provider): (n, a) for n, a in jobs}
        for f in futures.as_completed(fut):
            n, a = fut[f]
            try:
                r = f.result()
            except Exception as exc:                       # noqa: BLE001
                bad += 1
                print(f"FAIL {n}-{a}: {type(exc).__name__}: {exc}", flush=True)
                continue
            if r.get("skipped"):
                print(f"skip {n}-{a}", flush=True)
            elif r.get("ok"):
                print(f"ok   {n}-{a}  {r.get('seconds')}s  "
                      f"${r.get('estimated_usd')}", flush=True)
            else:
                bad += 1
                print(f"FAIL {n}-{a}: {r.get('error')}", flush=True)
    print(f"done, {bad} failed of {len(jobs)}")
    return 1 if bad else 0


if __name__ == "__main__":
    # THE KEYS ARE LOADED BEFORE ANY THREAD STARTS. The first run lost three
    # sheets to a race on exactly this: the earliest workers reached the adapter
    # while the project's .env was still being read and were told
    # "KREA_API_KEY not set" for a key that was there the whole time. Two of the
    # three did not even fail cleanly - the adapter's error path went looking for
    # a 'reason' the half-built availability dict did not have yet.
    from bgate_core import envfile
    envfile.load_env(str(ROOT))
    sys.exit(main())
