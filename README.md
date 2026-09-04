# Builders Gate

[![builders-gate.com](https://img.shields.io/badge/site-builders--gate.com-ff6a3d)](https://builders-gate.com)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Platform: Windows primary](https://img.shields.io/badge/platform-Windows%20primary-lightgrey)](docs/setup.md#platform-support)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dev streams on Twitch](https://img.shields.io/badge/twitch-thepizzzapie-9146FF)](https://twitch.tv/thepizzzapie)

**An MCP server for building games with a coding agent.** It gives that agent 251
tools scoped to one game project: a design database, a work queue, reference-pinned
art and music generation, Godot and Blender adapters, and playtest capture. All
state lives in one SQLite file inside the project. `bgate connect` wires it into
Claude Code, Codex, Gemini CLI, VS Code, Cursor, Windsurf or opencode in one
command.

You run several Claude Code sessions at once, each holding a seat — art, gameplay,
narrative, QA, audio, tech. They share one database, so they do not contradict each
other, and file locks stop two of them editing the same asset. A local dashboard
shows what each one is doing, and is where you dispatch work and approve output.

Three limits are enforced in code: a run is killed at a wall-clock ceiling, a file
locked by one seat cannot be written by another, and no agent approves its own art.

There is no spend ceiling and no ledger. Builders Gate does not meter money: the
only budget that exists is the balance on your own provider account, and
`provider_status` reads it from the provider.

**For:** people running Claude Code on a game who want more than one session working
on it at once. It is more machinery than a small project needs.

**Platform:** Windows. Linux is best-effort and macOS is untested.

<p align="center">
  <img src="docs/screenshots/overview.png" width="820"
       alt="The dashboard: live agents, the queue, and recent activity">
</p>

The dashboard also carries a scene editor that reads and writes the project's real
`.tscn` files, a sprite editor with frame detection and per-frame regeneration, a
3D viewer that measures scale and orientation against the project's units, an audio
lab, and a brainstorm room the seats can be invited into.
[See them](docs/screenshots.md).

## Requirements

- Python 3.11+
- An MCP client. `bgate connect` wires seven of them; [Claude
  Code](https://claude.com/claude-code) is what it is developed against, and the
  board dispatches work to Claude Code or Codex
- [Godot 4.x](https://godotengine.org), plus the Web export templates for
  `bgate publish`
- Optional: [Blender 4.2+](https://blender.org); `ffmpeg`/`ffprobe` for playtest
  capture; `faster-whisper` and `sounddevice` for transcription
- Optional: `OPENAI_API_KEY` or `KREA_API_KEY` for art, `KIE_API_KEY` for music,
  `DEEPGRAM_API_KEY` for speech — all settable from the dashboard

Full detail, including the key table and platform notes:
[`docs/setup.md`](docs/setup.md).

## Install

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .                          # or: pip install -e ".[dev,stt,record]"

bgate doctor                              # what is installed and what is missing
bgate init emberfall --kind 2d            # a project AND a runnable game
cd emberfall
bgate serve                               # dashboard on http://127.0.0.1:7788
```

`bgate init` creates a new directory named after the project rather than scaffolding
into the one you are standing in. `bgate adopt` points it at a Godot project you
already have, and never rewrites a file you wrote.

`bgate doctor` exits 1 if anything on its list is missing, including rows nobody
needs on day one. Read the rows, not the exit code: `python` and `godot` green is
enough to start.

### Connect your coding agent

```bash
bgate connect                        # what is installed, what is wired, what is wrong
bgate connect claude                 # wire one, pinned to this interpreter
bgate hook-install <game-project>    # lane and lock enforcement
```

`bgate connect` knows `claude`, `codex`, `gemini`, `vscode`, `cursor`, `windsurf`
and `opencode`, and writes through each client's own `mcp add` where there is one.
For clients that keep their servers in a hand-edited JSON file, `--show` prints the
block with the interpreter already filled in.

That interpreter is the point: a bare `python` resolves against whatever is first on
PATH when the client launches the server, which is routinely not the environment
`pip install` ran in. The client then reports "failed to connect" and the message
points nowhere near the cause. It is the most common setup failure on Windows.

Restart the client before the tools appear. If `project_status` is not in the tool
list, the server is not connected.

### Commands

| Command | What it does |
|---|---|
| `bgate serve` | The dashboard in a browser tab |
| `bgate app` | The same dashboard in a native window (`pip install -e ".[desktop]"`) |
| `bgate adopt` | Point it at an existing Godot project |
| `bgate projects` / `bgate use <name>` | List projects, switch the active one |
| `bgate publish` | Build a static arcade site from every game on the machine |
| `bgate connect` | Wire your coding agent to the MCP server, or say why it is not |
| `bgate key set openai --global` | Store an API key outside any repository |
| `bgate hook-status` | Prove the enforcement hook is live |
| `bgate panic` | Stop every agent on a project |

A `BuildersGate-windows.zip` is on the
[releases page](https://github.com/Thepizzapie/BuildersGate/releases) for people who
do not want Python. It is unsigned, so Windows warns about it; every release ships a
`.sha256` beside the zip.

## The working loop

After `bgate init`, everything is an MCP tool call, so any session of a connected
client can drive it. One agent per seat, each adopting its role through
`BGATE_SEAT`.

```text
1  bgate init <name>       .bgate/game.db and a runnable game
2  DIRECTOR seat           bible_add: pillars, the core loop, constraints
3  NARRATIVE seat          lore_add / lore_fact, canon_check before a write lands
4  ART seat                ref_pin first, then generate; asset_lock around binaries
5  GAMEPLAY seat           code in its lanes; godot_check_project and godot_run
6  QA seat                 headless tests via godot_run; asset_verify for drift
7  playtest_start          play it, talk out loud; you promote what becomes work
```

Four rules keep multi-agent work safe: check `seat_can_write` before writing outside
your lane, lock binaries before editing them, run `canon_check` before a narrative
write lands, and `queue_add` anything another seat has to do.

`seat_brief(role)` returns what a seat needs to start: mission, lanes, bible, canon,
pinned references, and who holds which locks. Every tool takes an optional
`project_dir`, falling back to `BGATE_ROOT` and then to the nearest `.bgate/`.

## Repository layout

| Path | What it holds |
|---|---|
| [`src/`](src/) | Every importable package, and the Godot templates that ship beside them |
| [`frontend/`](frontend/) | The dashboard's UI source. `src/` is the React shell, `public/` the classic modules |
| [`tests/`](tests/) | 6,500 tests, grouped to mirror the packages. `-m "not slow"` skips the ones driving real Blender and whisper |
| [`docs/`](docs/) | Setup, reference, design notes, and findings from real production runs |

[`docs/reference.md`](docs/reference.md) has the full tree, package by package.

## Project status

First public release 2026-07-27. A solo project, proven against a small number of
games on one Windows machine.

| State | What |
|---|---|
| Covered by tests and daily use | The MCP server and its tools. Seats, lanes, locks, the hook. The Godot adapter. Both image providers. The dashboard. Playtest capture. `bgate publish` |
| Works, proven by hand | The Blender adapter and the glTF round trip. Its tests are `slow`-marked or skip without Blender. Nothing here generates 3D geometry |
| Newer | Music through kie.ai, Deepgram speech, the streamer chat integration, the local-runtime panels |
| Uneven | Error surfacing in the dashboard. Godot version detection reports "unknown" on some builds |

## Docs

[`docs/README.md`](docs/README.md) indexes everything.

| Page | What it is |
|---|---|
| [start-here.md](docs/start-here.md) | The front door. Assumes nothing |
| [glossary.md](docs/glossary.md) | Every term this project uses narrowly |
| [setup.md](docs/setup.md) | Requirements, keys, adopt, MCP registration, platforms |
| [reference.md](docs/reference.md) | Every surface in detail |
| [design-notes.md](docs/design-notes.md) | Budgets, approval, canon, technology choices |
| [gotchas.md](docs/gotchas.md) | GPU cold starts, stdio deadlocks, telemetry clocks |
| [lessons-from-a-shipped-game.md](docs/lessons-from-a-shipped-game.md) | What a shipped game cost, and the rules that came out of it |

## Working on it

```bash
pip install -e ".[dev]"
ruff check .
python -m pytest -m "not slow" -q
```

Both are merge gates in CI. The front end is built separately and its output is
committed, so a plain `pip install` needs no toolchain:

```bash
cd frontend && npm install && npm run build
```

[`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) has the full set of checks.

## Streams and community

Builders Gate is built on stream at
[twitch.tv/thepizzzapie](https://twitch.tv/thepizzzapie), using the tool to make a
game, which is how most of its gates get found.

**Privacy mode** (Settings → Privacy) hides absolute paths, your username, hostname
and every API key from the dashboard, the logs and the CLI, so the screen is safe to
show. **Feedback sessions** bring the stream's chat into the dashboard and capture
its reactions against the build you are playing; closing one hands the session to
the director. Channel configuration is read from the environment, so a public
checkout carries the feature and none of the account.

## Security

The dashboard binds to `127.0.0.1`, which is not a boundary on its own: any page you
have open can POST to localhost. Three checks sit on the mutating surface — a host
allowlist that closes DNS rebinding, same-origin checks on `sec-fetch-site` and
`Origin`, and a per-project bearer token minted into `.bgate/ui-token` (0600,
gitignored) and required on every mutation.

Approval is human-only throughout. A dispatched agent carries
`BGATE_ACTOR=agent:item-<id>` and is refused the bible, provider keys, the revert,
workflow gates, and promoting a candidate into the build.

It does not protect against a hostile local user (anything that can read `.bgate/`
has the token), a hostile prompt or project (lanes and locks are coordination, not a
sandbox), exposure to a network (no roles, no multi-user model), or untrusted input
to the adapters — executing the GDScript you hand `godot_run` is the feature.

[`.github/SECURITY.md`](.github/SECURITY.md) has the whole threat model. Report
vulnerabilities through GitHub Security Advisories, not a public issue.

## Contributing and licence

Feedback is worth more than patches here, especially "it did not run on my machine"
and "this gate can be walked around". See
[`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md) and
[CHANGELOG.md](CHANGELOG.md).

MIT. See [LICENSE](LICENSE).
