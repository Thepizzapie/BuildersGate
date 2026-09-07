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
passes. Reading [project] means a floor cannot be raised or added without this
audit following it automatically.

A DEPENDENCY WITH NO `>=` IS AN ERROR, not a skip. An unfloored requirement is
the one case this script exists to catch and the one case a silent `continue`
would hide, so it fails loudly and names the requirement.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def floors(pyproject: Path) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]

    # Runtime deps plus dev: dev is what CI installs to run the suite, so a
    # vulnerable floor there is a real thing a contributor gets told to install.
    # The other extras (stt, voice, stems, desktop, build) are deliberately NOT
    # here — several pull heavy native trees that do not resolve on a plain
    # runner, and a job that cannot install is a job nobody can make green.
    declared = list(project.get("dependencies", []))
    declared += list(project.get("optional-dependencies", {}).get("dev", []))

    pinned, unfloored = [], []
    for raw in declared:
        req = Requirement(raw)
        lower = [s.version for s in req.specifier if s.operator == ">="]
        if not lower:
            unfloored.append(raw)
            continue
        # max by VERSION, not by string: "0.9" > "0.10" lexically and that is
        # exactly backwards. One `>=` is the normal case; this is for the day
        # someone writes two.
        pinned.append(f"{req.name}=={max(lower, key=Version)}")

    if unfloored:
        raise SystemExit(
            "these requirements declare no >= floor, so there is nothing to "
            "audit and a resolver may pick any version at all:\n  "
            + "\n  ".join(unfloored))
    return pinned


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
    print("\n".join(floors(root / "pyproject.toml")))
