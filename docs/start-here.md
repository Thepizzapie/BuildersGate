# Start here

2026-07-27. For someone who has never run an MCP server and has never had more
than one AI agent working on anything.

If you already know what MCP is, read the [README](../README.md) instead.

There is a [glossary](glossary.md) for every term below.

---

## The problem this solves

Claude writes GDScript fine. These are the things that go wrong after that, and
what this does about each one.

| Problem | What this does |
|---|---|
| A new session knows nothing about your game. You re-explain the premise, the art style and last week's decisions every time. | Design decisions live in a database in the project, not in the chat. Any session reads the same bible and lore. |
| You ask for a save system and get a save system, an achievement framework and a settings menu. | Work is tagged with a scope tier and there is a cut line. Anything below it is refused when queued, not argued about later. |
| It writes a jump, reports the jump works, and has never run the game. | `godot_run` runs the project headless and `godot_screenshot` captures a frame. A seat can look at what it built. |
| Sprite frame one and frame four are different characters. | References are pinned as files. Generation conditions on them, and a consistency check scores frames against the pinned anchor before anything ships. |
| Two sessions edit the same file for opposite reasons and one silently loses. | Seats take write locks on files. A PreToolUse hook refuses the second writer. |
| Image generation quietly costs more than you expected. | Per-project and daily spend ceilings, enforced before the call, not reported after. |

Everything above is one of four things: a database, some gates, a way to run the
game and look at it, and a discipline for generating art that stays on-model.

This is more machinery than a small project needs.

---

## What an MCP server is

MCP, the Model Context Protocol, is a standard for giving an AI assistant tools
that live outside itself. Normally Claude Code can read files, edit files, and
run shell commands. That is its whole vocabulary. An MCP server adds verbs.

You register one, once. From then on, every Claude session on your machine can
call the tools it exposes, the same way it calls "read a file". A "custom MCP"
is nothing more exotic than a server somebody wrote for their own domain instead
of using an off-the-shelf one.

Builders Gate is one of those, for game development. It exposes some eighty
tools: `godot_run`, `image_sprites`, `bible_add`, `asset_lock`,
`playtest_start`, and so on. It runs on your machine as a local process talking
over stdin and stdout. There is no network service, no account, no cloud. Each
game gets one SQLite file at `.bgate/game.db` that travels with the repo.

Concretely: instead of you pasting a design decision into chat for the fortieth
time, an agent calls `seat_brief("art")` and gets the mission, the design bible,
the approved references, and who currently holds which files, in one call.

---

## The vocabulary, defined once

Six words that mean something specific here. The [glossary](glossary.md) has
the rest.

**Seat.** A fixed job title an agent adopts for a session. There are seven and
there will always be seven: director, narrative, gameplay, tech, art, audio, qa.
A seat is not a process you start. It is an identity. A session sets
`BGATE_SEAT=art` and inherits art's mission and its writable paths. You do not
create agents. You hand out seats.

**Lane.** The file paths a seat may write, as glob patterns. Art's are
`game/assets/**`, `blender/**`, `art/**`. Gameplay's are `game/scripts/**` and
`game/scenes/**`. This is the answer to "won't they stomp each other". A write
outside your lane is refused, and unknown seats fail closed.

**Lock.** A claim on one binary file. Text merges; a `.blend` does not. An agent
calls `asset_lock` before editing a binary and `asset_release` after. A lock
that is already held **errors** rather than queueing, so the second agent gets
told to go do something else instead of quietly waiting.

**Work item.** One unit of queued work: a seat, a title, a brief, a status. It
is a database row. Filing one costs nothing and starts nothing.

**Dispatch.** Turning a queued work item into a *running agent*. This is where
money gets spent, and it is where the refusals live. See below.

**The cut line.** A marker in your design bible dividing what you are building
from what you are not. Everything at or below it is explicitly not being built.
This is the single most useful thing in the tool and the one people skip.

---

## What actually happens when you dispatch

Worth reading before you run anything, because "dispatch" sounds abstract and is
not. When you click Dispatch on a work item, `bgate_ui/dispatch.py` does this:

1. Re-checks the **cut line** against the item's scope tier. The line moves; an
   item filed legitimately on Tuesday can be out of scope by Thursday, and
   spending an agent on it is the exact gold-plating tiers exist to stop.
2. Checks the **concurrency cap** (default 4). The dashboard's "dispatch all"
   loops every queued item with no cap of its own, and twenty queued items would
   otherwise be twenty Claude trees on one laptop.
3. Checks the **spend ceilings**: per item, per day, per project.
4. Refuses a **dirty git tree** unless you insist. A run started on top of your
   uncommitted work produces a diff that cannot tell the agent's edits from
   yours.
5. Captures the current commit, so the run is readable as a diff afterwards and
   undoable with a scoped revert. (A per-item git worktree is available behind
   `BGATE_GIT_ISOLATION=1`; it is off by default because moving the agent's
   working directory is a bigger change to a run than most projects want.)
6. Spawns an actual `claude` process, with `BGATE_SEAT`, `BGATE_ROOT`,
   `BGATE_WORK_ITEM`, and `BGATE_ACTOR=agent:item-<id>` in its environment. That
   last one is what makes "approved" mean anything: without it a spawned agent
   inherits your identity and can approve its own work. It did, until that line
   was added.
7. Starts a watchdog that kills the process if it passes its runtime or cost
   ceiling, and marks the item failed with the reason.

Then you watch it in the dashboard and can type at it mid-run.

An important non-obvious rule: **exit 0 is not success.** A session that exits
cleanly without a terminal result event is marked failed, because that is what a
killed or wedged agent looks like from outside.

---

## First session

### 0. What you need

Python 3.11+, Godot 4.x, and an MCP client. Claude Code is what this is
developed against. Windows is the supported platform; Linux is best-effort and
macOS is untested. Blender is optional and only matters for the 3D leg. An image
API key (OpenAI or Krea) is optional and only matters for generated art.

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .
bgate doctor
```

`bgate doctor` exits 1 if *anything* on its list of eight is missing. That is
right for a CI step and alarming for a human: you only need Python and Godot for
the core loop. Read the rows, not the exit code. It also probes `OPENAI_API_KEY`
only, so a Krea-only setup will report `MISS openai_key` and exit 1 while
working perfectly.

### 1. Make a project

```bash
bgate init emberfall --kind 2d
cd emberfall
```

This creates a **new directory** named after the project, not whatever directory
you were standing in, containing `.bgate/game.db` and a runnable Godot game. It prints the absolute path it wrote to. `--kind 3d` gives you a
first-person slice instead; read the 3D answer
before you commit to that path.

If you already have a Godot game, you do not want `init`. See
pointing it at an existing game.

Open the game and press F1 while it runs. Every `@export` in the current scene
gets a slider bound to the live node, and moving it moves the game. No apply
button, because the point is to feel the change while you make it. Values
persist and are re-applied at boot.

### 2. Turn on the dashboard

```bash
bgate serve          # http://127.0.0.1:7788
```

Ten views over the same database: the queue, live agents you can steer, the seat
workspaces, the world bible, assets, playtests, the iteration timeline, and
**Settings**. No build step, no node, no CDN.

Mutations require a per-project bearer token from `.bgate/ui-token`, because
127.0.0.1 is not a security boundary. Any page in your browser can POST to
localhost.

**The bell** in the header counts what has happened since you last looked —
agents finishing, work parked for your approval, a chain that has stopped moving,
a question the director wants answered. It reads the same event log the follow-up
router does. It can only tell you things while the page is open, though, so if you
walk away: `bgate app` puts the unread count in the window title, and one optional
webhook (Settings → Notifications) is the only channel that leaves the machine.

**Settings** is every switch in one place, grouped, each row saying whether its
value is the default, something you stored, or an environment variable overriding
both — so a shell profile can never quietly disagree with what the panel shows.
The two that decide how much runs without you are `autopilot` (does work START
without you) and the approval gate (does it FINISH without you).

### 3. Register the server and install the hook

```bash
claude mcp add builders-gate --scope user -- <absolute-python-path> -m bgate_mcp.server
bgate hook-install .
bgate hook-status .
```

Use the **absolute** python path. The Claude CLI's health check resolves a bare
`python` differently than your shell does, and reports "failed to connect"
against a server that runs fine.

`hook-install` writes a PreToolUse hook into `.claude/settings.json` that asks
`seat_can_write` before every Bash, Write, or Edit, and blocks out-of-lane or
lock-violating writes. It is **inert unless a session sets `BGATE_SEAT`**, and it
fails open on anything unexpected, because a crashing hook must never dam a
session.
`hook-status` is the only thing that proves enforcement is actually live; it
exits 1 if it is not.

### 4. Draw the cut line before you build anything

This is the step everyone skips and the one that pays for itself fastest. In a
Claude session, or in the dashboard's World Bible view:

- Write your pillars: the three or four things the game is actually about.
- Write your scope tiers, ranked. "Core loop", "first vertical slice", "polish",
  "post-launch dreams".
- Put the **cut line** between two of them.

From that moment on, `queue_add` refuses to file work under a cut tier, and
dispatch re-checks before spawning. This is the only mechanism in the tool that
reliably stops an agent fleet building things nobody asked for.

Untiered work is deliberately let through and loudly flagged. Refusing it would
make the first cut line anyone draws reject their entire existing queue, and the
predictable next step would be turning the gate off, which is how a gate stops
gating.

### 5. Do one thing, end to end

Pick something small. File it as a work item against `gameplay`. Dispatch it.
Watch it in the Agents view. When it says it is done, look at the diff.

The loop the tool is built around, once you are past the first hour:

```text
DIRECTOR    bible_add: pillars, the core loop, the scope tiers, the cut line
NARRATIVE   lore_add / lore_fact; canon_check gates every narrative write
ART         ref_pin an approved reference FIRST, then generate against it;
            asset_lock before touching a binary, asset_release after
GAMEPLAY    writes GDScript in its lanes; godot_check_project + godot_run
            after every change; godot_screenshot to SEE what it did
QA          headless test scripts via godot_run; asset_verify for drift
YOU         play the build, talk out loud, promote what becomes work
```

### 6. Play it yourself

```text
playtest_check    preflight: ffmpeg, mic signal, transcriber, target window
playtest_start    records the game window and your voice
   ...play, and say what you like and what needs fixing...
playtest_stop     transcribes, splits per sentence, classifies, routes, aligns
playtest_brief    what the agents read
playtest_promote  YOU decide what becomes work
```

Agents cannot watch video. The mp4 is for you. What they get is the transcript
plus the frame pulled at each remark plus the game's own telemetry joined on one
clock, so "the jump feels floaty" arrives sitting next to
`jump {air_time: 0.94}`. That join is what turns a vibe into a number an agent
can act on.

Items land as `new` and stay there until you promote them. Thinking out loud
mid-play is not a decision to build.

This requires ffmpeg and a microphone. If you have neither, skip it; everything
else works without it.

---

## Stopping agents — the kill switch

Agents are real processes spending real money, so there is one control that
stops all of them and it is never more than one click or one command away.

**From the dashboard:** the red `stop all` button in the Agents console, top
right of the graph.

**From a terminal**, which is what you want when the dashboard itself is wedged
or was never running:

```bash
bgate panic
```

Either one does the same four things, in this order, because any other order
leaves a gap something restarts through:

1. **auto-deploy off first** — killing agents while the loop is on just
   dispatches a replacement into the gap;
2. every live agent killed **by process tree**, not just the `claude` parent
   (its MCP children hold the pipe open and outlive it otherwise);
3. every pid in the project's ledger reaped, including ones a *previous*
   dashboard spawned — the ledger is on disk and outlives the process that
   wrote it, which is exactly why orphans happen;
4. anything still marked `dispatched` settled, so the board stops claiming work
   is running the moment this returns.

Interrupted items are marked **stopped**, not done: you can see what was cut
off and re-queue it deliberately.

### What stops a run without you

Nothing runs unbounded, and there are three separate backstops because each one
misses a different failure:

| Backstop | Default | What it catches |
|---|---|---|
| Cost ceiling | `$5` per item | a run that keeps paying to go nowhere |
| Runtime ceiling | 30 min (`max_runtime_s`) | a run that never finishes |
| Hard runtime cap | 2 h (`BGATE_MAX_RUNTIME_S`) | a budget with its runtime set to 0 |
| Stall timeout | 25 min (`BGATE_STALL_S`) | a **hung** session: alive, silent, holding a slot |
| Concurrency cap | 4 agents (`max_concurrent`) | the whole fleet at once |

Silence is measured against real output — the log *and* files under
`.bgate_out/` and the game's assets — because a 30-minute atomic image batch
writes nothing until it returns, and killing those was how healthy agents used
to die. A session that has produced nothing at all for 25 minutes is wedged, not
working.

The ceilings live in the project's budget (`/api/spend`); the two environment
variables are escape hatches for a machine that needs different numbers.

## Inventory before you plan

The stamped `CLAUDE.md` names the calls; this is why they are worth the tokens.

Measured across a week of real builds, the single most expensive habit was not
running them. Each is one call and each returns a *list* rather than a verdict:

| call | what it enumerates |
|---|---|
| `project_status`, `queue_list`, `bible_read`, `seat_list` | the board and the design |
| `bgate_doctor` | the toolchain — **an inventory, not a pass/fail gate** |
| `image_status`, `blender_status` | providers, models and backends the doctor only summarises |
| `pending_decisions` | what is already blocked on a human |
| `seat_notes`, `handoff_read` | what earlier sessions and sibling agents know |
| `ref_list` | what is already pinned, before generating anything |

Three failures this prevents, all of them from the same session:

- **A green row is a capability, not a tick.** `imageto3d: {available: true}`
  was read as "that check passed" and the backend went unused for a week. If you
  decline a capability, say so as a decision.
- **`usable` means configured, not running, and not local-only.** A local
  backend is listed usable with no server up; a hosted one needs only its key. An
  agent tried the two local image-to-3D backends, got connection refused, and
  reported the path unavailable — the hosted one worked and had already produced
  every texture in that build. Check `hosted` before reporting anything down.
- **A subagent's "X was not available" deserves the same scepticism as its "X
  worked."** Check which variants it actually tried before repeating that upward.

The board is live while you read it: closing a chain link releases the next one
to auto-deploy even when the session-start banner said nothing was queued, so
re-read `queue_list` after every `queue_complete`, and treat a file you did not
write as evidence of a concurrent writer rather than a curiosity.

## Two things that will bite you

**Approval is human-only, on purpose.** An agent records a verdict; it does not
sign off. An art agent that judged its own frames approved off-style drift three
times, and a second agent doing the judging is the same failure with an extra
hop. So a QA reviewer can *fail* a candidate outright, because refusing to ship
is a call a machine can make alone. A pass only records evidence and leaves the
revision waiting for a person. If you never look at anything, nothing lands.

**This is a solo project that has built real games on one machine, and it shows
in both directions.** The [README's status section](../README.md#project-status)
says plainly what works, what is half-built, and what is a design note with no
runtime code. Read it before you plan a weekend around any part of this.

---

## Where to go next

- [Glossary](glossary.md): every term, one or two sentences each.
- [setup.md](setup.md): setup in full, including `bgate adopt` and API keys.
- [reference.md](reference.md): every surface in detail.
- character-consistency.md: the measurements behind
  the art discipline, including the ones that failed.
- gap-analysis.md: where this pipeline is weakest, written
  after a real production run, with what each gap cost.
- [README](../README.md): what it is, status, and install.
