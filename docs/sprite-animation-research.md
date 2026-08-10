# Sprite sheets that animate: what was missing, and what was measured

2026-08-10. Research plus the changes it produced, against 0.1.35. Written
because the painted sprite path was close to right and kept producing sheets
that passed every gate and still looked wrong in play.

The short version: 0.1.35 could prove a sheet was **the same character**. It had
almost nothing that could prove the sheet was **a character moving**. Those are
different questions, they fail in different ways, and the second one is what a
player actually sees.

---

## What 0.1.35 already had, and it is a lot

Worth stating first, because the changes below are additions to a working
pipeline rather than a rescue of a broken one.

| Mechanism | Where | What it guarantees |
|---|---|---|
| Reference-first generation | `image_sprites` | Every frame is an edit against ONE approved anchor, so identity re-grounds each call instead of decaying down a chain. |
| Anchor + rolling conditioning | `image_sprites` | The anchor is always present; the previous frame and (for a closing frame) the cycle's first ride along on top, so motion is continuous without the chain compounding. |
| Keyable-background contract | `chroma.py` | Alpha is manufactured and audited rather than requested, because neither provider actually returns usable alpha. |
| Alpha cut audit | `chroma.audit` | Border bleed, white halo, feathered alpha, dirty alpha, hollow interiors, residual chroma. |
| Vision identity judge + palette histograms | `_vision_consistency` | Per-frame identity score, frame-to-frame outlier detection, palette cohesion against the batch median. |
| Area-anchored scale | `sprites.from_pose_images` | Draw-size drift removed by normalising each frame to constant visual mass. |
| Spend and deadline ceilings | `image_sprites` | Priced before buying, re-checked before every pose. |

Every one of those is about a **frame**. The gap is everything that is only
visible **between** frames.

---

## The gap, stated as a failure that passes every gate

Four frames named `walk/0` … `walk/3`, described as "walking", "walking, left
foot forward", "walking", "walking, right foot forward".

That set assembles perfectly. The identity judge scores it high — it is
obviously the same character. The palette holds. The cut is clean. Nothing in
0.1.35 rejects it, and nothing should, because nothing is *wrong* with any
individual frame. It is simply not an animation: it is four drawings of a person
standing near a walk, and in play the character slides along the floor.

That is the shape of the whole gap. Everything below is an instance of it.

---

## Finding 1 — the registration bug: bounding boxes are not bodies

`from_pose_images` centred each frame horizontally on its **alpha bounding
box**. A jab widens the bounding box to the right, so box-centring shifts the
**torso** left to compensate, and the fighter takes a visible sideways step
every time he punches.

The literature on this is unambiguous and the fix is the alpha-weighted
centroid: the torso is most of the ink, so it dominates the centre of mass and
stays put whatever the limbs do. Published measurements on that substitution
report box-centred drift around 27px of standard deviation against ~0.2px
centroid-pinned.

Reproducing it here gave a more interesting answer than expected. Synthetic:
one torso that is **identical pixels** in both frames, plus one arm of constant
mass, folded across the body in the first frame and thrown out to the side in
the second. Any apparent movement of the torso is registration error.

| Strategy | Torso moves |
|---|---|
| Bounding-box centre (0.1.35) | **44.5px** |
| Alpha-weighted centroid | 6.0px |
| Weighted median column | 3.5px |
| **Core-column median (shipped)** | **1.0px** |

The centroid is a mean, and a mean is dragged by a tail. An outstretched limb
*is* a tail in the horizontal mass distribution — a small fraction of the ink
placed a long way from the body — which is the exact input shape a mean handles
worst. The median ignores the tail's *distance* but still counts its *mass*, so
a heavy arm still moves it a little. Dropping columns holding less than 25% of
the tallest column's ink removes the limb from the vote entirely, and what
remains is the torso: which is what an animator means by the centre line, and is
why they draw one.

Vertical registration deliberately did **not** change. Feet belong on a ground
line and a mass pin would float a crouch. X from mass, Y from the floor.

**A second bug fell out of fixing the first.** The set's fit scale was computed
from the widest *bounding box*. Under anchor registration the room a pose needs
is twice its furthest **reach from the anchor** — a jab extending 90px right of
the body and 30px left needs 180px of cell, not the 120px its box measures.
Fitting the box instead left the wide pose unable to sit where its anchor said,
the placement clamp dragged it back inside the cell, and the body landed off
centre by exactly the amount the clamp moved it. Nine of the surviving 12.5px in
an early measurement were the clamp, not the anchor.

`spritekit.anchor_x`, `spritekit.place_offset`, and the reach-based fit in
`sprites.from_pose_images`. `tests/test_spritekit.py::TestRegistration` rebuilds
that synthetic and fails if the ordering of those four numbers ever inverts.

---

## Finding 2 — palette drift was detected, and detection is the expensive option

0.1.35 computes 4-bit-per-channel histograms and scores each frame's cohesion
against the batch median, flagging outliers for re-roll. It works. It also costs
one image call per failure, and it only ever *reduces the probability* of drift.

Quantising every frame to the reference's own palette makes drift
**unrepresentable**: a colour the character does not have has nowhere to be
stored. Detection becomes prevention, and the re-rolls it would have bought are
not spent. This is the same inversion `vfx.py` already argues for — the model
draws, arithmetic holds identity over time.

**It is a posteriser, so it is not free everywhere.** On flat, cel-shaded,
limited-palette and pixel art it is invisible. On smoothly rendered painterly art
it bands the shading, and no palette size fixes that in general — it only moves
the banding. So `palette_lock` defaults to `"auto"`, which measures whether the
reference's ink lives on a small number of colours and switches on only when it
does. The result says which way it went and why.

Nearest-colour matching is done in RGB via Pillow's C quantiser rather than in
Lab. Lab is perceptually better and the difference does not survive contact with
art that was already generated at 1024×1536 and downscaled; the C path is roughly
three orders of magnitude faster than the equivalent Python loop, and that
mattered more.

**One bug worth recording**, because it shipped inverted and every downstream
gate would have called the result clean. A P-mode palette always holds 256
entries and Pillow searches all of them, so zero-filling the tail puts pure black
in the palette — and black wins the nearest-colour vote for anything mid-tone.
Measured on a red reference and a drifted green frame: green sits 206 from black
and 226 from the red it should have snapped to. "Lock this frame to the
character's colours" turned it black. The padding now repeats the last real
colour. `tests/test_spritekit.py::TestPaletteLock` covers it.

---

## Finding 3 — Godot has carried per-frame timing all along and we emitted 1.0

Godot 4's `SpriteFrames` gives every frame a relative `duration`: a frame at 2.0
holds twice as long as one at 1.0, and absolute duration is
`duration / (fps * playing_speed)`. The emitter in 0.1.35 wrote a literal `1.0`
for every frame ever produced by this project.

A uniform hold is the flattest possible reading of any action. It is the reason a
generated punch reads as four pictures of a punch rather than as a punch: in
hand animation the impact frame is **held** while the anticipation is **rushed**,
and that contrast is the feeling of a hit landing.

`timing` now carries per-animation `holds`, `order`, `loop` and `fps` into the
emitter. Per-animation `fps` matters more than it sounds: a 6fps idle and a 12fps
attack on one sheet is normal, and a single sheet-wide speed is a compromise
between two right answers.

---

## Finding 4 — ping-pong is "derive the rest" applied to timing

The art brief's first rule is generate the minimum and derive the rest. There was
no mechanism behind it for 2D character work.

Three drawings played `0, 1, 2, 1` are a four-step cycle. It costs three
generations instead of four, and it **cannot have a loop seam**, because the
wrap-around pair is a genuine adjacent pair. That is exactly what a breathing
idle, a hovering pickup or a bobbing NPC wants.

Godot has no ping-pong loop mode — the engine proposal for one is still open — so
it is baked into the emitted frame list, which is the right place for it anyway:
the plan belongs in the resource, not in the gameplay code that plays it. The
endpoints are not repeated (`0,1,2,1`, not `0,1,2,2,1,0`); the doubled endpoint
is the usual hand-rolled mistake and it stutters visibly at any speed.

---

## Finding 5 — four faults nothing could see, all of them silhouette overlap

Height jitter was the only cross-frame measurement in 0.1.35. These four are what
actually gets noticed, and every one of them is perfectly on-model, so every
identity score above the floor is compatible with all of them:

- **duplicate** — two adjacent frames are the same drawing. The animation holds
  still there and one generation was bought for nothing.
- **pop** — two adjacent frames share almost no silhouette. The character was
  redrawn, not moved.
- **open_loop** — the last frame does not flow into the first. Cheap to miss and
  impossible to unsee: it hitches once per repetition, forever.
- **detached** — the silhouette is in more than one piece. The key bit through a
  wrist and a hand is floating beside an armless character. Every existing audit
  inspects the *cut* — border, fringe, alpha under zero, enclosed holes — and not
  one of them asks whether this is *one figure*.

The first three are intersection-over-union of the alpha masks of registered
frames, which is a statement about the pose and nothing else once registration is
fixed. The loop check is **relative** to the animation's own mean adjacent
overlap, not an absolute floor: a fast run legitimately has low overlap
everywhere and an idle legitimately has high overlap everywhere, so an absolute
threshold would flag every run and pass every idle. One-shots (death, ko, fall)
are excluded outright.

`detached` also separates **parts** (blobs holding ≥2% of the ink) from
**speckles** (everything smaller), because they are different problems with
different fixes: parts above one usually means the key colour appeared inside the
art, while dozens of speckles means the backdrop was never flat.

**These are advisory, not a gate**, and that is deliberate. A duplicate frame is
fixed by a different pose description, not by re-rolling the same one, so
spending a retry on it would buy the same frame again. The art brief's eighth
rule applies: an audit that fires on everything gets switched off, which is worse
than never having had it. They are reported loudly — in the result, in the
artifact metadata and in the log line — and they are the agent's to act on.

---

## Finding 6 — nothing knew how a walk is built

`image_sprites` animates whatever poses it is handed, so the quality of a
character's animation was decided by whether the driving agent happened to
remember how a walk cycle works. That is the failure at the top of this document.

Animation has had the answer since the 1930s and it is not a matter of taste:

- A **walk** is CONTACT, DOWN, PASSING, UP, once per leg. The body rises and
  falls twice per cycle and that bob is what a walk *is*. Four frames with no
  height change is a character sliding along the floor. The two-pose version
  (contact and passing only, four frames total) is the standard for small
  sprites, where the recoil is a pixel or two and does not survive the downscale.
- A **run** is a walk with a **flight frame** — the moment neither foot touches.
  If every frame has a foot down it is a fast walk whatever the fps says, and
  players read the difference immediately.
- An **attack** is ANTICIPATION, CONTACT, FOLLOW-THROUGH, RECOVER, with the
  impact held and the wind-up rushed.
- A **death** is a one-shot whose last frame holds longest, because it is the
  frame that stays on screen.
- A **jump** holds longest at the apex, which is both physically true (least
  vertical speed) and what makes a jump feel like it hangs.

`bgate_core/animspec.py` encodes ten archetypes as pose descriptions plus timing,
written to name limb positions and weight rather than adjectives — "leaning back"
is a pose, "more dynamic" is not, and the second one returns a different
character. `sprite_plan` hands the plan and its price back for free; passing
`archetypes=[...]` to `image_sprites` runs exactly it.

---

## Finding 7 — a long sheet was a texture that would not upload

`_stitch` built a single horizontal strip with no width limit. Desktop GL and
Vulkan allow 16384px, but mobile and web commonly cap at 4096, and a texture over
the limit does not warn — it fails to upload and the sprite draws as nothing. A
thirty-frame character at 160px wide is 4800px, so this is the second sheet
anyone builds.

Sheets now wrap into a grid past a conservative 4096px and take a one-pixel
transparent gutter when they do, because a gridded sheet has vertical neighbours
and a sprite drawn at a non-integer scale samples across its region edge. Short
sheets are still a plain strip with no padding: that is what every existing sheet
and every existing region assertion is, and a layout change nobody asked for is a
re-import of every character in the project. `sheet_padding` is available for
projects that draw at non-integer scale with linear filtering.

The gutter is transparent rather than an extrusion of the edge pixel. Sprite
cells are alpha-trimmed with a margin, so what bleeds in from a transparent
gutter is nothing — which is the intended result, and is not true of extrusion,
which would bleed the sprite's own edge colour outward into a halo.

The layout is **returned** by the stitcher and passed to the emitter rather than
recomputed, because the only way a sheet and its `.tres` can disagree is if one
of them derives the geometry independently.

---

## What was considered and deliberately not built

- **Grid generation** (asking for a 3×3 of poses in one image so the model keeps
  palette and proportions consistent because it drew them all at once). This is
  the most-recommended technique in the current tooling literature and it is
  already banned here, on measured evidence: `imagegen._reject_multi_pose`
  refuses it and `vfx.py` documents the batch of twenty that produced it — a mug
  that shattered over three frames and was intact in the fourth, palettes that
  popped mid-sequence, trails pointing in different directions, fourteen of
  twenty unusable. The reference-first path costs more per frame and is the
  reason this pipeline works. Not revisited.
- **Lab-space palette matching.** Better in principle; the difference does not
  survive art generated at high resolution and downscaled, and RGB keeps the
  quantisation inside Pillow's C path.
- **Phase-correlation registration** between frames. Real technique, and the
  wrong tool here: it aligns whole images, and a pose that genuinely changes
  shape is not a translation of the one before it. The anchor is per-frame and
  needs no neighbour.
- **Gating on the motion report.** See Finding 5.
- **Automatic mirrored facings.** Genuinely a transform, genuinely worth having,
  and it interacts with the anchor policy (the anchor is pinned to the cell
  centre precisely so a mirrored sheet stays symmetric). Not built tonight.

---

## What shipped

| File | Change |
|---|---|
| `bgate_core/spritekit.py` | New. Anchor registration, palette locking, connected components, silhouette overlap, motion report, sheet layout. |
| `bgate_core/animspec.py` | New. Ten archetypes with key poses, holds, loop and ping-pong. |
| `bgate_adapters/sprites.py` | Anchor registration, reach-based fit, per-frame durations, ping-pong order, per-animation loop and fps, grid layout and padding, motion report, histogram-based mass. |
| `bgate_mcp/server.py` | `sprite_plan` (new, free). `image_sprites` gains `archetypes`, `view`, `palette_lock`, `palette_colors`, `sheet_padding`; reports and logs `motion` and `palette`. |
| `bgate_core/seats.py` | Art brief: call `sprite_plan` first, read the `motion` block, and rule 2 corrected. |
| `tests/test_spritekit.py` | New. 34 tests, including the registration table above. |

**Rule 2 of the art brief was wrong.** It said "NEVER CONDITION FRAME N ON FRAME
N-1", while `image_sprites` has deliberately conditioned on the previous frame —
on top of the anchor — since anchor+rolling landed. The rule now says what is
actually true and why it is safe: the pin is in every call, which is what stops
the decay; the previous frame rides on top, which is what keeps motion
continuous; and the previous frame must never be the only reference.

---

## Sources

- [Godot `SpriteFrames` docs](https://docs.godotengine.org/en/stable/classes/class_spriteframes.html) — per-frame relative `duration`.
- [godot-proposals#11698](https://github.com/godotengine/godot-proposals/issues/11698) — ping-pong loop mode, still open.
- [Texture atlas padding and extrusion](https://webglfundamentals.org/webgl/lessons/webgl-qna-how-to-prevent-texture-bleeding-with-a-texture-atlas.html) — why a gutter, and what bleeds without one.
- [Sprite jitter and alpha-weighted centroid alignment](https://dev.to/grace_lungu_bae72c2681d25/sprite-jitter-in-pixel-art-heres-a-real-fix-alignment-beforeafter-from-sprite-studio-10o) — the 27.2px → 0.2px measurement that prompted Finding 1.
- [Basic four-pose walk cycles](http://www.cmuleeper.com/_classes/ART207_introToAnimation/207_assignments/02_walkCycles/1.%20Basic%204%20Pose%20Walk%20Cycles.htm) and [walk cycle key poses](https://animation.monmouth.edu/instruct/animation/walk-cycle/) — contact, down, passing, up.
- [Walk cycle timing](https://en.wikipedia.org/wiki/Walk_cycle) — steps per second and what frame counts read as.
- [Gerstner et al., *Pixelated Image Abstraction*](https://gfx.cs.princeton.edu/pubs/Gerstner_2012_PIA/Gerstner_2012_PIA_full.pdf) — palette-constrained quantisation for pixel art.
- [Reference-locking and consistency scoring in current sprite tooling](https://www.seeles.ai/resources/blogs/how-we-create-ai-sprite-sheets) — including the grid-generation advice this project rejects, and why.
