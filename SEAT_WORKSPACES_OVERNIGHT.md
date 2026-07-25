# Overnight build — per-seat workspaces, reference system, art-QA reviewer

Branch: **`feat/seat-workspaces`** (off `06ebd0e`). **Nothing is committed** — the
whole thing is uncommitted in the working tree for you to review, per your ask.
The dashboard is already running on http://127.0.0.1:7788 off this branch;
open the **"Seat workspaces"** tab (top nav) and use the sub-nav to switch seats.

Built with **zero new dependencies** (no pip/npm installs). Frontend is vanilla
JS split into per-seat modules; graph/canvas are hand-rolled SVG (the two
vendored libs weren't needed — nothing to break or download). Done with 7
parallel agents, each owning disjoint files.

## What's here

### Foundation (I built this myself, then verified)
- **Reference system** — global project refs **+ per-task anchored refs, layered**
  (task anchors take priority, global still applies). New table `task_ref`
  (migration 0010). Set them from any seat via the reusable **RefManager** panel
  (upload by drag/drop or file-pick — base64, no multipart dep — or pin an
  existing project file; anchor to the active work item or globally).
  Core: `bgate_core/task_refs.py`; API: `bgate_ui/routes/refs.py`
  (`/api/refs`, `/api/tasks/{id}/refs`, `/api/tasks/{id}/refs/resolved`).
- **App plumbing** — a `bgate_ui/routes/` package that **auto-registers** routers
  (drop a file, no `app.py` edit) + a `/static` mount for the JS modules +
  `bgate_ui/deps.py` (shared `root()`), and a **seat-workspace shell**
  (`static/seats/_core.js`: `BGWS` helpers, `SeatShell` dispatcher, `RefManager`).
- **`workspace_doc`** — a generic per-seat JSON store (migration 0010) backing
  storyboards, cue sheets, bot rosters. Core: `bgate_core/workspace.py`.
- **`art_qa_verdict` MCP tool** — how the independent reviewer records pass/fail +
  score and flips the artifact to approved/rejected (so art can't self-approve).

### The 7 seat workspaces (each verified rendering in-browser, 0 console errors)
| Seat | What it does | Files |
|------|--------------|-------|
| **Director** | Multi-agent cockpit: live board of all running agents (steer/stop each), queue board by seat, **"review for delegation"** (spawns a director that splits a task across seats), batch dispatch. Fixes "managing multiple agents is awful." | `director.js`, `routes/orchestrator.py` |
| **Art** *(flagship)* | Active-item picker, **anchoring panel** (RefManager), **iteration lab** (every revision as a filmstrip, each candidate **side-by-side with its reference**, approve/reject/regenerate), **flow map** (SVG: assets → GODOT when rigged in), and the **independent art-QA reviewer** with per-candidate verdict badges. | `art.js`, `routes/art_qa.py` |
| **Gameplay** | Godot workspace: script browser + read-only viewer, GDScript runner, build-check, screenshot, **the running game embedded** (F1 tuning), live agent + steer. | `gameplay.js` |
| **Tech** | Engine/build health: version, web-build stale/rebuild, build-check, **resource inspector** (scene tree/mesh/tris), full file tree, GDScript runner, live agent. | `tech.js` |
| **QA** | **Bot playtest env** — define bots (input schedules), run them to **drive the real game headless** and report positions/hp/stamina; playtest recording; live agent. *(Probe proven driving the fight: player walked, stamina drained, a jab landed.)* | `qa.js`, `routes/qa_bots.py` |
| **Narrative** | **Storyboard canvas** — draggable panels (title/beat/image), SVG connectors, link mode, autosave. | `narrative.js` |
| **Audio** | v1: sound library with in-browser playback (new audio-serve endpoint), cue sheet (event→sound), live agent. *Seat spec still open — TODOs in the UI banner.* | `audio.js`, `routes/audio_ws.py` |

## Verified
- Migration 0010 applied; every new endpoint returns 200; all 8 routers + 8 JS
  modules parse clean; all 7 seats render real data with **zero console errors**.
- **Art-QA reviewer, end-to-end**: an independent `qa`-seat agent reviewed a real
  candidate — ran `consistency_check` (palette_drift 24.7), *looked* at candidate
  vs reference, and recorded a verdict via `art_qa_verdict`: artifact #29 →
  **approved, 86/100**, with a detailed reason (correctly judged against the
  current canonical anchor, not a deprecated ref). This is the "independent QA
  agent compares reference vs produced" you asked for.

## Caveats / TODOs
- **Godot version** header shows via `godot.version()`; some builds report
  "unknown" (adapter-level, cosmetic).
- **Flow map** shows no GODOT edges yet because no artifact is rigged into the
  engine (`engine_import`) in the current project — edges appear once assets are
  imported. By design.
- **Audio seat** is a deliberate v1 (library + playback + cue sheet). Needs a
  spec from you for waveform/generation/in-engine hookup.
- **QA default bot** lands one jab then the CPU retreats — proves the pipeline;
  a position-reactive pressure bot is a future enhancement.
- The art-QA review earlier flipped artifact **#29 to approved** (a real, correct
  verdict from the verification run) — not junk, but noting the state change.

## File inventory (all uncommitted on `feat/seat-workspaces`)
Modified: `bgate_core/db.py` (migration 0010), `bgate_mcp/server.py`
(`art_qa_verdict` tool), `bgate_ui/app.py` (static mount + route registration),
`bgate_ui/static/index.html` (seats tab + shell wiring).
New: `bgate_core/task_refs.py`, `bgate_core/workspace.py`, `bgate_ui/deps.py`,
`bgate_ui/routes/*` (8 routers), `bgate_ui/static/seats/*` (8 modules).

## To keep it
Review the diff (`git diff main...feat/seat-workspaces` + the untracked files),
then commit/merge if you like it — I left it all uncommitted so you decide.
