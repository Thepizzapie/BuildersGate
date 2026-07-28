# FAQ

2026-07-27. These are real questions people asked after seeing this, kept close
to how they asked them. Answers are checked against the code, and where the
answer is "less than you were hoping", it says that.

Terms you do not recognise are in the [glossary](glossary.md). If you have not
read [start-here.md](start-here.md), start there.

- [A lot of these terms sound like a different language](#a-lot-of-these-terms-sound-like-a-different-language)
- [What do you mean by custom MCPs?](#what-do-you-mean-by-custom-mcps)
- [How do you create those agents? I've never done anything like that](#how-do-you-create-those-agents-ive-never-done-anything-like-that)
- [So you used GPT to make images and then bots turned them into game assets?](#so-you-used-gpt-to-make-images-and-then-bots-turned-them-into-game-assets)
- [How did you get decent looking characters? Mine came out Roblox-looking](#how-did-you-get-decent-looking-characters-mine-came-out-roblox-looking)
- [Did you use AI for your whole entire game?](#did-you-use-ai-for-your-whole-entire-game)
- [Will this work for a 3D game?](#will-this-work-for-a-3d-game)
- [My maps and levels come out terrible](#my-maps-and-levels-come-out-terrible)
- [Can I point it at my existing game and ask what's missing?](#can-i-point-it-at-my-existing-game-and-ask-whats-missing)
- [I stopped because it was too slow. Any tips?](#i-stopped-because-it-was-too-slow-any-tips)
- [What does this actually cost?](#what-does-this-actually-cost)
- [I use ChatGPT to write prompts and paste them into Claude. Is that wrong?](#i-use-chatgpt-to-write-prompts-and-paste-them-into-claude-is-that-wrong)
- [What is this actually bad at?](#what-is-this-actually-bad-at)

---

## A lot of these terms sound like a different language

That is a fair complaint and mostly the fault of the people using them. The
whole vocabulary is about a dozen words, and most of them are ordinary words
being used narrowly:

| Word | What it means here |
|---|---|
| MCP server | A local program that gives Claude extra tools |
| seat | A job title an agent takes on for a session (there are seven) |
| lane | The folders a given seat is allowed to write to |
| lock | A claim on one file so two agents can't both edit it |
| work item | A row in a to-do list: a seat, a title, a description |
| dispatch | Actually starting an agent on a work item |
| the bible | Your design document, stored as fields instead of prose |
| cut line | The line in your plan below which you are not building |
| pin | An approved image the art has to stay consistent with |
| chroma key | Green-screening a generated image to get transparency |

The [glossary](glossary.md) has all of them with a sentence or two each. Nothing
below assumes you memorised any of it.

---

## What do you mean by custom MCPs?

MCP (Model Context Protocol) is a standard for giving an AI assistant tools that
live outside itself. Out of the box, Claude Code can read files, write files,
and run shell commands. That is its entire vocabulary. An MCP server adds verbs
to it.

"Custom MCP" is not a special technology. It means somebody wrote a server for
their own problem instead of using one off the shelf. Builders Gate is one: 78
tools for game development, running as a local process on your machine, no
account and no cloud. Once registered, any Claude session can call
`godot_run` or `image_sprites` the same way it calls "read a file".

You register it once:

```bash
claude mcp add builders-gate --scope user -- <absolute-python-path> -m bgate_mcp.server
```

Use the absolute path to python. The Claude CLI resolves a bare `python`
differently than your shell does, and will report "failed to connect" against a
server that runs fine.

The reason to bother: without it, every session starts from nothing and you
re-explain your game. With it, an agent calls `seat_brief("art")` and gets the
mission, the design bible, the approved reference images, and who currently
holds which files, in one call, from a database that persists.

---

## How do you create those agents? I've never done anything like that

You do not create them, and that is the part that sounds harder than it is.

There are seven fixed **seats**: director, narrative, gameplay, tech, art, audio,
qa. There will always be seven. A seat is an identity a session *adopts*,
not a process you build. Adopting it is one environment variable:

```bash
BGATE_SEAT=art
```

That is the whole mechanism. A session with that variable set inherits the art
seat's mission, its writable folders, and a one-call briefing. There is no agent
framework to learn, no per-task registration, no YAML.

"Multiple agents on different parts of the game" is then just: several Claude
sessions, each with a different `BGATE_SEAT`. Either you open several terminals
yourself, or you file work items in the dashboard and click Dispatch, which
spawns a `claude` process per item with the right variables already set.

The part that makes it *safe* rather than chaotic is the two gates:

- **Lanes.** Each seat has an allowlist of paths. Art writes
  `game/assets/**`, `blender/**`, `art/**`. Gameplay writes `game/scripts/**`
  and `game/scenes/**`. A PreToolUse hook checks every Write and Edit against
  this and blocks the ones that fall outside. Unknown seats fail closed.
- **Locks.** Text files merge; a `.png` or a `.blend` does not. An agent claims a
  binary before editing it. A second agent asking for a held lock gets an error
  naming the holder, not a silent wait.

Run `bgate hook-install .` then `bgate hook-status .`. The second one is the only
thing that proves the enforcement is actually live, and it exits 1 if it is not.

Honest caveat: more agents is not automatically faster. See
[the speed question](#i-stopped-because-it-was-too-slow-any-tips).

---

## So you used GPT to make images and then bots turned them into game assets?

Roughly, but the interesting half is the part after "make images", and calling
it "turning images into assets" undersells how much of the work that is.

The image models produce a PNG. What a game needs is a sprite sheet with
transparent backgrounds, consistent character size across every frame, a
consistent baseline so the character does not bob when the animation plays, and
a Godot `SpriteFrames` resource so it drops into an `AnimatedSprite2D`. Between
those two things sits most of the pipeline:

1. **A key colour is chosen against the character's own palette.** No image
   model returns usable transparency. Measured, 2026-07-25: gpt-image-1 called
   with `background="transparent"` came back with a brown gradient behind the
   character, and where it does work it punches the whites of the eyes into
   holes. Krea has no transparency parameter on any model at all. So alpha is
   not requested, it is manufactured. The model is made to paint a flat saturated
   backdrop in a colour the art never uses, and that gets keyed out.
   A character in a green shirt must never get a green screen, which is why the
   colour is picked by measuring distance from the art's actual palette rather
   than defaulting.
2. **The key gets audited.** Background bleed, white halo, feathered edges,
   colour still sitting under transparent pixels, holes eaten out of the middle
   of the figure. Each is a measured number with a threshold. A frame that
   fails is a named failure, not a sprite with dirty alpha that poisons
   everything downstream.
3. **The frames get normalised.** Each is alpha-trimmed, scaled to the
   *reference character's* visual mass rather than the batch median, and
   bottom-centered. The batch median follows the crowd: if several frames drift
   large, the median goes large and the good frames get upscaled to match the
   bad ones. Anchoring to the reference makes the set converge instead.
4. **The sheet and the `.tres` are written**, with one animation per pose.

Then a human looks at it. Nothing auto-lands.

---

## How did you get decent looking characters? Mine came out Roblox-looking

This is the question everyone asks, so it gets the long answer.

**The short version: the answer is a process, not a tool.** There is no model in
this repo you do not already have access to. What is here is a discipline around
them, and the discipline is where the difference lives.

### Why handing Claude a reference image produces garbage

Three separate failures, and they compound:

**One: you probably used the wrong model, and it was not "worse", it was
incapable.** This was measured head-to-head: the same prompt, the same anchor,
the same four-frame duck animation ([`bgate_core/tiers.py`](../bgate_core/tiers.py)):

```text
z-image        $0.003   4.5s   ONE giant portrait, ignored "4 frames" entirely
flux-1-dev     $0.007  10.0s   4 frames in a row, but nobody ducks; props hallucinated
krea-2-medium  $0.030  20.3s   4 frames, real crouch arc, character holds
krea-2-large   $0.065  31.6s   same, slightly cleaner
gpt-image-1    $0.042  28.3s   3 frames, one cropped off-canvas, garbled clipboard text
```

The load-bearing lesson: **the cheap models do not fail on polish, they fail on
capability.** z-image cannot lay out a sprite sheet at any price. A naive
cheap-to-expensive ladder would quietly hand you a portrait when you asked for
an animation, and you would conclude that AI sprite art does not work.

**Two: identity lived in prose.** If the character is described in a sentence,
then every generation re-imagines that sentence, and so does every human
correcting it. In a real production run here, an orchestrator issued a
confidently *wrong* correction, describing the character from stale prompt text
instead of from the approved reference. The art agent re-checked the pinned
reference, refused the instruction, and escalated. It was right. That incident is
the origin of everything below
([`docs/character-consistency.md`](character-consistency.md)).

**Three: each frame was generated independently.** Generate twelve frames from
one prompt and you get twelve different characters wearing the same adjectives.

### What is actually done instead

**Pin the reference first.** One approved image is copied into `.bgate/refs/`
under a name (`ref_pin`). From then on that name is passed anywhere a path is
accepted, it shows up in every seat's briefing, and it is what everything is
measured against. The pinned reference is canon: a correction that contradicts
it requires *re-pinning*, which is a deliberate act, not a sentence someone
types mid-flight.

**Write the character down while looking at it.** `profile_set` stores traits,
style, and a negative list of things that must never appear, authored while
looking at the pinned image, never from memory. That profile is injected
automatically into every generation for that character. Nobody types identity
prose again, so the orchestrator failure above becomes structurally impossible.

**Gate the anchor before buying anything else.** The first thing generated is one
canonical character. It is checked before a single pose is paid for: did the
backdrop key cleanly, and is this a cut-out figure rather than a filled frame or
a near-empty one? A broken anchor makes every pose broken, every pose gets
retried, and the run costs roughly 2N against garbage. Catching it here caps the
damage at one image.

**Derive the poses; do not generate them.** Every pose is an *edit* conditioned
on images, one frame per call, always carrying:

- the **anchor**, always, so identity re-grounds on every call and drift cannot
  compound telephone-style;
- the **previous successful frame**, for motion continuity;
- for the last frame of a cycle, that animation's **first** frame, so walk/2
  flows back into walk/0 and the loop does not pop.

The prompt is blunt about what may change: *keep the exact same body build,
musculature, height, weight, head size and limb proportions in every frame; do
not slim him down, bulk him up, or restyle the body between frames; only the
pose changes.* The agent describes the *stance*, meaning limb positions, never
anatomy.

**Key and audit every frame** (see the previous answer). Then normalise size and
baseline against the reference, so the character does not grow and shrink
mid-swing.

**Check consistency from a built comparison, never from memory.**
`consistency_check` composes reference and candidate side by side over a
checkerboard, attaches the profile's trait checklist, and requires the reviewer
to verdict every line from that one view. It exists because three off-style
batches were approved by agents judging frames in isolation.

**Let a human approve it.** An independent reviewer can *fail* a candidate
outright, because refusing to ship is a call a machine can make alone. A passing
verdict only records evidence. The revision stays a candidate until a person
promotes it.

### What this does NOT do, stated plainly

- **It does not make a bad reference good.** Everything downstream holds the
  anchor. If the anchor is Roblox-looking, you get a consistent set of
  Roblox-looking frames. The anchor is the one image worth iterating on by hand,
  at the highest tier, until you actually like it. Then pin it.
- **It cannot tell you the art is good.** The art-direction check measures the
  result against what your bible wrote down. It measures disagreement, not
  taste.
- **No automatic identity metric gates anything.** This was tried and reported
  honestly: palette distance separates colour drift only and is blind to
  identity. CLIP scored 0.91 to 0.92 for *everything*, no separation at all.
  Unicom separated characters cleanly (same character 0.66 to 0.83,
  cross-character 0.40 to 0.51) but extreme poses overlap the floor, and a duck
  scored 0.57. So it is a tripwire that flags for review, never an accept/reject. The reliable
  detector is still structured visual judgment against an explicit checklist.
- **It is 2D raster only.** The profiles, the pins, the tripwires and the chroma
  path all operate on flat images. Nothing analogous exists for meshes.

### If you take one thing

Iterate hard on **one** image until it is right. Pin it. Write the profile while
looking at it. Then never describe the character in words again.

---

## Did you use AI for your whole entire game?

The honest answer, from what this repository actually documents rather than what
would sound good:

The reference production run, the one the docs were written after, was **a
complete arcade fighter built by roughly 30 seat agents over two days**
([`docs/gap-analysis.md`](gap-analysis.md)). The agents wrote the GDScript, made
the art, and ran the tests. There is nothing in this repo describing a
commissioned human artist or hand-authored assets, and the art pipeline
documented above is clearly the one that produced the character frames.

But "AI did the whole game" is the wrong shape for what happened, and the same
document is blunt about why:

- **A human was the director and the only approver throughout.** Nothing lands
  without a person promoting it. That is enforced, not a habit.
- **The human supplied concept references.** `ref_pin`'s own documentation lists
  "concept mocks from the user" as a thing you pin. Those are inputs, not
  outputs.
- **The human was the only oracle for feel.** A balance wave passed 98
  deterministic assertions and a scripted acceptance simulation, shipped, and
  the user judged it *"an hour of nothing work."* Tests, sims and screenshots
  measure correctness. Feel has exactly one judge.
- **The human caught what the pipeline missed.** An entire six-frame batch
  drifted off-model and was caught by the user, not by any gate. Most of the
  consistency machinery described above exists *because* of that.
- **Roughly a quarter of total agent effort was recovery**, not production. Six
  agents were killed mid-flight by conversation interrupts across the run, and
  each death cost a successor doing forensics on what its predecessor half-did.
  One death left a parse error live in the repo; one lost a generated portrait.

So: the artifacts were largely machine-made, under continuous human direction,
with the human doing the judging that the machines demonstrably could not. If
you are asking "can I sit back and get a game", no. If you are asking "can the
production work be machine work while you direct and approve", that is the thing
this is built for.

One thing that is *not* documented anywhere: what that run cost in dollars. See
[cost](#what-does-this-actually-cost).

---

## Will this work for a 3D game?

Partly, and less than you want. Read this before you plan a weekend around it.

### The blunt version

**Builders Gate does not generate 3D models.** There is no text-to-3D and no
image-to-3D anywhere in it. What the Blender adapter does is run bpy Python that
*your agent wrote* and tell it precisely what came out. The entire 3D authoring
loop is: the agent emits Python, `blender_run` executes it headless, the agent
reads back tri counts, UV warnings, materials, game-readiness issues and
optionally a render, the agent edits its Python, repeat.

That loop is good. Structured measurements beat a subprocess log, and an agent
that cannot see what it built will confidently produce nothing. But the
*modeling* is an LLM writing `bpy.ops.mesh.primitive_*` calls and transforms, and
that has the quality ceiling you would expect.

If you were hoping to hand it a character reference and get a rigged mesh, that
is not here and is not close.

### What does exist for 3D, and works

- **A runnable 3D template.** `bgate init <name> --kind 3d` gives a first-person
  slice: `CharacterBody3D` with mouse-look, ground, a block to jump on, coyote
  time, and the same telemetry and F1 live-tuning autoloads the 2D template has.
  The shared autoloads are dimension-agnostic, so 3D gets full parity there.
- **glTF export with modifiers applied.** Blender's exporter defaults that off,
  which silently ships the un-beveled base mesh and makes an asset look right in
  Blender and wrong in the engine. `blender_export_gltf` applies them.
- **Verified import.** `godot_import_asset` does not trust the file. It loads
  the resource inside a real headless Godot and reports the mesh the *engine*
  built. A `.glb` that imports with zero surfaces is a silent failure; checking
  tri counts on both ends catches it. Measured end to end: a beveled shard came
  out 106 tris in Blender and 106 tris in Godot, UVs and material intact.
- **Game-readiness warnings** from the export: no UVs, n-gons, unapplied or
  non-uniform scale. Each is cheap here and expensive to debug in-engine.

### Where 3D is thinner than 2D, specifically

- **No gear or equipment system.** The whole equip/layer/paperdoll pipeline
  (`fighter.tscn`, `gear_rig.gd`, the item tools) is 2D-hardcoded. A 3D project
  gets none of it. Per-frame worn gear that deforms with the body is
  [explicitly deferred](gear-pipeline.md), "out of scope until the equip system
  above is carrying real combat".
- **The art seat's whole workflow briefing is 2D.** Its mission mentions the
  Blender tools, but the step-by-step workflow it hands an agent is sprite
  sheets, pose indices, and alpha-halo checks. The seat that is supposed to own
  3D is briefed almost entirely in 2D.
- **The 3D template has no character mesh.** It is first person, which is a
  defensible reason, but the player node is a collision capsule and a camera.
  The 2D template ships actual visuals.
- **The 3D player has no jump buffer.** The 2D one exports both `coyote_time`
  and `jump_buffer`, with a comment noting that the absence of forgiveness
  windows "reads to players as 'the jump didn't register', which gets reported
  as a bug, not as a missing feature." The 3D player has coyote time only.
- **Almost none of the 3D path runs in CI.** The tests that prove geometry
  survives Blender to glTF to Godot are marked `slow` and/or skipped without both
  binaries installed, so they only run on a developer machine that has them. The
  3D assertions that *do* run in CI are "the template directory stamps out
  files" and "a UI node declares a gltf output port". The round trip is real and
  measured, but it is verified by hand, not continuously.
- **`blender_sprites` is a 2D tool.** It renders a Blender model down to a 2D
  sprite sheet. It is the best consistency trick in the box, since the same rig,
  camera and light produce every frame and frames cannot drift. It contributes
  nothing to a 3D game.
- **Two agents rendering at once will fight over the GPU.** There is no
  per-binary concurrency limiter. That is a known open issue.

### What a 3D user actually gets today

The project store, the seats and lanes, the cut line, the queue and dispatch,
the locks, the bible and canon, the playtest loop, publishing, and a good
Blender feedback loop for scripted geometry. What you do not get is the art
generation pipeline, which is the part people come for.

If your 3D character matters and you were going to commission it anyway,
commission it. That is not a defeat. It is the same call the gear pipeline made.

---

## My maps and levels come out terrible

There is no level generator here, and pretending otherwise would waste your
time. What exists that helps:

- **Backgrounds and tiles are separate asset kinds** with their own model
  ladders, and they are explicitly *not* chroma-keyed. A background plate with
  its background removed is nothing at all. Getting that
  distinction wrong is a common way to produce an empty file and conclude the
  tool is broken.
- **Agents can look at the result.** `godot_screenshot` shows what it looks
  like. `godot_evidence` goes further and reports where everything actually *is*:
  every measurable node as screen-pixel bounds, visibility, z-order, and for
  bars and labels its runtime value. An agent iterating on a layout blind is
  what produces the results you are describing.
- **The atlas** wires every screen to every asset it uses, derived live from
  your scenes, scripts and SpriteFrames, so you can see which screen is starving
  for art.

But the underlying reality is unchanged: an image model asked for "a baseball
field" produces a picture of a baseball field, not a playable one. Level layout
is geometry and gameplay, not art, and this tool treats it that way.

---

## Can I point it at my existing game and ask what's missing?

Yes. `bgate adopt` exists for exactly this, because `bgate init` is the wrong
tool: it unpacks a template into an empty directory and refuses to touch
anything else.

```bash
cd path/to/your/game
bgate adopt
```

Adoption is defined by what it will not do. It **never copies a template file,
never passes force to the scaffolder, and never rewrites a byte you wrote.** The
only things it puts on disk are additive: a new `.bgate/game.db`, an appended
block in `.gitignore` (so that following the README about keeping your API key
in `.env` does not commit your key), and an appended block in `CLAUDE.md`. Both
appended blocks are inside markers, so running it twice rewrites the block in
place rather than stacking a second copy. It refuses outright if anything is in
the way.

Before writing anything it reads your project and reports what it found: your
Godot version, main scene, scene and script and asset counts, your biggest
scenes, and whether the project is 2D or 3D, decided by counting `Node3D`-family
against `Node2D`-family nodes in your `.tscn` files, with the evidence returned
alongside the verdict so you can correct it.

**Then the "what's missing" part is a conversation, not a report.** There is no
gap-analysis tool. What adoption gives you is the machinery for that
conversation: write your original game plan into the bible as pillars and scope
tiers, draw the cut line, and then an agent with `seat_brief` can compare what
the bible says the game should be against what `project_status` and the atlas
say it currently is. That is a useful hour. It is not a button.

---

## I stopped because it was too slow. Any tips?

Most of what makes this slow is not the model. It is waiting on a serial loop
where one agent does everything while you watch, and then discovering an hour
later that the direction was wrong. Concretely, in rough order of payoff:

**Draw the cut line first.** The most expensive kind of slow is work that should
never have been built. Rank your scope tiers, put the line between two of them,
and the queue will refuse to file below it. The dispatcher re-checks at the last
moment before spawning, because the line moves. This is the only
mechanism here that reliably stops an agent fleet gold-plating.

**Stop watching one agent.** File several work items against different seats and
dispatch them. They run in parallel, each confined to its own lanes, each
spawned on a captured base commit so its work reads back as a diff. Default
concurrency cap is 4, which is a laptop number, not a limitation of the design.
(Full per-item git worktree isolation exists but is off by default. Set
`BGATE_GIT_ISOLATION=1` to turn it on. It is off because moving the agent's
working directory is a bigger change to a run than most projects want.)

**Cut your increments.** From the post-run analysis: mechanisms, numbers and art
landed as bundles, feedback arrived after everything, and a wrong direction cost
a full wave. Every increment should end *playable*, with you touching the game
between increments rather than between waves.

**Never let an agent tune numbers.** Split changes into mechanisms (states and
systems, which are agent work verified by tests) and numbers (never agent work
again).
Press F1 in the running game: every `@export` in the scene gets a slider bound
to the live node, moving it moves the game, no apply button, values persist and
re-apply at boot. This turned a ~60 minute feel loop into about a minute, and it
is described in the analysis as the single biggest multiplier available.

**Make the agent look at the game.** A large share of slow is an agent
confidently finishing something that does not work. `godot_check_project` and
`godot_run` after every change, `godot_screenshot` to see it, `godot_evidence`
when "looks right" is not enough.

**Commit before dispatching.** Dispatch refuses a dirty tree by default, and the
reason is worth internalising: a run started on top of your uncommitted work
produces a diff that cannot separate the agent's edits from yours, and then you
cannot undo it.

**Expect interruption tax.** Interrupting a conversation kills background
agents, and roughly a quarter of agent effort in the reference run went to
successors doing archaeology on what their predecessor half-did. Interrupting is
normal usage; just know that it is not free, and prefer steering a live run to
killing it.

---

## What does this actually cost?

Two separate bills, and the smaller one is the one that is metered.

### Image generation, priced in the code, per request

**OpenAI gpt-image-1**, per image, approximate, from
[`bgate_adapters/imagegen.py`](../bgate_adapters/imagegen.py):

| quality | USD |
|---|---|
| low | 0.011 |
| medium | 0.042 |
| high | 0.167 |

**Krea**, 14 models behind one key, priced per request, from
[`bgate_adapters/krea.py`](../bgate_adapters/krea.py). The spread is 50x:

| model | USD | note |
|---|---|---|
| z-image | 0.003 | cannot lay out a sheet |
| flux-1-dev | 0.007 | |
| imagen-4-fast | 0.021 | |
| krea-2-medium | 0.030 | the value pick for animation |
| gpt-image (via Krea) | 0.030 | |
| flux-kontext | 0.040 | |
| seedream-5-lite | 0.040 | |
| imagen-4 | 0.042 | |
| krea-2-large | 0.060 | 0.065 with style references, 0.070 with a moodboard |
| nano-banana-2 | 0.060 | |
| flux-1.1-pro | 0.060 | |
| ideogram-3 | 0.063 | 0.1575 with character references, 2.5x |
| imagen-4-ultra | 0.063 | |
| nano-banana-pro | 0.150 | |

The price changes with the *request*, not just the model. Attaching style
references costs more, and for ideogram-3 it costs a great deal more. The
estimator reads the request.

You never name a model directly. You say what you are making (`anchor`,
`animation`, `background`, `ui`) and how good it needs to be (`draft`,
`standard`, `hero`), and the ladder resolves it. It refuses to hand you a model
that cannot do the job at all, which is why the cheap rungs are not offered for
sheet work.

**Practical arithmetic:** a character sprite set is one anchor plus one edit per
pose. Eight poses at `standard` is roughly 9 × $0.065, call it $0.60. Doing that
five times before you like the anchor is $3. `image_sprites` prices the whole
plan before buying any of it and refuses if it exceeds the ceiling, and it
re-checks the running tally before every single pose, so a retry storm stops
mid-set rather than turning up on the invoice.

### Agent time, the bigger bill, and less well metered

Every dispatched agent is a `claude` process, billed by your Claude plan or API
account. In the reference run that was ~30 agents over two days with roughly a
quarter of the effort going to recovery work. **No document in this repository
states what that run cost in dollars**, and it would be dishonest to estimate one
for you. Agent time is almost certainly the dominant cost. Plan for it, not for
the image bill.

### The ceilings, and what they actually cover

Every project ships with enforcement on and these defaults, stored in the
project database:

| ceiling | default |
|---|---|
| per item | $5 |
| per day | $25 |
| per project | $250 |
| max runtime per run | 1800s |
| max concurrent agents | 4 |

They are checked *before* a process exists, and a watchdog kills a run that
passes its runtime or cost ceiling. There are no environment variables for them;
they live in the DB and are edited through the dashboard. The edit endpoint
refuses an agent, because an agent cannot widen its own gate.

**Two honest gaps in the accounting:**

- **Krea spend bought through `image_sprites` is not written to the ledger.**
  The adapter returns an estimate but does not record it, and the sprite path
  calls it without accounting. So the day and project ceilings do not see Krea
  images bought that way. OpenAI-provider calls *are* recorded. Krea spend
  through the workflow engine *is* recorded. If you run Krea sprite work, your
  ledger under-reports.
- **The ledger is a floor, not an invoice.** Prices are estimate tables, not
  reconciled usage. Agent cost is a single figure lifted best-effort from the
  Claude CLI's own report. The prompt-writer uses a flat per-call constant.
  Vision calls are unpriced. Overriding the image model with an environment
  variable does not change the price table, so the estimate silently goes wrong.

There is no `bgate spend` CLI command. Spend is visible in the dashboard only.

You need at least one image key for the art seat to do anything. Everything else
(the queue, seats, locks, the bible, the Godot loop, playtests, publishing) costs
nothing but agent time.

---

## I use ChatGPT to write prompts and paste them into Claude. Is that wrong?

Not wrong, just a manual version of two things the tool does structurally.

For **art prompts**, hand-written prompts are where character identity leaks. The
alternative here is that identity language is assembled from the stored profile
and the pinned reference automatically, and the agent contributes only the pose.
The prompt is not a thing you write well once. It is a thing that gets built the
same way every time from artifacts that were approved.

For **code prompts**, the equivalent is `seat_brief`: the mission, the bible, the
canon, the pinned references, the promoted playtest feedback routed to that seat,
and who holds which locks, in one call from a database that persists. That
is what a good hand-written prompt is trying to reconstruct from memory, except
it does not go stale and you do not have to retype it.

Keep doing whatever produces good results. Just be aware that the paste step is
where things drift, because a prompt written from memory of a decision is not the
decision.

---

## What is this actually bad at?

Kept here so you find it before you find it the hard way. The
[README's status section](../README.md#project-status) is the maintained version;
this is the beginner-facing summary.

- **3D art generation.** Does not exist. See [above](#will-this-work-for-a-3d-game).
- **Judging whether art is good.** It measures agreement with what you wrote
  down. It has no taste and does not claim any.
- **Judging feel.** The strongest gates here (tests, sims, screenshots) measure
  correctness. Feel has exactly one oracle and it is you.
- **Error surfacing in the dashboard.** Uneven. A failed mutation still
  sometimes renders as nothing happening ([ui-ux-audit.md](ui-ux-audit.md)).
- **Platform coverage.** Windows is supported and verified. Linux is
  best-effort: CI runs there but marks the job `continue-on-error`, because
  parts of the product shell out to Windows tooling. macOS is untested.
- **The audio seat.** A deliberate v1. The UI says so in a banner.
- **`bgate_engine/`.** A design note with JSON schemas and no runtime code.
  Nothing in the repository imports it, and its central claim was withdrawn
  after its own experiment came back negative. It ships because the reasoning is
  worth reading.
- **Provenance.** Most of this was proven against a small number of games on one
  Windows machine. [qa-nitpick-audit.md](qa-nitpick-audit.md) is a harsh
  self-audit of exactly that. Read its dated status header before treating any
  finding as current.
