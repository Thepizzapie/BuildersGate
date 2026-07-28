# Builders Gate

[![CI](https://github.com/Thepizzapie/BuildersGate/actions/workflows/ci.yml/badge.svg)](https://github.com/Thepizzapie/BuildersGate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform: Windows primary](https://img.shields.io/badge/platform-Windows%20primary-lightgrey)](docs/setup.md#platform-support)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A game development pipeline that a fleet of AI agents can actually operate.**

You install it, run `bgate init`, and you have a runnable Godot game plus a local
dashboard. From there, agents drive the work through 78 MCP tools. A director
writes the design bible and draws a cut line. A narrative seat adds lore that a
deterministic canon check gates. An art seat generates sprites against pinned
references and locks the binaries while it edits them. A gameplay seat writes
GDScript, runs the game headless, and takes a screenshot to see what it did.

Then you play the build yourself, talking out loud. Your voice comes back as
classified feedback joined to the game's own telemetry on one clock.

The point is not that agents write code. It is that the pipeline **refuses**:
out-of-scope work, a spend ceiling, a locked file, an agent trying to approve its
own art. Approval is human-only throughout. An agent records a verdict, it does
not sign off.

Local-first. One SQLite file per game project. No daemon, no cloud, no build step
in the frontend.

**Who it is for:** solo developers and small teams running Claude Code (or
another MCP client) who want an agent fleet to build a real, playable game and
want to keep the steering wheel. If you want a chat that writes a design
document, this is far too much machinery.

> **New to this?** Start at **[`docs/start-here.md`](docs/start-here.md)**. It
> assumes nothing and defines the vocabulary once. The
> **[FAQ](docs/faq.md)** answers the questions people actually ask: how the
> character art was really made, whether this works for 3D, what it costs, and
> how to point it at a Godot game you already have. Unfamiliar term?
> **[`docs/glossary.md`](docs/glossary.md)**.

## Project status

Honest version, 2026-07-27, first public release. This is a solo project that has
built real games on one machine, and it shows in both directions.

**Works, and is exercised by the test suite and by daily use.** The MCP server
and its tools. Seats, lanes, asset locks and the PreToolUse hook. The Godot
adapter: headless run and check, import with in-engine inspection, screenshots,
scaffolds. Both image providers. The dashboard. Playtest capture through to a
joined brief. `bgate publish`. `bgate doctor`. CI runs the suite plus a
clean-venv wheel smoke test.

**Works, but not covered by CI.** The Blender adapter and the glTF round trip.
They are exercised by daily use and by tests that drive a real Blender, but those
tests are `slow`-marked or skip when Blender is absent. The CI badge above says
nothing about them. Treat 3D as the less-travelled path. [The
FAQ](docs/faq.md#will-this-work-for-a-3d-game) says exactly how much thinner it
is than 2D.

**Half-built, and named as such.** The audio seat's workspace is a deliberate v1
of sound library, playback and cue sheet, and the UI says so in a banner. The
dashboard's error surfacing is uneven; a failed mutation still sometimes renders
as nothing happening, per [`docs/ui-ux-audit.md`](docs/ui-ux-audit.md). Godot
version detection reports "unknown" on some builds.

**A proposal, not a product.** [`bgate_engine/`](bgate_engine/) is a design note
with JSON schemas and **no runtime code**. Nothing in the repository imports it.
Its central claim, a second authoritative simulation in Python, was **withdrawn**
after the experiment recorded in its own `DESIGN.md` §16.5 came back negative
against two other titles. It ships because the schemas are packaged data and the
reasoning is worth reading, not because it is a direction.

**Provenance to weigh.** Most of this was proven against a small number of games
on one Windows machine. [`docs/qa-nitpick-audit.md`](docs/qa-nitpick-audit.md) is
a harsh self-audit of exactly that. Its top-10 blockers have since been worked,
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

**Windows is the supported platform.** Linux is best-effort: CI runs the suite
there but marks it `continue-on-error`, because parts of the product shell out to
Windows tooling. macOS is untested.

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
| `bgate adopt` | Point it at a Godot project you already have. Additive only, never rewrites a byte you wrote |
| `bgate projects` | List every known project |
| `bgate use <name>` | Switch the active project without exporting `BGATE_ROOT` |
| `bgate publish` | Turn every game on the machine into a static arcade site |
| `bgate hook-status` | Prove the enforcement hook is actually live |

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
| [faq.md](docs/faq.md) | Character art, 3D, cost, speed, existing projects |
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
| [gap-analysis.md](docs/gap-analysis.md) | Where the pipeline can improve tenfold, each gap backed by something that happened and what it cost |
| [qa-nitpick-audit.md](docs/qa-nitpick-audit.md) | An eight-persona audit that took the product apart. **Historical**: much is fixed, and the status header says which |
| [ui-ux-audit.md](docs/ui-ux-audit.md) | A blind UX audit of all 15 views |

## Working on Builders Gate itself

```bash
pip install -e ".[dev]"
python -m pytest -m "not slow" -q
```

`-m "not slow"` deselects the tests that drive real Blender, real whisper, and
the in-suite wheel build. It is what CI runs. Drop it only if you have both
installed and want to wait.

CI runs that suite on Windows and Linux, plus a clean-venv wheel smoke test. The
failure that test exists to catch is invisible under `pip install -e .`: a wheel
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
