# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

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
- **A clean install pulled MCP SDK 2.0 and every MCP tool stopped importing.**
  The SDK's 2.0 removed `mcp.server.fastmcp`, which the whole of
  `bgate_mcp/server.py` is built on, and the dependency was a floor with no
  ceiling — so nothing changed but the date and the server broke on any machine
  that had not already resolved it. Pinned to `mcp>=1.2.0,<2`; lifting that is a
  port, not a version bump.
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

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.23...HEAD
[0.1.23]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.22...v0.1.23
[0.1.22]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.21...v0.1.22
[0.1.21]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.2...v0.1.21
[0.1.2]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Thepizzapie/BuildersGate/releases/tag/v0.1.0
