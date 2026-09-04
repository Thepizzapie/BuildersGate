"""Cinematics + storyboards MCP tools - carved out of server.py, verbatim.

server.py held ~226 tools in 12k lines; the domains that never
touch each other now live apart. The contract is unchanged: the
shared plumbing (_tool, _root, the gates) stays in server, each
domain imports it back, and server star-imports this module at its
BOTTOM - by then its globals all exist, which is what makes the
circular import legal - so server.<tool> still answers for every
caller and test.
"""
from typing import Annotated

from pydantic import Field

from bgate_mcp.server import (  # noqa: F401
    Optional, _Path, _log, _provider_gate,
    _root, _tool, _work_item_id,
)

# ---------------------------------------------------------------------------
# Cinematics - the pipeline half of video
# ---------------------------------------------------------------------------
# WHY THESE EXIST ALONGSIDE kie_video_generate, and it is the same split the
# music tools settled: the adapter buys a file, the core module makes it an
# asset. Video needs the split more than music did, because a copied .mp4 is not
# merely unmanaged - it is UNPLAYABLE. Godot's VideoStreamPlayer supports Ogg
# Theora and nothing else in core, so a keep that copies delivers a black
# rectangle and a green badge. cinematic_keep transcodes.
#
# AND BECAUSE ONE GENERATION IS NOT ONE DELIVERABLE. Every video model here caps
# at 15 seconds, so a cutscene is N paid generations that have to be planned,
# ordered, judged and joined - cinematic_plan is the only free step in the whole
# pipeline and is where a sequence should be argued about.
@_tool
def cinematic_options() -> dict:
    """What a cutscene generation may ask for, and whether this machine can
    deliver one. Costs nothing.

    Reports two independent availabilities and they fail differently: the
    PROVIDER (a kie key) is what buys a shot, and the ENCODER (ffmpeg built with
    libtheora) is what makes a bought shot playable. A machine with a key and no
    libtheora can generate a whole sequence, pay for all of it, and deliver none
    of it - so this is worth reading before the first shot, and
    cinematic_generate_shot checks it before spending anyway.
    """
    from bgate_core.cine import cinematic as _cine

    return {"ok": True, **_cine.options(_root())}


@_tool
def cinematic_plan(name: Annotated[str, Field(description='Sequence name; the row every shot, estimate and assembly refers to.')], shots: Annotated[list, Field(description='Shot objects in cut order: action (required), camera, shot_size, location, dialogue, duration, first_frame, last_frame, refs, transition, transition_s, vo.')], logline: Annotated[str, Field(description='One line on what the cutscene is, for humans reading the sequence.')] = "", style: Annotated[str, Field(description='A preset KEY (anime, noir, comic, painterly, pixel, stop_motion, cg_animated, watercolor, vhs, silhouette, live_action) or free prose; unlisted words are prose.')] = "",
                   style_note: Annotated[str, Field(description="The project's own wording, appended to the preset.")] = "", style_refs: Annotated[Optional[list], Field(description='Repo-relative paths to frames carrying the look; these beat prose and hold across generations.')] = None,
                   locations: Annotated[Optional[list], Field(description='[{slug, description, label?, plates?}]; each shot names one and its description is injected into every shot filmed there.')] = None,
                   model: Annotated[str, Field(description='Video model for the WHOLE sequence (cinematic_options lists them); changing it resets generated shots.')] = "", aspect_ratio: Annotated[str, Field(description='Frame shape every shot is bought at. Default 16:9.')] = "16:9",
                   resolution: Annotated[str, Field(description='Resolution every shot is bought at. Default 720p.')] = "720p", audio_track: Annotated[str, Field(description='Repo-relative path to the music bed laid under the whole cutscene; without it the cut is silent.')] = "",
                   audio_gain_db: Annotated[float, Field(description='Trim on the bed, in dB (-6 is a usual start under dialogue). Default 0.')] = 0.0, fade_in: Annotated[float, Field(description='Seconds to fade picture and sound in at the start. Default 0.')] = 0.0,
                   fade_out: Annotated[float, Field(description='Seconds to fade picture and sound out at the end. Default 0.')] = 0.0) -> dict:
    """Write a cutscene's shot list. SPENDS NOTHING - do this first, always.

    `shots` in cut order, each {action (REQUIRED), camera, shot_size (a fixed
    list), location (a slug from `locations`), dialogue (becomes subtitles),
    duration (4-15s, default 5), first_frame, last_frame (match cuts ONLY),
    refs, transition (cut | fade | dissolve | wipe), transition_s, vo}.
    Without audio_track the cut is SILENT. Name a style or the model's house
    look applies. Generate grouped by location (`generation_order`). ANCHOR
    EVERY SHOT on approved stills; never on the previous shot's output.
    Changing style or model resets generated shots.
    Full notes: docs/tools.md#cinematic_plan
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.plan(_root(), name, list(shots or []), logline=logline,
                      style=style, style_note=style_note,
                      style_refs=list(style_refs or []),
                      locations=list(locations or []), model=model,
                      aspect_ratio=aspect_ratio, resolution=resolution,
                      audio_track=audio_track,
                      audio_gain_db=float(audio_gain_db),
                      fade_in=float(fade_in), fade_out=float(fade_out),
                      work_item_id=_work_item_id())


@_tool
def cinematic_sequences(name: str = "") -> dict:
    """The shot lists in this project, or one sequence with every shot's state.

    The first thing to call when picking up a half-generated cutscene: it says
    which shots were bought, which were kept, and which are still planned, so a
    successor never re-buys a shot that already exists.
    """
    from bgate_core.cine import cinematic as _cine

    root = _root()
    if name:
        return {"ok": True, "sequence": _cine.sequence(root, name)}
    return {"ok": True, "sequences": _cine.sequences(root)}


@_tool
def cinematic_generate_shot(name: str, idx: int, model: str = "",
                            generate_audio: bool = False,
                            overwrite: bool = False, previs_ok: bool = False,
                            timeout: float = 1800.0) -> dict:
    """Buy ONE shot of a planned sequence. Costs real credits. Runs in minutes.

    ONE SHOT PER CALL, DELIBERATELY: look at the clip, keep or re-generate,
    then move to the next index. generate_audio is FALSE by default - model
    audio is baked into the clip and cannot be ducked, separated or
    localised; the audio seat scores the cut. Local conditioning frames are
    uploaded automatically. The encoder is checked BEFORE anything is
    charged.
    Full notes: docs/tools.md#cinematic_generate_shot
    """
    from bgate_core.cine import cinematic as _cine

    # Provider preflight: a drained kie account refuses regardless of what
    # the shot would cost, and it is cheaper to learn that here than off a
    # paid 402.
    refused = _provider_gate(_root(), "video", "a video shot")
    if refused:
        return refused
    result = _cine.generate_shot(_root(), name, int(idx), model=model,
                                 generate_audio=bool(generate_audio),
                                 overwrite=bool(overwrite),
                                 previs_ok=bool(previs_ok),
                                 timeout=float(timeout),
                                 work_item_id=_work_item_id())
    if result.get("ok"):
        _log("video", f"generated {name} shot {idx}",
             ref=str(result.get("artifact_id") or ""))
    return result


@_tool
def cinematic_assemble(name: str, quality: int = 6) -> dict:
    """Join a sequence's kept shots, in order, into ONE .ogv the game can load.

    Refuses while any shot not marked 'cut' is unkept, and refuses shots that
    are not all the same size (ffmpeg joins those into a broken file and
    reports success). The result is registered as a candidate. WATCH THE
    WHOLE CUT before keeping it - the light jumping and the camera crossing
    the line are invisible shot by shot.
    Full notes: docs/tools.md#cinematic_assemble
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.assemble(_root(), name, quality=int(quality))


@_tool
def cinematic_candidates(logical_name: str = "", limit: int = 100) -> dict:
    """Generated shots and cuts awaiting a decision, plus what has been kept.

    Every row carries its artifact_id (what cinematic_keep / cinematic_discard
    take) and `installed`, which is true only when the file in the engine
    project was transcoded from THIS revision - so a superseded take cannot
    claim to be the clip the game loads.
    """
    from bgate_core.cine import cinematic as _cine

    root, cap = _root(), max(1, min(int(limit), 500))
    return {"ok": True,
            "candidates": _cine.candidates(root, logical_name=logical_name,
                                           limit=cap),
            "kept": _cine.kept(root, limit=cap)}


@_tool
def cinematic_keep(artifact_id: int, note: str = "", quality: int = 6,
                   install_to_engine: Optional[bool] = None) -> dict:
    """Approve a take, and put it in the engine project if the engine loads it.

    An assembled CUT is transcoded to Ogg Theora (the only format Godot
    plays; an .mp4 imports with NO error and plays a blank rectangle) and
    copied into the game BEFORE the approval. A SHOT is approved and stays in
    .bgate_out. quality is 1-10, 6 the documented baseline (5 for 1440p+).
    install_to_engine overrides the default - true for a single clip used on
    its own.
    Full notes: docs/tools.md#cinematic_keep
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.keep(_root(), int(artifact_id), note=note,
                      quality=int(quality),
                      install_to_engine=install_to_engine)


@_tool
def cinematic_install(artifact_id: int, quality: int = 6) -> dict:
    """Transcode an ALREADY-APPROVED clip into the engine project. Repair verb.

    The same door music_install is, for the same measured reason: on a project
    whose approval gate is off, artifacts.register approves each revision as it
    is filed, so there is no candidate, no keep, and no installed file. Use it
    when cinematic_candidates shows a kept clip with `installed: false`, or when
    an approved cutscene's .ogv was deleted out of the engine project.
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.install(_root(), int(artifact_id), quality=int(quality))


@_tool
def cinematic_discard(artifact_id: int, note: str = "") -> dict:
    """Reject a shot or a cut, and put its shot back to planned so it can be
    re-generated. Refusing to ship something is an agent's call, so unlike
    cinematic_keep this needs no human.

    The file is left under .bgate_out - gitignored, outside the engine project.
    Say what was wrong with it: 'discarded' teaches the re-roll nothing, and at
    video prices the re-roll is the expensive part.
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.discard(_root(), int(artifact_id), note=note)


@_tool
def cinematic_register_model(name: Annotated[str, Field(description='What to call the model here, e.g. "kling-3".')], model: Annotated[str, Field(description='The LITERAL id kie wants in the top-level `model` field.')], intent: Annotated[dict, Field(description="Map of seconds, shape, quality, first_frame, last_frame, refs, audio to this model's own field names; omit what it cannot do.")],
                             label: Annotated[str, Field(description='Human-readable name shown in listings.')] = "", note: Annotated[str, Field(description='Free text about the model or where its page is.')] = "",
                             enums: Annotated[Optional[dict], Field(description="Allowed values per field, keyed by the model's own field names.")] = None,
                             ranges: Annotated[Optional[dict], Field(description="Numeric [min, max] per field, keyed by the model's own field names.")] = None,
                             caps: Annotated[Optional[dict], Field(description="Hard limits per field (e.g. max reference count), keyed by the model's own field names.")] = None,
                             intent_values: Annotated[Optional[dict], Field(description='{intent: {canonical: this model\'s spelling}}, e.g. shape 16:9 -> "landscape".')] = None,
                             intent_scale: Annotated[Optional[dict], Field(description='{intent: multiplier}, e.g. seconds -> n_frames.')] = None,
                             credits: Annotated[Optional[dict], Field(description='{"per_second", "per_call"} credit rates for the estimate; beats the built-in table, loses to BGATE_KIE_VIDEO_CREDITS.')] = None) -> dict:
    """Add a video model from a reference page you have READ. Spends nothing.

    name is what to call it here; model is the LITERAL id kie wants; intent
    maps each of seconds, shape, quality, first_frame, last_frame, refs, audio
    to this model's field name - omit any it cannot do and asking for it is
    refused before the spend. Optional and checked before money moves: enums
    / ranges / caps (keyed by ITS field names), intent_values ({intent:
    {canonical: spelling}}), intent_scale ({intent: multiplier}). Stamped
    source="registered"; lives for this server process.
    Full notes: docs/tools.md#cinematic_register_model
    """
    from bgate_adapters import kie

    return {"ok": True, **kie.register_video_model(name, {
        "model": model, "intent": dict(intent or {}), "label": label,
        "note": note, "enums": enums or {}, "ranges": ranges or {},
        "caps": caps or {}, "intent_values": intent_values or {},
        "intent_scale": intent_scale or {}, "credits": credits or {}})}


@_tool
def cinematic_estimate(name: str, model: str = "") -> dict:
    """What this sequence will cost to buy, before buying any of it. Free.

    Read it between cinematic_plan and the first cinematic_generate_shot. AN
    UNKNOWN PRICE IS REPORTED AS UNKNOWN, NEVER AS ZERO: shots on an unrated
    model land in `unknown_shots` and `usd` is null. The numbers are an upper
    bound from kie's published band; set BGATE_KIE_USD_PER_CREDIT or
    BGATE_KIE_VIDEO_CREDITS for real figures.
    Full notes: docs/tools.md#cinematic_estimate
    """
    from bgate_core.cine import cinematic as _cine

    return {"ok": True, **_cine.estimate_sequence(_root(), name,
                                                  model=model)}


@_tool
def cinematic_stuck_shots(older_than_s: int = 0, poll: bool = True) -> dict:
    """Find generations that were PAID FOR and never collected. This is the tool
    that finds money.

    A generation is charged at submit; a row stuck at 'generating' may be a
    finished clip you already own. Run after any dashboard restart or killed
    agent, and before re-generating a "failed" shot. Act on `recoverable`
    with cinematic_recover_shot (pays nothing); `lost` has no task id and
    probably no charge; `unknown` means the provider did not say.
    older_than_s: how stale counts as suspicious. poll=False never leaves the
    machine.
    Full notes: docs/tools.md#cinematic_stuck_shots
    """
    from bgate_core.cine import cinematic as _cine

    return {"ok": True, **_cine.stuck_shots(
        _root(),
        older_than_s=int(older_than_s) or _cine.STUCK_AFTER_S,
        poll=bool(poll))}


@_tool
def cinematic_probe_model(name: str, timeout: float = 30.0) -> dict:
    """Ask kie whether a registered model id actually exists. Opt-in, and READ
    THE CAVEAT.

    Submits a deliberately empty request: 404 means the id is wrong, 422 means
    it resolved. IT IS INFERENCE, NOT A CONTRACT - a model that ACCEPTS an
    empty input may start a billable job; a returned task id is reported
    loudly, so treat it as a real charge and collect it with
    cinematic_recover_shot. Registered models stay unverified until this
    says otherwise.
    Full notes: docs/tools.md#cinematic_probe_model
    """
    from bgate_adapters import kie

    return {"ok": True, **kie.probe_model_id(name, root=_root(),
                                             timeout=float(timeout))}


@_tool
def cinematic_styles() -> dict:
    """Every built-in style preset, with what each is good and bad at.

    Read this before planning rather than guessing at prose: each entry carries
    a `note` naming the trap. Two worth knowing up front - `pixel` is the
    WEAKEST fit for generated video (models produce pixel-looking output on a
    non-integer grid, which shimmers next to real pixel art), and `silhouette`
    is the one style that survives being unanchored, because no faces means no
    identity drift.

    A style that is not in this table is not refused: free prose works, and so
    do style_refs, which beat prose.
    """
    from bgate_core.cine import cinematic as _cine

    return {"ok": True, "styles": _cine.styles(),
            "fallback": _cine.STYLE_FALLBACK}


@_tool
def cinematic_shot_status(task_id: str) -> dict:
    """Where a submitted generation got to at the provider. Costs nothing.

    This only LOOKS. When it says `recoverable`, cinematic_recover_shot is what
    puts the clip on disk - do NOT re-run cinematic_generate_shot to get a file
    for a task that has already been charged.
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.shot_status(_root(), task_id)


@_tool
def cinematic_recover_shot(name: str, idx: int, task_id: str = "",
                           overwrite: bool = False) -> dict:
    """Download a shot that was ALREADY PAID FOR and register it. Repair verb.

    A generation is charged at SUBMIT, and everything after that - the poll
    loop, the download, this process surviving the ten minutes it takes - can
    fail while the provider sits on a finished clip you have been billed for.
    Pressing generate again pays twice.

    The task id is read off the shot row when omitted, which is why it is stored
    there: an agent that died mid-generation left the id behind, so its
    successor needs no archaeology. No cost is recorded against this call - the charge happened at submit, possibly days ago.
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.recover_shot(_root(), name, int(idx), task_id,
                              overwrite=bool(overwrite),
                              work_item_id=_work_item_id())


@_tool
def cinematic_transitions() -> dict:
    """How two shots may be joined, and what each one costs. Free to read.

    `cut` is the default and needs no filter graph at all, so a sequence of
    cuts is joined with one decode and one encode. Anything else decodes every
    shot in full - which is a real cost on a long sequence and the reason the
    cheap path is the default rather than an option.

    A transition OVERLAPS both shots, so a cut is shorter than the sum of its
    shot durations, and caption timing is computed from that rather than from
    the naive sum.
    """
    from bgate_core.cine import cinecut as _cut

    return {"ok": True, "transitions": _cut.TRANSITIONS,
            "default": _cut.DEFAULT_TRANSITION}


@_tool
def cinematic_continuity(name: str) -> dict:
    """Do this sequence's shots actually CUT TOGETHER? Costs nothing but time.

    Extracts the real frames either side of every join and compares
    brightness and palette on the pixels, never the prompts. IT CANNOT TELL
    YOU THE CUTSCENE IS GOOD - a cellar-to-snowfield cut should jump; every
    finding leaves the verdict to a human. Run it BEFORE assembling.
    Full notes: docs/tools.md#cinematic_continuity
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.check_continuity(_root(), name)


def _animatic_images(result: dict) -> list[str]:
    """The panels, handed back as pictures.

    A reel is a video and an agent cannot watch one. The panels ARE the edit,
    in order, so returning them is the difference between a tool that reports a
    runtime and a tool whose output can actually be reviewed.
    """
    out = []
    for path in ((result or {}).get("panel_files") or [])[:12]:
        if path and _Path(path).exists():
            out.append(str(path))
    return out


@_tool(images=_animatic_images)
def cinematic_animatic(name: str, source: str = "auto", fps: int = 12,
                       burn_captions: bool = True) -> dict:
    """Cut the storyboard panels together at their planned timings. FREE - calls
    no model and spends nothing.

    CALL THIS BEFORE cinematic_generate_shot. EVERY TIME. Read
    `average_shot_s` first (films sit at 4-6s), then runtime_s / measured_s,
    `placeholders` (beats with no still, held as slate cards) and `warnings`.
    `source`: "auto" (the planned sequence if any, else the board),
    "sequence" or "board". The reel is an H.264 .mp4 under
    design/cinematics/animatics/; the panels come back as images.
    Full notes: docs/tools.md#cinematic_animatic
    """
    from bgate_core.cine import animatic as _anim

    return _anim.build(_root(), name, source=str(source or "auto"),
                       fps=int(fps), burn_captions=bool(burn_captions))


@_tool
def cinematic_deliver(name: str, force: bool = False) -> dict:
    """Build the Godot scene that PLAYS this cutscene. The last mile.

    Writes <name>.tscn (a CanvasLayer at layer 100), <name>.gd (plays, draws
    captions, skips on ui_cancel/ui_accept, emits `finished(skipped: bool)`),
    <name>.srt and <name>_captions.json beside the kept .ogv. Gameplay
    instantiates it, add_child, `await cut.finished`. IT WILL NOT OVERWRITE
    A SCRIPT YOU HAVE EDITED; pass force to replace it.
    Full notes: docs/tools.md#cinematic_deliver
    """
    from bgate_core.cine import cinematic as _cine

    return _cine.deliver(_root(), name, force=bool(force))


# ---------------------------------------------------------------------------
# storyboards - the free half of a cutscene
# ---------------------------------------------------------------------------

def _board_images(result: dict) -> list[str]:
    """The frame this call drew, handed back as an image block.

    A board is a picture. A tool that draws one and returns only a path makes
    the agent spend another call to look at what it just bought.
    """
    root = _Path(_root())
    rel = (result or {}).get("path") or ""
    if not rel:
        return []
    full = root / rel
    return [str(full)] if full.exists() else []


def _frames_images(result: dict) -> list[str]:
    root = _Path(_root())
    out = []
    for frame in ((result or {}).get("frames") or []):
        rel = frame.get("image_path") if isinstance(frame, dict) else ""
        if rel and (root / rel).exists():
            out.append(str(root / rel))
    return out[:12]


@_tool(images=_frames_images)
def storyboard_auto(name: Annotated[str, Field(description='Board name; also the sequence name when promoted.')], premise: Annotated[str, Field(description="One or two sentences on what happens; empty reuses the board's existing beats.")] = "", frames: Annotated[int, Field(description='How many beats to break the premise into. Default 6.')] = 6,
                    style: Annotated[str, Field(description="A cinematic_styles preset key or free prose; empty applies the bible's art direction only.")] = "", style_note: Annotated[str, Field(description="The project's own wording, appended to the style.")] = "",
                    cast_refs: Annotated[Optional[list], Field(description='Pinned reference names for who is in the scene; empty derives a cast from character pins, then lore.')] = None,
                    aspect_ratio: Annotated[str, Field(description='Frame shape for every panel. Default 16:9.')] = "16:9", quality: Annotated[str, Field(description='low | medium | high per frame. Default low - a board is read at a glance.')] = "low",
                    promote_to: Annotated[str, Field(description='Sequence name to write a shot list into when the board is done; empty promotes nothing.')] = "", model: Annotated[str, Field(description='Image model id for the frames; "" takes the provider default.')] = "") -> dict:
    """Premise in, finished storyboard out, in ONE call. START HERE.

    THE DEFAULT DOOR FOR "MAKE ME A CUTSCENE"; the other storyboard tools are
    its parts. Without asking: derives a cast (character pins, then canon
    lore), applies the bible's locked art direction, writes beats from the
    premise, and draws every frame - a failed frame does not stop the rest.
    Images only, `quality="low"` by default; it buys NO video (promote_to
    writes the shot list for free). Re-running keeps existing images and
    beats.
    Full notes: docs/tools.md#storyboard_auto
    """
    from bgate_core.cine import storyboard as _sb

    refused = _provider_gate(_root(), "image",
                           "a storyboard build")
    if refused:
        return refused
    return _sb.auto(
        _root(), name, premise, frames=int(frames), style=style,
        style_note=style_note,
        cast_refs=list(cast_refs) if cast_refs else None,
        aspect_ratio=aspect_ratio, quality=quality,
        promote_to=promote_to, model=model,
        work_item_id=_work_item_id())


@_tool
def storyboard_write_script(name: str, premise: str, frames: int = 6,
                            style: str = "", style_note: str = "",
                            cast_refs: Optional[list] = None,
                            characters: str = "",
                            aspect_ratio: str = "16:9") -> dict:
    """Turn a premise into a script and a beat-per-frame board. Costs a fraction
    of a cent. START HERE when you know what the scene is ABOUT but not yet
    what is in it.

    Writes prose and beats; draws NOTHING and buys no video. frames: 1-24
    beats, default 6. cast_refs: PINNED REFERENCE NAMES - THE CAST IS THE
    POINT, a script without it invents strangers. characters: anything the
    pins do not say. style: a preset key or prose. Re-running replaces the
    beats; frames that already have an image keep it at the same index.
    Full notes: docs/tools.md#storyboard_write_script
    """
    from bgate_core.cine import storyboard as _sb

    return _sb.write_script(
        _root(), name, premise, frames=int(frames), style=style,
        style_note=style_note, cast_refs=list(cast_refs or []),
        characters=characters, aspect_ratio=aspect_ratio)


@_tool
def storyboard_plan(name: Annotated[str, Field(description='Board name.')], frames: Annotated[Optional[list], Field(description="[{beat, action, camera, dialogue, duration, refs, note}] in scene order; omit entirely to edit only the board's fields.")] = None, premise: Annotated[str, Field(description='What the scene is about.')] = "",
                    logline: Annotated[str, Field(description='One line on the scene, for humans.')] = "", style: Annotated[str, Field(description='A cinematic_styles preset key or free prose.')] = "", style_note: Annotated[str, Field(description="The project's own wording, appended to the style.")] = "",
                    style_refs: Annotated[Optional[list], Field(description='Pinned reference names carrying the look, applied to every frame.')] = None,
                    cast_refs: Annotated[Optional[list], Field(description='Pinned reference names for who is in the scene, conditioned on every frame.')] = None,
                    aspect_ratio: Annotated[str, Field(description='Frame shape for every panel. Default 16:9.')] = "16:9") -> dict:
    """Write or edit a storyboard by hand. SPENDS NOTHING.

    `frames` in scene order, each {beat (required unless action), action
    (what is VISIBLE - what the image model reads), camera, dialogue,
    duration (default 5), refs, note}. OMIT `frames` ENTIRELY to edit only
    the board's own fields (cast, style, premise) and leave the drawings
    alone. A frame that already has an image KEEPS it at the same index.
    storyboard_write_script does this from a premise.
    Full notes: docs/tools.md#storyboard_plan
    """
    from bgate_core.cine import storyboard as _sb

    return _sb.plan(
        _root(), name,
        None if frames is None else list(frames),
        premise=premise, logline=logline, style=style,
        style_note=style_note,
        style_refs=None if style_refs is None else list(style_refs),
        cast_refs=None if cast_refs is None else list(cast_refs),
        aspect_ratio=aspect_ratio)


@_tool
def storyboard_boards(limit: int = 100) -> dict:
    """Every storyboard in this project, newest first, with its frame counts."""
    from bgate_core.cine import storyboard as _sb

    return {"ok": True, "boards": _sb.boards(_root(), limit=int(limit))}


@_tool(images=_frames_images)
def storyboard_open(name: str) -> dict:
    """One board: its script, its cast, every frame in order, and whether it can
    be promoted yet. The drawn frames come back as images, so you can LOOK at the
    scene rather than reading paths.

    `ready.blockers` is the specific list of what stands between this board and
    a paid sequence. Read it before reaching for allow_unanchored.
    """
    from bgate_core.cine import storyboard as _sb

    return {"ok": True, **_sb.board(_root(), name)}


@_tool(images=_board_images)
def storyboard_frame_generate(name: Annotated[str, Field(description='Board name.')], idx: Annotated[int, Field(description='Zero-based frame index on the board.')], prompt: Annotated[str, Field(description="Override the prompt outright; empty builds it from the frame's action, camera and the board's style.")] = "",
                              provider: Annotated[str, Field(description='Image provider; "" uses the project\'s routing.')] = "", model: Annotated[str, Field(description='Provider model id; "" takes the default.')] = "",
                              refs: Annotated[Optional[list], Field(description='Extra pinned reference names for THIS frame only, on top of the cast.')] = None,
                              use_cast: Annotated[bool, Field(description="Condition on the board's cast_refs. False for a frame with nobody in it. Default True.")] = True, ref_strength: Annotated[float, Field(description='How hard the references pull, 0-1. Default 0.5.')] = 0.5,
                              quality: Annotated[str, Field(description='low | medium | high. Default medium; low costs about a quarter and is usually enough.')] = "medium") -> dict:
    """Draw ONE storyboard frame. This is the only tool here that costs money,
    and it is an IMAGE - far cheaper than the video shot it stops you buying
    blind.

    ONE FRAME PER CALL. The board's cast_refs and style_refs are passed as
    reference images automatically. refs: extra pins for THIS frame; use_cast
    False for a frame with nobody in it; quality low | medium | high (low is
    usually enough). The prompt is built from the frame's action, camera and
    style unless `prompt` overrides it. Comes back 'drafted', never
    'approved'.
    Full notes: docs/tools.md#storyboard_frame_generate
    """
    from bgate_core.cine import storyboard as _sb

    refused = _provider_gate(_root(), "image", "a storyboard frame")
    if refused:
        return refused
    return _sb.frame_generate(
        _root(), name, int(idx), prompt=prompt, provider=provider,
        model=model, refs=list(refs or []), use_cast=bool(use_cast),
        ref_strength=float(ref_strength), quality=quality)


@_tool
def storyboard_frame_attach(name: str, idx: int, image: str = "",
                            ref: str = "", approve: bool = False) -> dict:
    """Put an EXISTING image on a frame - one the author drew, shot, or pinned.
    Costs nothing.

    Pass exactly one of `image` (a repo-relative path) or `ref` (a pinned
    reference name). `source` records which this was - do not launder an
    uploaded frame as a generated one or the reverse. approve=True marks it
    approved in the same call; only if you are the one who decided.
    Full notes: docs/tools.md#storyboard_frame_attach
    """
    from bgate_core.cine import storyboard as _sb

    return _sb.frame_attach(_root(), name, int(idx), image=image, ref=ref,
                            approve=bool(approve))


@_tool
def storyboard_frame_set(name: Annotated[str, Field(description='Board name.')], idx: Annotated[int, Field(description='Zero-based frame index on the board.')], beat: Annotated[Optional[str], Field(description='What happens, in story terms.')] = None,
                         action: Annotated[Optional[str], Field(description='What is VISIBLE and moving; what the image model reads.')] = None,
                         camera: Annotated[Optional[str], Field(description='Shot size and movement ("low angle wide", "slow push in").')] = None,
                         dialogue: Annotated[Optional[str], Field(description='A spoken line.')] = None,
                         duration: Annotated[Optional[int], Field(description='Seconds this beat runs as a shot.')] = None,
                         note: Annotated[Optional[str], Field(description='Anything for the human reading the board.')] = None,
                         status: Annotated[Optional[str], Field(description='empty | generating | drafted | approved | cut. Approving a frame with no image is refused.')] = None,
                         slug: Annotated[Optional[str], Field(description='Short identifier for the frame.')] = None) -> dict:
    """Edit one frame's text, timing or status without touching the rest.

    status is empty | generating | drafted | approved | cut. APPROVING A FRAME
    WITH NO IMAGE IS REFUSED - a shot promoted from it would be bought against
    prose alone, which is the thing this board exists to prevent.

    An image is changed with storyboard_frame_generate or _frame_attach, never
    here, so how it got there is always recorded.
    """
    from bgate_core.cine import storyboard as _sb

    fields = {"beat": beat, "action": action, "camera": camera,
              "dialogue": dialogue, "duration": duration, "note": note,
              "status": status, "slug": slug}
    return _sb.frame_set(_root(), name, int(idx),
                         **{k: v for k, v in fields.items() if v is not None})


@_tool
def storyboard_frame_add(name: str, beat: str = "", action: str = "",
                         camera: str = "", dialogue: str = "",
                         duration: int = 5,
                         after: Optional[int] = None) -> dict:
    """Insert one frame. At the end by default, or straight after `after`.
    Everything below it shifts down and keeps its drawing."""
    from bgate_core.cine import storyboard as _sb

    return _sb.frame_add(_root(), name, beat=beat, action=action,
                         camera=camera, dialogue=dialogue,
                         duration=duration,
                         after=None if after is None else int(after))


@_tool
def storyboard_frame_cut(name: str, idx: int) -> dict:
    """Mark a frame cut. It stays on the board and stays out of the promotion.

    Cut rather than deleted because a drawn frame was paid for, and an argument
    about whether the scene needs it is one you may lose twice.
    """
    from bgate_core.cine import storyboard as _sb

    return {"ok": True, **_sb.frame_cut(_root(), name, int(idx))}


@_tool
def storyboard_reorder(name: str, order: list) -> dict:
    """Re-sequence a board. `order` lists every current index in its new order.

    Every live frame must appear exactly once - a partial list is refused rather
    than interpreted, because a reorder that quietly dropped a frame would throw
    away an image somebody paid for.
    """
    from bgate_core.cine import storyboard as _sb

    return {"ok": True, **_sb.frame_reorder(_root(), name, list(order or []))}


@_tool
def storyboard_promote(name: str, sequence_name: str = "", model: str = "",
                       resolution: str = "720p",
                       allow_unanchored: bool = False) -> dict:
    """Turn an approved board into a cutscene shot list ready to be bought.
    THIS IS THE LINE between free and paid.

    Each frame's image becomes that shot's `first_frame`; style, refs and
    aspect ratio ride along. REFUSES BY DEFAULT on a board whose live frames
    are not all approved and drawn, naming which; allow_unanchored=True is for
    the deliberate case only. Cut frames do not travel. Read the result with
    cinematic_sequences, then buy one shot at a time.
    Full notes: docs/tools.md#storyboard_promote
    """
    from bgate_core.cine import storyboard as _sb

    return _sb.promote(_root(), name, sequence_name=sequence_name,
                       model=model, resolution=resolution,
                       allow_unanchored=bool(allow_unanchored))


@_tool
def storyboard_delete(name: str, drop_images: bool = False) -> dict:
    """Remove a board. Its generated images stay on disk unless you ask
    otherwise - they were paid for, and a deleted row is not a reason to burn
    them."""
    from bgate_core.cine import storyboard as _sb

    return _sb.delete(_root(), name, drop_images=bool(drop_images))


