# Surfaces reference

What each surface does, and how to call it. The [README](../README.md) is the
short version, [design-notes.md](design-notes.md) covers why the choices were
made, and [gotchas.md](gotchas.md) covers what went wrong on the way.

Verified against the source on 2026-08-12.

Three gates run throughout: a spend budget refuses an over-ceiling agent, a seat
lane refuses an out-of-lane write, and watchdogs kill a wedged run. Approval is
human-only. An agent records a verdict, it does not sign off.

## The dashboard

```bash
bgate serve [--port 7788]     # from inside a project, or BGATE_ROOT
bgate app  [--port N]         # same dashboard in a desktop window
```

It prints the URL and the project it opened. With no project it shows a
first-run screen that creates one.

Twelve views, grouped in the nav rail:

| Group | View | What it is |
|---|---|---|
| Command | Overview | Live agents, the queue, the build, a play/record panel |
| Command | Agents | Dispatch work to a seat, then watch and steer it live. A run spawns on a captured git base commit, so its work reads as per-file diffs and undoes with a scoped revert. The revert is refused if anything it touched changed since, unless you look at the diff and insist |
| Command | Settings | Every switch from one registry: dispatch, approval gate, follow-up, notifications, budget, console. Each row says whether the value is default, stored, or overridden by an environment variable. A switch that widens a safety guard (`dispatch.allow_dirty`) asks before it is turned off and records that it was |
| Build | Studio | Node editors over the existing endpoints: workflow graphs and a Godot-style game workspace. Steps queue seat work, consistency nodes carry a measured score, and a `gate` node stops the run until a human approves or rejects |
| Build | Seat workspaces | One workspace per seat. Art's shows every candidate revision beside the reference it was drawn against, two frames stacked with an opacity slider, a `difference` blend, a palette delta, batch approve-reject over a selection, and a dispatch button for an independent QA reviewer |
| Build | Playtests | Recorded sessions: video, transcript, telemetry, the director's triage, editable repro steps, and a bug report exported as markdown or as a zip with linked frames |
| Edit | Sprite editor | Direct sprite painting and frame work, full-stage |
| Edit | Audio lab | Direct audio editing, full-stage |
| Edit | 3D viewer | Inspect delivered meshes |
| Library | Assets | Immutable revisions grouped by logical asset, with an integrity audit |
| Library | Atlas | Every screen wired to every asset it uses, derived live from scenes, scripts and SpriteFrames. Click a node to file work against it. Four modes: the map, a wiring graph, a scene builder, and a code editor that lists everything a scene reaches (its resources, plus one hop through what its scripts `preload`), edits them in place, and plays the rebuilt web build in the same pane |
| Library | World bible | A write surface: pillars, core loop, constraints and references, editable in place and drag-ordered within their kind, plus the lore graph. Every narrative write runs `canon_check` first. A conflict is a 409 carrying its flags, and only a human may override it |

Brainstorm is not a rail view. It is the second mode on the console composer,
beside `dispatch`: the conversation files nothing, and Deploy shows the plan and
the agents it would dispatch before anything is queued.

Saves in the Atlas code editor are refused if the file changed on disk since the
tab opened, and refused again if a seat holds a lock on it. The previous bytes
land under `.bgate_out/edits/` either way.

The header carries a bell fed by the event table the follow-up router drives
(`bgate_core/events.py`). It reaches you only while the page is open. `bgate app`
puts the count in the window title, and one optional https webhook is the only
channel that leaves the machine.

The cockpit owns user-facing mutations: queue and dispatch, recording, feedback
disposition, bible authoring, artifact approval. Production mutations stay MCP
tools attributable to a seat. The dashboard identifies an agent session by
`BGATE_ACTOR` and refuses it the bible, the scope filing, the budget, the
revert, a workflow gate, and promoting a candidate to the build.

127.0.0.1 is not a security boundary: any page in your browser can POST to
localhost. Every mutation must be same-origin and carry a per-project bearer
token from `.bgate/ui-token`, which is gitignored and 0600. The page is served
with the token injected and `fetch` wrapped to send it same-origin only.
`BGATE_NO_AUTH=1` opts out for a scripted run. See
[SECURITY.md](../SECURITY.md).

No build step, no node, no CDN.

## Seats

Eight stable identities: director, narrative, gameplay, tech, art, audio,
cinematic, qa. A seat is an identity a working agent adopts, not a spawned
process. There is no per-task registration.

| Tool | Does |
|---|---|
| `seat_list()` | This project's seat table |
| `seat_brief(role)` | Mission, lanes, bible with the scope cut applied, canon entities, promoted feedback routed to that seat, locks, notes |
| `seat_can_write(role, path)` | The write oracle: two gates, both must pass |
| `seat_post_note(role, body, topic)` / `seat_notes(...)` | The blackboard between seats |
| `seat_configure(role, ...)` | Per-project lane and mission overrides, or disable a seat |
| `handoff_note(kind, text)` / `handoff_read(...)` | Cross-run handoff records |

`seat_can_write` is what the PreToolUse hook asks. The path must be inside the
seat's lanes and not locked by another seat. Being in-lane does not excuse
stomping art's locked `.blend`, so lanes and locks are separate gates. Unknown
or disabled seats fail closed.

## Asset locking

Binary files do not merge. Two agents editing one `.blend` loses someone's work.

```text
asset_lock(path, seat)      # claim BEFORE editing; a held lock errors, not queues
   …edit…
asset_release(path, seat)   # frees it and records the new content hash
asset_verify()              # audits everything and names silent clobbers
asset_status(kind, locked_only)   # what is tracked and who holds it
asset_track(path)           # register a file the pipeline did not produce
```

`asset_verify` is the drift detector. A changed hash with no lock held means
someone stomped the file outside the discipline, and it is named rather than
absorbed. Locked files are expected to differ and are not drift.

`godot_import_asset` auto-registers what it lands. Locks are advisory at this
layer; enforcement is the PreToolUse hook described in [setup.md](setup.md).
Verify makes violations visible without it.

## The Blender to Godot round trip

An agent models in Blender, exports glTF, and the asset lands usable in Godot,
verified in the engine rather than on disk.

```text
blender_export_gltf(out_path, blend_file=None, script="…")   # modifiers APPLIED
godot_import_asset(godot_project, src_path, dest_rel="assets")
   → engine_view: {total_tris, meshes:[{tris, has_uv, material, aabb}]}
```

`godot_import_asset` does not trust the file. It loads the resource inside a
real headless Godot and reports the mesh the engine built. A `.glb` that imports
with zero surfaces is a silent failure, and comparing tri counts on both ends
catches it. Measured: a beveled shard came out 106 tris on both sides, UVs and
material intact, which only happens because export applies modifiers. Blender
defaults that off.

`blender_export_gltf` also returns game-readiness issues:

- no UVs, so it cannot be textured
- n-gons, which triangulate unpredictably per exporter
- unapplied or non-uniform scale, which shears children

This leg is not covered by CI. See the README status section.

## Proving a character can be animated

`blender_rig` answers one question, were weights written, with the unweighted
vertex count. Two more tools finish the sentence.

```text
blender_rig(model, out_path, kind="humanoid", symmetrize="auto")
   → audit.shells        connected components. A real generation arrived as 940;
                         heat will not cross the gaps and loose islands weight to
                         whatever bone is nearest.
   → audit.symmetry      how far the body is from its own mirror image, as a
                         fraction of its height. Also the gate on the next line.
   → symmetrised         skin weights averaged across the centre plane, only when
                         the audit says the two sides match. Heat fails
                         differently on each side: one clean elbow and one bound
                         to the ribs is the normal outcome.

blender_flex(model, out_dir="", stem="flex")
   → volume_ratio     posed over rest. A good bind costs 2-6% on a 115° elbow.
   → worst_pinch      the joint that lost the most cross-section. 1.0 is rigid,
                      0.6 is a visible waist, under 0.4 is a straw.
   → new_self_pairs   faces that intersect in this pose and did not at rest. The
                      increase, not the count: a generated mesh arrives with
                      overlapping shells and the absolute number is noise.
   → render           a PNG per pose. Look at them.
```

`blender_flex` refuses to pass an inert rig: no armature modifier, no vertex
groups, or no vertex that moves. The first run of this gate passed a model where
nothing was bound at all, with six green poses and zero issues, because nothing
that cannot move can pinch.

Then ask the engine whether the rig is a humanoid it can retarget onto:

```text
godot_retarget_check(godot_project, res_path="res://assets/hero.glb",
                     bone_map_res="res://hero_bonemap.tres")
   → missing / extra     coverage against SkeletonProfileHumanoid, by exact name
   → chain[].propagates  rotating a shoulder moves the hand
   → clip.drives         a profile-authored rotation track turns the bone
   → retargetable        the verdict
```

The chain check is the one nothing else catches. A `.glb` can carry 23
correctly-named bones in a flat hierarchy: `blender_rig` reports 0 unweighted,
`godot_deliver_asset` photographs it happily, and the character can be animated
by nothing except a clip authored for it alone.

Related mesh gates: `blender_weights`, `blender_silhouette`,
`blender_template_deviation`, `animation_curves`, `blender_turnaround`.

## Cutout characters: parts on a skeleton

The other way to animate in 2D. The frame pipeline pays per character per
animation; a cutout character pays once per template.

```text
cutout_templates()                          # slots, bones, clips, parts list
cutout_assemble(name="hero", parts={...}, template="biped_v1")
cutout_status(name="hero")                  # missing parts, stale pivots, origin
cutout_equip(name="hero", slot="hat", texture=…)
```

You get a Godot scene whose bones are Node2Ds and whose parts are Sprite2Ds,
shipping with idle, walk, run, attack_melee, hurt and death baked onto that
character's rest pose. Equipment is a texture swap on one slot, at author time
or at runtime. The cost is a puppet, not a painting: rigid parts, no mesh
deformation, no squash. For a hero seen in close-up the frame pipeline is
better.

Two contracts to know before authoring one:

- **Feet contact (0, 0), +y up.** The document is in doc space, and the emitter
  is the single place the flip into Godot's +y-down happens.
- **Clips are deltas from the template rest pose**, baked to absolute values at
  emit time. A per-character adjustment therefore survives frame one of every
  clip instead of being erased by it.

## The art providers, and what routes where

Three keys, not interchangeable. See [setup.md](setup.md) for where a key lives
and which layer wins.

| Provider | Catalogue | Anchored (reference) work |
|---|---|---|
| OpenAI `gpt-image` | one model, four quality tiers | edits the references. Refuses multi-pose sheet prompts, see [gotchas.md](gotchas.md) |
| Krea | 14 image models + 5 image-to-3D | models split into **style** models, which follow a look, and **edit** models, which take reference images. Only the second holds a character through a pose change |
| kie.ai | 3 image models, Suno music, Seedance video | the default image model takes no references; `flux-2-pro-edit` (a list) and `qwen-edit` (one) do, over a hosted URL `kie.upload_file` mints from a local file, expiring in three days. No 3D |

**Character work is routed, not defaulted.** `providers.provider_for(task_kind)`
sends the character kinds (`sprite`, `sheet`, `animation`, `anchor`, `portrait`)
to Krea and `nano-banana-2` whenever `KREA_API_KEY` is set, ahead of the general
`openai → krea → kie` order used for everything else. An explicit `provider=`
always wins, including when its key is missing: the caller gets that provider's
own error rather than a silent substitution it pays for.

Measured 2026-08-10, one pinned character, identical prompt and reference, a
16-frame NE/SE walk sheet, through every reference-capable model on both
providers:

| Model | Verdict |
|---|---|
| `nano-banana-2` ($0.06) | **won.** Eight frames a row, correct back and front rows, clean key |
| `flux-kontext`, `seedream-5-lite`, `gpt-image`, `krea-2-medium` | clean |
| `flux-1-dev` ($0.007) | clean, and the cheapest that passed |
| `krea-2-large` (the old default) | **failed** the alpha audit at 14% hollow interior, and drew near-identical frames with no direction change |
| `nano-banana-pro`, `z-image` | failed: background bleed |
| `ideogram-3` | rejects 1536x1024 outright |

Why a style reference loses is in `bgate_adapters/krea.py` and in
[sprite-animation-research.md](sprite-animation-research.md): asked for four back
views out of eight, `krea-2-medium` drew a face in seven.

### What a call costs

Krea prices per model and per payload. `krea-2-large` is $0.06 plain, $0.065
with style references attached, $0.07 with a moodboard, so an estimate has to
read the request rather than the model name. Every price in `krea.MODELS` comes
off that model's own API reference.

kie publishes no per-model price. Its quickstart bands (images "typically 10-50
credits", video "100-500") are per call, so they under-count a long shot.
Measured on one invoice: a 5-second `seedance-2` shot consumed 205 credits, and
$1.025 of account spend gives $0.005 per credit, about 41 credits or $0.205 per
second of video. That is one account, so the shipped estimate
(`kie.VIDEO_CREDITS`) still spreads the band's ceiling across the model's 4-15s
range at 33.3 credits/second, under-quoting a long sequence by roughly a quarter
against the measured figure. Two environment variables replace both numbers:

```bash
BGATE_KIE_USD_PER_CREDIT=0.005                              # your account's rate
BGATE_KIE_VIDEO_CREDITS='{"seedance-2":{"per_second":41}}'  # what invoices say
```

A model with no rate yields `known: false` and no number rather than zero. A
fabricated figure in front of a spend gate reads as "free", not "unpriced". kie
reports `creditsConsumed` on the finished record, so with the rate set, ledger
rows are exact.

**Known gap:** `imagegen.IMAGE_PRICE_USD` prices gpt-image by quality tier only
and ignores size, although gpt-image-1's published price moves with resolution.
A 1024x1536 generation is quoted as a 1024x1024 one. No corrected numbers have
been measured, so none are stated here.

## Local generation, no API key

Both the 2D and 3D paths can run on the user's own GPU through one ComfyUI on
loopback. Nothing here imports torch, ships a model, or downloads one. The
workflow graph is the user's own export and the licence is the declared model's.

```text
image_status()                       # both legs: hosted key, and the local server
local_status()                       # the local server in detail
bgate doctor                         # local_image row, optional like ffmpeg
image_generate(..., provider="local")
blender_generate(image, out_path, parts=True)   # part-aware image-to-3D
```

`parts=True` is the better request for a character: a monolithic generation
gives one blob and bone heat has to guess where the arm stops. A part-aware
graph returns head, torso, arms and legs as separate meshes with a `combine`
list ready for `blender_combine`. A run that comes back with one mesh is flagged
rather than reported as a success.

| Environment variable | Sets |
|---|---|
| `BGATE_COMFY_URL` | The ComfyUI base URL |
| `BGATE_COMFY_T2I_WORKFLOW` | Text-to-image workflow export |
| `BGATE_COMFY_EDIT_WORKFLOW` | Reference-edit workflow export |
| `BGATE_COMFY_WORKFLOW` | Single-mesh image-to-3D workflow export |
| `BGATE_COMFY_PARTS_WORKFLOW` | Part-aware image-to-3D workflow, one file per part |
| `BGATE_LOCAL_IMAGE_MODEL` | The declared model, a licence statement |

## Templates

```bash
bgate init emberfall --kind 2d                # or 3d
godot_scaffold(name="Emberfall", kind="2d")   # the same slice, from an agent
godot_check_project(godot_project)            # import + validate headless
```

Both are runnable slices: a player, ground, something to jump onto, and the
BGate autoloads registered.

The 2D slice is a side-on platformer reading exactly three actions: `move_left`
(A or left arrow), `move_right` (D or right arrow), `jump` (Space). Anything
else you see advertised is not in the template. Its feel tunables (`gravity`,
`fall_multiplier`, `coyote_time`) are exported and emitted on every jump and
land, so the first playtest already produces the join that makes "the jump feels
floaty" actionable.

**F1 opens a live tuning overlay** over the running game. Every `@export` the
current scene exposes gets a control bound to the live node, and moving a slider
moves the game. There is no apply button. Values persist to
`.bgate/tunables.json` and are re-applied at boot, the same file the iteration
snapshot reads as `overrides`. A release export is inert: no input hook, no file
access, no overlay.

`BGATE_AUTOQUIT=<seconds>` runs a build unattended for headless smoke tests and
CI. Without `BGATE_TELEMETRY` set, the autoload is inert and opening the game
normally writes nothing.

## Level generation

`level_plan` lays out a level and shows it as ASCII. `level_generate` does the
same and writes it into a scene as `TileMapLayer` nodes.

```bash
level_plan(width=48, height=32, seed=3)              # look at `ascii`, change seed
level_generate(godot_project, scene="scenes/level.tscn",
               tileset="tiles/dungeon.tres", seed=3, create=True)
```

The layout is BSP: cut the map in two until a piece holds one room, put a room
in each piece, then join the two halves of every cut on the way back up. The
join builds a spanning tree, so every room is reachable from every other by
construction. The result reports `connected`, checked with a flood fill.

Which sprite goes in each cell is a neighbour bitmask, the same job Godot's
terrain sets do in the editor. Builders Gate writes `tile_map_data` directly and
never opens the editor. `wall_layout` says how the sheet is arranged:

| layout | mask | tiles | for |
|---|---|---|---|
| `blob47` | 8-bit, sides + corners | 47 | walls with proper inside corners |
| `grid16` | 4-bit, sides only | 16 | wall one cell thick |
| `solid` | none | 1 | floors, and sheets with no variants |
| `none` | none | none | floor layer only |

**The row-major-ascending-mask order is this tool's convention, not a standard.**
A sheet from Tilesetter or an asset pack has its own, and a wrong order draws a
complete, confident, wrong-looking level. Look at the first screenshot.

Checked before anything is written: every atlas coordinate the layout will emit
must be a tile the `.tres` defines. A cell pointing at an undefined tile draws
nothing and reports nothing. A 47-blob asked to sit on a 16-tile sheet is
refused with the list.

Re-running replaces the layers it wrote rather than appending, so iterating on
`seed` leaves one `Floor` and one `Walls`. A pre-existing node of that name that
is not a `TileMapLayer` is refused rather than overwritten.

Wave Function Collapse is not used for structure: it gives no global guarantee
of a reachable exit or a room count, and it can fail and need restarting.

## Scene editing

The level generator writes terrain. Everything else in a scene (a prop, a
camera, a script, a texture swap) is node-level surgery on the `.tscn`.
`bgate_core.scenewire` does it as text with no engine involved: `load_steps`
accounting, `ext_resource` ids, name uniquing, a dry run on every mutation, and
a timestamped backup on every write.

| Tool | Does |
|---|---|
| `scene_outline` | Read the tree: paths, types, roles, scripts, resources |
| `scene_wire` | Put an asset in as a new node, typed from the file |
| `scene_unwire` | Remove a node, sweeping resources left referenced by nothing |
| `scene_node_add` | Add a plain node (Camera2D, Timer, CanvasLayer) |
| `scene_set_property` | Set or clear one property. This is the move tool |
| `scene_swap_resource` | Point a node at a different file |
| `scene_attach_script` | Attach a `.gd` to a node that exists |
| `scene_rename_node` | Rename, and repair every path that named it |
| `scene_reparent_node` | Move a node and its subtree under a different parent |

```bash
scene_outline(godot_project, scene="scenes/floor.tscn", match="desk")
scene_set_property(godot_project, scene="scenes/floor.tscn",
                   node="Props/Desk_12", key="position", value="Vector2(320, 96)")
```

**Read before you address.** Every mutation names a node by path, and
`scene_outline` is where a path comes from. It filters (`match`, `role`,
`parent`) and truncates loudly: a baked floor plate is fifteen hundred nodes,
and dumping the whole tree buries the task in furniture.

**Every write checks the lock.** A scene held by another seat is refused, from
an agent and from the dashboard alike, because the holder may be mid-edit and
about to write its own copy over yours. A seat is never blocked by its own lock.
`force=True` is the deliberate override, and `dry_run=True` is never blocked:
refusing to look at a locked file helps nobody.

**A generated scene is the wrong file to edit.** If a `.tscn` is bake output,
its header says so, and the generator's input is the authority. An edit here
survives until the next bake while the runtime keeps reading the source. Read
the top of the file before moving anything in it.

The dashboard reaches the same functions through `/api/scene/*`, which is what
Atlas drags against.

## Publishing: the arcade

One command turns every game on this machine into a static site anyone can play
in a browser.

```bash
bgate publish                          # -> ./arcade, ready to deploy
bgate publish --dry-run                # what would ship, and what would not
bgate publish --serve [PORT]           # preview exactly as the host serves it
bgate publish --project emberfall      # just this one (repeatable)
bgate publish --rebuild stale|always|never
bgate publish --out <dir> --config <file> --force --json
```

It finds projects through the machine-wide registry (`~/.bgate/projects.json`),
re-exports any game whose Web build is older than its source, and writes:

```text
arcade/index.html                    the grid
arcade/games/<slug>/index.html       title, description, real controls, the embed
arcade/games/<slug>/build/           the Godot Web export, verbatim
arcade/_headers                      COOP/COEP + the compression rules below
arcade/games.json                    machine-readable index of what shipped
```

**Every game gets a page, not just a canvas.** Controls come from the project's
own input map, read by the same reader the dashboard uses, so the keys listed
are the keys bound. A game with no custom actions says so instead of inventing a
scheme.

Per-game copy lives in `<project>/.bgate/site.json` and overrides the store:

```json
{ "title": "Salt Circuit", "tagline": "The loser draws the track.",
  "description": "Two cars, one pencil.\n\nLast place draws the next 200m.",
  "tags": ["racing", "2 players"], "cover": "art/cover.png",
  "credits": "Music placeholder.", "order": 1, "hidden": false }
```

Site-wide settings (title, author, links) come from `arcade.json` in the cwd, or
`--config`. A missing or malformed file degrades to defaults rather than
aborting a build that is otherwise fine.

### The 25 MiB problem, handled

Godot 4's release `index.wasm` is about 38 MiB, and Cloudflare Pages and Workers
reject any asset over 25 MiB, so a naive deploy fails after the upload.

`bgate publish` measures every file against the target host's ceiling and gzips
the ones that break it under their original names, emitting the matching
`Content-Encoding` rules into `_headers`. 37.7 MiB becomes 9.6 MiB and the
browser unwraps it at the transport layer.

| `--host` | Per-file limit | Pre-compress |
|---|---|---|
| `cloudflare` (default) | 25 MiB | yes |
| `netlify` | 25 MiB | yes |
| `github` | 100 MiB | no |
| `itch` | 1000 MiB | no |
| `none` | no limit | no |

A file still over the limit after compression is reported as an error with the
URL and the size, rather than discovered from a failed deploy.

`--serve` reads the generated `_headers` and applies it, so the preview is the
deployment. A plain `python -m http.server` would hand the browser gzip bytes
labelled `application/wasm` and the game would die at the loader.

The shipped Web preset (`templates/shared/export_presets.cfg`) exports without
threads. Threaded builds need cross-origin isolation on the host, which free
static hosts and iOS Safari do not reliably give you. `_headers` still sets
COOP/COEP, so flipping `variant/thread_support=true` later is a re-export, not a
hosting migration.

## Playtest mode

Play the game, talk out loud, get an agent-readable brief.

```text
playtest_check    → preflight: ffmpeg, mic SIGNAL, transcriber, target window
playtest_start    → snapshots the iteration; records game + voice
   …play, and say what you like / what needs fixing…
playtest_stop     → transcribes, classifies, aligns, extracts frames
playtest_brief    → what the agents read
playtest_promote  → YOU decide what becomes work
```

Also: `playtest_devices` (list mics), `playtest_list`, `playtest_dismiss`,
`playtest_telemetry_contract`.

**Agents cannot watch video.** The mp4 is for you. The brief is transcript, plus
frames pulled at each remark, plus game telemetry joined on one clock, so "the
jump feels floaty" arrives next to `jump {air_time: 0.94}`. The game emits JSONL
events matching `playtest_telemetry_contract`.

Items land as `new` and stay there until you promote them. Thinking out loud
mid-play is not a decision to build.

Native Godot sessions append telemetry to the session JSONL path. Web builds
loaded inside the cockpit post the same event contract to the active session API
using the `bgate_session` query parameter. The review screen marks sessions with
zero telemetry rather than presenting them as aligned.

Each start records the Git commit and dirty fingerprint, source fingerprint,
exported PCK hash, artifact revision IDs, tunables and overrides, the latest
automated-check result, and the telemetry schema version.
`iteration_record_checks` updates the check snapshot; `iteration_status` returns
the causal history.

## Tool index

196 MCP tools are registered. The families, so you know what to look for:

| Family | Prefix | Covers |
|---|---|---|
| Project | `project_*`, `bgate_doctor`, `recall` | Create, select, dimension, status, dependency check, search |
| Bible and lore | `bible_*`, `lore_*`, `canon_check` | Pillars, constraints, references, the entity graph, the canon gate |
| Seats and board | `seat_*`, `queue_*`, `board_digest`, `plan_status`, `agent_steer`, `ask_human`, `handoff_*` | Identity, lanes, work items, chains, dependencies, escalation |
| Blender | `blender_*`, `character_generate`, `animation_curves` | Modelling, rigging, weights, flex, texture, turnaround, sprites, image-to-3D |
| Godot | `godot_*`, `evidence_check_ui` | Run, test, scaffold, import, deliver, screenshot, inspect, retarget |
| Scene | `scene_*`, `level_plan`, `level_generate` | Node surgery and level layout |
| Images | `image_*`, `sprite_*`, `item_*`, `vfx_animate`, `ref_*`, `profile_*`, `consistency_check`, `art_qa_verdict`, `art_tournament_*` | Generation, sprite sheets, items, references, consistency, art review |
| Cutout | `cutout_*` | 2D part-on-skeleton characters |
| Audio | `voice_*`, `sfx_*`, `kie_music_*`, `music_*`, `dialogue_*` | Speech, sound effects, music generation and selection, dialogue trees |
| Cinematic | `cinematic_*`, `storyboard_*`, `kie_video_generate` | Shot planning, generation, assembly, delivery, boards |
| Assets | `asset_*`, `pending_decisions` | Locks, tracking, integrity audit |
| Playtest | `playtest_*`, `iteration_*`, `causal_*` | Recording, briefs, promotion, iteration snapshots |
| Brainstorm | `brainstorm_*` | Sessions, synthesis, deploy to the board |
| Local | `image_status`, `local_status`, `kie_status` | Which providers and local servers are reachable |

## Repository layout

```text
bgate_cli/        the `bgate` console script: init, adopt, use, projects, serve,
                  app, publish, doctor, key, panic, hook-install, hook-uninstall,
                  hook-status, un-adopt
bgate_core/       db, project, bible, lore, canon, scope, spend, queue,
                  workflows, artifacts, playtest, iterations, git, search
bgate_mcp/        FastMCP server (stdio), 196 registered tools
bgate_adapters/   blender, godot, imagegen, krea, kie, localgen, sprites,
                  recorder, transcribe
bgate_ui/         dashboard backend + routes/ + the single-page static/ front end
bgate_site/       `bgate publish`: the static arcade + its theme/
templates/        Godot project skeletons (2d, 3d, cutout, humanoid, shared)
bgate_engine/     a design proposal + JSON schemas. No runtime code, nothing
                  imports it. See bgate_engine/README.md for its actual status
docs/             onboarding, findings from real production runs, and the audits
tests/
```
