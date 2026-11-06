"""Print every declared dependency pinned to its own floor, for pip-audit.

WHY THIS EXISTS. security.yml audited `pip install .`, which resolves to the
NEWEST version of everything. That run is green essentially by construction —
the newest release of a package is the one least likely to carry an open
advisory — and it says nothing about the versions a user can actually end up
with. A floor is the OLDEST version a resolver may legally hand someone:
`>=10.0` on Pillow means a constrained environment, an old lockfile, or a
`--prefer-binary` resolver on a platform without new wheels can serve 10.0.0,
and 10.0.0 has eleven advisories against it. Auditing only the resolved tree
cannot see that, so the floors went unexamined through 45 releases and drifted
to a combined 23 known-vulnerable versions.

This turns `mcp>=1.28.1,<2` into `mcp==1.28.1` and hands the lot to pip-audit,
so CI checks the worst case a user can install rather than the best.

DERIVED, NOT DUPLICATED. The obvious version of this job is a second
requirements file listing the floors — which is a copy of pyproject that
nothing keeps in sync, so the first dependency bump makes it a lie that still
passes. Reading pyproject means a floor cannot be raised or added without this
audit following it automatically.

A DEPENDENCY WITH NO `>=` IS AN ERROR, not a skip. An unfloored requirement is
the one case this script exists to catch and the one case a silent `continue`
would hide, so it fails loudly and names the requirement.

TWO GROUPS, BECAUSE THEY CANNOT BE AUDITED THE SAME WAY. An earlier version of
this script read [project] only, and said so: the heavy extras were left out
because several pull native trees that do not resolve on a plain runner, and a
job that cannot install is a job nobody can make green. That reasoning is
sound and it is why the split below exists rather than one flat list — but it
had been quietly covering for a real gap. `[build-system] requires` was never
read at all, which let `setuptools>=68` sit unexamined while permitting four
advisories (PYSEC-2025-49, PYSEC-2026-1918, PYSEC-2026-3447,
GHSA-xmj3-9gf7-h52w). So:

  core      [project] dependencies + the dev extra + [build-system] requires.
            All resolve on a bare runner, so pip-audit can walk their
            transitive trees as it always has.
  isolated  every other extra (stt, voice, record, stems, desktop, floor,
            build, web). security.yml audits these ONE PIN AT A TIME and
            non-blockingly, because pip-audit resolves a requirements file as a
            unit and exits on the first failure: audited together, the one pin
            that cannot resolve here (`faster-whisper`, whose `av` wants libav
            headers) would hide what the other seven say. Singly, seven report
            and the eighth is a named gap. `--no-deps` is not a way out — it
            resolves and audits each pin's tree either way.

Run with --group to pick one; the default is `core`, which is what the
existing security.yml step asks for.
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

#: Requirements that are this repo's OWN packages. They live in-tree
#: (packaging/floor-assets), are never published to an index, and so have no
#: version for a resolver to choose between or for pip-audit to look up — the
#: unfloored check below would fail on them forever for no gain. This is a
#: closed, named list on purpose: anything NOT on it that lacks a `>=` is the
#: real finding and still raises.
SIBLING_PACKAGES = frozenset({"builders-gate-floor-assets"})

#: Extras audited alongside the runtime dependencies. `dev` is what CI installs
#: to run the suite, so a vulnerable floor there is a real thing a contributor
#: is told to install.
CORE_EXTRAS = ("dev",)


def _pin(raw: str) -> str | None:
    """`mcp>=1.28.1,<2` -> `mcp==1.28.1`. None for an in-repo sibling."""
    req = Requirement(raw)
    if req.name.lower().replace("_", "-") in SIBLING_PACKAGES:
        return None
    lower = [s.version for s in req.specifier if s.operator == ">="]
    if not lower:
        raise ValueError(raw)
    # max by VERSION, not by string: "0.9" > "0.10" lexically and that is
    # exactly backwards. One `>=` is the normal case; this is for the day
    # someone writes two.
    return f"{req.name}=={max(lower, key=Version)}"


def declared(pyproject: Path, group: str) -> list[str]:
    """Every requirement string in `group`, straight out of pyproject."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    extras = project.get("optional-dependencies", {})

    if group == "core":
        out = list(project.get("dependencies", []))
        for name in CORE_EXTRAS:
            out += list(extras.get(name, []))
        # THE ONE THAT GOT AWAY. A build backend is not a runtime dependency,
        # but it is code that runs on the installing machine every time there
        # is no wheel to use — which is exactly the constrained case a floor
        # describes. Reading [project] alone missed it for 46 releases.
        out += list(data.get("build-system", {}).get("requires", []))
        return out

    if group == "isolated":
        return [r for name, reqs in sorted(extras.items())
                if name not in CORE_EXTRAS for r in reqs]

    raise SystemExit(f"unknown group {group!r}")


def floors(pyproject: Path, group: str = "core") -> list[str]:
    pinned, unfloored = [], []
    for raw in declared(pyproject, group):
        try:
            pin = _pin(raw)
        except ValueError:
            unfloored.append(raw)
            continue
        if pin is not None and pin not in pinned:
            pinned.append(pin)

    if unfloored:
        raise SystemExit(
            "these requirements declare no >= floor, so there is nothing to "
            "audit and a resolver may pick any version at all:\n  "
            + "\n  ".join(unfloored))
    return pinned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", nargs="?", default=Path(__file__).parent.parent,
                    type=Path, help="directory holding pyproject.toml")
    ap.add_argument("--group", default="core", choices=("core", "isolated"),
                    help="core: runtime + dev + build backend (resolvable, "
                         "gating). isolated: the remaining extras, audited "
                         "one pin at a time and advisory only.")
    args = ap.parse_args(argv)
    print("\n".join(floors(args.root / "pyproject.toml", args.group)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
