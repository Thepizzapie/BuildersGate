"""Cinematics + storyboards MCP tools - carved out of server.py, verbatim.

server.py held ~226 tools in 12k lines; the domains that never
touch each other now live apart. The contract is unchanged: the
shared plumbing (_tool, _root, the gates) stays in server, each
domain imports it back, and server star-imports this module at its
BOTTOM - by then its globals all exist, which is what makes the
circular import legal - so server.<tool> still answers for every
caller and test.
"""
from bgate_mcp.server import (  # noqa: F401
    Optional, _Path, _gate_images, _log,
    _provider_gate, _root, _tool, _work_item_id,
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
    from bgate_core import cinematic as _cine

    return {"ok": True, **_cine.options(_root())}


@_tool
def cinematic_plan(name: str, shots: list, logline: str = "", style: str = "",
                   style_note: str = "", style_refs: Optional[list] = None,
                   locations: Optional[list] = None,
                   model: str = "", aspect_ratio: str = "16:9",
                   resolution: str = "720p", audio_track: str = "",
                   audio_gain_db: float = 0.0, fade_in: float = 0.0,
                   fade_out: float = 0.0) -> dict:
    """Write a cutscene's shot list. SPENDS NOTHING - do this first, always.

    `shots` is a list of objects, in cut order. Each takes:
      action       REQUIRED. What happens in this shot.
      camera       the MOVE and the prose ("slow push in", "low angle, handheld")
      shot_size    the framing, from a fixed list: establishing, wide, full,
                   medium, medium_close, close, extreme_close, over_shoulder,
                   insert, cutaway. Fixed so coverage can be COUNTED.
      location     the slug of one of this sequence's `locations`
      dialogue     a spoken line, quoted into the prompt as speech
      duration     4-15 seconds, default 5. Write 5-10; past that expect drift.
      first_frame  a repo-relative path to an APPROVED still to open on
      last_frame   a still to land on. For a deliberate match cut ONLY.
      refs         repo-relative paths to reference stills for identity
      transition   how the PREVIOUS shot becomes this one: cut (default, free),
                   fade, dissolve, wipe. cinematic_transitions explains each.
      transition_s the handle, default 0.5s. A transition overlaps both shots,
                   so the cut is SHORTER than the sum of its durations.
      vo           a voice-over clip for this shot

    SOUND. audio_track is a repo-relative path to the bed laid under the whole
    cutscene - a track the audio seat kept, or a hand mix. Without it the cut is
    SILENT: models generate audio baked into the picture, which cannot be
    separated, ducked or localised, so this pipeline keeps the picture clean and
    scores it here. audio_gain_db trims the bed under dialogue (-6 is a usual
    starting point); fade_in/fade_out fade both picture and sound.

    DIALOGUE BECOMES SUBTITLES automatically. Timing is derived from the shot
    durations and the transitions between them at assemble time, and written as
    both .srt (what a translator opens) and .json (what the delivered scene
    reads). Nothing stores caption timing, so it cannot drift from the shot list.

    STYLE - "cutscenes in whatever style" - has three levers, weakest first,
    and all three are applied to EVERY shot automatically:
      style       a preset KEY (cinematic_options lists them: anime, noir,
                  comic, painterly, pixel, stop_motion, cg_animated, watercolor,
                  vhs, silhouette, live_action) OR free prose. An unlisted word
                  is treated as prose rather than refused - whatever style means
                  whatever style.
      style_note  the project's own wording, appended to the preset
      style_refs  repo-relative paths to frames carrying the look. These BEAT
                  prose and are the only lever that holds across eight
                  generations.
    Naming no style is itself a choice, and a silent one: the model falls back
    to its own house look, which differs per model and per version. plan() says
    so rather than letting it pass.

    THE SET IS THE THIRD RAIL AND IT IS THE ONE THAT WAS MISSING. `locations` is
    a list of objects - slug, description, optional label and plates (repo-
    relative images of the set) - and each shot names one. That description is
    injected into EVERY shot filmed there, identically, in a fixed position.
    Without it the room lives inside each shot's own action prose, and four
    differently-worded descriptions of one office are four different offices:
    measured on real sequences, cast and style held across every shot and the
    SET drifted between all of them.

    GENERATE GROUPED BY LOCATION, NOT DOWN THE LIST. Two shots of one set agree
    with each other less the further apart they were generated, so buy a
    location's shots together - `generation_order` in the returned sequence is
    that order. The CUT is unaffected: cinematic_assemble joins by shot index.

    COVER THE BEATS TIGHT. Wides are both the flattest editorial choice and the
    most drift-prone thing to buy, because a wide shows the whole set and
    therefore shows every way the model disagreed with itself about it. A
    close-up contains almost no set to be inconsistent about. This returns
    advisory warnings when nothing is tighter than medium, when three shots in a
    row are the same size, and when a multi-location sequence establishes none
    of them.

    model picks which video model buys this sequence (cinematic_options lists
    what is registered and the exact ranges each accepts). It lives on the
    SEQUENCE because a cutscene generated half on one model does not cut
    together. Changing style or model resets already-generated shots - a clip
    rendered in the old look is not a rendering of the new one - and that is
    reported, because it means spending money again.

    ANCHOR EVERY SHOT. A text-only sequence invents the cast fresh each
    generation and no two shots agree on a face. Generate the keyframes through
    the art path first (image_generate conditioned on the pinned character), get
    them approved, then name them here. This returns a warning when no shot is
    anchored, and it is the most expensive warning in the product to ignore.

    NEVER point last_frame/first_frame at the previous shot's output. That is
    the art seat's rule 2 (chains decay) with a worse decay constant - a video
    model's final frame is the most drifted image it produced AND a lossy
    intermediate. Every shot anchors on the same approved stills.

    Re-running this to edit the list PRESERVES shots whose action text did not
    change, along with the clips already paid for; only changed shots reset.
    """
    from bgate_core import cinematic as _cine

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
    from bgate_core import cinematic as _cine

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

    ONE SHOT PER CALL, DELIBERATELY. There is no generate-the-whole-sequence
    tool: the thing a human has to do between shots is LOOK at the clip, and a
    loop is built to skip exactly that. Generate, watch, keep or re-generate,
    then move to the next index.

    generate_audio is FALSE by default and that is a considered default, not an
    oversight. Model audio is baked into the clip and cannot be separated
    afterwards, so it fights the score, cannot be ducked under dialogue and
    cannot be localised. The picture is this seat's; the sound is the audio
    seat's, laid over the top where it stays editable.

    Local conditioning frames are uploaded to the provider automatically. Both
    the budget and the encoder are checked BEFORE anything is charged.
    """
    from bgate_core import cinematic as _cine

    # Provider preflight only - the shot pipeline runs its own budget
    # arithmetic (the docstring's promise), but a drained kie account
    # refuses regardless of price and is cheaper to learn here.
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

    Refuses while any shot that is not marked 'cut' is unkept - assembling
    around a missing beat ships a story that does not make sense rather than an
    error. It also refuses a set of shots that are not all the same size,
    because ffmpeg joins those into a broken file and reports SUCCESS.

    The result is registered as a candidate like any other. WATCH THE WHOLE CUT
    before keeping it: shots were judged alone, and a cut is judged as a cut - the light jumping, a character swapping hands, the camera crossing the line
    are all invisible shot by shot.
    """
    from bgate_core import cinematic as _cine

    return _cine.assemble(_root(), name, quality=int(quality))


@_tool
def cinematic_candidates(logical_name: str = "", limit: int = 100) -> dict:
    """Generated shots and cuts awaiting a decision, plus what has been kept.

    Every row carries its artifact_id (what cinematic_keep / cinematic_discard
    take) and `installed`, which is true only when the file in the engine
    project was transcoded from THIS revision - so a superseded take cannot
    claim to be the clip the game loads.
    """
    from bgate_core import cinematic as _cine

    root, cap = _root(), max(1, min(int(limit), 500))
    return {"ok": True,
            "candidates": _cine.candidates(root, logical_name=logical_name,
                                           limit=cap),
            "kept": _cine.kept(root, limit=cap)}


@_tool
def cinematic_keep(artifact_id: int, note: str = "", quality: int = 6,
                   install_to_engine: Optional[bool] = None) -> dict:
    """Approve a take, and put it in the engine project if the engine loads it.

    WHAT GETS INSTALLED DEPENDS ON WHAT IT IS. An assembled CUT is transcoded to
    Ogg Theora and copied into the game - that is the asset the game plays. A
    SHOT is approved and stays in .bgate_out, because nothing references it: the
    game loads the cut, and cinematic_assemble reads the candidates directly.
    Installing every shot meant a Theora encode each and, at 1080p, tens of
    megabytes of files nobody asked for.

    THE TRANSCODE IS NOT A COPY, and that is why this is not music_keep with a
    different noun. Godot plays Ogg Theora and only Ogg Theora; the .mp4 every
    model returns produces NO IMPORT ERROR, so copying one in leaves a scene
    that runs perfectly with a blank rectangle where the cutscene was. The
    engine documentation's own settings are used (-q:v 6, keyframe interval 64),
    and the conversion happens BEFORE the approval so a failure can never leave
    a row saying approved over a game with no file.

    quality is 1-10, 6 is the documented baseline; drop to 5 for 1440p+.
    install_to_engine overrides the default either way - pass true for a single
    clip used on its own, as an attract-mode loop or a sting with no cut.
    """
    from bgate_core import cinematic as _cine

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
    from bgate_core import cinematic as _cine

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
    from bgate_core import cinematic as _cine

    return _cine.discard(_root(), int(artifact_id), note=note)


@_tool
def cinematic_register_model(name: str, model: str, intent: dict,
                             label: str = "", note: str = "",
                             enums: Optional[dict] = None,
                             ranges: Optional[dict] = None,
                             caps: Optional[dict] = None,
                             intent_values: Optional[dict] = None,
                             intent_scale: Optional[dict] = None,
                             credits: Optional[dict] = None) -> dict:
    """Add a video model from a reference page you have READ. Spends nothing.

    kie's market carries dozens of video models; this product ships only the
    ones whose id and schema were verified against their own documentation,
    because a guessed id is a 404 after a round trip and a guessed parameter
    name is a setting you paid for and did not get. That rule is not relaxed
    here - what this changes is WHO does the reading, so a user with the Kling
    or Sora page open is not blocked on a release.

      name    what to call it here, e.g. "kling-3"
      model   the LITERAL id kie wants in the top-level `model` field
      intent  what this model calls each of: seconds, shape, quality,
              first_frame, last_frame, refs, audio. Omit any it cannot do - asking for one it has no field for is then refused before the
              spend instead of being silently dropped.

    Optional, and worth filling in because they are checked before money moves:
      enums / ranges / caps   this model's own limits, keyed by ITS field names
      intent_values           {intent: {canonical: this model's spelling}} - e.g. shape 16:9 -> "landscape"
      intent_scale            {intent: multiplier} - e.g. seconds -> n_frames

    Registered models are stamped source="registered" everywhere they are
    listed, so nothing confuses your entry for a verified one. The registration
    lives for the life of this server process.
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

    Read this between cinematic_plan and the first cinematic_generate_shot. A
    shot list is the only artifact here that can be reviewed for nothing, and
    an eight-shot sequence is eight paid generations - the argument about
    whether shot 3 earns its place is much easier with the bill next to it.

    AN UNKNOWN PRICE IS REPORTED AS UNKNOWN, NEVER AS ZERO. kie publishes credit
    bands rather than per-model prices, so shots on an unrated model come back
    in `unknown_shots` and are left OUT of the total; `usd` is null, not 0.0. A
    total that silently omitted them would read as "this is cheap".

    The numbers are an upper bound derived from kie's published band, not read
    off an invoice. Set BGATE_KIE_USD_PER_CREDIT once you have real figures, or
    BGATE_KIE_VIDEO_CREDITS to correct a model's rate without a code change.
    """
    from bgate_core import cinematic as _cine

    return {"ok": True, **_cine.estimate_sequence(_root(), name,
                                                  model=model)}


@_tool
def cinematic_stuck_shots(older_than_s: int = 0, poll: bool = True) -> dict:
    """Find generations that were PAID FOR and never collected. This is the tool
    that finds money.

    A generation is charged at submit. Everything after that - the poll loop,
    the download, this process surviving the ten minutes it takes - can fail
    while the provider sits on a finished clip you have already been billed for.
    Nothing surfaces that on its own; a shot row simply stays at 'generating'
    forever and looks like work in flight.

    Run this after any dashboard restart, any killed agent, and before planning
    a re-generation of a shot that "failed". The classification to act on is
    `recoverable`: the clip is finished and waiting, and cinematic_recover_shot
    collects it WITHOUT paying again. Pressing generate instead pays twice.

      older_than_s  how stale a 'generating' row must be to be suspicious.
                    Defaults to the module's own threshold.
      poll          ask the provider about each one. False answers from the
                    database alone and never leaves the machine.

    `lost` means the row has no task id at all - the submit failed before it
    returned one, so there is probably nothing to collect and nothing was
    charged. `unknown` means the provider was asked and did not say.
    """
    from bgate_core import cinematic as _cine

    return {"ok": True, **_cine.stuck_shots(
        _root(),
        older_than_s=int(older_than_s) or _cine.STUCK_AFTER_S,
        poll=bool(poll))}


@_tool
def cinematic_probe_model(name: str, timeout: float = 30.0) -> dict:
    """Ask kie whether a registered model id actually exists. Opt-in, and READ
    THE CAVEAT.

    cinematic_register_model takes a model id on trust - kie publishes no
    catalogue endpoint, so a typo passes registration cleanly and surfaces as a
    PAID 404 at generation time. This narrows that window by submitting a
    deliberately empty request and reading which error comes back: 404 means the
    id is wrong, 422 means the id resolved and only the arguments were missing.

    IT IS INFERENCE, NOT A CONTRACT. The 404-vs-422 split is read off kie's
    error table, not documented behaviour, and the case it cannot rule out is a
    model that ACCEPTS an empty input and starts a billable job. If that happens
    the returned task id is reported loudly rather than swallowed - treat it as
    a real charge and collect it with cinematic_recover_shot.

    Registered models are marked unverified until this says otherwise. An
    unverified model is not a broken one; it is one nobody has confirmed.
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
    from bgate_core import cinematic as _cine

    return {"ok": True, "styles": _cine.styles(),
            "fallback": _cine.STYLE_FALLBACK}


@_tool
def cinematic_shot_status(task_id: str) -> dict:
    """Where a submitted generation got to at the provider. Costs nothing.

    This only LOOKS. When it says `recoverable`, cinematic_recover_shot is what
    puts the clip on disk - do NOT re-run cinematic_generate_shot to get a file
    for a task that has already been charged.
    """
    from bgate_core import cinematic as _cine

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
    from bgate_core import cinematic as _cine

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
    from bgate_core import cinecut as _cut

    return {"ok": True, "transitions": _cut.TRANSITIONS,
            "default": _cut.DEFAULT_TRANSITION}


@_tool
def cinematic_continuity(name: str) -> dict:
    """Do this sequence's shots actually CUT TOGETHER? Costs nothing but time.

    The measured half of the seat's "watch it twice" rule. It extracts the real
    frames either side of every join and compares overall brightness and colour
    palette - on the pixels, never on the prompts, because the whole reason a
    cut fails is that the model did something other than what was asked.

    IT CANNOT TELL YOU THE CUTSCENE IS GOOD and does not try. A cut from a
    cellar to a snowfield SHOULD jump in brightness. Every finding says what it
    measured and leaves the verdict to a human.

    Run it BEFORE assembling: the fix for a real mismatch is re-generating a
    shot, which is a decision to make before paying for the assembly - or
    softening the join with a dissolve, which is what a dissolve is for.
    """
    from bgate_core import cinematic as _cine

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

    CALL THIS BEFORE cinematic_generate_shot. EVERY TIME. Between planning a
    sequence and buying it there was nothing at all, which means the first human
    to see the EDIT saw it after every second of it had been paid for. By then
    the only cheap change left is deleting shots. This is the stage that makes
    the scene wrong in a place where being wrong is free.

    That is not a nicety borrowed from film school, it is the arithmetic. Hand
    animation runs at about one finished second per animator-hour, which is why
    nobody animates an unproven edit - they cut the boards together first and fix
    it there. Generated video costs MORE per second than that, and this pipeline
    had no previs at all.

    WHAT COMES BACK, AND WHAT TO DO WITH IT:
      average_shot_s   read this first. Modern films sit at 4-6s. Under 4 and
                       the cut is a montage nobody follows; over 6 and it is a
                       slideshow of stills. It is one number and it tells you
                       whether the edit reads.
      runtime_s / measured_s   what the shot list adds up to, and what the file
                       actually is. They disagree only when something is wrong,
                       and captions are timed off the first one.
      placeholders     beats with no still yet. They are rendered as slate cards
                       held for their full duration, never skipped - a gap in
                       the edit is information, and a reel that quietly ran
                       short would read as finished.
      warnings         untimed shots, two consecutive shots describing the same
                       beat, pacing outside the window. All advisory.

    `source` is "auto" (the planned sequence if there is one, else the board),
    "sequence" or "board". The sequence is preferred because that is the row
    money is spent against and the only one carrying transitions.

    The reel is an .mp4 under design/cinematics/animatics/ - H.264, not the
    Theora the shipped cutscene uses, because this is watched by a person in a
    browser and never by the engine. The panels come back as images so you can
    look at the edit rather than reading a runtime.
    """
    from bgate_core import animatic as _anim

    return _anim.build(_root(), name, source=str(source or "auto"),
                       fps=int(fps), burn_captions=bool(burn_captions))


@_tool
def cinematic_deliver(name: str, force: bool = False) -> dict:
    """Build the Godot scene that PLAYS this cutscene. The last mile.

    Keeping a cut installs an .ogv and prints a res:// path, and that is where
    this pipeline used to stop - leaving a designer to hand-author a
    VideoStreamPlayer, wire a skip input, drive the captions and work out how to
    hand control back to gameplay. This writes all four.

    What you get, beside the .ogv in the engine project:
      <name>.tscn   a CanvasLayer at layer 100, so it draws over whatever is
                    already rendering - 2D, 3D or the HUD
      <name>.gd     plays, draws captions off the video's own clock, skips on
                    ui_cancel/ui_accept, and emits `finished(skipped: bool)`
      <name>.srt    the caption file a translator opens
      <name>_captions.json  what the script reads at runtime

    The contract is ONE signal. `finished` fires whether the video ended or the
    player skipped, because every caller wants the same thing next and branching
    on which is how a skipped cutscene leaves a game on a black screen.

    Gameplay calls it with three lines:
        var cut := preload("res://.../<name>.tscn").instantiate()
        add_child(cut)
        await cut.finished

    IT WILL NOT OVERWRITE A SCRIPT YOU HAVE EDITED. The .gd is meant to be
    changed - a project will want its own skip input or a letterbox - so
    delivery detects a hand-edited file and keeps it. Pass force to replace it.
    """
    from bgate_core import cinematic as _cine

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
def storyboard_auto(name: str, premise: str = "", frames: int = 6,
                    style: str = "", style_note: str = "",
                    cast_refs: Optional[list] = None,
                    aspect_ratio: str = "16:9", quality: str = "low",
                    promote_to: str = "", model: str = "") -> dict:
    """Premise in, finished storyboard out, in ONE call. START HERE.

    THIS IS THE DEFAULT DOOR FOR "MAKE ME A CUTSCENE" and the other storyboard
    tools are its parts, for when you need to change one thing. Do not hand-run
    write_script then six frame_generates then promote: that is this tool with
    five extra places to stop, and stopping to ask about something the brief
    already answered is the failure mode this exists to remove.

    WHAT IT DOES WITHOUT ASKING:
      * No cast pinned? It derives one - character pins first, canon lore
        entities second - and conditions every frame on it, so the look holds
        across the board. An underspecified cast is a reason to go looking, not
        a reason to stop and file a note.
      * No style? The project bible's locked art direction is appended at the
        generation door regardless, so the game's look applies anyway.
      * No beats? It writes them from the premise for a fraction of a cent.
      * A frame fails? The rest still draw. You get a partial board and a named
        list of what failed, which is worth more than a refusal.

    COST: images only, and cheap - `quality="low"` is the default here because
    a board is read at a glance. Six frames is a few tens of cents. It does NOT
    buy video: promote_to writes the shot list, which is free, and
    cinematic_generate_shot spends per shot as a separate decision.

    Re-running is safe and does not re-buy: a frame that already has an image is
    kept and approved rather than redrawn, and a board that already has beats
    keeps them rather than having a model overwrite somebody's edits.
    """
    from bgate_core import storyboard as _sb

    refused = _gate_images(_root(), max(1, int(frames)), quality,
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
    of a cent. START HERE when you know what the scene is ABOUT but not yet what
    is in it.

    This writes prose and beats. It draws NOTHING and buys no video. The board it
    creates is a plan you can argue with, reorder and throw away for free, which
    is the entire reason it exists in front of cinematic_plan.

      premise     one or two sentences. What happens in this scene.
      frames      how many beats to break it into, 1-24. Default 6.
      cast_refs   PINNED REFERENCE NAMES for who is in this scene. Every one
                  contributes its stored profile so the script is written about
                  THIS project's characters rather than plausible strangers.
                  Pin them with ref_pin first; ref_list shows what exists.
      characters  anything about the cast the pins do not say
      style       a cinematic_styles preset key, or free prose

    THE CAST IS THE POINT. A script written without it invents people nobody has
    drawn, and every frame then anchors on a stranger. Pass cast_refs.

    Re-running replaces the board's beats. Frames that already have an image keep
    it at the same index, so re-writing the script does not throw away drawings.
    """
    from bgate_core import storyboard as _sb

    return _sb.write_script(
        _root(), name, premise, frames=int(frames), style=style,
        style_note=style_note, cast_refs=list(cast_refs or []),
        characters=characters, aspect_ratio=aspect_ratio)


@_tool
def storyboard_plan(name: str, frames: Optional[list] = None, premise: str = "",
                    logline: str = "", style: str = "", style_note: str = "",
                    style_refs: Optional[list] = None,
                    cast_refs: Optional[list] = None,
                    aspect_ratio: str = "16:9") -> dict:
    """Write or edit a storyboard by hand. SPENDS NOTHING.

    `frames` is a list of objects, in scene order. Each takes:
      beat      what happens, in story terms. Required unless action is given.
      action    what is VISIBLE and moving. This is what the image model reads.
      camera    shot size and movement ("low angle wide", "slow push in")
      dialogue  a spoken line
      duration  seconds this beat will run as a shot. Default 5.
      refs      frame-specific pinned reference names, on top of the cast
      note      anything for the human reading the board

    OMIT `frames` ENTIRELY to edit the board's own fields - cast, style, premise - and leave the drawings alone. That is how you re-cast a board you have
    already drawn without paying to draw it again.

    A frame that already has an image KEEPS it when the board is re-planned at
    the same index. Images are the only thing here that cost money.

    storyboard_write_script does this from a premise with one cheap model call.
    Use this to fix what it wrote, or when you already know your beats.
    """
    from bgate_core import storyboard as _sb

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
    from bgate_core import storyboard as _sb

    return {"ok": True, "boards": _sb.boards(_root(), limit=int(limit))}


@_tool(images=_frames_images)
def storyboard_open(name: str) -> dict:
    """One board: its script, its cast, every frame in order, and whether it can
    be promoted yet. The drawn frames come back as images, so you can LOOK at the
    scene rather than reading paths.

    `ready.blockers` is the specific list of what stands between this board and
    a paid sequence. Read it before reaching for allow_unanchored.
    """
    from bgate_core import storyboard as _sb

    return {"ok": True, **_sb.board(_root(), name)}


@_tool(images=_board_images)
def storyboard_frame_generate(name: str, idx: int, prompt: str = "",
                              provider: str = "", model: str = "",
                              refs: Optional[list] = None,
                              use_cast: bool = True, ref_strength: float = 0.5,
                              quality: str = "medium") -> dict:
    """Draw ONE storyboard frame. This is the only tool here that costs money,
    and it is an IMAGE - roughly two orders of magnitude cheaper than the video
    shot it exists to stop you buying blind.

    ONE FRAME PER CALL, deliberately. A loop that draws the whole board has
    nowhere to stop when frame 2 comes back wrong.

    CONDITIONING IS WHY THIS BEATS A BARE image_generate. The board's cast_refs
    and style_refs are resolved and passed as reference images automatically, so
    frame 6 is drawn against the same character files as frame 1. That is the
    drift this whole subsystem exists to prevent.

      refs         extra pinned names for THIS frame only
      use_cast     False for a frame with nobody in it (an empty room). A
                   character reference on a shot with no character is noise the
                   model has to fight.
      quality      low | medium | high. Boards are read at a glance; low is
                   usually enough and costs about a quarter of medium.

    The prompt is built from the frame's action, camera and the board's style
    unless you pass `prompt` to override it outright.

    Comes back as 'drafted', never 'approved'. A human or a judging pass decides
    that, because approval is what lets a shot be bought against this frame.
    """
    from bgate_core import storyboard as _sb

    refused = _gate_images(_root(), 1, quality, "a storyboard frame")
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

    Pass exactly one of:
      image   a repo-relative path to a file already in the project
      ref     a pinned reference name (ref_list shows them)

    THE HUMAN PATH, and it is first-class rather than a fallback. A frame a
    person chose is better evidence for spending video money than one a model
    guessed, so `source` records which this was. Do not launder an uploaded
    frame as a generated one or the reverse.

    approve=True marks it approved in the same call. Only do that if you are the
    one who decided, not merely the one who attached it.
    """
    from bgate_core import storyboard as _sb

    return _sb.frame_attach(_root(), name, int(idx), image=image, ref=ref,
                            approve=bool(approve))


@_tool
def storyboard_frame_set(name: str, idx: int, beat: Optional[str] = None,
                         action: Optional[str] = None,
                         camera: Optional[str] = None,
                         dialogue: Optional[str] = None,
                         duration: Optional[int] = None,
                         note: Optional[str] = None,
                         status: Optional[str] = None,
                         slug: Optional[str] = None) -> dict:
    """Edit one frame's text, timing or status without touching the rest.

    status is empty | generating | drafted | approved | cut. APPROVING A FRAME
    WITH NO IMAGE IS REFUSED - a shot promoted from it would be bought against
    prose alone, which is the thing this board exists to prevent.

    An image is changed with storyboard_frame_generate or _frame_attach, never
    here, so how it got there is always recorded.
    """
    from bgate_core import storyboard as _sb

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
    from bgate_core import storyboard as _sb

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
    from bgate_core import storyboard as _sb

    return {"ok": True, **_sb.frame_cut(_root(), name, int(idx))}


@_tool
def storyboard_reorder(name: str, order: list) -> dict:
    """Re-sequence a board. `order` lists every current index in its new order.

    Every live frame must appear exactly once - a partial list is refused rather
    than interpreted, because a reorder that quietly dropped a frame would throw
    away an image somebody paid for.
    """
    from bgate_core import storyboard as _sb

    return {"ok": True, **_sb.frame_reorder(_root(), name, list(order or []))}


@_tool
def storyboard_promote(name: str, sequence_name: str = "", model: str = "",
                       resolution: str = "720p",
                       allow_unanchored: bool = False) -> dict:
    """Turn an approved board into a cutscene shot list ready to be bought.
    THIS IS THE LINE between free and paid.

    Each frame's image becomes that shot's `first_frame`, which is exactly the
    "anchor on an approved still" the cinematic seat has always required and
    previously had no path to produce. Style, style refs and aspect ratio ride
    along, so the shots are bought under the look the board was approved under.

    REFUSES BY DEFAULT on a board whose live frames are not all approved and
    drawn, and names which ones. allow_unanchored=True is for the deliberate
    case only - every shot in what comes out of this is a paid generation.

    Cut frames do not travel. What comes back is a cine_sequence: read it with
    cinematic_sequences, then buy it one shot at a time with
    cinematic_generate_shot.
    """
    from bgate_core import storyboard as _sb

    return _sb.promote(_root(), name, sequence_name=sequence_name,
                       model=model, resolution=resolution,
                       allow_unanchored=bool(allow_unanchored))


@_tool
def storyboard_delete(name: str, drop_images: bool = False) -> dict:
    """Remove a board. Its generated images stay on disk unless you ask
    otherwise - they were paid for, and a deleted row is not a reason to burn
    them."""
    from bgate_core import storyboard as _sb

    return _sb.delete(_root(), name, drop_images=bool(drop_images))


