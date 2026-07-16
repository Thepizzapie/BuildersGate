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
| 4 | Godot adapter (headless run + project check) | done |
| 5 | Playtest recording → transcript → brief | done |
| 5b | 2D/3D project templates + telemetry autoload | next |
| 6 | Asset registry + binary locking | |
| 7 | Agent seats + fan-out | |

## Playtest mode

Play the game, talk out loud, get an agent-readable brief.

```
playtest_check    → preflight: ffmpeg, mic SIGNAL, transcriber, target window
playtest_start    → records game window (gdigrab) + your voice
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

**Speech-to-text does not preserve your word choice.** "floaty" comes back as
"floating"; `\benemy\b` silently misses "the enemies are too fast". Match stems,
not the adjective you imagined. Short pronoun remarks ("I do not like it") carry
no routable noun and inherit the previous seat — but only within a segment, since
across a pause "it" is anyone's guess.

## Choices worth knowing

- **SQLite over Postgres** — Forge projects are per-game and often throwaway. A
  daemon per game is a tax with no return. `.bgate/game.db` travels with the repo.
- **GDScript over .NET** — the agent loop is edit → headless run → result. .NET
  puts a compile step between every iteration, and GDScript is what the models
  have actually absorbed from Godot's docs and forums.
- **FTS5 over embeddings, for now** — no daemon, no model download, no cold start.
  Semantic recall can layer in behind the same `find()` signature later.
