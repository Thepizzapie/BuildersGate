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
Builders Gate is one such server (`bgate_mcp/server.py`, some eighty tools). It runs on
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
(`queued | dispatched | review | done | failed | cancelled`), and optionally the
scope tier it belongs to. Filed with `queue_add`. It is a row in
`.bgate/game.db`, not a running thing. Filing an item costs nothing and starts
nothing. `review` is finished-but-not-counted — see **the approval gate**.

### Work chain

Dependent items filed as one ordered group with `queue_add_chain`, where link N
does not become dispatchable until link N-1 reaches `done`. It exists because
priority could not express a dependency: priority orders things that are *all
ready*, so auto-deploy started the item that needed a scene in the same tick as
the item that creates it, and the second agent wrote against a file that did not
exist yet. A blocked link is refused by dispatch (`blocked_on_dependency`, which
names the item it waits for), filtered out of auto-deploy's candidates, and never
handed to a seat by `queue_next`. Chains are strictly linear; a fan-out is
modelled as separate chains, because a DAG needs a graph editor to stay legible.

### The approval gate

Who has to sign off before an agent's work counts as `done`, in three modes
(`bgate_core/gates.py`, set per project in the dashboard or `POST /api/gate`):

* **no gate** — an agent's own word closes its item.
* **agent gate** — the QA seat reviews every maker deliverable and reopens it
  with a nitpick list on FAIL. This is what shipped before the setting existed
  and is still the default.
* **builder's gate** — the human approves. A finished item parks in `review`,
  and anything chained behind it stays blocked until somebody says yes.

The question the modes answer is not "strict or loose", it is *who is away*: a
board left running overnight wants the agent gate, a board being watched wants
the builder's gate. Approve/reject are HTTP-only (`/api/queue/{id}/approve`,
`/reject`) and deliberately have no MCP equivalent — a tool an agent can call is
a gate an agent can clear on its own behalf. `BGATE_QA_GATE=0` still forces no
gate, and the dashboard says so rather than showing a mode the env is overriding.

### The event log

Every status transition, gate change, spend refusal and question, as rows in the
`event` table (`bgate_core/events.py`, migration 0016). Subscribers read it
forward from a **cursor** — a row id, kept per consumer — which is what lets a
dashboard that was off for an hour resume exactly where it stopped instead of
losing the interval. Delivery is at-least-once, so every reaction carries a guard
query ("is a QA round already open for this item"); a subscriber that acts and
then dies before writing its cursor sees the same event again.

It is a table and not a file for one reason: `queue_complete` runs in the MCP
server process while the reaper runs in the dashboard's, so the log is
multi-writer across processes and an appended file cannot hand out a monotonic
sequence without a lock. `since()` reports `gap: true` when a cursor points below
the oldest surviving row — "you missed forty events" and "nothing happened" must
never look the same.

`.bgate/notify.jsonl` is still written, and it is still the shell-readable view:
one appended JSON line per transition, tailable with no database and no MCP. It
now carries two classes rather than one, because it used to carry only work-item
transitions and a batch of art candidates waiting on a human therefore produced
**zero lines** while the dashboard drew an approval card for each. A blocking
gate with no signal looks exactly like an agent quietly working.

| `kind`                | what happened                                     |
|-----------------------|---------------------------------------------------|
| `item.status`         | a work item changed status (the original line)     |
| `artifact.candidate`  | a generated revision is waiting on a human         |
| `artifact.reviewed`   | that decision was made                             |

`kind` is additive: every line still carries `{ts, item_id, status, seat, title}`,
so a consumer reading `status` sees exactly what it saw before. What the stream
still cannot tell you is whether a decision is *stale* — for the current pending
list, call the `pending_decisions` MCP tool, which answers with the parked chains,
the undecided candidates and the open questions in one call.

### The follow-up router

What happens after an agent finishes (`bgate_ui/followup.py`). One subscriber,
five branches: reopen a failure, open a QA round, leave a held item alone, narrate
a chain advancing, or debrief the director. The decision is a pure function —
`decide(events, settings, board)` returns actions and touches nothing — because
the loop it replaced was dead in production for weeks with a green suite, having
swallowed every exception it raised inside a thread.

The **director debrief** files a `source='completion'` item carrying the
harness-observed file list, the chain context, the gate mode and the budget left,
whose only legal moves are: dispatch the follow-up, `ask_human`, or close it out.
It is off by default, one per chain, rate-capped, skipped past `max_age_min`, and
never fires for `qa-gate`/`completion`/`chat` work — a completion loop that
debriefs its own debriefs is a money pump. It dispatches with `allow_dirty`
deliberately: a completing agent always leaves a dirty tree, so without that the
debrief would be refused every time and look like it silently never ran.

### The heartbeat

Events that come from elapsed time rather than a transition
(`bgate_ui/heartbeat.py`): `chain.stalled` when the head of a chain has not moved
for `notify.stall_hours`, `item.aging` for a `review` item nobody has looked at.
It exists because half of what goes wrong is an ABSENCE of transitions — an item
waiting on an approval that never comes emits nothing, so the quiet failure the
whole notification surface exists to fix would otherwise reappear one layer up.
Once per subject per stall, re-armed when the subject moves.

### Notifications

Four channels, one bus. The **bell and drawer** in the dashboard header read
`/api/events` on the console's existing poll; the **desktop window title** carries
the unread count (`bgate app`); the optional **webhook** is the only channel that
leaves the machine. The bell and the drawer only tell you things while you are
already looking at the page, which is stated rather than papered over — the window
title and the webhook are what survive a closed tab.

The webhook is https-only and refuses any host that resolves to a private,
loopback or link-local address: a loopback service POSTing a user-supplied URL is
an SSRF, and it carries what the agents are doing as its payload. It ships off,
and turning it on deliberately breaks the "nothing leaves this machine" promise.

`ask_human(question, refs)` is the director's way to reach you. It is an event,
not a work item — a question that becomes a queued row is a row somebody has to
dispatch in order to read.

### Settings

One registry (`bgate_core/settings.py`) DESCRIBING where each switch already
lives — a workspace doc, the `spend_budget` row, a module default. Storage did not
move; what the registry adds is one validator and one precedence rule, **env >
project stored > default**, with the API always reporting which layer won so the
panel can grey out what an environment variable owns.

Two kinds of env override, because the existing variables are not one kind: a
**supplying** var holds the value (`BGATE_DIRECTOR_MODE=collide`), a **coercing**
one forces it (`BGATE_QA_GATE=0` forces `gate.mode` to `none`). A boolean kill
switch cannot supply one of three modes, which is why they are separate.

A setting marked **`guard`** widens a safety guard rather than tuning behaviour —
`dispatch.allow_dirty` is the one. The panel confirms before turning one off and
the change lands in the activity ledger and on the bus, because it used to need
an environment variable and is now one click.

### Dispatch

Turning a queued work item into a **running agent**. The dashboard (or
`queue_next` plus your own orchestration) spawns a `claude` process with
`BGATE_SEAT`, `BGATE_ROOT`, `BGATE_WORK_ITEM` and `BGATE_ACTOR=agent:item-<id>`
in its environment, on a captured git base commit so its edits are readable as a
diff afterwards. Dispatch is where the refusals live. Before any process exists,
it re-checks the cut line, the concurrency cap, the spend ceilings, whether the
item's chain predecessor has landed, and whether your working tree is dirty
(`bgate_ui/dispatch.py`). Filing is free; dispatching is what spends money.

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
not the exit code. The `art_key` row asks the provider registry, so it is green
when either `OPENAI_API_KEY` or `KREA_API_KEY` is set.
