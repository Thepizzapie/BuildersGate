# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been released to a package index yet; `0.1.0` is the state of the
repository at first publication. There is no earlier release history to record.

## [Unreleased]

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

[Unreleased]: https://github.com/Thepizzapie/BuildersGate/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Thepizzapie/BuildersGate/releases/tag/v0.1.0
