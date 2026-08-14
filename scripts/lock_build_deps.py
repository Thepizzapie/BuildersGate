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

EVERY ARTIFACT OF EACH PINNED VERSION IS HASHED, not just the wheel this
machine happened to resolve. The first version of this recorded one digest per
package, taken from pip's own report -- and that report describes THIS
interpreter on THIS platform. CI runs a different Python, needs
cffi-2.1.1-cp312-...whl where the lock had recorded cp313, and the build died
on a hash mismatch that was not a tampering signal at all. The hashes come from
PyPI's release metadata now, so any wheel or sdist of the pinned version is
accepted and anything outside it is not.

RE-RUN THIS WHEN pyproject.toml CHANGES, and read the diff. A lock file nobody
regenerates becomes a lock file somebody deletes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


def _release_hashes(pkg: tuple[str, str]) -> tuple[str, str, list[str]]:
    """Every sha256 PyPI publishes for that exact version.

    ALL of them, not just the wheel this machine resolved. pip's own report
    describes THIS interpreter on THIS platform, so a lock built from it pins
    (say) cffi's cp313 wheel — and CI, running 3.12, needs the cp312 wheel and
    dies on a hash mismatch that is not a tampering signal at all. Taking the
    hashes from the release metadata means any legitimate artifact of the
    pinned version is accepted and nothing outside it is.
    """
    name, version = pkg
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as r:
        meta = json.load(r)
    out = sorted({u["digests"]["sha256"] for u in meta.get("urls", [])
                  if u.get("digests", {}).get("sha256")})
    if not out:
        sys.exit(f"PyPI lists no artifacts for {name}=={version}")
    return name, version, out


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

    pinned: list[tuple[str, str]] = []
    skipped: list[str] = []
    for item in sorted(data.get("install", []),
                       key=lambda d: d["metadata"]["name"].lower()):
        name = item["metadata"]["name"]
        version = item["metadata"]["version"]
        # A local/editable requirement has no downloadable artifact to hash.
        if not (item.get("download_info", {})
                    .get("archive_info", {})
                    .get("hashes", {})
                    .get("sha256")):
            skipped.append(name)
            continue
        pinned.append((name, version))

    if skipped:
        print("  not hashable (installed separately): " + ", ".join(skipped))

    print(f"fetching every artifact hash for {len(pinned)} packages …")
    with ThreadPoolExecutor(max_workers=12) as pool:
        resolved = list(pool.map(_release_hashes, pinned))

    lines: list[str] = []
    total = 0
    for name, version, hashes in resolved:
        total += len(hashes)
        joined = " \\\n    ".join(f"--hash=sha256:{h}" for h in hashes)
        lines.append(f"{name}=={version} \\\n    {joined}")

    LOCK.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {LOCK.relative_to(ROOT)} — {len(lines)} packages, "
          f"{total} artifact hashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
