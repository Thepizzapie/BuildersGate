"""scripts/dep_floors.py: the floor lists security.yml hands to pip-audit.

The release audit reads the extras' floors with --extras. Two extras sharing a
requirement (pywebview is in desktop and build) must yield one pin, and a typo
in the extra name must fail rather than audit an empty list and pass.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "dep_floors", ROOT / "scripts" / "dep_floors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PYPROJECT = """\
[project]
name = "x"
dependencies = ["mcp>=1.30.0,<2"]

[project.optional-dependencies]
dev = ["pytest>=9.0.3"]
desktop = ["pywebview>=5.0", "comtypes>=1.2"]
build = ["pyinstaller>=6.14", "pywebview>=5.0"]
floor = ["some-assets"]
"""


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(PYPROJECT, encoding="utf-8")
    return p


def test_default_is_runtime_plus_dev(pyproject):
    assert _load().floors(pyproject) == ["mcp==1.30.0", "pytest==9.0.3"]


def test_extras_only_and_deduplicated(pyproject):
    assert _load().floors(pyproject, ["desktop", "build"]) == [
        "pywebview==5.0", "comtypes==1.2", "pyinstaller==6.14"]


def test_unknown_extra_fails(pyproject):
    with pytest.raises(SystemExit, match="nope"):
        _load().floors(pyproject, ["nope"])


def test_unfloored_extra_fails(pyproject):
    with pytest.raises(SystemExit, match="some-assets"):
        _load().floors(pyproject, ["floor"])


def test_real_release_extras_resolve():
    pins = _load().floors(ROOT / "pyproject.toml",
                          ["desktop", "build", "record", "stt"])
    assert pins and all("==" in p for p in pins)
