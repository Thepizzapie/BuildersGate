# Glossary

Every term Builders Gate uses in a sense you cannot look up elsewhere. These
are this project's meanings. Where the industry word is broader, the narrow
meaning here is what the code implements.

Ordered roughly by when you will meet them. New here? Read
[start-here.md](start-here.md) first.

## The board

**MCP (Model Context Protocol)**
A standard way for an AI coding assistant to call tools that live outside
itself. You register a server once, and its tools appear to the assistant
alongside "read a file". Builders Gate is one such server (`bgate_mcp/server.py`,
close to 200 tools), running on your machine over stdin/stdout with no network
service and no cloud account.

**Seat**
A fixed job title an agent adopts for a session: director, narrative, gameplay,
tech, art, audio, cinematic, qa. Eight, and the roster is fixed. A seat is an
identity, not a process: a session declares `BGATE_SEAT=art` and inherits art's
mission, writable paths and brief (`bgate_core/seats.py`).

**Lane**
The file paths a seat normally writes, as glob patterns. Art's are
`game/assets/**`, `blender/**`, `art/**`; gameplay's are `game/scripts/**`,
`game/scenes/**`. `seat_can_write(role, path)` is the oracle. Advisory by
default (`BGATE_LANES`): an out-of-lane write lands and the
human is warned; `block` restores hard refusal. The enforced boundary is the
**project** — a dispatched agent may not touch files outside the game it was
dispatched for (`BGATE_AEGIS`, default `block`).

**Lock**
A claim on one binary file, held by one seat, recorded in the database.
`asset_lock(path, seat)` before you edit, `asset_release(path, seat)` after,
which re-hashes the file. A held lock **errors rather than queues**: the second
agent is told who holds it and to do something else. Every lock carries a lease
sized to the operation (120s import, 900s paint, 1800s bake) so a dead agent's
lock expires. Lanes and locks are separate gates and both must pass. Text files
get an advisory path lease instead, which makes concurrent edits visible rather
than impossible.

**Work item**
One unit of queued work: a seat, a title, a brief, and a status
(`queued | dispatched | review | done | failed | cancelled`). Filed with
`queue_add`. A row in `.bgate/game.db`, not a running thing. `review` means
finished but not counted; see the approval gate.

**Work chain**
Dependent items filed as one ordered group with `queue_add_chain`, where link N
is not dispatchable until link N-1 reaches `done`. Priority could not express
this: it orders things that are all ready, so auto-deploy used to start the item
that needed a scene alongside the item that creates it. A blocked link is
refused by dispatch (`blocked_on_dependency`), filtered out of auto-deploy, and
never handed out by `queue_next`. Chains are strictly linear; model a fan-out as
separate chains.

**Dispatch**
Turning a queued item into a running agent. The dashboard spawns a `claude`
process with `BGATE_SEAT`, `BGATE_ROOT`, `BGATE_WORK_ITEM` and
`BGATE_ACTOR=agent:item-<id>`, on a captured git base commit so its edits read
as a diff. Before any process exists it re-checks the concurrency cap, the spend
ceilings, the chain predecessor, and whether your tree is dirty
(`bgate_ui/dispatch.py`). Filing is free; dispatching spends money.

**The approval gate**
Who signs off before an agent's work counts as `done`, in three modes
(`bgate_core/gates.py`, set per project in the dashboard or `POST /api/gate`):

| Mode | Who closes an item |
|---|---|
| no gate | the agent's own word |
| agent gate | the QA seat reviews every maker deliverable, reopening it with a nitpick list on FAIL (the default) |
| builder's gate | the human. The item parks in `review` and anything chained behind it stays blocked |

Pick by who is away. A board left running overnight wants the agent gate; a
board you are watching wants the builder's gate. Approve and reject are HTTP-only
(`/api/queue/{id}/approve`, `/reject`) with no MCP equivalent, because a tool an
agent can call is a gate an agent can clear for itself. `BGATE_QA_GATE=0` forces
no gate, and the dashboard says so rather than showing a mode the environment is
overriding.

## Events and notifications

**The event log**
Every status transition, gate change, spend refusal and question, as rows in the
`event` table (`bgate_core/events.py`). Subscribers read forward from a
**cursor**, a row id kept per consumer, so a dashboard that was off for an hour
resumes where it stopped. Delivery is at-least-once, so every reaction carries a
guard query. `since()` reports `gap: true` when a cursor points below the oldest
surviving row, because "you missed forty events" and "nothing happened" must not
look the same.

**`.bgate/notify.jsonl`**
The shell-readable view: one appended JSON line per transition, tailable with no
database and no MCP. Every line carries `{ts, item_id, status, seat, title}`
plus a `kind`:

| `kind` | what happened |
|---|---|
| `item.status` | a work item changed status |
| `artifact.candidate` | a generated revision is waiting on a human |
| `artifact.reviewed` | that decision was made |

The stream cannot tell you whether a decision is stale. For the current pending
list, call `pending_decisions`, which returns parked chains, undecided
candidates and open questions in one call.

**The follow-up router**
What happens after an agent finishes (`bgate_ui/followup.py`). One subscriber,
five branches: reopen a failure, open a QA round, leave a held item alone,
narrate a chain advancing, or debrief the director. `decide(events, settings,
board)` is a pure function returning actions and touching nothing.

The **director debrief** files a `source='completion'` item carrying the
harness-observed file list, chain context, gate mode and remaining budget. Its
only legal moves are: dispatch the follow-up, `ask_human`, or close it out. It
is off by default, one per chain, rate-capped, skipped past `max_age_min`, and
never fires for `qa-gate`, `completion` or `chat` work. It dispatches with
`allow_dirty`, because a completing agent always leaves a dirty tree.

**The heartbeat**
Events that come from elapsed time rather than a transition
(`bgate_ui/heartbeat.py`): `chain.stalled` when a chain head has not moved for
`notify.stall_hours`, `item.aging` for a `review` item nobody has looked at.
Half of what goes wrong is an absence of transitions. Once per subject per
stall, re-armed when the subject moves.

**Notifications**
Four channels, one bus. The bell and drawer in the dashboard header read
`/api/events`; the desktop window title carries the unread count (`bgate app`);
the optional webhook is the only channel that leaves the machine. The bell and
drawer only reach you while the page is open.

The webhook is https-only and refuses any host resolving to a private, loopback
or link-local address, because a loopback service POSTing a user-supplied URL is
an SSRF and the payload is what your agents are doing.

**`ask_human(question, refs)`**
The director's way to reach you. An event, not a work item: a question filed as
a queued row is a row somebody has to dispatch in order to read.

**Settings**
One registry (`bgate_core/settings.py`) describing where each switch already
lives: a workspace doc, the `spend_budget` row, a module default. Storage did
not move. What the registry adds is one validator and one precedence rule,
**env > project stored > default**, with the API reporting which layer won.

Two kinds of env override: a **supplying** var holds the value
(`BGATE_MODEL` sets `dispatch.model`), a **coercing** one forces it
(`BGATE_QA_GATE=0` forces `gate.mode` to `none`). A boolean kill switch cannot
supply one of three modes, so the two kinds stay separate.

A setting marked **`guard`** widens a safety guard rather than tuning behaviour.
`dispatch.allow_dirty` is the one. The panel confirms before you turn it on, and
the change lands in the activity ledger and on the bus.

## Design and story

**The bible**
The design document, stored as structured sections rather than prose: pillars,
core loop, constraints, art direction, references. Agents read it through
`bible_read` and `seat_brief`, and it is the source the art prompts are
assembled from, so editing it changes the art (`bgate_core/artdirection.py`).

**Lore**
The world's content: entities (characters, places, factions) with prose bodies,
plus links between them. Distinct from the bible, which is about how the game
works.

**Canon**
The subset of lore that is settled, expressed as **atomic facts**, one checkable
claim per row ("The siege lasted seven years"), separate from the prose body.
You cannot diff a paragraph for contradictions; you can diff a sentence.

**`canon_check`**
The deterministic gate every narrative write passes through. Lexical, not a
model call: retired entities appearing on stage, invented proper nouns, polarity
flips, number disagreements. It runs on every write because it is cheap. `ok`
means nothing **mechanical** is wrong; it will not catch thematic drift
(`bgate_core/canon.py`).

## Art

**Pin / pinned reference**
An approved image copied into `.bgate/refs/` under a name, via `ref_pin`. From
then on that name can be passed anywhere a path can, it appears in every seat
brief, and it is what generated art is measured against. The pinned reference is
canon: contradicting it requires re-pinning.

**Character profile**
The written identity of a character: traits, style, and a negative list of
things that must never appear. Authored while looking at the pinned reference,
never from memory. Stored with `profile_set` and injected into every generation
for that character.

**Consistency gate**
`consistency_check(candidate, character)` builds a side-by-side composite of the
reference and the candidate over a checkerboard, attaches the profile's trait
checklist and two measured tripwires (palette drift, alpha audit), and requires
the reviewer to verdict every line from that one view. Alpha flags auto-fail;
the rest is structured judgment, because no similarity metric measured here
separates identity from pose well enough to be a gate.

**Chroma key**
What gives a sprite transparency, since no image model reliably returns it. The
pipeline picks a saturated colour the character's palette does not contain,
demands a flat backdrop of exactly that RGB in the prompt, keys it out, then
**audits the cut**: background bleed, white halo, feathered edges, colour under
transparent pixels, holes eaten out of the art. A frame that fails the audit is
a named failure, not a sprite (`bgate_core/chroma.py`).

**Artifact / revision / candidate**
Generated art lands as an immutable **revision** of a named logical asset. A new
revision is a **candidate** until a human approves it. An independent reviewer
can `fail` a candidate outright; a passing verdict only records evidence, it
does not promote.

**`asset_verify` / drift**
The audit. It compares every tracked file against the registry and sorts them
into `clean`, `locked` (changes expected), `modified` (content changed with no
lock held, meaning someone stomped it), `missing` and `untracked_hash`.
`modified` is the one the tool exists to name.

## Running the game

**Evidence**
Measurement of what the game actually did, as opposed to a screenshot of what it
looked like. `godot_evidence` runs the game, walks the live scene tree at
capture time, and reports every node as screen-pixel bounds, visibility,
z-order, and for bars and labels its runtime value. So "the health bar looks
wrong" becomes "the bar reads 0.6 while hp is 0.3". Also used for the snapshot
an iteration records: commit, build hash, active asset revisions, tunables, last
check result.

**Playtest**
You playing the build while talking out loud, recorded. `playtest_start`
captures the game window and your microphone; `playtest_stop` transcribes it,
splits it per sentence, classifies each remark, routes it to a seat, pulls the
video frame at that moment, and joins it to the game's telemetry on one wall
clock, so "the jump feels floaty" arrives next to `jump {air_time: 0.94}`.
Agents cannot watch the video; the brief is what they read. Items land as `new`
until you promote them.

**Iteration**
One goal-to-outcome cycle with its causal chain recorded: what you were trying
to do, the source and build it started from, the assets and decisions involved,
the playtest evidence, and what the build became. `iteration_status` returns the
history.

**Doctor**
`bgate doctor` probes twelve dependencies (`python`, `art_key`, `local_image`,
`local_runtimes`, `agent_cli`, `ffmpeg`, `ffprobe`, `blender`, `godot`,
`godot_web_templates`, `whisper`, `imageto3d`) and reports
`{available, path, version, min_required, reason}` for each. It never opens the
microphone, launches an engine, or spends money; the key check is presence only.
It exits 1 if **anything** is missing, which is right for a CI step and alarming
for a human, so read the rows and not the exit code. The `art_key` row asks the
provider registry, so either `OPENAI_API_KEY` or `KREA_API_KEY` turns it green.

## Removed

**Scope tier / the cut line**
Ranked bands of ambition in the bible with a line drawn through them, below
which nothing was to be built. It never refused an item in the product's life.
Removed 2026-08-10, along with `scope_check`, the three World panels and
`work_item.scope_tier_id`. See [design-notes.md](design-notes.md). Deciding what
you are not building is still the director's job, written in the bible like
every other decision.
