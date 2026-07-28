# Glossary

2026-07-27. Every term Builders Gate uses in a sense you cannot look up
elsewhere. These are **this project's** meanings, not general ones. Where the
industry word is broader, the narrow meaning here is what the code implements.

Ordered roughly by when you will meet them.

---

### MCP (Model Context Protocol)

A standard way for an AI coding assistant to call tools that live outside
itself. You register a server once; from then on, tools it exposes appear to the
assistant as things it can call, the same way it can call "read a file".
Builders Gate is one such server (`bgate_mcp/server.py`, 78 tools). It runs on
your machine, over stdin/stdout, with no network service and no cloud account.
"Custom MCP" just means a server somebody wrote for their own domain instead of
using an off-the-shelf one.

### Seat

A fixed job title an agent **adopts** for a session: director, narrative,
gameplay, tech, art, audio, qa. Seven, always the same seven; there is no
registering a new one per task. A seat is not a process, it is an identity: a
session declares `BGATE_SEAT=art` and inherits art's mission, its writable
paths, and its brief. Defined in `bgate_core/seats.py`.

### Lane

The set of file paths a seat is allowed to write, as glob patterns. The art
seat's lanes are `["game/assets/**", "blender/**", "art/**"]`; gameplay's are
`["game/scripts/**", "game/scenes/**"]`. `seat_can_write(role, path)` is the
oracle, and it fails **closed**. An unknown seat, a disabled seat, or a path
matching nothing gets refused. This is what stops two agents editing the same
file for opposite reasons.

### Lock

A claim on one binary file, held by one seat, recorded in the database.
`asset_lock(path, seat)` before you edit; `asset_release(path, seat)` after,
which re-hashes the file. A lock that is already held **errors rather than
queues**. The second agent is told who holds it and to go do something else, not
to wait. Locks exist because `.png` and `.blend` files do not merge: two
agents editing one silently loses somebody's work. Lanes and locks are separate
gates and both must pass; being in your lane does not entitle you to stomp a
file art is holding. Every lock carries a **lease** sized to the operation
(120s for an import, 900s for a paint, 1800s for a bake) so a lock belonging to
a dead agent expires instead of blocking the path forever. Text files get a
softer parallel: an advisory path lease, meant to make concurrent edits visible
rather than impossible.

### asset_verify / drift

The audit. It compares every tracked file against the registry and sorts them
into `clean`, `locked` (changes expected), `modified` (**content changed with no
lock held, meaning someone stomped it**), `missing`, and `untracked_hash`. `modified`
is the one the tool exists to name; a silent clobber is otherwise invisible
until someone notices the art changed.

### Work item

One unit of queued work: a seat, a title, a brief, a status
(`queued | dispatched | done | failed | cancelled`), and optionally the scope
tier it belongs to. Filed with `queue_add`. It is a row in `.bgate/game.db`, not
a running thing. Filing an item costs nothing and starts nothing.

### Dispatch

Turning a queued work item into a **running agent**. The dashboard (or
`queue_next` plus your own orchestration) spawns a `claude` process with
`BGATE_SEAT`, `BGATE_ROOT`, `BGATE_WORK_ITEM` and `BGATE_ACTOR=agent:item-<id>`
in its environment, on a captured git base commit so its edits are readable as a
diff afterwards. Dispatch is where the refusals live. Before any process exists,
it re-checks the cut line, the concurrency cap, the spend ceilings, and
whether your working tree is dirty (`bgate_ui/dispatch.py`). Filing is free;
dispatching is what spends money.

### The bible

The design document, stored as structured sections rather than prose: pillars,
the core loop, constraints, art direction, and the scope tiers. It is a write
surface, not a viewer. Agents read it through `bible_read` and `seat_brief`, and
it is the source the art prompts are assembled from, so editing it changes the art
(`bgate_core/artdirection.py`). A bible that nothing reads is decoration; this
one is read at generation time.

### Lore

The world's content: entities (characters, places, factions) with prose bodies,
plus links between them. Distinct from the bible, which is about how the *game*
works.

### Canon

The subset of lore that is settled, expressed as **atomic facts**, one checkable
claim per row ("The siege lasted seven years"), separate from the
prose body. The split is deliberate: you cannot diff a paragraph for
contradictions, but you can diff a sentence.

### canon_check

The deterministic gate every narrative write passes through. It is lexical, not
a model call: retired entities appearing on stage, invented proper nouns,
polarity flips, number disagreements. It runs on every write because it is
cheap. `ok` means nothing **mechanical** is wrong. It will not catch thematic
drift, and it does not claim to (`bgate_core/canon.py`).

### Scope tier

A named, ranked band of ambition in the bible: roughly "must ship", "should
ship", "nice to have", "someday", in whatever words you choose. Work items can
be filed under one.

### The cut line

A marker placed between two scope tiers. Everything ranked at or below it is
**explicitly not being built**. This is the only mechanism in the tool that
reliably stops an agent fleet gold-plating, and it is a refusal, not advice:
`queue_add` will not file work under a cut tier, and dispatch re-checks at the
last possible moment before spawning, because the line moves and an item that
was in scope on Tuesday may not be on Thursday. Untiered work is deliberately
allowed through and loudly flagged. Refusing it would make the first cut line
anyone draws reject their entire existing queue, and the predictable next step
would be turning the gate off.

### Pin / pinned reference

An approved image copied into `.bgate/refs/` under a name, via `ref_pin`. From
then on, that name can be passed anywhere a path can, it appears in every seat
brief, and it is what generated art is measured against. The pinned reference is
**canon**: a correction that contradicts it requires re-pinning, which is a
deliberate act, not a sentence someone typed mid-flight.

### Character profile

The written identity of a character: traits, style, and a negative list of things
that must never appear. Authored *while looking at the pinned reference*, never
from memory. Stored with `profile_set`, and injected
automatically into every generation for that character, so nobody ever describes
the character from recollection again. This exists because an orchestrator once
issued a confident correction from stale prompt text, and the art agent that
refused it was right (`docs/character-consistency.md`).

### Consistency gate

`consistency_check(candidate, character)` **builds** a side-by-side composite of the reference and the candidate over a checkerboard, attaches the
profile's trait checklist and two measured tripwires (palette drift, and the
alpha audit), and requires the reviewer to verdict every line from that one
view. The alpha flags auto-fail. The rest is structured human-or-agent judgment,
because no similarity metric measured here separates identity from pose well
enough to be a gate. It exists because three off-style batches were approved by
agents judging frames in isolation.

### Chroma key

The mechanism that gives a sprite transparency, because no image model reliably
returns it. The pipeline picks a saturated colour the character's own palette
does not contain, demands a flat backdrop of exactly that RGB in the prompt,
keys it out afterwards, and then **audits the cut**: background bleed, white
halo, feathered edges, colour still sitting under transparent pixels, holes eaten
out of the art. A frame that fails the audit is a named failure, not a sprite
(`bgate_core/chroma.py`).

### Artifact / revision / candidate

Generated art lands as an immutable **revision** of a named logical asset. A new
revision is a **candidate** until a human approves it. An independent reviewer
can `fail` a candidate outright, because refusing to ship is a call a machine may
make alone. A passing verdict only records evidence. It does not promote.

### Evidence

Measurement of what the game actually did, as opposed to a screenshot of what it
looked like. `godot_evidence` runs the game, then walks the live scene tree at
capture time and reports every node as screen-pixel bounds, visibility, z-order,
and, for bars and labels, its runtime value. So "the health bar looks wrong"
becomes "the bar reads 0.6 while hp is 0.3". Also used more loosely for the
snapshot an iteration records: commit, build hash, active asset revisions,
tunables, last check result.

### Playtest

You playing the build while talking out loud, recorded. `playtest_start`
captures the game window and your microphone; `playtest_stop` transcribes it,
splits it per sentence, classifies each remark, routes it to a seat, pulls the
video frame at that moment, and joins it to the game's own telemetry on one wall
clock, so "the jump feels floaty" arrives next to `jump {air_time: 0.94}`.
Agents cannot watch the video; the brief is what they read. Items land as `new`
and stay there until **you** promote them, because thinking out loud mid-play is
not a decision to build.

### Iteration

One goal-to-outcome cycle, with its causal chain recorded: what you were trying
to do, the source and build it started from, the assets and decisions involved,
the playtest evidence, and what the build became. `iteration_status` returns the
history.

### Doctor

`bgate doctor` is one command that probes eight dependencies (python, the OpenAI
key, ffmpeg, ffprobe, blender, godot, godot's web export templates, whisper) and
reports `{available, path, version, min_required, reason}` for each. It never
opens the microphone, launches an engine, or spends money. The key check is
presence only, never a paid validation call. It exits 1 if **anything** is
missing, which is right for a CI step and alarming for a human: read the rows,
not the exit code. It probes `OPENAI_API_KEY` only, so a Krea-only setup shows
`MISS openai_key` and exits 1 while working fine.
