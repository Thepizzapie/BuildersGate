# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

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

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Thepizzapie/BuildersGate/releases/tag/v0.1.0
