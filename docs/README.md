# docs/

Three kinds of document live here. **Onboarding** is written for someone
arriving without context. **Reference** is the detail the README links out to.
**Findings** are write-ups of things that went wrong on real production runs,
plus the audits, kept because the reasoning is the useful part.

Every document is dated at the top and reflects the day it was written.

## Onboarding

New here, or never run an MCP server before? Read these three, in order.

| Document | What it is |
|---|---|
| [../CLAUDE.md](../CLAUDE.md) | Setup instructions written for a Claude session doing the install for someone else. |
| [start-here.md](start-here.md) | The front door. What problem this solves, what an MCP server is, this project's vocabulary defined once, what happens when you dispatch an agent, and a first-session walkthrough. Assumes nothing. |
| [glossary.md](glossary.md) | Every term this project uses in a narrow sense: seat, lane, lock, dispatch, cut line, pin, canon, evidence. A sentence or two each. |

## Reference

| Document | What it is |
|---|---|
| [setup.md](setup.md) | Setup in full: requirements, the API key table, `bgate adopt` for an existing game, switching projects, registering the MCP server and installing the hook, and platform support. |
| [reference.md](reference.md) | Every surface in detail: the dashboard's nine views, seats, asset locking, the Blender to Godot round trip, templates, `bgate publish`, playtest mode, and the repository layout. |
| [design-notes.md](design-notes.md) | The concepts the product rests on: the cut line, spend and runtime ceilings, human-only approval, facts versus prose, `canon_check`. Plus the technology choices and why. |
| [gotchas.md](gotchas.md) | Things that cost real time: GPU cold starts, `stdin=DEVNULL` under a stdio MCP server, whisper segmentation, unrelated clocks, telemetry that lies plausibly. |

## Findings and audits

| Document | What it is |
|---|---|
| [cinematic-research.md](cinematic-research.md) | The video-model landscape, the Godot format wall that silently swallows a finished cutscene, the anchoring limitation that turned out not to be real, why cutscenes are an eighth seat rather than a wider art seat, and the post-production half — transitions, score, captions, continuity and the scene that plays it. |
| [sprite-animation-research.md](sprite-animation-research.md) | Why sheets that passed every gate still animated badly. The registration bug (bounding boxes are not bodies), palette locking versus palette detection, per-frame timing Godot had all along, and the four cross-frame faults an identity judge structurally cannot see. Measured, with what was deliberately not built. |
| [visual-taste-research.md](visual-taste-research.md) | What the research literature says is computable about art, rigging and animation quality, what is judge-territory, and what is unsolved. Notes, not a build plan. |
| [lessons-from-a-shipped-game.md](lessons-from-a-shipped-game.md) | What a real shipped game cost to make, and the rules that came out of it. |
| [history/](history) | Archived agent-to-owner handoff notes. Historical only, see each file's header. |
