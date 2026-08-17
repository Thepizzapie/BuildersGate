"""Optional feature modules — what a project can switch OFF.

The product grew every feature into every install: 200+ MCP tools in every
agent's context, a studio-floor pane, a music pipeline, a brainstorm room —
whether or not a project wanted them. For someone shipping a small 2D game,
half of that is bloat they never asked for; for every dispatched agent it is
paid context on every single turn for tools it will never call.

So features that are genuinely severable are MODULES, chosen at setup (the
first-run card, `bgate init --without ...`) and changeable later in Settings
(``modules.disabled``). A disabled module:

  * does not register its MCP tools (the token win — the registry is built
    per process, and the pinned project's choice decides what an agent sees);
  * hides its panes in the dashboard shell (delivered in the page bootstrap);
  * stops being graded by doctor, so a missing ffmpeg on a project that
    turned playtest capture off is not a red row;
  * names the pip extras it would need, so the wizard can say exactly what
    `pip install builders-gate[...]` buys back.

WHAT IS DELIBERATELY NOT A MODULE: the board, seats, the bible/lore/dialogue
canon, image generation and the Godot tools. Those are the product — a
"module" nobody can meaningfully ship without is a checkbox that only exists
to be mis-unchecked. The seat table also stays universal: a project that
disables cinematic keeps the cinematic SEAT defined (an empty chair costs
nothing), it just has no cinematic tools to dispatch against.
"""
from __future__ import annotations

from typing import Optional

# name -> what the module is and what it gates. `tools` are PREFIXES matched
# against MCP tool names at registration; `doctor` names the doctor rows that
# only this module needs; `extras` are the pip extras that light it up fully.
MODULES: dict[str, dict] = {
    "floor": {
        "label": "Studio floor",
        "blurb": "The animated office-floor view of your agents. Pure "
                 "spectacle — the board shows the same facts as a list. Its "
                 "art and ambience are the optional assets pack (~30MB).",
        "tools": (), "extras": ("floor",), "doctor": (),
    },
    "brainstorm": {
        "label": "Brainstorm rooms",
        "blurb": "Read-only thinking-partner sessions with shared drawing "
                 "pads, and Deploy to turn a plan into board items.",
        "tools": ("brainstorm_",), "extras": (), "doctor": (),
    },
    "music": {
        "label": "Music",
        "blurb": "Suno music generation through kie.ai — candidates, "
                 "audition, keep/discard. Needs a KIE key.",
        "tools": ("kie_music_", "music_"), "extras": (), "doctor": (),
    },
    "cinematic": {
        "label": "Cinematics",
        "blurb": "Storyboards, shot planning and video generation for "
                 "cutscenes. Needs a KIE key; assembly uses ffmpeg.",
        "tools": ("cinematic_", "storyboard_", "kie_video_"),
        "extras": (), "doctor": ("ffmpeg",),
    },
    "voice": {
        "label": "Voice",
        "blurb": "Text-to-speech for lines and read-alouds (Deepgram), and "
                 "speech-to-text for talking to the brainstorm room.",
        "tools": ("voice_",), "extras": ("voice", "stt"),
        "doctor": ("whisper",),
    },
    "playtest": {
        "label": "Playtest capture",
        "blurb": "Record playtests with telemetry, promote findings to the "
                 "board. Capture needs ffmpeg and the audio extra.",
        "tools": ("playtest_",), "extras": ("record",),
        "doctor": ("ffmpeg",),
    },
    "three_d": {
        "label": "3D pipeline",
        "blurb": "Blender modelling, rigging, sprite baking and "
                 "image-to-3D. Needs Blender installed.",
        "tools": ("blender_", "character_generate", "godot_retarget_check"),
        "extras": (), "doctor": ("blender",),
    },
}


def names() -> tuple[str, ...]:
    return tuple(MODULES)


def catalog() -> list[dict]:
    """The wizard's checklist: every module with its label, blurb and the
    exact pip command that lights it up fully ('' when none is needed)."""
    out = []
    for name, spec in MODULES.items():
        out.append({"name": name, "label": spec["label"],
                    "blurb": spec["blurb"], "pip": pip_hint((name,))})
    return out


def disabled(root) -> set[str]:
    """The project's switched-off modules. Unknown names are dropped rather
    than obeyed — a typo in a stored list must not silently disable the
    nearest real feature, and must not survive a rename as a ghost."""
    try:
        from bgate_core import settings as _settings

        stored = _settings.get(root, "modules.disabled") or ()
    except Exception:
        return set()
    return {str(m).strip() for m in stored if str(m).strip() in MODULES}


def tool_enabled(tool_name: str, off: set[str]) -> bool:
    """Does this MCP tool survive the project's module choices?"""
    if not off:
        return True
    for module in off:
        for prefix in MODULES[module]["tools"]:
            if tool_name.startswith(prefix):
                return False
    return True


def doctor_row_enabled(row_name: str, off: set[str]) -> bool:
    """Is this doctor row still anyone's requirement?

    A row is dropped only when EVERY module that needs it is off — ffmpeg is
    named by both cinematic and playtest, and turning one of them off must
    not un-grade the other's dependency.
    """
    needers = [m for m, spec in MODULES.items() if row_name in spec["doctor"]]
    if not needers:
        return True          # a core row is nobody's option
    return any(m not in off for m in needers)


# ---------------------------------------------------------------------------
# Seat tool surfaces — which CRAFT a dispatched seat actually practises
# ---------------------------------------------------------------------------
# Modules trim the registry per PROJECT; crafts trim it per SEAT. A gameplay
# agent carried every cinematic_, blender_ and kie_music_ schema in its
# context on every turn — tools it has no lane, no brief step and no business
# calling. Craft groups are the unambiguous generation/authoring surfaces;
# everything outside them (queue, seats, bible, lore, godot, scene, assets,
# refs, checks) is the SHARED SPINE and stays universal, because guessing
# wrong about the spine breaks a workflow silently.
CRAFTS: dict[str, tuple[str, ...]] = {
    "image": ("image_", "item_", "cutout_", "vfx_animate",
              "animation_curves", "art_tournament_standings"),
    "three_d": ("blender_", "character_generate", "godot_retarget_check"),
    "music": ("kie_music_", "music_"),
    "cinematic": ("cinematic_", "storyboard_", "kie_video_"),
    "voice": ("voice_",),
    "sfx": ("sfx_",),
    "playtest": ("playtest_",),
    "dialogue": ("dialogue_",),
    "quest": ("quest_",),
    "level": ("level_",),
    "verdicts": ("art_qa_verdict", "art_tournament_verdict"),
    "brainstorm": ("brainstorm_",),
}

# Which crafts each dispatched seat holds. Absent seats — and the director,
# whose whole job is reaching across crafts — are unscoped. `brainstorm`
# belongs to no seat: the room is its own read-only process, and deploying a
# plan is seatless director work.
SEAT_CRAFTS: dict[str, tuple[str, ...]] = {
    "art": ("image", "three_d"),
    "gameplay": ("playtest", "level", "quest"),
    "tech": ("level", "three_d"),
    "audio": ("music", "voice", "sfx"),
    # narrative holds `cinematic` for the storyboard half — scripts and
    # boards are writing work — not for shot generation, which spends.
    "narrative": ("dialogue", "quest", "cinematic"),
    "qa": ("playtest", "verdicts"),
    "cinematic": ("cinematic", "image"),
}


def seat_tool_enabled(tool_name: str, seat: str) -> bool:
    """Does this seat's registry include this tool?

    Fail open three ways on purpose: no seat (a human's session), an unknown
    seat (a project invented one — its surface is unknowable, so it gets
    everything), and any tool outside every craft group (the shared spine).
    A wrongly-hidden tool is a silently broken workflow; a wrongly-shown one
    costs only context.
    """
    held = SEAT_CRAFTS.get((seat or "").strip().lower())
    if held is None:
        return True
    for craft, prefixes in CRAFTS.items():
        for prefix in prefixes:
            if tool_name.startswith(prefix):
                return craft in held
    return True


def pip_hint(for_modules) -> str:
    """The one pip command that installs the extras these modules need."""
    extras: list[str] = []
    for module in for_modules:
        for extra in MODULES.get(module, {}).get("extras", ()):
            if extra not in extras:
                extras.append(extra)
    if not extras:
        return ""
    return f"pip install \"builders-gate[{','.join(extras)}]\""
