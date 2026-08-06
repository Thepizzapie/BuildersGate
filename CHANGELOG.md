# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

### Added

- **Nine scene tools on the MCP surface.** `scene_outline`, `scene_wire`,
  `scene_unwire`, `scene_node_add`, `scene_set_property`, `scene_swap_resource`,
  `scene_attach_script`, `scene_rename_node`, `scene_reparent_node`.
  `bgate_core.scenewire` has parsed and edited `.tscn` text since the Atlas
  builder shipped, and all of it was reachable from a browser and none of it
  from an agent — so an agent told to place a prop hand-edited the file as text,
  inventing `ext_resource` ids, guessing at `load_steps`, and finding out at
  `godot_check_project`. Same functions the dashboard calls, same dry-run and
  backup contract.

- **The scene builder plays the build it just wrote.** Atlas → *scene · build
  it* gained the play panel the code editor already had, and `apply` now
  exports and reloads it. The viewport draws what the FILE says, which is the
  right picture to drag against and is not proof of anything; checking used to
  mean leaving the panel, opening the play tab, and remembering to rebuild
  first. Almost nobody remembered, so what got checked was yesterday's build —
  worse than not checking, because it comes back green.

- **`assets.lock_holder()`** — one implementation of "who holds this path",
  which never raises. Three callers were answering it three ways or not at all.

### Fixed

- **The build staleness check could not see the file you were editing.**
  `webbuild._newest_source_mtime` scanned `scripts/`, `scenes/` and `assets/`,
  and that allowlist quietly decided what a build was allowed to depend on. A
  project keeping its levels in `data/*.json` had every level edit invisible:
  change the level, `/api/play/status` reports **current**, you play the old
  game and conclude the tool ignored you — verbatim the morning `webbuild.py`
  was written to prevent, reintroduced by the scan instead of the rebuild. It
  is a denylist now (`.godot`, `export`, `.git`, caches), so a directory nobody
  has imagined yet counts as source rather than being ignored. `status()` also
  names the file that made the build stale, so "stale" is answerable rather
  than asserted.

- **`/api/scene/*` wrote straight through a held lock.** Every other writer in
  the system asks — the code editor, every agent through `asset_lock` — and the
  scene endpoints did not, which was survivable only while a human clicking
  buttons was the sole caller. They are on the MCP surface now, so two agents
  and a person can reach the same `.tscn`. Writes are refused with `423`,
  `force` overrides, a dry run is exempt, and `/api/scene/tree`, `/outline` and
  `/render` report the lock so the builder says so before twenty drags are
  staged against a file the write is going to refuse.

## [0.1.33] - 2026-08-02

### Added

- **Atlas grew a fourth mode: `code · edit it`.** Every code surface in the
  dashboard was read-only, which is the whole reason the engine had to stay open
  next to it. Now: pick a scene, get everything it reaches, edit it, save it,
  play the rebuilt web build — without leaving the page. CodeMirror 5 is
  vendored under `bgate_ui/static/vendor/` (MIT); the GDScript mode is ours,
  because CodeMirror's Python mode gets `func`, `@export` and `$Player/Sprite2D`
  wrong, and a near-miss highlighter is worse than none.

- **`POST /api/godot/file`** — the write half of the Godot workspace. Refuses a
  save when the file changed on disk since the tab opened (an agent editing the
  same script is not a merge this editor is qualified to perform), refuses again
  when a seat holds the lock, and keeps the previous bytes under
  `.bgate_out/edits/` either way. CRLF is normalised on the way in, but a file
  whose content did not change is never rewritten — normalising every save would
  rewrite a CRLF file nobody touched.

- **`GET /api/scene/files`** — everything a scene reaches: its `ext_resource`s,
  plus one hop through the scripts it attaches, following `preload`/`load`. The
  second hop is the point. On a real scene here it turns 2 declared files into
  15: `combat.tscn` names only `combat_view.gd`, and that script pulls in
  thirteen more. Deliberately one hop — a full closure on a project with a
  shared autoload returns most of the codebase and stops being a picture of
  *this* scene.

- **A `real` backdrop in the scene viewport.** Runs the game for one frame and
  paints it behind the editable overlay, so a floor whose props get their
  texture from a script at load finally shows the floor instead of 577 markers.
  Registration is to the game camera, not the scene origin, so it is a reference
  view and not something to place against — that is stated in the tooltip.

### Changed

- **Streamer mode costs roughly nothing now.** It was 25-40x on every response —
  `/api/state` took 16.7s with the filter on and 0.68s with it off — which made
  the dashboard unusable in the one mode you turn on when people are watching.

  The scrub is pure-CPU regex and regex holds the GIL, so none of it overlaps:
  eight concurrent requests measured exactly eight times one request. A single
  isolated request was always 0.63s; the rest was pile-up from six polling
  endpoints.

  Merging the twelve vendor-key patterns into one alternation does *nothing*
  (0.258s either way) — twelve branches with twelve different prefixes still get
  tried at every position. What works is not running passes that cannot match.
  Every shape needs a distinctive literal (`sk-`, `AKIA`, `ghp_`, `eyJ`), and a
  substring test is ~1ms/MB against ~20ms for the branch it replaces. A
  `/api/screenmap` body contains none of them, so its secret pass went from
  0.217s to 0.013s. `_replace_path` was six `re.sub` calls per root with the
  pattern rebuilt from a string each time; it is one cached alternation now.

  Verified by differential test against the original implementation over 3,582
  generated cases — every vendor key format, every path spelling (backslash,
  forward, JSON-escaped, `%5C`, `%2F`, case variants), foreign home directories,
  emails, plus two real 1MB payloads. Zero output differences.

- **`/api/screenmap` is cached and no longer walks the engine's import cache.**
  `.godot` is ~2000 files of `.import`/`.md5` on a real project and every scan
  enumerated it before discarding it; the walks prune now. 37.8s to 0.34s. On
  top of that the scan is memoised for 90s with single-flight, so six panels
  polling at once share one walk instead of starting six — the dashboard's own
  writes invalidate it, and `?fresh=1` (the `reread` button) forces a rescan.

- **`/api/preview` takes `item_id`** and resolves a run's worktree the same way
  `/api/peek` does. `peek` was resolving the worktree to decide a file existed
  and then handing back a preview URL with that context stripped, so every
  thumbnail of a file an isolated agent was editing 404'd into an empty box.

### Fixed

- **Three panels ignored `hidden`.** A class that sets `display` outranks the UA
  sheet's `[hidden]{display:none}` at equal specificity, so `el.hidden = true`
  set the attribute and changed nothing on screen: a staging bar announcing "0
  unsaved changes across 0 nodes" with a discard button, an empty dashed
  placement strip under it, and a surface toggle that could mount the viewport
  and the graph at once.

- **Long dropdowns closed the instant they opened.** `bgselect` closed on any
  scroll in the capture phase, and opening a picker calls `scrollIntoView()` on
  the selected row — so on any list long enough to need scrolling, the popup
  shut in the same tick it opened and clicking appeared to do nothing. Only
  long lists, which is why every other dropdown looked fine.

- **`app.css` is cache-busted like the JS.** Only `/static/*.js` got a `?v=`
  stamp, so every stylesheet edit sat in the browser cache and looked like a fix
  that did nothing.

- **The sprite editor opened into a hidden container.** `_host` is set once by
  the Studio's sprite tab and was never cleared, so afterwards every `open()`
  from anywhere else — the scene builder's `edit pixels`, the Atlas graph, the
  asset library — mounted the whole editor inside the Studio's off-screen
  container. It embeds only into a host that is actually on screen now.

- **The scene viewport stopped captioning 577 props at once.** Each undrawable
  node painted a full explanatory sentence; on a dressed floor they overdrew
  into a grey pulp with the level underneath. Capped, with the tip bar still
  naming them and stepping through them.

- **Both scene pickers defaulted to junk.** "Most edges" always picks a QA
  fixture, because those scenes exist to reference everything at once. They read
  `run/main_scene` from `project.godot` now, with tests and `_proof`/`_demo`
  scenes sorted last.

- **`/api/godot/*` 404'd on any adopted project.** It hardcoded `<root>/game`,
  which is right for `bgate init` and wrong for `bgate adopt`, whose
  `project.godot` sits at the root. It resolves the same way `screenmap` and
  `scenewire` do now.

### Added

- **`level_plan` and `level_generate` — rooms, corridors, and tiles that line
  up.** BSP for the layout, a neighbour bitmask for the tiles, the packed binary
  Godot stores cells in, and a backed-up `.tscn` edit. No engine, no editor, and
  a reviewable diff at the end.

  The BSP join is the point. Cut the map in two until a piece holds one room,
  put a room in each piece, then join the two halves of every cut on the way
  back up — that builds a spanning tree over the rooms, so every room is
  reachable from every other *by construction*. `connected` in the result is a
  flood fill, not an assertion.

  Which sprite lands in a cell is the job Godot's terrain sets do, and Godot
  only does it in the editor. Builders Gate writes `tile_map_data` directly and
  never opens the editor, so it is redone here: `blob47` (8-bit, sides and
  corners, 47 tiles), `grid16` (4-bit, sides, 16 tiles), or `solid`. Verified
  against a real Godot 4.7.1 — the engine's own `get_used_cells` returns exactly
  the counts the generator reported, with zero cells pointing at an undefined
  tile.

  **The row-major-ascending-mask order is a convention, not a standard.** A
  sheet from an asset pack has its own, and a wrong order draws a complete,
  confident, wrong-looking level. What *is* checked, before anything is written,
  is that every atlas coordinate the layout will emit is a tile the `.tres`
  actually defines — a cell pointing at an undefined tile draws nothing and
  reports nothing, so a 47-blob asked to sit on a 16-tile sheet is refused with
  the list rather than producing a level that is invisible exactly where the
  shape is most complicated.

  Wave Function Collapse is deliberately not used for structure: it gives
  plausible local adjacency and no global guarantee, and it can fail and need
  restarting. The bitmask is O(cells), cannot fail, and is exactly reproducible
  from the seed.

- **`tilemap.encode_cells`** — the write half of the format, which only had a
  reader. Cells go out in `(y, x)` order so the same seed produces byte-
  identical bytes; a coordinate outside int16 and two cells on one coordinate
  are both refused rather than wrapped or silently dropped.

- **`scenewire.wire_tilemap`** — writes tile layers into a scene, replacing
  same-named ones. Append-only turned a re-run into `Ground`, `Ground2`,
  `Ground3` stacked on each other, all still drawing, with nothing in the scene
  saying so. A node of that name that is not a `TileMapLayer` is refused.

- **`parse_tileset` now reports the tiles an atlas defines** (the `N:M/0 = 0`
  lines), which is what makes the coverage check above possible. Kept out of the
  draw payload — the canvas draws the cells it is given.

## [0.1.32] - 2026-08-01

One call turns "a model that looks like X" into a rigged character in the
engine, and `force` stops meaning "overwrite your project".

### Added

- **`character_generate` — the whole pipeline as one tool.** Plate, key, mesh,
  rig, deliver. Every stage already existed and every stage was reachable, and a
  caller still had to know: condition the plate on the template pose or the
  skeleton will not fit, key it or the backdrop arrives as geometry, which
  backend takes which knobs, and that a bind reports success having weighted
  nothing. Get any of them wrong and you find out ten GPU minutes later. The
  parameters are not defaults, they are the ones that produced a character that
  fitted at `limbs=1.0` with no compensation, 0 bones outside the mesh, 0
  unweighted vertices, delivered into Godot and animated:

  | stage | what it does | why these values |
  | --- | --- | --- |
  | plate | provider `generate`, template pose as a `0.45` reference, `1024x1536` | the reference carries the STANCE — arms out, feet flat, symmetrical, head to feet. Without it the generator picks a stance and the skeleton has to be bent to match. A square canvas puts the head at ~120 px and the face is invented rather than reconstructed |
  | key | `despill=0` | despill is right for green and wrong for grey — on a neutral backdrop it strips saturation from the whole image, and it took one plate's leather from brown to white |
  | mesh | `resolution=1024` | what was run on a 12 GB card; TRELLIS is where VRAM gets tight |
  | rig | `pose="t"`, `budget=45000` | an A-pose skeleton inside a T-pose body put the hand bones 14 cm outside the mesh |

  Each stage **gates** the next, so a failure costs the stage that found it
  rather than the whole chain. From the runs it was built on: an unkeyed plate
  cost **605 s and 21% non-manifold** and was refused by the quality gate; the
  same subject keyed, **216 s and 16%**, passes. A collapse met its triangle
  budget with **20,799 of 39,803 faces inside out** and reported `met=True`.

  `dry_run` is the default. It quotes the plate and the mesh and stops, and a
  backend that publishes no rate reports `None` rather than `0.0` — a caller who
  has not decided to spend should not be told a generation is free.

### Fixed

- **`force` meant "overwrite your project", and said nothing about it.** It was
  documented as "scaffold into a non-empty directory" and implemented as "write
  every template file over whatever is there". Someone reaching for it to top up
  a missing addon lost `project.godot`, `player.gd` and `export_presets.cfg` in
  place, with no backup and no mention of it in the result. `export_presets.cfg`
  is the unforgiving one: the `.gitignore` this same template stamps excludes
  it, so the customised export targets were not in git either.

  `force` now **fills in what is missing**. A file that already matches what the
  template would write is left alone; a file that differs is the user's and is
  skipped. `--replace` is the separate, explicit "put the template back", and
  even that copies each victim to `<name>.bak` first — never onto an existing
  backup, because a second `--replace` run reusing the same `.bak` would destroy
  the rescue copy the first one took. Both outcomes come back in the result, and
  `note` is one line a caller can print verbatim: a run that deliberately left
  the user's work alone used to report "0 files" and read as a no-op rather than
  as a decision.

## [0.1.31] - 2026-08-01

A generated mesh becomes a rigged character an engine can move, and the skeleton
it binds to is the same one every time.

### Added

- **`blender_rig` — the missing step between geometry and a character.** Every
  image-to-3D backend returns `rigged: false`. This adopts the mesh, fits the
  template skeleton to it, binds, and **proves the bind took**. The proof is the
  unweighted vertex count and nothing cheaper works: `parent_set` returns
  cleanly, creates all 22 vertex groups, and can leave every one of them empty —
  the modifier attaches, Godot shows a `Skeleton3D`, and the character animates
  not at all. Measured on a real generation: **64,878 of 64,878 vertices
  carrying no weight with every other check green**. Adopt and bind run in ONE
  Blender session because round-tripping through a file caused exactly that:
  glTF re-import carries a root transform, so the skeleton lands in a different
  space and heat finds nothing to weight. Same mesh in one session: 3 of 19,556.
- **A shipped humanoid template.** `templates/humanoid/` carries the 23-bone
  skeleton and the pose plates to generate against, and `blender_humanoid_template`
  hands a caller the reference image, the prompt clause and the bone names. It is
  the fixed end of the pipeline: art conforms to the skeleton instead of the
  skeleton being bent to fit each generation. Bones further than 6 cm from any
  mesh vertex, measured on one character — template scaled by height **16 of 24**,
  landmark fitting alone **5 of 23**, plate conditioned on the reference alone
  **8 of 23**, both **0 of 23 with 0 unweighted**. Bone names are Godot's
  humanoid profile, so BoneMap retargeting works and clips move between
  characters. It lives beside `templates/3d` rather than inside it — that
  directory is the Godot scaffold `bgate init --kind 3d` copies wholesale, and
  the first version of this put a skeleton and two 1.5 MB plates into the root of
  every new 3D game.

### Fixed

- **The shoulder joint was 20 cm outside the body.** `fit_bones` took "the
  innermost slice of everything past 55% of the half-width" as the shoulder, and
  on a T-pose figure that lands at the bicep — measured x=0.396 against a torso
  half-width of 0.198. The arm hung correctly from a joint outside her body, so
  every pose read as a zombie holding buckets and no animation could fix it.
  Sampling the torso below the armpit puts it at 0.157, with the hand inside the
  silhouette.
- **`bg_flipped` answered `0` both when it found nothing and when it broke** — a
  check that reports clean when it failed. It returns `-1` for "could not look"
  now, and `bg_collapse_ok` fails closed on it.
- **A collapse could meet its budget and destroy the asset.** A generated head
  decimated 143,534 → 39,803 faces reported `met: true` with no complaint and was
  ruined; the number that said so was in the same report — 20,799 flipped faces,
  52% of the surface inside out. Thresholds come from the runs either side of
  that failure: clean adopts measured 0.5–0.9% flipped and 8–11% non-manifold,
  the ruined one 52% and 33%.
- **A 404 meant "server missing" when it means "server answering".** `urlopen`
  raises on 4xx, the blanket except caught it, and a running trellis.cpp whose
  build has no `/health` was reported dead — so `choose()` could never select it
  while naming it by hand worked. Reported from the field.
- **EEVEE answers to two names** and which one is real depends on the binary. 4.2
  shipped the rewrite as `BLENDER_EEVEE_NEXT`; 5.x dropped the legacy engine and
  took `BLENDER_EEVEE` back. Two of the three places that assigned the engine had
  no fallback and raised `TypeError` mid-render on 5.x. The choice is made inside
  Blender against the enum the install offers.

### Added — the paths a session could not reach

Field feedback: "ComfyUI support out of the box (along with the better paid
models) would be fantastic." Three separate things were stopping that, and none
of them was the generator.

- **Krea 3D shipped unreachable.** `krea.generate_3d` landed as a Python
  function that no MCP tool called — grep across the whole codebase found no
  caller outside `krea.py` itself. So a user whose only key is `KREA_API_KEY`,
  the key `.env.example` and the setup docs both tell them to set, **could not
  produce a mesh from a session at all**; they needed a Stability/Tripo/Meshy
  key or a local GPU server instead. It is a backend now, delegating to
  `krea.py` rather than re-describing the HTTP, so there is one model table and
  one price table — and it inherits `choose()`, the licence gate, the price
  quote and the common result shape. Verified with a real generation: 15.7 MB,
  93 s, `estimated_usd 0.30` quoted before the run. `choose()` correctly refuses
  to select it automatically, because Krea runs the same open-weight models one
  could self-host and the service's terms and the model's terms are two
  different questions.
- **Which knobs a backend takes is now discoverable.** `status()` reported
  availability, VRAM, weights and licence but never `supports`, so an agent had
  no way to learn that `hunyuan-local` accepts `face_count`, `steps`,
  `octree_resolution` and `guidance` while `trellis-cpp` accepts only `seed` and
  `resolution`. That decides what a user can control: on `trellis-cpp` there is
  no way to ask the generator for less geometry, so post-generation decimation
  is the only density lever that exists.
- **ComfyUI claimed to be available with no workflow.** It runs *your* ComfyUI
  graph and can only substitute two placeholders into it — the adapter cannot
  invent one. Reporting `available: True` regardless meant `choose()` could hand
  back a backend that fails at generation time, after the server is running and
  the plate has been paid for. It refuses up front now and names
  `BGATE_COMFY_WORKFLOW` and "Save (API format)". **No default workflow JSON
  ships**: authoring one without the Trellis2 node pack installed would be
  untested JSON presented as working.
- **`image_generate` was hardcoded to OpenAI**, so the same Krea-only user could
  not reach the most obvious tool in the product while Krea sat configured two
  functions away. `provider=""` picks from what is configured; an explicit value
  always wins, including when its key is missing, because a silent substitution
  bills someone for a model they did not ask for.

### Fixed — Godot

Eight defects in the Godot surface, found by delivering a character into a real
project and booting it. Every one is a tool assuming it owns something the user
also owns, and a check counting the countable thing instead of measuring the
real one.

- **Every second delivery corrupted the asset's `.import`.** The `_subresources`
  regex was lazy and stopped at the `}` closing the inner `"PATH:<node>": {`
  dict rather than the outer brace. A fresh `.import` uses the single-line form,
  so the FIRST delivery was safe and the second — every art iteration — left
  `}
}` stranded and the file unparseable by Godot's ConfigFile. Reproduced at
  3 open / 5 close braces. The test passed the whole time because it counted
  keys and never checked balance.
- **The collision capsule tracked the pose, not the body.** Radius came from
  half the widest horizontal axis, which on a 1.75 m character with her arms out
  is her 1.6316 m span: **radius 0.8158**, a 1.63 m cylinder around a person who
  cannot then fit through a human-sized door. `has_collider` only counts shapes,
  so it shipped green. Now the smaller horizontal, capped for an upright figure
  at a human proportion of its height — gated on taller-than-wide so a crate
  keeps its own radius.
- **A crate is not a character.** Everything was wrapped in `CharacterBody3D`,
  which only moves when code calls `move_and_slide()`, so a delivered prop never
  simulated — and got a capsule meant for a person on top of the trimesh the
  importer had already built from its geometry. Skinned meshes get
  `CharacterBody3D` and the capsule; unskinned get `StaticBody3D` and the
  importer's colliders, not both.
- **A generated scene shipped a `Camera3D` that hijacked the view.** Observed: a
  delivered pirate instanced into a level with a player camera booted looking
  out of her eye sockets. Opt-in now, and never `current`. `templates/3d`'s
  `player.gd` took `$Camera3D` with no guard, so the fix alone would have
  null-dereffed it once per mouse event — verified on Godot 4.7.1 that the old
  spelling is silently null and fails on first touch.
- **Redelivery no longer regenerates the character scene.** It repoints the
  model `ext_resource` and keeps your node tree, because the point of iterating
  on a `.glb` is to see the new mesh and a skipped scene would keep showing the
  old one. `scene_action` reports written/rewired/left_alone, and `left_alone`
  marks the step not-ok — silence there reads as a delivery that never wired the
  mesh in. `overwrite_scene=True` is the escape hatch. This also stops
  `main.glb` replacing the scaffold's `scenes/main.tscn`.
- `_subresources` is merged rather than rebuilt, so per-node settings made in
  Godot's Import dock survive a delivery. `import_asset` reports a same-named
  asset it replaced instead of overwriting in silence. `screenshot()` and
  `evidence()` no longer strand an autoload in the project when the server is
  killed mid-run. The cache purge escapes the asset name — `hero[1].glb`
  produced a character class that could not match its own entries, so the purge
  silently no-opped and presented as "the tool ignored my collider settings".

- **`bgate init --force` filled a project in; it should not empty one out.**
  Reproduced: customise `scripts/player.gd`, `export_presets.cfg` and
  `.gitignore`, re-run with `force=True`, and all three were overwritten in
  place with nothing said. `templates/shared/.gitignore` excludes
  `export_presets.cfg` from git, so for a user with customised export targets
  that one was unrecoverable. **`force` now means "fill in what is missing"** —
  identical files skipped, differing files left alone and reported. `replace=True`
  is the new explicit overwrite and takes a `.bak` first, falling through to
  `.bak.1` rather than destroying the previous rescue copy. CLAUDE.md states the
  contract this restores: scaffolding over someone's existing game is the one
  unrecoverable mistake available here.

### Changed

- The unkeyed-plate warning carries its measurement. Same prompt and model,
  alpha the only difference: opaque **605 s and 21% non-manifold** (quality
  refused), keyed **216 s and 16%** (passes). `chroma.needs_key("character")` is
  `False`, so a character plate arrives opaque unless `keyed=True` is passed.

## [0.1.30] - 2026-07-31

The 3D path stops being a blockout generator. Everything below `### Added — 3D`
is the difference between "a correctly-proportioned mannequin with paddle arms
and no face" and a generated mesh that survives the same gates as one modelled
by hand. It stays on the `0.x` line: the design is still moving, which is what
`CONTRIBUTING.md` tells contributors and what the version should keep saying.

### Added — 3D

- **Image-to-3D on the user's own GPU.** `bgate_adapters/imageto3d.py` and the
  `blender_generate` MCP tool. The primary backends are open-weight models
  running locally over loopback HTTP — TRELLIS.cpp, ComfyUI with a 3D node
  pack, Hunyuan3D — because everything else in this product is local and
  renting a mesh generator would have been the one place that stopped being
  true. Hosted APIs (Stability, Tripo, Meshy) sit behind the same interface as
  a fallback, not as the design centre. It imports nothing heavy: no torch, no
  CUDA, no model library, ever, in this process — the GPU is probed with
  `nvidia-smi` and a local server with a short HTTP GET, the rule
  `transcribe.py` established for faster-whisper. It works with nothing
  configured, prices the request rather than the backend, and **states the
  licence before it generates**: a local backend is a transport, ComfyUI being
  MIT says nothing about the weights it loads, so `BGATE_LOCAL_MODEL` has to be
  declared and a backend whose terms carry conditions never becomes an
  automatic choice.
- **Krea 3D**, through the `KREA_API_KEY` that was already configured — the
  same open-weight models one would otherwise self-host, with no GPU, no CUDA
  toolchain and no 16 GB of weights.
- **`bg_adopt` — a generation is not an asset.** Measured on real output: a
  crate came back as 20,748 shells and 495,061 triangles, a mannequin as 604
  shells with 21,796 non-manifold edges. Weld, decimate, scale, orient, ground,
  in that order, because grounding before scaling leaves the mesh floating.
  Welding is gentle on purpose — chasing one shell took the mannequin to 20,285
  non-manifold edges, after which the decimator could not reach budget at all,
  since collapse will not cross a non-manifold junction. And it reports
  asked/got/met with a reason instead of returning a mesh 50× over budget under
  an ok-looking report.
- **Orientation is refused rather than guessed.** Generated meshes face any
  direction and nothing was checking — a mannequin rendered its BACK in the
  frame labelled "front". Two wrong attempts are recorded in the design: "up is
  the tallest axis" put a cap's up along its brim, and centroid offset scored a
  mannequin and a crate within 0.1% of each other. Foot reach separates them.
  `kind="none"` and unreadable geometry both leave the mesh untouched.
- **`doctor` gains an `imageto3d` row** with no floor — red means only that
  path is unavailable — asking `nvidia-smi` rather than torch, because a
  CPU-only torch in the default interpreter would call a working GPU "no GPU".
- **`spend` gains a `mesh` kind.** It was landing in `other` with genuinely
  uncategorised spend, and a textured generation is ~$0.30, an order of
  magnitude over an image.

### Added — CI

- **A CI pipeline that gates on more than pytest.** `.github/workflows/ci.yml`
  replaces `tests.yml` and adds three checks the repo had already written down
  and never run: `ruff` (configured in `pyproject.toml` since someone chose the
  rule set, invoked by nobody), a Linux run marked advisory exactly as
  `CONTRIBUTING.md` has claimed for months, and `wheel-smoke` — the job
  `tests/test_packaging.py` names by file and job name as the home of the
  build-install-import loop, which did not exist.
- **`packaging/smoke_wheel.py`** — installs the built wheel into a clean
  interpreter, refuses to run if its imports resolved to the checkout instead,
  then checks the shipped trees, runs `doctor`, scaffolds a real project out of
  `templates/`, and serves the dashboard's assets out of `site-packages`. It
  shares one path list and one fetch loop with the exe smoke test, so a check
  added for one artifact cannot be silently missing from the other.
- **A release guard.** `release-exe.yml` now fails in five seconds, before
  anything is built or signed, when the tag, `pyproject`'s version and the
  changelog disagree. `v0.1.28` and `v0.1.29` were both tagged against a tree
  declaring `0.1.27`, with no changelog section for either, and both shipped
  release bodies that described nothing.
- **`.github/workflows/security.yml`** — `pip-audit` against the dependency tree
  a user actually resolves to, and CodeQL. For a tool that runs arbitrary
  GDScript and reads API keys off disk, neither existed.

### Fixed

- **An interrupted migration wedged the database permanently.** `executescript()`
  issues a COMMIT before it runs, so a step's DDL landed in its own transaction
  and `PRAGMA user_version` was written afterwards — anything in that gap (a
  crash, `bgate panic`, a taskkill, the loser of the concurrent-startup race
  `_migrate` documents but does not actually prevent) left a database whose
  schema said "applied" and whose version said "pending". Every later
  `connect()` replayed the step, raised `table event already exists`, and took
  the dashboard and every agent's MCP server down for good. This repository's
  own `.bgate/game.db` was in that state: `user_version` 15, `event` table
  present, `GET /api/state` answering 500 on a database that was in substance
  fully migrated. SQLite DDL and `user_version` are both transactional, so each
  SQL step and its version bump are now one commit, and a replay of a step whose
  objects already exist repairs the version instead of raising.
- Eight `ruff` findings on `main`: three f-strings with no placeholders in
  `bgate_adapters/godot.py`, and unused imports in `bgate_core/vfx.py`,
  `bgate_ui/webview2.py` and `tests/test_handoff.py`. Plus a bare `Path` in
  `bgate_mcp/server.py` where the module imports it as `_Path` — a `NameError`
  waiting for the first `blender_generate` that registered an artifact.

## [0.1.29] - 2026-07-30

### Fixed

- **Every quality gate in the 3D path was a false negative.** `combine()`
  returned `ok=True` with `checks=[]` on an assembled asset carrying zero
  materials, zero images and zero textures — the exact "21 materials and ZERO
  images" failure the layered path was written to prevent, one level up, because
  the runner's `issues` were computed and then dropped on the floor. Measured
  against real Blender 4.5, before → after: a cap bound to a bone landed at
  `(0,-2,1)` rotated 90° → `(0,0,2)` rot 0; a layer's `rotate=[0,0,90]` was a
  silent no-op → applied; an authored `(2,2,3)` scale was flattened to `(1,1,1)`
  → preserved; `unweighted_verts` reported 0 of 940 → 8 of 8 and 200 of 940.
  `parent_type='BONE'` positions a child relative to the bone *tail* in
  bone-local space, so inverting `armature.matrix_world` misplaced every rigid
  layer, and `matrix_world` is a stale cache — without `view_layer.update()` the
  composition read identity and every turnaround subject was displaced by the
  whole of `centre`.

- **The importer's `Icosphere` was the whole story on weighting.**
  `io_scene_gltf2` links a bone custom shape at the world origin, parentless and
  rendering. It was the only object failing envelope weighting — dragging entire
  body layers down to `deform:nearest`, i.e. rigid — and the only thing the
  turnaround pivot was rotating, which is why four "angles" came back
  byte-identical. Body layers now settle on `deform:envelope`, and bone-heat
  across a glTF round trip went from 200/940 unweighted to 0/940 (glTF stores no
  bone tails, so leaf tails are restored from the layer record or grown).

- **The turnaround could not detect the failure it was written for.** `blown` and
  `mean` were measured over a frame that is 75–85% opaque background encoding to
  114/255, so a fully blown-white figure scored 0.20 against a 0.35 threshold and
  `mean` could never reach the dark floor. Lighting energy scaled such that a
  0.3 m prop received ~20× the irradiance of a 2 m character. Both fixed and
  measured: blown-white now rejected, 0.3 m and 10 m subjects within 1 luma.

- **`blender_sweep` could delete outside the project root.** A shared
  `base_human.blend` passed as a layer, or a hand-edited manifest, made it an
  arbitrary-file delete with `dry_run` as the only guard. Now confined through
  `assets.normalize_path` with a suffix gate, keeps the rig, and returns
  `refused` with reasons.

- **The tool told to make the logo was forbidden from drawing text.**
  `image_generate` passed no `task_kind`, and an empty kind means "give it
  everything" — so a texture prompt received the full 2D-sprite clause including
  "No text, letters, words, numbers, labels or signage anywhere", while the art
  brief instructed the agent to generate the team logo as its own layer.

- **Decals shipped opaque.** The image's `Alpha` was never linked, so the
  exporter wrote `alphaMode: OPAQUE` and a keyed logo rendered in Godot as a
  solid rectangle — worse than the z-fighting it replaced. `alphaMode` derives
  from the Alpha socket's *graph*, not from `blend_method`: through a
  `GREATER_THAN` it exports `MASK`.

### Added

- **A proportioned rigged base, so the agent stops inventing a body.**
  `bg_human` / `bg_quadruped` / `bg_prop_frame` return a clean, unwrapped,
  weight-ready mesh with a named 22-deform-bone skeleton and queryable
  landmarks. Measured: 940 verts at detail 1, 0 loose / 0 non-manifold /
  0 flipped / 0 ngons, height exact to 2e-6 m, and `ARMATURE_AUTO` binding with
  **zero** unweighted vertices. A cap fitted via `bg_fit(head_top, "on")` rests
  on the crown at 10% overlap; the same cap at a guessed 1.7 m — what gets
  written without a proportion frame — is 89% *inside* the skull, and passed
  every check the pipeline had.

- **`godot_deliver_asset`** — the last mile. Import, write `.import` with
  collider generation, purge cache, reimport, generate a `.tscn` under a
  `CharacterBody3D`, load it through the engine and screenshot it. The path used
  to stop at the `.glb`, so the only "look at it" was a Blender render of a
  Blender scene. The in-engine check now counts `Skeleton3D`/`AnimationPlayer`,
  asserts materials carry an `albedo_texture`, and measures the AABB through the
  accumulated transform — a character 40× too big read as 30.8 m locally and
  2880 m in truth.

- **`blender_layer_rerun`** — the manifest now carries the full recipe
  (`at`/`rotate`/`scale`/`bind`/`rig`) and each layer's generating script, so
  "re-run that one layer, not the character" is true for the first time.

- **3D is visible to QA.** Turnaround frames return as MCP image content and
  register one artifact *per angle*, and combined assets register at status
  `candidate`, so `art_qa_verdict` can reach a 3D asset at all.

- **`task_kind` `"texture"` and `"decal"`**, with a bible-independent form clause
  — flat albedo, no baked shading, no background for textures; "the lettering IS
  the subject" for decals.

### Changed

- **Characters face `+Y` in Blender**, with the turnaround's angle labels
  remapped to match rather than hiding a compensating rotation in the export
  path. glTF maps Blender `+Y` to `-Z`, which is what Godot calls forward.

- **`blender_texture`** takes `roughness`/`metallic`/`normal`/`emission` and
  alpha control. Every surface previously shipped as uniform 0.6-rough plastic.

- **The art seat brief: 1,442 → 773 words**, with the 3D block cut to ten
  numbered steps and keyed on `project.dimension` so a 2D project no longer
  carries 669 words about armature binding. It had been instructing things the
  tools could not do — conditioning on pinned refs through a tool with no
  reference parameter, and "DECLARE IT AND STOP" through an `ask_human` that
  never blocks. A test now reads the tool surface out of `server.py` by AST and
  fails in *both* directions if the brief drifts from it again.

- `pyproject.toml` had missed the `0.1.28` bump and still said `0.1.27`.

### Known

- The primitive path raises the **floor**, not the ceiling. The base mesh has no
  face and no fingers — a proportioned blockout, right for props, vehicles,
  terrain and block-out, and not a hero character seen close up.

- `bg_flipped` returns `0` on an internal error, which reads as "no flipped
  faces". Advisory only, but it is a check that reports clean when it failed.

## [0.1.28] - 2026-07-30

Nothing. `v0.1.28` is a second tag on `b2959a7` — the same commit `v0.1.27`
points at — so there is no diff to describe and no release that contains
anything `0.1.27` does not. The version in `pyproject.toml` was never bumped to
match it, which is how it stayed invisible; `0.1.29` corrects the number and
`.github/workflows/release-exe.yml` now refuses a tag whose version and
changelog do not agree, so a tag cannot go out undescribed again.

## [0.1.27] - 2026-07-30

### Added

- **An agent finishing now reaches somebody.** Work completed and nothing said
  so: a finished item spawned a QA agent or parked for approval, and in both
  cases the director — the seat that owns what happens next — was told nothing,
  while chains advanced silently and the only human-facing trace was a card in a
  console nobody had open. `bgate_ui/followup.py` is one subscriber with five
  branches: reopen a failure, open a QA round, leave a held item alone and say
  why, narrate a chain advancing, or debrief the director. Its decision is a pure
  function of `(events, settings, board)` — no database, no clock, no thread —
  because the loop it replaces was dead in production for weeks with a green
  suite, having swallowed every exception it raised inside a daemon.

- **A transition log something finally reads.** `queue._notify` has written every
  status change to `.bgate/notify.jsonl` since it was written, and its own
  docstring says why: so an orchestrator or the UI can tail one file instead of
  sleep-polling the queue. Nothing ever did. There is now an `event` table
  (migration 0016) that subscribers read forward from a cursor kept per consumer
  — a row id rather than a wall clock, which is what lets a dashboard that was
  off for an hour resume exactly where it stopped instead of losing the interval.
  It is a table and not the file it describes because `queue_complete` executes
  in the MCP server process while the reaper executes in the dashboard's: the log
  is multi-writer across processes, and an appended file cannot hand out a
  monotonic sequence without a lock nothing here has. `notify.jsonl` keeps being
  written, unchanged, because it is a documented surface.

- **Work chains: dependent items filed as one ordered group** (`queue_add_chain`).
  Before this the only way to say "this goes after that" was a priority, and
  priority is a preference among things that are all READY — so auto-deploy
  started the item that needed a scene in the same tick as the item that creates
  it, and the second agent wrote against a file that did not exist, reported
  done, and the damage surfaced two items later wearing someone else's name. A
  link does not become dispatchable until its predecessor reaches `done`;
  `queue_next` never hands a seat blocked work, and a refusal names the item it
  is waiting for.

- **Three approval gates, because the question is not "strict or loose", it is
  WHO IS AWAY.** `none` — an agent's own word closes its item. `agent` — the QA
  seat verifies every maker deliverable, which is what shipped before and is
  still the default. `builders` — the human approves, so finished work parks in
  `review` and the chain behind it stays blocked. Approve and reject are
  HTTP-only and deliberately have no MCP equivalent: a tool an agent can call is
  a gate an agent can clear on its own behalf.

- **A heartbeat, for the failures that are an ABSENCE of transitions**
  (`chain.stalled`, `item.aging`). An item waiting on an approval that never
  comes emits nothing, so a purely transition-driven bus would have reintroduced
  the quiet failure this whole surface exists to fix, one layer up.

- **`ask_human`** — the director's ping. An event, not a work item: a question
  that becomes a queued row is a row somebody has to dispatch in order to read,
  which is how "ask the human" turns into "spawn an agent to ask the human". The
  answer lands where it will actually be seen — the steer inbox for a live asker,
  a handoff `decision` note for one that has already finished.

- **Notifications**: a bell and drawer in the header, the unread count in the
  desktop window title, and one optional https webhook. Said plainly rather than
  implied: the bell only tells you things while the page is open, so the window
  title and the webhook are the two channels that survive a closed tab. The
  webhook refuses any host resolving to a private, loopback or link-local address
  — a loopback service POSTing a user-supplied URL is an SSRF, and it carries
  what the agents are doing as its payload. It ships off.

- **One settings surface** (`bgate_core/settings.py`, and a Settings view). The
  switches lived in four mechanisms — a SQL row, workspace docs, environment
  variables, and module constants — with nothing listing them and nothing saying
  which layer was winning. The registry DESCRIBES where each value already lives;
  storage did not move. What it adds is one validator and one precedence rule,
  env > project stored > default, with the API always reporting which layer won.
  Two kinds of override, because the existing variables are not one kind: a
  *supplying* var holds the value, a *coercing* one forces it (`BGATE_QA_GATE=0`
  forces `gate.mode` to `none`) — a boolean kill switch cannot supply one of
  three modes.

- **The agent rails open what they name.** Every path an agent mentions is now a
  file you can look at: the text with line numbers, the picture, or the diff of
  that path against the run's own base commit. Before this the rail could name a
  file and open none of them, so finding out whether the scene an agent said it
  baked had anything in it meant a second editor window.

### Changed

- **The QA gate's loop moved into the follow-up router**, which fixes a hole it
  had all along: it reviewed "only transitions after the server started", so
  every completion that happened while the dashboard was down was never reviewed
  and nothing said so. A cursor is a row id, so a restart resumes rather than
  skips. What stays in `qa_gate` is what was always worth having on its own —
  what a QA round is, when one is owed, and the brief the reviewer reads.

- **The agents canvas is readable with more than one run on it.** Phase stacks
  were drawn after the fact and never claimed the space they used — the column
  reserved 104px for a task whose stack was eight rows of 86 — so every stack
  grew down through the task below it and the canvas auto-fitted to 49%. Stacks
  now reserve their band and stay collapsed unless you are looking at that run.
  Colour also meant *state*, so every running node went the same orange; hue is
  now WHOSE (the seat's, inherited by its phases), structure is WHAT, and state
  is a treatment.

- **The console poll got cheaper by half.** Measured on a live board: 106KB of a
  162KB payload was raw step text repeated inside each phase, for phases the
  client never renders. Trimmed to the newest three, the rebuild memoized on step
  count (it ran per live agent, per poll, *per tab*), the cadence adaptive, and
  an unchanged payload now skips the repaint entirely.

- **`SessionStart` answers "which project" before an agent has to hunt for it.**
  A session started in this checkout was handed an empty board for a root that
  has a `.bgate` but no project — which reads exactly like "the game has nothing
  on it". It now says so, lists the projects that ARE games with their paths, and
  self-registers any root that is one. `BOARD` also stops claiming a dashboard is
  ready when it is serving a different project, autopilot is off, or the tree is
  dirty.

### Fixed

- `heartbeat.tick()` raised despite a docstring promising it never does. It runs
  on the router's thread, so an exception there did not merely lose a heartbeat —
  it aborted the rest of that tick, which is the notification path.
- The console's inline gate control wrote the same switch as the settings panel
  but rang no bell, so the drawer could not tell you which one somebody used.
- `dispatch.allow_dirty` and `dispatch.isolation` were read straight from the
  environment, so their toggles wrote a document nothing read.

### Known gaps

- `ask_human` is uncapped — the only agent-reachable effect here without a leash.
- An answer is written onto its question's event row, so a subscriber whose
  cursor already passed it never sees the answer.
- `notify.js` and `settingsview.js` have no tests; the repo has no JS harness.
- Claude sessions still count against the spend ceilings, which is what makes a
  `$25` daily budget stop dispatch after a night of agent work.

## [0.1.26] - 2026-07-29

### Added

- **The MCP server now ships `instructions`, so a session cannot be lobotomized
  by changing directory.** The working process was communicated four ways and
  every one was conditional: tool docstrings if the agent reads the schema, the
  `CLAUDE.md` block if the project was stamped *and* you are standing in it,
  `seat_brief` if the agent thinks to call it, and the dispatch prompt only for
  agents the dashboard spawned. A human-started session hit none of them, saw
  ~150 tool names, and reasonably concluded it should call them itself: unlaned,
  unlogged, past the QA gate, graded by the agent that did the work. The server
  is registered `--scope user`, so this string now arrives in every session on
  the machine with no per-project install. It is seat-aware for free, because
  each client spawns its own stdio server and `BGATE_SEAT` at boot is the
  session's identity. The director's mission is *read from the seat table*, not
  restated, so a project that customises it customises the brief.

- **A `SessionStart` hook that preloads the board.** `instructions` is fixed at
  boot, so it can state the role and never the situation: what is queued,
  whether the dashboard is even up to run it, which files another live session
  is holding. `clear` and `compact` are on the matcher alongside `startup` and
  `resume`, because those are precisely when the context is discarded. Silent
  outside a Builders Gate project, and guarded harder than the PreToolUse hook:
  a crash there costs one tool call, a crash here costs the session.

- **A project thread for in-flight state** (`handoff_note` / `handoff_read`).
  The board records what was dispatched and the bible records what was settled;
  between them sits what a session was halfway through, why it chose what it
  chose, and what it deliberately did not do. Appended as you go, never
  generated at the end, because a killed process, a crash and a closed window
  all fire nothing and those are the sessions worth resuming. One thread per
  project rather than per session: the per-session design required the server
  and the hooks to agree on what "this session" is, and they cannot.

- **`bgate hook-install --scope user`** installs both hooks once for every
  project on the machine, including ones that do not exist yet. The handler was
  always project-agnostic; only the settings entry was per-repo, and a switch
  you must remember to flip in each new project is off exactly when a fresh
  project needs it. User scope pins the absolute interpreter where project scope
  keeps `python -m`, because the project copy is committed and a bare `python`
  in `~/.claude` resolves against whatever is on PATH, dies on
  `ModuleNotFoundError`, fails open, and stops enforcing with no symptom.

- **The VFX pipeline** (`bgate_core/vfx.py`, the `vfx_animate` tool): key-frame
  motion derivation with art-direction scoping and a keyed chroma kind. See
  Known issues.

### Fixed

- **An agent's reported file list is now the one the harness observed.** A QA
  agent closed a gate reporting "no files were touched" while having written its
  own `.bgate/progress/item-<id>.jsonl`, which the WORK MANIFEST rule tells every
  seat to keep. The report was not dishonest — it answered about the project's
  files — but nothing in the system could contradict it: the hook logged only
  failures, the activity ledger records no writes, and `path_lease` is reaped on
  expiry by design. A required disclosure field would catch omission and never
  inaccuracy, so instead the hook records what it already sees and
  `queue_complete` attaches it.

- **Every seat was instructed to write a file its own lane forbade.** No seat's
  `write_globs` contain `.bgate/**`, so the WORK MANIFEST instruction was refused
  for all seven wherever the hook was installed. `METADATA_LANES` is a two-entry
  carve-out rather than `.bgate/**`, because that directory also holds `game.db`
  and the 0600 dashboard token.

- **The PreToolUse hook ignored the widest-reach agent in the system.** `if not
  seat: return ALLOW` was right that a hand-started session adopts no seat and
  wrong about what follows: it holds the director seat, and what matters is not
  its lane but whether another run is already in the file. It also had no
  execution identity, so the lease machinery could never see it — two sessions
  edited one module on one afternoon and neither was told. `BGATE_DIRECTOR_MODE`
  is `off` / `collide` (default) / `warn` / `block`; the default only refuses a
  genuine collision, because a gate people switch off is worth less than a
  quieter one they leave on.

- `bgate hook-status` no longer reports a seatless session as inert when it is
  not, and the scaffolded `CLAUDE.md` no longer claims `queue_next` "marks it
  dispatched" — it is a read-only `SELECT`, so two agents calling it get the
  same row.

### Known issues

Committed deliberately, with the findings on record rather than discovered later:

- `vfx_animate` joins model-supplied `name` / `out_dir` onto the output directory
  with no containment, so a traversal writes outside the project root.
- `peak` is not clamped before the notes slice, so an out-of-range peak emits a
  false coverage note or silently skips the decay check.
- `jitter` is omitted from the headroom reservation, so `churn` clips at the cell
  wall at small cell sizes.
- `_WORLD` lists only `background` / `tile` while `chroma.PLATE_KINDS` has seven,
  so `concept`, `plate`, `backdrop` and `splash` lost the isometric directive
  they previously received.
- `seats.py` instructs the seat to call `image_generate` with `task_kind='vfx'`,
  a parameter no MCP tool accepts.

## [0.1.25] - 2026-07-28

### Fixed

- **Every wheel and exe shipped without the Godot Web export preset.**
  `templates/shared/.gitignore` is a template — it is copied into scaffolded
  game projects, where ignoring `export_presets.cfg` is exactly right, because
  that file holds per-machine export config and can carry an Android signing
  password. It also sits inside this repository, so git applied it here and the
  preset every scaffolded project is supposed to ship was never committed.

  It looked fine on the machine that wrote it: the file is on disk, so a local
  build contained it and the tests passed. A fresh clone — CI, a contributor,
  the release build — produced an artifact without it, so `bgate publish` failed
  on the one manual step the preset exists to remove. A package-data glob cannot
  include a file that is not in the checkout. Force-added, with a test that every
  file under `templates/`, `bgate_ui/static/`, `bgate_site/theme/` and
  `bgate_engine/` is tracked.

  0.1.23 and 0.1.24 carry the broken artifact; a repository rule forbids moving a
  published tag, so this is its own release.

## [0.1.24] - 2026-07-28

### Fixed

- **A clean install pulled MCP SDK 2.0 and every MCP tool stopped importing.**
  The SDK's 2.0 removed `mcp.server.fastmcp`, which the whole of
  `bgate_mcp/server.py` is built on, and the dependency was a floor with no
  ceiling — so nothing changed but the date and the server broke on any machine
  that had not already resolved it. Pinned to `mcp>=1.2.0,<2`; lifting that is a
  port, not a version bump.

  The 0.1.23 tag carries the unbounded requirement and a repository rule forbids
  moving a published tag, so this is its own release rather than a correction to
  that one.

## [0.1.23] - 2026-07-28

### Added

- **The Agents view is a console, not a board.** It was a composer over four
  kanban lanes: you typed a task, picked the seat yourself, and watched cards
  move. What the floor actually does is a conversation — you say what you want,
  the director decides who does it, and work hands off between seats — and none
  of that shape was on screen. The view now has a transcript on the left and a
  live delegation graph on the right, both painted from one polled request
  (`GET /api/console/state`) instead of the three-plus-N the old view needed.
  The kanban board is still there under the `board` toggle.
- **A message to the director is a work item with its own log.** `POST
  /api/console/say` files what you typed (`source='chat'`), dispatches it, and
  fences your words inside the brief so the transcript can show the sentence you
  actually sent rather than the 80-character title it was cut down to. The
  director answers in the chat and delegates the pieces, stamping each child
  with the same `DELEGATED-FROM: #id` line the delegate endpoint uses — so the
  children of a sentence survive a reload instead of living in a JS variable.
- **A staging queue between the conversation and the graph.** Queued work waits
  in its own panel with `deploy`, `deploy all`, per-ticket discard and `clear`.
  Nothing reaches the canvas until it is deployed, so the graph only ever shows
  work that is actually running — a plan drawn next to work in progress is what
  made the old view read as a backlog.
- **Auto-deploy** (`bgate_ui/autodeploy.py`), a daemon thread that dispatches
  queued work as slots free up so a delegation's children fire the moment the
  parent lands. It holds back `qa-gate-escalation` (that item exists because a
  human has to decide), cools a refused item down instead of hot-looping it, and
  ends its pass on a floor-level refusal. The last refusal is served with the
  switch, because an autopilot quietly refusing looks exactly like one with
  nothing to do.
- **Phases — the pockets of work inside a running agent.** `bgate_ui/phases.py`
  splits an agent's step stream into units on its own narration, with each
  phase carrying its tools, its errors, the artifacts that appeared during its
  window, and the images it looked at. A run with no narration comes back as one
  phase called "working"; the heuristic does not pretend otherwise.
- **You can see what the agent sees.** Any step that touched an image renders
  that image inline, and the narration straight after it is tagged as the
  agent's reading of what it saw. Sprite sheets play, audio gets a player.
- **Sign-off gates.** When an agent reports an item done, an approval node
  appears on the graph: accept records that a human has looked at it, or send it
  back with a reason that lands in the brief for whoever picks it up next.
  'Done' is the agent's claim; without a separate record there was no way to say
  "and a human agrees".
- **Cross-agent work is drawn.** Dashed edges where two live items are producing
  the same logical asset, where one is blocked on a path another holds, and
  where one agent steered another.
- **The director can steer its own workers.** New `agent_steer(item_id, text)`
  MCP tool over a file inbox (`bgate_core/steerbox.py`) drained by a pump thread
  in the dashboard, which is the only process holding an agent's stdin. Being
  unable to say "not like that" mid-run made the director a dispatcher. The
  human can aim the same channel from the chat box.
- **Console sessions.** `clear` files the current conversation and starts a
  fresh one; `history` opens an earlier one with every turn's log still
  reachable. Nothing is deleted — only a cut line moves.
- **A talking-portrait pipeline** (`bgate_core/talkhead.py`, `image_talkhead`):
  one anchor, N mouth states, registered on silhouette width and stitched to a
  sheet, plus the app's own mascot drawn with it.
- **A kill switch.** `bgate panic`, and a red `stop all` in the console: auto-
  deploy off first (or the loop dispatches a replacement into the gap), every
  agent killed by process tree, every pid in the on-disk ledger reaped —
  including ones an earlier dashboard spawned — and the items settled so the
  board stops claiming work is running. The CLI path matters on its own: the
  moment you need this is the moment the dashboard may be the wedged thing.
- **A stall timeout.** A session that is alive but has produced no observable
  output for 25 minutes (`BGATE_STALL_S`) is killed as hung. Silence is measured
  against the log AND files under `.bgate_out/` and the game's assets, because a
  30-minute atomic image batch writes nothing until it returns and killing those
  is how healthy agents used to die.
- The test suite runs in CI. Nothing ran it before — the only workflow built the
  exe — so every guarantee in `tests/` held as long as somebody remembered to
  run pytest locally. The exe smoke test also asks `/api/routes/status` whether
  each route module made it into the bundle: discovery is `pkgutil`-based, so a
  frozen build can drop half the API while serving `index.html` perfectly.

### Fixed

- **A run that ended in an error sat there saying "thinking" until its runtime
  ceiling fired.** The CLI reports expired OAuth, max turns or an execution
  error as a result event and then goes back to waiting on the stdin held open
  for steering, so nothing settled it: the item stayed `dispatched`, the process
  stayed alive, and an expired login read as a hung dashboard. The watchdog now
  reaps a terminal error with the CLI's own words on the item. Only error
  results settle a run — a successful result with no `queue_complete` is an
  agent pausing mid-work, and settling that would break steering.
- **Two callers could spawn two agents for one item.** Everything `dispatch()`
  does between its liveness check and the actual spawn — scope, budget, git
  state, cutting a worktree — takes seconds without the lock, and the second
  spawn overwrote the first in the process table. The first was then never
  reaped, never budget-checked and never killed: it billed until somebody found
  it in Task Manager. The start is now reserved under the lock.
- **`queue_list` answered with every work item a project ever had, briefs and
  all.** On a real board that is 150,000 characters — past the tool-result
  ceiling, so the call failed, the CLI spilled it to a file, and the agent spent
  its next two turns grepping a dump of its own queue. It is paged now with
  brief previews, and `queue_get(item_id)` returns one item whole.
- **`seat_brief` came back at 93,000 characters.** Every list was already
  capped, but forty of anything is only small if the items are small, and bible
  sections, notes and complaints are prose. Caps are tighter, quoted prose is
  trimmed, and a measured pass shrinks the biggest fields until the payload fits
  a budget — with everything it cut named in `truncated`.
- The director's own brief now tells it not to gather context it does not need:
  routing a sentence to a seat does not require that seat's briefing, and
  fetching one turned a five-second decision into a minute of tool calls.
- The parsed-activity cursor is serialized. Two threads read it now (the
  console and the per-run watchdog) and interleaving them absorbed the same
  bytes twice — duplicated steps and doubled counts.
- Image paths are pulled out of agent logs by tokenizing rather than by a
  regex whose character class made it quadratic: ~5 ms on a single narration
  step, which over a 500-step ring and a three-second poll was more CPU than the
  rest of the dashboard.
- Auto-deploy no longer dispatches a work item whose brief is still the
  placeholder written by the first half of a two-statement create.
- A steer message that cannot be read or delivered no longer destroys the rest
  of its batch — `take()` has already removed them all from disk.
- The detail rail keeps a half-typed steer, its caret and its focus across a
  poll, and no longer forces itself open three seconds after a node drag.
- Agent-authored tool names are escaped before they reach the rail, and a seat
  name is whitelisted rather than interpolated into a CSS `var()`.
- The composer no longer wedges permanently — disabled, with its re-entry gate
  stuck — when a render throws after a failed poll.
- Node repaints batch their edge pass. Patching a dozen nodes per poll ran one
  full edge re-render each, and every edge measures both its ports.
- **A budget with `max_runtime_s` set to 0 meant no wall clock at all**, so an
  agent that never self-reported ran until somebody noticed. 0 is the hard cap
  (2 hours, `BGATE_MAX_RUNTIME_S`) now, not infinity.
- `image_talkhead` refuses a near-empty generation instead of scaling its
  two-pixel silhouette up to match the anchor and dying in `MemoryError`, and it
  contains `res_dir`/`name` — it writes with pathlib, so the lane hook never
  sees it and `../../..` would have landed outside the project.

## [0.1.22] - 2026-07-28

### Fixed

- **The first-run screen offered to create a project in `C:\Windows\system32`.**
  A double-clicked executable does not inherit a meaningful working directory —
  a shortcut with no "Start in", or a launch from the Run dialog, hands the
  process system32 — and the screen read `Path.cwd()` straight out, then failed
  with a raw `PermissionError` in a red box. New projects now land under the
  working directory when it is a real one and `~/BuildersGate` when it is not.
  Drive roots, `%SystemRoot%`, `%ProgramFiles%` and `%ProgramData%` are refused
  whether or not they happen to be writable. `bgate serve` from a terminal is
  unchanged.
- A `PermissionError` from the create endpoint now answers 400 with the
  directory and a suggestion instead of leaking `[WinError 5] Access is denied`.

## [0.1.21] - 2026-07-28

### Fixed

- **The desktop app had no icon.** `bgate_ui/webview2.py` called
  `GetModuleHandleW` without declaring a ctypes `restype`, so the 64-bit module
  handle was truncated (`0x7ff71d540000` → `0x1d540000`). `LoadImage` then
  looked for the icon resource in a module that is not loaded, returned NULL,
  and Windows substituted its generic application icon on the taskbar and the
  desktop. Every Win32 call carrying a handle now declares both `restype` and
  `argtypes`, and the icon has a file-on-disk fallback.
- **The logo was a redrawing of itself in four places** — the rail brand, the
  first-run card, the dashboard tab and the arcade tab each carried
  hand-approximated geometry with the proportions and the chevron angle off, and
  the two favicons dropped the broken gate post entirely. All four are now
  traced from `packaging/logo.svg`.
- **The rail brand painted the whole mark one colour**, collapsing the gate and
  the chevron into a single shape. New `--brand-post` token: the logo's own
  `#1800ad` on the light ground, the same hue lifted to `#8f7cff` on the dark
  one, where that blue is invisible.

### Added

- **"Run anyway" on the uncommitted-changes refusal.** Dispatch declines to
  start an agent on a dirty git tree, because it records `base_commit` and a
  diff taken over uncommitted work cannot separate the agent's edits from
  yours. That refusal used to arrive as a toast reading "dispatch with
  `allow_dirty`" — a parameter the browser had no way to send. The route now
  forwards it and the dashboard offers the choice, with the offending paths
  listed.

## [0.1.2] - 2026-07-28

### Fixed

- The standalone Windows build ships as a folder rather than a self-extracting
  executable. The 0.1.1 binary was quarantined by Defender as
  `Trojan:Win32/Sabsik.TE.A!ml` — a machine-learning guess triggered by the
  `--onefile` stub unpacking a compressed archive into `%TEMP%` and running code
  from it. Downloads are now a zip with a published SHA256.
- It is still **not code signed**, so Smart App Control will refuse to launch it
  ("we can't confirm who published BuildersGate.exe"). Repackaging cannot fix
  that; it needs a certificate. `pip install -e ".[desktop]"` avoids it entirely.

## [0.1.1] - 2026-07-28

A UI/UX pass over the whole dashboard, and the first downloadable build.

### Added

- **Light and dark grounds**, switchable from the rail, remembered, and applied
  before first paint so there is no flash. Follows the OS unless you pick one.
- **Desktop app.** `bgate app` runs the same local server in a native window
  (Edge WebView2 on Windows, so nothing extra to install). `pip install
  "builders-gate[desktop]"`.
- **A standalone Windows build** for people who do not want Python. Built by
  `python packaging/build_exe.py`, which boots the result and fetches real
  assets out of it before declaring success, then zips it with a SHA256.
  It is **not code signed**: Defender's ML quarantined the first onefile
  attempt as `Trojan:Win32/Sabsik.TE.A!ml`, and Smart App Control refuses to
  launch unsigned binaries at all. Shipping as a folder rather than a
  self-extracting exe removes the first trigger; the second needs a
  certificate. `pip install -e ".[desktop]"` remains the recommended route.
- **Sprite editor**: sheets grouped by category, a named edit history you can
  click to step back through, a looping animation preview, and onion skin that
  shows the frames before *and* after the one you are painting.
- **Audio lab**: a layer timeline with per-lane waveforms and drag-to-offset,
  file import, microphone recording, and non-destructive per-layer trim.
- **World bible**: the relationships already in the lore data are drawn as a
  graph, with search and canon/kind filters.
- **Scene composition convention** — one editable thing, one named node —
  written into the seat briefs, the scaffold template and the QA fail list.

### Changed

- The dashboard's CSS is one stylesheet (`bgate_ui/static/app.css`) with a
  declared cascade order, replacing six `<style>` blocks that had accumulated
  inside `index.html` across five redesigns.
- Every `<select>` is a searchable in-app combobox. A native dropdown draws its
  popup through the OS, so none of the app's styling reached it.
- Every `window.prompt` / `window.confirm` is an in-app dialog. Destructive
  actions still ask.
- The clip editor gained parameters: numeric selection with zero-crossing snap,
  a gain slider, fade curves, and A/B against the original before committing.
- The sprite editor and audio mixer are Studio pages rather than fullscreen
  overlays launched from a small button.

### Fixed

- Secondary text and placeholders failed WCAG AA (2.5:1 and 3.9:1). Every
  colour that carries text now clears AA on both grounds.
- Card borders were drawn with a *surface* token in 44 places, which made them
  invisible on the light ground.
- Canvas drawing cannot resolve `var()`; several modules had frozen to one
  ground working around that. Tokens now resolve at draw time.
- The active navigation item was tinted violet, left over from a theme that was
  reverted everywhere else.
- Cancelling a rejection or a regeneration prompt submitted it anyway — one
  persisted a blank reason as precedent, the other queued a paid image job.
- Visiting the audio tab left a document-level key handler running, so Space,
  Ctrl+S and Backspace acted on an invisible clip from anywhere in the app.
- `sys.executable` is the executable itself in a frozen build, so the doctor's
  speech-to-text probe launched a new copy of the application per check.

### Removed

- **Studio "Asset flow"** — duplicated the Assets library and the art seat.
- **Studio "Game editor"** — it had no save and no write path; it read the
  Godot tree, took screenshots and dispatched queue items, all of which the
  Playtests and Agents views already do.

## [0.1.0] - 2026-07-27

First public release. Everything below already existed when the repository was
opened; this entry describes the state, not a set of changes against a previous
version.

### Added

- **MCP server** (`bgate_mcp`, stdio/FastMCP) exposing the whole pipeline as
  tools: design bible, lore canon and `canon_check`, scope tiers and the cut
  line, seats, asset registry and locks, queue, workflows, playtest, iterations.
- **Seven agent seats** (director, narrative, gameplay, tech, art, audio, qa)
  with write lanes, one-call briefs, a shared blackboard, and a PreToolUse hook
  (`bgate hook-install`) that enforces lanes and locks.
- **Godot adapter.** Headless run and project check, asset import with
  in-engine inspection, live screenshots, 2D/3D project scaffolds with telemetry
  and an F1 live-tuning overlay wired in.
- **Blender adapter.** Headless `bpy` returning structured facts (tri/vert
  counts off the evaluated mesh, UV warnings, materials), sprite factory, glTF
  export with modifiers applied and game-readiness checks.
- **Two image providers.** OpenAI `gpt-image` and Krea (a catalogue of 14
  models with per-request pricing), behind quality tiers, with chroma-key alpha
  extraction since neither returns usable transparency.
- **Dashboard** (`bgate serve`). Nine views over one SQLite store, including
  live agent steering, node editors, per-seat workspaces, playtest review, the
  asset registry, the project atlas, the world bible and the iteration timeline.
  No build step, no node, no CDN.
- **Playtest mode.** Screen and voice capture, whisper transcription, feedback
  classification, and a join against game telemetry on one clock.
- **`bgate publish`.** Turns every game on the machine into a static arcade
  site, respecting the target host's per-file upload limit (Godot 4's ~38 MiB
  `index.wasm` versus Cloudflare's 25 MiB ceiling).
- **`bgate doctor`.** One bounded probe of every external dependency, exiting
  non-zero if anything is unavailable.
- MIT licence, `.env.example`, and this changelog.

### Security

- Dashboard mutations require a same-origin request, a per-project bearer token
  from `.bgate/ui-token`, and a `Host` header that resolves to loopback. That
  last check is not disabled by `BGATE_NO_AUTH`, because it is what closes DNS
  rebinding. See [SECURITY.md](SECURITY.md).
- `.env` and `.env.*` are gitignored here and in every project `bgate init`
  stamps out.

### Known limitations

- Windows is the supported platform; Linux is best-effort and macOS is untested.
- `bgate_engine/` is a design proposal with no runtime code, and its central
  claim was withdrawn after the experiment in its own `DESIGN.md` §16.5 came
  back negative.
- The audio seat workspace is a deliberate v1 (library, playback, cue sheet).
- The dashboard's error surfacing is uneven; see `docs/ui-ux-audit.md`.

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.32...HEAD
[0.1.32]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.31...v0.1.32
[0.1.31]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.30...v0.1.31
[0.1.30]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.29...v0.1.30
[0.1.29]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.28...v0.1.29
[0.1.28]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.27...v0.1.28
[0.1.27]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.26...v0.1.27
[0.1.26]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.25...v0.1.26
[0.1.25]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.24...v0.1.25
[0.1.24]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.23...v0.1.24
[0.1.23]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.22...v0.1.23
[0.1.22]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.2...v0.1.21
[0.1.2]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Thepizzapie/BuildersGate/releases/tag/v0.1.0
