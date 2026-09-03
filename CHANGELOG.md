# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

### Added
- **`bgate connect` — connecting your coding agent is a command now, not a
  paragraph.** Setup told a new user to type

      claude mcp add builders-gate --scope user -- <ABSOLUTE-python-path> -m bgate_mcp.server

  and then explained, correctly, that getting that one argument wrong produces
  a registration which looks fine, fails at the first tool call, and reports
  "failed to connect" pointing nowhere near the cause — the most common setup
  failure on the supported platform. Asking someone to hand-assemble the
  argument the docs admit is the trap was the papercut. `bgate connect` fills
  it in, because the interpreter it should name is the one the command is
  running on.

  With no argument it writes nothing and reports every client: installed or
  not, registered or not, and — the state nothing else could see — whether that
  registration names an interpreter that can actually import the server. Every
  write is verified afterwards by asking that interpreter to import it, because
  "registered" is precisely the claim that has been wrong before.

- **Six more clients, and an answer for the ones not on the list**
  (`bgate_ui/agentcli.py`). Wiring was Claude Code and Codex, and it was
  coupled to the dispatch table: a client had to be something the board could
  run work on before it could be something the human's own sessions could call
  the tools from. Those are different capabilities and the second is why people
  install this. `WIRINGS` no longer requires a `RUNNERS` entry, so Gemini CLI,
  VS Code (Copilot), Cursor, Windsurf and opencode all get rows — each marked
  "wiring only" where the board cannot dispatch to it, which is a true sentence
  rather than an absence.

  Where a client ships its own `mcp add`, that subcommand still performs the
  write. Cursor, Windsurf and opencode do not, and keep their servers in a JSON
  file the user also hand-edits: those are **read** — a bare `python` is the
  same bug in `~/.cursor/mcp.json` as in `~/.claude.json`, and nothing could see
  it there before — and rendered as a paste-in block with the interpreter
  already correct. Nothing merges into a config this tool does not own.
  `bgate connect --show` also prints a generic block for any MCP client that is
  not on the list at all.

### Changed
- **The root config files carry rules, not essays.** `.gitignore`, `pyproject.toml`,
  `.gitattributes` and `.env.example` had grown 285 lines of commentary between
  them — history that belongs in history. They are 159 lines of declarations now,
  with the same behaviour: every ignore rule, negation and package-data pattern is
  unchanged, and the packaging tests that read pyproject still pass. `.gitattributes`
  also still pointed at the pre-`src/` bundle path.
- **The README is 232 lines instead of 308.** The screen-by-screen tour is one
  paragraph and a link to `docs/screenshots.md`; the rest is trimmed rather than
  cut, and the `CONTRIBUTING.md`/`SECURITY.md` links follow those files into
  `.github/`.
- **The repository root is a table of contents again.** Six importable
  packages sat at the top level beside the docs, the CI config, the front end
  and the build scripts, so the first thing a reader saw was a list that did
  not say where the product was. They are under `src/` now — with
  `templates/`, which has to stay their sibling because `scaffold.py` resolves
  it relative to its own file and site-packages is the layout that has to
  match. `CONTRIBUTING.md` and `SECURITY.md` moved to `.github/`, where GitHub
  reads them from just the same.
- **`bgate_engine/` is `docs/engine/`, and stops shipping.** It is a design
  proposal — a README, a DESIGN.md and seven JSON schemas, no runtime code and
  no importer — and it was being installed into every wheel and bundled into
  every .exe. A document belongs in `docs/`; the wheel is for the product.
- **The dashboard package and the suite got the same treatment.** `bgate_ui`
  had 22 loose modules beside `routes/`; the ones that belong together are now
  `agents/` (dispatch, runners, follow-up, the QA gate, the two CLI sessions),
  `pumps/` (the shared loop shape and the three loops using it) and `window/`
  (`bgate app`). `tests/` was 226 files in one directory and is now grouped to
  mirror the packages it tests — `store/`, `board/`, `art/`, `ui/`,
  `adapters/`, `mcp/`, `packaging/` and the rest. Same 6557 tests collected
  before and after.
- **`bgate_core` is ten subpackages, not 117 files in a heap.** The domain
  package had every module at its top level, so the tree said nothing about
  what belonged with what and a new module's only home was the same flat pile:
  `store/` (db, project, settings, assets, events), `board/` (seats, queue,
  workflows, spend, gates), `design/` (bible, lore, canon, greenlight),
  `runtime/` (providers, doctor, external binaries), `art/`, `three_d/`,
  `audio/`, `cine/`, `level/`, `qa/`. Every import site moved with them and
  nothing re-exports the old paths — one name per module, not two.
- `tools/` held a single file. `panel_api.py` is a script, so it lives in
  `scripts/` with the others and the Pulsiron routines call it there.
- **One sandbox path for the floor-art scripts.** `_sandbox()` was copied
  byte-for-byte into four of them under the comment explaining why the
  hardcoded Desktop path it replaced was wrong — while `gen_floor_layers.py`
  and `gen_floor_rooms.py` still carried that very `Path.home() / "Desktop" /
  "bg-testbed"`. Setting `BGATE_CAST_PROJECT` therefore moved four scripts and
  silently did nothing for the other two. All six now read
  `scripts/_floorpaths.sandbox()`.

### Security
- **`bgate publish` put the author's own directory on the public web.** Every
  entry in the generated `games.json` carried `root` — the absolute path to
  that game on the machine that ran the command, which on Windows names the
  user's home directory. The report keeps it (it is printed locally and the
  CLI needs it); the published index no longer does, and a test asserts the
  project path appears in neither `games.json` nor `index.html`.
- **`0.0.0.0` is no longer an accepted `Host`.** The dashboard's rebinding gate
  allowed it, and it is the one spelling of loopback that a browser will route
  to a 127.0.0.1 listener while treating the origin as foreign — walking around
  the private-network protections `localhost` gets. Refused like any other
  non-loopback name, with a test.
- `release-exe.yml` asked for `contents: write` across the whole workflow, which
  also runs on pull requests. The permission is now on the one job that attaches
  release artefacts, and that job says what would close the remaining gap.
- `.claude/launch.json.example` shipped a backslash-escape typo, so its
  placeholder `BGATE_ROOT` contained a literal tab character.

### Fixed
- The copyable registration line escaped nothing, so VS Code's — which carries
  a JSON object as one argument — ended at its first inner quote when pasted.
- `bgate doctor`'s agent row read "no coding-agent CLI found on PATH — the board
  can file work but nothing can be dispatched" for a machine that could be
  perfectly well wired and merely have no runner. Wiring and dispatch are now
  two sentences, because a green lamp on the first does not settle the second.

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
- **`blender_flex` — the deformation gate**
- **`blender_rig` audits before it binds**
- **`godot_retarget_check` — the engine's own verdict**
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
- **`character_generate` — the whole pipeline as one tool**

### Fixed
- **`force` meant "overwrite your project", and said nothing about it**

Full narrative: [docs/decisions/0.1.32.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.32.md)

## [0.1.31] - 2026-08-01

A generated mesh becomes a rigged character an engine can move, and the skeleton it binds to is the same one every time.

### Added
- **`blender_rig` — the missing step between geometry and a character**
- **A shipped humanoid template**

### Fixed
- **The shoulder joint was 20 cm outside the body**
- **`bg_flipped` answered `0` both when it found nothing and when it broke**
- **A collapse could meet its budget and destroy the asset**
- **A 404 meant "server missing" when it means "server answering"**
- **EEVEE answers to two names**

### Added — the paths a session could not reach
- **Krea 3D shipped unreachable**
- **Which knobs a backend takes is now discoverable**
- **ComfyUI claimed to be available with no workflow**
- …and 9 more, in the decisions file.

Full narrative: [docs/decisions/0.1.31.md](https://github.com/Thepizzapie/BuildersGate/blob/main/docs/decisions/0.1.31.md)

## [0.1.30] - 2026-07-31

The 3D path stops being a blockout generator.

### Added — 3D
- **Image-to-3D on the user's own GPU**
- **Krea 3D**
- **`bg_adopt` — a generation is not an asset**
- **Orientation is refused rather than guessed**
- **`doctor` gains an `imageto3d` row**
- **`spend` gains a `mesh` kind**

### Added — CI
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
- **Phases — the pockets of work inside a running agent**
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
