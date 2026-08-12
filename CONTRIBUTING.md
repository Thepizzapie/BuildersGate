# Contributing

This is a solo project, published because the ideas in it are worth arguing
about. **Feedback is worth more to me than patches right now.** The design is
still moving. A good bug report, or a "this concept does not survive contact with
my studio", is more useful than a merged diff.

## What is most useful

- **It did not run.** Setup failures beat everything else. If `pip install -e .`,
  `bgate doctor`, `bgate init` or `bgate serve` failed on your machine, that is
  the highest-value report there is. The quickstart is only ever verified on one
  machine.
- **The docs lie.** Anywhere the README or `docs/` describes behaviour the code
  does not have. Quote the line.
- **A gate that does not gate.** Any place a refusal (seat lanes, asset locks,
  the budget, the canon check, human-only approval) can be walked around. That
  class of bug is the point of the project. Security-shaped findings go through
  [SECURITY.md](SECURITY.md), not a public issue.
- **It does not fit how you actually work.** Concept-level disagreement is
  welcome and does not need a repro.

Less useful: style preferences, dependency-addition proposals, and large
unsolicited refactors.

## Reporting a bug

Use the bug report form under **Issues**, and include:

- **OS and version** (Windows 11, Ubuntu 24.04, and so on)
- **Python version**, from `python -V`
- **`bgate doctor --json` output.** It answers "is the toolchain even here" in
  one pass, and it never prints your API key, only whether one is set.
- **What you ran, what you expected, what happened**, with the exact error text.

## Platform

**Windows is the primary and only supported platform.** It is what everything is
developed and verified on, and parts of the product shell out to Windows tooling
(`taskkill`, `tasklist`). Linux is best-effort: the suite runs there but the CI
job is `continue-on-error`, and tests that need Windows tooling skip cleanly.
macOS is untested. Reports from Linux and macOS are welcome and will be read,
but they are not promised a fix.

## What must pass before a change lands

Four gates, all defined in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). Two you run directly:

```bash
pip install -e ".[dev]"

ruff check .                              # lint job
python -m pytest -m "not slow" -q         # test job, Windows, py3.11 and py3.12
```

The third is a route-import check. Every route module must import, with
`BGATE_STRICT_ROUTES=1` turning an import failure into a hard error. A missing
route module is the failure that looks healthiest from the outside, so CI gives
it its own step. The suite covers it too, so a green `pytest` run is the practical
signal.

The fourth is the `wheel-smoke` job, below. It runs on every pull request.

`-m "not slow"` deselects the tests that drive real Blender and real whisper.
Drop it only if you have both installed and want to wait. CI runs the same
command.

`ruff` is pinned in the `dev` extra so your run and CI's cannot disagree on the
version. The rule set is narrow, `E9` and `F`, which catch bugs rather than
taste, and is explained in `pyproject.toml`. Turning it on found eight dead
imports, pointless f-strings on `main`, and a `NameError` waiting to happen in an
unlanded branch.

## Changing anything the wheel ships

That means JavaScript under `bgate_ui/static/`, anything in `templates/`, or the
engine schemas. A wheel that quietly ships no JavaScript is a bug this repo has
already had once. CI covers it in the `wheel-smoke` job, and you can run it
yourself:

Run it from a bash shell (Git Bash on Windows), because the last two lines rely
on a glob and on `Scripts/`:

```bash
pip install build
python -m build --wheel
python -m venv .wheelenv && .wheelenv/Scripts/python -m pip install dist/*.whl
.wheelenv/Scripts/python packaging/smoke_wheel.py
```

`smoke_wheel.py` refuses to run if its imports resolved to the checkout, then
checks the shipped trees, runs `doctor`, scaffolds a project out of
`templates/`, and serves the dashboard's assets out of `site-packages`.

## Pull requests

Small and focused, with the reasoning in the commit message. This codebase
comments *why*, not *what*, and is candid about its own failures. Match that
voice rather than smoothing it out. New behaviour needs a test.
