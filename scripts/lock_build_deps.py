"""Freeze the release build's dependency tree, with hashes.

    python scripts/lock_build_deps.py

Writes packaging/build-requirements.lock — every package the frozen app needs,
pinned to an exact version and to the SHA-256 of each artifact pip may install.
build_exe.py then installs with --require-hashes, which makes pip refuse
anything whose bytes do not match.

WHY THIS EXISTS. The build venv is now the ONLY thing that decides what goes
into the release: bgate.spec bundles what that interpreter can import, and
nothing else. Before this, that venv was populated by resolving
`.[desktop,build,record,stt]` against PyPI at build time — so the same commit
produced different binaries on different days, and a compromised or
typosquatted release of any transitive dependency would have been packaged and
shipped under our name, silently. There is no signature on the output to catch
it afterwards.

--require-hashes also has a side effect worth having: pip refuses to install
ANYTHING not listed, so a dependency appearing out of nowhere fails the build
rather than joining the bundle.

RE-RUN THIS WHEN pyproject.toml CHANGES, and read the diff. A lock file nobody
regenerates becomes a lock file somebody deletes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "build-requirements.lock"

# Must match VENV_INSTALL in packaging/build_exe.py, minus the project itself:
# `-e .` cannot be hashed (it is a directory, not an artifact) and is installed
# separately with --no-deps.
EXTRAS = ".[desktop,build,record,stt]"

HEADER = """\
# GENERATED — do not edit by hand. Regenerate with:
#
#     python scripts/lock_build_deps.py
#
# Every package the release build installs, pinned by version and by the
# SHA-256 of each artifact pip is allowed to use. packaging/build_exe.py
# installs this with --require-hashes, so pip refuses anything whose bytes do
# not match and refuses anything not listed at all.
#
# The project itself is absent on purpose: `pip install -e . --no-deps` cannot
# be hash-pinned, and its contents are the commit you are building.
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bgate-lock-") as tmp:
        report = Path(tmp) / "report.json"
        print(f"resolving {EXTRAS} …")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
             "--ignore-installed", "--report", str(report), EXTRAS],
            cwd=ROOT,
        )
        if r.returncode != 0:
            sys.exit(f"pip could not resolve the tree (exit {r.returncode})")

        data = json.loads(report.read_text(encoding="utf-8"))

    lines: list[str] = []
    skipped: list[str] = []
    for item in sorted(data.get("install", []),
                       key=lambda d: d["metadata"]["name"].lower()):
        name = item["metadata"]["name"]
        version = item["metadata"]["version"]
        # A local/editable requirement has no downloadable artifact to hash.
        digest = (item.get("download_info", {})
                      .get("archive_info", {})
                      .get("hashes", {})
                      .get("sha256"))
        if not digest:
            skipped.append(name)
            continue
        lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}")

    if skipped:
        print("  not hashable (installed separately): " + ", ".join(skipped))

    LOCK.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {LOCK.relative_to(ROOT)} — {len(lines)} pinned packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
