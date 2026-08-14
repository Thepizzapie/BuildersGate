# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

## [0.1.41]

Everything in this release is one thing: the app you download now does what the
app you ran from a source checkout always did.

Several things were broken in every release before this, and none of it showed
up unless you installed the app and used it. The build only ever checked that
the code compiled, never that the finished program could open a window, record
audio, or start the way people actually start it.

### Fixed

- **The app could not start from a shortcut.** A windowed PyInstaller build
  launched from Explorer or the Start Menu has no stdout, and uvicorn's logger
  calls `.isatty()` on it while starting up, so the process died before it ever
  opened a port. It only worked when launched from a terminal. The build now
  boots the binary with no console attached and fails if it cannot serve a page,
  which is how the real launch actually happens.

- **The app's own window code had never run.** On Windows there are two ways to
  open the window and the good one needs `comtypes`, which nothing declared. It
  happened to be installed on the machine this was written on, so it worked
  there and silently fell back everywhere else. Declaring it turned up two more
  faults inside that path, both of which crashed before a window appeared.

- **Playtest recording could not work.** The audio libraries were left out of
  the bundle as "optional", but the recorder captures the mic through them, so
  leaving them out did not make audio optional, it made recording impossible.
  The Playtests screen sat on "record unavailable" and told you to run a `pip`
  command that a packaged app has no way to run.

- **Speech to text was left out for a reason that was not true.** The build
  notes said it drags in PyTorch and CUDA for 415 MB. It does not. It runs on
  something else entirely and costs about 121 MB. It is in the download now.

- **A missing transcriber disabled the Record button.** Transcription decides
  whether you get a transcript, not whether you can record. Checks now say
  whether they block recording or just take a feature away.

- **Menus and drawers rendered behind the page.** The notification drawer and
  the project menu were nested inside chrome that creates its own layer, so they
  could never sit on top of anything. Both are attached to the page root now.

- **Clicking a project in the switcher did nothing.** The menu closed on mouse
  down, which destroyed the button before the click landed.

- **"Open Orchestration" went nowhere**, and **brainstorm's reset left every
  message on screen** even though the reset itself had worked.

- **The test suite wrote into your real project list.** A full run registered
  its throwaway projects on the machine, and they showed up in the app as
  projects you could open until the temp folders were deleted underneath them.

### Added

- **An installer.** `BuildersGate-setup.exe` puts the app in your user folder
  with no admin prompt, adds a Start Menu entry and an uninstaller, upgrades in
  place, and refuses to install over a running copy. The zip is still there for
  anyone who prefers it.

- **The window has its own title bar** instead of the grey Windows one, with
  dragging, snapping and edge resizing intact.

- **A project switcher**, on the rail, with New game and Open an existing game
  next to it. Switching projects was possible from the API the whole time and
  had no button anywhere in the app.

- **Missing tools install themselves.** ffmpeg is a button that downloads a
  pinned, checksummed build into a folder the app owns, instead of a paragraph
  telling you what to type. Nothing downloads until you ask for a feature that
  needs it.

- **Settings read like English.** All 42 of them have a name instead of an
  identifier, and each group has an icon. The identifier is still there,
  underneath, because that is what you search for.

### Changed

- **Releases are reproducible.** The build used to pick up whatever happened to
  be installed on the machine doing the building, so the same commit produced a
  59 MB download in one place and 37 MB in another. It now builds in a clean
  environment from a locked list of 69 packages, each checked against a known
  hash. A swapped dependency stops the build instead of shipping.

- The app no longer loads the MCP toolkit just to build one small config file,
  which was quietly pulling three unrelated cloud SDKs into the download.

- The logo is updated everywhere it appears: the app icon, the installer, both
  favicons, and the mark drawn in the interface.

### Known

Nothing here is code signed, so Windows will still warn you about an unknown
publisher on first run. That needs a certificate, and there is not one yet. The
SHA256 of both downloads is published next to them.

## [0.1.40] - 2026-08-12

Forty-nine commits. The tool count went from 144 to 196, but the shape of this
release is not new surfaces: it is the cheap check that goes in front of an
expensive one. A cutscene now has a reel you watch before you buy it, a pose row
gets measured before anybody slices it, every paid tool asks the budget, and the
board stops refusing its own work. Three security fixes and a full documentation
sweep close it out.

Sprite sheets, in two halves. On the **generation** side, the anchor was the
weakest reference configuration available and is a model sheet now. On the
**assembly** side, the painted path could already prove a sheet was *the same
character*; it had almost nothing that could prove the sheet was *a character
moving*, and those fail in different ways. Written up in full, with the
measurements and with what was deliberately not built, in
[docs/sprite-animation-research.md](docs/sprite-animation-research.md).

### Added

- **Machine-wide API keys (`~/.bgate/.env`), and `bgate key` to manage them.**
  A credential belongs to the person, not to one game, and it used to be
  reachable only through a project root, so the tool you would reach for to
  diagnose a missing key was the one tool you could not run without a project.
  Keys now resolve through three layers, most specific first: a shell variable
  beats the project `.env`, which beats the machine-wide one. `bgate key` prints
  which layer is actually in force per provider, which is the question worth
  asking when a key is set and nothing works. Clearing a project key *uncovers*
  the machine-wide one rather than leaving the provider unset until a restart.

  `bgate key set <provider> [--global]` prompts with echo off and takes no key
  argument at all: the project's own rule has been "never put one on a command
  line" since a key was committed once, and a convenience flag would be that
  rule with an exception carved into it. The dashboard's Generators panel has
  the same choice as a tick box. There is deliberately **no MCP tool** that
  writes a key: an agent that can write credentials can hand itself a provider
  nobody paid for. `~/.bgate/.env` is written `0600`, and `~/.bgate` is not a
  repository, so it has nothing to leak into.

- **A scratch project at `~/.bgate/scratch`, for generations that belong to no
  game.** The other half of the same problem: a key you can set without a
  project is only useful if something can then *use* it without one.
  `image_generate`, `image_edit`, `image_sprites` and `image_talkhead` fall back
  here when there is nowhere else, and `project_dir="scratch"` asks for it
  explicitly from inside a project you would rather not touch.

  It is a real project, not an output folder, because everything downstream of a
  generation needs one (the artifact registry, the spend ledger, `.bgate_out`),
  and the first question anyone asks of a loose file is what it cost. It carries
  no game: a tool that needs an engine still says so in its own words. It is
  created on demand, so a user who never generates outside a project never gets
  a directory they did not ask for, and it sits at the BOTTOM of the discovery
  chain, below the remembered active project, so anyone who has run `bgate
  init`, `adopt` or `use` keeps landing in their own work, and a mistyped
  directory keeps failing loudly instead of quietly filling a folder nobody
  looks in. Tools that edit game files, run Godot or take locks are unchanged
  and still refuse. `project_status` says when the scratch project is the one
  in use.

- **The anchor is a model sheet now (`anchor_views`, default 3).** The largest
  single lever found, and it is a reference policy rather than a mechanism. Every
  reference a sprite run carried was the *same view* of the character: one
  front-facing idle, plus previous frames that are near-copies of it, which is
  the weak configuration: two to three images from *distinct angles* carry far
  more identity than more of the same angle, which is why a model sheet exists
  and why animators keep the profile and three-quarter views on the desk. It
  bites hardest in the ordinary case, a side-view game asking for side-view poses
  against a front-view anchor: the model re-invents the profile on every call and
  re-invents it differently each time, and a re-roll cannot fix that because it
  buys another guess at information the anchor never carried. A three-quarter and
  a profile view are now generated off the approved anchor once and passed on
  every pose call. Two extra generations per character against one per pose plus
  one per re-roll; priced into the spend gate before anything is bought.

- **Character work on Krea is pinned to `nano-banana-2`.** The provider's general
  default, `krea-2-large`, conditions on a reference as *style*, and a style
  reference cannot be asked to hold a subject through a pose change because
  holding the subject is not what it does, and the adapter already records the
  measurement, krea-2-medium drawing a face in seven of eight frames when four
  were specified as back views. `nano-banana-2` takes its references as edit
  inputs, keeps `styles` so a trained LoRA still rides alongside, and bills a
  flat $0.06 against krea-2-large's $0.065-with-references, so it is not a cost
  regression. Scoped to the `anchor` and `animation` kinds: an item, a prop, a
  decal or a VFX key frame has no pose continuity to preserve. Naming `model`
  still wins.

- **`sprite_plan`, and archetypes for `image_sprites`.** The key poses for ten
  standard actions, with their timing: a walk as contact / down / passing / up
  once per leg, an attack as anticipation / contact / follow-through / recover
  with the impact frame held and the wind-up rushed. `sprite_plan` costs nothing
  and returns the poses and the price; `archetypes=["idle","walk4","attack"]`
  runs exactly that plan. The failure this exists to stop is not a broken sheet:
  it is four frames named `walk/0`…`walk/3` described as "walking", which
  assembles perfectly, passes the identity gate, holds its palette, and animates
  like a character sliding along the floor. Nothing rejected it, because nothing
  was wrong with any single frame.

- **Per-frame timing in the emitted resource.** Godot 4's `SpriteFrames` has
  carried a relative per-frame `duration` all along and this project wrote a
  literal `1.0` for every frame it ever produced. A uniform hold is the flattest
  reading of any action, and it is why a generated punch read as four pictures of
  a punch. Loop and fps are now per-animation too: a 6fps idle and a 12fps
  attack on one sheet is normal, and one sheet-wide speed was a compromise
  between two right answers.

- **Ping-pong cycles.** Three drawings played 0, 1, 2, 1 are a four-step cycle
  that costs three generations and *cannot* have a loop seam, which is what a
  breathing idle or a hovering pickup wants. Godot has no ping-pong loop mode, so
  it is baked into the frame list, which is where the plan belongs anyway.

- **Palette locking (`palette_lock`, default `"auto"`).** The existing gate
  detects palette drift and pays for a re-roll. Quantising each frame to the
  reference's own palette makes drift *unrepresentable* instead. It is a
  posteriser, so `"auto"` measures the reference and switches on only for flat,
  cel and limited-palette art, where it is free; painterly art is left alone.

- **A motion report on every assembled sheet.** Four faults the identity judge
  structurally cannot see, because all four are perfectly on-model: two frames
  that are the same drawing, two adjacent frames sharing almost no silhouette, a
  cycle whose last frame does not flow into its first, and a figure in more than
  one piece. Advisory, and surfaced as chips on the art card: a duplicate frame
  is fixed by a different pose description, not by re-rolling the same one.

- **A 3D model viewer and editor**, the third page beside the sprite editor and
  audio lab. The Blender/image-to-3D pipeline has generated `.glb` files for a
  while with nowhere in the dashboard to actually look at one, and this closes
  that gap: orbit camera with environment lighting, shaded/wireframe/unlit/
  normals display modes, an outliner with per-node visibility and tint, and
  animation clip playback for rigged characters. The editing half is named
  attachment **sockets**, a 3D position and rotation, optionally hung off a
  node, placed by clicking the mesh, which is rigmap's sprite slot-anchor
  system carried into three dimensions and deliberately shares its taxonomy
  (`main_hand`, `off_hand`, `head`, ...): a project that ships both a 2D rig
  and a 3D character does not maintain two vocabularies for "where the sword
  goes." The mesh's bytes are never rewritten, only a JSON sidecar next to it,
  because a browser cannot safely re-export a `.glb`, so this surface is look, label,
  never repaint. Reads Draco-compressed geometry and KTX2/Basis-compressed
  textures too, since Blender's glTF exporter offers Draco as a one-click option,
  and a loader that only handles the uncompressed case would fail on exactly
  the models that used it. three.js is vendored under
  `bgate_ui/static/vendor/three/`, self-contained like the CodeMirror build
  beside it.

- **A cinematic seat, and generated cutscenes that the engine can actually
  play.** The eighth seat, and the first added since the table was written. It
  owns cutscenes, trailers and attract-mode video: write a shot list for free,
  buy one shot at a time, watch each one, keep the ones that work, and assemble
  them into a cut. `kie.generate_video` could already buy a clip; nothing could
  receive one, and the `video` capability had been listed with no provider behind
  it since the column was added.

  **Keeping a shot transcodes it, and that is the whole point.** Godot plays Ogg
  Theora and nothing else in core (H.264 is patent-encumbered and WebM went away
  in 4.0), while every video model returns an `.mp4`. An `.mp4` in a Godot project
  produces *no import error*: it is simply never loaded, so the scene runs
  perfectly with a blank rectangle where the cutscene was. A pipeline that copied
  its output in, which is exactly what music correctly does for `.mp3`, would put
  a green badge over an unplayable file.

- **Style, applied to every shot rather than remembered per shot.** Eleven
  presets (anime, noir, comic, painterly, pixel, stop-motion, CG, watercolour,
  VHS, silhouette, live action), each carrying the trap it comes with; a style
  note in the project's own wording; and style reference frames, which beat both.
  Free prose is a first-class style: an unlisted word is treated as prose, not
  refused. Naming no style is reported as the silent choice it is, because a
  model given no instruction uses its own house look, which differs per model and
  per version. Changing the style resets already-generated shots and says so,
  since a clip rendered in the old look is not a rendering of the new one.

- **More than one video model, without guessing at any of them.** The pipeline
  speaks intent (seconds, shape, quality, first/last frame, refs, audio), and
  each model's table entry says what it calls those. kie's own catalogue does not
  agree with itself: Sora 2 counts `n_frames` and spells its shape "landscape"
  where Seedance takes `duration` and "16:9". `cinematic_register_model` adds a
  model from a reference page a human has read, stamped as registered so nothing
  confuses it for a verified entry.

- **Anchored generation through kie, which the adapter had declared
  impossible.** Its docstring said flatly that a local pinned ref cannot reach a
  kie model. True of the generation endpoints, false of kie: there is a file
  upload API on another host that takes base64 and returns a URL those endpoints
  accept. One missing call, not a missing capability, and it matters far more
  for video than for images, because an anchored still can fall back to Krea and
  an anchored *shot* has nowhere to go.

- **Recovering a shot that was already paid for.** A generation is charged at
  submit, and the poll loop, the download and the process surviving ten minutes
  can all fail while the provider holds a finished clip. Pressing generate again
  pays twice.

- **Post-production, which is what makes it a cutscene rather than a video.**
  Transitions (cut, fade, dissolve, wipe) at the join, with the cheap concat path
  kept for a sequence of hard cuts. A music bed laid under the picture and muxed
  into the Ogg, since the picture is copied, not re-encoded, because it has been
  through Theora once already. Dialogue timed into `.srt` and `.json` captions
  off the shot list, so nothing can drift from it. A continuity check that
  extracts the real frames either side of every join and measures brightness and
  palette jumps. And `cinematic_deliver`, which writes the `.tscn`, the script,
  the skip and a `finished(skipped)` signal, so gameplay plays a cutscene in
  three lines instead of hand-authoring a video player.

- **An animatic, and a previs gate in front of the money (`cinematic_animatic`,
  `bgate_core/animatic.py`).** `plan()` wrote a shot list and `generate_shot`
  bought a clip, with nothing in between, so the first time anyone saw the ORDER,
  the rhythm, or that a scene ran long was after paying for all of it, when the
  only cheap edit left is deleting shots. The animatic cuts the storyboard panels
  together at their planned durations with the planned transitions, calls no
  model and spends nothing. A beat with no still gets a slate held at full
  length, because a reel shorter than the scene reads as finished and that is the
  one outcome which makes previs worse than useless. It reports average shot
  length, which is the number that says whether an edit reads.

- **A LOCATION rail on every sequence (`cine_location`, plus `location` on
  `cine_shot`).** A sequence carried a LOOK rail and a shot carried a CAST rail.
  Nothing anywhere carried the SET, so "the office" lived only inside four
  differently worded action strings and the model correctly drew four different
  offices. On the real sequences identity and style held and the set drifted
  between every shot: the two rails that existed did not fail, the one that was
  missing did. A location is a table row because a sequence has several sets; the
  shot holds a slug rather than an id because `plan()` rewrites every row. The
  description goes into `prompt_for` in a fixed slot, after the framing and
  before the action, because trailing text modifies the whole prompt, which is
  right for a look held across a sequence and wrong for a set that changes
  between shots. Generation is now ordered by location rather than by shot index,
  which is recurrence distance control and costs nothing: it changes the order of
  a loop and buys exactly the same shots. The CUT stays in narrative order.

- **A shot size vocabulary, checked at plan time (`shot_size`).** Measured across
  two real sequences of nine shots: one close, one medium, six wides, no over the
  shoulder, no reverse, and one sequence that was push in three times running. A
  wide is simultaneously the flattest edit and the most drift prone thing to buy,
  because a wide shows the whole set, and the century old film fix for a
  continuity error, cut tighter, is also the cheapest fix for generative drift.
  `plan()` now warns on no tight coverage, three same size shots in a row, and
  multiple locations with nothing marked establishing. A fixed vocabulary is what
  makes any of that checkable.

- **`bgate_core/ffmpegbin.py`, one place that decides which ffmpeg runs.** Six
  modules independently called `shutil.which("ffmpeg")`, so there was no way to
  use a different binary short of uninstalling software. Resolution is explicit
  argument, then `BGATE_FFMPEG`, then `~/.bgate/bin/ffmpeg`, then PATH. A
  deliberately placed binary outranks PATH because PATH is usually whatever a
  package manager installed, and in the case that produced this fix, that was the
  broken one.

- **Row and sheet auditing, before anybody slices anything
  (`sprite_sheet_check`, `spritekit` section 6).** Every existing sprite check
  runs on frames that have already been registered into their own cells, which is
  the half of the pipeline that works. A row is not four frames: it is ONE
  drawing containing four figures, drawn left to right with each figure
  conditioned on the canvas so far, so a row is a chain with the reference pin
  removed and it degrades across the row exactly as a chain degrades across a
  sequence. Measured on rows this project generated: `attack_ne` shrinks
  monotonically at rank correlation -1.0, `idle_ne` has one head yawed against
  the other three, `walk_ne` has feet wandering 17% of the figure's height off
  the ground line while the head moves 33%, and `idle_se` and `walk_se` are
  clean. That last pair matters as much as the first three, because an audit that
  fires on everything is one somebody switches off.

  The findings are `foot_drift`, `head_drift`, `size_drift`, `size_ramp`,
  `facing_flip`, `stray_ink` and `empty_cell` within a row, and
  `sheet_size_drift`, `sheet_size_ramp` and `band_palette` across the bands of a
  stacked sheet. The two ramps read differently from the rest: they say the drift
  is monotonic, which means it compounds, which means no re-roll survives it and
  the fix is structural. It hands back an annotated copy of the image with the
  ground line, the head line and each figure's true feet and mass anchor drawn on
  it, because every one of these faults was first spotted by a human holding a
  straight edge against a screenshot, and an agent given `0.175` has to take on
  faith what a human sees at a glance. Advisory, never a gate: a turnaround
  SHOULD flip its facing and a size chart SHOULD ramp.

- **A storyboard, in front of the shot list.** Cutscene planning started where
  somebody already knows what the scene is, so the only place to work one out was
  the shot list, where every wrong idea sits one click from a paid generation. A
  premise now becomes a script and a beat per frame through one cheap text call,
  and each beat is drawn as an image, two orders of magnitude cheaper than the
  video shot it stands in for. `cast_refs` resolves the same pinned character
  files on every frame, stored as pin NAMES rather than paths, so a re-pin moves
  the pointer instead of leaving the board drawn against a character art has
  since redrawn. `source` records whether a human or a model put each frame
  there, because those are not the same evidence for spending video money, and
  approving a frame with no image is refused outright. `promote()` is the single
  verb that crosses into paid, and it wires every approved frame in as that
  shot's `first_frame`, so "anchor on an approved still" holds by construction.

- **A premise compiles to a manifest, and coverage answers what is left.** The
  brainstorm room was already the front end of a premise to plan compiler and
  the back end was a pile of loose work items. The manifest names what gets
  built, the acceptance test that settles it, and its dependencies. Slice rows
  compile onto the board in dependency order with real `depends_on` links, and
  everything else stays `spec` on purpose: the board holds the slice, the
  manifest holds the game. `plan_status` reconciles the two and deliberately does
  not stop at "built", because a sprite on disk that no scene references is not
  in the game and neither is a scene no reviewer passed. "Wired" is proved by
  reading the `.tscn`/`.tres` text rather than by asking the agent that made it.
  `board_digest` answers "what happened while I was away": finished, failed,
  awaiting you, spend, coverage, and a `blocked` field that names a dirty tree as
  the whole board stopper.

- **A worker can claim its own next item (`queue_claim_next`), and dependencies
  are a graph.** A finished worker's only move was to exit and let the board pay
  a fresh agent's whole briefing for the next item. The claim is atomic against
  the dashboard, because there are two dispatchers now and both used to read
  `queued` and proceed. Separately, `work_item.depends_on` held one parent, so a
  scene needing the sprite AND the sound AND the script could express one of
  three; migration 0033 adds `work_item_dep` alongside it, additively, and
  `queue_cut_dependency` is the repair verb for the board's one state with no
  exit, a cancelled predecessor blocking its successors permanently.

- **Audio, QA and narrative can execute their own missions.** Three of eight
  seats had no tool for their own lane. `sfx_generate` synthesizes game sound
  effects procedurally with no key and no provider, writes the `.synth.json`
  recipe sidecar dispatch's own audio rule has always demanded, and rebuilds byte
  identical output from that sidecar alone. `godot_test_run` discovers a
  project's own test scripts under either layout, runs them headless, and returns
  per script pass/fail; a SCRIPT ERROR at exit 0 fails the script, and finding no
  tests returns `no_tests` naming the directory it searched rather than a green.
  `dialogue_write` writes trees as engine loadable JSON, refusing a dangling
  goto, an orphan node, a node from which no ending is reachable, and an end node
  with choices, each by name.

- **Spend answers who spent it, and over which window.** The ledger could say
  what a project cost and what kind of thing bought it, and nothing else, so
  "which seat is expensive", the question that decides where a budget actually
  gets cut, was unanswerable. Migration 0034 adds `spend_event.seat`, filled from
  the environment inside `record()` so nearly every call site is unchanged, with
  unattributed rows kept rather than dropped. Windows were lifetime and today;
  week and month are the two people actually ask about, and someone returning
  after a break asking what last month cost was told $0.00, which reads as cheap
  rather than as not measured. Agent runs are reported as their own block,
  because they bill as subscription and are excluded from the ceilings.

- **A music generation survives the process that started it.** `generate_music`
  submitted and blocked, and the task id first reached durable storage after a
  successful absorb, so a crash mid poll lost the only handle to a batch already
  charged for. It now opens a JSON ticket before the provider call and closes it
  on absorb, the way cinematic already persisted `task_id` at submit.
  `music_stuck_tracks` is the twin of `cinematic_stuck_shots`: same states, same
  result keys, plus the retention deadline inside which a paid track can still be
  collected.

- **One verb from "saved" to "in the game", in all three editors.** The sprite
  editor, audio lab and 3D viewer all ended at a saved file that the game knew
  nothing about. The same button in the same place now offers two exits weighted
  equally: wire it here, picking a scene, parent node, node type and properties
  with a dry run before the commit; or hand it to an agent as a work item whose
  brief carries the asset path, the engine side resource, the target scene and
  parent, the attached references, a pattern to follow and a per kind "done looks
  like". `path` is always the file on disk, never the editor buffer, so an
  unsaved editor is told so rather than quietly wiring the last save.

- **A now-playing readout in the dashboard (`bgate_ui/static/nowplaying.js`).**
  Sound is the only asset class here with more than one player: the audio seat's
  library rows, cue rows and music candidates each render an `<audio>`, so do the
  asset library, peek sheet and scene builder, the lab plays through WebAudio and
  by design keeps playing when you navigate off its page, and the beat maker
  schedules its own preview. Browsers do not make `<audio>` exclusive, so two
  things play at once and nothing on the page NAMED the second one. It is a
  readout rather than a mute, deliberately: hearing a cue over a music bed is a
  real thing to want and the lab's mixer exists to play lanes together.
  Concurrency stays, and what is fixed is that it was invisible.

- **Seat lanes are re-rooted at what the repository actually contains.** Every
  glob in the default table was written against `<root>/game` and
  `<root>/design`, but `bgate init` scaffolds straight into `<root>` and an
  adopted repo has whatever layout its author chose. Run the real matcher against
  an ordinary Godot repo and `src/player.gd`, `assets/hero.png` and
  `scenes/level.tscn` are owned by no seat, so with the hook installed every
  dispatched agent is refused on contact with the source tree, and the refusal
  reads as "wrong seat" when the truth is "wrong layout". Adopt already computed
  the top level directories and threw them away. Layout detection now runs from
  both `adopt` and `init` and is stored as ordinary per project overrides,
  visible in `seat_list`, editable and reversible.

- **`bgate hook-uninstall` and `bgate un-adopt`,** the two ways back out that did
  not exist. The uninstall is surgical and leaves other tooling's hooks alone.

- **A QA probe that declares what it drives.** The headless probe hardcoded a 2D
  fighter in three places, so any other game got "no scene with both a Player and
  an Opponent node was found" and a bot that watched nothing. `bgate_core.qaprobe`
  now holds the contract, stored in `workspace_doc` under the qa seat so it needs
  no migration, with a copy riding along in every run's samples so an old
  baseline can still say what it was measuring. With nothing declared it derives
  from the scene the project actually has and says out loud what it settled for.

### Changed

- **BREAKING: `cinematic_generate_shot` now refuses on a multi-shot sequence
  with no current animatic.** Anything scripting against `generate_shot` has to
  either build a reel first with `cinematic_animatic` or pass `previs_ok=True`,
  or the call returns `ok: False` at `stage: "previs"` with nothing charged. It
  sits beside the encoder check and is there for the same reason: both are free
  to fix now and expensive later. Staleness is the half that matters, since a
  reel cut before the shots were re-ordered is worse than none, because it is why
  somebody believes the edit was checked. One-shot sequences are exempt, an
  unparseable timestamp does not block spending, and the override is a parameter
  rather than a config setting so that skipping previs is something somebody
  typed on the call.

- **The ffmpeg check round-trips the encoder instead of reading a list.**
  `ffmpeg_status` decided the encoder was usable with `"libtheora" in listed`,
  which is presence and not function, so `bgate doctor` was green while every
  cutscene shipped unreadable. It now encodes and decodes one second of synthetic
  video, cached per executable and only when the probe actually ran, so a single
  timeout cannot mark a build unusable for the life of an MCP server. It keeps
  three distinct answers: no ffmpeg, no libtheora, and libtheora that lies. This
  supersedes the presence check described in the doctor row at the end of this
  section.

- **Every paid MCP tool asks the budget.** `_spend_gate` was written for
  `image_sprites` and called only there, so `image_generate`, `image_edit`,
  `item_generate`, `item_variants`, `image_talkhead`, `vfx_animate` and
  `character_generate` all billed first and recorded afterwards, which makes the
  project budget an invoice on every path except the one it was demonstrated on.
  `item_variants` was the sharpest: its `limit` capped the count and never the
  money. `voice_speak` was worse, billed per character, recording its spend, and
  never once asking. `kie_video_generate` is gated too, and its unknown price
  case is handled honestly: kie reports an unpriced model as `None` rather than
  `0.0`, and folding that to zero would read as free for exactly the models whose
  cost is least predictable, so an unknown price still asks the budget and says
  so. A test walks the AST and fails if any paid tool ever loses its gate.

- **A spending workflow node cannot be started by a machine.** Every generate
  node and every registry-paid tool node is now a spending node. `run_one_node`
  never called `require_human` unlike its sibling routes, and `run_node` accepted
  an `actor` it ignored, so an agent could POST a paid video or music node
  directly; generate nodes were worse, since `advance` auto-started them whenever
  their inputs were satisfied, ignoring the dispatch switch entirely. Both now
  wait for a human press when dispatch is off, and the guards are mutation
  checked: disabling either fails ten tests.

- **Deploying a brainstorm closes the thinking partner process.** It used to
  leave it running, on the reasoning that a deployed session is one people keep
  talking in. That is true of the SESSION and was the wrong conclusion about the
  PROCESS: observed on this project's own director seat, a session deployed one
  day was still live the next, the seat page silently reopened it, and further
  requests had been stacking in it behind a transcript nobody scrolls back
  through, on top of a plan already on the board. Nothing is lost, since close is
  the verb whose whole contract is that the transcript, notes and drawing are
  rows in a table and stay put, and speaking in the room reopens it. A partner
  that will not shut down does not fail the deploy, because the items are already
  filed and re-running would file them twice.

- **The board commits its own work when a run ends.** `dispatch.auto_commit` is
  on by default and calls `gitwork.commit_paths` with the run's OWN paths, never
  `commit -a`, so a human's uncommitted work is never swept in and still
  correctly blocks the next dispatch.

- **The Python floor is 3.11.** `pyproject` refuses to install on 3.10, so
  doctor's green row there was a false pass. Doctor also gains a project report
  for the two faults no binary probe can see, lanes that match nothing in this
  repository and a hook that was never installed, and states which rows the core
  loop actually needs, because it exits 1 for the optional ones too.

- **A lane refusal now routes the work instead of only naming the wall.** The
  field evidence was unambiguous: fifteen files carrying LEFTOVERS blocks, seat
  notes five hours newer than the last item anyone filed, four paid assets
  orphaned because the note asking for them was never a job, and a 270-line
  integration script written to route around cross-lane one-liners. The hook now
  names the seat that owns the path and the `queue_add` call that hands the work
  over, suggests `depends_on` when the blocker is a work item rather than letting
  an agent poll a lease, and says plainly when NO seat owns a path that this is a
  configuration problem.

- **Sectioning across the dashboard.** The `.spanel` plus `.sec-h` pattern with
  an icon, a seat colour and a count already existed and was live in four views;
  every other view rendered a flat stack of divs, so everything blended together.
  Twelve views now use it, including the sprite editor, audio lab and 3D viewer,
  each of which had invented its own one-off heading class. Seat identity is
  painted on the header icon only, because inside a seat workspace the seat
  colour is constant down the whole column and carries no information while kind
  varies.

- **All fourteen user-facing documents rewritten,** to shorter and plainer prose:
  302 em dashes to zero, 25 self-aware asides to zero, 32,795 words to 27,828.
  The style pass was the cheap half. Verifying every claim against source turned
  up documentation that would have stopped or misled a reader following it:
  `reference.md` claimed 98 registered tools when there are 196, and said "Nine
  views" in one place and "Ten views" in another over a nine row table when there
  are twelve; `setup.md` described the PreToolUse hook as inert without a seat
  when `DEFAULT_DIRECTOR_MODE` is `collide`, so a seatless session holds the
  director seat and gets blocked on a collision, meaning somebody would have
  believed they were unchecked; `screenshots.md` told you to run `bgate serve
  --port 7801` when the capture script reads `BGATE_URL` at default 7788, so
  following the instructions captured nothing. Also fixed: doctor described as
  probing eight dependencies when it probes twelve, a README findings table that
  was a header with no rows, wheel instructions that failed on paste, links to
  two files that do not exist, and four tools documented with parameter names
  they do not take. One number was removed rather than corrected, a batch count
  attributed to `vfx.py` that exists in no file, branch or history. Three files
  got longer against the brief, because cutting verified facts to reach a word
  target is the same failure in the other direction.

- **The setup documentation covers the steps a first run actually fails on.**
  Python on PATH and what to do on Windows when `bgate` is "not recognized", how
  to get the absolute interpreter path the MCP registration step demands, and
  the fact that you must restart Claude Code before the tools appear, which was
  stated plainly in the `CLAUDE.md` written for an assistant and appeared in no
  user-facing file, so someone following the guide finished it, saw no tools and
  concluded the whole thing was broken. Contradictions resolved against the code:
  eight seats, one tool count, one Python version. The stamped agent briefing's
  cross-seat section told agents to write a LEFTOVERS comment block and never
  mentioned `queue_add`, so the product was teaching the dead-ending it was
  suffering from; it now opens with the queue call.

- `bgate doctor`'s ffmpeg row now says when the build has no libtheora. It stays
  green, since recording and frame extraction work fine without it, but such a build
  cannot write the one format Godot plays, and finding that out after a whole
  sequence has been generated is expensive. The cinematic seat refuses to spend
  anything until the encoder passes.

### Fixed

- **Sprite frames were registered on their bounding box, so a punch moved the
  fighter sideways.** An outstretched limb widens the box on one side, which
  slides the body the other way to compensate. Measured on a synthetic with an
  identical torso in both frames: the torso moved 44.5px under box-centring, 6.0px
  under the textbook alpha-weighted centroid, and 1.0px under the shipped anchor,
  the median of the *core* columns, which drops the limbs from the vote and
  leaves the torso, which is what an animator means by a centre line.

- **The set's fit scale used the widest bounding box, which quietly undid the
  registration.** Under anchor registration a pose needs twice its reach *from
  the anchor*, not its box width, so a wide pose could not sit where its anchor
  said and the placement clamp dragged the body off centre.

- **A sheet longer than the safe texture width was a texture that would not
  upload.** No warning: the sprite simply draws as nothing, and a thirty-frame
  character at 160px is already past the 4096px that mobile and web commonly cap
  at. Long sheets now wrap into a padded grid. Short ones are still a plain
  strip, so nothing existing re-imports.

- **The spend gate priced every run off the gpt-image table whichever provider
  was named**, which under-quoted every Krea run. A cap fed the wrong provider's
  prices is the failure the cap exists to rule out.

- **The art brief's rule 2 said "never condition frame N on frame N-1" while
  `image_sprites` had deliberately done so, on top of the anchor, since
  anchor+rolling landed.** It now says the true thing: the pin is in every call,
  which is what stops the decay, and the previous frame must never be the *only*
  reference.

- **Assembled cutscenes shipped silent while three documents said otherwise.**
  The module docstring, the seat brief and the research note all explained that
  generated audio is off because "the audio seat scores the cutscene over the
  top". There was no bed, no mix and no mux, a sentence that read as a design
  decision and was an unbuilt feature, which is worse than an admitted gap
  because nobody goes looking for it.

- **Every kept shot was transcoded into the game.** Nothing referenced them: the
  game loads the assembled cut, and assembly reads the `.mp4` candidates
  directly. It was a Theora encode per shot and, at 1080p, tens of megabytes each
  of unreferenced files. A cut installs; a shot does not, with an override for
  the one real case, a single clip used alone as an attract loop or a sting.

- **Captions could stack when a transition overlapped two shots.** A dissolve
  pulls the incoming shot back while the outgoing line still owns its own shot's
  full length, so two subtitles were on screen at once and the player showed the
  previous line over the new shot.

- **An empty storyboard was told to install ffmpeg.** `animatic.build()`
  resolved the encoder before it validated the board, so a board with every row
  cut refused with "ffmpeg is not on PATH" rather than "no live shots to cut".
  Both refusals were true at once and the order decided which one a human read.
  It picked the one about the machine over the one about their work, sending
  somebody to install software they did not need. Found by CI on a runner with
  no ffmpeg, which is exactly the machine that cannot tell the two apart.

- **The shipped cutscenes were not badly compressed, they were broken files.**
  Found by chasing one screenshot of a cutscene playing as coloured blocks in
  Godot: a shipped `.ogv` decoded 14 of its 193 frames and the rest threw
  `error in unpack_block_qpis`, with frame 3 extracting as a flat green
  rectangle. Ruled out one at a time: pixel-art content, the quality setting,
  encoder threading, frame size, and Godot's own decoder, which already carries
  the Theora fixes in 4.4. It is the ffmpeg BUILD, and not in the way the obvious
  summary suggests: 8.1.1-full gives 37 decode errors on a one second probe where
  7.1-essentials, also a gyan build, gives 0. It is a regression in one build
  line, so the rule is to prove the build rather than trust its version string.
  `transcode()` now decodes what it wrote, deletes it and raises rather than
  installing something unreadable.

- **A `first_frame` silently threw away the location plates on Seedance.** The
  model takes an anchor OR references and never both, and the code reported that
  the model "has no parameter for refs", which is false and blamed the model for
  a trade made on the caller's behalf. Now named, with what was lost and the
  actual choice.

- **The board deadlocked itself on the first file any agent wrote.** Nothing in
  this system ever committed anything, and dispatch refuses a dirty tree by
  default, so the first agent to write a file made the tree dirty and every later
  dispatch was refused, forever, on a 20 second retry. `dirty_tree` is a FLOOR
  refusal, so it stopped the whole board rather than one item. Measured: an
  overnight queue of thirty items finished three.

- **Timeout, stall, cost and expired-OAuth kills produced no event at all.**
  `_trip`'s ceiling kills wrote `failed` through `set_status`, which never emits,
  and `_reap` skipped `complete()` because the item was no longer dispatched, so
  none of those kills raised an `item.failed`: no bell, no webhook, no auto
  reopen. Separately, a runner failing instantly on every item, which is what an
  expired login looks like, burned a whole queue to `failed` in ten minutes,
  because a dispatch that SPAWNS counted as a success and never triggered a
  cooldown.

- **`queue_reopen` never incremented `attempts`,** which is the round counter the
  QA gate's cap reads, so the fail and reopen loop the cap exists to stop could
  never trip it: an unbounded money pump wearing the gate's own uniform. It
  routes through `queue.reopen` now and carries the harness's record of what the
  last attempt already wrote, so a fix round continues rather than regenerating.

- **A seat page silently dropped you into the middle of yesterday's brainstorm.**
  It read `bs-last-<seat>` from `localStorage` unconditionally, with no mark to
  say so. Auto-restore now covers what it was for, picking a thread back up in
  the same sitting, and anything older than four hours, deployed or archived
  lands on the list with the drawer open. The session is one click away, and the
  only thing that changed is that continuing it is a decision. The thread itself
  is windowed to twenty-four messages with a "show earlier" control that holds
  the reading position: a brainstorm that reached a plan is routinely a hundred
  turns and every one of them was re-rendered, with a full markdown re-parse of
  the whole session, each time one bubble arrived.

- **A node whose references could not be resolved was marked `passed`.**
  `_context_output` and `_ref_output` return their failures as `context_error`
  and `ref_error`, and `advance` only ever inspected `flow_error`, so the failed
  node carried straight through and the generate node downstream ran with an
  empty prompt and no style anchor, which is the exact bug `_ref_output`'s own
  docstring says it was written to fix. All three keys now fail the node.

- **A node left `running` by a crashed worker held its line forever.**
  `_INFLIGHT` is in-memory and dies with the process, while four user-facing
  messages told the reader to "reopen the run", a verb that did not exist.
  `reconcile()` releases them and the messages name it.

- **A stall that never resolved went quiet permanently.** The heartbeat's mark is
  the subject's own `updated_at`, so a chain that stalled and then genuinely
  never moved kept matching its mark and was never mentioned again, including
  across a three week absence, which is exactly when the stall is what the person
  coming back needs told. Marks now carry `said_at` and re-report after a full
  window.

- **An unanswered `ask_human` question past the 200 row scan was invisible** to
  `pending_decisions`, the console and the stale reminder simultaneously. The
  query already excludes answered questions, so the cap was only ever bounding
  how many open ones a project may have before the oldest silently vanish.

- **Krea and kie wrote no spend rows at all,** so a budget ceiling could never be
  reached on either provider. Both now account, and a failed music run carries
  `accounted: False` explicitly rather than returning no cost keys, which callers
  reading `credits_consumed` with an `or 0` default scored as free.

- **The reaper shot working agents on every restart.** `_live` is empty at
  startup, so every recorded pid looked orphaned. A live agent with recent log
  output is adopted now, not killed.

- **The level template pointed at a tileset no project has ever had.** The card
  hardcoded `res://assets/tiles/main.tres` and `wall_layout=blob47`, so the one
  template whose entire promise is that it generates a level died on its third
  node every time anyone opened it. The tileset is read from the project now, and
  the layout offer is computed by walking the coordinates a `.tres` actually
  defines, because a tile COUNT cannot answer it: 50 tiles in the wrong shape
  fails and 36 in the right shape passes. Source ids travel with `columns`,
  because the same sheet that fits at its own width runs off the edge at the
  argument default of 8, silently, drawing nothing exactly where the wall shape
  is hardest. With no tileset present the field is left empty rather than
  fabricated.

- **Godot 4.4 reported a failing build for every 3D project containing a glTF.**
  4.4 grew an editor thumbnail step that renders each imported 3D scene into a
  viewport and reads it back; under `--headless` the RenderingServer is the dummy
  stub, which owns no textures, so the readback prints `Parameter "t" is null.`,
  and `check_project` counts engine error lines rather than the exit code.
  Upstream godotengine/godot#108994. Classified benign only after measuring that
  it is content blind, fires only on the pass that reimports, and leaves a
  correct resource, and matched on the engine source frame as well as the
  message, so the same words from a real renderer stay fatal.

- **Streamer mode had two caches and the fixture cleared one.** `redact._cache`
  separately holds "is the filter on at all" for two seconds, so any earlier test
  leaving it `False` gave the streamer tests a fully unredacted response. It
  survived only because the suite happened to be slow enough between the two.

- **`nowplaying.js` was untracked while `index.html` already loaded it,** so a
  fresh clone served a 404 for a script the page loads before every audio player
  on it. Two packaging tests were red on exactly that.

- **Text to speech required the `websockets` package it never uses.**

- **The ffmpeg concat list was corrupt on Windows, silently.** Inside a quoted
  concat entry ffmpeg treats a backslash as an ESCAPE character, so a native path
  is read as escape sequences and a project at `C:\Users\nina\new-game` feeds the
  demuxer `\n`, making the entry a filename with a newline in it. Forward slashes
  now, which ffmpeg accepts on Windows. This is the difference between a cut that
  assembles and one that cannot open its own shots.

- **Six subprocess calls flashed a console window.** Doctor, gitwork, playtest,
  blender and godot all carry `creationflags=_NO_WINDOW`; the cutscene pipeline
  carried none and spawns more binaries than any of them, one per shot for
  continuity, one per join, one per mix, which under a stdio MCP server is a
  window per ffmpeg. A test walks the AST of both modules and asserts every
  `subprocess.run` passes `creationflags` and closes stdin.

- **Three tests died instead of skipping on a machine with no ffmpeg,** because
  `needs_theora` guarded the tests that encode while the helpers were used by
  tests that only need a file to exist. Verified by moving ffmpeg and ffprobe out
  of `/usr/bin` and running the whole suite: 4,317 pass with no ffmpeg at all.

- **Every board collapsed onto one sequence called "unnamed".** `slugify("")`
  returns the truthy string `"unnamed"`, so the empty check had to test the raw
  name.

- **Icons inside buttons stacked above their labels.** `BGIcon` renders
  `display:block`, so "Run", "generate" and "Lore" were two lines tall.

### Security

- **A real path traversal, found by attacking the containment check rather than
  trusting it.** Eleven shapes were tried against the check every caller-supplied
  path goes through; ten were refused and one was not. `normalize_path` resolved
  the path as given, verified it sat inside the project, and THEN rewrote
  backslashes to forward slashes on the way out. On Windows that is harmless,
  since both characters are separators and `resolve()` had already seen any
  traversal. On POSIX a backslash is an ordinary filename character, so
  `..\..\outside\secret.png` is one legal filename: it resolved to a harmless non
  existent child of the project, passed containment, and was handed back as
  `../../outside/secret.png`, which every caller then joins to the root and
  follows straight out of the project. Separators are normalised BEFORE the
  resolve now, so containment is verified against the same interpretation the
  caller gets back. This is shared code, so every registry key, artifact path and
  asset lookup in the product goes through it.

- **Six caller-supplied paths in the cinematic pipeline were joined to the root
  unchecked:** `style_refs`, `first_frame`, `last_frame`, `refs`, `vo` and
  `audio_track`. These paths are not merely read: a conditioning frame is handed
  to the provider's file upload API, which POSTs the bytes to a third party, so a
  path escaping the project root is exfiltration off the machine rather than
  local file disclosure. The callers are the dashboard body and MCP tool
  arguments, which means an agent, and constraining what an agent can reach is
  the entire premise of the seat and lane system. Reproduced before fixing. All
  six now go through `project_path()`, which reuses `assets.normalize_path`
  rather than reimplementing containment, refused at PLAN time because the shot
  list is what a human reviews, and checked again in `keyframes_for` and
  `assemble` because a row can be written by something other than `plan()`. It
  refuses by DESTINATION, not by shape, so `game/../art/hero.png` is still kept.

- **No exception-derived text reaches a response body.** `safe_error` is a
  constant now, not even the exception type name, since `type(exc).__name__` is
  an attribute read on a caught exception and would have been the third failed
  attempt rather than a fix. Two earlier attempts are on the record: scrubbing
  the message, which a taint tracker does not count as sanitising, and logging it
  instead, which traded a MEDIUM for a HIGH by writing a possibly secret bearing
  string to stdout. What this costs is bounded, and that bound is the only reason
  it is acceptable: `safe_error` is reached only from an `except` around an
  unexpected failure, while every deliberate refusal in the product raises
  `ApiError` with a message written as a literal in this repository's own source,
  and those are unchanged. Two tests pin that the deliberate refusals still
  explain themselves. The replacement message says detail was withheld and that
  the traceback is on the server, because a blank panel is the failure `api.py`
  exists to prevent.

## [0.1.35] - 2026-08-09

Twenty-two commits. The tool count went from 78 to 144, which is the shape of
this release: the dashboard grew the surfaces that were missing rather than
deepening the ones that already worked. Music generation, speech, the stream's
chat, a room to think in before dispatching, and one place to put every API key.

Underneath it, three things had been quietly broken for weeks and none of them
said so.

### Added

- **Music generation through Suno (kie.ai).** A request comes back with two
  takes, they land as candidates, a human auditions and keeps one, and only then
  does anything reach the game. The same candidate-then-keep path art uses,
  because a track is an asset. Progress is reported in words rather than a
  spinner, since "still working" and "stuck" look identical at three minutes.

- **Brainstorm.** A conversation with the director that ends in a plan. Nothing
  is queued until you press Deploy, and Deploy shows the plan and the agents it
  would dispatch before anything is filed. Director and narrative get a writing
  pad and a drawing pad beside the chat; the agents console gets the same
  conversation type without the pads, so you can talk it through where you
  already dispatch from.

- **Streamer chat, and feedback sessions.** Chat lands in the dashboard live. A
  feedback session is started and stopped by hand, and closing it hands the whole
  session to the director to synthesise into notes you can brainstorm from or
  dispatch against. Synthesis waits for the close on purpose: a running commentary
  is not a conclusion. No channel configuration lives in this repository.

- **Deepgram speech, both directions**, for the part of this that is thinking out
  loud.

- **A provider registry.** One table holds every API key: id, label, env var,
  what it powers, where to get one, and a probe. Keys can be set from the
  dashboard instead of by editing a file, and adding a provider no longer means
  touching the adapter, the doctor, the settings page and `.env.example`
  separately.

- **Local runtime and coding-agent setup panels.** They detect and describe what
  is installed rather than starting or stopping it, because a tool that silently
  launches and kills GPU processes fights whatever else was using that card.

- **A playtest notepad** that writes into the transcript itself, with the current
  frame attachable to a note.

- **Work history on the overview**, so finished work is somewhere after it leaves
  the board.

- **The sprite editor and audio lab are pages again**, with rail items of their
  own. Both are full-bleed direct-manipulation tools; reaching one used to cost a
  seat pick and then a mode pick.

- **An `orbit` theme**, glass over a true black ground, and 59 more icons so no
  toolbar is built from unicode glyphs.

### Fixed

- **The spend ledger had been discarding every 3D row since image-to-3D
  shipped.** `spend_event` carried a `CHECK` on `kind` written before `mesh`
  existed. The insert is wrapped in a "never lose the work" guard, so each row
  raised, was swallowed, and vanished with no error anywhere. Migration 0023
  widens it; verified against a copy of a real database first, 476 rows and
  $1,379.36 preserved.

- **Playtests recorded a black rectangle with a live cursor.** The capture used a
  window rect that did not account for DWM's extended frame bounds, so it recorded
  the wrong region of the desktop.

- **Atlas disagreed with Godot in three ways at once**, all invisible from inside
  the viewport because it was consistently wrong with itself. `cell_center`
  returned the diamond's top corner where `map_to_local` returns its centre; the
  draw rect had the texture-origin sign backwards, sinking art by `h-32`px, which
  is nothing for a floor tile and 68px for a wall; and cells drew in file order,
  which on an isometric map paints the wall behind over the wall in front.

- **An injection wrapped in `<instructions>` tags reached the director
  unflagged.** The chat filter matched the singular `<instruction>` only, and the
  plural is the spelling that gets used.

- **The chat digest cap never worked.** It was a default argument reading a module
  constant, so Python froze it at import and no later change could move it.

- **Cancelling a music generation in the first second crashed** instead of
  returning, because the submit-stage progress call sat outside the try that
  handles cancellation.

- **The diff endpoint spawned a process per file and never answered** on a large
  change: 90 seconds and a timeout became 3.7 seconds for 2,381 files.

- `bgate doctor`'s `art_key` row asks the provider registry now, so a working
  Krea-only setup stops reporting `MISS openai_key`. Four doc pages carried a
  warning about that and no longer need to.

### Changed

- Test fixtures no longer carry the author's real first name.
- Em dashes are out of the copy the dashboard actually renders (594 of them).
  Comments and docstrings keep theirs.
- Atlas drops its list and graph modes; it is two editors over one scan now.
- The queue board shows only active work, which is what makes the new history
  necessary rather than redundant.

## [0.1.34] - 2026-08-08

Twenty-two commits, and one sentence covers most of them: **a check that grades
its own homework is not a check.** The rig gates measured whether weights had
been WRITTEN rather than whether the elbow survives being bent; the art gates
reported the request instead of the artifact; the approval gate asked the human
for decisions they had switched off; and the harness advertised constraints an
agent could turn off from inside. Each was green, and each was green about the
wrong question.

### Added

- **`blender_flex` — the deformation gate.** `blender_rig` proves weights were
  written by counting the unweighted, which is silent about whether the joint
  works. This poses each joint one at a time and measures volume loss, per-bone
  cross-section (the candy-wrapper detector) and the INCREASE in
  self-intersecting faces, then renders every pose. It refuses an inert rig — its
  own first real run passed a model with no armature modifier: six green poses,
  nothing moving.

- **`blender_rig` audits before it binds.** Shell count and mirror distance are
  measured BEFORE the bind because both predict how it will go: a real generated
  mesh arrived as 940 disconnected shells, and bone heat cannot cross the gaps.
  Weights are then averaged across the body's own centre plane, but only when the
  audit says the two sides actually match.

- **`godot_retarget_check` — the engine's own verdict.** A `.glb` can carry 23
  correctly named bones in a FLAT hierarchy, pass every other check in this
  product, and be animatable by nothing except a clip authored for it alone. This
  rotates a shoulder, watches the hand, drives a profile-authored clip and writes
  the BoneMap.

- **Rig and animation quality metrics.** Weight-island/bleed detection,
  humanoid bone-name coverage as a Blender-side pre-check, reference-skeleton
  joint-deviation, silhouette-sweep scoring, and animation curve quality (arcs,
  easing, jitter, foot skate) including an anticipation/follow-through detector
  via LoG correlation. Research notes on what "taste" can and cannot be measured
  as are in `docs/visual-taste-research.md`.

- **Pairwise art tournament judging.** The literature on VLM-as-judge is
  unusually consistent that a pairwise "which is better" tracks human raters far
  better than an absolute 1–10 score, which judges get wrong even when the
  ranking they imply is right. So a match log is stored and a rating is DERIVED
  from it, rather than a score column being written. `shown_first` is recorded
  because position bias is a documented failure of this exact pattern, and
  keeping it is what lets a later audit check for it in real verdicts instead of
  assuming the finding transfers.

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

- **`pending_decisions`** — everything waiting on a human, in one call: the work
  items parked by the builder's gate (the hard block — the chain behind each does
  not advance), the artifact revisions nobody has dispositioned, and the open
  `ask_human` questions, alongside the active gate mode. There was no way to ask
  this. `asset_status` lists candidates but exposes no approval *state*,
  `art_tournament_standings` reports Elo from matches already decided, and human
  approval was dashboard-only — so a director could not see, surface or triage a
  queue of decisions blocking its own board. It cannot approve anything and is
  not meant to: the agent that made a candidate is exactly who must not clear it.
  A candidate a QA agent already passed is reported distinctly from a raw one,
  because the human is then confirming a check rather than performing the first.

- **The heartbeat carries pending decisions.** `.bgate/notify.jsonl` now takes
  `artifact.candidate` and `artifact.reviewed` lines beside the work-item ones,
  and the event bus takes matching kinds. It carried only work-item transitions,
  so five species candidates generated inside two minutes produced **zero lines**
  while the dashboard drew an approval card for each — a blocking gate with no
  signal, which looks exactly like an agent quietly working. Purely additive:
  every line still carries `{ts, item_id, status, seat, title}`, now with a
  `kind`, so a consumer reading `status` sees what it always saw.

- **`queue_add(depends_on=...)`** — order for an item filed onto a board that
  already has the work it waits on. Priority is a preference among things that
  are ALL ready; it does not stop auto-deploy from starting both agents in the
  same tick, so the one that needed the other's output writes against a file
  that does not exist and reports done. Order could previously only be expressed
  by `queue_add_chain`, which files a whole group at once — and chains are
  strictly linear and cannot be appended to, so a dependent FOLLOW-UP had no
  correct form at all. A dependency on an item that does not exist is refused,
  not dropped: an item silently waiting on nothing dispatches immediately.

- **`project_set_dimension`** — correct the 2d/3d record after the game changes
  shape. `init` wrote it and `adopt` detected it; nothing could change it, so a
  2D prototype that grew a 3D scene reported `dimension: "2d"` forever. Not
  cosmetic — the field steers scaffolding templates and the wording of seat
  briefs. The only workaround was re-running `project_init`, which overwrites
  name, pitch and engine to correct one field.

- **The seat brief carries the traps, gated by seat and dimension.** Two agents
  independently hit the `.tscn` Transform3D transpose in one night; once the list
  was pasted into briefs by hand, nobody did — and the hand-pasting only happened
  because somebody remembered. Every row is a bug that has already cost a run AND
  emits no error a search would find (transpose, winding-vs-normals,
  parse-error-looks-like-a-hang, `preload` not `class_name`, deferred `_ready`,
  sequential imports, sRGB-vs-linear, self-reported success, screenshot focus).
  A 2D project is not billed for the 3D rows. The brief also now states that the
  MCP tools are DEFERRED and need `ToolSearch` first — universal, non-obvious,
  and an agent that has not been told concludes the tools do not exist.

- **A long correction can reach a running agent.** `agent_steer` past 2000
  characters writes the full text to the project's steer box and hands the agent
  the opening paragraph plus that path, instead of refusing outright. The cap is
  right and truncation is still never on the table — half a sentence with no way
  to know it was cut is worse than either — but the refusal left no route for a
  long correction short of killing the run and paying for it twice.

- **A stopped run says it was stopped** (`work_item.stopped_by` / `stopped_at`,
  migration 0019, and `queue.stop`). Deliberately NOT a sixth status: `failed` is
  keyed on in ~85 places — `reopen`'s guard, the QA gate query, the chain
  interlock, the console's lanes — and a new status changes behaviour in every one
  that filters by name, silently and by omission. A stopped item stays failed
  because it did not finish and it IS worth reopening; what was missing was the
  CAUSE, which is a different question and now has its own column and an
  `item.stopped` event.

### Fixed

- **An agent could switch off the reviewer that judges it, and did, four times.**
  From a stress test of a real project — 19 items, 5 seats, one overnight run.
  `gate.mode` was found back at `none` with no human action; three items reached
  done with nothing verified, including a rigged character whose bind weights
  nobody checked. `budget.enforced` was found off, which turns every ceiling into
  a report, and `dispatch.max_concurrent` went from the 4 a human set to 9 and
  then 11. `human_only` is now declared per setting in the registry and enforced
  in `settings.set` — not per route, because the hole was that one write path did
  not know it was a policy boundary. Machine detection reads `BGATE_SEAT` and
  `BGATE_WORK_ITEM` as well as the actor prefix: the prefix is one stamp in one
  spawn path, and a gate a forgotten line disables is not a gate.

- **Ceilings terminated after the spend instead of limiting it.** Seven of nine
  items breached, worst case 3.3x — and the kill discarded the work: one item was
  killed at $7.17 having already written every file it was asked for, and a human
  had to inspect the filesystem to find out the run had succeeded. A wrap-up
  message now goes out at 80% telling the agent to stop starting things and
  report a partial result, and `queue.reopen` carries the harness's own
  observed-writes list into the next brief so a reopened item continues instead
  of paying twice.

- **The QA gate reviewed hand-closed items**, spawning and paying for a reviewer
  against a result note describing work already superseded. `work_item` records
  `closed_by` and `gate_skip`. QA coverage also stopped being a hardcoded tuple —
  `qa.gated_seats`, per project, instead of editing harness source and changing
  it for every project on the machine.

- **Two rig detectors were red on 100% of valid input, and five verdicts were
  green on input they had not looked at.** Both directions teach a reading agent
  to ignore the result. `weight_islands` built adjacency on the raw glTF edge
  graph, so it measured the vertex splits the exporter makes at UV and normal
  seams rather than weight bleed — 940 authored vertices come back as 1559 and
  every bone's weight set duly fell apart along them; `RightUpperLeg` read 14
  islands where the welded graph shows 2. And `template_deviation` compared bone
  HEAD POSITIONS, which are stance-dependent, against a T-posed template while
  `rig()` defaults to an A-pose: it reported both hands 0.154 body-heights out
  against a 0.08 threshold, perfectly mirrored, every non-arm bone at exactly
  0.0 — the arm swing, not a fit fault. Lengths are compared now, and islands are
  judged against the number of shells a bone touches rather than against 1,
  because this pipeline joins characters out of primitives and a hip bone
  legitimately spans three.

- **`art.auto_approve` did not stop candidates queuing for review.** Gating
  `review()` alone was not enough — agents REGISTER artifacts and never call
  `review()`, so every registration landed as a candidate whatever the setting
  said. The switch is applied at registration.

- **The approval gate's `none` setting was not honoured, for approvals OR
  sign-offs, across every seat.** With the selector reading `NONE` — labelled *an
  agent's own word closes its item* — the console went on drawing SIGN-OFF cards
  over every finished item and APPROVAL cards over every generated candidate,
  each saying only a human could decide. Observed across art and director both,
  which is what ruled out "the art path forgot to check" and named it one defect
  at the gate layer: `routes/console.py::_gates` never read `gates.mode` at all.

  Setting a gate to `none` is a sentence — *do not stop to ask me* — and asking
  anyway has two silent failure modes: work stalls behind a card nobody knew to
  click, or the human learns to rubber-stamp, which spends the gate's credibility
  on the runs where it IS on. It is now off at the DECISION and not merely at the
  drawing: `artifacts.register` auto-approves under `none`, because suppressing
  the card while leaving the revision a candidate would leave the live path
  holding the previous image with the one surface that said so now hidden.
  Untouched for `agent`, where an agent's verdict still leaves a candidate
  pending — an agent approving art is the drift the art-QA router exists to stop.

- **The builder's gate drew no card for the items it actually blocks.** The
  inverse of the above, found while fixing it. `queue.complete` parks a completion
  in `review` under that mode and the sign-off query asked for `done`, so the one
  mode that genuinely mandates a human decision was the one whose stopped chains
  were invisible. Parked items are now listed, marked `parked`, never aged out of
  the window (a stopped chain is not a claim worth a glance), and `accept` routes
  to `queue.approve` — acking one into a workspace doc would have cleared the card
  and left the chain stopped forever.

- **`image_generate(tileable=True)` silently did nothing.** `Image.save`
  dispatches on the destination's suffix, so an output named `litter_albedo`
  rather than `litter_albedo.png` raised `ValueError: unknown file extension:`
  inside a swallowing `try` — the mirror pass never ran and the caller got a
  texture back reporting success. The evidence arrived days later as a visible
  seam at 2.4m tiling across a full-screen terrain floor. The format is now taken
  from the source image, and the metadata records the MEASURED outcome
  (`tileable`) beside the request (`tileable_requested`), with a `warning` on the
  result when they disagree. A flag computed from the request is not evidence.

- **The alpha keyer reported `clean: true` over an opaque pink slab.** Krea
  ignores the pure-magenta backdrop contract and paints a DESATURATED magenta —
  measured ~`#c0559f`, which is 143 from `(255,0,255)` and so outside the tol=125
  sphere. The frame border keyed (closest to the contract colour) and the
  interior did not. Every one of the audit's five measurements inspects THE CUT —
  border, soft edge, RGB under zero alpha, enclosed holes — and not one inspects
  what the cut left behind, so a solid rectangle passed all five.

  Two fixes, because either alone leaves the hole. `key` gains a second pass on
  PINKNESS — `(R+B)/2 - G`, a scalar the desaturated backdrop keeps and a green
  leaf, brown twig or buff plume does not — unioned with the distance key and
  gated on the chroma actually being a pink. And `audit(path, chroma=...)` now
  measures `residual_chroma`: is the key colour still sitting opaque in the
  frame. Reported as `None` rather than `0.0` when no chroma is passed, because
  "not checked" and "checked and clean" reading the same is how this shipped.

- **glTF `alphaMode: MASK` is flagged at import.** Godot 4.7 imports it as
  `DEPTH_PRE_PASS`, not `ALPHA_SCISSOR`, so surfaces meant to be opaque cutouts
  land in the sorted transparent pass. Nothing errors and one tree looks
  identical; a forest is a framerate cliff with no error to grep for. Reported
  rather than corrected — whether a MASK surface wants scissor (foliage) or the
  transparent pass (genuinely translucent) is not knowable from the file. The
  glTF JSON chunk is read directly, so warning costs no new dependency.

- **`godot_screenshot` says its window has no foreground focus**, on every
  result rather than only when something looks wrong. The capture window never
  takes the foreground on Windows, so `Input.mouse_mode` stays VISIBLE and
  anything gated on `MOUSE_MODE_CAPTURED` collapses in the shot while working
  for a human. A previous pass "fixed" that by re-asserting capture every frame
  with a comment blaming the game, masking the real finding for a whole pass —
  when a fix has to run every frame forever, it is a symptom, not a cure.

- **`blender_status` conflated "configured" with "running", and hosted with
  local.** `usable: ["hunyuan-local", "krea", "trellis-cpp"]` gave no hint that
  two of those need a server the user has to start and one is a hosted API
  needing only its key — and the summary probes nothing, on purpose, because a
  status call that blocks on a TCP timeout is one nobody makes. An agent tried
  the two local backends, got connection refused from both, reported image-to-3D
  unavailable, and the whole path was written off for a session; Krea was hosted,
  its key was set, and it had already produced every texture in that build. The
  `local`/`hosted` split the adapter always computed is now surfaced, alongside
  `checked` and a note naming a reachable hosted backend by name.

- **Nothing could be dispatched at all.** A CodeQL autofix landed a containment
  check in `runners.preflight` requiring the working directory to sit under
  `Path.cwd()` — the dashboard's own process directory. A board serves projects
  it does not live inside: `bgate serve` runs from the checkout, the game lives
  wherever the user keeps games, and git-isolated runs move cwd again to
  `.bgate/work/item-N`. Every dispatch refused with *"outside the allowed
  project root"*, which reads like policy rather than like the bug it was. The
  path is still normalised and an unusable directory is still refused; the
  containment rule is gone, and a test now asserts a project outside the
  server's directory dispatches, because nineteen existing tests already said
  so and the merge went in red anyway.

- **The redaction suite was doxing its author.** `tests/test_streamer.py` was
  written with a real account name, home directory, hostname and game-project
  path as its fixture, so a public repository carried all four and every red CI
  run printed them in full to a public log. Fictional now, along with the
  worked examples in `streamer.py`'s comments, the approver names in the queue
  tests and the absolute path in `docs/lessons-from-a-shipped-game.md`. Two new
  tests grep the tracked tree for *this machine's* home and account name, so
  the next one is caught where it is introduced — skipped on CI, where the
  runner's own identity is documented on purpose.

- **The redactor did nothing on a foreign path spelling.** `Redactor.__init__`
  canonicalised home and project roots with `Path.resolve()`, which is a
  question asked of the running filesystem rather than a string operation: on
  Linux, `Path(r"C:\Users\x").resolve()` is the process's cwd with a directory
  literally named `C:\Users\x` on the end. Home then matched nothing, paths
  fell through to the foreign-home rule half-substituted, the project
  placeholder never appeared, `restore()` could not put anything back — and
  `status()` reported the filter healthy throughout. Foreign absolute paths are
  now kept verbatim, which is as canonical as this machine can make them.

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

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.40...HEAD
[0.1.40]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.35...v0.1.40
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
