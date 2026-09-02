"""Makes `python -m bgate_cli ...` work, identically to the `bgate` script.

WHY THIS IS NOT REDUNDANT WITH [project.scripts]. The console script only
exists on PATH, and PATH is exactly what is missing in the cases where someone
reaches for `python -m`: a shell that never picked up the interpreter's
Scripts directory, a CI step running a bare interpreter, a Git Bash session on
Windows, an editor terminal with its own environment. In every one of those
`bgate` is "command not found" while the package itself imports fine.

Without this file the fallback fails too, and it fails MISLEADINGLY:

    $ python -m bgate_cli serve
    No module named bgate_cli.__main__; 'bgate_cli' is a package and
    cannot be directly executed

which reads as a broken or partial install rather than a missing four-line
shim. Measured on this repo: two attempts to start the dashboard were lost to
that message before anyone looked at [project.scripts] and found the entry
point was `bgate_cli.main:main`.

The exit code is main()'s own, so `python -m bgate_cli doctor` still reports
failure the way the script does.
"""
from __future__ import annotations

import sys

from bgate_cli.main import main

if __name__ == "__main__":
    sys.exit(main())
