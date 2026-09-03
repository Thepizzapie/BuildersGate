"""Build the optional floor-assets wheel from packaging/floor-assets/.

The studio floor's paintings (img/) and ambience (audio/) are ~30MB of
decoration that every install used to pay for. They live in their own package
now — `builders-gate-floor-assets`, source in packaging/floor-assets/ — whose
whole API is `path()`, which bgate_ui.app mounts under /static/img/floor and
/static/audio/floor when the package imports.

    python packaging/build_floor_assets.py        # wheel lands in dist/

Versioned in lockstep with the main package: this script refuses to build if
the two pyproject files disagree, because one number to reason about beats
two drifting.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "packaging" / "floor-assets"
PKG = SRC / "builders_gate_floor_assets"
DIST = REPO / "dist"


def _version(pyproject: Path) -> str:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def main() -> int:
    main_version = _version(REPO / "pyproject.toml")
    pack_version = _version(SRC / "pyproject.toml")
    if main_version != pack_version:
        print(f"error: pyproject.toml says {main_version} but "
              f"packaging/floor-assets/pyproject.toml says {pack_version} — "
              "bump them together")
        return 1

    for sub in ("img", "audio"):
        if not any((PKG / sub).rglob("*")):
            print(f"error: {PKG / sub} is empty — build from a source checkout")
            return 1

    DIST.mkdir(exist_ok=True)
    got = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--wheel-dir", str(DIST), str(SRC)],
        capture_output=True, text=True)
    if got.returncode != 0:
        print(got.stdout[-2000:])
        print(got.stderr[-2000:])
        return got.returncode
    made = sorted(DIST.glob("builders_gate_floor_assets-*.whl"))
    if not made:
        print("error: pip wheel reported success but no wheel landed in dist/")
        return 1
    print(f"built {made[-1].name} ({made[-1].stat().st_size / (1 << 20):.1f}MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
