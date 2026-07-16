# Builders Gate

An agentic game development pipeline over MCP. Design bible, lore canon, agent
seats, and headless Blender/Godot adapters — so a fleet of agents can plan, build,
and actually *playtest* a game instead of just writing about one.

Local-first: one SQLite file per game project, no daemon, no cloud.

## Status

| Step | | |
|---|---|---|
| 1 | Repo, SQLite schema, MCP server | done |
| 2 | Design bible + lore graph + canon_check | done |
| 3 | Blender adapter (headless bpy → render + mesh stats) | done |
| 4 | Godot adapter + 2D template | needs Godot |
| 5 | Playtest harness + QA gate | |
| 6 | 3D template + asset registry/locking | |
| 7 | Agent seats + fan-out | |

## Layout

```
bgate_core/       db, project, bible, lore, canon, search, util
bgate_mcp/        FastMCP server (stdio)
bgate_adapters/   blender, godot, playtest        [step 3+]
templates/        Godot project skeletons          [step 4+]
tests/
```

## Quickstart

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

Register with Claude Code from inside a game project:

```bash
claude mcp add builders-gate -- python -m bgate_mcp.server
```

Every tool resolves the project by walking up from the cwd for a `.bgate/` dir.
`BGATE_ROOT` overrides that when you need to point elsewhere.

## The concepts that carry the design

**The cut line.** Scope tiers are ranked; the `cut_line` section marks where
shipping stops. Anything ranked at or below it is explicitly not being built.
This is the only mechanism that reliably stops an agent fleet from gold-plating —
`scope_check(rank)` answers "should I build this?" without a judgment call.

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

## Choices worth knowing

- **SQLite over Postgres** — Forge projects are per-game and often throwaway. A
  daemon per game is a tax with no return. `.bgate/game.db` travels with the repo.
- **GDScript over .NET** — the agent loop is edit → headless run → result. .NET
  puts a compile step between every iteration, and GDScript is what the models
  have actually absorbed from Godot's docs and forums.
- **FTS5 over embeddings, for now** — no daemon, no model download, no cold start.
  Semantic recall can layer in behind the same `find()` signature later.
