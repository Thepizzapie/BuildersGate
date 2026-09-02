# Pulsiron panels

Three docked panels that drive a running Builders Gate from a Pulsiron
session — generate art, generate music, and work the board — without a browser
tab and without an agent in the middle.

They ship with this repository. Nothing to install: open a Pulsiron terminal
anywhere inside your checkout and they appear under **Custom Panel** in the
panel type picker.

| Panel | What it does |
| --- | --- |
| Builders Gate · Art | Paints one image, files it as a reviewable artifact, shows what you have made recently |
| Builders Gate · Music | Generates Suno takes, auditions them, keeps and installs one |
| Builders Gate · Orchestrate | Files work for a seat, reads the board, deploys what is ready, steers a running agent |

## What has to be true

**`bgate serve` must be running.** Every button is an HTTP call to the
dashboard's own loopback API — the panels can do exactly what the dashboard can
do and nothing else. With the server down, a button answers

```
no dashboard on http://127.0.0.1:7788 — run `bgate serve` in the project,
then press this button again
```

which is the single most common thing to go wrong. On a non-default port, set
`BGATE_UI_URL=http://127.0.0.1:9000` in the environment Pulsiron launches
terminals with.

**Python must be on `PATH`**, the same interpreter that has Builders Gate
installed. The routines call `python {repo}/scripts/panel_api.py`.

**Authentication is automatic.** Mutating endpoints require the dashboard token;
the panels read it from `<project>/.bgate/ui-token`, the same file the browser's
session is built on. There is no field to paste a key into and no key stored in
this repository.

## Press Load first

Every panel opens with its selects empty and a **Load** button at the top. One
press fills all of them from the live project — the task kinds this build
supports, the sizes and qualities, the providers you actually have keys for, the
pinned references by name, the Suno models, the music takes waiting to be
auditioned, the open board items, the seats working. Nothing in a panel asks you
to remember an identifier.

Load is free. Generating art or music is not, and those buttons say so.

## Seeing what you made

The art panel previews in place: **Show folders** fills the browser with every
directory under `.bgate_out/art`, and picking one draws its images in the grid
below. `· everything ·` is the top level, where a one-off image lands.

The music panel does not preview in place, and cannot: the panel schema has
widgets for images and sprite sheets and none for sound. **Play it** hands the
selected take to whatever plays audio on your machine, which is as close as the
format allows.

## What each button costs

- **Load**, **Show recent**, **Show board** — read-only, free.
- **Generate** (art) — one image, roughly $0.02–$0.19 depending on quality.
- **Generate** (music) — one Suno batch of several takes, real credits.
- **File it**, **Deploy**, **Steer** — free, but they put work on the board and
  spawn agents, which are not free.

Art and music both land as **candidates**: registered, reviewable, and not in
the game until a human keeps or imports them.

## When a panel shows nothing

*"No specs discovered — open a terminal in a repo with `.pulsiron/panels/`"*

A repo's panels are only discoverable while a terminal is open somewhere inside
that repo, and "inside that repo" means inside its **git root**. If your home
directory happens to be a git repository, a terminal opened at
`~/Desktop/anything` resolves to `~` rather than to the checkout, and no panel
is found. Point the Pulsiron project profile at the checkout itself.

## The pieces

```
.pulsiron/panels/*.panel.json    the three panels
.pulsiron/routines/*.toml        the routines their buttons run
.pulsiron/feeds/                 what Load writes; gitignored, rewritten per press
scripts/panel_api.py               the CLI the routines call
```

`scripts/panel_api.py` is usable on its own, which is the quickest way to tell a
broken panel from a broken dashboard:

```bash
python scripts/panel_api.py board
python scripts/panel_api.py art --prompt "a chipped enamel mug" --kind prop
python scripts/panel_api.py music --prompt "tense corporate ambience" --name tension
```
