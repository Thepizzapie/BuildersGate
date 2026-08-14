# AI cinematic generation, and what a cutscene seat has to do

2026-08-10, extended 2026-08-11. Research notes plus the build they justify.

Can a seat generate cutscenes with the video models? Yes, and the model call is
the easy third of it. The three hard problems:

1. **A format wall in Godot** that silently swallows a finished cutscene.
2. **Anchoring**, which this repo had solved for sprites and had declared
   impossible for video. It was not.
3. **Everything after acquisition**: previs, coverage, sets, captions,
   transitions, sound, delivery.

## The working loop

```
cinematic_plan          # free. the only cheap point to argue with a sequence
cinematic_animatic      # free. cut the boards together and watch the EDIT
cinematic_generate_shot # one shot per call. watch it. re-roll or keep
cinematic_keep          # approves the shot (stays out of the engine project)
cinematic_continuity    # do these shots cut together? free, before you assemble
cinematic_assemble      # transitions, score under, captions timed
cinematic_keep          # the cut. THIS one transcodes into the engine project
cinematic_deliver       # the .tscn, the script, the skip, the finished signal
```

`cinematic_generate_shot` refuses to spend until an animatic exists and is newer
than the last edit to the shot list. `previs_ok=True` overrides. Sequences of
fewer than two shots are exempt.

## 1. The model landscape

Prices are per second of output unless noted and move constantly. Ratios matter
more than absolutes.

| Model | Rough cost | Notes |
|---|---|---|
| Veo 3.1 | ~$0.40/s | Only one shipping native audio in the video output. Region and account-tier gated through Vertex/Gemini. On kie it is not a market `createTask` model at all: it has its own `/api/v1/veo/generate` endpoint, so it needs an adapter path, not a table entry. |
| Kling 3.0 | ~$0.09-0.14/s | Cheapest of the tier. Motion consistency and subject tracking much improved over 2.0. |
| Sora 2 | ~$1/clip base, $3-5 Pro | The Sora 2 API sunsets 2026-09-24, which disqualifies it as a pipeline dependency. |
| Seedance 2.0 | unpublished (kie bills in credits) | The default (`kie.DEFAULT_VIDEO_MODEL = "seedance-2"`). |
| Runway Gen-4.5 | not surveyed | Best creative-control surface: video-to-video, motion controls. |

Seedance is the default because kie.ai was already wired with a verified schema,
on one key that also covers images and Suno music. Adding a provider is four
lines in `kie.MODELS` or one new adapter.

The load-bearing constraint every model shares:

> **No model generates more than about 15 seconds, and none holds together well
> past about 10.**

Seedance's documented range is 4-15s; workflow guides independently land on
5-10s per shot (`SHOT_SECONDS = (5, 10)`). That is what makes a cutscene a
pipeline rather than a call: a 90-second scene is ten separate paid generations
that must be planned, judged individually, and joined.

## 2. The format wall

**Godot 4 plays Ogg Theora and nothing else in core.** The only `VideoStream`
implementation shipped is `VideoStreamTheora`. H.264 and H.265 are
patent-encumbered and WebM support was removed in 4.0. Every video model on the
market returns H.264 in an `.mp4`.

The failure mode is what makes it a trap. An `.mp4` in a Godot project produces
**no import error**. It is not imported as a `VideoStream`, so `load()` returns
null, the `VideoStreamPlayer` stays empty, and the scene runs with a blank
rectangle where the cutscene was.

So keeping a shot transcodes it, with settings from Godot's own docs:

```
ffmpeg -i input.mp4 -q:v 6 -q:a 6 -g:v 64 output.ogv
```

- `-q:v 6` is the documented baseline on a 1-10 scale. Drop to 5 for 1440p+.
- `-g:v 64` is not optional. libtheora's default GOP is 12, which the engine docs
  call insufficient and which inflates a cutscene several times over. The
  sanctioned range is 64-512, trading seek time for size, and a cutscene is
  played start to finish.
- `-vf "scale=-1:720"` when the source is more than the cutscene needs.

Streaming from a URL is not supported, and Theora audio downmixes to stereo.

**ffmpeg on PATH is necessary and not sufficient.** libtheora and libvorbis are
optional build flags, and a build without them fails at the encode with
`Unknown encoder 'libtheora'`, after the sequence has been generated and paid
for. `cinematic.ffmpeg_status()` probes `-encoders`, not `shutil.which`, and
`generate_shot` refuses to spend until it passes.

## 3. Anchoring

An off-model sprite is one bad frame. An off-model cutscene is the player
watching a stranger deliver the story beat.

`kie.py` used to state that anchored generation through kie was unavailable:
every reference field is a URI and every pinned anchor here is a file on disk.
True of `/api/v1/jobs`, false of kie. kie serves a file upload API from a
different host, `POST https://kieai.redpandaai.co/api/file-base64-upload`, same
Bearer token, `{base64Data, uploadPath, fileName}` in, `downloadUrl` back, and
the generation endpoints accept the URL it mints.

- **Uploads die in 3 days**, against Suno's fourteen. No minted URL is cached or
  persisted as an anchor. The shot list stores *project paths* and uploads fresh
  at generation time.
- **It is still worse than Krea for a still**: a separate POST, a copy on someone
  else's disk, an expiry. `image_generate` keeps preferring Krea. The upload path
  exists because video has no alternative.

**Never chain shot N+1 off shot N's last frame.** Several commercial tools
advertise it. It is the chain the art seat banned after measuring it (a back view
turned front-facing by frame 3, a figure shrank from 932px to 821px across one
cycle), and for video the decay constant is worse: a video model's final frame is
already the most drifted image it produced, it is a lossy intermediate carrying
compression artefacts, and eight shots of it is eight generations of a photocopy.

Every shot anchors on **the same approved stills**, generated through
`image_generate` against the pinned character and approved before a frame of
video is bought. `last_frame` is for one deliberate match cut a human asked for.

## 4. Audio is off by default

Seedance generates its own audio unless told not to. For a game that is a trap:
the bed is **baked into the clip and cannot be separated**. It fights the score,
cannot be ducked under dialogue, cannot be re-mixed for localisation, and Godot
downmixes it to stereo anyway.

A cutscene here is picture. Score and voice come from the seat that owns them,
laid over the top, where they stay editable. `generate_audio=True` is the
override.

## 5. Its own seat, and when not to use it

`cinematic` is an eighth seat rather than a widening of `art` because an `.ogv`
does not merge any more than a `.blend` does and is fifty times the size, art's
lane is `game/assets/**`, every shot is a separate paid generation that cannot
be derived from anything, and a sequence is the most expensive thing this product
buys in one sitting.

**When not to use it.** A short beat that could be an in-engine scripted camera
move is almost always better as one: interactive, re-uses art already made, free
per iteration, localises, and cannot go off-model because it *is* the model.
Generated video earns its place where the engine cannot go: an establishing shot
of a place that was never built, a stylised prologue, a trailer.

## 6. Previs

Between planning a sequence and buying it there was nothing, so the first human
to see the edit saw it after every second had been paid for. By then the only
cheap change left is deleting shots.

`cinematic_animatic` cuts the storyboard panels together at their planned timings
into an `.mp4` under `design/cinematics/animatics/`, H.264 because it is watched
in a browser and never by the engine.

| Field | What to do with it |
|---|---|
| `average_shot_s` | Read first. Modern films sit at 4-6s. Under 4 is a montage nobody follows; over 6 is a slideshow. |
| `runtime_s` / `measured_s` | What the shot list adds up to, and what the file is. They disagree only when something is wrong. |
| `placeholders` | Beats with no still yet, rendered as slate cards held for their full duration. A gap in the edit is information. |
| `warnings` | Untimed shots, consecutive shots describing the same beat, pacing outside the window. Advisory. |

`source` is `"auto"` (the planned sequence if there is one, else the board),
`"sequence"` or `"board"`.

**Staleness is the half that matters.** An animatic built before three shots were
re-ordered is worse than none, because it is the reason somebody thinks the edit
has been checked. `previs_state` requires the reel to be newer than the
sequence's `updated_at`, which `plan()` stamps on every call.

## 7. Shot size and coverage

`shot_size` is a table key, separate from `camera`, which keeps the move and the
prose. Ten values: `establishing`, `wide`, `full`, `medium`, `medium_close`,
`close`, `extreme_close`, `over_shoulder`, `insert`, `cutaway`. Each carries a
prompt fragment and a `tight` flag; `TIGHT_SIZES` is derived from the flag so a
new entry cannot be added and forgotten by the warnings.

Sizes are not ranked 0..9. A cutaway is a different subject, an insert is an
object, an over-the-shoulder is a coverage choice. Ranking them would invent a
precision that then gets compared. The only question the warnings ask is whether
a shot gets close enough to stop showing the set.

Four advisory warnings, measured off two real sequences (nine shots: one close,
one medium, six wides, no over-the-shoulder, no reverse; and one sequence that
was push-in / push-in / push-in / static):

| Warning | Fires when |
|---|---|
| Unmeasurable | No shot states its size, so no coverage check can run. Silence would read as "well covered". |
| No tight coverage | Every shot is wide or medium. Wides are the hardest framing for the model to hold and the flattest to cut. |
| Stuck camera | Three or more adjacent shots share a size. Two is a shot/reverse pair; three reads as the camera being stuck. |
| No establishing shot | The sequence moves between more than one location and none is marked establishing. |

The cheapest real coverage is `medium_close`: enough face to act with, little
enough set to drift. `insert` is the editor's repair kit, hiding a continuity
error at the price of the cheapest shot on the list.

## 8. The location rail

Cross-shot consistency collapsed because "the office" existed only inside four
differently-worded free-prose `action` strings, and four descriptions of an
office are four offices. The model was not wrong; it was asked four times.

The fix is the style fix applied to the set: ONE description, injected into EVERY
shot filmed there, byte for byte, in one place (`location_text`). Locations carry
plates, reachable exactly as a `first_frame` is.

**Order is the free half.** Consistency degrades with recurrence distance: the
further apart two shots of the same set sit in the generation run, the less they
agree. `generation_order` groups a location's shots together, which is the
practitioner rule "generate all the shots in one location together" and costs
nothing but the order of a loop. The cut stays in narrative order; `assemble`
reads `ORDER BY idx`.

Re-describing a location, or moving a shot to a different one, resets the clips
filmed there and only those.

## 9. Style

Style is applied in ONE place (`prompt_for`), to EVERY shot, automatically. The
first cut of this module had a `style` column that nothing read.

Three levers, ascending in strength:

1. **A preset**, 11 of them: `live_action`, `anime`, `comic`, `painterly`,
   `noir`, `pixel`, `stop_motion`, `cg_animated`, `watercolor`, `vhs`,
   `silhouette`. Each describes medium, light and motion and carries a `note`
   naming its trap. None names a studio, film or living artist: that fragment is
   legally fraught and unreliable, and a description of the look is what steers.
2. **A style note**, the project's own wording, appended to the preset.
3. **Style reference images**. These beat both and are the only lever that holds
   a look across eight generations.

An unlisted word is treated as prose, not refused.

`pixel` is the **weakest** fit for generated video: models produce
pixel-*looking* output on a non-integer grid, which shimmers next to real pixel
art, so a pixel-art game almost always wants an in-engine cutscene.
`silhouette` is the one style that survives being unanchored, because no faces
means no identity drift.

**Naming no style is a silent choice.** A model given no stylistic instruction
uses its own house look, which differs per model and per version, so an unstyled
sequence is one whose appearance nobody chose and nobody can reproduce. `plan()`
says so.

**Changing the style resets generated shots**, with a count, because real money
has to be spent again. A clip bought under the old look is not a rendering of the
new one.

The fourth lever does not exist: the art seat can train a Krea LoRA
(`bgate_core/styles.py`) and no video provider wired here trains anything. A
trained look reaches a cutscene by generating that sequence's keyframes through
the art path and anchoring every shot on them.

## 10. Driving more than one model, without guessing

The initial build called `generate_video(duration=…, aspect_ratio=…,
first_frame_url=…)`. Those are **Seedance's** parameters, and a pipeline that
speaks them has one model in it forever.

kie's own catalogue does not agree with itself: **Sora 2 takes `n_frames` where
Seedance takes `duration`, and spells its aspect ratio `"landscape"` where
Seedance wants `"16:9"`.** Same vendor, same API family, three incompatible
spellings.

So the pipeline speaks **intent**: `seconds`, `shape`, `quality`, `first_frame`,
`last_frame`, `refs`, `audio`. Each model's table entry says what it calls those,
with declarative value maps and scales.

| Intent class | Members | When a model lacks it |
|---|---|---|
| Advisory | `quality`, `shape` | Dropped and **reported** on the result, never silently swallowed. |
| Essential | `seconds`, the frame/ref fields, explicit `audio=True` | Refuse *before* the spend. |

`audio=False` on a model that makes no audio is already true, so it is dropped;
`audio=True` is a missing deliverable, so it refuses. Getting this wrong made
every model simpler than Seedance unusable, and it was caught end to end, not by
a unit test.

**What is not in the model table.** This environment's egress policy blocks
`docs.kie.ai` and `api.kie.ai`, so the reference pages for Kling 3.0, Sora 2 and
Veo 3.1 could not be read, and the rule here is that only models whose id and
schema were read off their own reference page go in the table. So the ceiling
moved instead: **`cinematic_register_model`** adds a model from a page a human
has read. It refuses a spec without an id and an intent map, and stamps every
entry `source: "registered"`.

## 11. Post-production

Acquiring shots is not making a cutscene. `bgate_core/cinecut.py` is the second
half. It exists because the module docstring, the seat brief and this document
all said generated audio is off because the audio seat scores it over the top,
and no path existed for that to happen: every assembled cut shipped silent while
three documents described a mechanism.

**Captions are derived, never stored.** Timing comes from the shot durations and
the transitions between them; storing it would be a second answer to a question
the shot list already answers. **A transition eats time from both shots**, so a
1s dissolve is not 1s of extra runtime, and timing captions off the naive sum
drifts later and later through a sequence. Two files come out: `.srt` (what a
translator opens) and `.json` (what the generated scene reads, because GDScript
parses JSON in one call).

**Transitions**: `cut` (default), `fade`, `dissolve`, `wipe`. Cuts with no fades
take the concat demuxer, one decode and one encode. Anything else needs `xfade`,
which decodes every shot in full, so a project that wants none never pays for it.
`xfade` chains pairwise and its `offset` is measured on the *output built so
far*, not the input being added. Get it wrong and ffmpeg still exits 0, having
produced a cut with a shot missing or a frozen frame at the join.

**Sound is a mux, not a re-encode.** The picture has been through Theora once;
`-c:v copy` with a new Vorbis stream is the whole operation. **The video length
is authoritative**: `-shortest` would silently truncate the cutscene under a
short track, so the bed is padded with silence and the shortfall is reported.

**Continuity is measured on the pixels.** It extracts the real frames either side
of every join and compares mean luma (Rec. 601 weighted, because the eye is ~6x
more sensitive to green than blue and an unweighted mean misses jumps a viewer
sees) and palette distance. It reads the frames, not the prompts, because the
reason a cut fails is that the model did something other than what was asked.
Thresholds are loose: a detector that fires on every join gets switched off.

**Delivery writes four files**, because a file is not a cutscene:

- `<name>.tscn`, a **CanvasLayer at layer 100**, not a Control. A cutscene draws
  over whatever is rendering; a Control would inherit its parent's transform.
- `<name>.gd`, which plays, draws captions off `stream_position` (a separate
  timer drifts the moment the video stalls), skips on `ui_cancel`/`ui_accept`
  with `set_input_as_handled`, and emits one signal.
- `<name>.srt` and `<name>_captions.json`.

The contract is `finished(skipped: bool)`, fired whether the video ended or the
player skipped, because every caller wants the same thing next, and branching on
which is how a skipped cutscene leaves a game stuck on a black screen.

```gdscript
var cut := preload("res://.../prologue.tscn").instantiate()
add_child(cut)
await cut.finished
```

Delivery **refuses to overwrite a hand-edited script**. The `.gd` is meant to be
changed, so a second delivery silently reverting those edits would destroy a
user's work. A generated marker comment distinguishes the two; `force`
overrides.

**Shots no longer ship into the game.** Keeping a shot used to transcode to
Theora and copy into the project, but nothing references those: the game loads
the cut, and `assemble()` reads the `.mp4` candidates directly. That was a Theora
encode per shot and tens of megabytes of unreferenced files at 1080p. Now a cut
installs and a shot does not, with `install_to_engine` for the one real case of a
single clip used alone as an attract loop or a sting. A three-shot demo went from
8 files in the project to 5.

## 12. What got built

| Piece | Where |
|---|---|
| Styles, shot sizes, locations, plan / generate / keep / assemble / transcode / recover | `bgate_core/cinematic.py` |
| Captions, transitions, audio mux, continuity, scene generation | `bgate_core/cinecut.py` |
| Animatic previs reel | `bgate_core/animatic.py` |
| kie file upload (`upload_file`), auto-upload of local anchors, the intent layer (`video_input`, `video_capabilities`, `register_video_model`) | `bgate_adapters/kie.py` |
| Shot lists, style, model, locations, sound / transitions / VO | `bgate_core/db.py` migrations 0027, 0028 |
| The seat, its lane, its workflow and its trap | `bgate_core/seats.py` |
| Twenty MCP tools (`cinematic_*`) | `bgate_mcp/server.py` |
| Dashboard endpoints and seat workspace | `bgate_ui/routes/cinematic.py`, `frontend/public/seats/cinematic.js` |
| `video` asset kinds (`.mp4`, `.ogv`, `.webm`, `.mov`) | `bgate_core/assets.py` |
| libtheora on the ffmpeg row | `bgate_core/doctor.py` |

**The shot list is a table, not metadata**, which is the one place this diverges
from `music.py` structurally. A Suno request returns the whole deliverable; a
cutscene does not. Order is not derivable from disk (`..._shot3.mp4` sorts before
`_shot10`), and an agent killed after five of eight shots has spent real money
while the plan for the remaining three lived only in its context.

### Three things the tests pin, because all three fail silently

1. **A kept clip is really Theora**, verified with `ffprobe` rather than by
   extension. A test asserting on `.ogv` alone would pass for a renamed `.mp4`,
   which is the bug, since Godot reads the container and not the name.
2. **Mismatched shot sizes refuse to assemble.** ffmpeg's concat demuxer does not
   scale; handed a 1080p shot after four 720p ones it produces a garbled or
   truncated file and frequently exits zero.
3. **A superseded take reports `installed: false`.** Every take installs to the
   destination named for the logical asset and the game loads one file, so
   without a measured check two cards both claim to be the clip in the game.

### Recovering what was already paid for

A generation is charged at **submit**. The poll loop, the download, and this
process surviving the ten minutes it takes can all fail while the provider sits
on a finished, billed clip. `cinematic_shot_status` looks;
`cinematic_recover_shot` collects. The task id is read off the shot row, so an
agent that died mid-generation leaves no archaeology for its successor. No cost
is claimed on recovery, because a balance delta measured after the fact is
meaningless.

### Bugs the tests caught during the build

`slugify("")` returns `"unnamed"`, which is **truthy**, so
`slugify(x) or f"shot{i}"` gave every unnamed shot the same slug: one logical
name, one candidate path, and shot 2 silently overwriting the clip shot 1 had
been paid for. It surfaced because the mismatched-size test could not find two
different sizes.

Two more the end-to-end run caught that no unit test would have: `audio=False`
being *refused* by a model with no audio parameter, and then, fixing that too
eagerly, `audio=False` being *omitted* rather than sent, which would have shipped
Seedance clips with a baked-in bed, because its `generate_audio` defaults to true
upstream.

## Sources

- [VideoStreamPlayer](https://docs.godotengine.org/en/stable/classes/class_videostreamplayer.html) and [Playing videos](https://docs.godotengine.org/en/stable/tutorials/animation/playing_videos.html), Godot Engine
- [Supported video formats, godot-proposals #9669](https://github.com/godotengine/godot-proposals/discussions/9669)
- [Base64 File Upload](https://docs.kie.ai/file-upload-api/upload-file-base-64) and [File Upload Quickstart](https://docs.kie.ai/file-upload-api/quickstart), docs.kie.ai
- [Veo 3.1 vs Kling 3.0 vs Sora 2: AI Video API Pricing 2026](https://modelslab.com/blog/api/veo-3-1-vs-kling-3-sora-2-ai-video-api-cost-2026)
- [AI Video Generation API Pricing (July 2026)](https://www.buildmvpfast.com/api-costs/ai-video)
- [The 2026 AI Video Production Playbook](https://medium.com/data-science-collective/the-2026-ai-video-production-playbook-bc683d5b85da)
- [The AI Video Workflow in 2026](https://vivideo.ai/blog/state-of-ai-video-creation-2026)
- [Encoding Theora Ogg with ffmpeg](https://blog.archive.org/2008/11/25/fast-and-reliable-way-to-encode-theora-ogg-videos-using-ffmpeg-libtheora-and-liboggz/)
