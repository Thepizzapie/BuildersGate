# Tool notes

The long-form notes behind each MCP tool's docstring. Every tool's
docstring now carries the headline, what it does and the rules an
agent must know to call it; the rest of the original text lives here,
verbatim, under the tool's name. `docs/reference.md` is the surface
reference; this file is the per-tool detail.

## agent_activity

```text
Watch a dispatched agent work - its recent steps, result and liveness.

The read half of agent_steer: queue_add hands work out, agent_steer
corrects it mid-run, and this is how you SEE what the run is doing before
deciding either. Everything comes off disk (the item's stream-json log
plus the machine-wide agent registry), so it works from any session -
CLI, desktop, a second dashboard - whether or not `bgate serve` is up.

Returns the last ``limit`` steps ("say" / "tool" / "result" / "steer"),
the run's final result event if it has one, `running` (True/False, or
null when the host will not vouch for the pid), and the log path for
anyone who wants the whole transcript. `step_count`/`truncated` say
whether the window is the full story.

Read this BEFORE clearing or re-dispatching a failed item: the last few
steps almost always name the actual wall the agent hit, and a
queue_reopen whose reason quotes it beats one that guesses.
```

## agent_steer

```text
Say something to the agent currently running a work item, mid-run.

The director's other half. queue_add hands work OUT; this is how you correct
it while it is happening - "that pose is off-model, use the pinned ref",
"stop widening the scope, ship the three screens" - without killing the run
and paying for it twice.

The message is left in the project's steer inbox and delivered by the
dashboard, which is the process that owns the agent's input pipe. So:

  * it needs `bgate serve` to be running to land;
  * the agent reads it when its CURRENT step ends, not instantly;
  * an item with no live agent gets no delivery - check queue_list(
    status='dispatched') first, and use queue_update or queue_reopen for
    work that is not running.

  * A STEER IS CAPPED AT 2000 CHARACTERS. It is an interruption, not a
    brief. Past the cap the text is written to a file in the project's steer
    box and the agent is handed the opening paragraph plus that path - so a
    long correction reaches a RUNNING agent instead of forcing you to kill
    the run and pay for it twice, which is what the cap used to mean in
    practice. Truncation is still never on the table: half a sentence with
    no way to know it was cut is worse than either.

    A correction that should OUTLIVE the run is still not this tool. This
    one dies with the process it was aimed at, which is the honest lifetime
    for "no, not like that"; a change to the work itself goes in
    queue_update's brief or a queue_reopen.

Delivery, and any failure to deliver, is recorded in the activity ledger
against the item.
```

## agent_steer_all

```text
Say ONE thing to every agent running right now.

For the correction that is not about one item: the art direction changed,
the file everybody is about to touch is moving, stop writing to the old
path. Steering that agent by agent means retyping the same sentence four
times and the last one hears it a minute after the first.

Same delivery as agent_steer - the steer inbox, read by each agent when its
current step ends - and the same caps. Returns one row per item it reached,
so "who did not get this" is answerable: an item whose runner takes its
prompt at launch has no live channel and is reported as refused rather than
silently skipped.

Aim carefully. This reaches seats working on unrelated things, and a
sentence that only makes sense to the art seat is noise in the middle of a
tech run.
```

## animation_contacts

```text
Where a character's feet ACTUALLY are, frame by frame - the question
animation_curves structurally cannot answer.

Every metric in animation_curves reads a channel's raw local values, which
is right for "is this curve smooth" and wrong for anything about where a
body part IS. A foot bone on a skinned humanoid has no translation channel
at all - it moves because a hip and a knee rotate above it - so the foot
skate check over there has never run on a real character. It read a
constant and honestly said so.

This runs forward kinematics off the file, composing each joint onto its
parent per frame, and then measures what only world positions can show:

  support      how many feet are down on each frame, and FLIGHT - frames
               with nothing down at all. Judged only against a DECLARED
               `gait`, because the identical number is correct for a run
               and impossible for a walk; an undeclared gait returns the
               measurement with a refusal rather than a pass.
  contact      the planted foot's ground-plane speed. Judged against the
               clip's own convention, which the evaluator detects: a
               ROOT-MOTION clip should hold its planted foot still, an
               IN-PLACE clip must slide it at a STEADY speed, because
               there the foot is the ground. Judging in-place clips
               against zero would fail every correct locomotion loop in
               the project.
  clearance    frames where a foot passes below the floor. Pass `floor`
               (usually 0.0) for the real one; the default asks the weaker
               question of whether it dips below its own resting contact.

`feet` names the contact joints exactly (LeftFoot/RightFoot on this
project's humanoids, the four paws on a quadruped). Without it, joints
whose names look like feet are used and the guess is reported.

`gait` is one of walk, run, stand, any. It is a declaration about what the
clip was MEANT to be, and there is deliberately no default - see the
support verdict.
```

## animation_curves

```text
Measure an exported animation clip's curves - no Blender/Godot needed.

Reads a GLB's animation channels directly (glTF is a public format, so
this is a plain file parse, not another headless spawn) and reports, per
channel:

  arc_deviation      (translation only) how far the path bows from the
                     straight line between its endpoints. DESCRIPTIVE,
                     not pass/fail - an arc is right for a swinging limb
                     and wrong for a jab's extension, and this cannot
                     tell which the clip is doing.
  velocity_profile   what fraction of the clip's DURATION is spent near
                     its own peak speed. High means the motion travels
                     at near-constant speed rather than easing in/out - the curve-math signature of raw linear-interpolated
                     keyframes.
  concentration      THE OPPOSITE TAIL OF THAT SAME PROFILE, and
                     velocity_profile is blind to it: what share of a
                     track's whole travel lands in its fastest tenth of
                     frames, against what even pacing would put there.
                     1.0 is evenly paced, 1.5 a clean sine swing; a clip
                     whose entire pose change happens in two frames with
                     a drift around it runs 4x and up. A snap is about as
                     far from constant-speed as a curve gets, so it sails
                     through the check above.
  sparc              spectral arc length of the speed profile - a
                     smoothness/jitter measure from the mocap-cleanup
                     literature. Its threshold is a starting point
                     borrowed from gait research, not yet validated on
                     this project's own stylized clips - treat FAILs as
                     worth a look, not as certain defects.
  anticipation       EXPERIMENTAL, per axis. Laplacian-of-Gaussian
                     correlation looking for curvature spread across a
                     transition (shaped, eased, wound-up) vs. a narrow
                     spike (a raw interpolated corner). No prior art
                     exists for this as a detector - the cited research
                     (Wang/Xu/Cohen SIGGRAPH 2006) shows the FORWARD
                     direction, that this filter CREATES anticipation;
                     using it to detect whether anticipation is already
                     present is this project's own experiment. Also has
                     a real resolution floor: quick transitions sampled
                     at only a few frames are unreliable to call either
                     way. Set check_anticipation=False to skip it.

`foot_bones` (channel node names, exact match) additionally get
foot_skate: frames where the bone sits near its lowest point in the clip
but still moves horizontally - a planted foot sliding.

None of this measures appeal or exaggeration - nothing computational
does. A clean pass here means "no obvious curve-math defect", not
"looks good"; it is a floor, not a ceiling.
```

## animation_library

```text
Which hand-keyed CC0 clip packs are fetched, and what is in them.

THE OTHER SOURCE OF CLIPS. blender_animate's procedural clips are correct
and engine-generic; a pack keyed by an animator is the difference between
the character walking and the character being someone. A library clip rides
into blender_animate as {"clip": "Walk_Loop", "name": "walk"} (pack defaults
to quaternius-ual) and is RETARGETED onto the rig: world-space rotation
deltas from the pack skeleton's rest, turned by the yaw between the two
rigs' measured forwards, applied to the target's rest after each bone has
been aligned along the pack bone's rest direction - so a T-posed pack drives
an A-posed rig, bone roll is irrelevant, and a rig facing the other way
still lands. Hips translation scales by the ratio of hip heights. Fingers
are not mapped (no rig here has them) and are listed as unmapped.

THIS TOOL NEVER DOWNLOADS. A missing pack is fetched by the owner:

    bgate animlib status              what is fetched, and the command if not
    bgate animlib fetch quaternius-ual  commit-pinned zip, SHA-256 checked
    bgate animlib list quaternius-ual   every clip, its length, loop / root-motion

into ~/.bgate/animlib (BGATE_HOME moves it), shared by every project, in no
repository. `bgate doctor` carries an anim_library row. The same rule keeps
key-writing out of an agent's hands: a tool that can pull files onto the
machine is a tool that can be talked into pulling the wrong one.

Packs: quaternius-ual - Quaternius' Universal Animation Library, Standard
tier, glTF mirror (CC0-1.0, github.com/J-Ponzo/gltf-universal-animation-
library). 46 clips: Idle_Loop, Walk_Loop, Jog_Fwd_Loop, Sprint_Loop,
Crouch_Idle_Loop, Crouch_Fwd_Loop, Jump_Start/Loop/Land, PickUp_Table,
Interact, Sitting_*, Pistol_*, Sword_*, Spell_Simple_*, Punch_*, Hit_Chest,
Hit_Head, Death01, Dance_Loop, Push_Loop, Swim_*, Roll, Roll_RM, Driving,
Fixing_Kneeling, Idle_Talking_Loop, Idle_Torch_Loop, Walk_Formal_Loop,
A_TPose. Names ending _Loop cycle; _RM variants carry root motion on the
pack's own root bone, which is not retargeted - use the in-place twin.
```

## animation_generate

```text
CONTRACT-DRIVEN character animation via Retro Diffusion.

Reads the sprite contract (sprite_contract_get) for this character+action:
which directions to draw, cell size, frame count, layout. For each DRAWN
direction it takes a start frame from the character's existing sheet,
sends it to RD's purpose-trained animation model (~$0.14/direction), runs
the full battery (facing, height, motion, palette conform when pinned),
and stitches the contract-shaped sheet + SpriteFrames .tres with
animations named {action}_{direction}. Mirrored directions are reported
for runtime flip_h - the contract's mirror map, no pixels duplicated.

source_sheet: the sheet whose row-0-per-direction cells seed each start
frame; defaults to the character's idle sheet, then the action's own.
Start-frame quality decides identity fidelity: in-distribution characters
(small, game-styled) come back intact; large detailed characters get
redrawn at ~70% and this tool says so rather than hiding it.
```

## art_qa_verdict

```text
Record an INDEPENDENT art-QA reviewer's verdict on a candidate artifact.

For the art-consistency reviewer (a seat that did NOT make the image) after
it has run consistency_check and looked at the produced image beside its
reference. The score (0-100 similarity) and reasons are stored on the
revision under metadata.qa_review so the dashboard can show why.

verdict 'fail' REJECTS the revision outright - refusing to ship something is
a call a machine is allowed to make alone. verdict 'pass' does NOT approve
it: the pass is recorded and the revision stays a candidate, marked
machine-checked and queued for a human to approve in the dashboard. An LLM's
opinion is evidence; only a person promotes evidence to canon.

Returns {ok, artifact_id, verdict, score, status, awaiting_human,
logical_name, revision}. `status` is the revision's status AFTER the call:
'rejected' on fail, still 'candidate' on pass.
```

## art_tournament_standings

```text
Elo standings for a target, derived fresh from its decided matches.

`tournament_ref` scopes the result to ONE tournament. Left empty, every
match ever recorded for `logical_name` is pooled - which is the right
answer for a target run once, and misleading for one re-tournamented after
new candidates landed, since the two runs' verdicts then rate each other's
candidates. Pass the ref the tournament was opened with to separate them.

Returns {logical_name, standings: [{artifact_id, rating, matches, wins}]
ranked highest first, decided_matches, pending_matches}. An artifact
with no matches yet does not appear - it has no rating to report.
```

## art_tournament_verdict

```text
Record an INDEPENDENT reviewer's pick in ONE pairwise art match.

art_qa_verdict asks "is this on-model" against a reference. This asks a
different question a reviewer only sees when dispatched against a
tournament brief: given two candidates shown in a RANDOMISED order - fixed when the match was opened, before you were asked, and not something
you control or need to look up - which one is better. The brief you were
dispatched with lists each match's two images in that settled order and is
the only place match ids come from.

PICK A WINNER - do not skip a match because it feels close. The
VLM-as-judge research behind this tool is consistent that comparative
judgement ("which is better") is far more reliable than an absolute
score, but only if every match actually resolves; an abstained match
just removes one data point from a rating that already has few of them.

winner_artifact_id must be one of the two candidates IN THIS match - an
id from a different match or a different logical_name is refused, not
silently recorded. So is a second verdict on a match already decided:
re-sending after a dropped response used to overwrite the first pick in
silence, so a retry is now an error rather than a rewrite.

Returns {ok, match_id, logical_name, winner_id}. The rating itself is
NOT computed here - see art_tournament_standings, which derives it fresh
from every decided match so a corrected verdict can never leave a stale
number behind.
```

## aseprite_antialias

```text
Soften stair-step corners in a sprite or tile PNG. Free and local.

The rule (C.T. Matthews' extension, headless): a pixel with two or more
orthogonal neighbours of one same other colour is a stair-step corner
and becomes the midpoint of the pair; straight edges are untouched, and
transparent pixels are never written, so silhouettes do not grow. With
use_pinned_palette (default) every blended pixel snaps back into the
project's pinned palette - the pass cannot mint colours the conform
would reject. Writes beside the source as <stem>_aa.png unless `out`
names a path. Run it AFTER generation and conform, before delivery.
```

## aseprite_export

```text
Hand-edited .aseprite -> sheet PNG + EXACT SpriteFrames .tres.

The export JSON states every frame's rect, duration and tag, so the .tres
is a translation of facts, not a grid guess - per-animation speeds and
per-frame holds survive exactly as authored in Aseprite. This is the way
back from a hand edit: fix frames in Aseprite, save, call this, import
the pair into the game at res://<res_dir>/. Output lands beside the
master (or in out_dir), named <stem>_sheet.png + <stem>_frames.tres.

Also, when present in the master:
- SLICES named after rig slots (main_hand, off_hand, muzzle, ... - drag
  one over the hand, key it per frame) become EXACT per-frame anchors:
  merged into <stem>_sheet.rig.json and emitted as <stem>_offsets.json,
  which gear_rig.gd's `offsets` parameter consumes. Hand-authored labels
  in the sidecar are never overwritten.
- Every tag gets a playable GIF preview at the authored timing, and the
  export is re-graded (motion_report + pinned-palette check) ADVISORY -
  a human edited this on purpose, so findings report, never refuse.
```

## aseprite_master

```text
Sheet PNG -> tagged .aseprite MASTER, the file a human edits.

The master is a playable animation - each cell a real frame, each
animation a named tag with its authored timing - so fixes happen with
onion-skin and scrub instead of nudging pixels in a flat strip. Written
beside the sheet. image_sprites builds one automatically; this tool is
for sheets that predate that or came from elsewhere.

cell: [width, height] of one frame. anims: [{name, frames, fps?, loop?}]
in sheet order; omitted, the whole sheet becomes one looping "default".
After editing in Aseprite, aseprite_export brings it back as sheet + an
EXACT SpriteFrames .tres.
```

## ask_human

```text
Ask ONE question of a NAMED recipient - and keep working.

`to` says WHO, and it is never silently changed:

  human       DEFAULT. The person who owns this project.
  director    the live director session. If there is not one, this FAILS
              with an error naming what to do instead - it does NOT quietly
              send your director question to a human who is not there.
              MEASURED: it used to, and two items sat waiting on an answer
              nobody knew existed.
  seat:<name> another seat. Lands on the blackboard AND steers that seat's
              running agent if it has one.
  decision    a FORMAL decision for the register, with its acceptance test.
              Use decision_add directly when you can write the acceptance
              test yourself; this is the "somebody has to settle this"
              shape.


The director's ping. Use it for the calls that are genuinely not yours: which
of two directions to take, whether a scope cut is acceptable, whether a thing
you just finished is what they meant. It returns immediately and DOES NOT
BLOCK - do not poll for the answer, and do not sit idle waiting for one. Say
in your result note that you asked and what you assumed in the meantime.

NOT A WORK ITEM, DELIBERATELY. A question that becomes a queued row is a row
somebody has to dispatch in order to read, which is how "ask the human" turns
into "spawn an agent to ask the human" - paid, laned, and still in front of
nobody. This lands on the event bus instead, so it reaches the console card,
the drawer and any notification channel the project has switched on.

WHERE THE ANSWER COMES BACK depends on whether you are still running when it
arrives, and you do not have to do anything either way:

  * still running -> it arrives as a steer, the same channel a mid-run
    correction uses; you read it when your current step ends;
  * already finished -> it is filed as a handoff `decision` note (so the next
    session reads it from one file) and attached to the question itself.

Unanswered questions are reminded about ONCE, past notify.question_stale_h.
So: ask one thing, make it decidable, and name the options - "A or B?" gets an
answer, "any thoughts?" does not. `refs` are ids or paths the human should
look at ("item 41", "bible#12", "game/scenes/hub.tscn"); cite, do not paste.
```

## asset_verify

```text
PRESENCE IS NOT CORRECTNESS: is every asset intact, WIRED, and CURRENT?

Three questions, because a delivered asset can fail at three different
points and only the first one was ever asked here:

  intact       'modified' means content changed with NO lock held - an
               unlocked write or an outside edit. Locked files are expected
               to differ and are not drift.
  integration  which shipped assets NOTHING in the project names
               ('unreferenced'), and which res:// references name a file
               that is not there ('dangling'). Read as a pair they are the
               filename-contract mismatch: the art seat delivered
               projectile.png and the gameplay code still loads
               bolt_sheet.png. 'delivered_but_unwired' is the strong half -
               orphans this project's own artifact ledger says a seat
               produced, named with the item that paid for them.
               CANDIDATES, not verdicts: 'dynamic_load_sites' counts the
               places a path is built at run time, which no static scan
               follows.
  freshness    whether the ENGINE is serving the bytes that are on disk. A
               PNG written straight into the project resolves, measures and
               references perfectly while Godot keeps drawing the old
               import-cache product. 'stale' names those; the repair is
               godot_check_project, then ask again.

Costs nothing and spawns no engine. Run it before builds, after any
multi-agent session, and as part of any QA gate on a delivered asset - a
gate that only proved the file exists has proved the one thing that was
never in doubt.
```

## bgate_doctor

```text
Can this machine actually do the work? One call, every dependency.

Run this FIRST, and any time a tool fails with "not found" - instead of
calling blender_status, godot_status, image_status, playtest_check and
playtest_devices one after another to assemble the same picture.

Nothing here opens the microphone, renders a frame, launches an engine or
downloads a model: playtest_check does open the mic (deliberately - a muted
mic is invisible any other way), so it stays the pre-SESSION check, not the
is-my-toolchain-here check. Results are cached a few seconds, so polling
this is cheap; pass refresh=True right after installing something.

Returns {blender, godot, ffmpeg, ffprobe, whisper, art_key, python},
each {available: bool, path, version, min_required, reason}. `art_key` is
green when ANY registered art provider has a key (it used to probe
OPENAI_API_KEY alone and call a working Krea-only setup broken).
`reason` is
filled in when available is False (missing, too old, or the probe hung) and
says what to install or which BGATE_* env var points at it. Never raises.
```

## bible_ref_attach

```text
Anchor a pinned reference IMAGE to a bible section - the art that says
what the words mean.

The bible is prose. A pillar reading "grimy corporate satire", a canon
entity, a locked art-direction constraint - each is settled by pictures that
until now lived only in the pin list, unconnected to the text. A seat read
the section, got the words, and guessed the look; that guess is where style
drift starts, and afterwards nobody can point at where it began.

ref is a PIN NAME from ref_list (or a project-relative path); pin the image
first with ref_pin. The NAME is what gets stored, never the resolved path,
so re-pinning better art under the same name upgrades every section that
points at it instead of stranding them on the old revision.
kind: character | style | ui | concept.
```

## bible_ref_list

```text
The reference art anchored to the bible. READ BEFORE WRITING OR
ILLUSTRATING CANON.

No section_id: every section that has anchors, so the director can see at a
glance which pillars are still described in words alone. With one: that
section's anchors plus `resolved` - the layered set to condition a
generation on (this section's anchors first, then the global pins), the same
shape the task-level refs hand back.

Every entry carries resolved_path and exists. A false `exists` is a pin
whose file went missing underneath it, and generating against one of those
produces an unconditioned image that looks like a result.

suggest=True adds `suggestions`: sections whose PROSE names a pin (people
typed "(pinned: concept-battle / concept-battle-dark)" into titles because
there was nowhere structured to put it). It is a proposal only - attach the
ones that are right with bible_ref_attach, and leave the titles alone.
```

## blender_animate

```text
Put gameplay clips on a rigged humanoid, export the .glb, and SHOW them.

THE ANIMATION LAYER THE 3D PATH WAS MISSING. blender_rig binds, blender_flex
proves the bind bends, godot_retarget_check proves an engine can drive it -
and nothing could put a clip on it. Every agent asked for animations wrote
its own bpy pose script, and the one that shipped assumed the rig faced +Y
with its left on -X, aimed bones at absolute directions and interpolated
nine sparse keys. MEASURED: every clip strode backwards (the rig sat 180
degrees from the assumed frame), the torso never bent past 28 degrees
(absolute aims do not accumulate down a chain), the arms hung like a
mannequin's - and every automated gate passed it, because a moonwalk still
plants its feet and still keys smoothly.

WHAT THIS DOES INSTEAD, and each line is a defect it closes:
  * forward, left, leg length and hip height are MEASURED off the bones it
    is given, so a rig whose Left bones sit on +X lands its clips correctly
  * poses are ANATOMICAL - lean, reach, elbow, head_yaw - carried through
    the parent's pose; nobody needs a bone's roll and no clip breaks when
    the roll changes
  * feet are solved by two-bone IK against ankle targets with the knee
    pushed forward, and the hip height is DERIVED from what the legs can
    reach at full stride
  * the spine bends cumulatively, arms counter-swing their own leg, the
    pelvis yaws and the torso counters it, the head stays on the horizon,
    feet roll heel-to-toe
  * every distance is a fraction of THIS rig's leg length, so the same
    parameters walk a 1.2 m child and a 2.2 m brute at their own stride

ORIENTATION. The pipeline's forward is +Y in Blender, which the exporter
lands on -Z - the axis Godot moves and looks along. A rig whose skin and
skeleton agree with each other can still both face -Y (one that never went
through blender_rig), and the game then plays every clip walking away from
the direction the body travels. orient=True (default) turns bones and skin
180 degrees in the DATA before authoring and reports `oriented`.

THE FACING GATE. The single failure that ate four work items was a skin
facing -Y bound to a skeleton whose feet pointed +Y. This refuses when the
skin's toes and the skeleton's foot bones disagree (facing='check', the
default), re-aims the foot bones to the skin with facing='repair' (weights
are by name and stay put), or trusts the bones with facing='skeleton'.
`refused` True in the result is this gate; `error` says which fix to make.

STRAYS. An unbound mesh in the scene ships as a statue inside the
character. The usual one is not in the file at all: the glTF importer
builds a 42-vertex "Icosphere" bone shape at the origin for every rigged
.glb (a character measured 2.71 m tall through it). Dropped by default,
always listed under `strays`.

THE PROOF. `proof_frames` frames per clip from the side and the front
three-quarter, composited into one sheet per clip and returned as IMAGES.
Look at them: a number cannot say whether a walk reads as a walk. `support`
is the foot-contact gate off the EXPORTED file (forward kinematics, so what
the engine will play), judged against what each clip was meant to be - a
run with no flight frame fails as "a fast walk".

CLIPS. A LIBRARY clip is {"clip": "Walk_Loop", "name": "walk"} (see
animation_library) and is retargeted, animator-keyed motion; prefer it
where the pack has the motion, and mix freely with the procedural kinds
below in one call. kind: idle | walk | run | sneak (cycles; "overrides" tunes stride,
lift, lean, arm, elbow, pelvis_yaw, cycle_s - fractions of leg length where
they are distances) | crouch_idle | pickup | look_around | wave | hit | jump
(presets) | keyed. A keyed clip is {"keys": [{"t": seconds, "lean": deg,
"hips_up": metres, "reach_r": deg, "elbow_l": deg, "foot_l_forward": metres,
...}], "loop": bool, "ease": "inout"|"linear"} - every field is in character
terms (humanpose.KEY_FIELDS), never a bone rotation. A looping keyed clip
that ends elsewhere gets its first key appended so the loop closes.

loop_suffix=True names looping actions "<name>-loop", which Godot's importer
reads as a loop hint. Off by default: it changes the animation names a
scene references.
```

## blender_combine

```text
Assemble separately-modelled LAYERS into one rigged .glb, and test it.

The end of the layered 3D path: model body, clothing, hard accessories and
any logo as their own files, then join them here. Built in ONE pass instead,
a figure comes back with the parts that lost the attention budget deformed - on a real baseball player, the hands, the cap, and a scrambled team logo.

`parts` is the layer list, each a path or a dict:
  {"path": "out/uniform.glb",   # .glb / .gltf / .blend
   "name": "uniform",           # how it is reported and referenced
   "at": [0,0,0], "rotate": [0,0,0], "scale": 1.0,
   "bind": "deform",            # deform | bone:<Name> | none
   "decal_on": "cap"}           # conform to that layer's surface

A LOGO OR ANY TEXT GOES IN AS ITS OWN LAYER WITH decal_on. Flush against the
surface it z-fights and tears in-engine; shrinkwrap plus an offset fixes it.
Hard geometry rides a bone (a cap does not bend), soft geometry deforms.
`rig` names the layer holding the armature - without it nothing binds, which
is right for a prop and a shipped statue for a character.

Returns per-layer objects/tris/binding, plus `checks`: `unbound` and
`unweighted_verts` name the layer that detaches or tears the first time it
animates, so you re-run that layer instead of the whole character.

The assembled file is REGISTERED as a candidate artifact (`artifact_id`),
which is what puts it under the same QA gate every 2D asset passes through.
Write out_path inside the project - an artifact cannot be recorded for a
file outside it, and an unregistered asset is one no reviewer ever sees.
```

## blender_flex

```text
Bend a rigged character and report what bending it did to the body.

THE SECOND HALF OF THE RIG PROOF. `blender_rig` answers "were weights
written" with the unweighted count, and that is the only thing it can
answer. It says NOTHING about whether the elbow survives being bent, and a
rig with zero unweighted vertices routinely collapses a joint to a straw,
loses a quarter of its volume in one bend, or drives the forearm through the
ribs. Every number stays green while the character animates like a bag of
spanners. Run this before you deliver one.

Poses each joint a walk cycle moves, ONE AT A TIME so a failure is
diagnosable, and per pose measures:

  volume_ratio      posed volume over rest volume. A good bind costs 2-6%.
  worst_pinch       the joint that lost the most cross-section. 1.0 is
                    rigid, 0.6 is a visible waist, under 0.4 is a straw.
  new_self_pairs    faces that intersect in this pose and did not at rest.
                    The increase, not the count - a generated mesh arrives
                    with overlapping shells and the absolute number is
                    meaningless.
  render            a PNG of the pose. LOOK AT IT. The whole lesson of this
                    pipeline is that green gates are not evidence.

`verdict.passed` False is a refusal, not a warning: those weights are not
animatable as they stand. The usual fixes, in order - raise `budget` on the
rig so the joint has enough loops to bend, check `audit.shells` for a
fragmented mesh heat could not cross, and re-run the rig with
symmetrize='force' when only one side failed.
```

## blender_generate

```text
Turn ONE generated image into a draft mesh. The other way to get geometry.

The primitive path (blender_run + the kit) is for props, vehicles, terrain
and block-out - things made of boxes and cylinders. It tops out at a
proportioned blockout with no face and no fingers, so a hero character
seen close up comes from here instead: generate the plate with
image_generate, then hand it over.

WHAT COMES BACK IS A DRAFT, NOT AN ASSET. Expect dense, unpredictable
topology, no armature, no unit convention, and possibly baked lighting in
the texture. It has to be scaled to 1.8 m, faced +Y, cleaned, unwrapped
and weighted to a skeleton before blender_combine will make anything of
it - bg_human's rig is the one to weight it to. `draft` is True in the
result and `next_steps` says so; there is no path straight to
godot_deliver_asset and that is deliberate.

Nothing runs until you configure a backend (see .env.example) - this
machine ships no model and downloads none. blender_status reports what is
reachable. A local backend costs nothing per generation; a hosted one is
priced before it submits, and `dry_run=True` returns that quote plus the
licence verdict without spending anything.

LICENCE IS PART OF THE RESULT. A local server is only a transport, so the
model must be declared (BGATE_LOCAL_MODEL) - undeclared reads as unknown,
never as permission. Some grants exclude whole territories and some
forbid commercial use outright, which is a shipping problem rather than a
technical one, so read `licence` before building on the mesh.

parts=True ASKS FOR A BODY IN PIECES, and for a character it is the better
request. A monolithic generation gives one blob - measured on a real user's
asset, 940 disconnected shells with no relationship to anatomy - and bone
heat then has to guess where the arm stops and the torso starts, which is
how fingers end up weighted to a hip. A part-aware graph returns a head, a
torso, arms and legs as SEPARATE meshes, and every step after it gets
easier: `out_path` is read as a DIRECTORY, the result carries `parts` and a
`combine` list ready for blender_combine, and a run that comes back with
one mesh is flagged rather than reported as a success.

It needs its own workflow (BGATE_COMFY_PARTS_WORKFLOW) whose saver writes
one file per part. Without it this says so instead of quietly falling back
to the monolith, because a silent fallback here is indistinguishable from
the feature working.
```

## blender_humanoid_template

```text
The shipped humanoid skeleton and the pose plate to generate against.

START A CHARACTER HERE. Every generated mesh used to invent its own
proportions, so the skeleton had to be bent to fit each one and no two
characters could share an animation. Conditioning the PLATE on this
reference inverts that - the art conforms to the skeleton, and a clip
authored for one character plays on the next.

Measured on one character, bones further than 6 cm from any mesh vertex:
  template scaled by height only ............ 16 of 24
  landmark fitting alone ..................... 5 of 23
  plate conditioned on this reference alone .. 8 of 23
  BOTH ....................................... 0 of 23, and 0 unweighted

Returns the reference image to pass as `ref_images` to image_generate, the
prompt clause that holds the stance, and the 23 Godot-profile bone names
every humanoid from this pipeline carries - so BoneMap retargeting works
and animations move between characters.

The five-step path:
  1. image_generate(prompt + pose_clause, ref_images=[pose_front])
  2. key it - an opaque plate becomes geometry, measured 2.8x slower and
     21% non-manifold against 16% keyed
  3. blender_generate(plate, out)          draft mesh
  4. blender_rig(mesh, out)                adopt, fit, bind, PROVE it
  5. godot_deliver_asset(project, rigged)  .tscn, verified in-engine
```

## blender_layer_rerun

```text
Rebuild ONE layer of an assembled asset and re-assemble it. Not the
character - the layer.

"Re-run that one layer, not the whole character" is the promise the layered
3D path is built on, and until this tool existed there was no way to keep
it: the recipe lived in the manifest and nothing read it back, so a bad cap
meant re-modelling, re-texturing and re-assembling everything beside it.
blender_combine names the layer that failed (`checks`: unbound,
unweighted_verts, and the per-layer tri counts) - this is what you do with
that name.

`asset` is the ASSEMBLED .glb (the manifest sits beside it). `layer` is the
layer name as blender_combine reported it. Then ONE of:
  script   bpy source for that layer, run and exported over the layer's own
           file. The modelling kit is injected (kit=True) exactly as in
           blender_run, and the script is recorded beside the layer so the
           next re-run has it.
  source   a .glb/.gltf/.blend you already built - used in place, nothing
           is run.
  neither  the layer's RECORDED script is re-run. After blender_sweep the
           layer files are gone and this is the recovery path: each swept
           layer's manifest entry carries the script that built it. If the
           file is still on disk and no script is given, it is reused as-is.

Everything else - placement, rotation, scale, binding, decal_on, which layer
holds the rig, the root name - comes back off the manifest untouched. A
layer put back at the origin unrotated is a different asset, which is why
those arguments are recorded rather than re-typed.

Refuses BEFORE spending time in Blender when another layer's source is
missing, and names those layers: combine would otherwise assemble happily
around the hole and hand back a character with no arms. Re-run those first.

The re-assembled file is registered under the SAME logical name, so it is
revision N+1 of the asset a reviewer already saw, not a new one. Returns the
combine result plus `changed` - the layer's tri and object counts before and
after - so "did that fix it" is a number rather than an impression.
```

## blender_rig

```text
ANATOMY IS A VERDICT, NOT A NOTE: the report carries `anatomy` - whether the
trunk (Hips/Spine/Chest/Neck/Head) was hung from the crotch, shoulder line
and crown MEASURED off this mesh, and how far the height-only template
would have put them (template_worst_shift, in body heights, against an 0.08
bound). A trunk that could not be measured FAILS the rig (ok false, error
"TRUNK ASSUMED"): that is how a character shipped with thigh bones 37 cm
above its crotch while every gate said green. Fix the plate or pass heads=.

Take a GENERATED mesh to a bound, weighted character an engine can move.

Every image-to-3D backend returns `rigged: false` - geometry and nothing
else. This is the missing step between that and a character: adopt the mesh
(weld, decimate, scale, orient, ground), fit a skeleton to its own measured
height, bind it, and PROVE the bind took.

THE PROOF IS `unweighted`, AND NOTHING CHEAPER WORKS. Blender's parent_set
returns cleanly, creates all 22 vertex groups, and can leave every one of
them empty. The modifier attaches. Godot loads it and shows a Skeleton3D.
The character animates not at all. MEASURED on a real generation: 64,878 of
64,878 vertices carrying no weight with every other check green.

Adopt and bind happen in ONE Blender session on purpose. Round-tripping
through a file between them is what produced that failure: glTF re-import
carries a root transform, the skeleton lands in a different space from the
mesh, and heat finds no vertices near any bone. Same mesh in one session:
3 of 19,556.

Bone heat is tried first because it deforms properly; ARMATURE_ENVELOPE is
the fallback and is rigid, so elbows and shoulders pinch. `bound_with` says
which one shipped. **`rigged` False means the asset is not animatable** - it is not a warning to pass along, it is a refusal.

kind    "humanoid" reads a front from foot reach; "none" refuses to guess.
        A subject with no feet (a prop, a bust) wants "none", and then
        orientation is NEVER ESTABLISHED - check the turnaround yourself.
budget  0 leaves the density alone. A local backend with no face_count knob
        hands back ~280k faces, and post-decimation here is the only lever
        those users have. 8k shattered a character; 45-60k was clean.

symmetrize  "auto" (default) mirrors the skin weights across the body's own
        centre plane, but ONLY when the audit says the two sides are within
        2% of the character's height of each other. Heat fails differently
        on each side - one clean elbow and one bound to the ribs is the
        normal outcome - and averaging the pair fixes it without picking a
        winner. "off" skips it. "force" runs it on an asymmetric body, which
        is right for a cosmetic asymmetry (one pauldron, a cloak) and wrong
        for anything else.

THE REPORT NOW CARRIES `audit` BEFORE THE BIND, and it is the part worth
reading first. `audit.shells` is the fragmentation count - a real user's
character arrived as 940 separate shells, which passes every
well-formedness gate and guarantees a bad bind, because heat will not cross
the gaps and loose islands weight to whichever bone is nearest.
`audit.symmetry.mean` is how far the body is from its own mirror image.

AND `rigged: true` IS STILL NOT "ANIMATABLE". Run blender_flex on the
output: it bends the thing and measures what bending it did.

`coverage` (kind="humanoid" only) is a fast pre-check for the 15 bone
names godot_retarget_check calls essential - Hips, the spine/head chain,
both arms, both legs, under the EXACT name a BoneMap-free retarget
matches by. It cannot see hierarchy or binding, only naming, so a pass
here is not a substitute for retarget_check against the real engine - it just means a naming problem shows up now instead of after the Godot
round-trip.
```

## blender_run

```text
Run a bpy script in headless Blender and get the scene back as facts.

`bpy` is already imported. Returns per-object tri/vert counts (evaluated, so
modifiers count), UV warnings, materials, your print() output, and - with
render=True - a PNG of the active camera view (archived to the project's
preview gallery; give a `label` so humans can tell renders apart).

THE MODELLING KIT IS ALREADY THERE (kit=True, the default). Do not write your
own material/UV/hygiene helpers - an agent burned 33 KB and most of an hour
doing exactly that on the first real character run. Available:
  bg_help()                      PRINTS A COMPLETE WORKED LAYER SCRIPT - a
                                 humanoid built from one head-height, a
                                 named rig with roll, the checks, bg_finish
                                 last. Read it before writing your first one.
  bg_wipe()                      empty the scene (no default cube)
  bg_box/bg_cyl/bg_ball/bg_plane named primitives
  bg_mirror/bg_smooth/bg_taper   symmetry, subsurf, limb taper
  bg_join(objs, name)            one layer should leave as ONE mesh
  bg_clean(obj)                  doubles/loose/degenerate/normals - THIS is
                                 what makes automatic weighting work later
  bg_unwrap(obj)                 smart-project UVs (no UVs = no texture)
  bg_mat(obj, name, rgb)         a BLOCKING-IN colour, not a shipped surface
  bg_bone_chain(name, bones)     an armature with NAMED bones. Entries are
                                 (name, head, tail, parent=None, roll_deg=0);
                                 order does not matter, parents are wired in
                                 a second pass, and ROLL IS IN DEGREES - set
                                 it on limbs or a humanoid retarget gives you
                                 the twisted-forearm look.
  bg_finish(obj, colour=...)     clean + apply + unwrap + material, in order
  bg_stats(obj)                  verts/faces/loose/nonmanifold/ngons/flipped
                                 PLUS world-space dims/centre/min/max
  bg_bounds(obj)                 world-space min/max/dims/centre, in metres
  bg_flipped(obj)                how many faces point INWARD (count, measured
                                 on a throwaway copy - the mesh is untouched)
  bg_overlap(a, b)               do two layers' world bounds intersect, and
                                 by how much. Layers are built in isolated
                                 scenes, so "is the cap sunk into the head"
                                 is a question NOTHING else in the pipeline
                                 can ask until they are already combined.

bg_bone_chain RAISES - deliberately, and it is the only thing in the kit that
does. Everything else swallows its problems because a helper that raises
takes the whole run down; a rig cannot afford that trade, because a wrong rig
looks built and comes apart in the engine several steps later. It refuses: a
parent no bone in the list defines (which used to produce silent parentless
roots), a duplicate bone name, head == tail (Blender DELETES zero-length
bones on leaving edit mode and says nothing, so the bone simply is not in the
armature you get back), and a name Blender had to rename or truncate (bind=
'bone:Head' then matches nothing in blender_combine). Every message names the
bone. Read the message and fix the chain - do not wrap it in a try.

START A BODY FROM THE BASE MESH LIBRARY, NOT FROM PRIMITIVES. Same kit, same
namespace, no import:
  bg_human(height=1.8, heads=7.5, build, limbs, shoulders, detail,
           pose="t"|"a", convention="godot"|"blender", rig=True)
  bg_quadruped(...) / bg_prop_frame(...)
                                 each returns {"obj","rig","marks","props",
                                 "convention","pose"} - a correctly
                                 proportioned, closed, unwrapped,
                                 weight-ready body with a NAMED skeleton.
  bg_proportions(...)            45 measurements out of one number
  bg_mark(base, "head_top")      one landmark: position, radius, girth.
                                 RAISES on a name that is not there.
  bg_fit(obj, mark, mode="at"|"on"|"around"|"in", clearance, scale)
                                 places AND resizes a layer onto a landmark
  bg_shell / bg_human_chain / bg_human_skeleton / bg_roll
  bg_bone(base, "hand.R")        the real bone name (RAISES on an unknown
                                 role); BG_BONE_NAMES carries Godot's
                                 SkeletonProfileHumanoid spelling by default
  bg_weight(obj, rig)            binds AND counts what stayed unweighted
  bg_base_report / bg_base_assert  the base's own self-check (assert RAISES)
  bg_base_help()                 prints BG_BASE_EXAMPLE, the worked script
  BG_UNIT="metre", BG_HUMAN_HEIGHT=1.8, BG_GROUND=0.0, BG_FORWARD=(0,1,0),
  BG_LEFT=(-1,0,0), BG_SIDES - the base FACES +Y, which the glTF exporter
                                 turns into -Z, which is what Godot calls
                                 forward. Author faces, visors and emblems
                                 on the +Y side; the figure's own left is -X.
  bg_unit_check / bg_unit_assert (RAISES) / bg_rescale

FIT LAYERS ONTO LANDMARKS INSTEAD OF GUESSING COORDINATES. MEASURED: a cap
placed with bg_fit(cap, bg_mark(base, "head_top"), "on") rests on the crown
at 10% overlap; the same cap at a hand-typed 1.7 m is 89% INSIDE the skull
and passed every check the old pipeline had. The honest limit - the base has
no face and no fingers. It is a correctly-proportioned blockout to build the
character ONTO, not a finished character.

Pass kit=False only for a script that must run against bare bpy.

A broken script is a normal result with ok=False plus the traceback, so read
the result and iterate rather than assuming it worked. engine:
BLENDER_WORKBENCH (fast preview) | BLENDER_EEVEE_NEXT | CYCLES.
```

## blender_silhouette

```text
The character's projected 2D outline across flex's own pose sweep.

EXPERIMENTAL - no production rig-QA tool anywhere this project's
research found automates a pose-sweep silhouette check; studios render
the sweep and a human watches it. This is a real attempt at that, not
an adopted technique.

A DIFFERENT QUESTION FROM blender_flex. Volume and pinch are 3D
measures against the mesh itself and cannot see a failure that only
shows up from a CAMERA's point of view - a limb that folds directly
behind the torso and vanishes from the silhouette while its 3D volume
stays intact, or a shoulder that balloons on screen without losing any
measured volume. This projects the SAME pose sweep through the SAME
fixed, rest-fitted camera flex() uses (never refit per pose) and
measures the projected convex-hull area.

'Preserved' means SANITY BOUNDS, not 'unchanged' - a pose is EXPECTED
to change how a character reads on screen. `verdict.passed` False means
the silhouette nearly vanished (min_ratio) or ballooned far past what a
single joint's rotation should produce (max_ratio), not that anything
changed at all.

It is ALSO False on a sweep that proves nothing: every pose skipped for
want of the bones it rotates, or every pose projecting the identical
outline as rest. The second is the important one - an unbound mesh does
exactly that, and bounds that only fire far from 1.0 would otherwise call
a ratio of exactly 1.0 across the whole sweep a perfect result.
```

## blender_sprites

```text
Render a Blender-built character as a transparent 2D sprite set.

THE 2D art path: build the model once in base_script (bpy; lights included - camera optional, an auto-framed ORTHO one is added if missing), then each
pose in poses=[{"name","script"}] tweaks the scene and renders one frame.
Output: per-pose PNGs + <name>_sheet.png + <name>_frames.tres (a Godot
SpriteFrames with one animation per pose) ready for an AnimatedSprite2D via
godot_import_asset into res_dir. Rendered sprites cannot drift between
poses the way hand-drawn ones do - same rig, camera, light every frame.

A pose script that errors fails only that pose; check `failed` in the result.
The sheet is archived to the preview gallery.
```

## blender_sweep

```text
Delete a finished asset's intermediate layer files, keeping the record.

A character run leaves a per-layer .glb each, a .blend rig, the assembled
asset and its renders - fourteen files for one request. This removes the
layer sources listed in that asset's manifest and NOTHING ELSE, so a
neighbouring asset's layers survive.

Kept: the assembled file, its manifest, the renders. What was removed is
written back into the manifest, so the run's history outlives its files and
a single layer can still be identified and rebuilt later.

Defaults to dry_run=True. Look at the list, then call again with
dry_run=False.
```

## blender_template_deviation

```text
How far a rigged character's joints sit from the shipped humanoid template.

A FOURTH RIG PROOF. `blender_rig`, `blender_flex`, and `blender_weights`
all ask questions about ONE character in isolation - is it bound, does it
survive bending, is the paint contiguous. None of them can tell you the
fit itself landed a bone somewhere anatomically wrong, because a bone
can be fully weighted, pinch-free, and bleed-free while still sitting in
the wrong place on the body if height/limb fitting mis-solved.

Compares bone LENGTHS against HUMANOID_SKELETON (or a supplied
`reference`), matched by name and each expressed as a fraction of its own
file's body height, so two characters of different heights aren't
penalised for that alone. Lengths rather than joint positions because the
two skeletons are never posed alike - this pipeline rigs in an A-pose and
the template is a T-pose, and a positional check reports that difference
as a fault on every correctly-rigged character. Bone length does not move
when a joint rotates. Parent links are compared too, so a rig that kept
the 23 names but rewired the chain is caught.

NOT a weight comparison - the reference skeleton and a generated character
never share mesh topology, so there is nothing to diff vertex-for-vertex.

`max_deviation` (0.08 body-heights) is a GROSS-ERROR line - a limb
collapsed to nothing or stretched across the body - not a proportional-
fidelity one. Fitting is meant to adapt the template to each body.

`verdict.passed` False names which bones are mis-proportioned or
misparented. It is also False when nothing could be compared: a candidate
whose bones are named on another scheme entirely reports `checked: 0` and
refuses, rather than passing an empty intersection as agreement.
```

## blender_texture

```text
Put GENERATED maps on a 3D layer's material and re-export it.

The surface half of the layered path. Measured on the first real character
run: the assembled asset carried 21 materials and ZERO images - every
surface a flat colour an agent typed by hand, because nothing connected the
image adapter to the 3D layers. Generate the maps with image_generate
(task_kind="texture", conditioned on the pinned refs via use_pinned), then
apply them here, per layer, before blender_combine.

`image` is the albedo / base colour and is what the one-image call has
always meant. The rest are optional and each drives its own BSDF input.
WITHOUT THEM EVERY SURFACE IS THE SAME PLASTIC - the modelling kit types
rough=0.6, metal=0.0, so cloth, leather, skin and steel all ship as one
dielectric and colour is the only thing that varies across an asset:
  roughness   how glossy, per texel        metallic  0 dielectric, 1 metal
  normal      tangent-space normals        emission  what glows
Those four are DATA and are loaded Non-Color; `image` and `emission` feed
colour sockets and stay sRGB. Pass image="" to apply maps without changing
the base colour. normal_strength scales the Normal Map node.

ALPHA - auto | opaque | clip | blend. MEASURED: a decal needs alpha="clip"
to export `alphaMode: MASK`. Without it the logo layer ships as a solid
rectangle of key colour glued over the cap, which is worse than the
z-fighting the decal layer exists to prevent. `auto` inspects the base image
and picks clip only when it ACTUALLY carries transparent pixels - an opaque
PNG with an RGBA header is not a cut-out - so say clip explicitly when you
know it is one. alpha_cutoff is the MASK threshold. decal=True is shorthand
for a conformed graphic and implies backface culling; backface_cull
overrides it either way.

`material` names ONE slot. IT IS EFFECTIVELY REQUIRED on a model carrying
more than one authored material: `all_slots=True` is the explicit opt-in
that says you meant to paint every slot, because that used to be the DEFAULT
and it put one image over skin, eyes and mouth and called the layer
textured. A named material matching no slot is a failure, not a cheerful
ok=True with an empty list. Meshes with no UVs are unwrapped first - a map
on an unwrapped mesh is silently ignored, which looks exactly like the
generation having failed.

The re-exported layer is REGISTERED as a candidate artifact (`artifact_id`)
and carries the maps it was given, so the surface a reviewer is judging can
be traced to the images that produced it. Write out_path inside the
project; a file outside it cannot be recorded.
```

## blender_turnaround

```text
Render a model from four angles under a fixed rig - and JUDGE each frame.

THE FRAMES COME BACK IN THIS RESULT AS IMAGES, not as paths you are trusted
to go and open. Measured: four turnarounds of a correctly-coloured model
came back white because the lights were far too hot, and were reported as
finished without anybody opening them. The model was fine; the render was
not, and nothing could tell the difference. Look at what you were handed,
and read the verdicts - they are the half of the check you cannot argue with.

Camera and three-point lighting are scaled to the subject's own bounding
box, so a giant and a doll both frame correctly. Every frame returns a
`blown`/`mean` reading and a verdict; `ok` is False when any frame is
unreadable, and the verdict of the frame that failed is the `error`. A
failing frame is a lighting problem, not a modelling one - do not go back
and change the mesh because a render was white.

Each frame is archived to the preview gallery and REGISTERED as a candidate
artifact, so a turnaround can be handed to an independent reviewer by
`artifact_id` (see art_qa_verdict) and shows up in the dashboard beside the
2D work. Point out_dir INSIDE the project - frames written outside it cannot
be registered, and an unregistered render is one nobody reviews.
```

## blender_weights

```text
Per deform bone, does its weight paint cover one patch of the mesh or two.

A THIRD RIG PROOF, ALONGSIDE `blender_rig` AND `blender_flex`. Neither of
those catches this: `rig()`'s `unweighted` count only sees vertices with
NO weight, and `flex()` only sees a joint after it bends. Bleed is
neither - a hand painted mostly to Hand but partly to Spine, because a
brush stroke crossed empty space in the viewport rather than the mesh
surface, has full weight coverage and may not even move wrong at any of
flex's six test poses if the bleed region is small. It still reads as a
seam-tearing glitch the moment the spine and the hand pose differently.

Reports each deform bone's weighted vertices as connected components on
the mesh surface, and flags a bone whose paint makes MORE components than
the number of separate mesh pieces it touches - a split inside one
connected piece of surface, which only a stray stroke explains. Spanning
several pieces is not itself a fault: this pipeline assembles bodies from
joined primitives, so a hip bone legitimately covers three of them.

`threshold` is the minimum weight at which a vertex counts as belonging to
a bone (0.02). `min_bleed_vertices` (3) is a noise floor - a single stray
vertex is a cleanup nit, not the seam-tearing failure this exists to catch.

`verdict.passed` False names which bones split and how many vertices sit
off their own patch. It is also False when nothing could be measured - a
bind with no weights above `threshold` reports `checked: 0` and refuses,
rather than passing an empty result as a clean one.
```

## board_digest

```text
WHAT HAPPENED WHILE YOU WERE AWAY - finished, failed, blocked, spent.

The morning report. Nothing else answers it: the autopilot keeps only its
LAST refusal, notices collapse past three into "11 items finished", and the
heartbeat reports stalled chains rather than "the board stopped at 23:02".
So after an overnight run the most common question had no surface at all.

The field to read first is ``blocked``. Queued work with nothing running is
either a dead dashboard or a floor refusal - most often a dirty tree, which
stops the WHOLE board rather than one item - and this names which. If the
board is holding whole SEATS, ``stage`` says which and why; that is the
production stage, and greenlight_status is the long answer.

``restart_cost`` is what killing the MCP server right now would orphan, and
``orphaned`` is what a previous one already did. READ THE FIRST ONE BEFORE
RESTARTING ANYTHING: a tool that has been silent for ten minutes is
usually a provider call that is still running, and the restart does not
stop the charge - it only throws away the result.

Spends nothing. Read it at the start of a session before deciding anything.
```

## brainstorm_close

```text
End the session's THINKING PARTNER process. Keeps everything it said.

THREE WORDS THAT ALL SOUND FINAL, kept apart on purpose:
  close     (this) stops the spawned CLI process. The conversation, the
            notes and the drawing are untouched, and the next message
            reopens it - resuming the same CLI session where it left off, or
            replaying the transcript if the CLI no longer has it. It is
            about the PROCESS. Nothing is decided and nothing is lost.
  archive   files the SESSION away as a record: no new turns, notes or
            deploys until reopened. Implies a close.
  deployed  a STATUS, not an ending: this session put work on the board.
            Usually still open, and usually still being talked in.

Available to a machine, unlike deploy and delete: stopping a process costs
nobody their work, and an agent that noticed a room has gone quiet should be
able to stop it paying for a listener. Idempotent.
```

## brainstorm_deploy

```text
File a confirmed plan onto the board. HUMAN-ONLY, AND THE ONLY ONE HERE.

WHY A MACHINE IS REFUSED. This whole room exists so a human reads a plan
before agents are dispatched against it. An agent that can deploy closes
that loop on itself: it writes the session, synthesizes a plan out of its
own sentences, files N items, and each of those spawns another agent - a
work generator with a review step nobody attended. The same reasoning
already refuses a machine the write lanes in seat_configure and the
human-only settings in bgate_core.store.settings, and the same fail-closed test is
used: BGATE_SEAT / BGATE_WORK_ITEM, not the actor string alone, because a
gate that reads one stamp is disabled by forgetting one line.

A human's own session - no seat, no work item - deploys normally. Nothing is
taken away by this rule: that session could already call queue_add, and
routing through here is what keeps `source=brainstorm` on the items so the
board can name the conversation they came from.

`plan` is what brainstorm_synthesize returned, as the human approved it - edits included, since their text is the point. Items are validated strictly
rather than repaired: quietly rewriting a confirmed plan files something
other than what was agreed. Set "chained": true ONLY when each item needs
what the one before it produced; that becomes a real dependency chain rather
than a priority preference two agents can start in the same tick.

`again=True` overrides the guard against filing the identical plan twice.
```

## brainstorm_feed

```text
What the session's partner PROCESS actually emitted - the terminal channel.

Not the conversation (brainstorm_open has that): this is the raw stream the
spawned CLI wrote - run boundaries, its own `init` event stating the tool
list it really built, its calls to the two-tool pad server, their results,
and its prose. Read forward from `cursor`; keep the one you are handed and
pass it back to get only what is new.

Worth reading when a human asks what the partner has been doing, or when you
want to check the room's promise rather than take it on trust: the `init`
step names every tool the process holds, and there should be exactly two.
```

## brainstorm_invite

```text
INVITE A SEAT INTO A BRAINSTORM. It arrives WITHOUT ITS TOOLS.

The room had two voices - the human and the owning seat's thinking partner - and the question a human actually has ("would weather in the hub be cheap or
a fortnight") is one only the seat that would BUILD it can answer. This is
how that seat gets asked.

WHAT AN INVITED SEAT IS. A CLI session spawned by the same read-only path as
the room's own partner: an empty built-in tool set, --strict-mcp-config so
it cannot inherit the server you are reading this on, and at most the
two-tool pad server. It is the seat's JUDGEMENT, not the seat's HANDS. It
cannot write a file, run a command, claim work or file anything, and nothing
it says becomes work on its own - a human still reads a synthesis and
presses Deploy. Compare queue_add, which puts a real agent with real tools
on the board; that is the other room and this is deliberately not it.

Refused, each saying which: a seat that is not a seat, a seat this project
has disabled, a seat already in the room (including the seat that OWNS it,
whose partner is the room's own voice), a room already at its limit, and a
runner that has not declared a read-only mode - that last one is refused
rather than started with dispatch flags, which is the whole guarantee.
```

## brainstorm_list

```text
The brainstorm file drawer - what has been thought about, and what it filed.

THE CHEAP ROOM. A brainstorm session is where an idea is still an idea:
conversation, a writing pad and a drawing pad, none of which queue anything.
The board is the expensive room. Read a session before proposing work in its
area - half the "new" ideas an agent files were already argued out and cut
in a room nobody looked in.

seat: director (what to BUILD) | narrative (what is TRUE).
status: open | deployed | archived. Archived sorts last whatever you pass.

Titles and counts only - never the pads, which come one session at a time
from brainstorm_open, because an index that ships every scratch document is
an index nobody can afford to poll.
```

## brainstorm_new

```text
Open a brainstorm session. Nothing about this reaches the board.

Use it when the human is thinking rather than asking - "what if the hub had
weather" is not a work order, and turning it into one costs a spawned
session per half-thought and leaves a board full of items nobody meant to
file. Everything said here stays here until a human deploys it.

seat picks what the room is FOR, and it is enforced at plan time rather than
trusted to the prompt:
  director   what to BUILD. May propose work for any seat.
  narrative  what is TRUE - canon, lore, the bible. May propose narrative
             work only.
```

## brainstorm_note

```text
Write the session's pads - the title, the writing pad, the drawing scene.

`notes` REPLACES the whole pad. It is one text area a person types into, not
a patch protocol, so brainstorm_open it and send the whole document back if
you are adding to somebody's writing - a partial write here deletes the rest
of their hour.

`drawing` is the pad's structured scene ({"elements": [...], "appState":
{...}}), which is the whole reason a text model can work on it: reuse the
element ids brainstorm_open showed you rather than inventing new ones, or
the arrows come back unbound. The flattened PNG is not settable here - only the browser renders one.

Omitted fields are left alone. Nothing here queues anything.
```

## brainstorm_open

```text
One session, whole: the conversation, the notes pad, the drawing, what it filed.

THE DRAWING COMES BACK AS WORDS. `drawing_text` is the pad's elements
rendered as lines - "rectangle#hub-1 'hub'", "arrow#a1 hub-1 -> shrine-1" - which is the content of the board and is readable without vision. The raw
`drawing` scene is there too, and the ids in it are what you reuse if you
write elements back with brainstorm_note. `drawing_png` is a preview path
the browser renders; it is never the source of truth.

`deploys` is what this session has already put on the board. Read it before
proposing more: a session that filed three items last week is not a blank
page.

`thinker` is who else is in the room: which runner and model this session's
partner runs on, whether a process is live right now, what the conversation
has cost, and the path to its raw transcript. Worth a glance before you pass
`reply=` to brainstorm_say - if a partner is already live, its answers and
yours are two voices in one conversation.
```

## brainstorm_reset

```text
START THE THREAD OVER in the same room. Stops the partner, drops the transcript.

THE FOURTH END-STATE, and the one people reach for most: the conversation
has gone circular or is arguing from a premise that stopped being true, and
what is wanted is a clean head - NOT a closed process that resumes the same
dead thread, and NOT a delete that takes an hour of notes and diagram with
it. brainstorm_close resumes where it left off; this one makes the next
message the first message.

The notes and the drawing SURVIVE by default: they are the human's own
document, not the conversation. `keep_pads=False` clears those too.

Deploys are never touched - work already on the board outlives the thread
that thought of it.
```

## brainstorm_say

```text
Say something in a brainstorm session. NOTHING ELSE HAPPENS.

No work item, no dispatched agent, no approval gate: a turn here is two rows
in a table. queue_add and brainstorm_deploy are the two calls that file
work, and neither is reachable from this one.

`reply` IS THE POINT OF THIS DOOR, and it is worth more now than it was.
YOU are a model and you are already holding the session, so pass your own
answer and it is stored as the assistant turn - no second session, no CLI,
no cost, and no key was ever needed for it. Leave it empty and the dashboard's
partner answers instead, which spawns a real (tool-less, read-only) CLI
session and bills a turn against the subscription. Between an answer you
already have and a process you have to start, the answer you already have is
strictly better.

So: a MACHINE must pass `reply=`. A caller stamped as an agent that leaves it
empty is refused rather than allowed to spawn a nested thinking session - an agent already inside a CLI session paying to start another one to think
for it is a loop with a bill attached, and it was one flag away from being
the default behaviour of this tool.

Either way the human's sentence is stored BEFORE anything is asked, so a
dead partner costs a reply and never what was typed.

`to` ADDRESSES ONE SEAT that has been invited into the room
(brainstorm_invite). Leave it empty and everyone present answers - one CLI
turn each, in invite order, the room's own partner first - which is what you
want for "what does everybody think" and is four times the cost for "what
does the art seat think". `to` is ignored when you pass `reply=`, because
then YOU are the one answering and there is nobody to address.

Push back in the reply when something does not hold together, and say which
part. Do not write a task list here - proposing the work is a separate step
(brainstorm_synthesize) that a human takes when they are ready.
```

## brainstorm_synthesize

```text
THE PREVIEW: what work this session adds up to. WRITES NOTHING.

Not a work item, not the session's status, not even the plan - a stored plan
would be a fourth thing that can go stale. Safe to press, and safe to press
twice.

What comes back under `plan` is exactly the shape brainstorm_deploy takes:
{"summary", "chained", "questions", "items": [{"seat", "title", "brief"}]}.
`plan.notes` lists every repair made to the model's answer (a seat it named
that a narrative session may not file, an item with no brief) - those are
corrections a human should see, not silent fixes.

HAND THE RESULT TO THE HUMAN. You may not deploy it; see brainstorm_deploy
for why. If there is no thinking partner on this machine the call fails and
you can write the plan yourself in that same shape - the review step is what
matters, not which model drafted it.
```

## causal_chains

```text
Why did that action fail? The gate ladder, reconstructed from telemetry.

A log line says `whiffed reason=facing`. A causal chain says the attack was
thrown, cleared its cooldown, reached contact, PASSED the range gate at
dist=104 vs reach=115, and only then failed on facing - a completely
different bug from failing on range, which the raw event cannot distinguish.

Works on telemetry the game ALREADY emits: no engine, no new store, no
change to the game. The inference is sound because resolution gates run in
a fixed order, so the gate that failed implies every earlier one passed.

`spec` names one of THIS PROJECT's chain specs (see `causal_specs`). The
harness ships none - event kinds are your game's vocabulary, not Builders
Gate's. Draft one from a telemetry file with `causal_infer_spec`.

Filter with actor, outcome ("landed", "failed", "blocked", "refused",
"aborted", "dropped", "unresolved"), failed_gate, or move.
```

## causal_infer_spec

```text
Draft a chain spec by reading what your game actually emits.

Bootstraps `causal_chains` for a game the harness has never seen. Clusters
event kinds into pipelines by shared prefix, guesses the opener, finds the
actor field, and collects the `reason` values it observes.

It CANNOT infer the one thing that matters most: the ORDER of the gates.
Order is a property of your resolution code, not of its telemetry, and the
whole passed-gate inference rests on it. So the draft comes back
`order_verified: false` - open your resolution function, put the ladder in
the order it actually checks, add each gate's detail fields, then set
order_verified true. Until you do, chains mark passed gates with '~'.

save=True writes it to .bgate/causal_specs.json.
```

## character_generate

```text
"I want a model that looks like X." Plate, mesh, rig, into the engine.

THE WHOLE CHARACTER PATH AS ONE CALL. Every stage was already reachable and
a caller still had to know: condition the plate on the humanoid template or
the skeleton will not fit; key it or the backdrop arrives as geometry no
bone can reach; which backend takes which knobs; that a bind reports success
having weighted nothing. Get any of those wrong and it costs ten GPU minutes
to find out. They are the same five steps in the same order every time.

Each stage gates the next, so a failure costs the stage that found it.
Measured on the runs this was built from - an unkeyed plate took 605 s and
came back 21% non-manifold, refused by the quality gate, against 216 s and
16% for the same subject keyed; a collapse met its triangle budget with
20,799 of 39,803 faces inside out; a bind created all 22 vertex groups and
filled NONE, 64,878 of 64,878 vertices carrying no weight with every other
check green.

DRY_RUN IS TRUE BY DEFAULT. It quotes the backend and stops. This spends
real money at the plate and again at the mesh, and a tool that bills on the
first call is a tool nobody trusts twice - pass dry_run=False to run it.

backend   "" asks choose(), which REFUSES to pick a backend whose licence
          carries conditions. That refusal is the design: this tool does not
          know your revenue, territory or monthly actives. Name one after
          reading its terms.
godot_project  set it and the rigged .glb is imported, given a body and
          collider suited to what it is, wired into a .tscn and loaded
          through the engine to prove it opens. Leave it empty and nothing
          is written into a game project.

Returns every artifact by path, the gate result from each stage, and `stage`
naming where it stopped. `ok` is True only if a RIGGED character came out - a mesh that failed to bind reports ok=False with the unweighted count, and
that is a refusal, not a warning.
```

## cinematic_animatic

```text
Cut the storyboard panels together at their planned timings. FREE - calls
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
```

## cinematic_assemble

```text
Join a sequence's kept shots, in order, into ONE .ogv the game can load.

Refuses while any shot that is not marked 'cut' is unkept - assembling
around a missing beat ships a story that does not make sense rather than an
error. It also refuses a set of shots that are not all the same size,
because ffmpeg joins those into a broken file and reports SUCCESS.

The result is registered as a candidate like any other. WATCH THE WHOLE CUT
before keeping it: shots were judged alone, and a cut is judged as a cut - the light jumping, a character swapping hands, the camera crossing the line
are all invisible shot by shot.
```

## cinematic_continuity

```text
Do this sequence's shots actually CUT TOGETHER? Costs nothing but time.

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
```

## cinematic_deliver

```text
Build the Godot scene that PLAYS this cutscene. The last mile.

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
```

## cinematic_estimate

```text
What this sequence will cost to buy, before buying any of it. Free.

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
```

## cinematic_generate_shot

```text
Buy ONE shot of a planned sequence. Costs real credits. Runs in minutes.

ONE SHOT PER CALL, DELIBERATELY. There is no generate-the-whole-sequence
tool: the thing a human has to do between shots is LOOK at the clip, and a
loop is built to skip exactly that. Generate, watch, keep or re-generate,
then move to the next index.

generate_audio is FALSE by default and that is a considered default, not an
oversight. Model audio is baked into the clip and cannot be separated
afterwards, so it fights the score, cannot be ducked under dialogue and
cannot be localised. The picture is this seat's; the sound is the audio
seat's, laid over the top where it stays editable.

Local conditioning frames are uploaded to the provider automatically. The
encoder is checked BEFORE anything is charged, so a project with no
libtheora finds out before it buys a sequence it cannot play.
```

## cinematic_keep

```text
Approve a take, and put it in the engine project if the engine loads it.

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
```

## cinematic_plan

```text
Write a cutscene's shot list. SPENDS NOTHING - do this first, always.

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
```

## cinematic_probe_model

```text
Ask kie whether a registered model id actually exists. Opt-in, and READ
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
```

## cinematic_register_model

```text
Add a video model from a reference page you have READ. Spends nothing.

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
```

## cinematic_stuck_shots

```text
Find generations that were PAID FOR and never collected. This is the tool
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
```

## cutout_assemble

```text
Build a cutout character from its parts and emit a scene that moves.

`parts` maps SLOT -> image path (absolute, or relative to the project root).
cutout_templates lists the slots. Anything you leave out simply does not get
a sprite, so a half-generated kit assembles and shows what it has.

What comes out: `<name>.cutout.json` (the rig document - the editable
thing), `<name>.tscn` (bones, sprites, z order, an AnimationPlayer) and
`<name>.anims.tres` (six clips, baked onto THIS character's rest pose).
Drop the .tscn into a scene and call play("walk") on it.

THE FAR SIDE IS FREE. Slots ending _far reuse the matching _near image with
a tint unless you pass them explicitly, so a side-view kit is ten images.

`adjustments` nudges the template per character - {"arm_near": {"rot": -8}} - and those survive every clip, because the animation is baked as deltas on
top of them rather than as absolute poses.

It REFUSES to overwrite a .tscn that has changed since it last wrote one
(someone opened it in Godot, or edited it). `force=True` discards those
changes deliberately.
```

## cutout_equip

```text
Put a different part in one slot and re-emit - a hat, a sword, an arm.

This is what the whole pipeline is for: swapping equipment is one texture
on one slot, not a re-drawn character. The scene carries a pivot table so
the runtime `equip()` can do the same swap at RUNTIME; this tool is for
changing what the character ships wearing.

`pivot` is [x, y] as a fraction of the new part's own bounding box, y
measured UP from the bottom. Pass it and it is recorded as AUTHORED, which
means cutout_status will tell you if the part is later regenerated under it
rather than letting the pivot quietly point somewhere else.
```

## cutout_status

```text
What is wrong with a cutout character, and what is merely unfinished.

Reports rather than refuses - a half-generated kit is the normal state
while a character is being made. `missing` is slots with no part yet;
`problems` is the list that actually needs action:

  missing_texture  the document points at a file that is not there.
  stale_pivot      a pivot placed BY HAND against a drawing that has since
                   been regenerated. The pivot is still at the same
                   fraction of a different picture, so the hand now hangs
                   off the middle of the forearm and nothing says why.
  origin           the rig's feet are not on the ground line, so it hovers
                   or sinks in every scene it is placed in.
```

## cutout_templates

```text
The cutout rig templates, and what a kit for each has to contain.

A CUTOUT CHARACTER IS THE OTHER WAY TO ANIMATE IN 2D. The frame pipeline
pays per character per animation - six clips at eight frames is 48 paid
generations that all have to agree with each other, and a new hat means
regenerating every one. A cutout kit is about ten parts, the animation is
authored once per TEMPLATE and free forever after, and equipment is a
texture swap on one slot.

What it costs you: a puppet, not a painting. Parts are rigid Sprite2Ds on
Node2D bones - no mesh deformation, no squash, no per-frame redraw. For a
hero seen in close-up the frame pipeline is still the better answer.

`parts_to_generate` is the actual generation list: the far-side limbs reuse
the near-side drawings with a tint, which is what makes a side-view kit ten
images instead of sixteen.
```

## decision_add

```text
File a decision - with its acceptance test and what it leaves dark.

All three are MANDATORY and the tool refuses without them, because the two
that are easy to skip are the two that make the register worth keeping:

  acceptance   how anyone checks the call was honoured. Without one this is
               an opinion, and six weeks from now nobody can tell whether it
               held. "the hub loads in under 2s on the 3060 box", not "it
               should be fast".
  leaves_dark  what this call deliberately does NOT cover. A deferral nobody
               labelled gets 'fixed' as a bug by the next agent that finds
               it - naming it here is what stops that.

state: 'settled' is a ruling and only a human session may file one. 'open' is
a PROPOSAL - it lands in the director's "Awaiting a ruling" rail and binds
nobody until a human settles it. A dispatched agent asking for 'settled' gets
a refusal, not a quiet downgrade.

work_item_id / session_id link the ruling to the board item or the brainstorm
room it came out of. Both optional; both survive the thing they point at
being deleted.
```

## dialogue_write

```text
Author a dialogue tree as an engine-loadable resource, validated first.

nodes is a list of {id, speaker, text, choices: [{text, goto}], end}. `goto`
names another node's id; `end: true` marks a closing line, which must have
no choices. start defaults to the first node.

THE WRITE IS REFUSED, NOT WARNED, when the graph is broken, and the refusal
names the node: a choice pointing at a node that does not exist, a node
nothing reaches, a node from which no ending is reachable. All three are
invisible in the file and expensive in the game - a dead-ended branch is
found weeks later by a player, and a node with no exit reads as a hang.

canon_check runs on the way in, on the lines AND the choice labels. A hard
conflict refuses (pass allow_canon_conflict=True only when the canon is what
changed); review-level flags ride along in the result, because an unknown
name is the normal state of a first draft.

Lands at <godot project>/dialogue/<name>.dialogue.json - inside the
narrative seat's own lane, for both project layouts.
```

## encounter_design_set

```text
Declare enemies as INTERACTIONS and tasks as COMMITMENT SHAPES.

Two design failures this refuses, both of which look like finished work:

ENEMIES AS A LIST OF STATE MACHINES. "melee / ranged / support" is a
category table, not a design — three enemies that never change each
other's threat profile make fighting all three arithmetic on the same
fight. Each roster row is {name, pressure, alters:[{enemy, effect}],
role?, counterplay?}, and `alters` is the whole point: how does THIS enemy
change the player's read of THAT one?

TASKS THAT ARE ALL "STAND HERE FOR N SECONDS". A quota list can be long,
varied in fiction and completely uniform in mechanics, and prose review
will not catch it because the fiction is where the variety is. Each
objective row is {name, shape, costs, notes?} where `shape` comes from a
closed vocabulary — dwell, carry, escort, defend, route, timing, disarm,
restrict, manipulate, spend, gather — and `costs` says what the player
gives up for the duration. Call greenlight_status('encounter') for the
vocabulary with its definitions.

Both are optional: a game with no enemies is not refused for having none.
A DECLARED roster of isolated machines, or a declared task list that is one
shape eight times, blocks the move to production.
```

## evidence_assert

```text
Record what you SAW in a captured frame. CAPTURING IS NOT EXAMINING.

THE TWO-TAILED CAT. A character shipped with a forked tail for a full day
while its turnaround renders and a contact sheet sat on disk. Nobody opened
the rear view. Every defect that reached the player in that benchmark was
found by a human looking at a frame, and several of those frames already
existed. An evidence gate that records "beauty.png, 412 KB" has recorded
the one thing that was never in doubt.

`says` is prose, and it has to be: no static check reads a picture. What
makes it evidence is everything around it - the file, its DIGEST at the
moment of the claim, who said it, when. Regenerate the frame and the digest
moves, so the claim is reported stale rather than carried forward onto an
image it was never about.

`views` is which of front/back/left/right this covers, for a character.
Name them: the forked tail was invisible from every angle anybody had
rendered, and 'a turnaround' meant three views in one run and six in
another. `subject` groups views of one character.

Refuses a sentence short enough to be a shrug. "looks fine" is what the
two-tailed cat shipped under.
```

## game_view_get

```text
WHICH 2D VIEW THIS GAME IS, and everything that follows from it.

Read this BEFORE generating any level art or any prop. The view is not a
style preference, it decides what "correct" means: a barrel showing its lid
and a sliver of side is right for a top-down game and wrong for a
platformer, and a barrel showing two side faces is right for an isometric
game and wrong for both of the others.

It was not declared anywhere until now, and the cost was measured: a prop
batch prompted with "a high 3/4 top-down game view" came back ISOMETRIC —
to an image model "three-quarter" means the standard product render — and
every prop showed two side faces, to stand on a floor tileset drawn flat
top-down. The prompt was the proximate cause. The real one was that the
view lived in a prompt instead of in the project, so each agent re-derived
it and drifted.

The result carries the camera clause per prop mount (`cameras`), the tile
geometry, which prop mounts exist at all in this view, and how the level
generator checks playability. USE `cameras`, do not paraphrase it: the
clauses forbid the wrong reading BY NAME, because a clause that only says
what it wants inherits the model's default for everything it forgot to
forbid.
```

## godot_deliver_asset

```text
3D ONLY: take a finished .glb the rest of the way - engine, then scene.

IT TAKES A MESH AND NOTHING ELSE. There is no way to hand this a PNG, a
.wav or a tileset, and a 2D seat that reaches for it because the NAME
sounds like the delivery path has taken a wrong turn - observed: an art
seat was steered to "deliver via godot_deliver_asset", loaded its schema,
and could not use it. For a 2D asset the delivery path is
godot_import_asset, which copies, purges the stale import cache, reimports
and reports whether the engine now serves the current bytes.

THE STEP THE 3D PATH WAS MISSING. Everything before this ends at a file:
blender_combine writes a .glb, and blender_turnaround photographs a BLENDER
scene under BLENDER lights. Neither asks the engine anything, so a rig that
did not import, a texture that did not travel, a 40x scale and an asset with
no collider were invisible by construction. THIS is where an asset stops
being a file and becomes a thing in the game: imported, given ONE collision
strategy, instanced under the body its mesh implies in its own .tscn, stood
on a lit floor, and photographed by Godot's own renderer.

THE SCREENSHOT COMES BACK IN THIS RESULT AS AN IMAGE, and it is the first
time anyone - you included - sees the asset under the renderer that will
ship it. Look at it, then read `checks`: the measurements are the half you
cannot argue with.

`checks` is the gate. loads_in_engine, has_geometry,
materials_carry_a_texture, real_world_size and has_collider are required;
has_skeleton / has_animations / has_blend_shapes report without failing, so
a prop is not marked broken for having no rig. A FAILING GATE STILL WRITES
THE SCENE AND TAKES THE SCREENSHOT, deliberately - a 2880 m `giant_hero`
fails real_world_size and you still get the frame, because a gate that hides
the asset is one you cannot debug.

THE BODY IS CHOSEN FROM WHAT THE MESH IS, and it decides the collider with
it. Skinned (it has a skin, so a Skeleton3D and joints) → CharacterBody3D
with a capsule fitted to the TORSO. Unskinned → StaticBody3D whose colliders
the importer builds from the real geometry, and no capsule. Pass
character_body="RigidBody3D" for a prop that should fall and be pushed, or
any class name to override; `root_body` and `collision` in the result say
what it became. Every asset used to be wrapped in CharacterBody3D - which
only moves when code calls move_and_slide(), so a crate delivered that way
never simulated at all - AND carried both an accurate trimesh and an
invisible person-shaped capsule on two different bodies at once.

physics: auto (the strategy above) | all (mesh shapes on every mesh, capsule
stands down) | none (importer defaults, capsule is the collider). Leave
max_size_m unset and the bound comes from what the asset IS: 4 m skinned,
50 m otherwise.

THE CAPSULE IS SIZED FROM THE TORSO, NOT THE POSE. It used to come off the
widest horizontal axis of the merged bounds, so an A-pose handed it the ARM
SPAN: a 1.75 m character shipped inside a 1.63 m wide cylinder that could
not fit through a human door, and passed the gate because has_collider only
counted shapes. has_collider now fails a capsule wider than half the figure
it wraps.

DELIVERING A .glb WHOSE FILENAME IS ALREADY IN assets/ OVERWRITES IT. The
destination is keyed on the filename alone, so two different `hero.glb` from
two different output directories collide and the second wins under the
first's uid. `replaced` in the result names what was overwritten and whether
the bytes actually differ.

REDELIVERING DOES NOT CLOBBER THE SCENE. If <name>.tscn already exists its
model ext_resource is repointed at the new import and the node tree is left
exactly as the human left it - scripts, extra nodes, tweaked transforms all
survive, and `scene_action` in the result says which happened (written /
rewired / left_alone). Pass overwrite_scene=True to deliberately throw those
edits away. The old behaviour rewrote the file every time, which during one
session destroyed the same hand edit five times running.

with_camera adds a first-person Camera3D to the character. OFF by default,
and do not turn it on for anything you intend to instance into a level:
Godot makes the first camera into the tree current, and an OBSERVED boot
came up looking out of the delivered character's eye sockets instead of the
player's. Turn it on only when this scene IS the player (templates/3d's
player.gd requires a $Camera3D child).

The frame is archived to the preview gallery and REGISTERED as an artifact
(`artifact_id`), so art_qa_verdict can be pointed at the in-engine shot
rather than at a Blender render of a Blender scene.

godot_project: the directory holding project.godot. `glb`: the asset to
deliver, e.g. the out_path blender_combine just wrote.
```

## godot_evidence

```text
Capture a frame PLUS a screen-space manifest of what is actually where.

The upgrade over godot_screenshot. A PNG shows what the game looks like; it
cannot tell you whether the health bar matches the fighter's real hp,
whether a hitbox lines up with its sprite, or whether an entity is on
screen at all. This runs the game the same way, then walks the live tree at
capture time and reports every measurable node as screen-pixel bounds,
visibility, z, and - for progress bars and labels - its RUNTIME VALUE.

Returns beauty.png, an overlay.png with collision shapes (red) and other
bounds (blue) stroked over the frame, and manifest.json with `entities` and
`ui`. Pair with `causal_chains` - the manifest says what was on screen, the
chains say why it happened.

CALL IT WITH NO `scene` TO PROVE THE DEFAULT. That is the capture the
release gate requires and the one nobody was taking: a 3D benchmark shipped
with `application/run/main_scene` still pointing at the scaffold demo while
every named-scene test passed, because each of those tests named the scene
it tested and none of them named the one the game boots into. With no
`scene` this launches exactly what pressing play launches, records the
proof against the release gate, and FAILS if the default scene is missing,
broken, or still the template's demo room. Named-scene evidence does not
substitute for it.

ENTITIES CAN BE EMPTY ON A POPULATED 3D SCENE, and that used to come back
as `ok: true` with `entities: {}`. The manifest walk is screen-space and
2D-shaped; on a 3D project it is not authoritative about what is in the
world. The result now says so rather than reading as "this scene has
nothing in it" - measure 3D contents with godot_inspect_resource.

THE CAPTURE IS NOT THE EVIDENCE. Record what you saw in the frame with
evidence_assert(scene, frame, says=...) - a file on disk that nobody opened
is what let a two-tailed character ship for a day.

godot_project: the directory holding project.godot.
```

## godot_import_asset

```text
THE DELIVERY PATH FOR ANY ASSET - 2D or 3D. A PNG belongs here too.

Copies the file in, DROPS THE STALE IMPORT CACHE, triggers a headless
import, then loads the resource IN-ENGINE and reports what Godot actually
built. Copying a file in is not integration: an asset that imports with
zero surfaces is a silent failure, and this catches it by checking the
engine's view, not the file's presence.

USE THIS INSTEAD OF WRITING THE FILE INTO THE PROJECT YOURSELF. Measured
twice in the benchmark games: an art seat wrote new PNGs straight to
assets/, every structural check passed - resource path, dimensions, scene
reference - and the running game drew the OLD placeholder, because Godot
serves the imported product and the cache had not been rebuilt. A
screenshot was the only thing that caught it. The `freshness` field in this
result is that check, made mechanical: it says whether the engine's cached
product now matches the bytes on disk.

NOT godot_deliver_asset, which is the 3D-only sequel to this (it takes a
.glb, gives it a collider and stands it in its own scene). For a sprite,
a tileset, a sound or a font, THIS is the tool. `src_path` must be OUTSIDE
the project - generate to a staging directory and import FROM there.

THE DESTINATION IS KEYED ON THE FILENAME ALONE, so a second `hero.glb` from
a different output directory lands on the first one and wins. Keeping the
existing .import and its uid is right - every .tscn in the project points at
that uid - but the mesh underneath it has changed, and `replaced` in the
result is where that is said. Read it before telling anyone the import was
clean.

`alpha_mode` carries the OTHER silent one: Godot 4.7 imports glTF
`alphaMode: MASK` as DEPTH_PRE_PASS rather than ALPHA_SCISSOR, which moves
every one of those surfaces into the sorted transparent pass. Nothing errors
and a screenshot looks correct; it costs frames once a scene has hundreds of
alpha quads on it. Read the warning before shipping foliage.

IMPORT SEQUENTIALLY. Two headless imports at once fight over the shared
`.godot/` cache and one of them dies with a Windows PermissionError that
reads like a locked file rather than like the race it is. Batching them in
parallel to save wall-clock buys an error instead. And a `src_path` already
inside the project makes this copy a file onto itself, which Windows also
refuses - generate to a staging directory and import FROM there.

godot_project: the directory holding project.godot.
```

## godot_retarget_check

```text
Ask the ENGINE whether a rigged character is a humanoid it can retarget.

The rigs this pipeline builds carry Godot's own SkeletonProfileHumanoid bone
names, and the whole point of that is that any humanoid animation library
then plays on the character. Nothing tested that claim until this tool. A
.glb can export 23 perfectly-named bones in a FLAT hierarchy - blender_rig
reports 0 unweighted, godot_deliver_asset photographs it happily, and the
character can be animated by nothing except a clip authored for it alone.

Three answers, and they fail independently:

  missing / extra   coverage against the profile, by exact name.
  chain[].propagates  rotating a shoulder moves the hand. This is the one
                    that catches a lost hierarchy, and it is invisible to
                    every other check in the product.
  clip.drives       a profile-authored rotation track actually turns the
                    bone. A NodePath that resolves to nothing plays
                    silently and moves zero.

`retargetable` is the verdict. False means the humanoid animation ecosystem
is unavailable to this asset - treat it the way you treat `rigged: false`.

bone_map_res: a res:// path to save the BoneMap to, or "" to skip. Written,
it is what the user's import settings point at to retarget real clips.

res_path must already be imported - godot_import_asset first.
```

## godot_run

```text
Run a GDScript headless and capture its output.

`script` is EITHER the source itself OR a path to a .gd file — this reads
the file when you hand it one. Passing a path used to run the path AS
GDSCRIPT: `res://tests/door_test.gd` is not a statement, so the engine
reported a parse error on line 1 of a file the caller never wrote. The
common case is re-running a script that already exists on disk, and making
the caller inline it first is asking them to copy a file into an argument.

The script MUST `extends SceneTree`, do its work in `_init()`, and call
`quit()` - without quit() it runs until the timeout. Returns stdout, stderr,
and any parse/script errors (Godot prints SCRIPT ERROR and still exits 0, so
check `errors`, not just the exit code).

godot_project is the GODOT project directory (the one holding project.godot),
not the Builders Gate root - that one is `project_dir`.
```

## godot_scaffold

```text
Create a runnable Godot project wired for playtesting.

kind: 2d (platformer slice) | 3d (third-person slice + prop kit + vehicle demo). dest defaults to
<project root>/game.

The template ships the BGate telemetry autoload already registered, and a
player whose feel tunables (gravity, fall_multiplier, coyote_time) are both
exported AND emitted on jump/land - so the first playtest already produces
the telemetry join.

A non-empty dest is refused unless force or replace, and THOSE TWO ARE NOT
THE SAME THING:

  force=True    fill in WHAT IS MISSING. A file that already matches is left
                alone; a file that differs is the user's and is SKIPPED, not
                overwritten. This is the one to reach for to top up a
                missing addon or a deleted script.
  replace=True  put the template back over the top, and copy each victim to
                <name>.bak first.

force used to mean what replace means now, and it was a data-loss bug in a
feature's clothing: someone topping up one missing file lost their
project.godot, their player.gd and their export_presets.cfg in place. That
last one is unrecoverable in the usual case - the .gitignore this same
template stamps excludes export_presets.cfg, so the customised export
targets were not in git either.

The result lists `created`, `unchanged`, `skipped` and `replaced`, so say
what happened rather than letting the user find it in a diff.
```

## godot_screenshot

```text
Run the ACTUAL game and capture the viewport to a PNG at `at` seconds.

The look-iteration loop: headless checks prove the game boots, this shows
what it LOOKS like. A game window appears briefly on the user's screen
(rendering needs a display) and closes itself after the capture. The shot
is archived to the preview gallery - check it before and after visual work.

THE WINDOW NEVER GAINS TRUE FOREGROUND FOCUS ON WINDOWS, AND THAT IS THIS
TOOL'S ARTIFACT, NOT YOUR GAME'S. Read this before "fixing" anything it
seems to reveal about input.

The capture window is spawned by a background process, so Windows does not
hand it the foreground. The mouse is therefore never captured:
`Input.mouse_mode` stays VISIBLE no matter what `_ready` asked for, and any
code gated on MOUSE_MODE_CAPTURED - a viewfinder, a first-person look
controller, a pointer-lock HUD - collapses in the shot while working
perfectly for a human running the same build.

Measured cost of not knowing: a previous pass "fixed" this by re-asserting
mouse capture EVERY FRAME from the shot rig, with a comment blaming the
game. That masked the real finding for a whole pass. **When a fix has to run
every frame forever, it is a symptom, not a cure.**

So: do not conclude anything about input capture from a screenshot, and do
not add per-frame re-capture to make one look right. To check input for
real, run the game with `godot_run` and assert on state, or have a human
play it. `focus` in the result says this out loud on every call.

Two more things this tool cannot give you. It returns NO STDOUT, so a
windowed run's diagnostics must be written to `user://` and read back. And
`res://.bgate_shot.gd` is the harness's own helper - it can error during
unrelated headless runs; work around it, do not delete it.

godot_project: the directory holding project.godot.
```

## godot_test_run

```text
Run this project's own Godot test scripts headless and score them.

Discovers `<godot project>/tests/*.gd` - resolved through the project's real
layout, so it works whether project.godot sits at the root (bgate init) or
in <root>/game. Pass `paths` to run a subset; each may be absolute,
project-relative, or relative to the Godot project. Dotfiles and
underscore-prefixed files are SKIPPED: agents leave scratch (`.orig_*.gd`,
temp probes) in test directories and a backup of a broken file is not a
test.

`mode` BOUNDS WHAT COMES BACK, and the default is not `full`:

  summary        counts and failing script names only
  failures_only  DEFAULT - the failing assertions, an excerpt of their
                 output, and a path to the complete log
  changed        only scripts whose verdict MOVED since the last run. The
                 shape for iterative debugging
  full           everything, including passing scripts' output

Measured on one agent's log while it debugged a single assertion: 68% of
8 MB was tool results echoed back, 366 entries at ~15 KB each. That is what
consumed the turn and clock ceilings - not the model.

TWO SIGNALS, REPORTED SEPARATELY. `assertions_ok` is whether the script's
own FAIL markers held; `process_ok` is whether the ENGINE ran cleanly.
Collapsing them produced `ok: false` with `0` failed assertions, which
reads as nonsense and got dismissed as harness noise for a full session -
while the engine error behind it (`N resources still in use at exit`) was a
REAL leak in the project's tests. Read `engine_error_scripts` and the
per-script `errors`; an engine complaint is not automatically noise.

A PROJECT WITH NO TEST SCRIPTS IS NOT A PASS. It answers ok=false with
no_tests=true and says where it looked, because "0 failures" out of nothing
run is the single most misleading thing this tool could report.

Each script must `extends SceneTree` and call `quit()`, like godot_run - or
`extends Node` if it needs this project's autoloads.

Every run is RECORDED (.bgate/engine-tests.jsonl), which is what the
dashboard's Tests tab reads. It used to carry its own copy of the runner
and record nothing, so an agent's runs were invisible to the QA seat.
```

## greenlight_advance

```text
Move the project to the next production stage, or learn why it cannot.

thesis -> graybox -> production -> release. Moving BACKWARD is always
allowed and is not a failure: a project that discovers in production that
its loop does not hold should drop to graybox and say so.

Forward, each boundary asks for something real:
  graybox      a settled mechanical thesis
  production   a graybox the director passed, plus an enemy roster that
               is interactions rather than isolated state machines and an
               objective list that is more than one commitment shape
  release      the presentation gate: every room reviewed as a WHOLE room,
               every delivered asset measured at game scale, every wired
               audio cue heard in a gameplay capture

THE RELEASE BOUNDARY TAKES NO WAIVER. greenlight_waive lets one seat
through a stage hold; nothing lets a release candidate through an
incomplete presentation QA. Call greenlight_status('presentation') for the
outstanding list.
```

## greenlight_status

```text
WHAT STAGE IS THIS PROJECT AT, and what is it holding.

THE FIRST TOOL TO CALL WHEN A QUEUED ITEM WILL NOT DISPATCH. Builders Gate
holds whole SEATS until the work ahead of them is real: art, audio and
cinematic do not start until gameplay has proved the core loop in one ugly
graybox room and the director has ruled that the interaction is actually
interesting. That hold is enforced in the readiness rule itself, so a held
item looks exactly like a blocked one from the board and this is where the
reason lives.

`section` narrows the read, because the whole thing is four gates:
  ''            stage, thesis, graybox, held seats, what blocks the next
                stage — the default, and what a director wants
  'encounter'   the enemy roster and objective shapes, with their findings
  'scale'       the reference scale contract and anything unmeasured
  'rooms'       full-room composition reviews and their thresholds
  'presentation' what a release candidate still owes (this gate takes no
                waiver — see greenlight_advance)
```

## greenlight_supersede

```text
RETRACT a gate finding that a better measurement has disproved.

THE ROW THAT COULD NEVER BE DONE. A false blocker reached the presentation
gate because a tool answered outside its competence - scale_check measured
the opaque pixel box of a 3D character's TURNAROUND RENDER and reported
"30.00 player-heights tall" for an animal 0.24 m long. The row said it
cleared by being done, and it could never be done, because it was measuring
nothing real. There was no way to withdraw it.

Fixing the tool is not enough on its own: the bad finding is already in the
ledger, and a gate that cannot retract one teaches operators to route
around it.

So: a later, AUTHORITATIVE measurement supersedes an earlier one. The
retraction is itself a recorded row - the finding stops blocking and stays
readable, carrying what replaced it and why. `why` has to say what was
measured instead and why it outranks the finding; "superseded" with no
antecedent is the same unaccountable erasure as a delete.

greenlight_status(section='findings') lists what is there, with ids.
```

## greenlight_thesis_set

```text
Settle the MECHANICAL THESIS — the one sentence the game is built on.

"What decision is the player repeatedly making that makes this game
interesting?" A feature list is not an answer to that question, and this
tool refuses one: the sentence has to name an act of choosing, and the
decision has to have at least two options, real stakes, and a reason the
answer is not the same every time.

That last field is the one that matters. A game can have nouns, systems,
a setting and a full backlog and still have no decision structure, and the
way you find out is that nobody can say why the player would ever pick the
other option. Say it here, before anything is built against it.

  sentence           one sentence naming the repeated decision
  options            two or more things the player is deciding between
  stakes             what the wrong pick costs
  tension            why the answer is not the same every time
  dominant_strategy  the play that would COLLAPSE this decision, named on
                     purpose so QA can go looking for it
  cadence            how often the decision comes round

Settling a thesis does not advance the stage — greenlight_advance does.
```

## handoff_note

```text
Record IN-FLIGHT state on the project thread, for the next session.

The board says what was dispatched and the bible says what was settled.
Neither says what you were halfway through, why you chose the thing you
chose, or what you deliberately did not do - and that is what evaporates
when a session ends. This is an append-only trail read back at the start of
the next one, so a death costs a successor one read instead of an
investigation.

CALL IT AS YOU GO, not at the end. A closed window, a kill and a crash all
fire nothing, and those are the sessions most worth resuming.

kind:
  state     where things stand; what is half-done.
  decision  a call you made, WITH the reason. If it is settled canon it
            belongs in the bible - bible_add it and cite the section in
            `refs` rather than restating it here.
  deferred  something you chose NOT to do, and why. An unlabelled deferral
            is the most expensive thing to lose: the next agent finds it and
            "fixes" it as a bug.
  blocker   what is in the way, and who owns it.
  next      the very next action.

refs: ids/paths this note points at - "bible#12", "item 41",
"game/data/loot/floor_0.json". Cite, do not duplicate.
```

## image_edit

```text
Generate an image CONDITIONED ON reference image(s) - the consistency
primitive, exposed raw. Use it to regenerate a single sprite pose against a
character's existing reference (~$0.04 at medium) instead of re-buying the
whole set, or to derive variants that must stay on-model.

ref_images: PINNED REFERENCE NAMES (see ref_list - preferred) or absolute
paths. filename lands under the project's .bgate_out/art/. Result is
archived to the gallery - LOOK at it. transparent=True runs the
keyable-background contract (flat chroma backdrop -> keyed -> audited), not
the API's background=transparent, which does not reliably return alpha.
```

## image_generate

```text
Generate PAINTED art - portraits, select-screen cards, title splashes,
textures, decals, stage paint-overs. Costs real money per image
(~$0.02-0.19).

provider  "" picks from what is CONFIGURED - openai if OPENAI_API_KEY is
          set, else krea if KREA_API_KEY is. Name one to force it, and you
          get that provider's own error if its key is missing rather than a
          silent substitution that bills you for a model you did not ask
          for. This was pinned to openai, so a project holding only a Krea
          key could not reach this tool at all while krea sat configured
          and unused.
model     provider-specific; "" takes that provider's default.

Division of labor: use blender_sprites for anything needing the SAME
character across multiple frames (an image model can't hold a rig steady);
use this for one-off illustrated pieces and for the maps that go onto 3D
layers via blender_texture.

CONDITIONING ON THE PINNED REFERENCES IS PART OF THIS TOOL NOW. It used to
take no reference at all while every seat brief said to generate "against
the pinned refs", so the instruction could only be obeyed by switching to
image_edit or by not obeying it:
  ref_images   pin NAMES (see ref_list - preferred) or absolute paths.
               `name@r2` reaches an older revision.
  use_pinned   pull the project's own anchors with NO paths passed by hand:
               a ref kind (character | style | ui | concept) or "all".
               Capped at the first 4 pins, explicit ref_images first.
  anchors      extra images used ONLY to choose the key colour, never sent
               to the model - the identity whose palette the chroma must
               avoid colliding with.
  ref_strength how hard a reference pulls (Krea-side; 0-1).

task_kind names WHAT IS BEING MADE and changes real decisions, not wording:
  texture   forced square (a non-square map stretches across a unit UV and
            nothing downstream can undo it), given the flat-albedo clause - no baked light, no camera angle - and not keyed, because the
            surface IS the whole frame. Pair with tileable=True for a
            repeating field; the seam guarantee is a mirrored post-pass, not
            a sentence in the prompt.
  decal     a logo, wordmark or insignia, where THE TEXT IS THE SUBJECT.
            Keyed to real alpha like a sprite, with the one variant of the
            background contract that does not forbid lettering. Do not pass
            transparent=True for these; the keying is automatic.
  anchor/animation/item/sprite/... keyed sprite work.
  background/tile/ui/concept      full-bleed plates, never keyed.
Omit it and nothing changes: keying then follows `transparent` exactly as
it always did.

transparent=True does NOT ask the API for alpha - measured, gpt-image
answers that request with a gradient. It runs the KEYABLE-BACKGROUND
contract instead: flat chroma backdrop, keyed out, then audited. A cut that
comes back haloed/bled FAILS with the flag rather than being handed back as
a sprite with dirty alpha.

filename is relative to the project's .bgate_out/art/ (e.g. "tommy_portrait.png").
The result is archived to the preview gallery - LOOK at it before importing
into the game with godot_import_asset.
```

## image_sprites

```text
PAINTED sprite set - REFERENCE-FIRST for consistency.

provider: blank means the project's stored preference (art.provider),
then the identity routing. "openai" and "krea" condition on the
reference in genuinely different ways, and it changes what you get:
gpt-image EDITS the reference image, which holds identity hard but drags
the reference's own lighting along; a STYLE REFERENCE at `ref_strength`
follows an art style more faithfully and holds a specific face less.

On krea this tool takes nano-banana-2 unless `model` says otherwise, and
that pin is the same distinction: krea-2-large (the provider's general
default) conditions on a reference as STYLE, and a style reference cannot be
asked to hold a subject through a pose change, because holding the subject
is not what it does - measured on the party idles, krea-2-medium drew a FACE
in seven of eight frames when four of them were specified as back views.
nano-banana-2 takes its references as edit inputs, keeps `styles` so a
trained LoRA still rides alongside, and bills a flat $0.06 against
krea-2-large's $0.065-with-references. Name `model` to override; every
entry in bgate_adapters.krea.MODELS is available.


How it works (and why): a fresh generation invents a new character every
time, and asking for many poses in one image comes back misaligned. So:
(1) generate ONE reference character (or pass ref_image to reuse an approved
one - reusing the ref is also how you REGENERATE a single pose later without
changing the fighter); (2) each pose is an EDIT conditioned on that
reference - same character, new stance; (3) frames are alpha-trimmed,
registered on the body's mass, stitched into <name>_sheet.png +
<name>_frames.tres - drop-in for AnimatedSprite2D.

THE SPRITE CONTRACT SUPPLIES THE SHAPE YOU DID NOT TYPE. When
frame_width/frame_height and `view` are left at their defaults, they are
read from sprite_contract_get(character=name) - the cell size and camera
the game DECLARED - so the usual call carries no geometry at all and a
minted sheet cannot disagree with the contract by a typo. Passing either
explicitly wins outright; `contract_used` in the result says which
authority shaped the sheet.

character_prompt: the character + art style (full body, single character - framing/transparency contracts are appended automatically).
poses: [{"name": "jab", "description": "lead fist fully extended right,
body driving forward"}] - name becomes the animation; description is the
stance. LOOK at the reference preview before the poses run wild, and at the
sheet preview before importing. Cost: 1 ref + 1 edit per pose (~$0.04-0.25
each by quality). Failed poses are listed, never silently shipped.

ARCHETYPES ARE THE BETTER WAY TO ORDER AN ANIMATION, and they cost nothing
extra. Pass `archetypes=["idle", "walk", "attack"]` INSTEAD of `poses` and
the key poses come from bgate_core.art.animspec: a walk built from contact /
down / passing / up rather than four frames described as "walking", an
attack whose impact frame HOLDS while its wind-up is quick, an idle that
ping-pongs so its loop cannot seam. Call sprite_plan first to see exactly
what will be generated and what it will cost. `view` ("side view, facing
right") is prepended to every pose description so the camera convention is
stated once instead of drifting between eight prose strings. Hand-written
`poses` still work and are still right for anything the catalogue does not
cover.

anchor_views: how many views of the character condition every pose. 3 (the
default) generates a three-quarter and a profile view off the approved
anchor ONCE and passes all three on every call; 1 is the old single
front-view behaviour. This is the highest-leverage knob here. One front view
plus previous frames that are near-copies of it is the weak reference
configuration - distinct angles carry far more identity than more of the
same angle - and it bites hardest in the normal case, a side-view game asking
for side-view poses against a front-view anchor, where the model re-invents
the profile on every call and re-invents it differently each time. That is
not drift a re-roll fixes; a re-roll buys another guess at information the
anchor never carried. Two extra generations per character against one per
pose plus one per re-roll.

palette: {"lock": ..., "colors": ...}, the sheet's colour handling.
`lock` is "auto" (default), "on" or "off". Locking quantises every frame
to the reference's own palette, which makes colour drift UNREPRESENTABLE
rather than merely detectable - the existing palette gate finds drift and
pays for a re-roll; this leaves nowhere for the drift to be stored. It is a
posteriser, so "auto" measures the reference and turns it on only for flat,
cel or limited-palette art, where it is free. Painterly art with real
gradients is left alone.

sheet_padding: transparent gutter between cells, in pixels. 0 is a plain
strip. Raise it to 1-2 if the game draws sprites at a non-integer scale with
linear filtering, where sampling at a region edge otherwise pulls in the
neighbouring frame. Sheets long enough to exceed the safe texture width wrap
into a padded grid automatically whatever this says.

THIS IS THE MOST EXPENSIVE TOOL HERE. The plan is priced before anything is
bought and the estimate is reported, so a human sees what a set costs before
they buy it. Nothing refuses it on money: this product keeps no ledger and
holds no budget, and the balance that decides is your provider account's.

limits: {"max_retries": 1, "timeout": 300, "max_seconds": 1800} - how long
this run may take and how hard it may retry, all optional. `timeout` bounds
ONE image call and `max_seconds` the whole run; past the deadline the
remaining poses are reported as skipped and whatever was made is still
assembled, because half a sheet plus a reason beats a hung call. An unknown
key is refused by name.

Returns the assembled sheet result, or {ok: false, stage, error} when the
provider preflight, the reference gate or every pose fails. The result carries a
`motion` block - duplicated frames, popped poses, cycles that do not close,
figures in more than one piece - which is the half of quality the identity
judge cannot see, because every one of those faults is perfectly on-model.
```

## image_talkhead

```text
ANIMATED TALKING PORTRAIT: a face whose mouth moves while it speaks.

Different asset class from image_sprites, and the difference is the point.
A sprite set animates a BODY through space, so its frames differ by pose.
This animates a FACE at rest: every frame is meant to be identical except
the mouth, and "identical twice" is exactly what a generator will not give
you. So the work here is holding everything still, not posing anything.

Worth the four generations: a dialogue card showing a still bust reads as a
picture of a character. The same bust with a mouth moving while the line
types reads as the character talking to you.

HOW IT HOLDS STILL, and each of these was learned by it not doing so:

  * ONE ANCHOR, N SIBLINGS. Every frame conditions on `anchor`, never on the
    frame before it. Chained conditioning drifts, and on a face drift is
    instantly legible: three frames in, the ears have moved and it is a
    different character. Pass a `ref_pin` name or a path as `anchor`; with
    none, the first frame generated becomes the anchor for the rest.
  * MOUTHS ARE GENERATED, NOT DERIVED. Elsewhere the rule is derive what you
    can, because a mirrored facing is a transform. There is no transform
    from a closed mouth to an open one.
  * REGISTERED ON SILHOUETTE WIDTH. Independent generations do not share a
    pixel grid, and a head that jumps two pixels reads as a flinch. Width is
    the rigid measurement; an open jaw grows the silhouette downward, so
    aligning on height shrinks the face every time it speaks.
  * DRIFT IS MEASURED AND RETRIED. "Same colours" in the prompt works about
    three times in four, which is the dangerous amount: the fourth comes
    back colour-shifted, invisible at 128px and obvious as flicker at 10fps.
    Any frame past `drift_limit` is regenerated up to `max_retries` times.

Emits `<name>_talk.png` (4 cells: rest, half, wide, blink) and
`<name>_talk.tres` with a looping `talk` animation over rest/half/wide/half
and a one-shot `blink`. Blink is kept out of the cycle so it cannot land
mid-syllable. Drop the .tres on an AnimatedSprite2D.

Returns {ok, sheet, tres, frames:[{frame, drift, attempts}], worst_drift}.
```

## item_generate

```text
Mint ONE gear/item icon - transparent, class-templated, tracked.

item_class is one of item_classes() (main_hand, off_hand, head, body, feet,
consumable, throwable, ranged). descriptor names the item ("curved saber").
material/element/tier are the variant axes. `character` names a pinned ref
with a visual profile (profile_set) - its style is appended so worn gear
reads as the same set as the fighter it hangs on. An already-minted item
(manifest on disk) is skipped, not re-bought; force=true regenerates.
Costs real money per image (~$0.02-0.19 at `quality`). For a batch, use
item_variants. LOOK at the preview before importing into the game.
```

## item_variants

```text
Mint a BATCH of variants of one class from a parameter grid - the
cartesian product of the axes you pass, each a self-contained item.

This is the "plethora of gear, easily" engine: pass materials=[...],
tiers=[...], elements=[...] and get one on-set icon per combination.
`character` names a pinned ref with a visual profile - its style is woven
into every prompt so the whole set matches the fighter that wears it.
Already-minted variants (manifest on disk) are skipped and reported, so a
re-run finishes a batch instead of re-buying it; force=true re-buys.
Every image costs money, so `limit` caps what a run may BUY (default 12) - the plan and its $ estimate are reported and refused if new images exceed
the cap, so you confirm the spend before it happens. LOOK at the set
before importing.
```

## kie_video_generate

```text
Generate a VIDEO CLIP through kie.ai. Costs real credits, and video is the
most expensive thing this product can buy - kie's own docs put video at
100-500 credits against an image's 10-50.

THE ARGUMENTS ARE INTENT, NOT ONE MODEL'S FIELD NAMES, so this drives any
registered video model rather than only Seedance. kie's own catalogue does
not agree with itself on spelling - Sora 2 counts `n_frames` where Seedance
takes `duration`, and calls its shape "landscape" where Seedance wants
"16:9" - so the model's table entry translates. cinematic_options lists
every registered model with the exact ranges each one accepts.

  seconds     how long. Seedance does 4-15; ranges move per model.
  quality     480p | 720p | 1080p | 4k on Seedance
  shape       16:9 | 9:16 | 1:1 | 4:3 | 3:4 | 21:9 | adaptive on Seedance
  audio       generated audio is BAKED IN and cannot be removed later

first_frame may be a public URL OR a local path - a local one is uploaded to
kie's file store first and the minted URL (which dies in 3 days) is used.

Runs in MINUTES, not seconds; the default timeout is half an hour.
filename lands under .bgate_out/video/.

THIS IS THE RAW DOOR, AND IT IS PROBABLY NOT THE ONE YOU WANT. It buys one
unmanaged .mp4 - no shot list, no provenance row against a sequence, and
Godot CANNOT PLAY AN .mp4 at all, so nothing here reaches a
game. Use it for a one-off reference clip a human watches. For anything the
project ships, cinematic_plan / cinematic_generate_shot / cinematic_keep run
the same clip through the candidate -> human decision -> transcoded asset
path, which is what makes it playable.
```

## level_generate

```text
Generate a level and write it into a scene as TileMapLayer nodes.

The whole chain: BSP layout -> neighbour-bitmask autotiling -> the packed
binary Godot stores tiles in -> a .tscn edit, backed up. No engine and no
editor involved, so it runs headless and is a normal reviewable diff.

THE TILESET DESCRIBES ITSELF, so this call takes NO atlas coordinates.
`tileset_generate` writes a `<name>.tiles.json` sidecar for a set it made;
for a hand-built or imported sheet, `tileset_describe` writes the same file
once. Either way the mask table, the sources and the layouts come off disk.
Without that file this refuses rather than guessing - a guessed coordinate
draws a complete, confident, wrong-looking level, which is worse than an
error. `walls=False` writes the floor layer only.

WHICH TILE GOES WHERE is decided by a neighbour bitmask, the same job the
Godot editor's terrain sets do - and they only run in the editor, which is
why it is redone here.

`props=True` adds a third layer of DRESSING — wall torches, clutter against
the architecture, cover in the rooms you walk through, a feature in the dead
ends. Placement is by what the room is for (see `bgate_core.art.props`) and every
solid prop is refused if it would break the level into two regions, checked
by flood filling rather than by reasoning about it. It needs
`prop_manifest`, the file `prop_generate` writes beside its atlas: the
types, coordinates, spans, texture origins and animation all come from
there. `prop_density` is the only dial here, because it is the only one
that is about the LEVEL rather than about the sheet.

EACH TYPE DECLARES ITS OWN CONSTRAINTS and the placer obeys them instead of
assuming a prop goes anywhere. A wall mount occupies the WALL cell, so it is
attached rather than floating in the room beside it. A side-view or angled
sprite declares which walls it can be drawn on — `torch` is ("e", "w"), so it
never lands on a horizontal wall where a three-quarter view reads as pasted
on. Nothing mounts on the wall south of a room, whose inner face points away
from the camera, and nothing mounts on a corner, where the face it needs is
interrupted. If your north walls come back dark that is `no_side` in the
report, and the fix is a front-facing type such as `sconce` rather than a
wider tolerance. Godot's flip bit mirrors a sprite whose type allows it.

THAT ORDER IS A CONVENTION, NOT A STANDARD. A sheet authored in Tilesetter
or bought from an asset pack has its own order, and a wrong order draws a
complete, confident, wrong-looking level. Check the first screenshot. If
`unmapped` in the result is non-empty, the sheet is missing shapes the level
needs and that field says which masks and how often - that is what to hand
an artist.

Re-running REPLACES the layers it wrote rather than adding more, so
iterating on `seed` leaves one Floor and one Walls, not eight.

godot_project: the directory holding project.godot.
scene/tileset: res:// paths, or paths relative to that directory.
tuning: the eight BSP partition knobs, all optional — min_leaf, min_room,
  margin, max_depth, corridor_width, room_fill, rooms, side_rooms. The
  same numbers `level_plan` takes, so preview there and pass the dict
  here. An unknown key is refused by name rather than ignored.
names: layer node names, {"floor": ..., "walls": ..., "props": ...};
  defaults Floor/Walls/Props.
```

## level_plan

```text
Lay out a room-and-corridor level and show it, WITHOUT touching a scene.

`room_fill` is the share of its BSP cell a room must take, and it is the
difference between a dungeon and a set of thin rooms with slabs between
them: at 0 a room is a uniform-random slice of its cell, so the rest of the
cell stays solid. `corridor_width` defaults to 2 because a one-cell passage
loses most of its width to the wall the tile art draws inside its own edge.

BSP: cut the map in two until a piece holds one room, put a room in each
piece, then join the two halves of every cut on the way back up. That join
is the guarantee - it builds a spanning tree over the rooms, so every room
is reachable from every other by construction rather than by luck. The
result says `connected` and it is checked with a flood fill, not asserted.

Read the `ascii` field. It is the fastest way to see that a level is one big
room, or two halves joined by nothing, and it costs no engine and no
screenshot. Iterate on `seed` here until the shape is right, THEN call
level_generate with the same numbers to write it.

Knobs that actually change the shape:
  seed            same seed, same level, forever.
  min_leaf        bigger -> fewer, larger rooms. Must be at least
                  min_room + 2*margin or nothing fits and it says so.
  max_depth       caps how many times the map is cut, so it caps room count.
  corridor_width  1 reads as a dungeon, 2+ as a complex.
  margin          gap between a room and its leaf's edge; 0 lets neighbouring
                  rooms fuse into one L-shaped cavity.
```

## level_reskin

```text
RE-BUILD AN EXISTING LEVEL'S LAYOUT against a different tileset.

The layout is the expensive part and the art is not. A floor somebody
designed by hand — where the rooms are, which cells are corridor, where
the walls run — is worth keeping when the tile set under it changes, and
re-drawing it by hand in the editor is how a re-skin never happens.

So this reads the CELL SETS out of a scene's TileMapLayers and emits them
again against a new tileset: the floor re-autotiled from its own shape, so
every cell gets the edge its neighbours imply rather than the flat tile it
had, and the walls placed as whatever the new set uses for a wall — in an
isometric project that is the raised BLOCK, which is what turns a flat
wall layer into a room you can see the inside of.

It writes a NEW scene by default (`out_scene`, or `<scene>_reskin.tscn`).
The source scene is never modified: a level carries props, scripts,
spawns and quest wiring that this tool knows nothing about, and quietly
rewriting the layers under them is not a re-skin, it is a demolition.

`sunken` is "x,y,w,h" — a region that stays on the base plane while
everything else rises one level, which is how you get a BASEMENT out of a
generator that only knows how to raise things. The rim of the drop is
ramped wherever the two heights actually touch, and because the walls of
a designed floor already separate its rooms, the only places they touch
are its doorways. Reachability is then checked the same way the
side-scroller checks its jumps: if a walker cannot get from the high
ground into the hole, that is refused rather than rendered.

`doors` is "x,y x,y ..." — cells the WALL layer holds that are actually
openings. A designed floor does not have to leave gaps in its wall layer
to have doorways: downsizing's tutorial floor draws a door tile inside
the wall run and records the opening in its level data, which is a scene
reader's blind spot. Without them the walkable set comes apart into one
component per room — measured here, eighteen of them — and any question
about reaching anything is answered wrongly rather than refused. Given
them, the cells stop being walls and become floor, which is what a
doorway looks like when the wall is a solid block.

Returns the cell counts it moved and the masks the new set could not
answer, which is the list to hand an artist.
```

## local_status

```text
Every generator running on THIS machine: 2D, 3D, and what each one needs.

The read half of the local setup surface. ``image_status`` answers for the
2D leg only and folds local in beside the hosted providers; this is the
whole local registry - the ComfyUI image path, every local image-to-3D
backend - each with a STAGE rather than a boolean, because "not set up",
"set up but nothing is running" and "running but the workflow file is gone"
are three different problems with three different fixes and a caller told
only "unavailable" cannot pick one.

READ ONLY, AND DELIBERATELY WITHOUT A WRITE COUNTERPART. Configuring a local
runtime writes the project's .env and repoints what every subsequent
generation runs; the dashboard gates that on a human (see
``bgate_ui/routes/localsetup.py``) and an MCP tool that did it would be that
gate with a hole in it. The same reason there is no ``set_api_key`` tool.
Report the reason to the human instead - every row here carries the
adapter's own sentence, which is what to tell them.

Nothing here starts anything either. Builders Gate talks to software the
user runs; it does not run it.
```

## music_generate

```text
Generate MUSIC with Suno through kie.ai. Costs real credits.

ONE REQUEST RETURNS SEVERAL TRACKS - Suno generates variations, typically
two, and no page of the reference commits to a number. So this returns a
BATCH OF CANDIDATES, not a file: every take is downloaded and registered as
an artifact revision under one logical name, exactly as a batch of candidate
images is. Audition them (music_candidates), then music_keep ONE - keeping
installs it under the engine project and approves the revision; the rest get
music_discard. Nothing reaches the game until a human keeps it.

instrumental defaults to TRUE: background music with a vocalist singing over
the dialogue is the wrong asset almost every time. Pass False for a title
song or a diegetic track - and only then does vocal_gender ('m'/'f') apply.

custom=False (simple mode) takes a 500-character DESCRIPTION and Suno writes
everything. custom=True takes lyrics up to 3,000-5,000 characters depending
on the model, plus `style` and `title` - which are refused in simple mode
rather than silently dropped. See music_options for the exact ceilings.

duration is V5_5 ONLY (10-360s). Every other model would be charged for its
default length while ignoring the request, so it is refused here.

Candidates land in .bgate_out/audio/<name>/ and are DOWNLOADED inside this
call, never linked: kie serves its own copies for fourteen days only.

WHAT IT COST MAY BE UNKNOWN. The Suno record carries no creditsConsumed, so
the charge is measured as the account-balance delta around the call and the
result says which number it is (`credits_source`). With no rate configured
(BGATE_KIE_USD_PER_CREDIT) the run is reported UNPRICED and `accounted` is
false - it is never reported as $0.00, which reads as free.

Runs for one to three minutes. This blocks for the whole batch.
```

## music_install

```text
Put an ALREADY-APPROVED take where the game can load it. The repair verb.

music_keep installs and approves together, which assumes a take is a
candidate until a human keeps it. On a project whose approval gate is off
(`gate.mode = none`, or `art.auto_approve`) that assumption is false:
artifacts.register approves each take as it is filed, so there is no
candidate, no keep, and - before this existed - no installed file. The row
said approved and the engine project had nothing.

Use it when music_candidates shows a kept track with `installed: false`, or
when an approved track's file has been deleted out of the engine project.
Idempotent, and it does not change review state - music_keep is what picks a
DIFFERENT take from an auto-approved batch.
```

## music_keep

```text
Keep one candidate: install it under the engine project and approve it.

THIS IS THE STEP THAT MAKES A TRACK REAL. The file is copied from the
scratch directory to game/assets/audio/music/ - inside the audio seat's
write lane, inside the Godot project, and visible to both the audio library
and the audio lab's mixer - and only then is the revision approved.

APPROVAL IS A HUMAN'S CALL and this does not get around that: it goes
through artifacts.review, which refuses an agent by name unless the project
has deliberately turned its approval gate off. If you are an agent and this
refuses you, say which candidate you would keep and why; do not look for
another route to the same write.
```

## music_recover

```text
Download and register the tracks of a task ALREADY PAID FOR. Costs nothing.

music_generate submits, waits, downloads and files in one blocking call,
so anything that goes wrong after the submit - a timeout, a dropped
connection, a cancel, a CDN refusing the download - leaves a task id, a
charge, and no files. kie holds the audio for FOURTEEN DAYS from generation.
This collects it.

Not hypothetical: kie's file host answered this product's downloads with a
403 (Cloudflare bot integrity, not auth) for every generation it made, so
every batch rendered, was billed, and was thrown away at the last step.

IDEMPOTENT. Takes are matched against the Suno track ids already registered,
so running this twice downloads nothing twice and files nothing twice; the
result reports how many were `skipped`.

NO COST IS CLAIMED against this call - the charge happened at submit time,
possibly days ago, and a balance delta measured now would be fiction.
```

## music_stuck_tracks

```text
Music generations that were PAID FOR and never collected. Finds money.

The music twin of cinematic_stuck_shots, and it exists for the same reason:
a batch is charged at SUBMIT, and everything after that - the poll loop, the
download, the absorb - can die without the charge going anywhere. Until the
task id was persisted before the provider call, a crash mid-poll lost the
only handle to work you had already bought, and nothing anywhere noticed.

Run it after any crash, kill or dashboard restart. `recoverable` is the list
worth acting on: those are finished generations sitting on the provider that
music_recover can still collect - but only inside the provider's
retention window, which the result names. `poll=False` answers from local
tickets alone and reaches no provider, which is the right call when you only
want to know whether anything is outstanding.
```

## not_building_add

```text
Write down something this project is deliberately NOT building.

Human sessions only, and that restriction is the point of the list rather
than an obstacle to it: a refusal is read as binding by every agent that
lists it and has no acceptance test anyone could check it against, so an
agent-written no is an unreviewable instruction to every future session. An
agent that wants to refuse something calls decision_add(state='open') saying
so, and a human turns it into a line here.

reason is mandatory. An unexplained no is re-proposed every few weeks by
somebody who cannot see what was wrong with it, and each re-proposal costs
the argument again.

tag is free-form and optional - 'scope', 'engine', 'v2', whatever this
project groups its refusals by.
```

## not_building_list

```text
What this project has said no to. CALL THIS BEFORE YOU FILE WORK.

An unsaid no gets built anyway - this tool is the reason the no is not
unsaid. Each row carries the thing refused and WHY, so a proposal that looks
obviously good to a session with no history can be checked against the
argument that already happened.

A refusal is not a permanent law; it is the current answer with its reason
attached. If the reason no longer holds, say so - do not build the thing and
hope nobody notices, and do not silently work around it either, because a
workaround for a deliberate no is the no getting built with extra steps.
```

## palette_pin

```text
PIN THE PROJECT PALETTE in the art bible - the fix for uneven pixel art.

Generated "pixel art" carries thousands of smeared colours per sheet, and
every sheet invents its own, which is why assets look mushy alone and
mismatched together. Pinning writes a LOCKED bible constraint listing the
palette; from then on every image_sprites sheet, item and vfx set is
conformed to exactly these colours, artdirection measures compliance on
every generation, and drift becomes unrepresentable rather than reviewable.

colors: explicit hex list ("#1a1c2c" or "1a1c2c"). Omitted: derived from
the pinned style refs (ref_pin kind="style") - Aseprite quantises them to
at most max_colors; without Aseprite, the refs' dominant colours are used.
Re-running replaces the pinned palette. 16-40 colours is the useful range:
fewer flattens faces, more stops being a palette.
```

## pending_decisions

```text
EVERYTHING WAITING ON A HUMAN, in one call. The gate's read path.

There was no way to ask this. ``asset_status`` lists candidates but exposes
no approval STATE, ``art_tournament_standings`` reports Elo from matches
already decided, and human approval was dashboard-only - so a director could
not see, surface or triage a queue of decisions blocking their own board.
Measured: five candidates generated in two minutes with an approval card each,
and nothing an agent could call would say so.

WHY THAT IS DANGEROUS AND NOT MERELY INCOMPLETE. A pending decision is a
blocking gate. Work stalls behind it and the heartbeat shows nothing, which
looks exactly like an agent quietly working - the same "silence is not
success" failure as a dead agent, arriving through a different door.

Three classes, because the floor has three:

  review      work items parked by the builder's gate. THE HARD BLOCK: the
              chain behind each one does not advance until a human approves.
  candidates  generated artifact revisions nobody has dispositioned. An
              agent may record a verdict (art_qa_verdict) but may not
              promote - that is the human's call, by design.
  questions   open ask_human questions, oldest first.

``gate`` names the mode that decides whether any of this is being asked for
at all. Under 'none' the board is not supposed to be stopping for a human;
a non-empty list under that mode is worth reporting rather than clicking
through.

WHAT THIS DOES NOT DO: approve anything. It cannot - the agent that made a
candidate is exactly who must not clear it. Hand the list to the human with
what each decision is blocking, and keep working on what it does not block.
```

## plan_status

```text
Coverage: what the game consists of vs what is built - THE completeness
read.

An empty queue is NOT a finished game: after a bad decomposition the two
are indistinguishable from the board alone, which is how six items died
superseded while the objective ranked last. This reads the game-plan
manifest (plan_row, written when a human deploys a brainstorm plan that
carries one) joined live against the board: spec / on_board / built /
lost per row, slice completeness, and `remaining` - the rows the board
does not currently hold. Anyone may read it; the director reads it before
declaring anything finished, and uses queue_add to put uncovered rows on
the board.
```

## project_select

```text
Resolve a Builders Gate project by registered name or path. DEPRECATED as
a mode switch - it no longer changes what later calls affect.

It used to latch the choice into a server-wide variable, which meant the
project a tool touched depended on who called project_select last. Now it
only ANSWERS: it verifies the project exists, registers it so it stays
discoverable, and hands back its absolute root. Feed that root to the
`project_dir` parameter that every tool carries (or export BGATE_ROOT before
spawning the server) - then the target of a call is written on the call.

Empty arg: report the root this session resolves to plus every known project.
Returns {active, known} or {active, project, use_project_dir, deprecated}.
```

## project_set_dimension

```text
Correct the project's 2d | 3d | 2d+3d record after the game changed shape.

``init`` writes this and ``adopt`` detects it; nothing could change it
afterwards. A 2D prototype that grew a 3D scene went on reporting
``dimension: "2d"`` in project_status indefinitely, and the only workaround
was re-running ``project_init``, which rewrites name, pitch and engine from
its own defaults - overwriting four fields to correct one.

Not cosmetic: the field steers scaffolding templates and the wording of seat
briefs, so a stale value aims the board at the wrong kind of game. Use
``2d+3d`` for the real mixed case (a 3D game with a 2D HUD, a prototype
mid-port) rather than picking whichever is closer.
```

## prop_generate

```text
GENERATE THE PROPS FOR A LEVEL - art, cleanup, atlas and manifest.

ONE CALL. You do not pack an atlas, you do not work out texture origins,
you do not build an atlas string, and you do not have to remember which
cleanup steps exist. Pass a name and the types you want; hand the manifest
this returns to `level_generate(prop_manifest=...)` and the props are in
the level.

THAT IS WHY THIS TOOL EXISTS. The first prop set was made by a hand-written
script, and the script silently dropped the palette conform and the
defringe: 32-pixel sprites carrying 600 colours, two thirds of them off the
pinned palette, with feathered edges. Nobody chose that. There was no
pipeline for the decision to live in, the way `animation_generate` is the
pipeline for a character cycle - so the steps were skipped by omission.

THE CHAIN, all of it mandatory:
  * the project's VIEW decides the camera per prop mount (`game_view_get`)
  * `props.art_spec` decides the canvas, the ground anchor and how many
    DRAWINGS each type needs - a wall mount needs one per facing, because
    the engine mirrors a sprite but NOT its texture_origin
  * kie draws the sprite. RD is for motion and never for originating a look
  * the background is keyed client-side, the sprite is stepped down in
    halves to the contract box, its alpha is hardened to binary, and it is
    conformed to the pinned palette
  * the atlas packs on 2x2 slots so no spanning tile can overlap another,
    which Godot answers by silently dropping the tile
  * a MANIFEST is written beside the atlas with every coordinate, size,
    facing and animation

`types` is a comma list, "" for the default set. `install=False` leaves
everything in `.bgate_out/props/` for review.
```

## provider_status

```text
Which paid providers are LIVE - keyed, and funded where they will say.

READ THIS BEFORE CONCLUDING A PIPELINE IS CLOSED. One account answering
"no credit" is a routing event, not an outage: the same job usually has a
second keyed provider, and the observed failure is an agent that tried
one key, read $0, and hand-rolled the asset while another account sat
funded. Every billing-shaped tool failure also carries this board in its
`route` field, so you should rarely need to guess.

Per provider: `keyed` (offline truth), `balance` (a NUMBER only where the
provider exposes one - kie and Retro Diffusion do; openai never says, and
krea's API balance shows only as a 402 at call time - so None means
UNKNOWN and the provider is still routable), and the adapter's own
`reason` when unkeyed.

Pass `capability` ("image" | "animate" | "three_d" | "music" | "video")
to also get `pick`: the provider that job should route to right now,
honouring the craft division (sprites/stills mint with kie; motion is
Retro Diffusion; music/video are kie). `fresh=true` re-probes past the
2-minute cache - the right call after the human tops an account up.

Keys never appear here, and there is no tool that writes one - a human
sets keys (bgate key / the Generators panel), deliberately.
```

## quest_add

```text
Write a quest and its ordered steps.

steps is a list of {text, done_when, optional?}:

  text       what the player does.
  done_when  MANDATORY - the observable that closes the step. "the wizard's
             signed form is in the player's inventory", not "talk to the
             wizard". Without it nothing can finish the step: not the
             engine, which has nothing to test, and not the player, who
             cannot tell what counted.
  optional   a step that does not gate completion. If EVERY step is
             optional the quest can never be finished, and the verdict on
             the returned row says so.

giver is a lore entity slug or name - the quest hangs off the graph rather
than sitting beside it. A giver that names no entity is refused, because
that is either a typo or a character nobody wrote down. Omit it for a quest
that comes from the world rather than from somebody.

Steps go in with the quest in ONE call: a quest with no steps is one of the
three things the verdict refuses on, so a create-then-append API would make
the invalid state the normal first state of every quest.

The returned row carries `ok` and `problems` - the shape checks, each naming
its step.
```

## queue_add

```text
Queue work for a seat. Use when your work uncovers work that isn't yours.

``depends_on`` is an EXISTING item id this work must not start before. Pass
it whenever the new item reads or edits something another queued item is
about to produce.

PRIORITY IS NOT ORDER, and this parameter exists because that gap had no
workaround. Priority is a preference among things that are ALL ready - it
does not stop auto-deploy from starting both agents in the same tick, so the
one that needed the other's output writes against a file that does not exist,
reports done, and the damage surfaces two items later wearing someone else's
face. Only a dependency stops that.

Before this, order could only be expressed by ``queue_add_chain``, which
files a whole ordered group at once. Chains are strictly linear and cannot be
appended to, so filing a dependent FOLLOW-UP once a chain already existed had
no correct form at all - which is how a fidelity pass nearly got dispatched
into a running build. Use the chain when you are filing the group; use this
when the group is already on the board.

A dependency on an item that does not exist is refused rather than dropped:
an item silently waiting on nothing is indistinguishable from one that is
ready, and it would dispatch immediately - the exact failure being prevented.
```

## queue_add_chain

```text
File DEPENDENT work as one ordered chain instead of N loose items.

USE THIS WHENEVER THE SPLIT YOU JUST MADE HAS AN ORDER. The tell is a brief
that has to say "AFTER #41 lands" or "this needs the scene from the tech
item": if one agent must not start before another finishes, priority cannot
express it. Priority is a preference among things that are all ready; a chain
is what decides which are ready. Filed as separate items, both agents start
in the same auto-deploy tick and the second writes against a file that does
not exist yet - reports done, and the failure surfaces two items later
looking like something else.

``links`` is an ORDERED list of dicts, each taking queue_add's fields:
{"seat": ..., "title": ..., "brief": ..., "priority": ...}. Link N waits for
link N-1 to reach 'done' - approved, if this project runs an approval gate.
Chains are strictly linear; model a fan-out as separate chains that share a
first link, or as one link whose brief covers both halves.

A CHAIN CANNOT BE APPENDED TO. To hang one dependent item off work that is
already on the board - a follow-up you did not know about when you filed the
chain - use ``queue_add(..., depends_on=<item id>)`` instead of filing a
second chain that races the first.

WRITE EACH BRIEF AS IF ITS PREDECESSOR ALREADY LANDED, because it will have.
Name what it produced (the file, the function, the scene) rather than saying
"wait for it" - the waiting is now the board's job, not the brief's.

Returns {chain_id, items: [...]} in running order. Nothing dispatches until
`bgate serve` is up, exactly as with queue_add.
```

## queue_claim_next

```text
Claim the next READY item for YOUR seat and keep this session working.

THE PICKUP LOOP. A finished worker used to have one move - exit, and let
the board pay a fresh agent's entire briefing to start the next item. This
is the other move: claim the next item, complete your current one, and
continue in the same session with your context already paid for.

ORDER MATTERS: CLAIM FIRST, THEN queue_complete. The dashboard closes this
session shortly after your current item settles unless you already hold a
claim - a claim made after completing races that shutdown and loses.

The claim is atomic against the dashboard's own dispatcher: whoever loses
simply does not get the item, so a claim that returns a row is yours. It
honours the same holds autodeploy does (dependencies, human-only sources),
and it only ever claims for the seat you already hold - this is a loop,
not a way to change lanes. Your run's cost and runtime ceilings still come
from the ORIGINAL dispatch and bound the whole session; if the claimed
work will not fit under what remains, do not claim it.

Returns the claimed item (its brief is the task) or {empty: true}, which
means: queue_complete and finish - an empty board is a finished shift.
```

## queue_complete

```text
Close out a work item with an honest one-paragraph result.

failed=True when the work did not land - say why plainly; a false 'done'
poisons the queue's trustworthiness for everyone.

THE EYES GATE: a run that WROTE SCENES (.tscn) may not report 'done'
without a render to show for it. Either this run already took a
godot_screenshot (the usual case - you looked before claiming), or pass
`evidence` = the path to a render/screenshot you actually judged. The
refusal is a redirect, not a wall: it names the screenshot call and the
scenes you wrote. Exists because geometry stats, node counts and green
checks kept standing in for a picture with holes in it - every number
was true and the level was broken. failed=True never needs evidence,
and an honest 'failed' is always accepted.

WHAT "CLOSED" MEANS DEPENDS ON THE PROJECT'S APPROVAL GATE, and the returned
row says which happened. Under the agent gate the item goes to 'done' and a
QA agent is spawned to verify the claim; under the builder's gate it goes to
'review' and waits for the human - you are finished either way, but anything
chained behind it does not start until it reaches 'done'. Do not "fix" a
'review' status by re-reporting: it is the gate working.

`next_approach` — FAILURES ONLY, AND NOT A SUBSTITUTE FOR TRYING IT.

Two failures wear the same word. BLOCKED — a missing key, a credit block, an
asset that does not exist, a lane this seat cannot write to — fails
identically however many times it runs; fail it fast, leave next_approach
empty, and the item goes to a human on the first round. OUT OF IDEAS is the
other kind: iterative work that NARROWED the problem and ran out of turns.

Naming the one concrete thing you would try next buys the item exactly one
automatic round beyond the normal cap, and that round's brief OPENS with your
sentence instead of burying it under the post-mortem. It is a bonus, not a
bypass: worth one round however many times it is named, and qa.max_rounds
still ends the item.

It does not buy you out of the work. If the thing you are about to name is in
your lane and you can afford it, RUN IT BEFORE YOU CLOSE — handing the next
agent a suggestion you could have executed yourself pays for a whole cold
start (a fresh session, a re-read of every file you already hold, a re-run of
the probes you already paid for) to arrive where you were already standing.
MEASURED: a rig repair closed 'failed' at three rounds having gone knee
weight-bleed → ankle non-manifold damage → that patch cleaned to zero
non-manifold faces, seam remaining, and handed a human a diagnosis instead of
an asset while holding a cheaper approach it had just written down.

`premise_refuted` — THE BRIEF CONTAINED A MEASURED CLAIM THAT IS NOT TRUE.
Pass {"claim": ..., "measured": ..., "did_instead": ...} and it becomes a
structured outcome on the board rather than a paragraph nobody can search.

This is the single most valuable thing agents did in the benchmark, and it
survived only as prose. Three times a brief carried a false measured
premise, twice written by the director: a "0.14 m guard clearance" that was
a task marker 2.75 m from the real target; a "blind spot" in a vision cone
that used the flattened horizontal bearing and therefore could not exist;
an inherited PASS from a previous attempt that turned out to be a driver
bug. In each case the agent MEASURED, refused to make the change it was
asked for, and fixed the real thing. Each one stopped a wrong fix shipping.

All three fields are required and the middle one is the point: a refutation
without a measurement is a disagreement. Recording one does not close the
item on its own - report the outcome of what you actually did as usual.
```

## queue_cut_dependency

```text
Release an item from a predecessor that will never land. THE REPAIR VERB.

Only 'done' satisfies a dependency, so a CANCELLED predecessor blocks its
successors forever - the board's one state with no exit. Before this the
only escape was deleting and re-filing the work, losing its brief, its
round count and everything the harness observed it write.

Use it when the predecessor was cut, superseded, or turned out to be
unnecessary - NOT to jump a queue: the item you release starts writing
against whatever the predecessor was supposed to produce, so if that
output is genuinely still needed, this is how a run writes against a file
that does not exist. The cut is recorded with your identity, not deleted.

queue_get shows what an item waits on; the refusal message on a blocked
dispatch names it too.
```

## queue_list

```text
The work queue. status: queued | dispatched | done | failed.

WORK-ITEM IDs ARE CREATION IDENTIFIERS, NOT EXECUTION ORDER. Observed, and
it read as a broken scheduler:

    #42 enlarge rooms       done
    #45 swap in furniture   running
    #43 rebuild routes      queued

The real order was #42 -> #45 -> #43, and it was correct: #43 was filed
after #42, then #45 was inserted between them because the route
measurements had to wait for real furniture dimensions. The dependency
engine did the right thing; the presentation made it look like a skip, and
an operator who believes the scheduler is broken starts working around it.

Ids are NEVER renumbered - they are in briefs, commit messages and people's
heads. What changed is that the order is stated instead of inferred:

  order="id"         (default) creation order, as before
  order="execution"  topological: predecessors first, ids untouched.
                     Each row carries `execution_position` and
                     `execution_state` (ready | running | waiting |
                     blocked | held | done | failed)

Every queued row also carries `waiting_on`, which now NAMES the blocker:
"WAITING ON #45 Swap in furniture" rather than "#43 QUEUED". `blocked`
means a predecessor will never land on its own; `waiting` means the board
is working.

BRIEFS ARE PREVIEWS and the list is PAGED. This used to answer with every
work item a project had ever had, brief text and all - on a real board that
is 150,000 characters, which does not fit in a tool result at all: the call
failed, the CLI spilled it to a file, and the agent spent its next two turns
grepping a dump of its own queue instead of doing the work. A board is a
list of titles you scan; the one brief you actually need comes from
queue_get(item_id).

Pass full=True only when you genuinely need brief text for several items at
once, and keep the limit small when you do.
```

## queue_reopen

```text
Send a done/failed item back to 'queued' for another round.

The QA gate's FAIL path: reason is the ranked nitpick list (specific
problems + fixes). It is APPENDED to the item's brief so the next
dispatched agent reads exactly what to fix, and recorded as the result.

ROUTED THROUGH queue.reopen, and the difference is not cosmetic. This tool
used to re-queue directly, which left ``attempts`` at zero forever - and
``attempts`` is the round counter the QA gate's max-rounds cap reads, so
the fail/reopen loop the cap exists to stop could never trip it: an
unbounded money pump wearing the gate's own uniform. queue.reopen counts
the round AND carries the harness's record of what the last attempt
already wrote into the new brief, so a fix round continues instead of
regenerating.
```

## queue_update

```text
Edit an existing work item in place (title/brief/seat/priority).

For enriching a ticket without re-filing it - e.g. rewriting a transcript-
era brief to add the frames, timestamps, and telemetry you saw while
watching the recording. Only the fields you pass change; status and lineage
stay put. Pass the full new brief text (this replaces, it does not append).

THIS DOES NOT REACH A RUNNING AGENT. A dispatched agent was handed its
brief at spawn; rewriting the row afterwards changes what the NEXT reader
sees and nothing about what the agent is doing right now. That was easy to
misread as a mid-run correction - it looks like one, it returns ok, and the
agent carries on doing the thing you just edited out.

So a brief change on a DISPATCHED item is refused unless you say what you
mean:

  steer_running=False  (default) - refused, with the agent_steer call to
                       make instead
  steer_running=True   - the row is updated AND the change is delivered to
                       the running agent as a steer

Every result carries `live_delivered`, so no caller ever has to infer
whether a change reached anybody.
```

## room_review

```text
Review a WHOLE ROOM against a full-room screenshot.

Not the asset, not a crop, not a contact sheet: the room. A cropped shot is
refused rather than accepted with a caveat, because accepting cropped
evidence is how a level of empty rectangles with the furniture shoved
against the walls passed art QA on every individual prop.

Alongside your judgement it MEASURES the scene tree and reports: empty
floor, perimeter hugging, prop scale spread, whether any region holds the
eye, and the lanes between obstacles. A pass is refused while any measured
finding still stands — answer them one at a time with room_override, or
fail the room. `bounds` is [x0,y0,x1,y1] when the room is larger than what
is placed in it.
```

## scale_contract_set

```text
Declare the REFERENCE SCALE every asset is measured against.

Player height and tile size already existed; nothing fixed how big a door,
a desk, a mug, a HUD icon or an enemy should be, so each was sized against
whatever the generator felt like and reviewed on a contact sheet — the one
presentation that cannot show a scale error, because it draws everything
in the same box.

`player_height_px` is the unit. `classes` overrides the default bands,
which are multiples of that height: {"door": {"low": 1.05, "high": 1.5}}.
The classes are prop, furniture, door, ui and enemy; every one of them has
a band, because "no expectation" is how a mug ends up chair-sized.
```

## scale_record_3d

```text
Record a 3D asset's ENGINE-MEASURED scale against the contract.

THE AFFIRMATIVE HALF scale_check's 3D refusal left missing. scale_check
correctly refuses to measure a mesh (or any world-space asset on a 3D
project) in pixels, and its gate row says "measure with
godot_inspect_resource and compare" — but nothing RECORDED that
comparison, so a 3D asset could never actually CLEAR the scale row; the
only exit was a director retracting it by hand. That is the
unclearable-row failure, one layer up.

Pass the numbers the ENGINE gave you — godot_inspect_resource's
size_check.longest_axis_m, and the vertical extent as height_m. Never a
pixel count. Grades against the same class band the 2D path uses
(players = metres / player_height_m), records under the same key the
release gate reads, and retracts the standing "no measurement" finding
when it passes. A failing measurement blocks exactly as a 2D one does.
```

## scene_outline

```text
Read a scene's node tree - paths, types, roles, scripts, resources.

THE READ THAT MAKES THE EDITS SAFE. Every other tool here addresses nodes by
PATH ("Characters/Desk_12"), and this is where a path comes from. Guessing
one costs a failed call; reading one costs nothing.

FILTER BEFORE YOU LOOK. A hand-authored scene has thirty nodes and a baked
floor plate has fifteen hundred - dumping that whole tree would bury the
task in furniture. `match` is a substring of the node name or path, `role`
is one of the roles the builder groups by (character, prop, visual, ui,
collision, layer, camera, audio, controller, marker, instance), `parent`
returns only what hangs under that node. `total` always reports the true
count so a truncated answer says so.

`properties` is off by default: property maps are the bulkiest part of a
node and are only wanted once you know which node you mean.
```

## scene_set_property

```text
Set one property on one node - position, z_index, visible, scale, a flag.

THIS IS THE MOVE TOOL. "Put the desk two cells left" is this call with
key="position". Vector and colour values are Godot literals passed as
strings - "Vector2(320, 96)", "Color(1, 0.5, 0, 1)" - while numbers, bools
and strings pass through as themselves.

`clear=True` removes the property instead of setting it, which is how a node
goes back to the class default rather than to a hardcoded copy of it.

ON A GENERATED SCENE THIS IS THE WRONG FILE. If the .tscn header says it is
bake output, the generator's input is the authority and your write survives
exactly until the next bake. Read the top of the file before moving anything
in it.
```

## scene_wire

```text
Put an asset into a scene as a new node, wired correctly.

The node type comes from the FILE, not from you: a .png becomes a Sprite2D,
a SpriteFrames .tres an AnimatedSprite2D, a .tscn an instance. `node_type`
overrides that when the default is wrong (a background .png that wants to be
a TextureRect), and is otherwise better left alone.

What this does that editing the text does not: allocates a non-colliding
ext_resource id, reuses the existing one if the scene already references the
file, bumps load_steps, and uniquifies the node name against its siblings - the four things a hand-written block gets wrong, three of which the engine
reports as something else entirely.

A .gd is not an asset here; a script attaches to a node that already exists,
which is scene_attach_script.
```

## seat_can_write

```text
May this seat write this path? Check BEFORE editing outside your obvious lane.

Two gates, both must pass: the path must be inside the seat's write lanes,
and the file must not be locked by another seat - being in-lane does not
excuse stomping a locked binary. Fails closed for unknown/disabled seats.

`allowed: false` DOES NOT ALWAYS MEAN THE WRITE WILL BE REFUSED, and that
gap read as a bug: an agent edited out of lane, the Edit LANDED, and then
this tool said no. Enforcement is a PreToolUse hook, so it does run before
the mutation - but a seated worker's lane gate defaults to `warn`, where the
write lands and the human gets a non-blocking note. That default is
deliberate (a hard lane gate refused whole source trees on adopted repos and
turned refusals into dead agents instead of routed work); what was missing
was any way to tell the ORACLE'S answer from the ENFORCED one.

So the result now carries both:

  allowed         the lane/lock verdict - unchanged
  enforced        whether a write that fails it is actually BLOCKED
  lane_mode       collide (silent) | warn (lands, human told) | block
  aegis_mode      the project BOUNDARY, which is block by default and is
                  what stopped a shell redirect out of the project
  what_happens    the sentence for this exact combination

A lock or lease collision blocks in EVERY mode, because that is a fact
about two live runs rather than a rule about one.
```

## seat_configure

```text
Override a seat for this project: change its mission or its look on the
studio floor, or (human only) its write lanes and enabled flag.

`mission` is prose about what a seat should focus on and any caller may
rewrite it. `write_globs` and `enabled` are PERMISSIONS, and an agent
calling this is refused: write_globs=['**'] is a seat granting itself the
whole repo, and enabled=false is a seat switching off the QA that would
have caught it. A lane change that comes from a machine is not a lane
system, it is a suggestion. Ask the human to make the change in the
dashboard, or state the case in a work item and let them decide.

`persona` is how this seat LOOKS on the floor view, and it is merged key by
key rather than replacing what is stored - so changing one field keeps the
rest, and a call that does not mention it cannot wipe it:

  style    HOW THE SEAT CARRIES ITSELF, appended to the dispatch prompt of
           every agent spawned into this seat. Manner only: the prompt tells
           the agent in as many words that it changes tone and not the job,
           and that the mission wins wherever the two disagree.
  name     what the seat goes by on the floor's nameplate
  lines    this seat's own lounge banter, replacing the shared pool
  cast     which character sprite walks around the room ("art", "tech",
           ... , or "generic" for an invented seat with no art)
  surface  the room's floor: carpet | tile | wood | vinyl | concrete
  vibe     the one word under the nameplate, in the studio's own language

It carries no permissions, so any caller may set it: the worst a wrong value
does is give a room the wrong carpet.

Returns the merged seat {role, title, mission, write_globs, enabled,
persona}, or
{ok: false, error} - including on the permission refusal, which is a normal
result to read and route around, not a crash.
```

## sfx_generate

```text
Synthesize a game sound effect into the project. No key, no provider.

kind        sfx_kinds() lists them: blip, pickup, jump, laser, explosion,
            hit, powerup, sweep (plus aliases - "coin", "shoot", "thud").
name        becomes <name>.wav in the audio seat's lane.
base_hz     scales every pitch in the preset - a bigger gun, not a
            different sound. 0 keeps the preset's own.
duration_s  scales every time in the preset. 0 keeps its nominal length.
bits        3-8 bit-crushes it for the retro sound; 0 leaves it clean.

WRITES TWO FILES, AND THAT IS THE POINT. `<name>.synth.json` lands beside
the wav carrying the complete parametric recipe - waves, sweeps, ADSR,
filter, seed - because the audio house rule requires it and because a .wav
whose knobs are lost cannot be nudged by anyone, ever. sfx_rerender rebuilds
the identical bytes from that sidecar alone.

Deterministic: the same kind and name give the same file every time, so
regenerating is a no-op rather than a diff nobody asked for. Pass an
explicit seed to get a different roll of the noise.
```

## sidescroll_generate

```text
GENERATE A SIDE-SCROLLING LEVEL and write it into a scene.

The platformer counterpart of `level_generate`, and a separate tool because
it is a separate problem. `level_generate` partitions a SPACE into rooms and
guarantees the floor is one connected region. Under gravity that guarantee
is meaningless — you cannot walk upward — so this builds a SEQUENCE of
segments left to right and guarantees something else entirely: that the
goal can be REACHED, by a character with this exact jump.

THE JUMP IS AN INPUT, not a detail. `jump` is one character's physics -
{"run": ..., "jump_speed": ..., "gravity": ..., "body_cells": ...}, the
first three in CELLS PER SECOND - and every segment sizes itself from what
they allow: a pit is never wider than this character clears, a pipe never
taller than it rises. An unclearable gap is unrepresentable rather than
generated and rejected. One dict rather than four loose floats because
they describe ONE thing, and four separate arguments invite three of them
being right. An unknown key is refused by name rather than ignored.

`player_scene` IS THE WAY TO PASS THEM. Point it at the player's .tscn and
the tunables are read from the scene itself — its script's @export
defaults, overridden by anything the scene sets — converted to cells by
the tileset's own tile size, and the player is INSTANCED AT SPAWN in the
written scene. `jump` is then ignored, because two sources of the same
number is the drift this parameter closes: a level built for one jump and
played with another is the failure the whole parameterisation exists to
prevent. `fall_multiplier` is honoured by modelling with the fall gravity,
so the error runs only in the safe direction. Without `player_scene` the
`jump` numbers are trusted as given — then it is on you to keep the player
scene agreeing with them.

IT REFUSES AN UNPLAYABLE LEVEL rather than reporting one. The checks are
`reachable` (the goal is in the flood fill of jump arcs from spawn),
`clearance` (the body fits where it must pass), `softlock` (nowhere you can
land and never leave) and `stranded` (no platform outside its own jump).
A finding here is a bug to report, not a difficulty dial.

THE TILESET DESCRIBES ITSELF. Where the platform tiles live comes off the
`<tileset>.tiles.json` sidecar - written by `tileset_generate` for a set it
made, or by `tileset_describe` once for a hand-built one. This tool takes
no atlas coordinates; without that file it refuses rather than guessing.

`segments` is a comma list from flat, pit, stair, hop, blocks, pipe — "" for
all of them. `prop_manifest` is what `prop_generate` wrote; pass it and the
props are placed and drawn, and the types come from the manifest too.
`names` renames the layer nodes, {"solid": ..., "props": ...}.

Returns the ASCII map, which is the cheapest way to see a level before
anything is spent on art for it.
```

## skin_dominance

```text
Is each vertex driven by a bone that is anywhere NEAR it - no Blender needed.

A FIFTH RIG PROOF, and the one that catches a character which tears when
it animates while every other gate reports it clean. Found on a shipped
cat whose walk and run were reported as tearing, with the idle tail wag
the only motion that read as smooth. At that moment:

  blender_rig            passed - `unweighted` was 0, every vertex had weight
  weights summed to 1.0  on every single vertex
  blender_weights        passed - the guilty bone's paint was ONE connected
                         patch; it just ran too far down the legs, and a
                         patch that is too big is not a patch that split
  blender_flex           passed - its six poses did not open the seam far
                         enough to trip the volume/pinch bounds
  blender_template_dev.  passed - the SKELETON was correct. Names, lengths
                         and parenting were all fine. Only the paint was wrong

The defect: 42% of the vertices in the lower third of the model - the legs
and paws - had their dominant weight on `spine`, `hips`, `chest` or `neck`.
The worst sat at y=0.005, ON THE FLOOR, driven by a bone 0.15 m up inside
the body. That geometry cannot follow the leg it belongs to, so the leg
stretches away from the body as soon as the leg swings. It is invisible in
bind pose, which is what every stand-up photograph in this pipeline
captures, and invisible to every check above.

Measures, per vertex, the distance to the BONE SEGMENT that dominates it
against the distance to the nearest deform bone segment available. Segments
rather than joint origins because a vertex halfway down a thigh is far from
both the hip and the knee. A ratio rather than a distance because a
tolerance in metres would need retuning per asset, which means it would not
get run.

ONLY DEFORM BONES ARE CANDIDATES for "nearest" - a root or an IK target
skins nothing and is often parked at the origin, and comparing against one
inflated this check's own first run to a 6.91x false positive.

Defaults are set from two rigs measured in the same project, one known-good
and one known-bad:

                    median   p95    max    rigid
    good rig          1.00   1.06   1.56      9%
    the torn cat      1.00   2.32   3.76     57%

THE MEDIAN IS 1.00 FOR BOTH. Most vertices in a broken bind are painted
correctly; the defect lives entirely in the tail, so any average hides it.
That is why the verdict reads the maximum and the rigid share.

`flag_dead_bones` is off by default: every rig legitimately carries bones
that deform nothing, and the good rig above fails on exactly that when it
is on. They are always listed in the report as information.

`verdict.passed` False names the bones that reach too far and, separately,
a bind with no falloff at all. It is False rather than True when nothing
could be measured - an unrigged file refuses instead of passing empty.
```

## sprite_plan

```text
The key poses and timing for standard animations. FREE - spends nothing.

Call this BEFORE image_sprites. With no arguments it returns the catalogue:
every archetype, how many frames it generates, how many steps it plays, and
one line on why it is built that way. With `archetypes=["idle","walk4",
"attack"]` it returns the exact pose list and timing that run would use, plus
what it would cost - so the plan can be read, edited and priced before any of
it is bought.

WHY THIS EXISTS. image_sprites will animate whatever poses it is handed, and
the expensive failure is not a broken sheet - it is a sheet that assembles
perfectly, passes the identity gate, holds its palette, and animates like
nothing alive. Four frames named walk/0..walk/3 and described as "walking,
left foot forward" / "walking" / "walking, right foot forward" / "walking"
is a legal, costly, useless animation and not one existing gate rejects it.

Animation has had the answer since the 1930s. A walk is CONTACT, DOWN,
PASSING, UP, once per leg - the body rises and falls twice per cycle and that
bob is what a walk IS; four frames with no height change is a character
sliding along the floor. An attack is ANTICIPATION, CONTACT, FOLLOW-THROUGH,
RECOVER, and its impact frame is HELD while its wind-up is rushed, because
that contrast is the feeling of a hit landing. Godot 4 has carried per-frame
durations all along and this pipeline emitted a flat 1.0 for every frame ever
made, which is why generated attacks read as a slideshow of an attack.

`view` ("side view, facing right") is prepended to every description, so the
camera convention is stated once rather than drifting between eight prose
strings. Feed the returned `poses` and `archetypes` straight to
image_sprites, or edit them first - this is a starting point with reasons
attached, not a rule.
```

## sprite_sheet_check

```text
LOOK AT A GENERATED POSE ROW OR CHARACTER SHEET BEFORE SPENDING ANYTHING
ELSE ON IT. Free - calls no model, buys nothing, changes nothing.

CALL THIS THE MOMENT A MULTI-FIGURE IMAGE COMES BACK, and call it before
keying, before slicing, before assembling, and before generating the next
row. Everything downstream of this point either hides these faults or
multiplies them, and all of it costs money.

THE PROBLEM IT EXISTS FOR. An image model asked for four figures on one
canvas does not draw four frames. It draws ONE picture, left to right, each
figure conditioned on the canvas so far - so every small error is inherited
by the next figure and added to. The result degrades ACROSS the row and, on a
stacked sheet, DOWN the page: the character grows, the feet leave the ground
line, a head yaws the wrong way, a necktie appears in row three and is still
there in row four. None of it is visible in any single frame, all of it is
obvious with a straight edge held against the image, and none of the existing
audits can see any of it - they run on frames that have already been sliced
and bottom-pinned into their own cells, by which point the evidence has been
destroyed rather than fixed.

WHAT COMES BACK. Named findings, each carrying its own fix: `foot_drift`,
`head_drift`, `size_drift`, `size_ramp`, `facing_flip`, `stray_ink`,
`empty_cell` within each row; `sheet_size_drift`, `sheet_size_ramp` and
`band_palette` across them. Plus - and this is the part to actually read - an
ANNOTATED COPY of the image with the ground line, the head line and each
figure's true feet and mass anchor drawn on it, returned as an image so you
can see what the numbers are talking about.

READ `size_ramp` AND `sheet_size_ramp` DIFFERENTLY FROM THE REST. Every other
finding says "re-roll that figure". Those two say the drift is MONOTONIC,
which means it compounds, which means re-rolling buys one better figure and
the next attempt does exactly the same thing. The fix is structural: stop
asking for a row, and generate each pose as its own image against ONE
approved reference - which is what `image_sprites` does, and why it exists.

`columns` and `rows` are the grid you asked the model for. `labels` names the
columns (pose names) and `row_labels` the rows (animation names); both are
only there so the findings read as "walk/2" instead of "row1/2".

Advisory, never a gate - a turnaround SHOULD flip its facing and a size chart
SHOULD ramp. It reports; you decide.
```

## sprite_sheet_slice

```text
Find every sprite on an IRREGULAR sheet and cut it out. Free and local.

sprite_sheet_check assumes the grid you asked for; this is for the sheet
that has no grid - a generator that scattered five sprites across one
canvas, a found atlas with uneven gutters. Connected-alpha analysis
(Better Slicer's auto mode, headless): each blob of ink becomes a box,
speckle under `min_px` pixels is ignored, `pad` grows each box, and the
result comes back in reading order - rows top to bottom, left to right
within a row - so slice N is frame N when you assemble a master from it.

With `out_dir` each box is also cropped to <stem>_NN.png there. Without,
nothing is written - call it bare first to see what the sheet holds, then
again with out_dir once the boxes look right.
```

## storyboard_auto

```text
Premise in, finished storyboard out, in ONE call. START HERE.

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
```

## storyboard_frame_attach

```text
Put an EXISTING image on a frame - one the author drew, shot, or pinned.
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
```

## storyboard_frame_generate

```text
Draw ONE storyboard frame. This is the only tool here that costs money,
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
```

## storyboard_plan

```text
Write or edit a storyboard by hand. SPENDS NOTHING.

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
```

## storyboard_promote

```text
Turn an approved board into a cutscene shot list ready to be bought.
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
```

## storyboard_write_script

```text
Turn a premise into a script and a beat-per-frame board. Costs a fraction
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
```

## tileset_describe

```text
TEACH THE LEVEL TOOLS A HAND-BUILT TILESET. Once per sheet, then never again.

A sheet bought from an asset pack or authored in Tilesetter knows where
its tiles are; nothing else does. That knowledge used to travel as ten
parameters on EVERY `level_generate` call - re-typed per level, per seat,
per session, and wrong in exactly one of them. The art seat holds the
sheet; gameplay and tech build the levels; a pasted string was the only
channel between them.

So it is written down instead. This produces the same
`<tileset>.tiles.json` sidecar `tileset_generate` writes for a set it made
itself, which is why `level_generate` now takes no atlas coordinates from
anybody: generated or hand-built, the sheet describes itself on disk.

LAYOUTS, for both floor and wall:
  blob47   8-bit mask, 47 tiles, row-major from (atlas_x, atlas_y),
           `columns` wide, masks ascending. Sides plus corners.
  grid16   4-bit mask, 16 tiles, same layout rule. Sides only - right for
           a wall one cell thick.
  solid    one tile everywhere, at (atlas_x, atlas_y). No autotiling.
  none     no layer of this kind at all (walls only).

THAT ORDER IS A CONVENTION, NOT A STANDARD, and a wrong one draws a
complete, confident, wrong-looking level rather than an error. Check the
first screenshot after describing a sheet, not the tenth.

variants/interior are optional and only affect floors: `interior` is the
plain fill tile "x,y" and `variants` is a space list of alternates
("3,0 4,0") scattered deterministically over it, which is what stops a
large room reading as one repeated square.

Re-describing a sheet needs `overwrite=True` - a set generated by
`tileset_generate` already has a manifest holding its full mask table, and
silently replacing it with a guess is how a working level starts drawing
at (0, 0).
```

## tileset_generate

```text
GENERATE A GODOT TILESET — the bridge the level pipeline was missing.

levelgen, autotile, tilemap and wire_tilemap have all been real for a
while and all blocked on the same thing: nothing here could WRITE a
TileSet, so level_generate refused unless a human had already built one in
the Godot editor. This makes one.

THE PROVIDER DIVISION IS A HOUSE RULE, not a convenience: kie draws every
static asset, Retro Diffusion only ever ANIMATES a sheet that already
exists. An earlier build of this tool generated on RD's tile styles and
its coverage varied 16/16 to 7/16 per roll — the kie path replaced it and
removed the roll entirely.

`prompt` names the FLOOR material, `void_prompt` what shows where there
is no floor (default: featureless darkness). kie paints each as a
material texture; the tile is cut from it and every mask tile is built
GEOMETRICALLY from those two — the same `normalise_edges` inset a
hand-made autotile set has by construction. Coverage is total by
construction, so the old partial-roll refusal has nothing to refuse; the
seam report and the engine load are the gates that remain.

`bits`: 8 for blob47 (the default), 4 for the 16-mask side set.

EIGHT IS THE DEFAULT BECAUSE FOUR HAS A VISIBLE DEFECT. A 4-bit mask cannot
say "floor to the north and east, void at the north-east corner", so at every
step in a room's outline there is no tile to draw and the shadow band along
the wall breaks — a row of notches down the level that reads as broken art.
The corner tiles are a nibble out of the tile and pure geometry, so
eight bits costs no extra image call and no extra money.
`install=False` lands everything in .bgate_out/tiles/ for review; True also
writes into the Godot project and LOADS IT IN THE ENGINE to prove it.

ISOMETRIC PROJECTS GET BLOCKS. The view's tiles are diamonds, its walls
are raised cells showing two camera-facing sides, and both come from one
primitive — a wall is a cell you may not enter and a terrace is one you
may. `wall_lift` is that height in pixels (0 = one tile height); the
resource carries the taller region and the texture_origin that lands the
block's top face exactly that far above the floor plane.

The atlas is also written as an Aseprite master — every AI-generated
sheet goes through the Aseprite cleanup, tilesets included.
```

## tileset_synth

```text
BUILD AN ISOMETRIC TILESET FROM THE PALETTE, with no image model at all.

The counterpart to `tileset_generate`, and the right tool for the
surfaces a building is mostly made of. A generated texture carries
structure at roughly tile scale, so cropping one onto a diamond grid
lays a visible lattice of motifs across the floor, and mirroring it to
hide the diamond seams trades that lattice for symmetry. The tiles a
real project ships are nearly featureless — near-black, a faint grain,
at most one soft panel seam — and that is arithmetic's job: per-pixel
noise cannot repeat, every value is a palette entry by construction, a
variant is a different SEED rather than a different crop, and the whole
set costs nothing and arrives in a second.

Reach for `tileset_generate` when a material's features are meant to be
read individually — terrazzo chips, a checkerboard lino, a poster wall.
Reach for this for carpet, concrete, vinyl, asphalt and every other
surface whose job is to be quiet.

`floors` and `walls` are semicolon lists of
``name=#rrggbb[,grain][,seam][,speck]`` — one atlas source each, so a
level generator can put a different surface in every room.
```

## traversal_prove

```text
DRIVE THE PLAYER and prove it arrives — settled, in the real volume.

FOUR OF SIX CLIMBING ROUTES PASSED AND WERE NOT TRAVERSABLE. The tests
measured vertical rise against jump height. None measured the horizontal
edge gap, the launch surface size, the landing pad size, the player's own
body width, or what the controller does when you hold the stick that way.

Then the gate written to catch that produced its own false green, which is
the sharpest defect in the whole benchmark: the driver accepted arrival on
ANY frame where the body was near the target - including mid-ballistic-arc
during a jump that MISSED. Requiring `is_on_floor` was not enough either: a
scripted mantle carries that flag from before it began, so the check passed
mid-interpolation while an AnimationPlayer was lerping the body through the
air.

So arrival here is three conditions, all required:

  IN THE VOLUME  inside `destination`, which must be the destination's OWN
                 Area2D/Area3D - the volume the GAME uses to know the
                 player is there. Not a marker plus a radius; a radius
                 around a point is what passed mid-jump.
  SETTLED        grounded AND not inside any scripted or interpolated move
  HELD           for `settle_frames` CONSECUTIVE frames. One frame is a
                 sample of a trajectory; N frames is a state.

YOUR CONTROLLER MUST SAY WHEN IT IS BUSY. Nothing outside a controller can
tell "standing on the ledge" from "being lerped by a mantle" - that is
exactly why the grounded check failed. Expose
`func is_in_scripted_move() -> bool` (or is_busy / is_traversal_busy /
is_animation_driving / is_scripted_move_active) returning true while any
scripted move owns the body. Pass `player_script` and this REFUSES a
controller without one rather than sampling it naively, because sampling it
naively is the bug.

`inputs` is the real input program through the real input map:
[{"action": "move_forward", "frames": 20}, {"action": "jump", "frames": 1}]

Bounded by construction: the run stops at a frame ceiling and prints a
heartbeat, because an unbounded wait-until-condition loop is
indistinguishable from a hang and three agents were killed after 25 minutes
of silence having written nothing.

Geometry (rise, gap, pad sizes, player bounds) is reported when available
as EXPLANATION. It is never the verdict.
```

## vfx_animate

```text
Turn ONE approved key frame into an effect ANIMATION, arithmetically.

    THE TOOL FOR PROJECTILE AND IMPACT VFX. Do NOT buy an effect animation as a
    grid of frames from an image model - that returns N INDEPENDENT DRAWINGS,
    not an animation, and the faults are not promptable away: a mug shatters and
    is intact again in frame 4, a cloud's palette pops mid-set, a "fading"
    effect ends at full opacity, a trail's frames point different ways. Identity
    over time is the one thing the model cannot hold and the one thing
    arithmetic gets free.

    THE WORKFLOW, in order:
      1. Generate ONE key frame - the effect at its PEAK, alone on the keyed
         backdrop, via image_generate/image_edit. One image, so you can LOOK at
         it and re-roll it cheaply.
      2. Call this. It derives every other frame from those pixels: frames
         before `peak` grow into it, frames after decay out of it. Frame 3 is
         provably the same art as frame 2 because it is made of it.
      3. Read `notes` in the result. They are findings, not decoration.

    Emits <name>_sheet.png + <name>_frames.tres through the same emitters the
    character pipeline uses, every frame registered to the cell centre - so the
    effect stacks on the projectile it belongs to without anyone computing an
    offset. `anchor` in the result is the pixel a runtime manifest should place.

    MOTIONS:
{motions}
    `peak` is which output frame the key frame IS. A burst drawn at its widest
    wants peak=1 of 4 - one frame snapping in, two coming apart.

    `overrides` tunes one motion's numbers (grow/expand/scatter/drift/fade/
    gravity/jitter/squash/chunk) without inventing a new one.

    COSTS NOTHING AND CALLS NO MODEL.
```

## blockout_generate

```text
Generate a MEASURED 3D graybox from a JSON spec, or from a level_plan result
(from_plan + cell_m): rooms and corridors in metres, doors (cut through ONE
shared wall, with a lintel), box props resting on their floor, a spawn, goal
volumes, a baked NavigationRegion3D, Sun and WorldEnvironment - every piece a
named node. Then it measures: walkable m2 and coverage per room AFTER props,
door and corridor widths against the agent (2r + 2 cells + 0.2), room and
door heights against the player, and a real NavigationServer path from the
spawn to every room. report.ok is false when a bar fails and the fix is
named. Overlapping rooms are refused - make one a corridor that ENDS at the
other's wall. The generator lands in scripts/tools/bgate_blockout_gen.gd
(editable). Block out and measure BEFORE generating a single prop.
```

## track_generate

```text
Generate a closed, drivable circuit scene from a JSON spec (driving games).

Walks sectors (straight | arc, with grade), SOLVES the closure back to the
start line as an arc-line-arc at closure.radius, bakes the road at 1.5 m and
emits a node-shaped scene: Road (+RoadBody on layers 1 and 6 - 6 is ROAD
ONLY, for wheel rays), RacingLine with target_speeds, Checkpoints, pitched
Barrier runs, Tunnel roof/walls/lamps + rock mass, a terrain heightfield
CLAMPED under the road corridor, a Sea plane, GridSlot markers, Sun and
WorldEnvironment, MultiMesh props. Then it MEASURES: per-sector minimum
radius, closure length/radius bars, lap-length bars, and a road-support
sweep. report.ok is false when a bar fails and the fix is named. The
generator lands in scripts/tools/bgate_track_gen.gd (editable).
```

## ui_concept

```text
Paint concept frames for the game's screens (title, main_menu, hud,
results, pause, options) conditioned on the project's pins, then derive a
palette and a Godot Theme from them: <out_dir>/ui_brief.md and
<out_dir>/theme_concept.tres. The gameplay seat lays Controls out AGAINST
these. Costs one image per screen. Never ship the scaffold theme when this
exists.
```

## sfx_prompt

```text
A REAL sound effect through kie (Suno sounds endpoint): engines, skids,
impacts, ambience, UI. Lands every take in audio/sfx/<name>_<n>.<ext> with
<name>.prompt.json beside it. Costs credits per call. sfx_generate stays for
retro/8-bit projects only.
```

## godot_scene_audit

```text
Audit a 3D scene for the defects that shipped green everywhere else. Call
it with scene="" to audit the scene the game BOOTS into - the one nobody
else names. STATIC: run/main_scene set and not the scaffold demo
(boot_is_scaffold); a mesh/shape/material sub_resource shared by several
nodes and assigned into by a script (shared_subresource_mutated - every
node gets the last writer's value); an instanced scene whose script resizes
its own embedded sub_resources without resource_local_to_scene
(instanced_subresource_mutated); a ConcavePolygonShape3D under a moving
body (trimesh_on_moving_body). IN-ENGINE, world space, after two physics
steps: every visible mesh has a collider on its body (no_collider) that
matches its bounds (collider_mismatch); every static/rigid body above the
floor rests on something within 3 cm or is touched by a neighbour
(floating / unsupported / sunk); every landing surface between 0.15 and
2.5 m up has `headroom` clear above a 5x5 grid of its top (no_headroom /
partial_headroom). ok is false on any error-level finding. Run it once per
project before any presentation gate.
```

## godot_export_verify

```text
Load one scene from the PROJECT and from the exported PCK and diff what the
engine built: node set, types, visibility, transforms, mesh and bounds,
materials per surface (albedo, texture, shader), collider class and size,
bone and animation counts, and every exported script variable - the
per-instance overrides a pck has been seen to drop (Corniche: six identical
cars where every editor screenshot showed six liveries). ok is false on any
difference; each diff names the node, the field and both values. Make the
pck with `godot --headless --path <project> --export-pack <preset> out.pck`
and run this after every export whose evidence came from an editor run.
```

## godot_export_probe

```text
Run a SceneTree script against an EXPORTED pck (res:// IS the pck), so scene
overrides, imports and resources are the shipped ones. Use it for the
release gate and after any delivery whose evidence came from an editor run.
headless=False opens a window so the script can save a viewport image.
```
