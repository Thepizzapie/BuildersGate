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
        "tools": ("music_",), "extras": (), "doctor": (),
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


def machine_defaults() -> set[str]:
    """The MACHINE's default switched-off modules — what the installer wrote.

    The setup wizard's component page is where "install only what I need"
    is decided, and its answer lands in ``~/.bgate/modules.json`` (BGATE_HOME
    aware) as ``{"disabled": [...]}``. New projects seed their own stored
    choice from this; a project that has stored a choice never consults it
    again, so Settings > Modules remains the per-project word. Unknown names
    are dropped, same rule as the stored list.
    """
    try:
        import json as _json

        from . import project as _project

        path = _project.user_dir() / "modules.json"
        data = _json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("disabled") or ()
    except Exception:
        return set()
    return {str(m).strip() for m in raw if str(m).strip() in MODULES}


def disabled(root) -> set[str]:
    """The project's switched-off modules. Unknown names are dropped rather
    than obeyed — a typo in a stored list must not silently disable the
    nearest real feature, and must not survive a rename as a ghost.

    A project that has never stored a choice takes the machine defaults —
    the installer's component page — so an install that declined music is
    music-less on every project until a project says otherwise.
    """
    try:
        from . import settings as _settings

        if _settings.source(root, "modules.disabled") != "stored":
            return machine_defaults()
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
# agent carried every cinematic_, blender_ and music_ schema in its
# context on every turn — tools it has no lane, no brief step and no business
# calling. Craft groups are the unambiguous generation/authoring surfaces;
# everything outside them (queue, seats, bible, lore, godot, scene, assets,
# refs, checks) is the SHARED SPINE and stays universal, because guessing
# wrong about the spine breaks a workflow silently.
CRAFTS: dict[str, tuple[str, ...]] = {
    "image": ("image_", "item_", "cutout_", "vfx_animate",
              # animation_contacts is its sibling — same files, same
              # question one layer down — so it is held by both crafts for
              # the same reason sprite_sheet_check is.
              "animation_curves", "animation_contacts",
              "art_tournament_standings",
              "aseprite_", "palette_pin",
              "animation_generate", "sprite_contract_", "tileset_",
              "game_view_", "prop_generate",
              # SHEET CRAFT IS IMAGE CRAFT. These matched no prefix here
              # (`sprite_contract_` is not `sprite_`) and so fell through to
              # the spine, which meant every audio, narrative and gameplay
              # agent carried 870 words of sheet-slicing docstring it can
              # never act on. sprite_sheet_check is ALSO a verdict below —
              # a tool may hold several crafts.
              "sprite_plan", "sprite_sheet_check", "sprite_sheet_slice",
              # The local generator board: 2D and 3D on this machine.
              "local_status",
              # Scale and room composition are the art seat's own checks
              # before it hands anything over — see `verdicts`.
              "scale_check", "room_review", "room_override"),
    "three_d": ("blender_", "character_generate", "godot_retarget_check",
                # "THE STEP THE 3D PATH WAS MISSING" by its own docstring —
                # it takes a finished .glb into the engine. `godot_` is not a
                # craft prefix (most godot_ tools really are spine), so these
                # two had to be named one at a time.
                "godot_deliver_asset", "godot_import_asset",
                # THE TWO RIG GATES THAT CARRY NO `blender_` PREFIX, because
                # neither one spawns Blender — both read a .glb directly. That
                # is why they fell through: the prefix above is a proxy for
                # "3D rig work", and it stopped being one the moment a gate
                # answered the question without the engine. skin_dominance
                # asks whether a vertex is driven by a bone anywhere near it;
                # animation_contacts runs forward kinematics and asks what the
                # feet actually do. Both are the 3D seat's own checks on a rig
                # it just changed.
                "skin_dominance", "animation_contacts",
                "local_status"),
    "music": ("music_", "audio_listen_record"),
    "cinematic": ("cinematic_", "storyboard_", "kie_video_"),
    "voice": ("voice_",),
    "sfx": ("sfx_", "audio_listen_record"),
    # TELEMETRY AND EVIDENCE ARE THE PLAYTEST CRAFT, not a separate one: a
    # causal chain is reconstructed from playtest telemetry and an evidence
    # manifest is captured from a running game. gameplay and qa hold
    # `playtest`, which is exactly who reads them.
    "playtest": ("playtest_", "causal_", "godot_evidence",
                 "evidence_check_ui"),
    "dialogue": ("dialogue_",),
    "quest": ("quest_",),
    # game_view_ is READ-ONLY-ish shared ground: the level craft
    # needs it to know what a correct prop even looks like, and the
    # image craft needs it to generate one.
    # sidescroll_generate is level_generate's platformer counterpart by its
    # own docstring; only the name kept it out.
    # tileset_describe is BOTH crafts and has to be. `tileset_` belongs to
    # image (art owns the sheet), but level_generate now refuses a sheet with
    # no sidecar - so a gameplay or tech seat handed an imported .tres could
    # see the refusal and not the tool that answers it.
    "level": ("level_", "game_view_", "sidescroll_generate",
              "tileset_describe"),
    "verdicts": ("art_qa_verdict", "art_tournament_verdict",
                 # The free look before anything else is spent on a sheet.
                 "sprite_sheet_check",
                 # THE PRESENTATION GATE'S THREE VERDICTS. Each is a judgement
                 # the old pipeline made against the wrong evidence: an asset
                 # crop instead of a room, a contact sheet instead of game
                 # scale, a file metric instead of a listen. qa holds this
                 # craft; art and audio get their own copies below, because
                 # the seat that made the thing has to be able to check it
                 # before it hands it over.
                 "room_review", "room_override", "scale_check", "scale_record_3d",
                 "audio_listen_record",
                 # THE TWO THE PRESENTATION GATE GAINED. `evidence_assert` is
                 # the verdict a captured frame never had — recording what
                 # somebody SAW, which is the half that let a two-tailed
                 # character ship past a folder full of renders. `traversal_prove`
                 # is the verdict on a route: it drives the real controller and
                 # is the only thing here that measures the player rather than
                 # the geometry. Both are judgements about a runtime, which is
                 # what this craft is.
                 "evidence_assert", "traversal_prove"),
    "brainstorm": ("brainstorm_",),
}

# THE SPINE, BY EXACT NAME, because the alternative is what this fixes.
#
# CRAFTS is a PREFIX table, and a tool whose name matched no prefix landed in
# the shared spine SILENTLY - not because anyone decided it was universal, but
# because nobody noticed. That is how `sidescroll_generate` (27 parameters) and
# `godot_deliver_asset` (713 words of docstring) came to ride in the audio
# seat's context on every turn. The failure mode is invisible by construction:
# a miscategorised tool still works, it just costs every seat that will never
# call it.
#
# So membership is now DECLARED. Exact names, never prefixes: a prefix here
# would re-open the same hole one level up, letting the next `godot_*` tool
# join the spine by accident exactly as these did. A new tool matching neither
# a craft nor this list fails `test_modules.py::TestEveryToolIsClassified`,
# and the fix is one line in whichever of the two tables is correct - a
# decision someone makes, rather than one the string table makes for them.
SPINE: frozenset[str] = frozenset({
    "agent_activity", "agent_steer", "agent_steer_all", "ask_human",
    "asset_lock", "asset_release", "asset_status", "asset_track",
    "asset_verify", "bgate_doctor", "bible_add", "bible_read",
    "bible_ref_attach", "bible_ref_detach", "bible_ref_list",
    "bible_update", "board_digest", "canon_check",
    "consistency_check", "decision_add", "decision_list",
    "decision_settle",
    # THE PRODUCTION STAGE. `greenlight_status` is spine because it is the
    # answer to "why will my queued item not dispatch" — a seat the stage is
    # holding has to be able to read the hold, or it concludes the board is
    # broken and works around it, which is the exact behaviour the stage
    # exists to stop. The three writers below it are the director's
    # arbitration and are DIRECTOR_ONLY; graybox_submit is gameplay's move
    # and is not. encounter_design_set and scale_contract_set are project
    # canon, filed the way bible_add is.
    "greenlight_status", "greenlight_thesis_set",
    "greenlight_graybox_submit", "greenlight_graybox_verdict",
    "greenlight_advance", "greenlight_waive", "greenlight_supersede",
    "encounter_design_set", "scale_contract_set",
    "godot_check_project",
    "godot_inspect_resource", "godot_run", "godot_scaffold",
    "godot_screenshot", "godot_status", "godot_templates",
    "godot_test_run", "handoff_note", "handoff_read",
    "iteration_record_checks", "iteration_status",
    # kie_status STAYS SPINE and it is not an oversight. kie is one key over
    # three capabilities - images, Suno music, Seedance video - so filing it
    # under `image` would hide it from the audio seat whose music path is
    # kie-only. A wrongly-hidden tool breaks a workflow silently; 36 words is
    # not worth that trade.
    "kie_status",
    "lore_add", "lore_brief", "lore_fact", "lore_link", "lore_list",
    "lore_update", "not_building_add", "not_building_list",
    "pending_decisions", "plan_status", "profile_get", "profile_set",
    "project_init", "project_select", "project_set_dimension",
    "project_status", "provider_status", "queue_add",
    "queue_add_chain", "queue_add_dependency", "queue_claim_next",
    "queue_complete", "queue_cut_dependency", "queue_get",
    "queue_list", "queue_next", "queue_reopen", "queue_update",
    "recall", "ref_list", "ref_pin", "ref_unpin",
    # The map of this list. Universal by definition - a seat that cannot ask
    # what its own surface contains is the problem this whole phase is about.
    "tool_index",
    "scene_attach_script", "scene_node_add", "scene_outline",
    "scene_rename_node", "scene_reparent_node", "scene_set_property",
    "scene_swap_resource", "scene_unwire", "scene_wire", "seat_brief",
    "seat_can_write", "seat_configure", "seat_list", "seat_notes",
    "seat_post_note",
})


# THE SPINE IS NOT ONE THING. Splitting it is the only P2 lever that pays
# without renaming anything.
#
# `SPINE` above says "not a craft". It does NOT say "everyone needs this", and
# nine of its tools are the top-level session's job by construction: a
# dispatched worker does not create projects, does not rewrite the seat table,
# does not settle a decision (`decision_settle` says "Human sessions only" in
# its own first line), and does not watch or steer the agents it never
# dispatched. They rode in every seat's context anyway, at 1,243 docstring
# words a turn, because "not a craft" was the only category available.
#
# THE TEST IS "IS THERE A SEAT", NOT "WHICH SEAT". A seat env var means this
# process was dispatched by the board; its absence is the human's own session.
# That is the same signal `director_instructions` already switches on, so a
# project inventing a new seat name gets the same answer as a known one rather
# than falling open the way craft scoping deliberately does.
#
# WHAT IS DELIBERATELY NOT HERE, having been considered and rejected:
# `ask_human` (escalation is the worker's lifeline), `queue_claim_next` (its
# whole purpose is keeping a seat worker fed), `board_digest` and `plan_status`
# (peer awareness is a goal, not a leak), `queue_add_chain` (workers file
# dependent work), and `bgate_doctor` (an agent hitting a missing dependency
# should be able to ask why). Hiding any of those breaks a workflow silently,
# which costs more than the words save.
DIRECTOR_ONLY: frozenset[str] = frozenset({
    "agent_activity", "agent_steer", "agent_steer_all",
    "decision_settle", "project_init", "project_select",
    "project_set_dimension", "seat_configure",
    # THE STAGE IS THE DIRECTOR'S TO MOVE. A dispatched seat that could pass
    # its own graybox, waive its own hold, or advance the project past the
    # gate holding it is not gated at all — the whole mechanism reduces to a
    # note. greenlight_status stays open to every seat (it is the answer to
    # "why am I held"); these four are the arbitration.
    "greenlight_thesis_set", "greenlight_graybox_verdict",
    "greenlight_advance", "greenlight_waive",
    # RETRACTING A GATE FINDING IS ARBITRATION. A seat that could withdraw the
    # row blocking its own work has not been gated, it has been asked nicely —
    # the same reduction the four above exist to prevent. The evidence for a
    # retraction (a better measurement) is anybody's to produce; the decision
    # to accept it is not.
    "greenlight_supersede",
})


def crafts_owning(tool_name: str) -> set[str]:
    """Every craft that claims this tool. Empty means spine-or-unclassified."""
    return {craft for craft, prefixes in CRAFTS.items()
            if any(tool_name.startswith(p) for p in prefixes)}


def unclassified(tool_names) -> list[str]:
    """Tools that are in neither table - the accident this design forbids.

    Returns names, sorted, so a failing test names what to file rather than
    only that the count moved.
    """
    return sorted(n for n in tool_names
                  if n not in SPINE and not crafts_owning(n))

# Which crafts each dispatched seat holds. Absent seats — and the director,
# whose whole job is reaching across crafts — are unscoped. `brainstorm`
# belongs to no seat: the room is its own read-only process, and deploying a
# plan is seatless director work.
SEAT_CRAFTS: dict[str, tuple[str, ...]] = {
    "art": ("image", "three_d"),
    # gameplay gets `verdicts` for traversal_prove and nothing else would be
    # the wrong trade — a seat that builds routes and cannot prove one drives
    # the QA seat for every jump it places. The rest of the craft is cheap.
    "gameplay": ("playtest", "level", "quest", "verdicts"),
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
    if (seat or "").strip() and tool_name in DIRECTOR_ONLY:
        # Before the craft lookup, and gated on ANY seat rather than a known
        # one: dispatch is the fact that matters here, not which chair.
        return False
    held = SEAT_CRAFTS.get((seat or "").strip().lower())
    if held is None:
        return True
    # A TOOL MAY BELONG TO SEVERAL CRAFTS, and it is enabled if the seat holds
    # ANY of them. This used to return on the first craft whose prefix matched,
    # so a shared tool resolved to whichever craft happened to be declared
    # first in CRAFTS — `game_view_` is in both `image` and `level`, and
    # gameplay (which holds `level`, not `image`) was refused it because
    # `image` is written higher in the dict. Dict order is not a permission
    # model.
    owners = crafts_owning(tool_name)
    if not owners:
        return True
    return bool(owners & set(held))


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
