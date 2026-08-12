# Start here

For someone who has never run an MCP server and has never had more than one AI
agent working on anything. If you already know what MCP is, read the
[README](../README.md) instead.

Every term below is defined in the [glossary](glossary.md).

## What this is

An MCP server is a program that gives an AI assistant extra tools. Claude Code
can read files, edit files and run shell commands; an MCP server adds verbs.
You register one once, and every Claude session on your machine can call it.

Builders Gate is one of those, for Godot game development. It runs as a local
process over stdin and stdout: no network service, no account, no cloud. Close
to 200 tools, including `godot_run`, `image_sprites`, `bible_add`, `asset_lock`
and `playtest_start`. Each game gets one SQLite file at `.bgate/game.db` that
travels with the repo.

What that buys you:

| Problem | What this does |
|---|---|
| Every new session re-learns your premise, art style and last week's decisions. | Design decisions live in the project database. Any session reads the same bible and lore. |
| You ask for a save system and get three other features with it. | Work is filed as items with a written brief and an acceptance test. Widening the job means filing a new item, where you can see it. |
| An agent reports the jump works and has never run the game. | `godot_run` runs the project headless, `godot_screenshot` captures a frame. |
| Sprite frame one and frame four are different characters. | References are pinned as files. Generation conditions on them, and `consistency_check` scores frames against the pinned anchor. |
| Two sessions edit one file and one silently loses. | Seats hold write locks. A PreToolUse hook refuses the second writer. |
| Image generation costs more than you expected. | Per-item, per-day and per-project spend ceilings, enforced before the call. |

This is more machinery than a small project needs.

## The vocabulary

| Term | Meaning |
|---|---|
| **Seat** | A fixed job title an agent adopts for a session. Eight of them: director, narrative, gameplay, tech, art, audio, cinematic, qa. A seat is an identity, not a process: a session sets `BGATE_SEAT=art` and inherits art's mission and writable paths. |
| **Lane** | The glob patterns a seat may write. Art's are `game/assets/**`, `blender/**`, `art/**`; gameplay's are `game/scripts/**`, `game/scenes/**`. Writes outside your lane are refused, and unknown seats fail closed. |
| **Lock** | A claim on one binary file. Text merges, a `.blend` does not. `asset_lock` before editing, `asset_release` after. A held lock errors instead of queueing, so the second agent goes and does something else. |
| **Work item** | One unit of queued work: seat, title, brief, status. A database row. Filing one costs nothing and starts nothing. |
| **Dispatch** | Turning a queued item into a running agent. This is where money gets spent and where the refusals live. |
| **Chain** | Items filed as one ordered group with `queue_add_chain`, each waiting on the last. Nothing dispatches ahead of its predecessor. |

## First session

### 0. What you need

Python 3.11+, Godot 4.x, and Claude Code. Windows is the supported platform,
Linux is best-effort, macOS is untested. Blender (3D), ffmpeg (playtest
capture), whisper (voice) and an image API key are all optional.

Check that Python is on your `PATH` with `python --version` before you start.

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .
bgate doctor
```

If `bgate` is "not recognized" on Windows, pip put the shortcut in Python's
`Scripts\` directory and that directory is not on your `PATH`. Either call
every command the long way (`python -m bgate_cli.main doctor`) or re-run the
Python installer, choose Modify, tick **Add Python to PATH**, and open a new
terminal.

### Reading `bgate doctor`

It exits 1 if anything on its list is missing, and most of the list is
optional. Read the rows, not the exit code.

| Row | Needed for |
|---|---|
| `python`, `godot` | **Required.** Making, building and running a project |
| `blender` | The 3D leg |
| `ffmpeg`, `ffprobe` | Playtest capture, cutscene transcoding |
| `whisper` | Voice transcription while you play |
| `art_key`, `local_image` | Image generation, rented or local |
| `imageto3d`, `local_runtimes`, `agent_cli`, `godot_web_templates` | Capability inventory |

If `python` and `godot` are green, move on. The `art_key` row covers every art
provider, so either `OPENAI_API_KEY` or `KREA_API_KEY` turns it green. Set
either from the dashboard's Generators panel (Studio, Generators).

### 1. Make a project

```bash
bgate init emberfall --kind 2d
cd emberfall
```

This creates a **new directory** named after the project, not whatever
directory you are standing in, containing `.bgate/game.db` and a runnable Godot
game. It prints the absolute path it wrote to. `--kind 3d` gives you a
first-person slice instead.

If you already have a Godot game, use `bgate adopt` instead. It never
scaffolds and never overwrites. See [setup.md](setup.md).

Run the game and press F1. Every `@export` in the current scene gets a slider
bound to the live node, and moving it moves the game. Values persist and are
re-applied at boot.

### 2. Turn on the dashboard

```bash
bgate serve          # http://127.0.0.1:7788
```

Views over the same database: the queue, live agents you can steer, seat
workspaces, the world bible, assets, playtests, and Settings.

Mutations require a per-project bearer token from `.bgate/ui-token`. 127.0.0.1
is not a security boundary; any page in your browser can POST to localhost.

The bell in the header counts what has happened since you last looked. It only
works while the page is open, so if you walk away, `bgate app` puts the unread
count in a window title, and a webhook (Settings, Notifications) is the only
channel that leaves the machine.

Settings holds every switch, each row saying whether its value is the default,
something you stored, or an environment variable overriding both. Two of them
decide how much runs without you: `autopilot` (does work start without you) and
the approval gate (does it finish without you).

### 3. Register the server and install the hook

```bash
python -c "import sys; print(sys.executable)"    # prints <absolute-python-path>
claude mcp add builders-gate --scope user -- <absolute-python-path> -m bgate_mcp.server
bgate hook-install .
bgate hook-status .
```

Use the **absolute** python path, from the same environment where
`pip install -e .` ran. The Claude CLI resolves a bare `python` differently
than your shell does and reports "failed to connect" for a server that runs
fine. This is the most common failure on Windows.

**Restart Claude Code before you look for the tools.** A running session does
not pick up a newly registered server. Confirm by asking Claude to call
`project_status`; if that tool is not in its list, the server is not connected.

`hook-install` writes a PreToolUse hook into `.claude/settings.json` that calls
`seat_can_write` before every Bash, Write or Edit and blocks out-of-lane or
lock-violating writes. It is inert unless a session sets `BGATE_SEAT`, and it
fails open on anything unexpected. `hook-status` proves enforcement is live and
exits 1 if it is not.

### 4. Write the bible before you build anything

In a Claude session, or in the dashboard's World Bible view, write down:

- Your **pillars**: the three or four things the game is about.
- The **core loop**: what the player does over and over.
- Your **constraints**: art rules, platform limits, settled decisions.
- What you are **not** building. An unsaid no gets built.

Every seat reads this in its brief, and the art constraints are assembled into
the image prompts.

### 5. Do one thing, end to end

File a small work item, dispatch it from the dashboard, watch it in the Agents
view, then read the diff. Ask Claude to file this, or type it into the queue
form:

- **Seat:** `gameplay`
- **Title:** Add a double jump
- **Brief:** The player can jump once more while airborne, and the second jump
  is weaker than the first. It resets on landing, not on a timer. Acceptance:
  `godot_run` starts clean, and a `godot_screenshot` shows the player above the
  height a single jump reaches.

A brief that names what "done" looks like is the difference between one round
and four. Filing files a database row and starts nothing: the dashboard is what
dispatches it, so `bgate serve` has to be running or the item just sits there
looking like work in flight.

### 6. Play it yourself

```text
playtest_check    preflight: ffmpeg, mic signal, transcriber, target window
playtest_start    records the game window and your voice
   ...play, and say what you like and what needs fixing...
playtest_stop     transcribes, splits per sentence, classifies, routes, aligns
playtest_brief    what the agents read
playtest_promote  you decide what becomes work
```

Agents cannot watch video. What they get is the transcript, plus the frame
pulled at each remark, plus the game's own telemetry joined on one clock, so
"the jump feels floaty" arrives next to `jump {air_time: 0.94}`.

Items land as `new` and stay there until you promote them. This needs ffmpeg
and a microphone; skip it if you have neither.

## The working loop

Once you are past the first hour, a day looks like this:

1. Call `project_status`, `queue_list`, `bible_read`, `seat_list` and
   `pending_decisions` before planning anything. Add `seat_notes` and
   `handoff_read` for what earlier sessions know, `ref_list` before generating
   art, and `image_status` or `blender_status` for the backends `bgate_doctor`
   only summarises.
2. Split the work. Dependent pieces go in one `queue_add_chain`, not separate
   `queue_add` calls with different priorities.
3. Dispatch from the dashboard and watch the Agents view. You can type at a
   running agent.
4. Re-read `queue_list` after every `queue_complete`. Closing a chain link
   releases the next one, so the board moves while you are reading it.
5. Read the diff. Approve or reject in the dashboard.
6. Play the build, record it, and promote what should become work.

What each seat does in that loop:

```text
DIRECTOR    bible_add: pillars, the core loop, constraints, references
NARRATIVE   lore_add / lore_fact; canon_check gates every narrative write
ART         ref_pin an approved reference FIRST, then generate against it;
            asset_lock before touching a binary, asset_release after
GAMEPLAY    writes GDScript in its lanes; godot_check_project + godot_run
            after every change; godot_screenshot to see what it did
QA          headless test scripts via godot_run; asset_verify for drift
YOU         play the build, talk out loud, promote what becomes work
```

Two habits that cost the most when skipped:

- **A green doctor row is a capability, not a tick.** If you decline a
  capability, say so as a decision.
- **`usable` means configured, not running.** A local backend lists as usable
  with no server up. Check `hosted` before reporting a path unavailable.

## What dispatch does

When you dispatch a work item, `bgate_ui/dispatch.py`:

1. Refuses a chain link whose predecessor has not landed.
2. Checks the concurrency cap (default 4).
3. Checks the spend ceilings: per item, per day, per project.
4. Refuses a dirty git tree unless you insist.
5. Captures the current commit, so the run reads as a diff afterwards. A
   per-item git worktree is available behind `BGATE_GIT_ISOLATION=1`, off by
   default.
6. Spawns a `claude` process with `BGATE_SEAT`, `BGATE_ROOT`,
   `BGATE_WORK_ITEM` and `BGATE_ACTOR=agent:item-<id>` in its environment. That
   last one is what stops a spawned agent inheriting your identity and
   approving its own work.
7. Starts a watchdog that kills the process if it passes its runtime or cost
   ceiling, and marks the item failed with the reason.

**Exit 0 is not success.** A session that exits cleanly without a terminal
result event is marked failed, because that is what a killed or wedged agent
looks like from outside.

## Stopping agents

One control stops everything, from the dashboard's red `stop all` button in the
Agents console, or from a terminal when the dashboard is wedged:

```bash
bgate panic
```

Both do the same four things in this order:

1. Auto-deploy off first, or killing agents just dispatches replacements.
2. Every live agent killed by process tree, not just the `claude` parent. Its
   MCP children hold the pipe open and outlive it otherwise.
3. Every pid in the project's ledger reaped, including ones a previous
   dashboard spawned.
4. Anything still marked `dispatched` settled, so the board stops claiming work
   is running.

Interrupted items are marked **stopped**, not done. You can see what was cut
off and re-queue it.

### What stops a run without you

| Backstop | Default | What it catches |
|---|---|---|
| Cost ceiling | `$5` per item | a run that keeps paying to go nowhere |
| Runtime ceiling | 30 min (`max_runtime_s`) | a run that never finishes |
| Hard runtime cap | 2 h (`BGATE_MAX_RUNTIME_S`) | a budget with its runtime set to 0 |
| Stall timeout | 25 min (`BGATE_STALL_S`) | a hung session: alive, silent, holding a slot |
| Concurrency cap | 4 agents (`max_concurrent`) | the whole fleet at once |

Silence is measured against real output, the log plus files under `.bgate_out/`
and the game's assets, because a 30-minute image batch writes nothing until it
returns. The ceilings live in the project's budget; the environment variables
are escape hatches.

## Two things that will bite you

**Approval is human-only.** An agent records a verdict, it does not sign off. A
QA reviewer can *fail* a candidate outright, because refusing to ship is a call
a machine can make alone. A pass records evidence and leaves the revision
waiting for a person. If you never look at anything, nothing lands.

**This is a solo project that has built real games on one machine.** The
[README's status section](../README.md#project-status) says plainly what works,
what is half-built, and what is a design note with no runtime code. Read it before you plan a weekend around
any part of this.

## Where to go next

- [Glossary](glossary.md): every term, a sentence or two each.
- [setup.md](setup.md): setup in full, including `bgate adopt` and API keys.
- [reference.md](reference.md): every surface in detail.
- [gotchas.md](gotchas.md): what goes wrong and what to do about it.
- [design-notes.md](design-notes.md): features that were measured and removed.
- [README](../README.md): what it is, status, and install.
