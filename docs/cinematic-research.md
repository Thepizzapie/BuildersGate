# AI cinematic generation, and what a cutscene seat actually has to do

2026-08-10. Research notes plus the build they justify. Written before and
during the implementation of the `cinematic` seat, so the reasoning survives
the diff.

The question was "can a seat generate cutscenes with the video models". The
short answer is yes, and the interesting part is that the model call is the
*easy* third of it. Two of the three hard problems have nothing to do with
generation quality: one is a format wall in the engine that silently swallows
a finished cutscene, and one is an anchoring problem that this repo had
already solved once for sprites and had explicitly declared impossible for
video.

## 1. The model landscape, as of now

Four families are worth naming. Prices are per second of output unless noted
and move constantly; the ratios matter more than the absolutes.

| Model | Rough cost | Notes |
|---|---|---|
| Veo 3.1 | ~$0.40/s | The only one shipping native audio in the video output. Gated by region and account tier through Vertex/Gemini. |
| Kling 3.0 | ~$0.09–0.14/s | Cheapest of the tier; motion consistency and subject tracking much improved over 2.0. |
| Sora 2 | ~$1/clip base, $3–5 Pro | OpenAI announced the Sora 2 API sunsets 2026-09-24, which alone disqualifies it as a pipeline dependency. |
| Seedance 2.0 | unpublished (kie bills in credits) | What this repo can already reach. |
| Runway Gen-4.5 | — | Best creative-control surface: video-to-video, motion controls. |

**Why the build targets Seedance and not the "best" model.** kie.ai was already
wired here (`bgate_adapters/kie.py`) with a verified Seedance schema, and it is
one key that also covers images and Suno music. Adding a second video provider
is four lines in a table (`kie.MODELS`) or one new adapter; picking a provider
was never the hard part and is the part most likely to be wrong in six months.

The genuinely load-bearing constraint is one every model in the table shares:

> **No model generates more than about 15 seconds, and none holds together
> well past about 10.**

Seedance's own documented range is 4–15s. Every current workflow guide
independently lands on writing shots at 5–10 seconds. That single fact is what
makes a cutscene a *pipeline* rather than a *call* — a 90-second scene is ten
separate paid generations that must be planned, judged individually, and
joined.

## 2. The format wall, which is the expensive one

**Godot 4 plays Ogg Theora and nothing else in core.** Not "prefers" — the only
`VideoStream` implementation shipped is `VideoStreamTheora`. H.264 and H.265
cannot be included because they are patent-encumbered, and WebM support was
*removed* in 4.0 as too buggy to maintain.

Every video model on the market returns H.264 in an `.mp4`.

What makes this a trap rather than an inconvenience is the failure mode. An
`.mp4` dropped into a Godot project **does not produce an import error**. It is
simply not imported as a `VideoStream`, so `load()` returns null, the
`VideoStreamPlayer` stays empty, and the scene runs perfectly with a blank
rectangle where the cutscene was. Nothing anywhere says "wrong format". A
pipeline that copies its output into the project — which is exactly what the
existing `music.py` does, correctly, for `.mp3` — would report a green badge
over a cutscene the game cannot play.

So **keeping a shot transcodes it**. The encode settings are taken from Godot's
own documentation rather than from taste:

```
ffmpeg -i input.mp4 -q:v 6 -q:a 6 -g:v 64 output.ogv
```

- `-q:v 6` is the documented baseline on a 1–10 scale; drop to 5 for 1440p+.
- `-g:v 64` is the one that is not optional. libtheora's default GOP is **12**,
  which the engine docs call insufficient and which inflates a cutscene several
  times over for nothing. The sanctioned range is 64–512, trading seek time for
  size — and a cutscene is played start to finish, so the seek cost is not real.
- `-vf "scale=-1:720"` when the source is more than the cutscene needs.

Two further constraints worth knowing before designing around video: streaming
from a URL is not supported, and Theora audio downmixes to stereo (4/5.1/7.1 are
not supported).

**ffmpeg on PATH is necessary and not sufficient.** libtheora and libvorbis are
optional build flags. A build without them fails at the encode with `Unknown
encoder 'libtheora'` — *after* the whole sequence has been generated and paid
for. `cinematic.ffmpeg_status()` therefore probes `-encoders`, not just
`shutil.which`, and `generate_shot` refuses to spend a cent until it passes.

## 3. The anchoring problem, and the limitation that was not real

An off-model sprite is one bad frame. An off-model cutscene is the player
watching a stranger deliver the story beat. Identity matters *more* here and
this repo had *less* to work with:

`bgate_adapters/kie.py`'s own docstring said, flatly, that anchored generation
through kie is unavailable — every reference field is documented as a URI, none
takes inline bytes, and every pinned anchor in this product is a file on disk.
For images that is survivable, because Krea accepts a data URI inline and the
art seat can go there instead. For video there is nowhere to go: **no other
provider wired here generates a frame of video at all.**

That limitation turned out to be a true statement about `/api/v1/jobs` and a
false one about kie. kie serves a **file upload API from a different host** —
`POST https://kieai.redpandaai.co/api/file-base64-upload`, same Bearer token,
`{base64Data, uploadPath, fileName}` in, `downloadUrl` back — and the
generation endpoints accept the URL it mints. The capability was one missing
call, not a missing capability.

Two properties of it shape the design:

- **Uploads die in 3 days.** Much shorter than Suno's fourteen. Short enough
  that no minted URL may ever be cached or persisted as if it were an anchor —
  every one is stamped with the day it stops working, and the shot list stores
  *project paths*, uploading fresh at generation time.
- **It is still worse than Krea for a still.** A separate POST, a copy on
  someone else's disk, an expiry. `image_generate` should keep preferring Krea.
  The upload path exists because video has no alternative.

### The continuity rule, which is this repo's own rule with a worse constant

The tempting way to chain shots is to pull the last frame out of shot N's video
and hand it to shot N+1 as its first frame. The field is right there and it
looks exactly like what it is for. Several commercial tools advertise precisely
this ("reference frames carry forward automatically").

It is the same chain the art seat banned after measuring it:

> "NEVER CONDITION FRAME N ON FRAME N-1. Chains decay. Measured: a back view
> turned front-facing by frame 3, and a figure shrank from 932px to 821px
> across one cycle." — `seats.py`, art seat rule 2

For video the decay constant is worse, for three compounding reasons: a video
model's final frame is *already* the most drifted image it produced; it is a
lossy intermediate carrying compression artefacts; and eight shots of it is
eight generations of a photocopy.

So every shot in a sequence anchors on **the same approved stills**, generated
through the existing art path (`image_generate` conditioned on the pinned
character) and approved before a frame of video is bought. `last_frame` is
reserved for one deliberate match cut a human asked for, never for the spine of
a sequence.

## 4. Audio is off by default

Seedance generates its own audio unless told not to, and Veo's native audio is
sold as its headline feature. For film that is correct. For a game it is a trap:
the generated bed is **baked into the clip and cannot be separated afterwards**.
It fights the score the audio seat wrote, it cannot be ducked under dialogue, it
cannot be re-mixed for localisation, and Godot downmixes it to stereo anyway.

A cutscene here is **picture**. The score and the voice come from the seat that
owns them, laid over the top, where they stay editable. `generate_audio=True`
remains available as a deliberate override.

## 5. Why an eighth seat rather than widening `art`

The bar for a new seat is a body of work with its own failure modes, its own
binaries, and a lane nobody else should write in. Cutscenes clear all three:

- **Different binaries, same lock problem.** A `.ogv` does not merge any more
  than a `.blend` does, and it is fifty times the size. Art's lane is
  `game/assets/**`; a cutscene landing there would have the art seat locking
  video it did not make and cannot judge.
- **Different unit of work.** Art's rules count *frames of one subject* and its
  whole discipline is derive-don't-generate. A cutscene cannot be derived from
  anything — every shot is a separate paid generation against a hard ceiling,
  and the skill is writing a shot list and judging a cut.
- **Different money.** A sequence is the most expensive thing this product buys
  in one sitting. A seat whose brief does not open with that will spend it.

Named `cinematic` rather than `video` (that is the *capability*, and naming a
seat after one invites every future video-adjacent tool into its lane),
`cutscene` (one deliverable of several — a trailer and an attract loop are the
same craft) or `film` (claims a scope this cannot deliver).

### And when not to use it at all

A short beat that could be an **in-engine scripted camera move** is almost
always better as one: interactive, re-uses art already made, free per
iteration, localises, and cannot go off-model because it *is* the model.
Generated video earns its place where the engine cannot go — an establishing
shot of a place that was never built, a stylised prologue, a trailer. The seat's
brief says so explicitly, because the failure of handing "the character walks
in and talks" to this seat is expensive and silent.

## 6. What got built

| Piece | Where |
|---|---|
| kie file-upload API (`upload_file`, `upload_all`), auto-upload of local anchors in `generate_video` | `bgate_adapters/kie.py` |
| Shot lists (`cine_sequence`, `cine_shot`) | `bgate_core/db.py` migration 0027 |
| Plan / generate / keep / assemble / transcode | `bgate_core/cinematic.py` |
| The seat, its lane, its workflow and its trap | `bgate_core/seats.py` |
| Eight MCP tools (`cinematic_*`) | `bgate_mcp/server.py` |
| `video` asset kinds (`.mp4`, `.ogv`, `.webm`, `.mov`) | `bgate_core/assets.py` |

The working loop:

```
cinematic_plan          # free. the only point a sequence can be argued with cheaply
cinematic_generate_shot # one shot per call, deliberately. watch it. re-roll or keep
cinematic_keep          # TRANSCODES to .ogv into the engine project, then approves
cinematic_assemble      # joins the kept shots into one .ogv
cinematic_keep          # the cut itself
```

**The shot list is a table, not metadata**, and that is the one place this
diverges from `music.py` structurally. A Suno request returns the whole
deliverable; a cutscene does not. Order is not derivable from disk
(`..._shot3.mp4` sorts before `_shot10`), and an agent killed after five of
eight shots has spent real money while the plan for the remaining three lived
only in its context. That is exactly the kill-tax the WORK MANIFEST rule exists
to prevent, and a `.jsonl` checkpoint is the wrong shape because a shot list is
*edited* rather than appended to.

### Three things the tests pin, because all three fail silently

1. **A kept clip is really Theora**, verified with `ffprobe` rather than by
   checking the extension — a test asserting on `.ogv` alone would pass for a
   renamed `.mp4`, which is precisely the bug, since Godot reads the container
   and not the name.
2. **Mismatched shot sizes refuse to assemble.** ffmpeg's concat demuxer does
   not scale; handed a 1080p shot after four 720p ones it produces a garbled or
   truncated file and **frequently exits zero**, so this cannot be left to the
   return code.
3. **A superseded take reports `installed: false`.** Every take installs to the
   destination named for the logical asset and the game loads one file, so
   without a measured check two cards both claim to be the clip in the game.

### One bug the tests caught during the build

`slugify("")` returns `"unnamed"`, which is **truthy** — so
`slugify(x) or f"shot{i}"` gave every unnamed shot in a sequence the same slug.
One logical name, one candidate path, and shot 2's generation silently
overwriting the clip shot 1 had just been paid for. Nothing errored; the
sequence simply came back with every beat looking like the last one generated.
It surfaced because the mismatched-size test could not find two different sizes
— there was only ever one file.

## Sources

- [VideoStreamPlayer — Godot Engine](https://docs.godotengine.org/en/stable/classes/class_videostreamplayer.html)
- [Playing videos — Godot Engine](https://docs.godotengine.org/en/stable/tutorials/animation/playing_videos.html)
- [VideoStreamPlayer supported video formats — godot-proposals #9669](https://github.com/godotengine/godot-proposals/discussions/9669)
- [Base64 File Upload — docs.kie.ai](https://docs.kie.ai/file-upload-api/upload-file-base-64)
- [File Upload API Quickstart — docs.kie.ai](https://docs.kie.ai/file-upload-api/quickstart)
- [Veo 3.1 vs Kling 3.0 vs Sora 2: AI Video API Pricing 2026](https://modelslab.com/blog/api/veo-3-1-vs-kling-3-sora-2-ai-video-api-cost-2026)
- [AI Video Generation API Pricing (July 2026)](https://www.buildmvpfast.com/api-costs/ai-video)
- [The 2026 AI Video Production Playbook](https://medium.com/data-science-collective/the-2026-ai-video-production-playbook-bc683d5b85da)
- [The AI Video Workflow in 2026: A Hands-On Guide](https://vivideo.ai/blog/state-of-ai-video-creation-2026)
- [Fast and reliable way to encode Theora Ogg videos using ffmpeg](https://blog.archive.org/2008/11/25/fast-and-reliable-way-to-encode-theora-ogg-videos-using-ffmpeg-libtheora-and-liboggz/)
