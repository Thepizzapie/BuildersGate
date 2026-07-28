# Start here

2026-07-27. For someone who has never run an MCP server and has never had more
than one AI agent working on anything.

If you already know what MCP is and just want the reference, the
[README](../README.md) is the reference and this page will bore you. If you have
a Godot project and a Claude subscription and a vague sense that other people
are getting more out of this than you are, this is the right page.

There is a [glossary](glossary.md) for every term below, and an [FAQ](faq.md)
that answers the questions people actually asked, including how the character
art was made and whether any of this works for 3D.

---

## The problem this solves

You can already ask Claude to write GDScript, and it will. The trouble starts
one step later, and it is always one of these:

- **It forgets.** Every session starts from nothing. You re-explain the game,
  re-explain the art style, re-explain what you decided last week.
- **It gold-plates.** You ask for a save system and get a save system, an
  achievement framework, and a settings menu you did not want.
- **It cannot see.** It writes a jump, says the jump works, and has never once
  looked at the game running.
- **The art drifts.** Frame one and frame four are different characters.
- **Two of them stomp each other.** The moment you run more than one session,
  they edit the same file for opposite reasons and one of them loses.
- **You cannot tell what it did.** Twenty file edits, no diff, no way back.

Builders Gate is the machinery around Claude that addresses those specifically.
It is a memory (a database per game), a set of gates that refuse work, a way to
run the game and *look* at it, and a discipline for generating art that stays
on-model. The point is not that agents write code. They already do that. The
point is that this thing **refuses**: out-of-scope work, a spend ceiling, a
locked file, an agent trying to approve its own art.

It is also more machinery than a small project needs. If you want a chat that
writes a design document, close this tab.

---

## What an MCP server is

MCP, the Model Context Protocol, is a standard for giving an AI assistant tools
that live outside itself. Normally Claude Code can read files, edit files, and
run shell commands. That is its whole vocabulary. An MCP server adds verbs.

You register one, once. From then on, every Claude session on your machine can
call the tools it exposes, the same way it calls "read a file". A "custom MCP"
is nothing more exotic than a server somebody wrote for their own domain instead
of using an off-the-shelf one.

Builders Gate is one of those, for game development. It exposes 78 tools:
`godot_run`, `image_sprites`, `bible_add`, `asset_lock`, `playtest_start`, and
so on. It runs on your machine as a local process talking
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
API key (OpenAI or Krea) is optional and only matters for generated art; see
[costs in the FAQ](faq.md#what-does-this-actually-cost).

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
first-person slice instead; read [the 3D answer](faq.md#will-this-work-for-a-3d-game)
before you commit to that path.

If you already have a Godot game, you do not want `init`. See
[pointing it at an existing game](faq.md#can-i-point-it-at-my-existing-game-and-ask-whats-missing).

Open the game and press F1 while it runs. Every `@export` in the current scene
gets a slider bound to the live node, and moving it moves the game. No apply
button, because the point is to feel the change while you make it. Values
persist and are re-applied at boot.

### 2. Turn on the dashboard

```bash
bgate serve          # http://127.0.0.1:7788
```

Nine views over the same database: the queue, live agents you can steer, the
seat workspaces, the world bible, assets, playtests, and the iteration timeline.
No build step, no node, no CDN.

Mutations require a per-project bearer token from `.bgate/ui-token`, because
127.0.0.1 is not a security boundary. Any page in your browser can POST to
localhost.

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

- [FAQ](faq.md): the character art process, 3D, cost, speed, existing projects.
- [Glossary](glossary.md): every term, one or two sentences each.
- [setup.md](setup.md): setup in full, including `bgate adopt` and API keys.
- [reference.md](reference.md): every surface in detail.
- [character-consistency.md](character-consistency.md): the measurements behind
  the art discipline, including the ones that failed.
- [gap-analysis.md](gap-analysis.md): where this pipeline is weakest, written
  after a real production run, with what each gap cost.
- [README](../README.md): what it is, status, and install.
