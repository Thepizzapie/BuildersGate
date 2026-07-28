# Builders Gate

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform: Windows primary](https://img.shields.io/badge/platform-Windows%20primary-lightgrey)](docs/setup.md#platform-support)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Builders Gate is an MCP server for building games with Claude Code.

It gives Claude 78 tools scoped to one game project: a design database, a work
queue, reference-pinned art generation, Godot and Blender adapters, and playtest
capture. State lives in one SQLite file in the project.

You run several Claude Code sessions at once, each assigned a role: art,
gameplay, narrative, QA, audio, tech. They read and write the same database, so
they do not contradict each other, and they cannot edit the same files at the
same time. A local dashboard shows what each one is doing and is where you
dispatch work and approve output.

Your own Claude session uses the same tools and the same database. Asking it what
is left before the vertical slice reads the queue and the scope tiers, not the
chat history.

`bgate init` creates a project with a runnable Godot game in it. `bgate adopt`
points it at a game you already have.

There are limits enforced in code: work outside the current scope tier is
refused, generation stops at a spend ceiling, files locked by one seat cannot be
written by another, and no agent can approve its own art.

Local-first. No daemon, no cloud, no build step in the frontend.

**Who it is for:** people running Claude Code who are building a game and want
more than one session working on it. It is more machinery than a small project
needs.

> **Setting this up with Claude?** Point it at
> [`CLAUDE.md`](CLAUDE.md) in this repo. It is written for an
> assistant doing the install on your behalf, including the two
> mistakes that waste the most time.
>
> **New to this?** Start at **[`docs/start-here.md`](docs/start-here.md)**. It
> assumes nothing and defines the vocabulary once. Unfamiliar term?
> **[`docs/glossary.md`](docs/glossary.md)**.

## Project status

Honest version, 2026-07-27, first public release. This is a solo project that has
built real games on one machine, and it shows in both directions.

**Works, and is exercised by the test suite and by daily use.** The MCP server
and its tools. Seats, lanes, asset locks and the PreToolUse hook. The Godot
adapter: headless run and check, import with in-engine inspection, screenshots,
scaffolds. Both image providers. The dashboard. Playtest capture through to a
joined brief. `bgate publish`. `bgate doctor`.

**Works, but only proven by hand.** The Blender adapter and the glTF round trip.
They are exercised by daily use and by tests that drive a real Blender, but those
tests are `slow`-marked or skip when Blender is absent, so a normal test run says
nothing about them. Nothing here generates 3D geometry, so treat 3D as the
less-travelled path.

**Half-built, and named as such.** The audio seat's workspace is a deliberate v1
of sound library, playback and cue sheet, and the UI says so in a banner. The
dashboard's error surfacing is uneven; a failed mutation still sometimes renders
as nothing happening. Godot
version detection reports "unknown" on some builds.

**A proposal, not a product.** [`bgate_engine/`](bgate_engine/) is a design note
with JSON schemas and **no runtime code**. Nothing in the repository imports it.
Its central claim, a second authoritative simulation in Python, was **withdrawn**
after the experiment recorded in its own `DESIGN.md` §16.5 came back negative
against two other titles. It ships because the schemas are packaged data and the
reasoning is worth reading, not because it is a direction.

**Provenance to weigh.** Most of this was proven against a small number of games
on one Windows machine. It has been through a harsh self-audit and the top-10
blockers have since been worked,
and its status header says which.

## Requirements

- Python 3.11+
- An MCP client. [Claude Code](https://claude.com/claude-code) is what it is
  developed against
- [Godot 4.x](https://godotengine.org). Add the Web export templates if you want
  `bgate publish`
- Optional: [Blender 4.2+](https://blender.org) for the 3D leg, `ffmpeg` and
  `ffprobe` for playtest capture, `faster-whisper` and `sounddevice` for
  transcription
- Optional: an `OPENAI_API_KEY` or `KREA_API_KEY` for generated art

**Windows is the supported platform.** Linux is best-effort: parts of the product
shell out to Windows tooling, and the suite has not been kept green there. macOS
is untested.

Full detail, including the API key table and the platform notes:
[`docs/setup.md`](docs/setup.md).

## Install and quickstart

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .                          # or: pip install -e ".[dev,stt,record]"

bgate doctor                              # python/key/ffmpeg/blender/godot/whisper
bgate init emberfall --kind 2d            # a project AND a runnable game
cd emberfall
                                          # optional: drop a .env here with your
                                          # image key, see .env.example
bgate serve                               # dashboard on http://127.0.0.1:7788
```

`bgate doctor` exits 1 if **anything** on its list is unavailable. That is right
for a CI step and alarming for a human: you only need Python and Godot for the
core loop. Read the rows, not the exit code.

`bgate init` creates a NEW directory named after the project, not whatever
directory you were standing in. It writes `.bgate/game.db`, unpacks the Godot
template, and prints the absolute path. `bgate serve` with no project shows a
first-run screen that does the same thing from the browser.

To let agents drive it:

```bash
claude mcp add builders-gate --scope user -- <abs-python> -m bgate_mcp.server
bgate hook-install <game-project>         # lane/lock teeth
```

Use the ABSOLUTE python path. The claude CLI's health check resolves a bare
`python` differently than your shell and reports "failed to connect" against a
server that runs fine.

Other entry points, covered in [`docs/setup.md`](docs/setup.md):

| Command | What it does |
|---|---|
| `bgate app` | The dashboard in a native window instead of a browser tab |
| `bgate adopt` | Point it at a Godot project you already have. Additive only, never rewrites a byte you wrote |
| `bgate projects` | List every known project |
| `bgate use <name>` | Switch the active project without exporting `BGATE_ROOT` |
| `bgate publish` | Turn every game on the machine into a static arcade site |
| `bgate hook-status` | Prove the enforcement hook is actually live |

### Desktop app

`bgate serve` runs a local web server and you open it in a browser. `bgate app`
runs the same server and puts it in a native window instead — on Windows that
is the Edge WebView2 runtime, which ships with Windows 11, so there is no
browser bundled and nothing extra to install:

```bash
pip install -e ".[desktop]"
bgate app
```

It binds to a loopback port the OS picks, so it will not collide with a
`bgate serve` you already have open, and it shuts the server down when you
close the window.

### Download it as a single .exe

No Python needed on the target machine. Grab `BuildersGate.exe` from the
[releases page](https://github.com/Thepizzapie/BuildersGate/releases) and run
it — double-click for the window, or `BuildersGate.exe serve` for the browser
dashboard out of the same binary.

To build it yourself:

```bash
pip install -e ".[desktop]" pyinstaller
python packaging/build_exe.py
```

That builds `dist/BuildersGate.exe` and then boots it to check it actually
serves its own assets. The check is not ceremony: this app finds `static/` and
`templates/` by walking up from `__file__`, so a bundle that lays them out
wrongly still starts, still renders the shell, and 404s every stylesheet. A
green PyInstaller run does not mean a working binary.

## The working loop

Step 1 is `bgate init`. Everything after it is an MCP tool call, so any Claude
session with the server registered can drive it. The intended shape: you or an
orchestrator fan out one agent per seat, each adopting its role via `BGATE_SEAT`.

```text
1  bgate init <name>       .bgate/game.db + a runnable game, path printed
2  godot_scaffold          (or, into an existing project: the same runnable slice)
3  DIRECTOR seat           bible_add: pillars, the core loop, scope tiers, the CUT LINE
4  NARRATIVE seat          lore_add / lore_fact; canon_check on every narrative write
5  ART seat                ref_pin approved references first, then blender_sprites /
                           image_sprites / image_generate; asset_lock before touching
                           any binary, asset_release after
6  GAMEPLAY seat           writes code in its lanes; godot_check_project + godot_run
                           after every change; godot_screenshot to SEE the game
7  QA seat                 headless test scripts via godot_run; asset_verify for drift
8  playtest_check/start    play it yourself, talk out loud; feedback lands classified
                           and joined to telemetry; YOU promote what becomes work
```

Four rules make multi-agent work safe. Check `seat_can_write` before writing
outside your obvious lane. Lock binaries before editing. Leave a `seat_post_note`
when your work changes another seat's world. Call `scope_check(rank)` before
building anything new.

`seat_brief(role)` returns everything a seat needs to start: mission, lanes,
bible, canon, pinned reference anchors, promoted feedback, and who holds which
locks.

Every tool takes an optional `project_dir`. It resolves the project from that,
then `BGATE_ROOT`, then by walking up from the cwd for a `.bgate/` dir. Pass it
explicitly whenever more than one project could be in play.

## Docs

[`docs/README.md`](docs/README.md) indexes everything with a line each.

**Start here**

| Page | What it is |
|---|---|
| [start-here.md](docs/start-here.md) | The front door. Assumes nothing |
| [glossary.md](docs/glossary.md) | Every term this project uses narrowly |

**Reference**

| Page | What it is |
|---|---|
| [setup.md](docs/setup.md) | Requirements, API keys, `adopt`, project switching, MCP registration, platform support |
| [reference.md](docs/reference.md) | Every surface in detail: dashboard, seats, locks, Blender to Godot, templates, publishing, playtest, layout |
| [design-notes.md](docs/design-notes.md) | The cut line, budgets, human-only approval, canon, and the technology choices |
| [gotchas.md](docs/gotchas.md) | GPU cold starts, stdio deadlocks, whisper segmentation, telemetry clocks |

**Findings**

| Page | What it is |
|---|---|

## Working on Builders Gate itself

```bash
pip install -e ".[dev]"
python -m pytest -m "not slow" -q
```

`-m "not slow"` deselects the tests that drive real Blender, real whisper, and
the in-suite wheel build. Drop it only if you have both installed and want to
wait.

There is a wheel smoke test in the suite, and the failure it exists to catch is invisible under `pip install -e .`: a wheel
that shipped no JavaScript and no `templates/` produced a dashboard of 404s and a
scaffolder that raised `FileNotFoundError`, and nothing had ever verified
otherwise. Linux is `continue-on-error`.

## Contributing, security, licence

Feedback is worth more here than patches, especially "it did not run on my
machine" and "this gate can be walked around". See
[CONTRIBUTING.md](CONTRIBUTING.md) for what to include in a report, and
[CHANGELOG.md](CHANGELOG.md) for the release state.

This tool executes arbitrary GDScript, shells out, and spawns agent sessions with
edit permissions. [SECURITY.md](SECURITY.md) states plainly what the localhost
guards do protect against (a browser page reaching your dashboard, including via
DNS rebinding) and what they do not (a hostile local user, a network deployment,
untrusted input to the adapters). Report vulnerabilities privately through GitHub
Security Advisories, not a public issue.

MIT. See [LICENSE](LICENSE).
