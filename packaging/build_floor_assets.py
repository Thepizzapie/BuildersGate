"""Build the optional floor-assets wheel from this repo's own trees.

The studio floor's paintings (static/img/floor) and ambience
(static/audio/floor) are ~30MB of a 39MB static tree — decoration every
install used to pay for. The main wheel now excludes them (see
exclude-package-data in pyproject.toml) and this script packages the SAME
files, unchanged, as `builders-gate-floor-assets`: a one-module package whose
whole API is `path()`, which bgate_ui.app uses to mount the assets when the
local tree is absent.

    python packaging/build_floor_assets.py        # wheel lands in dist/

Versioned in lockstep with the main package — the assets change when the
floor's art changes, and one number to reason about beats two drifting.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "bgate_ui" / "static"
DIST = REPO / "dist"
STAGE = REPO / "build" / "floor_assets_stage"

INIT = '''"""The Builders Gate studio-floor assets. One function; see path()."""
from pathlib import Path


def path() -> str:
    """The directory holding img/ and audio/ — what the dashboard mounts."""
    return str(Path(__file__).resolve().parent)
'''

PYPROJECT = '''[project]
name = "builders-gate-floor-assets"
version = "{version}"
description = "Studio-floor paintings and ambience for Builders Gate (optional)."
requires-python = ">=3.11"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["bgate_floor_assets"]

[tool.setuptools.package-data]
bgate_floor_assets = ["img/**/*", "audio/**/*"]
'''


def main() -> int:
    version = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    for sub in ("img/floor", "audio/floor"):
        if not (STATIC / sub).is_dir():
            print(f"error: {STATIC / sub} is missing — build from a source "
                  "checkout (the wheel deliberately does not carry these)")
            return 1

    if STAGE.exists():
        shutil.rmtree(STAGE)
    pkg = STAGE / "bgate_floor_assets"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(INIT, encoding="utf-8")
    (STAGE / "pyproject.toml").write_text(
        PYPROJECT.format(version=version), encoding="utf-8")
    shutil.copytree(STATIC / "img" / "floor", pkg / "img")
    shutil.copytree(STATIC / "audio" / "floor", pkg / "audio")

    DIST.mkdir(exist_ok=True)
    got = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--wheel-dir", str(DIST), str(STAGE)],
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
