# Setup in full

The [README](../README.md) has the short version. This page is the full
sequence: install, point it at a game, set keys, register the MCP server, verify.

## 1. Requirements

| Thing | Needed for | Notes |
|---|---|---|
| Python 3.11+ | everything | `requires-python = ">=3.11"`; `pip install -e .` pulls mcp, fastapi, uvicorn, Pillow, openai |
| An MCP client | agents | [Claude Code](https://claude.com/claude-code) is what this is developed against |
| [Godot 4.x](https://godotengine.org) | the core loop | Portable exe is fine. Discovery checks common install dirs, or set `BGATE_GODOT` |
| Godot Web export templates | `bgate publish` | A separate ~1 GB download from inside the editor |
| [Blender 4.2+](https://blender.org) | the 3D leg, optional | Or set `BGATE_BLENDER` |
| `ffmpeg` + `ffprobe` | video, optional | Playtest capture, frame extraction, cutscene encoding. See [gotchas.md](gotchas.md) before you trust the build you have |
| `faster-whisper` + `sounddevice` | playtest transcription, optional | `pip install -e ".[stt,record]"` |
| `pywebview` | `bgate app`, optional | `pip install -e ".[desktop]"` |

**Windows is the supported platform.** Linux is best-effort: CI runs the suite
there but marks it `continue-on-error`, and parts of the product shell out to
Windows tooling (`taskkill`, `tasklist`). macOS is untested. Reports are welcome,
see [CONTRIBUTING.md](../CONTRIBUTING.md).

## 2. Install

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .
```

## 3. Point it at a game

**If you already have a Godot project, adopt it.** `bgate init` scaffolds into a
NEW directory and will not help you here.

```bash
cd my-existing-game
bgate adopt --pitch "what this game is"    # or: bgate adopt path/to/game
```

Adoption is additive. It never copies a template file and never rewrites a byte
you wrote. It:

- creates `.bgate/game.db`
- merges the API-key ignore rules into your `.gitignore`, inside a marked block
- appends a `CLAUDE.md` briefing the same way
- re-roots the seat lanes if your game does not live at `<root>/game`
- prints what it detected: Godot version, main scene, 2D vs 3D, and scene,
  script and asset counts

Read that printout. If the dimension is wrong, pass `--kind 2d|3d|2d+3d`.
Re-running refreshes the marked blocks in place instead of stacking a copy.

**If you are starting from nothing:**

```bash
bgate init emberfall --kind 2d
```

This creates a new directory named after the project, under the directory you
are standing in. It does not scaffold into the current directory.

To undo an adoption: `bgate un-adopt <dir> --yes` deletes `.bgate/` and leaves
the game files alone.

## 4. Keys

Three art providers. The art seat needs at least one. Which one a given job goes
to is a routing rule, not a preference: see [reference.md](reference.md).

```bash
bgate key set openai --global    # stored once, every project inherits it
bgate key                        # what is set, and which layer supplies it
bgate key clear openai --global  # forget it again
```

The key is prompted for with echo off. There is no argument that takes one, so
it never lands in shell history or a process list.

| Variable | Provider | What it buys |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI `gpt-image` | Portraits, UI, backdrops, reference-first sprite sets. Prices by quality tier. |
| `KREA_API_KEY` | [Krea](https://krea.ai) | A catalogue of 14 models (Flux, Imagen, Nano Banana, Krea-2) plus image-to-3D, behind one key. Character work routes here by default. Prices per model and per payload, so attaching references changes the price. |
| `KIE_API_KEY` | [kie.ai](https://kie.ai/api-key) | Nano Banana / FLUX.2 / Qwen images, Suno music and Seedance video. The only key here that generates audio or video. No 3D. Billed in credits with no published per-model price; set `BGATE_KIE_USD_PER_CREDIT` to your account's rate for dollar ledger rows. |

Two more credentials exist and are not art: `DEEPGRAM_API_KEY` (talking to an
agent) and `TWITCH_OAUTH_TOKEN` (reading stream chat). Neither turns the
`art_key` doctor row green.

No provider returns usable transparency. Measured: `background="transparent"`
came back as a brown gradient. Sprite work goes through the chroma-key path in
`bgate_core/chroma.py` either way.

### Where a key can live, and which one wins

Three layers, most specific first. The highest one that is set is what gets
sent:

| Layer | Where | Use it when |
|---|---|---|
| Shell variable | your own environment | CI, or a one-off override for one session |
| Project `.env` | `<game>/.env` | this game bills a different account from your others |
| Machine-wide `.env` | `~/.bgate/.env` (or `$BGATE_HOME/.env`) | the usual case: a personal machine, one set of keys |

`bgate key` prints the layer in force per provider. Ask it when a key is set and
nothing works. Clearing a project key uncovers the machine-wide one rather than
leaving the provider unset.

The machine-wide store is the only one that exists when you are not in a
project, so `bgate doctor` and `image_status` answer correctly with no game in
sight. Copying [`.env.example`](../.env.example) to a project `.env` by hand
still works.

`.env` and `.env.*` are gitignored here and in every project `init` and `adopt`
stamp out. They were not, for a while, which is how following these instructions
committed a key once. `~/.bgate` is not a repository, and it is written `0600`
where the OS supports it. Keys are never logged.

The dashboard writes keys too: **Settings → Art providers**, or the
**Generators** tab on Studio. Both have a *save for every project on this
machine* tick box, write the ignore rule before writing a project `.env`, and
make the key live without a restart. No MCP tool sets a key. An agent that can
write credentials can hand itself a provider nobody paid for.

### Using a tool with no project open

The image tools (`image_generate`, `image_edit`, `image_sprites`,
`image_talkhead`) fall back to a **scratch project** at `~/.bgate/scratch` when
there is nowhere else to put the result, created on first use.
`project_dir="scratch"` asks for it explicitly from inside a project you would
rather not touch. It is a real project because everything downstream of a
generation needs one: the artifact registry, the spend ledger, `.bgate_out`, the
review queue. It carries no game, so a tool that needs an engine still refuses.

It is the last resort, below the remembered active project. If you have ever run
`bgate init`, `bgate adopt` or `bgate use`, your own project keeps winning and a
mistyped path keeps failing loudly. Tools that edit game files, run Godot or take
locks never fall back. `project_status` says when the scratch project is in use.

## 5. Register the MCP server

```bash
python -c "import sys; print(sys.executable)"     # get the absolute path
claude mcp add builders-gate --scope user -- <abs-python> -m bgate_mcp.server
```

**Use the absolute python path.** The claude CLI resolves a bare `python`
differently than your shell and reports "failed to connect" against a server
that runs fine. On Windows this is the most common setup failure and the error
message points nowhere near the cause.

Then install the enforcement hook:

```bash
bgate hook-install --scope user      # every project on this machine, now and later
bgate hook-status <game-project>     # proves enforcement is live; exits 1 if not
```

`--scope project` (the default, taking a directory) writes into
`<project>/.claude/settings.json` instead. User scope pins the interpreter for
you; project scope writes `python -m` because that file gets committed.

The hook writes a PreToolUse entry that asks `seat_can_write` before every Bash,
Write, Edit, MultiEdit and NotebookEdit call, and blocks out-of-lane or
lock-violating writes with exit 2 plus guidance. It also writes a SessionStart
entry that preloads the board.

A session that sets `BGATE_SEAT=<role>` gets full enforcement: out of lane is
refused, and so is a file another seat holds.

A session you started yourself sets no seat. It holds the director seat, and how
hard it is checked comes from `BGATE_DIRECTOR_MODE`:

| Value | What a seatless session gets |
|---|---|
| `off` | nothing. No identity, no lease, no checks |
| `collide` | **default.** It takes path leases like any other run, and a write into a file another live run holds is blocked and names the holder. Lane violations pass |
| `warn` | as `collide`, plus lane violations reported on exit 1. The write still lands |
| `block` | the director is a seat like any other: out of lane is refused |

Outside a bgate project, or on anything unexpected, the hook stays out of the
way and fails open. A crashing hook must never dam a session, so
`hook-status` is the only thing that proves enforcement is on.

`bgate hook-uninstall [DIR] [--scope user]` removes the entry and leaves your
other hooks alone.

**Restart your MCP client before the tools appear.** A fresh session is the only
thing that picks up a new server. Confirm with `project_status`.

## 6. Verify

```bash
bgate doctor              # every dependency, one pass
bgate doctor --json
```

Twelve rows: `python`, `art_key`, `local_runtimes`, `agent_cli`, `ffmpeg`,
`ffprobe`, `blender`, `godot`, `godot_web_templates`, `whisper`, `imageto3d`,
`local_image`. Each reports `{available, path, version, min_required, reason}`.

**Read the rows, not the exit code.** It exits 1 if anything at all is
unavailable, including things nobody needs on day one. The core loop needs
`python` and `godot`, and the command prints that itself along with which of the
two are missing.

- `art_key` is green when **any** art provider has a key, from any layer. A
  Krea-only setup is fine.
- `local_runtimes` and `local_image` answer "can this machine generate without
  renting anything". Red there is not a fault on a machine that never wanted it.
- `agent_cli` catches a coding-agent CLI that is installed and registered and
  still cannot reach the tools, because the registration names an interpreter
  without Builders Gate in it.
- `ffmpeg` goes red on a build whose libtheora writes files nothing can decode.
  See [gotchas.md](gotchas.md).

Below the rows it prints project-level faults no binary probe can see (seat lanes
matching no directory here, a hook that was never installed), then the effective
settings and where each value came from. Neither block affects the exit code.
`doctor` never opens the microphone, launches an engine, or spends money.

Then start the dashboard:

```bash
bgate serve                 # http://127.0.0.1:7788, loopback only
bgate serve --port 7801
bgate app                   # the same dashboard in a native window
```

`bgate app` needs `pip install -e ".[desktop]"`. On Windows it renders through
the WebView2 runtime that ships with 11. It takes a loopback port the OS picks,
so it does not fight a `bgate serve` that is already running.

## Switching between projects

```bash
bgate projects                    # every known project, * marks the active one
bgate use emberfall               # by registered name, or by directory
```

`bgate use` writes a pointer to `~/.bgate/active.json`. That file is user-scoped
and never in the repo, so `bgate serve`, `bgate doctor` and the MCP tools pick it
up without you exporting `BGATE_ROOT` in every shell.

The pointer is the lowest-priority answer. Resolution order:

1. an explicit `project_dir=` on a tool call
2. `BGATE_ROOT`
3. standing inside a project directory
4. the pointer

`project_select` is deprecated and switches nothing. Pass `project_dir=` or run
`bgate use`.

## CLAUDE.md

Both `init` and `adopt` stamp a `CLAUDE.md` into the project. That file is the
instructions for the Claude Code session working in *your game*: what a seat is,
how a work item is created and closed, what the bible and lore are for, the art
pipeline, and what not to do. Read it next.
