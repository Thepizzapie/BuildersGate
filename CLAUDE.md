<!-- This file is for a Claude Code session that has been asked to install and
boot Builders Gate for someone. It is not the file that gets stamped into a game
project; that one is templates/shared/CLAUDE.md and it covers working IN a
project after setup. -->

# Setting up Builders Gate for a user

You have probably been asked something like "set this up for me" or "help me get
this running". The user may not know what an MCP server is. Do not explain the
architecture unless asked. Get them booted, then point them at
[docs/start-here.md](docs/start-here.md).

Windows is the supported platform. Linux is best-effort, macOS is untested. Say
so if they are on macOS rather than letting them find out.

## Ask two things first

1. **Do they already have a Godot game?** This decides `adopt` vs `init` and it
   is the single most common thing to get wrong. Scaffolding over someone's
   existing work is the one unrecoverable mistake available here.
2. **Do they have an image API key?** `OPENAI_API_KEY` or `KREA_API_KEY`. If
   not, everything except art generation still works. Do not block on it.

## Install

```bash
git clone https://github.com/Thepizzapie/BuildersGate
cd BuildersGate
pip install -e .
bgate doctor
```

`bgate doctor` exits 1 if anything on its list is missing, including things
nobody needs on day one. **Read the rows, not the exit code.** Python and Godot
are what the core loop requires. Blender, ffmpeg and whisper are for the 3D,
playtest-capture and voice paths respectively; a red row there is not a blocker
and you should say that instead of trying to fix it.

The `art_key` row is green when **any** art provider has a key — OpenAI or
Krea. (It used to probe `OPENAI_API_KEY` alone and fail a working Krea-only
setup; if you see `MISS openai_key`, that build predates the fix.)

## Point it at a game

Existing game:

```bash
cd <their game>
bgate adopt
```

Never scaffolds, never overwrites. Writes `.bgate/game.db`, appends a marked
block to `.gitignore`, appends a marked block to `CLAUDE.md`. Running it twice is
a no-op. It prints what it detected: Godot version, main scene, 2D or 3D, scene
and script counts. **Read that back to the user.** If the dimension is wrong,
pass `--kind`.

New game:

```bash
bgate init emberfall --kind 2d
```

Creates a NEW directory named after the project. It does not scaffold into the
directory you are standing in, which surprises people.

## Keys

Put them in a `.env` at the GAME PROJECT root, not in the BuildersGate checkout.
See `.env.example`. Both `init` and `adopt` stamp a `.gitignore` that ignores
`.env`, so this is safe by default now, but do not talk the user out of it: an
earlier version of these instructions caused a committed key.

Never write a key into a file you are about to commit, never echo one back in
chat, and never put one on a command line.

## Register the MCP server

```bash
claude mcp add builders-gate --scope user -- <ABSOLUTE-python-path> -m bgate_mcp.server
```

**Use the absolute path to the interpreter.** The claude CLI resolves a bare
`python` differently than the shell does and reports "failed to connect" for a
server that runs fine. On Windows this is the single most common failure, and
the error message points nowhere near the cause.

Get the path with `python -c "import sys; print(sys.executable)"` in the same
environment where `pip install -e .` ran.

Then, in the game project:

```bash
bgate hook-install <game project>
bgate hook-status <game project>
```

The hook is what makes lane and lock enforcement real rather than advisory.
`hook-status` proves it is biting; run it, do not assume.

## Verify, then hand over

```bash
bgate serve
```

Dashboard on http://127.0.0.1:7788. It binds to loopback only.

If they would rather have a window than a browser tab, `bgate app` runs the
same dashboard in one (needs `pip install -e ".[desktop]"`; on Windows it uses
the WebView2 runtime that ships with 11, so there is nothing else to install).
It takes a loopback port the OS picks, so it does not fight a `bgate serve`
that is already running.

There is also a standalone `BuildersGate.exe` on the releases page for people
who do not want Python at all — `python packaging/build_exe.py` builds it.

The user must restart Claude Code before the MCP tools appear. Tell them that
explicitly; a fresh session is the only thing that picks up a new server.

Then confirm the tools are actually live by calling `project_status`. If it is
not in your tool list, the server is not connected and you should say so plainly
rather than describing what it would have returned.

## Then

Point them at [docs/start-here.md](docs/start-here.md), which defines the
vocabulary, and tell them their project now has its own `CLAUDE.md` explaining
the working loop.

## Do not

- Do not scaffold into a directory that already has a game in it.
- Do not run `bgate init` when the user described an existing project.
- Do not put keys anywhere but a gitignored `.env` in the game project.
- Do not report a step as done without running it.
- Do not claim an MCP tool worked if the tool is not in your list.
