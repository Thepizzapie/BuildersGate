# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

### Changed
- **Codex workers: no reviewer, a real sandbox, and the hook.** `codex exec`
  seats no longer run with `--approve-for-me`. They run `workspace-write`
  with `approval_policy = never`, network off, and — on Windows —
  `windows.sandbox = "elevated"` passed explicitly, because
  `--ignore-user-config` had been dropping the user's sandbox backend and
  Codex was treating every command and patch as read-only-refused; the
  reviewer was approving ordinary commands out of a sandbox that was not
  there. The Builders Gate PreToolUse hook is now injected into every Codex
  seat with `-c hooks.PreToolUse` and `--dangerously-bypass-hook-trust`
  (the flag for automation that vets its own hooks), so containment, lanes,
  locks and the two gates below apply to Codex exactly as to Claude. The
  hook answers Codex with stdout JSON (`permissionDecision: deny`): measured
  on 0.154, a hook that exits 2 is logged and ignored. `apply_patch` is
  judged per file from the patch text (it arrives as `command`). Codex's
  MCP tools run under `default_tools_approval_mode = "approve"`, the one
  mode that answers under `never`. `dispatch.codex_auto_approve` now covers
  only the Codex Director console, and that console reads a command through
  the egress gate before accepting it: `pip install` from the project root
  goes back to the human.
- **Egress gate.** A seated agent may not run installers (pip, npm install,
  winget, cargo install, …), network clients (curl, wget, ssh,
  Invoke-WebRequest, …), `git push/clone/fetch/pull`, or another agent CLI.
  Refused by program name with a message that says what to do instead
  (`queue_complete(next_approach=…)`); logged to `.bgate/hook.log`. Read
  through `sudo`/`env`/`bash -c`/`eval`/`powershell -Command` and `-c`
  snippets. `dispatch.allow_egress` (machine, human-only, guarded) lifts it.
- **File text does not travel on a command line.** A seated agent that
  writes a file with `cat > f <<EOF`, `echo … > f`, `tee`, `Set-Content`,
  `Out-File` or `[IO.File]::Write*` is refused and sent to Write/Edit
  (Codex: apply_patch). Program output may still redirect (`godot … >
  run.log`). Observed 2026-09-16: a heredoc carrying download-and-run
  strings on a `bash -c` line was killed by Windows Defender as ClickFix;
  the shape is what any agent on any machine can reproduce, so the shape
  is what a seat is refused.
- **`godot_test_run` marker scan.** `failures=0` in a probe's summary and
  `PASS … (the fail state)` lines no longer count as FAIL markers; markers
  are counted per line, zero-count summaries are not failures.
- **Delivery preview frames the model where it is.** The photo stage aims
  at the model's measured box centre and puts the floor at its lowest
  point. With `normalize_origin` off (the default), a feet-at-zero rig was
  photographed floating with its head off the frame — every Meridian cast
  delivery — and then approved.
- **Artifacts with a failed required check are never auto-approved.**
  `art.auto_approve` / `gate.mode = none` waive the human gate, not the
  machine verdict; such a revision stays a candidate and the activity log
  says which check failed. The auto-approve note now names the setting that
  actually waived the gate.
- Frozen build: `BuildersGate.exe hook` hosts the PreToolUse hook, as `mcp`
  hosts the server.

### Added
- **Canon: which world is current, which files are retired.** A project
  now records the scene the game IS (`world`, defaulting to project.godot's
  main scene), named current files (`reporter`, `world_layout`, …) and a
  RETIRED list of paths/globs, each with its successor and reason
  (`bgate_core/board/canon.py`). It reaches agents three ways: printed at
  the top of every dispatched brief and in the SessionStart block; the
  `canon_status` / `canon_audit` tools (every seat) with `canon_set` /
  `canon_retire` / `canon_unretire` (refused to a seated worker — what is
  current is the director's and the human's call); and the PreToolUse hook,
  which refuses a seat's READ or write of a retired path and names the
  successor — reads too, because the way agents ended up extending a
  retired map was by reading it as the example. `canon_audit` reads the
  tree against the list: scenes/scripts still referencing retired paths,
  scene basenames living under two directories, and scenes/scripts nothing
  reaches from the world, the autoloads or a tool (through ext_resource,
  `res://` strings, directory prefixes a script builds paths from, and
  `class_name`). Observed on Meridian: forty items dispatched into a tree
  carrying the first street beside the island; 58 references to
  `main.tscn` in one agent's log, 88 to a retired module in another.

## [0.1.46] - 2026-09-12

### Added
- **Three engines. `project.engine` decides which tools register, which doctor rows are graded, where the scaffolder looks, and what every seat is told to verify with**
- **The web engine: vite + TypeScript templates, a dev server, a build with a payload budget, vitest, headless screenshots**
- **The Unity engine: adopted through the Hub's project, compiled in batchmode, tested through the Test Framework, photographed by the editor, instrumented by a self-booting MonoBehaviour**
- **`engine_status`, `engine_check`, `engine_screenshot`, `engine_scaffold`, `engine_templates`, `project_set_engine`: the engine-neutral spine**
- **Atlas builds 3D scenes: a three.js viewport with orbit, selection, a move/rotate/scale gizmo and staged writes**
- **The file's own meshes: ArrayMesh geometry decoded from the .tscn's base64 surfaces and served per mesh**
- **The first-run card offers Godot, Web and Unity; Unity as an adopt path; `bgate init --engine web`; `POST /api/project/adopt`**
- **The 3D character pipeline: quadruped poses, `godot_character_wire`, `godot_clip_capture`, `godot_clip_retarget`**
- **The 3D viewport renders on demand and can be stopped; the exe build checks and smokes its assets**

### Changed
- **Seat lanes, playtest, the Tests tab, the asset scan and the dashboard's engine card follow the project's engine**
- **The scene draw list is memoised on the text, the render on the files it reads: a 5,483-node scene opens in 0.1 s after the first 5**
- **Every seat carries the engine-neutral tools; web and Unity families go to the seats that were measured using their Godot equivalents**
- **Dependency floors raised above their advisories, and audited at the floor in CI**
- **Templates live under `templates/<engine>/<kind>`**

### Fixed
- **A running game could not report telemetry: the guard refused the proxied origin and the tokenless POST**
- **`engine_screenshot` spread an async tool wrapper's coroutine into a dict**
- **Node subprocesses decoded as cp1252 on Windows and handed back an empty stdout for a green vitest run**
- **The dev server's stdout was a pipe nobody drained; a recycled pid could get its tree killed**
- **The Unity Test Framework's `-runTests` was combined with `-quit`, so a green suite wrote no results**
- **The character probe's IK weight read 0.00 for every NPC; Blender's self-intersection baseline kept the last keyed pose**
- **Managed blocks stamped by an earlier release were doubled by re-adopting; Unity's Library tree was scanned as the game**
- **An instance's placement stacked on the packed root's transform; caches raced on the thread pool; `engine_status` lost the Godot project path**

### Security
- **Every optional path argument on the web and Unity tools is contained; the headless browser visits loopback URLs only; `dist` must resolve inside the project**

## [0.1.45] - 2026-09-06

### Added
- **The modelling kit builds the smooth shape FIRST: hull, skin, blob, tube, round, rock**
- **Surface tools: fuse, shade, lathe, loft, eleven material presets, bake, decals**
- **`blender_tree`, `blender_scatter` and `blender_look_audit`**
- **Live Blender: the kit inside a Blender that stays open**
- **`mesh_faceting`: the gap between the silhouette and the triangle count**
- **`blockout_generate`: a measured 3D graybox from a spec or a level_plan**
- **`godot_scene_audit` and `godot_export_verify`: the 3D gates every game hand-wrote and none shipped**
- **The 3D scaffold is third-person, with a prop kit and an arcade vehicle demo**
- **Chaos dispatch mode: every run in its own worktree, integration bounded**
- **Codex seats: forwarded MCP identity, isolated config, bounded auto-approval**

### Changed
- **A dispatched agent carries the tools it was measured using, not 203**
- **Autodeploy refuses to spawn an item whose brief names a leased file**
- **The tool schema bills the contract, not the essay**
- **In a 3D project the cast is GENERATED; every visible mesh routes to art**
- **The Builders Gate checkout itself is refused as a project root**
- **The test suite lost 144 prose-pinning tests and can no longer launch real agents**

### Fixed
- **`blender_animate` runs on Blender 5.x**
- **The director console died at spawn on Windows with a >8 KB system prompt**
- **Playtest told every adopted project it had no game**
- **Dashboard and MCP project creation re-root the seat lanes to the scaffold's layout**
- **SessionStart read the raw autopilot doc; project_create left the active pointer behind**
- **Audio Lab: mp3 probing, an edited mp3 saves as a sibling ogg, `encode_ogg` no longer raises**
- **The rig anatomy verdict fails a trunk it could not measure**
- **Agent commits and chaos merges carry a fallback author when the repo has none**
- **Two tools were registered as `blender_sweep`; the tube is `blender_tube`**
- **`bgate_track_gen.gd` preloaded a path that only exists after `track_generate`**

### Security
- **Audio Lab paths are contained in the shape CodeQL recognises; `project_create` keeps exception text out of the reply**

Full narrative: [docs/decisions/0.1.45.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.45.md)

## [0.1.44] - 2026-09-04

### Removed
- **The spend ledger and every budget ceiling**

### Added
- **The director console runs on Codex as well as Claude Code**
- **An opt-in, credential-free Claude usage bridge**
- **Audio Lab separates a clip into stems locally**
- **`art.mesh_route` settles how the art seat makes NEW geometry**
- **`track_generate` builds a measured, drivable circuit from a JSON spec**
- **`ui_concept` paints the game's screens and derives a palette and a Godot Theme**
- **`sfx_prompt` generates real SFX through kie sounds**
- **`godot_export_probe` runs a script against the EXPORTED pck**
- **`bgate connect`, wiring your coding agent is a command, not a paragraph to retype**
- …and 1 more, in the decisions file.

### Changed
- **A spawned agent files at most two non-duplicate items**
- **The root config files carry rules, not essays**
- **The README is 232 lines instead of 308**
- **The repository root is a table of contents again**
- **`bgate_engine/` is `docs/engine/`, and stops shipping**
- **The dashboard package and the suite got the same treatment**
- **`bgate_core` is ten subpackages, not 117 files in a heap**
- **`tools/` is gone; `panel_api.py` is a script and lives in `scripts/`**
- **One sandbox path for the floor-art scripts**

### Security
- **Every path derived from an Audio Lab stem request is re-checked**
- **`bgate publish` put the author's own directory on the public web**
- **`0.0.0.0` is no longer an accepted `Host`**
- **`release-exe.yml` narrows `contents: write` to the one job that needs it**
- **`.claude/launch.json.example` had a literal tab in its `BGATE_ROOT` placeholder**

### Fixed
- **Three MCP tools were joining the shared spine by accident**
- **The model card on the workflow canvas gets its per-node run button back**
- **The copyable registration line escapes its JSON, so VS Code's pastes whole**
- **`bgate doctor` no longer conflates "nothing is wired" with "no runner installed"**

Full narrative: [docs/decisions/0.1.44.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.44.md)

## [0.1.43] - 2026-09-01

### Added
- **The rig and animation gates now measure the thing they are named after**
- **Every important gate now terminates at the actual player-facing runtime**

### Changed
- **`godot_run` is autoload-safe**
- **Evidence tools no longer dirty the tree**
- **`godot_test_run` returns concise output by default and separates two signals**
- **`godot_deliver_asset` handles non-humanoids**
- **`ask_human` names its recipient**
- **`queue_update` on a running item says whether it reached anybody**
- **Steers are recorded in the item's own history**
- **Retry exhaustion is a state, not two counters to add up by hand**
- …and 23 more, in the decisions file.

Full narrative: [docs/decisions/0.1.43.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.43.md)

## [0.1.42]

The director you talk to is a real session, a failed item no longer stops everything behind it, and the installer asks what you actually want.

### Added
- **The console's director is a persistent Claude Code session**
- **A preferred provider and model, honoured everywhere**
- **Optional features are modules, chosen when you install**
- **A dispatched seat gets its own craft's tools, not all of them**

### Changed
- **A failure is retried and then acted on**
- **Agents stay on the item they were given**
- **A 2D project starts without the 3D pipeline**

### Fixed
- **Five ways past the write gate**
- **Tools that took a raw path could reach another game**
- **Dispatch races that cost real runs**
- …and 3 more, in the decisions file.

Full narrative: [docs/decisions/0.1.42.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.42.md)

## [0.1.412]

The app opened a black window if you had ever renamed a project.

### Fixed
- **A folder registered under two names blanked the whole interface**

Full narrative: [docs/decisions/0.1.412.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.412.md)

## [0.1.411]

The installer, which 0.1.41 built and then forgot to publish.

### Fixed
- **`BuildersGate-setup.exe` is on the release page**

Full narrative: [docs/decisions/0.1.411.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.411.md)

## [0.1.41]

Everything in this release is one thing: the app you download now does what the app you ran from a source checkout always did.

### Fixed
- **The app could not start from a shortcut**
- **The app's own window code had never run**
- **Playtest recording could not work**
- **Speech to text was left out for a reason that was not true**
- **A missing transcriber disabled the Record button**
- **Menus and drawers rendered behind the page**
- **Clicking a project in the switcher did nothing**
- **"Open Orchestration" went nowhere**
- **The test suite wrote into your real project list**

### Added
- **An installer**
- …and 7 more, in the decisions file.

Full narrative: [docs/decisions/0.1.41.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.41.md)

## [0.1.40] - 2026-08-12

Forty-nine commits.

### Added
- **Machine-wide API keys (`~/.bgate/.env`), and `bgate key` to manage them**
- **A scratch project at `~/.bgate/scratch`, for generations that belong to no game**
- **The anchor is a model sheet now (`anchor_views`, default 3)**
- **Character work on Krea is pinned to `nano-banana-2`**
- **`sprite_plan`, and archetypes for `image_sprites`**
- **Per-frame timing in the emitted resource**
- **Ping-pong cycles**
- **Palette locking (`palette_lock`, default `"auto"`)**
- **A motion report on every assembled sheet**
- **A 3D model viewer and editor**
- …and 68 more, in the decisions file.

Full narrative: [docs/decisions/0.1.40.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.40.md)

## [0.1.35] - 2026-08-09

Twenty-two commits.

### Added
- **Music generation through Suno (kie.ai)**
- **Brainstorm**
- **Streamer chat, and feedback sessions**
- **Deepgram speech, both directions**
- **A provider registry**
- **Local runtime and coding-agent setup panels**
- **A playtest notepad**
- **Work history on the overview**
- **The sprite editor and audio lab are pages again**
- **An `orbit` theme**
- …and 12 more, in the decisions file.

Full narrative: [docs/decisions/0.1.35.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.35.md)

## [0.1.34] - 2026-08-08

Twenty-two commits, and one sentence covers most of them: **a check that grades its own homework is not a check.** The rig gates measured whether…

### Added
- **`blender_flex`, the deformation gate**
- **`blender_rig` audits before it binds**
- **`godot_retarget_check`, the engine's own verdict**
- **Rig and animation quality metrics**
- **Pairwise art tournament judging**
- **Nine scene tools on the MCP surface**
- **The scene builder plays the build it just wrote**
- **`assets.lock_holder()`**
- **`pending_decisions`**
- **The heartbeat carries pending decisions**
- …and 22 more, in the decisions file.

Full narrative: [docs/decisions/0.1.34.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.34.md)

## [0.1.33] - 2026-08-02

### Added
- **Atlas grew a fourth mode: `code · edit it`**
- **`POST /api/godot/file`**
- **`GET /api/scene/files`**
- **A `real` backdrop in the scene viewport**

### Changed
- **Streamer mode costs roughly nothing now**
- **`/api/screenmap` is cached and no longer walks the engine's import cache**
- **`/api/preview` takes `item_id`**

### Fixed
- **Three panels ignored `hidden`**
- **Long dropdowns closed the instant they opened**
- **`app.css` is cache-busted like the JS**
- …and 8 more, in the decisions file.

Full narrative: [docs/decisions/0.1.33.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.33.md)

## [0.1.32] - 2026-08-01

One call turns "a model that looks like X" into a rigged character in the engine, and `force` stops meaning "overwrite your project".

### Added
- **`character_generate`, the whole pipeline as one tool**

### Fixed
- **`force` meant "overwrite your project", and said nothing about it**

Full narrative: [docs/decisions/0.1.32.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.32.md)

## [0.1.31] - 2026-08-01

A generated mesh becomes a rigged character an engine can move, and the skeleton it binds to is the same one every time.

### Added
- **`blender_rig`, the missing step between geometry and a character**
- **A shipped humanoid template**

### Fixed
- **The shoulder joint was 20 cm outside the body**
- **`bg_flipped` answered `0` both when it found nothing and when it broke**
- **A collapse could meet its budget and destroy the asset**
- **A 404 meant "server missing" when it means "server answering"**
- **EEVEE answers to two names**

### Added, the paths a session could not reach
- **Krea 3D shipped unreachable**
- **Which knobs a backend takes is now discoverable**
- **ComfyUI claimed to be available with no workflow**
- …and 9 more, in the decisions file.

Full narrative: [docs/decisions/0.1.31.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.31.md)

## [0.1.30] - 2026-07-31

The 3D path stops being a blockout generator.

### Added, 3D
- **Image-to-3D on the user's own GPU**
- **Krea 3D**
- **`bg_adopt`, a generation is not an asset**
- **Orientation is refused rather than guessed**
- **`doctor` gains an `imageto3d` row**
- **`spend` gains a `mesh` kind**

### Added, CI
- **A CI pipeline that gates on more than pytest**
- **`packaging/smoke_wheel.py`**
- **A release guard**
- **`.github/workflows/security.yml`**
- …and 2 more, in the decisions file.

Full narrative: [docs/decisions/0.1.30.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.30.md)

## [0.1.29] - 2026-07-30

### Fixed
- **Every quality gate in the 3D path was a false negative**
- **The importer's `Icosphere` was the whole story on weighting**
- **The turnaround could not detect the failure it was written for**
- **`blender_sweep` could delete outside the project root**
- **The tool told to make the logo was forbidden from drawing text**
- **Decals shipped opaque**

### Added
- **A proportioned rigged base, so the agent stops inventing a body**
- **`godot_deliver_asset`**
- **`blender_layer_rerun`**
- **3D is visible to QA**
- …and 7 more, in the decisions file.

Full narrative: [docs/decisions/0.1.29.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.29.md)

## [0.1.28] - 2026-07-30

Nothing.

Full narrative: [docs/decisions/0.1.28.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.28.md)

## [0.1.27] - 2026-07-30

### Added
- **An agent finishing now reaches somebody**
- **A transition log something finally reads**
- **Work chains: dependent items filed as one ordered group**
- **Three approval gates, because the question is not "strict or loose", it is WHO IS AWAY**
- **A heartbeat, for the failures that are an ABSENCE of transitions**
- **`ask_human`**
- **Notifications**
- **One settings surface**
- **The agent rails open what they name**

### Changed
- **The QA gate's loop moved into the follow-up router**
- …and 10 more, in the decisions file.

Full narrative: [docs/decisions/0.1.27.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.27.md)

## [0.1.26] - 2026-07-29

### Added
- **The MCP server now ships `instructions`, so a session cannot be lobotomized by changing directory**
- **A `SessionStart` hook that preloads the board**
- **A project thread for in-flight state**
- **`bgate hook-install --scope user`**
- **The VFX pipeline**

### Fixed
- **An agent's reported file list is now the one the harness observed**
- **Every seat was instructed to write a file its own lane forbade**
- **The PreToolUse hook ignored the widest-reach agent in the system**
- `bgate hook-status` no longer reports a seatless session as inert when it is not, and the scaffolded `CLAUDE.md` no longer claims `queue_next` "marks…

### Known issues
- `vfx_animate` joins model-supplied `name` / `out_dir` onto the output directory with no containment, so a traversal writes outside the project root.
- …and 4 more, in the decisions file.

Full narrative: [docs/decisions/0.1.26.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.26.md)

## [0.1.25] - 2026-07-28

### Fixed
- **Every wheel and exe shipped without the Godot Web export preset**

Full narrative: [docs/decisions/0.1.25.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.25.md)

## [0.1.24] - 2026-07-28

### Fixed
- **A clean install pulled MCP SDK 2.0 and every MCP tool stopped importing**

Full narrative: [docs/decisions/0.1.24.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.24.md)

## [0.1.23] - 2026-07-28

### Added
- **The Agents view is a console, not a board**
- **A message to the director is a work item with its own log**
- **A staging queue between the conversation and the graph**
- **Auto-deploy**
- **Phases, the pockets of work inside a running agent**
- **You can see what the agent sees**
- **Sign-off gates**
- **Cross-agent work is drawn**
- **The director can steer its own workers**
- **Console sessions**
- …and 19 more, in the decisions file.

Full narrative: [docs/decisions/0.1.23.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.23.md)

## [0.1.22] - 2026-07-28

### Fixed
- **The first-run screen offered to create a project in `C:\Windows\system32`**
- A `PermissionError` from the create endpoint now answers 400 with the directory and a suggestion instead of leaking `[WinError 5] Access is denied`.

Full narrative: [docs/decisions/0.1.22.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.22.md)

## [0.1.21] - 2026-07-28

### Fixed
- **The desktop app had no icon**
- **The logo was a redrawing of itself in four places**
- **The rail brand painted the whole mark one colour**

### Added
- **"Run anyway" on the uncommitted-changes refusal**

Full narrative: [docs/decisions/0.1.21.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.21.md)

## [0.1.2] - 2026-07-28

### Fixed
- The standalone Windows build ships as a folder rather than a self-extracting executable.
- It is still **not code signed**, so Smart App Control will refuse to launch it ("we can't confirm who published BuildersGate.exe").

Full narrative: [docs/decisions/0.1.2.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.2.md)

## [0.1.1] - 2026-07-28

A UI/UX pass over the whole dashboard, and the first downloadable build.

### Added
- **Light and dark grounds**
- **Desktop app**
- **A standalone Windows build**
- **Sprite editor**
- **Audio lab**
- **World bible**
- **Scene composition convention**

### Changed
- The dashboard's CSS is one stylesheet (`bgate_ui/static/app.css`) with a declared cascade order, replacing six `<style>` blocks that had accumulated…
- Every `<select>` is a searchable in-app combobox.
- Every `window.prompt` / `window.confirm` is an in-app dialog.
- …and 11 more, in the decisions file.

Full narrative: [docs/decisions/0.1.1.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.1.md)

## [0.1.0] - 2026-07-27

First public release.

### Added
- **MCP server**
- **Seven agent seats**
- **Godot adapter**
- **Blender adapter**
- **Two image providers**
- **Dashboard**
- **Playtest mode**
- **`bgate publish`**
- **`bgate doctor`**
- MIT licence, `.env.example`, and this changelog.
- …and 6 more, in the decisions file.

Full narrative: [docs/decisions/0.1.0.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.0.md)

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.43...HEAD
[0.1.43]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.42...v0.1.43
[0.1.42]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.412...v0.1.42
[0.1.412]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.411...v0.1.412
[0.1.411]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.41...v0.1.411
[0.1.41]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.40...v0.1.41
[0.1.40]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.35...v0.1.40
[0.1.35]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.34...v0.1.35
[0.1.34]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.33...v0.1.34
[0.1.33]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.32...v0.1.33
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
