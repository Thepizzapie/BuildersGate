# Contributing

This is a solo project, published because the ideas in it are worth arguing
about. **Feedback is worth more to me than patches right now** — the design is
still moving, and a good bug report or a "this concept does not survive contact
with my studio" is more useful than a merged diff.

## What is most useful

- **It did not run.** Setup failures beat everything else. If `pip install -e .`,
  `bgate doctor`, `bgate init` or `bgate serve` failed on your machine, that is
  the highest-value report there is — the quickstart is only ever verified on one
  machine.
- **The docs lie.** Anywhere the README or `docs/` describes behaviour the code
  does not have. Quote the line.
- **A gate that does not gate.** Any place a refusal (the cut line, seat lanes,
  asset locks, the budget, human-only approval) can be walked around. That class
  of bug is the point of the project, so it is the class I most want to hear
  about. Security-shaped findings go through [SECURITY.md](SECURITY.md), not a
  public issue.
- **It does not fit how you actually work.** Concept-level disagreement is
  welcome and does not need a repro.

Less useful: style preferences, dependency-addition proposals, and large
unsolicited refactors.

## Reporting a bug

Use the bug report form under **Issues**. Whatever route you take, include:

- **OS and version** (Windows 11, Ubuntu 24.04, …)
- **Python version** — `python -V`
- **`bgate doctor --json` output** — it answers "is the toolchain even here" in
  one pass, and most first-run reports are answered by it. It never prints your
  API key, only whether one is set.
- **What you ran, what you expected, what happened**, with the exact error text.

## Platform

**Windows is the primary and only supported platform.** It is what everything is
developed and verified on, and parts of the product shell out to Windows tooling
(`taskkill`, `tasklist`). Linux is best-effort: CI runs the suite there but
marks it `continue-on-error`, and tests that need Windows tooling skip cleanly.
macOS is untested. Reports from Linux and macOS are welcome and will be read;
they are not promised a fix.

## Running the tests

```bash
pip install -e ".[dev]"
python -m pytest -m "not slow" -q
```

`-m "not slow"` deselects the tests that drive real Blender and real whisper.
Drop it only if you have both installed and want to wait. This is the same
command CI runs.

If you are changing anything the wheel ships (JavaScript under
`bgate_ui/static/`, anything in `templates/`, the engine schemas), check the
`wheel-smoke` job in `.github/workflows/ci.yml` — a wheel that quietly ships no
JavaScript is a bug this repo has already had once.

## Pull requests

Small and focused, with the reasoning in the commit message. This codebase
comments *why*, not *what*, and is candid about its own failures; match that
voice rather than smoothing it out. New behaviour needs a test.
