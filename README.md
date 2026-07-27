# Builders Gate

[![CI](https://github.com/Thepizzapie/BuildersGate/actions/workflows/ci.yml/badge.svg)](https://github.com/Thepizzapie/BuildersGate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform: Windows primary](https://img.shields.io/badge/platform-Windows%20primary-lightgrey)](#platform-support)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A game development pipeline that a fleet of AI agents can actually operate.**

You install it, run `bgate init`, and you have a runnable Godot game plus a local
dashboard. From there, agents drive the work through ~80 MCP tools: a director
writes the design bible and draws a cut line; a narrative seat adds lore that a
deterministic canon check gates; an art seat generates sprites against pinned
references and locks the binaries while it edits them; a gameplay seat writes
GDScript, runs the game headless, and takes a screenshot to see what it did. Then
you play the build yourself, talking out loud, and your voice comes back as
classified feedback joined to the game's own telemetry on one clock.

The point is not that agents write code. It is that the pipeline **refuses** —
out-of-scope work, a spend ceiling, a locked file, an agent trying to approve its
own art. Approval is human-only throughout: an agent records a verdict, it does
not sign off.

Local-first: one SQLite file per game project, no daemon, no cloud, no build step
in the frontend.

**Who it is for:** solo developers and small teams running Claude Code (or
another MCP client) who want an agent fleet to build a real, playable game and
want to keep the steering wheel. If you want a chat that writes a design
document, this is far too much machinery.

> **New to this?** Everything below is reference. If you have never run an MCP
> server, start at **[`docs/start-here.md`](docs/start-here.md)** — it assumes
> nothing and defines the vocabulary once. The
> **[FAQ](docs/faq.md)** answers the questions people actually ask: how the
> character art was really made, whether this works for 3D (honestly), what it
> costs, and how to point it at a Godot game you already have. Unfamiliar term?
> **[`docs/glossary.md`](docs/glossary.md)**.

## Project status

Honest version, 2026-07-27, first public release. This is a solo project that
has built real games on one machine, and it shows in both directions.

**Works, and is exercised by the test suite and by daily use:** the MCP server
and its tools; seats, lanes, asset locks and the PreToolUse hook; the Godot
adapter (headless run/check, import with in-engine inspection, screenshots,
scaffolds); both image providers;
the dashboard; playtest capture through to a joined brief; `bgate publish`;
`bgate doctor`. CI runs the suite plus a clean-venv wheel smoke test.

**Works, but not covered by CI:** the Blender adapter and the glTF round trip.
They are exercised by daily use and by tests that drive a real Blender — but
those tests are `slow`-marked or skip when Blender is absent, so the CI badge
above says nothing about them. Treat 3D as the less-travelled path; see
[docs/faq.md](docs/faq.md) for exactly how much thinner it is than 2D.

**Half-built, and named as such:** the audio seat's workspace is a deliberate v1
(sound library, playback, cue sheet — the UI says so in a banner). The
dashboard's error surfacing is uneven; a failed mutation still sometimes renders
as nothing happening (see [`docs/ui-ux-audit.md`](docs/ui-ux-audit.md)). Godot
version detection reports "unknown" on some builds.

**A proposal, not a product:** [`bgate_engine/`](bgate_engine/) is a design note
with JSON schemas and **no runtime code** — nothing in the repository imports it.
Its central claim (a second authoritative simulation in Python) was **withdrawn**
after its own §16.4 experiment came back negative against two other titles. It
ships because the schemas are packaged data and the reasoning is worth reading,
not because it is a direction.

**Provenance to weigh:** most of this was proven against a small number of games
on one Windows machine. [`docs/qa-nitpick-audit.md`](docs/qa-nitpick-audit.md) is
a harsh self-audit of exactly that; its top-10 blockers have since been worked,
and its status header says which.

## Contents

**Getting it running**
[Requirements](#requirements) ·
[Platform support](#platform-support) ·
[Setup](#setup-once) ·
[Building a game with it](#building-a-game-with-it--the-loop)

**The surfaces**
[What's in the box](#whats-in-the-box) ·
[The dashboard](#the-dashboard) ·
[Seats](#seats) ·
[Asset locking](#asset-locking) ·
[Blender → Godot](#the-blender--godot-round-trip) ·
[Templates](#templates) ·
[Publishing](#publishing--the-arcade) ·
[Playtest mode](#playtest-mode)

**How it works, and what it cost to learn**
[Layout](#layout) ·
[The concepts that carry the design](#the-concepts-that-carry-the-design) ·
[Gotchas found the hard way](#gotchas-found-the-hard-way) ·
[Choices worth knowing](#choices-worth-knowing)

**Meta**
[Working on Builders Gate itself](#working-on-builders-gate-itself) ·
[Docs](#docs) ·
[Contributing, security, licence](#contributing-security-licence)

## What's in the box

- **Design bible + lore canon** — pillars, scope tiers with a mechanical cut
  line, an entity graph with atomic facts, and `canon_check` (a deterministic
  lexical gate every narrative write passes through)
- **Seven agent seats** — director / narrative / gameplay / tech / art / audio /
  qa, each with write lanes, one-call briefs, and a shared blackboard;
  a PreToolUse hook gives the lanes teeth
- **Blender adapter** — headless bpy with structured feedback (tri counts, UV
  warnings, renders), sprite factory, glTF export verified in-engine
- **Godot adapter** — headless run/check, asset import with engine inspection,
  live game screenshots, project scaffolds with telemetry and F1 live-tuning
  autoloads already wired
- **Painted-art leg (optional)** — portraits/UI/backdrops and reference-first
  sprite sets with pinned reference anchors, from **two** providers: OpenAI
  `gpt-image` and Krea's 14-model catalogue, chosen per asset and per quality tier
- **Asset registry** — content hashes + per-file locks for binaries (they don't
  merge), with a drift detector that names silent clobbers
- **Playtest mode** — record the game window + your voice, whisper-transcribe,
  classify feedback, join it to game telemetry on one clock, and export a bug
  report you can paste into a tracker
- **Dashboard** — nine views over the same store: overview, live agents you can
  steer mid-run, node editors, per-seat workspaces, playtests, assets, the
  project atlas, the world bible, and the iteration timeline
- **The arcade** — `bgate publish` turns every game on the machine into a static
  site with a page per game (real controls, read from the input map) and gets it
  under the host's per-file limit, which Godot 4's 38MB wasm otherwise breaks
- **Gates with teeth** — the cut line refuses out-of-scope work, a spend budget
  refuses an agent that would blow the ceiling, watchdogs kill a wedged run, and
  approval is human-only: an agent records a verdict, it does not sign off

## Requirements

- Python 3.11+ (`pip install -e .` pulls mcp/fastapi/uvicorn/Pillow/openai)
- An MCP client to drive it — [Claude Code](https://claude.com/claude-code) is
  what it is developed against
- [Godot 4.x](https://godotengine.org) — portable exe is fine; discovery checks
  common install dirs, or set `BGATE_GODOT`. Add the **Web export templates** if
  you want `bgate publish` (a separate ~1 GB download from the editor)
- [Blender 4.2+](https://blender.org) (optional, for the 3D leg) — or set
  `BGATE_BLENDER`
- `ffmpeg` + `ffprobe` on PATH (optional) — screen capture, frame extraction,
  reading a recording's duration back
- `faster-whisper` + `sounddevice` (optional, for playtest transcription):
  `pip install -e ".[stt,record]"`

### API keys — two image providers, either or both

The art seat needs at least one. Copy [`.env.example`](.env.example) to `.env`
**at your game project's root** and fill in what you have:

| Variable | Provider | What it buys |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI `gpt-image` | Portraits, UI, backdrops, reference-first sprite sets. Prices by quality tier. |
| `KREA_API_KEY` | [Krea](https://krea.ai) | A catalogue of ~20 models (Flux, Imagen, Nano Banana, Krea-2) behind one key, with `image_style_references` as first-class input — which is exactly what the art seat's pinned anchors are. Prices per model, per request. |

Neither returns usable transparency (measured: `background="transparent"` came
back as a brown gradient), so sprite work goes through the chroma-key path in
`bgate_core/chroma.py` either way — see the module docstring in
`bgate_adapters/krea.py` for the full comparison.

`.env` and `.env.*` are gitignored here **and** in every project `bgate init`
stamps out (they were not, for a while, which is how following these
instructions committed a key). Keys are loaded per-project and never logged.

Note that `bgate doctor` currently probes `OPENAI_API_KEY` only — a Krea-only
setup will show `MISS openai_key` and exit 1 while working fine.

### Platform support

**Windows is the supported platform.** It is what everything is developed and
verified on. **Linux is best-effort:** CI runs the suite there but marks it
`continue-on-error`, because parts of the product shell out to Windows tooling
(`taskkill`, `tasklist`); the tests that need it skip cleanly, the rest has
simply never been depended on there. **macOS is untested.** Reports from Linux
and macOS are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

`bgate doctor` answers all of the above in one pass and exits 1 if anything is
unavailable — it is the line a setup script or a CI step runs instead of
grepping five status commands for "not found". It never opens the microphone,
launches an engine, or spends money; every probe is wall-clock bounded and
reports `{available, path, version, min_required, reason}`.

## Setup (once)

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .                          # or: pip install -e ".[dev,stt,record]"

bgate doctor                              # python/key/ffmpeg/blender/godot/whisper
bgate init emberfall --kind 2d            # a project AND a runnable game
cd emberfall
                                          # optional: drop a .env here with your
                                          # image key — see .env.example
bgate serve                               # dashboard on http://127.0.0.1:7788
```

`bgate doctor` exits 1 if **anything** on the list is unavailable, which is the
right behaviour for a CI step and a slightly alarming one for a human: you only
need Python and Godot for the core loop. Read the rows, not the exit code.

`bgate init` creates `.bgate/game.db`, unpacks the Godot template, and prints the
absolute path it wrote to — into a NEW directory named after the project, not
whatever directory you were standing in. There is no "make a project" step
hidden inside an MCP session any more; if you would rather start in the browser,
`bgate serve` with no project shows a first-run screen that posts the same call.

### Already have a game? `bgate adopt`

`bgate init` scaffolds into an EMPTY directory. If you already have a Godot
project — months of work, your own layout — adopt it instead:

```bash
cd my-existing-game
bgate adopt --pitch "what this game is"   # or: bgate adopt path/to/game
```

Adoption is additive only. It never copies a template file and never rewrites a
byte you wrote. It creates `.bgate/game.db`, merges the API-key ignore rules
into your existing `.gitignore` inside a marked block, appends a `CLAUDE.md`
briefing the same way, and prints what it detected — Godot version, main scene,
2D vs 3D, scene/script/asset counts — so you can see it read your project rather
than replaced it. Safe to re-run: the second run refreshes the marked blocks in
place instead of stacking a second copy.

Both `init` and `adopt` stamp a **`CLAUDE.md`** into the project. That file is
the instructions for the Claude Code session working in *your game*: what a seat
is, how a work item is created and closed, what the bible and lore are for, the
art pipeline, and what not to do. It is the first thing to read.

### Switching between projects

```bash
bgate projects                    # every known project, * marks the active one
bgate use emberfall               # by registered name, or by directory
```

`bgate use` writes a pointer to `~/.bgate/active.json` (user-scoped, never in the
repo), so `bgate serve`, `bgate doctor` and the MCP tools pick it up without you
exporting `BGATE_ROOT` in every shell. It is the LOWEST-priority answer on
purpose — an explicit `project_dir=` on a tool call wins, then `BGATE_ROOT`,
then standing inside a project directory, and only then the pointer.

To let agents drive it, register the MCP server and install the enforcement hook:

```bash
claude mcp add builders-gate --scope user -- <abs-python> -m bgate_mcp.server
bgate hook-install <game-project>         # lane/lock teeth
```

Registration must use the ABSOLUTE python path — the claude CLI's health check
resolves a bare `python` differently than your shell and reports
"failed to connect" against a server that runs fine.

Enforcement activates when a session sets `BGATE_SEAT=<role>`: the PreToolUse
hook asks `seats.can_write` and blocks out-of-lane or lock-violating writes
(exit 2 with guidance). No seat adopted, not a bgate project, or anything
unexpected → the hook is inert / fails open — a crashing hook must never dam a
session.

## Building a game with it — the loop

Step 1 is `bgate init`. Everything after it is an MCP tool call; any Claude
session (or agent) with the server registered can drive it. The intended shape:
you (or an orchestrator) fan out one agent per seat, each adopting its role via
`BGATE_SEAT`.

Every tool takes an optional `project_dir` and resolves the project from it, then
`BGATE_ROOT`, then by walking up from the cwd for a `.bgate/` dir. Pass it
explicitly whenever more than one project could be in play — it is the only way a
call is guaranteed to land in the game you mean. (`project_select` is deprecated
and switches nothing: it used to mutate a module-level active root, which made
"which game does this call affect" a function of call order.)

```text
1  bgate init <name>       .bgate/game.db + a runnable game, path printed
2  godot_scaffold          (or, into an existing project: the same runnable slice)
3  DIRECTOR seat           bible_add: pillars, the core loop, scope tiers, the CUT LINE
4  NARRATIVE seat          lore_add / lore_fact (locked facts mirror real tunables),
                           canon_check on every narrative write
5  ART seat                ref_pin approved references first; then blender_sprites /
                           image_sprites (reference-first painted sets) / image_generate;
                           asset_lock before touching any binary, asset_release after
6  GAMEPLAY seat           writes code in its lanes; godot_check_project + godot_run
                           after every change; godot_screenshot to SEE the game
7  QA seat                 headless test scripts via godot_run; asset_verify for drift
8  playtest_check/start    play it yourself, talk out loud; feedback lands classified
                           and joined to telemetry; YOU promote what becomes work
```

Rules that make multi-agent work safe: check `seat_can_write` before writing
outside your obvious lane, lock binaries before editing, leave a
`seat_post_note` when your work changes another seat's world, and
`scope_check(rank)` before building anything new — that one is advice; the
refusals behind it are under **The cut line** below. `seat_brief(role)` returns
everything a seat needs to start — mission, lanes, bible, canon, pinned
reference anchors, promoted feedback, and who holds which locks.

## The dashboard

```bash
bgate serve [--port 7788]     # from inside a project, or BGATE_ROOT
```

It prints the URL and the project it opened, because a command that starts the
product and says nothing looks like a hang. With no project it does not error —
it shows a first-run screen that creates one.

Nine views over the same store:

- **Overview** — live agents, the queue, the build, and a play/record panel
- **Agents** — dispatch work to a seat, then watch and steer it live. A run is
  spawned on a captured git base commit, so its work is readable as per-file
  diffs and undoable with a scoped revert (refused if anything it touched has
  changed since, unless you look at the diff and insist)
- **Studio** — node editors over the existing endpoints: workflow graphs and a
  Godot-style game workspace. A workflow run is a real graph — steps queue seat
  work, consistency nodes carry a measured score, and a `gate` node stops the run
  until a **human** approves or rejects it
- **Seat workspaces** — one workspace per seat, tuned to its craft. Art's is the
  flagship: every candidate revision beside the reference it was drawn against,
  two frames stacked with an opacity slider / `difference` blend / palette delta,
  batch approve-reject over a selection, and a dispatch button for an
  **independent** QA reviewer that never made the image
- **Playtests** — recorded sessions: video, transcript, telemetry, the director's
  triage, editable repro steps, and a bug report exported as markdown (or a zip
  with the frames it links) so the evidence stops being trapped in the session
- **Assets** — immutable revisions grouped by logical asset, with an integrity audit
- **Atlas** — every screen wired to every asset it uses, derived live from the
  scenes, scripts, and SpriteFrames. Click a node to file work against it
- **World bible** — a write surface, not a viewer: pillars, constraints, and one
  drag-ordered list of scope tiers with the **cut line as a draggable row in it**,
  plus the lore graph. Every narrative write runs `canon_check` first; a conflict
  is a 409 carrying its flags, and only a human may override it
- **Timeline** — the causal chain per iteration: goal, source/build snapshot,
  assets, playtest evidence, decisions, work, resulting build, outcome

The cockpit owns explicit user-facing mutations: queue/dispatch, recording,
feedback disposition, bible authoring, and artifact approval. Production
mutations remain MCP tools attributable to a seat. Approval is human-only
throughout — the dashboard identifies an agent's session by `BGATE_ACTOR` and
refuses it the bible, the scope filing, the budget, the revert, a workflow gate,
and promoting a candidate to the build.

127.0.0.1 is not a security boundary — any page in your browser can POST to
localhost — so every mutation must be same-origin AND carry a per-project bearer
token from `.bgate/ui-token` (gitignored, 0600). The page is served with the
token injected and `fetch` wrapped to send it same-origin only; nothing else is
asked to know it. `BGATE_NO_AUTH=1` opts out for a scripted run.

No build step, no node, no CDN.

## Seats

Seven stable game-dev identities — director, narrative, gameplay, tech, art,
audio, qa. A seat is an identity a working agent **adopts**, not a spawned
process; there is never a per-task registration.

```text
seat_brief(role)            # mission, lanes, bible, canon, promoted feedback, locks, notes
seat_can_write(role, path)  # the write oracle — two gates, both must pass
seat_post_note / seat_notes # the blackboard between seats
seat_configure(role, …)     # per-project lane/mission overrides, or disable a seat
```

`seat_can_write` is the oracle a PreToolUse hook asks: the path must be inside
the seat's lanes **and** not locked by another seat. Being in-lane does not
excuse stomping art's locked `.blend` — that's why lanes and locks are two
separate gates. Unknown or disabled seats fail closed.

`seat_brief` replaces re-deriving project state from scratch: one call returns
the mission, the bible with the scope cut applied, canon entities, the promoted
playtest feedback routed to that seat, and who holds which binaries.

## Asset locking

Binary files don't merge — two agents editing one `.blend` loses someone's work.

```text
asset_lock(path, seat)      # claim BEFORE editing; a held lock errors, not queues
   …edit…
asset_release(path, seat)   # frees it and records the new content hash
asset_verify()              # audits everything: names silent clobbers
```

`asset_verify` is the drift detector: a changed hash with **no lock held** means
someone stomped the file outside the discipline — it's named, not silently
absorbed. Locked files are expected to differ and aren't drift.
`godot_import_asset` auto-registers what it lands, so bridge output is covered
from birth. Locks are advisory at this layer — enforcement is the PreToolUse hook
from **Setup** — but verify makes violations visible even without it.

## The Blender → Godot round trip

The spine: an agent models in Blender, exports glTF, and the asset lands usable
in Godot — verified in the engine, not just on disk.

```text
blender_export_gltf(out.glb, script=…)   # build + export; modifiers APPLIED
godot_import_asset(project, out.glb)      # copy in, import, load in-engine
   → engine_view: {total_tris, meshes:[{tris, has_uv, material, aabb}]}
```

`godot_import_asset` doesn't trust the file — it loads the resource inside a real
headless Godot and reports the mesh the *engine* built. A `.glb` that imports with
zero surfaces is a silent failure; checking tri counts on both ends catches it.
Measured end to end: a beveled shard came out **106 tris in Blender → 106 tris in
Godot**, UVs and material intact. Matching counts prove the modifier survived —
which it only does because export applies modifiers (Blender defaults that off,
and a naive export ships the un-beveled base mesh).

`blender_export_gltf` also returns **game-readiness issues** — no UVs (can't be
textured), n-gons (triangulate unpredictably per exporter), unapplied/non-uniform
scale (shears children) — each cheap to catch here, expensive to debug in-engine.

## Templates

```bash
bgate init emberfall --kind 2d                # or 3d — the usual way in
godot_scaffold(name="Emberfall", kind="2d")   # the same slice, from an agent
godot_check_project(dest)                     # import + validate headless
```

Both are runnable slices, not empty shells: a player, ground, something to jump
onto, and the BGate autoloads already registered. The 2D slice is a side-on
platformer and reads exactly three actions — `move_left` (A/←), `move_right`
(D/→), `jump` (Space). That is the whole control surface; anything else you see
advertised is not in the template. The feel tunables (`gravity`,
`fall_multiplier`, `coyote_time`) are exported **and** emitted on every
jump/land — so the first playtest already produces the join that makes "the jump
feels floaty" actionable.

**F1 opens a live tuning overlay** over the running game. Every `@export` the
current scene exposes gets a control bound to the live node, and moving a slider
moves the game — no apply button, because the point is to feel the change while
you make it. Values persist to `.bgate/tunables.json` and are re-applied at boot,
which is the same file the iteration snapshot reads as `overrides`, so a tuned
build is visible rather than invisible drift. A release export is inert: no input
hook, no file access, no overlay.

`BGATE_AUTOQUIT=<seconds>` runs a build unattended (headless smoke tests, CI).
Without `BGATE_TELEMETRY` set, the autoload is completely inert — open the game
normally and nothing is written.

## Publishing — the arcade

One command turns every game on this machine into a static site anyone can play
in a browser.

```bash
bgate publish                          # -> ./arcade, ready to deploy
bgate publish --dry-run                # what would ship, and what would not
bgate publish --serve                  # preview it exactly as the host serves it
bgate publish --project emberfall      # just this one (repeatable)
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
own input map — the same reader the dashboard uses — so the keys listed are the
keys bound, and a game with no custom actions says so instead of inventing a
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

Godot 4's release `index.wasm` is ~38 MiB. **Cloudflare Pages and Workers reject
any asset over 25 MiB**, so a naive deploy of any Godot 4 web build fails — after
the upload, not before it. `bgate publish` measures every file against the
target host's ceiling and gzips the ones that break it *under their original
names*, emitting the matching `Content-Encoding` rules into `_headers`. 37.7 MiB
becomes 9.6 MiB and the browser unwraps it at the transport layer.

```bash
bgate publish --host cloudflare   # 25 MiB/file, pre-compress  (default)
bgate publish --host netlify      # 25 MiB/file, pre-compress
bgate publish --host github       # 100 MiB/file, no compression needed
bgate publish --host itch         # 1 GB/file
bgate publish --host none         # ship the bytes as they are
```

A file still over the limit *after* compression is reported as an error with the
URL and the size, because the alternative is finding out from a failed deploy.

`--serve` reads the generated `_headers` and applies it, so the preview is the
deployment: a plain `python -m http.server` would hand the browser gzip bytes
labelled `application/wasm` and the game would die at the loader.

The shipped Web preset (`templates/shared/export_presets.cfg`) exports
**without threads** on purpose — threaded builds need cross-origin isolation on
the host, which is exactly the thing free static hosts and iOS Safari do not
reliably give you. `_headers` still sets COOP/COEP, so flipping
`variant/thread_support=true` later is a re-export, not a hosting migration.

`bgate doctor` checks for the Web export templates specifically: they are a
separate ~1 GB download from the editor, and without them the export fails with
an error that reads like a broken preset.

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

**Agents cannot watch video.** The mp4 is for you. The brief is transcript +
frames pulled at each remark + game telemetry joined on one clock — so "the jump
feels floaty" arrives next to `jump {air_time: 0.94}`. The game emits JSONL
events (`playtest_telemetry_contract`); that join is what turns a vibe into a
number an agent can act on.

Items land as `new` and stay there until you promote them. Thinking out loud
mid-play is not a decision to build.

Native Godot sessions append telemetry to the session JSONL path. Web builds
loaded inside the cockpit post the same event contract directly to the active
session API using the `bgate_session` query parameter; the review screen marks
sessions with zero telemetry rather than silently presenting them as aligned.
Each start automatically records the Git commit and dirty fingerprint, source
fingerprint, exported PCK hash, active artifact revision IDs, exported tunables
and overrides, latest automated-check result, and telemetry schema version.
`iteration_record_checks` updates the check snapshot; `iteration_status` returns
the complete causal history.

## Layout

```text
bgate_cli/        the `bgate` console script: init, serve, publish, doctor, hook
bgate_core/       db, project, bible, lore, canon, scope, spend, queue,
                  workflows, artifacts, playtest, iterations, git, search
bgate_mcp/        FastMCP server (stdio)
bgate_adapters/   blender, godot, imagegen, sprites, recorder, transcribe
bgate_ui/         dashboard backend + routes/ + the single-page static/ front end
bgate_site/       `bgate publish`: the static arcade + its theme/
templates/        Godot project skeletons (2d, 3d, shared autoloads)
bgate_engine/     a design proposal + JSON schemas — no runtime code, nothing
                  imports it. See bgate_engine/README.md for its actual status
docs/             findings from real production runs, and the audits — docs/README.md
                  indexes them; docs/history/ is archived handoff notes
tests/
```

## The concepts that carry the design

**The cut line.** Scope tiers are ranked; the `cut_line` section marks where
shipping stops. Anything ranked at or below it is explicitly not being built.
This is the only mechanism that reliably stops an agent fleet from gold-plating —
`scope_check(rank)` answers "should I build this?" without a judgment call.

It is a refusal, not advice. `queue.add` will not FILE work under a cut tier, and
the dispatcher re-checks at the last possible moment before spawning a process:
the line moves, so an item queued legitimately can be retroactively out of scope
by the time anyone runs it, and spending an agent on that is the exact
gold-plating the tiers exist to stop. Untiered work is deliberately allowed
through, loudly flagged — refusing it would make the first cut line anyone draws
reject the entire existing queue, and the predictable fix would be to turn the
gate off, which is how a gate stops gating.

**Money and wall clock are the two things that run away unattended.** Every
paying call appends to a spend ledger, and the dispatcher consults the budget
*before* a process exists: per-item, per-day, per-project ceilings and a
concurrency cap (the dashboard's "dispatch all" has no cap of its own — twenty
queued items is twenty claude trees on one laptop). Once running, a watchdog
kills the tree when it passes its runtime or cost ceiling and says so on the
item. A dispatch also refuses a dirty tree by default, because a run started on
top of uncommitted work produces a diff that cannot tell the agent's edits from
yours.

**An agent may propose; only a human approves.** A spawned session carries
`BGATE_ACTOR=agent:item-<id>`, and that is what makes "approved" mean anything.
An art agent that judged its own frame approved off-style drift three times, and
a second agent doing it instead is the same failure with an extra hop — so
`art_qa_verdict` lets a reviewer FAIL a candidate outright (refusing to ship is a
call a machine can make alone) while a pass only records evidence and leaves the
revision a candidate waiting for a person.

**Facts vs. prose.** Entity `body` is prose for humans. `canon_fact` rows are one
atomic, checkable claim each ("The siege lasted seven years"). You cannot diff a
paragraph for contradictions; you can diff a sentence. `canon_check` reads facts.

**canon_check is a filter, not a judge.** Deterministic lexical checks — retired
entities on stage, invented proper nouns, polarity flips, number disagreements.
No model call, so it can run on every write. It will not catch subtle thematic
drift, and `ok` only means nothing *mechanical* is wrong. An LLM adjudication
layer can consume this output; it can't replace it, since a model checking its
own output for canon drift is the fox guarding the henhouse.

**Assets lock, they don't merge.** Two agents editing one `.blend` is the failure
mode the `asset` table exists for. Content-hashed, seat-locked, never merged.

**Blender gives facts back, not logs.** `blender_run` returns per-object tri/vert
counts off the *evaluated* mesh (so modifiers count), UV warnings, materials, and
optionally a render. A script that throws is a normal result with `ok=False` plus
the traceback and the partial scene — an agent that can't see what it built will
confidently produce nothing.

## Gotchas found the hard way

**GPU cold start will eat your first render.** Measured here (Blender 4.5,
Windows): the first EEVEE render after a cold boot blew past a 240s timeout. Every
run after took 1–12s — the *same script* that timed out later ran in 1.4s.
Clearing Blender's own `gl-shader-cache` did **not** bring the stall back, so the
warmup lives below Blender (GPU driver shader cache, or the OS first-loading
Blender's GPU DLLs). Root cause unconfirmed; the cost is real and reproducible.

Mitigation: `blender_warmup()` once per boot to pay it deliberately, and the first
GPU-engine render gets `COLD_START_TIMEOUT` regardless of the caller's timeout —
an agent's real render should never be the one that stalls. Iterate on
`BLENDER_WORKBENCH` (~1s) and switch to EEVEE/Cycles only for a beauty pass.

**`bpy.ops.uv.smart_project` needs EDIT mode.** In OBJECT mode it fails
`poll()`. In EDIT mode it's fine headless (~0.5s) — it does not hang, despite the
folklore.

**Subprocesses from a stdio MCP server MUST use `stdin=DEVNULL`.** The server's
stdin *is* the client's protocol channel; a child that inherits it blocks forever
at ~0% CPU and can corrupt the session. This presents as a *slow* render and gets
misdiagnosed as a GPU stall. Tell: works standalone (stdin is a terminal), hangs
under the server. Diagnose by **CPU time, not wall clock** — an idle child is
blocked, a busy one is genuinely slow. Cost us an hour on the Blender adapter.

**Godot's plain `.exe` does NOT lose stdout when piped** — measured on 4.7.1,
both it and `_console.exe` deliver identical output. The console variant is a
~200KB launcher that only attaches a console *window* for double-clicking. We
prefer the main exe: same output, one less process to leak on a kill.

**A failed unzip leaves a 0-byte `.exe`** that looks installed and fails with
"not recognized as a program". Discovery rejects stubs under 64KB.

**ctranslate2's `device="auto"` picks CUDA on any NVIDIA box** without checking
that the CUDA libraries load — then dies at inference with `cublas64_12.dll is
not found`. Worse, `WhisperModel(...)` construction touches no CUDA and
`transcribe()` returns a **lazy generator**, so a naive probe "succeeds" without
running an encode. The runner consumes the generator to force a real encode, then
falls back to CPU/int8 and reports why.

**Whisper segments are not utterances.** One segment routinely holds several
remarks: *"the jump feels floaty. I do not like it. But I love the music here."*
Classified whole, that becomes ONE item routed to **audio** (the word "music"
wins) — a physics complaint lands on the wrong seat and the compliment vanishes.
Segments are split per sentence with interpolated timestamps.

**The game's clock and the recorder's clock are unrelated.** The game may have
been running an hour before you hit record. Telemetry therefore carries `ts` (unix
wall clock), and `playtest_session.started_epoch` anchors the conversion. A raw
"seconds since game start" silently offsets every join by however long the game
had been up. If an event arrives without `ts`, ingest says so rather than quietly
assuming the clocks agree.

**Uninitialized telemetry lies plausibly.** The template player spawns in mid-air;
with `_peak_y` initialized only on jump, the opening drop reported
`peak_height: 302` for a 24px player and no jump had happened. Nonsense that looks
like a measurement is worse than a missing field — it sends an agent chasing
physics that never occurred. Airborne state is now stamped on every entry
(`spawn` / `jump` / `fall`) and `cause` rides along on every landing.

**Speech-to-text does not preserve your word choice.** "floaty" comes back as
"floating"; `\benemy\b` silently misses "the enemies are too fast". Match stems,
not the adjective you imagined. Short pronoun remarks ("I do not like it") carry
no routable noun and inherit the previous seat — but only within a segment, since
across a pause "it" is anyone's guess.

## Choices worth knowing

- **SQLite over Postgres** — Builders Gate projects are per-game and often
  throwaway. A daemon per game is a tax with no return. `.bgate/game.db` travels
  with the repo.
- **GDScript over .NET** — the agent loop is edit → headless run → result. .NET
  puts a compile step between every iteration, and GDScript is what the models
  have actually absorbed from Godot's docs and forums.
- **FTS5 over embeddings, for now** — no daemon, no model download, no cold start.
  Semantic recall can layer in behind the same `find()` signature later.

## Working on Builders Gate itself

```bash
pip install -e ".[dev]"
python -m pytest -m "not slow" -q
```

`-m "not slow"` deselects the tests that drive real Blender and real whisper (and
the in-suite wheel build). It is what CI runs; drop it only if you have both
installed and want to wait.

CI runs that suite on Windows and Linux, plus a clean-venv wheel smoke test,
because the failure it exists to catch is invisible under `pip install -e .`: a
wheel that shipped no JavaScript and no `templates/` produced a dashboard of 404s
and a scaffolder that raised `FileNotFoundError`, and nothing had ever verified
otherwise. Linux is `continue-on-error` — see [Platform support](#platform-support).

## Docs

[`docs/`](docs/) is onboarding plus write-ups of things that went wrong on real
production runs and the audits. [`docs/README.md`](docs/README.md) indexes them
with a line each.

Start with [`docs/start-here.md`](docs/start-here.md) if you are new, and
[`docs/faq.md`](docs/faq.md) for the character-art process, the honest 3D
answer, and costs. Two of the findings are worth flagging directly:

- [`docs/gap-analysis.md`](docs/gap-analysis.md) — where the pipeline can improve
  tenfold, every gap backed by something that actually happened and what it cost.
- [`docs/qa-nitpick-audit.md`](docs/qa-nitpick-audit.md) — an eight-persona audit
  that took the product apart. **Historical**: much of it is fixed, and its
  status header says which. Read the header before believing a finding.

## Contributing, security, licence

Feedback is worth more here than patches — especially "it did not run on my
machine" and "this gate can be walked around". See
[CONTRIBUTING.md](CONTRIBUTING.md) for what to include in a report and
[CHANGELOG.md](CHANGELOG.md) for the release state.

This tool executes arbitrary GDScript, shells out, and spawns agent sessions with
edit permissions. [SECURITY.md](SECURITY.md) states plainly what the localhost
guards do protect against (a browser page reaching your dashboard, including via
DNS rebinding) and what they do not (a hostile local user, a network deployment,
untrusted input to the adapters). Report vulnerabilities privately through GitHub
Security Advisories, not a public issue.

MIT — see [LICENSE](LICENSE).
