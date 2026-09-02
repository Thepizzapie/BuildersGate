"""Canonical animation plans — the key poses, in order, with their timing.

WHY A CATALOGUE AND NOT A PROMPT. ``image_sprites`` takes whatever list of poses
it is handed. That means the quality of a character's animation is decided by
whether the agent driving it happened to remember how a walk cycle is built, and
the failure mode is not a broken sheet — it is a sheet that assembles perfectly
and animates like nothing alive. Four frames named walk/0..walk/3 described as
"walking, left foot forward", "walking", "walking, right foot forward",
"walking" is a legal, expensive, useless animation, and every gate in this
pipeline passes it: the identity holds, the palette holds, the cut is clean.

Animation has had the answer to this since the 1930s and it is not a matter of
taste. A walk is built from four key poses — CONTACT, DOWN, PASSING, UP — played
once per leg. An attack is ANTICIPATION, CONTACT, FOLLOW-THROUGH, RECOVER, and
the impact frame is HELD while the anticipation is rushed, because that contrast
is what makes a hit feel like it landed. Those are the poses this module hands
back, described tightly enough that an image model draws the right one.

TIMING IS PART OF THE PLAN, NOT A PROPERTY OF THE SHEET. Godot 4's SpriteFrames
gives every frame a relative ``duration`` — a frame at 2.0 holds twice as long as
one at 1.0, and the emitter wrote a literal 1.0 for every frame ever produced
here. A uniform hold is the flattest possible reading of any action: it is why a
generated punch reads as a slideshow of a punch. The holds below are where the
weight goes.

WHAT IS DERIVED RATHER THAN BOUGHT. The art brief's first rule is generate the
minimum and derive the rest, and a ping-pong loop is that rule applied to timing:
frames 0,1,2 played 0,1,2,1 is a four-step cycle from three generations, it
CANNOT have an open-loop seam (the wrap-around pair is a real adjacent pair), and
it is what a breathing idle or a hovering pickup actually wants. Godot has no
ping-pong loop mode — the proposal for one is still open — so it is baked into
the emitted frame list, which is where it belongs anyway: the plan should be in
the resource, not in the gameplay code that plays it.

Nothing here calls a model, and nothing here spends. It is a lookup table with
its reasoning attached.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# Each entry:
#   frames    [(pose_description, relative_hold)] in play order
#   loop      does it repeat (False emits "loop": false and holds the last frame)
#   pingpong  play forward then back — a cycle from half the drawings
#   fps       the speed this action reads at, before the holds redistribute it
#   why       one line a human can check the plan against
#
# The descriptions are written to be dropped straight into image_sprites' pose
# `description`, which the tool prefixes with "now in this stance:". They name
# limb positions and weight, never adjectives — "leaning back" is a pose, "more
# dynamic" is not, and the second one gets you a different character.

ARCHETYPES: dict[str, dict] = {
    "idle": {
        "frames": [
            ("standing at rest, weight settled evenly, shoulders at their "
             "lowest, arms hanging relaxed at the sides, chin level", 1.4),
            ("the same standing rest pose, chest expanded slightly on an "
             "inhale, shoulders risen a little, head a fraction higher — "
             "identical stance and foot placement, only the breath differs", 1.0),
            ("the same standing rest pose at the top of the inhale, chest "
             "fullest, shoulders highest, arms hanging identically — feet have "
             "not moved at all", 1.4),
        ],
        "loop": True, "pingpong": True, "fps": 6.0,
        "why": "A breathing idle is three drawings played four ways. Ping-pong "
               "makes the cycle close by construction, and a standing character "
               "who never quite stops moving is the cheapest signal in a game "
               "that it is not paused.",
    },
    "walk": {
        "frames": [
            ("CONTACT, left leg leading: left heel just touching the ground "
             "ahead, right toe still touching behind, legs at their widest "
             "split, body at mid height, right arm forward and left arm back", 1.0),
            ("DOWN, the recoil: weight has dropped onto the leading left leg, "
             "that knee bent to absorb it, body at its LOWEST point of the "
             "cycle, back foot lifting, arms passing toward neutral", 1.0),
            ("PASSING: the right leg swings through directly beneath the body, "
             "knee bent and foot just clearing the ground, left leg straight and "
             "carrying all the weight, body at mid height, arms near neutral at "
             "the sides", 1.0),
            ("UP, the high point: the supporting left leg straightens and pushes "
             "the body to its HIGHEST point of the cycle, right leg swinging "
             "forward, arms beginning to reverse", 1.0),
            ("CONTACT, right leg leading: mirror of the first pose — right heel "
             "touching ahead, left toe behind, widest split, left arm forward "
             "and right arm back", 1.0),
            ("DOWN, the recoil on the right: weight dropped onto the leading "
             "right leg, knee bent, body at its LOWEST point, arms toward "
             "neutral", 1.0),
            ("PASSING on the left: left leg swings through beneath the body, "
             "knee bent, right leg straight and carrying the weight, body at mid "
             "height, arms neutral", 1.0),
            ("UP on the right: supporting right leg straightens, body at its "
             "HIGHEST point, left leg swinging forward, arms reversing", 1.0),
        ],
        "loop": True, "pingpong": False, "fps": 10.0,
        "why": "The four key poses — contact, down, passing, up — played once per "
               "leg. The body rises and falls TWICE per cycle and that bob is "
               "what a walk actually is; a set of four 'walking' frames with no "
               "height change reads as a character sliding along the floor.",
    },
    "walk4": {
        "frames": [
            ("CONTACT, left leg leading: left heel touching ahead, right toe "
             "behind, legs at their widest split, right arm forward", 1.0),
            ("PASSING on the right: right leg swings through beneath the body, "
             "knee bent, left leg straight and carrying the weight, body at its "
             "highest, arms near neutral", 1.0),
            ("CONTACT, right leg leading: right heel touching ahead, left toe "
             "behind, widest split, left arm forward", 1.0),
            ("PASSING on the left: left leg swings through beneath the body, "
             "knee bent, right leg straight and carrying the weight, body at its "
             "highest, arms near neutral", 1.0),
        ],
        "loop": True, "pingpong": False, "fps": 8.0,
        "why": "The two-pose walk: contact and passing, once per leg. Half the "
               "spend of the eight-frame version and the standard for small "
               "sprites, where the down/up recoil is a pixel or two and does not "
               "survive the downscale anyway. Use 'walk' for a hero at size.",
    },
    "run": {
        "frames": [
            ("CONTACT: leading foot striking the ground hard beneath a forward-"
             "leaning torso, trailing leg folded up behind, opposite arm driving "
             "forward across the body, elbows sharply bent", 1.0),
            ("DOWN, the compression: full weight over the bent supporting leg, "
             "body at its LOWEST, trailing leg swinging through, torso pitched "
             "further forward", 1.0),
            ("PASSING into the push: supporting leg extending, other knee driving "
             "up in front, arms at their extremes", 1.0),
            ("AIRBORNE, the flight frame: BOTH feet clear of the ground, body at "
             "its HIGHEST, legs scissored wide, arms fully counter-swung", 1.2),
            ("CONTACT on the other side: mirror of the first pose, opposite foot "
             "striking, opposite arm forward", 1.0),
            ("DOWN on the other side: weight over the bent leg, body at its "
             "LOWEST, torso pitched forward", 1.0),
            ("PASSING on the other side: supporting leg extending, opposite knee "
             "driving up", 1.0),
            ("AIRBORNE on the other side: both feet clear, body at its HIGHEST, "
             "legs scissored, arms counter-swung", 1.2),
        ],
        "loop": True, "pingpong": False, "fps": 12.0,
        "why": "A run is a walk with a FLIGHT frame — the moment neither foot "
               "touches. If every frame has a foot down it is a fast walk, "
               "whatever the fps says, and players read the difference "
               "immediately.",
    },
    "attack": {
        "frames": [
            ("ANTICIPATION: winding back away from the target, weapon or fist "
             "drawn behind the body, weight loaded onto the back foot, shoulders "
             "rotated away — the whole body coiled in the OPPOSITE direction to "
             "the strike", 0.7),
            ("CONTACT, the strike landing: arm or weapon fully extended at the "
             "target, hips and shoulders squared through, weight driven onto the "
             "front foot, body at full reach", 2.0),
            ("FOLLOW-THROUGH: the strike carried past the target, arm crossing "
             "the body, torso over-rotated, weight fully committed forward", 1.0),
            ("RECOVER: returning toward the neutral stance, arm coming back, "
             "weight resettling between the feet, still slightly off balance", 1.4),
        ],
        "loop": False, "pingpong": False, "fps": 12.0,
        "why": "The impact frame holds twice as long as the wind-up is quick, and "
               "that ratio IS the feeling of a hit. Even holds turn the same four "
               "drawings into a slideshow. The anticipation frame is also the one "
               "an opponent reads to dodge, so a fighting game needs it drawn even "
               "though it is the frame nobody looks at in a still.",
    },
    "hurt": {
        "frames": [
            ("the instant of impact: head snapped back, torso recoiling away from "
             "the blow, arms flung outward, both feet still planted", 1.6),
            ("the stagger: weight fallen back onto the rear foot, body folded "
             "forward over the hit, arms drawn in protectively, head down", 1.0),
        ],
        "loop": False, "pingpong": False, "fps": 12.0,
        "why": "Two frames is enough for a hit reaction and more is usually worse "
               "— the animation has to be interruptible by the next hit, so a long "
               "one either gets cut off mid-swing or locks the player out of their "
               "own controls.",
    },
    "death": {
        "frames": [
            ("the killing blow landing: body arched backward, head thrown back, "
             "arms flung wide, still on both feet", 1.0),
            ("balance lost: knees buckling, body folding downward, arms falling, "
             "head dropping forward", 1.0),
            ("collapsing: down on one knee and a hand, torso pitched over, weight "
             "no longer carried by the legs", 1.0),
            ("on the ground: lying flat, limbs slack and settled, head turned "
             "aside, nothing supported", 3.0),
        ],
        "loop": False, "pingpong": False, "fps": 8.0,
        "why": "One-shot, and the last frame holds three times as long because it "
               "is the frame that stays on screen. A death that loops stands the "
               "character back up forever, which is why 'death' is in the "
               "never-loop list in the emitter as well as here.",
    },
    "jump": {
        "frames": [
            ("the crouch before the leap: knees deeply bent, arms swung back "
             "behind the hips, torso folded forward, both feet flat and planted", 0.8),
            ("the launch: legs explosively straightened, arms thrown up overhead, "
             "body fully extended and stretched tall, toes just leaving the "
             "ground", 0.8),
            ("APEX: at the top of the arc, legs tucked up beneath the body, arms "
             "coming down, torso compact — the moment of least upward speed", 1.6),
            ("falling: legs reaching down for the ground, arms out for balance, "
             "torso upright, body stretched downward", 1.0),
            ("the landing: feet planted, knees deeply bent absorbing the impact, "
             "torso folded forward over them, arms forward for balance", 1.2),
        ],
        "loop": False, "pingpong": False, "fps": 10.0,
        "why": "The apex holds longest, which is both physically true (least "
               "vertical speed) and what makes a jump feel like it hangs. Note "
               "that the sprite assembler ALSO lifts jump frames along an arc so "
               "the feet actually leave the floor — draw the poses, not the "
               "height.",
    },
    "cast": {
        "frames": [
            ("the gather: arms drawn in toward the chest, hands cupped together, "
             "head bowed over them, weight settled and still", 1.0),
            ("the charge: arms opening outward and up, hands apart, head lifting, "
             "back arching, weight rising onto the front of the feet", 1.2),
            ("the release: arms thrust forward at full extension, palms out, head "
             "up, weight driven onto the front foot, body committed forward", 2.0),
            ("the settle: arms lowering, weight coming back between the feet, "
             "head levelling, returning toward the neutral stance", 1.2),
        ],
        "loop": False, "pingpong": False, "fps": 10.0,
        "why": "Same anticipation/release/recover shape as an attack, stretched — "
               "a cast reads as slower than a punch because its wind-up is the "
               "part the player is meant to see and react to.",
    },
    "float": {
        "frames": [
            ("hovering at rest, body level, at the LOW point of its bob", 1.0),
            ("hovering at the mid point of the rise, body level, everything else "
             "identical", 1.0),
            ("hovering at the HIGH point of the bob, body level, everything else "
             "identical", 1.0),
        ],
        "loop": True, "pingpong": True, "fps": 6.0,
        "why": "For a pickup, a drone, a floating enemy. Three drawings, four "
               "steps, and the ping-pong makes the fall an exact reverse of the "
               "rise — which is what hovering IS, and what three separately "
               "generated 'falling' frames would never be.",
    },
}

# Aliases people actually type. Kept separate from the catalogue so the archetype
# names stay canonical and one concept never has two entries to keep in step.
ALIASES: dict[str, str] = {
    "idle_breath": "idle", "breathe": "idle", "stand": "idle",
    "walk8": "walk", "walkcycle": "walk", "walk_cycle": "walk",
    "sprint": "run", "running": "run",
    "punch": "attack", "swing": "attack", "slash": "attack", "melee": "attack",
    "hit": "hurt", "damage": "hurt", "flinch": "hurt",
    "die": "death", "ko": "death",
    "leap": "jump", "hop": "jump",
    "spell": "cast", "magic": "cast",
    "hover": "float", "bob": "float",
}


def resolve(archetype: str) -> Optional[str]:
    """Canonical archetype name for whatever the caller typed. None if unknown."""
    key = str(archetype or "").strip().lower().replace("-", "_")
    if key in ARCHETYPES:
        return key
    return ALIASES.get(key)


def catalog() -> list[dict]:
    """Every archetype with the numbers that decide whether to use it.

    ``generated`` is what the run will cost in image calls and ``steps`` is how
    many frames actually play — they differ exactly where ping-pong is doing its
    job, which is the comparison worth putting in front of whoever is paying.
    """
    out = []
    for key, spec in sorted(ARCHETYPES.items()):
        count = len(spec["frames"])
        out.append({
            "archetype": key,
            "generated": count,
            "steps": _pingpong_len(count) if spec["pingpong"] else count,
            "loop": spec["loop"],
            "pingpong": spec["pingpong"],
            "fps": spec["fps"],
            "why": spec["why"],
            "aliases": sorted(a for a, t in ALIASES.items() if t == key),
        })
    return out


def _pingpong_len(count: int) -> int:
    """Play length of a ping-pong cycle over `count` drawings: 0..n-1..1.

    The endpoints are NOT repeated — a cycle of 0,1,2,1 holds no frame twice in a
    row, while 0,1,2,2,1,0 stutters at both ends. That stutter is the usual
    hand-rolled mistake and it is visible at any speed.
    """
    return count if count < 3 else count * 2 - 2


def pingpong_order(count: int) -> list[int]:
    """Frame indices for a ping-pong cycle. [0,1,2] -> [0,1,2,1]."""
    if count < 3:
        return list(range(count))
    return list(range(count)) + list(range(count - 2, 0, -1))


def plan(archetype: str, *, anim: str = "", view: str = "",
         pingpong: Optional[bool] = None) -> dict:
    """The pose list and timing for one archetype, ready for ``image_sprites``.

    ``anim`` names the animation in the emitted resource and defaults to the
    archetype (so ``plan("attack", anim="uppercut")`` gives uppercut/0..3).
    ``view`` is prepended to every description — pass the project's camera
    convention ("side view, facing right", "three-quarter view from the front")
    once here rather than writing it into eight pose strings and getting it
    subtly different in two of them.

    Returns {archetype, anim, poses, timing, fps, loop, pingpong, generated,
    steps, why}. ``poses`` goes straight into the tool; ``timing`` goes into the
    emitter and is what puts the weight on the impact frame.
    """
    key = resolve(archetype)
    if not key:
        raise ValueError(
            f"unknown archetype {archetype!r} — one of "
            f"{', '.join(sorted(ARCHETYPES))} (aliases: "
            f"{', '.join(sorted(ALIASES))})")
    spec = ARCHETYPES[key]
    name = (anim or key).strip().split("/", 1)[0]
    prefix = (view.strip().rstrip(".") + ". ") if view.strip() else ""
    frames = spec["frames"]
    use_pingpong = spec["pingpong"] if pingpong is None else bool(pingpong)

    poses = [{"name": f"{name}/{i}", "description": prefix + description}
             for i, (description, _) in enumerate(frames)]
    holds = [hold for _, hold in frames]
    order = pingpong_order(len(frames)) if use_pingpong else list(range(len(frames)))
    return {
        "archetype": key,
        "anim": name,
        "poses": poses,
        "timing": {name: {"holds": holds, "order": order,
                          "loop": spec["loop"], "fps": spec["fps"]}},
        "fps": spec["fps"],
        "loop": spec["loop"],
        "pingpong": use_pingpong,
        "generated": len(frames),
        "steps": len(order),
        "why": spec["why"],
    }


def plans(archetypes: list[str], *, view: str = "") -> dict:
    """Several archetypes as ONE sprite set — poses concatenated, timing merged.

    This is the shape a character is actually ordered in: an idle, a walk and an
    attack in a single run, sharing one reference and one sheet. Duplicate
    archetypes are given numbered animation names rather than being refused,
    because two attacks is a normal thing to want.
    """
    poses: list[dict] = []
    timing: dict[str, dict] = {}
    used: dict[str, int] = {}
    chosen: list[str] = []
    for entry in archetypes:
        key = resolve(entry)
        if not key:
            raise ValueError(f"unknown archetype {entry!r}")
        seen = used.get(key, 0)
        used[key] = seen + 1
        name = key if not seen else f"{key}{seen + 1}"
        built = plan(key, anim=name, view=view)
        poses.extend(built["poses"])
        timing.update(built["timing"])
        chosen.append(name)
    return {
        "poses": poses, "timing": timing, "animations": chosen,
        "generated": len(poses),
        "steps": sum(len(t["order"]) for t in timing.values()),
    }
