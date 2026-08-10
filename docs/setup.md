# Setup in full

2026-07-27. The [README](../README.md) has the short version. This page has the
rest: API keys, adopting an existing game, switching projects, registering the
MCP server, and platform detail.

## Requirements

| Thing | Needed for | Notes |
|---|---|---|
| Python 3.11+ | everything | `pip install -e .` pulls mcp, fastapi, uvicorn, Pillow, openai |
| An MCP client | agents | [Claude Code](https://claude.com/claude-code) is what it is developed against |
| [Godot 4.x](https://godotengine.org) | the core loop | Portable exe is fine. Discovery checks common install dirs, or set `BGATE_GODOT` |
| Godot Web export templates | `bgate publish` | A separate ~1 GB download from inside the editor |
| [Blender 4.2+](https://blender.org) | the 3D leg, optional | Or set `BGATE_BLENDER` |
| `ffmpeg` + `ffprobe` on PATH | playtest capture, optional | Screen capture, frame extraction, reading a recording's duration |
| `faster-whisper` + `sounddevice` | playtest transcription, optional | `pip install -e ".[stt,record]"` |

## API keys: two image providers, either or both

The art seat needs at least one.

```bash
bgate key set openai --global    # stored once, every project inherits it
bgate key                        # what is set, and which layer supplies it
```

The key is prompted for with echo off — there is no argument that takes one, so
it never lands in shell history or a process list.

| Variable | Provider | What it buys |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI `gpt-image` | Portraits, UI, backdrops, reference-first sprite sets. Prices by quality tier. |
| `KREA_API_KEY` | [Krea](https://krea.ai) | A catalogue of 14 models (Flux, Imagen, Nano Banana, Krea-2) behind one key, with `image_style_references` as a first-class input. That input is exactly what the art seat's pinned anchors are. Prices per model, per request. |

Neither provider returns usable transparency. Measured:
`background="transparent"` came back as a brown gradient. Sprite work goes
through the chroma-key path in `bgate_core/chroma.py` either way. The module
docstring in `bgate_adapters/krea.py` has the full comparison.

### Where a key can live, and which one wins

Three layers, most specific first. Whichever is highest on this list and set is
the one actually being sent:

| Layer | Where | Use it when |
|---|---|---|
| Shell variable | your own environment | CI, or a one-off override for a single session |
| Project `.env` | `<game>/.env` | this game bills a different account from your others |
| Machine-wide `.env` | `~/.bgate/.env` (or `$BGATE_HOME/.env`) | **the usual case** — a personal machine, one set of keys |

`bgate key` prints the layer in force per provider, which is the question worth
asking when a key is set and nothing works. Clearing a project key **uncovers**
the machine-wide one rather than leaving the provider unset.

The machine-wide store is also the only one that exists when you are not in a
project, so `bgate doctor` and `image_status` answer correctly with no game in
sight. Copying [`.env.example`](../.env.example) to a project `.env` by hand
still works and is unchanged.

`.env` and `.env.*` are gitignored here and in every project `bgate init` stamps
out. They were not, for a while, which is how following these instructions
committed a key. `~/.bgate` is not a repository, so the machine-wide store has
nothing to leak into; it is written `0600` where the OS supports it. Keys are
never logged.

You can also set these from the dashboard — **Settings → Art providers**, or the
**Generators** tab on Studio. Both have a *save for every project on this
machine* tick box, add the ignore rule first when writing a project `.env`, and
make the key live without a restart. Writing a key is human-only in both the
dashboard and the CLI, and there is deliberately **no MCP tool** that sets one:
an agent that can write credentials can hand itself a provider nobody paid for.
`bgate doctor`'s `art_key` row is green when any provider has a key, from any
layer.

## Platform support

**Windows is the supported platform.** It is what everything is developed and
verified on.

**Linux is best-effort.** CI runs the suite there but marks it
`continue-on-error`. Parts of the product shell out to Windows tooling
(`taskkill`, `tasklist`). The tests that need it skip cleanly. The rest has
never been depended on there.

**macOS is untested.** Reports from Linux and macOS are welcome. See
[CONTRIBUTING.md](../CONTRIBUTING.md).

## bgate doctor

```bash
bgate doctor              # python/key/ffmpeg/ffprobe/blender/godot/whisper
bgate doctor --json
```

One pass, exits 1 if anything on the list is unavailable. That is the right
behaviour for a CI step and a slightly alarming one for a human: you only need
Python and Godot for the core loop. Read the rows, not the exit code.

It never opens the microphone, launches an engine, or spends money. Every probe
is wall-clock bounded and reports `{available, path, version, min_required,
reason}`. It also checks for the Web export templates specifically, because
without them the export fails with an error that reads like a broken preset.

## Already have a game? `bgate adopt`

`bgate init` scaffolds into a NEW directory. If you already have a Godot project,
adopt it instead.

```bash
cd my-existing-game
bgate adopt --pitch "what this game is"   # or: bgate adopt path/to/game
```

Adoption is additive only. It never copies a template file and never rewrites a
byte you wrote. It:

- creates `.bgate/game.db`
- merges the API-key ignore rules into your existing `.gitignore`, inside a
  marked block
- appends a `CLAUDE.md` briefing the same way
- prints what it detected: Godot version, main scene, 2D vs 3D, and scene,
  script and asset counts

Safe to re-run. The second run refreshes the marked blocks in place instead of
stacking a second copy.

## CLAUDE.md

Both `init` and `adopt` stamp a `CLAUDE.md` into the project. That file is the
instructions for the Claude Code session working in *your game*: what a seat is,
how a work item is created and closed, what the bible and lore are for, the art
pipeline, and what not to do. It is the first thing to read.

## Switching between projects

```bash
bgate projects                    # every known project, * marks the active one
bgate use emberfall               # by registered name, or by directory
```

`bgate use` writes a pointer to `~/.bgate/active.json`. That file is user-scoped
and never in the repo, so `bgate serve`, `bgate doctor` and the MCP tools pick it
up without you exporting `BGATE_ROOT` in every shell.

The pointer is the LOWEST-priority answer on purpose. Resolution order:

1. an explicit `project_dir=` on a tool call
2. `BGATE_ROOT`
3. standing inside a project directory
4. the pointer

`project_select` is deprecated and switches nothing. It used to mutate a
module-level active root, which made "which game does this call affect" a
function of call order.

## Registering the MCP server

```bash
claude mcp add builders-gate --scope user -- <abs-python> -m bgate_mcp.server
bgate hook-install <game-project>         # lane/lock teeth
bgate hook-status <game-project>          # proves enforcement is live
```

Registration must use the ABSOLUTE python path. The claude CLI's health check
resolves a bare `python` differently than your shell and reports "failed to
connect" against a server that runs fine.

`hook-install` writes a PreToolUse hook into `.claude/settings.json`. It asks
`seat_can_write` before every Bash, Write and Edit call, and blocks out-of-lane
or lock-violating writes with exit 2 plus guidance.

Enforcement activates only when a session sets `BGATE_SEAT=<role>`. With no seat
adopted, outside a bgate project, or on anything unexpected, the hook is inert
and fails open. A crashing hook must never dam a session. `hook-status` is the
only thing that proves enforcement is actually live, and it exits 1 if it is not.
