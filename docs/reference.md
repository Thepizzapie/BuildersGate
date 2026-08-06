# Surfaces reference

2026-07-27. What each surface does, in detail. The [README](../README.md) is the
short version. [design-notes.md](design-notes.md) covers why these choices were
made. [gotchas.md](gotchas.md) covers what went wrong on the way.

## What is in the box

- **Design bible + lore canon.** Pillars, scope tiers with a mechanical cut
  line, an entity graph with atomic facts, and `canon_check`, a deterministic
  lexical gate every narrative write passes through.
- **Seven agent seats.** Director, narrative, gameplay, tech, art, audio, qa.
  Each has write lanes, one-call briefs, and a shared blackboard. A PreToolUse
  hook gives the lanes teeth.
- **Blender adapter.** Headless bpy with structured feedback (tri counts, UV
  warnings, renders), a sprite factory, and glTF export verified in-engine.
- **Godot adapter.** Headless run and check, asset import with engine
  inspection, live game screenshots, and project scaffolds with telemetry and F1
  live-tuning autoloads already wired.
- **Painted-art leg, optional.** Portraits, UI and backdrops, plus
  reference-first sprite sets with pinned reference anchors. Two providers:
  OpenAI `gpt-image` and Krea's 14-model catalogue, chosen per asset and per
  quality tier.
- **Asset registry.** Content hashes plus per-file locks for binaries, which do
  not merge, with a drift detector that names silent clobbers.
- **Playtest mode.** Record the game window and your voice, whisper-transcribe,
  classify feedback, join it to game telemetry on one clock, and export a bug
  report you can paste into a tracker.
- **Dashboard.** Nine views over the same store.
- **The arcade.** `bgate publish` turns every game on the machine into a static
  site with a page per game, and gets it under the host's per-file limit.
- **Gates with teeth.** The cut line refuses out-of-scope work. A spend budget
  refuses an agent that would blow the ceiling. Watchdogs kill a wedged run.
  Approval is human-only: an agent records a verdict, it does not sign off.

## The dashboard

```bash
bgate serve [--port 7788]     # from inside a project, or BGATE_ROOT
```

It prints the URL and the project it opened, because a command that starts the
product and says nothing looks like a hang. With no project it does not error. It
shows a first-run screen that creates one.

Ten views over the same store:

| View | What it is |
|---|---|
| Overview | Live agents, the queue, the build, and a play/record panel |
| Agents | Dispatch work to a seat, then watch and steer it live. A run is spawned on a captured git base commit, so its work is readable as per-file diffs and undoable with a scoped revert. The revert is refused if anything it touched has changed since, unless you look at the diff and insist |
| Studio | Node editors over the existing endpoints: workflow graphs and a Godot-style game workspace. A workflow run is a real graph. Steps queue seat work, consistency nodes carry a measured score, and a `gate` node stops the run until a human approves or rejects it |
| Seat workspaces | One workspace per seat, tuned to its craft. Art's is the flagship: every candidate revision beside the reference it was drawn against, two frames stacked with an opacity slider, a `difference` blend and a palette delta, batch approve-reject over a selection, and a dispatch button for an independent QA reviewer that never made the image |
| Playtests | Recorded sessions: video, transcript, telemetry, the director's triage, editable repro steps, and a bug report exported as markdown or as a zip with the frames it links |
| Assets | Immutable revisions grouped by logical asset, with an integrity audit |
| Atlas | Every screen wired to every asset it uses, derived live from the scenes, scripts and SpriteFrames. Click a node to file work against it. Four modes: the map, a graph that wires an asset in, a scene builder, and a code editor — the editor lists everything a scene reaches (its resources, plus one hop through what its scripts `preload`), edits them in place, and plays the rebuilt web build in the same pane. A save is refused if the file changed on disk since the tab opened, and refused again if a seat holds a lock on it; the previous bytes land under `.bgate_out/edits/` either way |
| World bible | A write surface, not a viewer: pillars, constraints, and one drag-ordered list of scope tiers with the cut line as a draggable row in it, plus the lore graph. Every narrative write runs `canon_check` first. A conflict is a 409 carrying its flags, and only a human may override it |
| Timeline | The causal chain per iteration: goal, source and build snapshot, assets, playtest evidence, decisions, work, resulting build, outcome |
| Settings | Every switch in one place, from one registry: dispatch, the approval gate, follow-up, notifications, budget, console. Each row says whether the value is the default, stored, or overridden by an environment variable — and a switch that widens a safety guard (`dispatch.allow_dirty`) asks before it is turned off and records that it was |

The header carries a **bell**: what has happened since you last looked, read
from the event table the follow-up router drives (`bgate_core/events.py`). It
only reaches you while the page is open — `bgate app` puts the count in the
window title, and one optional https webhook is the only channel that leaves
the machine.

The cockpit owns explicit user-facing mutations: queue and dispatch, recording,
feedback disposition, bible authoring, and artifact approval. Production
mutations remain MCP tools attributable to a seat.

Approval is human-only throughout. The dashboard identifies an agent's session by
`BGATE_ACTOR` and refuses it the bible, the scope filing, the budget, the revert,
a workflow gate, and promoting a candidate to the build.

127.0.0.1 is not a security boundary. Any page in your browser can POST to
localhost. So every mutation must be same-origin AND carry a per-project bearer
token from `.bgate/ui-token`, which is gitignored and 0600. The page is served
with the token injected and `fetch` wrapped to send it same-origin only. Nothing
else is asked to know it. `BGATE_NO_AUTH=1` opts out for a scripted run. See
[SECURITY.md](../SECURITY.md).

No build step, no node, no CDN.

## Seats

Seven stable game-dev identities: director, narrative, gameplay, tech, art,
audio, qa. A seat is an identity a working agent adopts, not a spawned process.
There is never a per-task registration.

```text
seat_brief(role)            # mission, lanes, bible, canon, promoted feedback, locks, notes
seat_can_write(role, path)  # the write oracle: two gates, both must pass
seat_post_note / seat_notes # the blackboard between seats
seat_configure(role, …)     # per-project lane/mission overrides, or disable a seat
```

`seat_can_write` is the oracle a PreToolUse hook asks. The path must be inside
the seat's lanes AND not locked by another seat. Being in-lane does not excuse
stomping art's locked `.blend`, which is why lanes and locks are two separate
gates. Unknown or disabled seats fail closed.

`seat_brief` replaces re-deriving project state from scratch. One call returns
the mission, the bible with the scope cut applied, canon entities, the promoted
playtest feedback routed to that seat, and who holds which binaries.

## Asset locking

Binary files do not merge. Two agents editing one `.blend` loses someone's work.

```text
asset_lock(path, seat)      # claim BEFORE editing; a held lock errors, not queues
   …edit…
asset_release(path, seat)   # frees it and records the new content hash
asset_verify()              # audits everything: names silent clobbers
```

`asset_verify` is the drift detector. A changed hash with no lock held means
someone stomped the file outside the discipline. It is named, not silently
absorbed. Locked files are expected to differ and are not drift.

`godot_import_asset` auto-registers what it lands, so bridge output is covered
from birth. Locks are advisory at this layer. Enforcement is the PreToolUse hook
described in [setup.md](setup.md). Verify makes violations visible even without
it.

## The Blender to Godot round trip

The spine: an agent models in Blender, exports glTF, and the asset lands usable
in Godot, verified in the engine rather than just on disk.

```text
blender_export_gltf(out.glb, script=…)    # build + export; modifiers APPLIED
godot_import_asset(project, out.glb)      # copy in, import, load in-engine
   → engine_view: {total_tris, meshes:[{tris, has_uv, material, aabb}]}
```

`godot_import_asset` does not trust the file. It loads the resource inside a real
headless Godot and reports the mesh the *engine* built. A `.glb` that imports
with zero surfaces is a silent failure, and checking tri counts on both ends
catches it.

Measured end to end: a beveled shard came out 106 tris in Blender and 106 tris in
Godot, UVs and material intact. Matching counts prove the modifier survived. It
only does because export applies modifiers. Blender defaults that off, and a
naive export ships the un-beveled base mesh.

`blender_export_gltf` also returns game-readiness issues, each cheap to catch
here and expensive to debug in-engine:

- no UVs, so it cannot be textured
- n-gons, which triangulate unpredictably per exporter
- unapplied or non-uniform scale, which shears children

This leg is not covered by CI. See the README status section.

## Proving a character can actually be animated

`blender_rig` answers one question — were weights written — with the unweighted
vertex count. That is the right proof that *something* was bound, and it says
nothing about whether the result is usable. Two tools finish the sentence.

```text
blender_rig(mesh, out.glb)        # adopt, fit, bind, and audit BEFORE binding
   → audit.shells        connected components. A real generation arrived as
                         940; heat will not cross the gaps and loose islands
                         weight to whatever bone is nearest.
   → audit.symmetry.mean how far the body is from its own mirror image, as a
                         fraction of its height. Also the gate on the next line.
   → symmetrised         skin weights averaged across the body's centre plane,
                         but only when the audit says the two sides match.
                         Heat fails differently on each side — one clean elbow
                         and one bound to the ribs is the normal outcome.

blender_flex(rigged.glb, out_dir)  # bend it and measure what bending did
   → volume_ratio     posed over rest. A good bind costs 2-6% on a 115° elbow.
   → worst_pinch      the joint that lost the most cross-section. 1.0 is rigid,
                      0.6 is a visible waist, under 0.4 is a straw.
   → new_self_pairs   faces that intersect in this pose and did not at rest.
                      The increase, not the count — a generated mesh arrives
                      with overlapping shells and the absolute number is noise.
   → render           a PNG per pose. Look at them.
```

`blender_flex` refuses to pass an **inert** rig: a mesh with no armature
modifier, no vertex groups, or no vertex that moves. That is not hypothetical —
the first run of this gate passed a model where nothing was bound at all, with
six green poses and zero issues, because nothing that cannot move can pinch.

Then ask the engine whether the rig is a humanoid it can retarget onto:

```text
godot_retarget_check(project, "res://assets/hero.glb",
                     bone_map_res="res://hero_bonemap.tres")
   → missing / extra   coverage against SkeletonProfileHumanoid, by exact name
   → chain[].propagates  rotating a shoulder moves the hand
   → clip.drives       a profile-authored rotation track turns the bone
   → retargetable      the verdict
```

The chain check is the one nothing else in the product can catch. A `.glb` can
carry 23 correctly-named bones in a **flat** hierarchy: `blender_rig` reports 0
unweighted, `godot_deliver_asset` photographs it happily, and the character can
be animated by nothing except a clip authored for it alone — which is the
opposite of why the bone names are Godot's own.

## Cutout characters: parts on a skeleton

The other way to animate in 2D. The frame pipeline pays per character per
animation; a cutout character pays once per *template*.

```text
cutout_templates()                          # slots, bones, clips, parts list
cutout_assemble(name="hero", parts={...})   # document + .tscn + .anims.tres
cutout_status(name="hero")                  # missing parts, stale pivots, origin
cutout_equip(name="hero", slot="hat", texture=…)
```

What you get is a Godot scene whose bones are Node2Ds, whose parts are
Sprite2Ds, and which ships with idle, walk, run, attack_melee, hurt and death
already baked onto *that character's* rest pose. Equipment is a texture swap on
one slot, at author time or at runtime.

What it costs: a puppet, not a painting — rigid parts, no mesh deformation, no
squash. For a hero seen in close-up the frame pipeline is still better.

Two contracts worth knowing before you author one:

- **Feet contact (0, 0), +y up.** The document is in doc space and the emitter
  is the single place the flip into Godot's +y-down happens.
- **Clips are deltas from the template rest pose**, baked to absolute values at
  emit time. That is what makes a per-character adjustment survive frame one of
  every clip instead of being erased by it.

## Local generation, no API key

Both the 2D and 3D paths can run entirely on the user's own GPU, through one
ComfyUI on loopback. Nothing here imports torch, ships a model, or downloads
one; the workflow graph is the user's own export and the licence is the
declared model's.

```text
image_status()                 # both legs: hosted key, and the local server
bgate doctor                   # local_image row, optional like ffmpeg
chroma / image_generate        provider="local"
blender_generate(..., parts=True)   # part-aware image-to-3D
```

`parts=True` is the better request for a character. A monolithic generation
gives one blob and bone heat has to guess where the arm stops; a part-aware
graph returns head, torso, arms and legs as separate meshes, and the result
carries a `combine` list ready for `blender_combine`. A run that comes back with
one mesh is flagged rather than reported as a success — a graph that merges
before saving is the monolith with extra steps.

Environment: `BGATE_COMFY_URL`, `BGATE_COMFY_T2I_WORKFLOW`,
`BGATE_COMFY_EDIT_WORKFLOW`, `BGATE_COMFY_PARTS_WORKFLOW`,
`BGATE_LOCAL_IMAGE_MODEL`.

## Templates

```bash
bgate init emberfall --kind 2d                # or 3d
godot_scaffold(name="Emberfall", kind="2d")   # the same slice, from an agent
godot_check_project(dest)                     # import + validate headless
```

Both are runnable slices, not empty shells: a player, ground, something to jump
onto, and the BGate autoloads already registered.

The 2D slice is a side-on platformer and reads exactly three actions:
`move_left` (A or left arrow), `move_right` (D or right arrow), and `jump`
(Space). That is the whole control surface. Anything else you see advertised is
not in the template.

The feel tunables (`gravity`, `fall_multiplier`, `coyote_time`) are exported AND
emitted on every jump and land, so the first playtest already produces the join
that makes "the jump feels floaty" actionable.

**F1 opens a live tuning overlay** over the running game. Every `@export` the
current scene exposes gets a control bound to the live node, and moving a slider
moves the game. There is no apply button, because the point is to feel the change
while you make it. Values persist to `.bgate/tunables.json` and are re-applied at
boot. That is the same file the iteration snapshot reads as `overrides`, so a
tuned build is visible rather than invisible drift. A release export is inert: no
input hook, no file access, no overlay.

`BGATE_AUTOQUIT=<seconds>` runs a build unattended, for headless smoke tests and
CI. Without `BGATE_TELEMETRY` set, the autoload is completely inert. Open the
game normally and nothing is written.

## Level generation

Two tools. `level_plan` lays out a level and shows it as ASCII; `level_generate`
does the same and writes it into a scene as `TileMapLayer` nodes.

```bash
level_plan(width=48, height=32, seed=3)              # look at `ascii`, change seed
level_generate(godot_project, scene="scenes/level.tscn",
               tileset="tiles/dungeon.tres", seed=3, create=True)
```

The layout is BSP: cut the map in two until a piece holds one room, put a room in
each piece, then **join the two halves of every cut on the way back up**. That
join is the whole reason for the algorithm — it builds a spanning tree over the
rooms, so every room is reachable from every other by construction. The result
reports `connected`, checked with a flood fill rather than asserted.

Which sprite goes in each cell is a neighbour bitmask, the same job Godot's
terrain sets do. Godot only does it *in the editor*, and Builders Gate writes
`tile_map_data` directly and never opens the editor, so it is redone here.
`wall_layout` says how the sheet is arranged:

| layout | mask | tiles | for |
|---|---|---|---|
| `blob47` | 8-bit, sides + corners | 47 | walls with proper inside corners |
| `grid16` | 4-bit, sides only | 16 | wall one cell thick |
| `solid` | none | 1 | floors, and sheets with no variants |
| `none` | — | — | floor layer only |

**The row-major-ascending-mask order is this tool's convention, not a standard.**
A sheet from Tilesetter or an asset pack has its own, and a wrong order draws a
complete, confident, wrong-looking level. Look at the first screenshot.

What *is* checked before anything is written: every atlas coordinate the layout
will emit has to be a tile the `.tres` actually defines. A cell pointing at an
undefined tile draws nothing and reports nothing — the level goes invisible
exactly where the shape is most complicated, which is the last place anyone
looks. A 47-blob asked to sit on a 16-tile sheet is refused with the list.

Re-running replaces the layers it wrote rather than appending, so iterating on
`seed` leaves one `Floor` and one `Walls`, not eight. A pre-existing node of that
name that is *not* a `TileMapLayer` is refused rather than overwritten.

Wave Function Collapse is deliberately not used for structure. It produces
plausible local adjacency and no global guarantee — no reachable exit, no room
count — and it can fail and need restarting. The bitmask is O(cells), cannot
fail, and is exactly reproducible from the seed.

## Scene editing

The level generator writes terrain. Everything else in a scene — a prop, a
camera, a script, a texture swap — is node-level surgery on the `.tscn`, and
`bgate_core.scenewire` does it as text with no engine involved: `load_steps`
accounting, `ext_resource` ids, name uniquing, a dry run on every mutation and a
timestamped backup on every write.

| tool | does |
|---|---|
| `scene_outline` | read the tree — paths, types, roles, scripts, resources |
| `scene_wire` | put an asset in as a new node, typed from the file |
| `scene_unwire` | remove a node, and sweep resources left referenced by nothing |
| `scene_node_add` | add a plain node — Camera2D, Timer, CanvasLayer |
| `scene_set_property` | set or clear one property. This is the move tool |
| `scene_swap_resource` | point a node at a different file |
| `scene_attach_script` | attach a `.gd` to a node that exists |
| `scene_rename_node` | rename, and repair every path that named it |
| `scene_reparent_node` | move a node and its subtree under a different parent |

```bash
scene_outline(godot_project, scene="scenes/floor.tscn", match="desk")
scene_set_property(godot_project, scene="scenes/floor.tscn",
                   node="Props/Desk_12", key="position", value="Vector2(320, 96)")
```

**Read before you address.** Every mutation names a node by path, and
`scene_outline` is where a path comes from. It filters (`match`, `role`,
`parent`) and truncates loudly, because a baked floor plate is fifteen hundred
nodes and dumping that whole tree buries the task in furniture.

**Every write checks the lock.** A scene held by another seat is refused, from
an agent and from the dashboard alike — the holder may be mid-edit and about to
write its own copy over yours, and a backup is recovery, not prevention. A seat
is never blocked by its own lock. `force=True` is the deliberate override, and
`dry_run=True` is never blocked at all: refusing to *look* at a locked file
helps nobody.

**A generated scene is the wrong file to edit.** If a `.tscn` is bake output —
its header will say so — the generator's input is the authority and an edit here
survives exactly until the next bake, while the runtime keeps reading the
source. Read the top of the file before moving anything in it.

The dashboard reaches the same functions through `/api/scene/*`, which is what
Atlas → *scene · build it* drags against.

## Publishing: the arcade

One command turns every game on this machine into a static site anyone can play
in a browser.

```bash
bgate publish                          # -> ./arcade, ready to deploy
bgate publish --dry-run                # what would ship, and what would not
bgate publish --serve                  # preview it exactly as the host serves it
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
own input map, read by the same reader the dashboard uses, so the keys listed are
the keys bound. A game with no custom actions says so instead of inventing a
scheme.

Per-game copy lives in `<project>/.bgate/site.json` and overrides the store:

```json
{ "title": "Salt Circuit", "tagline": "The loser draws the track.",
  "description": "Two cars, one pencil.\n\nLast place draws the next 200m.",
  "tags": ["racing", "2 players"], "cover": "art/cover.png",
  "credits": "Music placeholder.", "order": 1, "hidden": false }
```

Site-wide settings (title, author, links) come from `arcade.json` in the cwd, or
`--config`. A nonexistent or malformed file degrades to defaults rather than
aborting a build that is otherwise fine.

### The 25 MiB problem, handled

Godot 4's release `index.wasm` is ~38 MiB. Cloudflare Pages and Workers reject
any asset over 25 MiB, so a naive deploy of any Godot 4 web build fails, after
the upload rather than before it.

`bgate publish` measures every file against the target host's ceiling and gzips
the ones that break it *under their original names*, emitting the matching
`Content-Encoding` rules into `_headers`. 37.7 MiB becomes 9.6 MiB and the
browser unwraps it at the transport layer.

```bash
bgate publish --host cloudflare   # 25 MiB/file, pre-compress  (default)
bgate publish --host netlify      # 25 MiB/file, pre-compress
bgate publish --host github       # 100 MiB/file, no compression needed
bgate publish --host itch         # 1 GB/file
bgate publish --host none         # ship the bytes as they are
```

A file still over the limit after compression is reported as an error with the
URL and the size, because the alternative is finding out from a failed deploy.

`--serve` reads the generated `_headers` and applies it, so the preview is the
deployment. A plain `python -m http.server` would hand the browser gzip bytes
labelled `application/wasm` and the game would die at the loader.

The shipped Web preset (`templates/shared/export_presets.cfg`) exports without
threads on purpose. Threaded builds need cross-origin isolation on the host,
which is exactly the thing free static hosts and iOS Safari do not reliably give
you. `_headers` still sets COOP/COEP, so flipping `variant/thread_support=true`
later is a re-export, not a hosting migration.

## Playtest mode

Play the game, talk out loud, get an agent-readable brief.

```text
playtest_check    → preflight: ffmpeg, mic SIGNAL, transcriber, target window
playtest_start    → snapshots the iteration; records game + voice
   …play, and say what you like / what needs fixing…
playtest_stop     → whisper transcribes, classifies, aligns, extracts frames
playtest_brief    → what the agents read
playtest_promote  → YOU decide what becomes work
```

**Agents cannot watch video.** The mp4 is for you. The brief is transcript plus
frames pulled at each remark plus game telemetry joined on one clock, so "the
jump feels floaty" arrives next to `jump {air_time: 0.94}`. The game emits JSONL
events (`playtest_telemetry_contract`). That join is what turns a vibe into a
number an agent can act on.

Items land as `new` and stay there until you promote them. Thinking out loud
mid-play is not a decision to build.

Native Godot sessions append telemetry to the session JSONL path. Web builds
loaded inside the cockpit post the same event contract directly to the active
session API using the `bgate_session` query parameter. The review screen marks
sessions with zero telemetry rather than silently presenting them as aligned.

Each start automatically records the Git commit and dirty fingerprint, source
fingerprint, exported PCK hash, active artifact revision IDs, exported tunables
and overrides, latest automated-check result, and telemetry schema version.
`iteration_record_checks` updates the check snapshot. `iteration_status` returns
the complete causal history.

## Repository layout

```text
bgate_cli/        the `bgate` console script: init, adopt, use, projects,
                  serve, publish, doctor, hook-install, hook-status
bgate_core/       db, project, bible, lore, canon, scope, spend, queue,
                  workflows, artifacts, playtest, iterations, git, search
bgate_mcp/        FastMCP server (stdio), 98 registered tools
bgate_adapters/   blender, godot, imagegen, sprites, recorder, transcribe
bgate_ui/         dashboard backend + routes/ + the single-page static/ front end
bgate_site/       `bgate publish`: the static arcade + its theme/
templates/        Godot project skeletons (2d, 3d, shared autoloads)
bgate_engine/     a design proposal + JSON schemas. No runtime code, nothing
                  imports it. See bgate_engine/README.md for its actual status
docs/             onboarding, findings from real production runs, and the audits
tests/
```
