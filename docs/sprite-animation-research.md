# Sprite sheets that animate: what was missing, and what was measured

2026-08-10, extended 2026-08-11. Research plus the changes it produced, against
0.1.35. The painted sprite path kept producing sheets that passed every gate and
still looked wrong in play.

## The findings in one place

| # | Finding | Shipped as |
|---|---|---|
| 0 | The anchor was the weakest reference configuration available: one front idle plus near-copies of it. | `anchor_views=3` default, a generated three-quarter and profile off the approved anchor |
| 1 | Bounding-box centring shifts the torso when a limb extends. | Core-column median anchor, `spritekit.anchor_x` |
| 2 | Palette drift was detected. Detection costs a re-roll; quantisation makes drift unrepresentable. | `palette_lock="auto"` |
| 3 | Godot has carried per-frame `duration` all along; the emitter wrote `1.0`. | `timing` with per-animation `holds`, `order`, `loop`, `fps` |
| 4 | Ping-pong is "derive the rest" applied to timing. | Baked frame order (`0,1,2,1`) |
| 5 | Four cross-frame faults nothing could see: duplicate, pop, open_loop, detached. | Motion report, advisory |
| 6 | Nothing knew how a walk is built. | `bgate_core/animspec.py`, ten archetypes; `sprite_plan` |
| 7 | A long sheet was a texture that would not upload. | Grid wrap past 4096px, `sheet_padding` |
| 8 | Video in-betweening is the one continuity technique per-frame generation cannot buy. | Not built. Three blockers below. |
| 9 | The pose prompt breaks this project's own rule 5 (negation summons the thing). | Not changed. A cheap A/B nobody has run. |
| 10 | A multi-figure row degrades left to right and the slice destroys the evidence. | `sprite_sheet_check` (2026-08-11) |

Findings 0, 8 and 9 change what the models return, which is where the leverage
is. Findings 1 to 7 are assembly, which is what a player sees. Finding 10 is free
and sits between them.

The failure that motivates all of it: four frames named `walk/0` to `walk/3`,
described as "walking", "walking, left foot forward", "walking", "walking, right
foot forward". That set assembles perfectly. The identity judge scores it high,
the palette holds, the cut is clean, and nothing rejects it, because nothing is
wrong with any individual frame. It is four drawings of a person standing near a
walk, and in play the character slides along the floor.

## Finding 0: the anchor was the weakest reference configuration available

The largest single lever, and one line of policy.

Every reference `image_sprites` carried was the same view: one front-facing idle
plus previous frames that are near-copies of it. Two to three images **from
distinct angles** materially improve identity retention, and four distinct
angles carry more useful information than ten slightly-different front shots.
More same-angle references do not substitute. Animation reached the same answer a
century earlier and called it a model sheet.

It bites hardest in the normal case: a side-view action game asks for side-view
poses against a front-view anchor, so the model invents the profile on every call
and invents it differently each time. Re-rolling a flagged frame buys another
guess at information the anchor never carried, so the retry budget goes on
re-guessing.

`anchor_views=3` (default) generates a three-quarter and a profile off the
approved anchor **once** and passes all three on every pose call. Two extra
generations against a run that pays one per pose plus up to one per re-roll: a
twelve-pose set goes from 13 calls to 15 before it prevents a single re-roll.
`anchor_views=1` restores the old behaviour.

### The model matters as much as the references

Character work on Krea is pinned to `nano-banana-2`
(`bgate_adapters/krea.py:CHARACTER_MODEL`). `krea-2-large` and `krea-2-medium`
condition on a reference as **style**, following a look and owing nothing to a
pose. A second group takes references as **edit** inputs.

| Measurement | Result |
|---|---|
| krea-2-medium on the party idles | A **face** in seven of eight frames, when four were specified as back views |
| nano-banana-2, same job | Eight frames a row, correct back and front views |
| nano-banana-2 price | Flat $0.06, against krea-2-large's $0.065 with references, a quarter of nano-banana-pro |

A style reference cannot hold a subject through a pose change, because holding
the subject is not what it does. `nano-banana-2` uses the same `image_urls` edit
contract as `nano-banana-pro` and keeps `styles`, so a trained LoRA still rides
alongside. Scoped to character kinds: an item, a prop, a decal or a VFX key frame
has no pose continuity to preserve. `sprite`, `sheet` and `portrait` were missing
from that list, so the pin did not bite on the kinds people generate most.
Corrected 2026-08-10; the bake-off is in [reference.md](reference.md).

The pin exposed a second bug: `image_sprites` priced every run off the gpt-image
quality table whichever provider was named, so the spend gate under-quoted every
Krea run. Krea prices per model and payload, and the gate reads its numbers now.

`ideogram-3` is the only Krea model with a dedicated
`character_reference_images` field rather than a style slot doing two jobs, which
is the split art rule 4 says cannot share a weight. The adapter already routes
references to a model's own `ref_field`. It bills 2.5x once references are
attached, so reach for it when a cast keeps collapsing into one person.

## What 0.1.35 already had

| Mechanism | Where | What it guarantees |
|---|---|---|
| Reference-first generation | `image_sprites` | Every frame is an edit against ONE approved anchor, so identity re-grounds each call instead of decaying down a chain. |
| Anchor + rolling conditioning | `image_sprites` | The anchor is always present; the previous frame and, for a closing frame, the cycle's first ride along on top. |
| Keyable-background contract | `chroma.py` | Alpha is manufactured and audited rather than requested, because neither provider returns usable alpha. |
| Alpha cut audit | `chroma.audit` | Border bleed, white halo, feathered alpha, dirty alpha, hollow interiors, residual chroma. |
| Vision identity judge, palette histograms | `_vision_consistency` | Per-frame identity score, frame-to-frame outliers, palette cohesion against the batch median. |
| Area-anchored scale | `sprites.from_pose_images` | Draw-size drift removed by normalising each frame to constant visual mass. |
| Spend and deadline ceilings | `image_sprites` | Priced before buying, re-checked before every pose. |

Every one of those is about a **frame**. The gap is everything visible only
**between** frames.

## Finding 1: bounding boxes are not bodies

`from_pose_images` centred each frame on its alpha bounding box. A jab widens the
box to the right, so box-centring shifts the **torso** left to compensate and the
fighter takes a sideways step every time he punches.

Synthetic: one torso that is identical pixels in both frames, plus one arm of
constant mass, folded across the body in the first frame and thrown out to the
side in the second. Any apparent torso movement is registration error.

| Strategy | Torso moves |
|---|---|
| Bounding-box centre (0.1.35) | **44.5px** |
| Alpha-weighted centroid | 6.0px |
| Weighted median column | 3.5px |
| **Core-column median (shipped)** | **1.0px** |

Published measurements on the box-to-centroid substitution report ~27px of
standard deviation against ~0.2px centroid-pinned.

An outstretched limb is a tail in the horizontal mass distribution, the input
shape a mean handles worst. The median ignores the tail's distance but still
counts its mass; dropping columns holding less than 25% of the tallest column's
ink removes the limb from the vote entirely, leaving the torso.

Vertical registration did not change. X from mass, Y from the floor: feet belong
on a ground line and a mass pin would float a crouch.

**A second bug fell out of it.** The set's fit scale came from the widest
*bounding box*, but under anchor registration the room a pose needs is twice its
furthest **reach from the anchor**. A jab extending 90px right of the body and
30px left needs 180px of cell, not the 120px its box measures. Fitting the box
left the wide pose unable to sit where its anchor said, the placement clamp
dragged it back inside, and the body landed off centre by exactly that amount.
Nine of the surviving 12.5px in an early measurement were the clamp.

`spritekit.anchor_x`, `spritekit.place_offset`, the reach-based fit in
`sprites.from_pose_images`. `tests/test_spritekit.py::TestRegistration` rebuilds
the synthetic and fails if the ordering of those four numbers inverts.

## Finding 2: detection is the expensive option

0.1.35 scores each frame's 4-bit-per-channel histogram against the batch median
and flags outliers for re-roll. It works, costs one image call per failure, and
only reduces the probability of drift. Quantising every frame to the reference's
own palette makes drift **unrepresentable**: a colour the character does not have
has nowhere to be stored.

**It is a posteriser.** On flat, cel-shaded, limited-palette and pixel art it is
invisible. On smoothly rendered painterly art it bands the shading, and no
palette size fixes that in general. So `palette_lock` defaults to `"auto"`,
measuring whether the reference's ink lives on a small number of colours and
switching on only when it does. The result says which way it went.

Matching is RGB via Pillow's C quantiser, not Lab. The perceptual difference does
not survive art generated at 1024x1536 and downscaled, and the C path is roughly
three orders of magnitude faster than the equivalent Python loop.

**One bug shipped inverted, and every downstream gate called the result clean.**
A P-mode palette always holds 256 entries and
Pillow searches all of them, so zero-filling the tail puts pure black in the
palette, and black wins the nearest-colour vote for anything mid-tone. Measured
on a red reference and a drifted green frame: green sits 206 from black and 226
from the red it should have snapped to. "Lock this frame to the character's
colours" turned it black. The padding now repeats the last real colour;
`tests/test_spritekit.py::TestPaletteLock` covers it.

## Finding 3: per-frame timing

Godot 4's `SpriteFrames` gives every frame a relative `duration`: a frame at 2.0
holds twice as long as one at 1.0, and absolute duration is
`duration / (fps * playing_speed)`. The 0.1.35 emitter wrote `1.0` for every
frame this project ever produced.

A uniform hold is the flattest possible reading of any action. In hand animation
the impact frame is **held** while the anticipation is **rushed**, and that
contrast is the feeling of a hit landing.

`timing` now carries per-animation `holds`, `order`, `loop` and `fps`. A 6fps
idle and a 12fps attack on one sheet is normal, and a single sheet-wide speed is
a compromise between two right answers.

## Finding 4: ping-pong

Three drawings played `0, 1, 2, 1` are a four-step cycle. Three generations
instead of four, and it **cannot have a loop seam**, because the wrap-around pair
is a genuine adjacent pair. That is what a breathing idle, a hovering pickup or a
bobbing NPC wants.

Godot has no ping-pong loop mode (the engine proposal is still open), so it is
baked into the emitted frame list, where the plan belongs. Endpoints are not
repeated (`0,1,2,1`, not `0,1,2,2,1,0`); the doubled endpoint stutters visibly at
any speed.

## Finding 5: four faults nothing could see

Height jitter was the only cross-frame measurement in 0.1.35. These four get
noticed, and every one is perfectly on-model, so any identity score above the
floor is compatible with all of them:

- **duplicate**: two adjacent frames are the same drawing. The animation holds
  still and one generation was bought for nothing.
- **pop**: two adjacent frames share almost no silhouette. The character was
  redrawn, not moved.
- **open_loop**: the last frame does not flow into the first. It hitches once per
  repetition, forever.
- **detached**: the silhouette is in more than one piece. The key bit through a
  wrist and a hand floats beside an armless character. Every existing audit
  inspects the *cut*; none asks whether this is *one figure*.

The first three are intersection-over-union of the alpha masks of registered
frames. The loop check is **relative** to the animation's own mean adjacent
overlap: a fast run legitimately has low overlap everywhere and an idle high
overlap everywhere, so an absolute threshold would flag every run and pass every
idle. One-shots (death, ko, fall) are excluded. `detached` separates **parts**
(blobs holding at least 2% of the ink, `spritekit.parts`) from **speckles**:
parts above one points at the key colour appearing inside the art, dozens of
speckles at a backdrop that was never flat.

**Advisory, not a gate.** A duplicate frame is fixed by a different pose
description, so a retry buys the same frame again. An audit that fires on
everything gets switched off (art rule 8). Reported in the result, the artifact
metadata and the log line.

## Finding 6: nothing knew how a walk is built

`image_sprites` animates whatever poses it is handed, so animation quality was
decided by whether the driving agent remembered how a walk cycle works.

- A **walk** is CONTACT, DOWN, PASSING, UP, once per leg. The body rises and
  falls twice per cycle and that bob is what a walk *is*. Four frames with no
  height change is a character sliding along the floor. The two-pose version
  (contact and passing, four frames total) is the standard for small sprites,
  where the recoil is a pixel or two and does not survive the downscale.
- A **run** is a walk with a **flight frame**, the moment neither foot touches.
  If every frame has a foot down it is a fast walk whatever the fps says.
- An **attack** is ANTICIPATION, CONTACT, FOLLOW-THROUGH, RECOVER, impact held
  and wind-up rushed.
- A **death** is a one-shot whose last frame holds longest, because it is the
  frame that stays on screen.
- A **jump** holds longest at the apex, which is physically true and is what
  makes a jump feel like it hangs.

`bgate_core/animspec.py` encodes ten archetypes as pose descriptions plus timing,
naming limb positions and weight rather than adjectives. "Leaning back" is a
pose; "more dynamic" is not, and returns a different character. `sprite_plan`
hands back the plan and its price for free; `archetypes=[...]` on `image_sprites`
runs exactly it.

## Finding 7: a long sheet was a texture that would not upload

`_stitch` built one horizontal strip with no width limit. Desktop GL and Vulkan
allow 16384px; mobile and web commonly cap at 4096, and a texture over the limit
does not warn. It fails to upload and the sprite draws as nothing. A thirty-frame
character at 160px wide is 4800px, so this is the second sheet anyone builds.

Sheets wrap into a grid past 4096px and take a one-pixel transparent gutter when
they do, because a gridded sheet has vertical neighbours and a sprite drawn at a
non-integer scale samples across its region edge. Short sheets stay a plain strip
with no padding: that is what every existing sheet and region assertion is, and a
layout change nobody asked for is a re-import of every character in the project.
`sheet_padding` is there for projects that draw at non-integer scale with linear
filtering.

The gutter is transparent, not an extrusion of the edge pixel. Cells are
alpha-trimmed with a margin, so a transparent gutter bleeds nothing; extrusion
would bleed the sprite's own edge colour outward into a halo.

The layout is **returned** by the stitcher and passed to the emitter. The only
way a sheet and its `.tres` can disagree is if one derives the geometry
independently.

## Finding 8: the technique per-frame generation cannot buy

Within a single generation, consistency works because the model produces all
frames in one pass, treating the sequence as one context: geometry, palette and
lighting stay stable because nothing resets mid-generation. Per-frame generation
cannot buy that at any price.

A character's motion is not an affine transform, which is exactly the case
`vfx.animate` says is out of scope and should be handled by generating a second
key frame. A video model interpolating between two approved key poses is that
missing middle.

| Workflow step | Status |
|---|---|
| 1. Generate key poses through the keyable-background contract | Exists. The published workflow independently arrives at `#FF00FF`; `chroma.CHROMA[0]` is magenta, `(255, 0, 255)`. |
| 2. Feed first and last key pose to a video model as `first_frame_url` / `last_frame_url` | Not built |
| 3. Extract frames with ffmpeg | Not built |
| 4. Key each extracted frame, `chroma.finish` | Exists, unchanged |
| 5. Assemble, `sprites.from_pose_images` | Exists, unchanged |

Three blockers to resolve before building it:

- **It could not be exercised at all.** No kie key and no ffmpeg in that
  environment, so neither half ran once. Shipping a paid pipeline whose every
  step is untested is worse than not shipping it.
- **Chroma through video compression is unproven and is the likeliest failure.**
  H.264 4:2:0 subsampling halves chroma resolution and a pure-chroma key colour
  is its worst case. The magenta edge may smear enough that the alpha audit
  rejects every extracted frame, which fails loudly but may mean choosing the key
  colour for compression survival rather than palette distance, or forcing
  1080p/4k.
- **It uploads the user's art to a third party.** kie's image fields take public
  URLs; its file-upload API (`/api/file-base64-upload`, same bearer token,
  different host, files dead after three days, `UPLOAD_TTL_DAYS`) turns a local
  anchor into one. That makes it possible, not automatic. It should be an
  explicitly invoked tool that says what it uploads and for how long, never a
  silent default inside `image_sprites`.

kie prices video at "typically 100-500 credits" against "10-50" for images, so
this buys continuity, not cheapness.

The same endpoint retired a refusal that was too strong: `chroma.generate` told
callers kie cannot condition on the pinned refs because its image fields take
public URLs only. The first half is true, the conclusion was not, and the upload
path is wired in now. See [gotchas.md](gotchas.md) on kie's default image model.

## Finding 9: the pose prompt breaks this project's own rule 5

Not changed, because changing a prompt that works without a way to measure the
result is how things get worse.

Rule 5 is stated as measured: "NEGATION SUMMONS THE THING. 'No face, no hair'
returns a face with hair. Reframe rather than forbid." The per-pose prompt in
`image_sprites` is built almost entirely out of negations: "do NOT slim him down,
bulk him up, change his muscle definition, or restyle the body between frames…
no text, no cropping of limbs."

By the project's own finding, that phrasing is a candidate cause of the
build-drift the consistency gate keeps flagging. The positive reframing (describe
the build to hold, ask for full-body framing rather than forbid crops) is a
one-line change scored by the existing `_vision_consistency` judge. That
experiment has not been run.

## Finding 10: the row degrades left to right, and the slice hides it

Added 2026-08-11. `sprite_sheet_check` (MCP, free, no model call) measures a
multi-figure image **before** anything is spent keying, slicing or assembling it.

An image model asked for four figures on one canvas does not draw four frames. It
draws one picture, left to right, each figure conditioned on the canvas so far,
so every small error is inherited and added to. The result degrades across the
row and, on a stacked sheet, down the page. None of it is visible in any single
frame, all of it is obvious with a straight edge held against the image, and no
existing audit can see it, because they run on frames already sliced and
bottom-pinned into their own cells, by which point pinning has **destroyed the
evidence without fixing the drawing**.

Findings within a row:

| Finding | Threshold | Meaning |
|---|---|---|
| `foot_drift` | `FOOT_DRIFT_MAX = 0.03` of figure height | The figures are not standing on one line. Tightest threshold in the module: a bob lifts the head, not the feet. 3% of a 450px figure is 13px, about where a walk stops reading as contact with the floor. |
| `head_drift` | `HEAD_DRIFT_MAX = 0.18` | The figures translate vertically further than a bob accounts for. Loose, because the head is supposed to move. |
| `size_drift` | `SIZE_DRIFT_MAX = 0.08` of the median | The character is drawn at different sizes. |
| `size_ramp` | `TREND_RHO = 0.9` Spearman | The size drift is **monotonic**, so it compounds. |
| `facing_flip` | `FACING_SKEW_MIN = 0.05` over the top `HEAD_BAND_FRAC = 0.30` of the figure | A head is yawed against its row. Silhouette overlap cannot see it and neither can any identity check: same character, correctly drawn, pointing wrong. |
| `stray_ink` | `STRAY_FRAC = 0.003` of canvas ink | Something on the canvas is not the character. A lettered "FORWARD!" is a percent or two of the ink; bad-key confetti is tenths of one percent per fleck. |
| `empty_cell` | | A grid cell with no figure in it. |

Across rows: `sheet_size_drift` and `sheet_size_ramp` (the rows disagree on how
big the character is, so animations pop when the game switches between them) and
`band_palette` (a row carries colours no other row uses, which is how a tie, a
light that comes on, or a trim that appears for two rows gets in and stays).

**Read the two ramps differently.** Every other finding says re-roll that figure.
A ramp says the drift compounds, so re-rolling buys one better figure and the
next attempt does the same thing. The fix is structural: stop asking for a row,
generate each pose as its own image against one approved reference, which is what
`image_sprites` does.

It returns an **annotated copy** of the image with the ground line, the head line
and each figure's true feet and mass anchor drawn on it. It measures a raw
un-keyed generation as well as a keyed one: alpha when there is alpha, otherwise
the backdrop comes from the four corners.

Advisory, never a gate. A turnaround SHOULD flip its facing and a size chart
SHOULD ramp. It says nothing about whether it is the right *character*, which is
`consistency_check`, because identity is not arithmetic.

## Considered and not built

- **Seed locking across the frames of one animation.** A negative result: fixing
  the seed is brittle the moment the prompt changes, and a changed prompt with a
  fixed seed commonly returns a completely different composition. Every Krea
  model exposes `seed` and the sprite path continues not to set it.
- **LoRA / custom character model training.** The strongest identity mechanism
  available. `styles.for_generation` and `art.style_source == 'lora'` already
  ride alongside the references, so the LoRA carries the *style* and frees the
  reference slot for *identity*. What is missing is a trained CHARACTER rather
  than a trained style: a different training set and lifecycle, not a different
  call.
- **Grid generation** (a 3x3 of poses in one image). The most-recommended
  technique in current tooling literature, banned here on measured evidence:
  `bgate_adapters/imagegen.py:_reject_multi_pose` refuses it, and
  `bgate_core/vfx.py` records the faults it produces: a mug that shatters over
  three frames and is intact again in the fourth, a palette that pops between
  frames 2 and 3, a "fading" effect that ends at full opacity, trails pointing in
  different directions, registration that walks a growing burst across the
  screen. Finding 10 measures the same decay inside a single row.
- **Lab-space palette matching.** The difference does not survive art generated
  at high resolution and downscaled.
- **Phase-correlation registration.** It aligns whole images, and a pose that
  changes shape is not a translation of the one before it.
- **Gating on the motion report.** See finding 5.
- **Automatic mirrored facings.** Genuinely a transform and worth having. It
  interacts with the anchor policy, since the anchor is pinned to the cell centre
  so a mirrored sheet stays symmetric.

## What shipped

| File | Change |
|---|---|
| `bgate_core/spritekit.py` | New. Anchor registration, palette locking, connected components, silhouette overlap, motion report, sheet layout. Later: row and sheet auditing (`row_report`, `draw_guides`). |
| `bgate_core/animspec.py` | New. Ten archetypes with key poses, holds, loop and ping-pong. |
| `bgate_adapters/sprites.py` | Anchor registration, reach-based fit, per-frame durations, ping-pong order, per-animation loop and fps, grid layout and padding, motion report, histogram-based mass. |
| `bgate_mcp/server.py` | `sprite_plan` and `sprite_sheet_check` (new, free). `image_sprites` gains `anchor_views`, `archetypes`, `view`, `palette_lock`, `palette_colors`, `sheet_padding`; reports and logs `motion` and `palette`. |
| `bgate_core/seats.py` | Art brief: the model sheet, `ideogram-3`'s character-reference field, call `sprite_plan` first, read the `motion` block, rule 2 corrected. |
| `tests/test_spritekit.py` | New. 35 tests, including the registration table above. |

**Rule 2 of the art brief was wrong.** It said "NEVER CONDITION FRAME N ON FRAME
N-1", while `image_sprites` has conditioned on the previous frame, on top of the
anchor, since anchor+rolling landed. The rule now says what is
true: the pin is in every call, which is what stops the decay; the previous frame
rides on top, which keeps motion continuous; and the previous frame must never be
the only reference.

## Sources

Generation side:

- [Character reference vs style reference, and multi-angle conditioning](https://oakgen.ai/blog/ai-character-consistency-guide): two to three distinct angles beat more same-angle references.
- [Reference images and staying on-model for game characters](https://sorceress.games/blog/ai-character-generator-stay-on-model-with-reference-images-2026): the five-view model sheet.
- [Character turnaround sheets](https://multipleangles.app/use-cases/character-turnaround-generator).
- [Sprite Sheet Diffusion](https://arxiv.org/html/2412.03685v2): identity and pose as distinct conditioning channels.
- [Seed behaviour in diffusion pipelines](https://getimg.ai/guides/guide-to-seed-parameter-in-stable-diffusion) and [character consistency guidance](https://thinkpeak.ai/stable-diffusion-character-consistency-tutorial/).
- [Video-model in-betweening for sprite work](https://neatforge.com/guides/how-to-create-sprite-sheets-from-video-for-game-devs/) and [Scenario's spritesheet workflow](https://help.scenario.com/en/articles/create-spritesheets-with-scenario/).
- [kie.ai file upload API](https://docs.kie.ai/file-upload-api/upload-file-base-64): base64 upload, three-day retention.

Assembly side:

- [Godot `SpriteFrames` docs](https://docs.godotengine.org/en/stable/classes/class_spriteframes.html): per-frame relative `duration`.
- [godot-proposals#11698](https://github.com/godotengine/godot-proposals/issues/11698): ping-pong loop mode, still open.
- [Texture atlas padding and extrusion](https://webglfundamentals.org/webgl/lessons/webgl-qna-how-to-prevent-texture-bleeding-with-a-texture-atlas.html).
- [Sprite jitter and alpha-weighted centroid alignment](https://dev.to/grace_lungu_bae72c2681d25/sprite-jitter-in-pixel-art-heres-a-real-fix-alignment-beforeafter-from-sprite-studio-10o): the 27.2px to 0.2px measurement behind finding 1.
- [Basic four-pose walk cycles](http://www.cmuleeper.com/_classes/ART207_introToAnimation/207_assignments/02_walkCycles/1.%20Basic%204%20Pose%20Walk%20Cycles.htm) and [walk cycle key poses](https://animation.monmouth.edu/instruct/animation/walk-cycle/).
- [Walk cycle timing](https://en.wikipedia.org/wiki/Walk_cycle).
- [Gerstner et al., *Pixelated Image Abstraction*](https://gfx.cs.princeton.edu/pubs/Gerstner_2012_PIA/Gerstner_2012_PIA_full.pdf): palette-constrained quantisation for pixel art.
- [Reference-locking and consistency scoring in current sprite tooling](https://www.seeles.ai/resources/blogs/how-we-create-ai-sprite-sheets): including the grid-generation advice this project rejects.
