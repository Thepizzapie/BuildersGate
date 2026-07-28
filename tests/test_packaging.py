"""The wheel has to contain the product — not just the .py files.

The shipped wheel used to declare `package-data = {bgate_ui = ["static/*.html"]}`
and nothing else, which meant `pip install builders-gate` produced:

* a dashboard whose every ``/static/**/*.js`` 404'd (seat panels, flows, wf,
  nodecanvas, atlas — the entire frontend),
* a scaffolder that raised ``FileNotFoundError`` because ``templates/`` was not
  package data at all, and
* no ``bgate_engine/`` schemas.

Nothing caught it because nothing ever installed the wheel. These tests are the
fast guard (declaration coverage, no build), and ``test_wheel_contains_*`` is
the slow one that actually builds the artifact and looks inside it. The full
build-install-import loop lives in CI (.github/workflows/ci.yml, wheel-smoke).
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

# (package dir, extensions that MUST ship). Anything the runtime reads at
# request/scaffold time and cannot regenerate.
SHIPPED_TREES = (
    ("bgate_ui", REPO / "bgate_ui" / "static", {".js", ".html", ".css", ".svg", ".png"}),
    ("templates", REPO / "templates", {".gd", ".tscn", ".godot", ".svg", ".cfg"}),
    ("bgate_engine", REPO / "bgate_engine", {".json", ".md"}),
)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _package_data(cfg: dict) -> dict[str, list[str]]:
    return cfg["tool"]["setuptools"]["package-data"]


def _matches_any(rel: str, patterns: list[str]) -> bool:
    """setuptools glob semantics, close enough for a coverage assertion:
    ``**/`` spans directories, ``*`` does not."""
    for pat in patterns:
        if pat.startswith("**/"):
            tail = pat[3:]
            parts = rel.split("/")
            if any(fnmatch.fnmatch("/".join(parts[i:]), tail)
                   for i in range(len(parts))):
                return True
        elif "**" in pat:
            head, tail = pat.split("**", 1)
            if rel.startswith(head) and fnmatch.fnmatch(
                    rel[len(head):].lstrip("/"), tail.lstrip("/") or "*"):
                return True
        elif fnmatch.fnmatch(rel, pat):
            return True
    return False


class TestDeclarations:
    def test_data_only_trees_are_installed_packages(self, cfg):
        """templates/ and bgate_engine/ carry no .py, but scaffold.py resolves
        ``__file__/../../templates`` — so they must land in site-packages next
        to the code packages or the scaffolder cannot find them."""
        find = cfg["tool"]["setuptools"]["packages"]["find"]
        assert "templates" in find["include"]
        assert "bgate_engine" in find["include"]
        # __init__-less dirs are only collectable as namespace packages.
        assert find.get("namespaces") is True

    def test_route_subpackages_are_included(self, cfg):
        """bgate_ui.routes is auto-discovered at runtime; a wildcard include is
        what keeps a NEW route module from silently missing the wheel."""
        include = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
        assert "bgate_ui*" in include
        assert (REPO / "bgate_ui" / "routes" / "__init__.py").is_file()

    def test_tests_are_not_shipped(self, cfg):
        exclude = cfg["tool"]["setuptools"]["packages"]["find"].get("exclude", [])
        assert any(p.startswith("tests") for p in exclude)

    @pytest.mark.parametrize("pkg,tree,exts", SHIPPED_TREES,
                             ids=[t[0] for t in SHIPPED_TREES])
    def test_every_runtime_data_file_is_declared(self, cfg, pkg, tree, exts):
        patterns = _package_data(cfg)[pkg]
        pkg_dir = REPO / pkg
        missed = [
            str(p.relative_to(pkg_dir)).replace("\\", "/")
            for p in tree.rglob("*")
            if p.is_file() and p.suffix in exts and "__pycache__" not in p.parts
            and not _matches_any(str(p.relative_to(pkg_dir)).replace("\\", "/"),
                                 patterns)
        ]
        assert not missed, f"{pkg} package-data {patterns} misses: {missed}"

    def test_nested_static_seats_covered(self, cfg):
        """The regression that motivated all of this: ``static/*.html`` matched
        one file and nothing under ``static/seats/``."""
        seats = list((REPO / "bgate_ui" / "static" / "seats").glob("*.js"))
        assert seats, "no seat panels found — did the tree move?"
        patterns = _package_data(cfg)["bgate_ui"]
        for p in seats:
            rel = f"static/seats/{p.name}"
            assert _matches_any(rel, patterns), rel

    def test_slow_marker_is_registered(self, cfg):
        markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
        assert any(m.split(":")[0].strip() == "slow" for m in markers)


def _build_wheel(outdir: Path) -> Path:
    cmd = [sys.executable, "-m", "build", "--wheel", "--no-isolation",
           "--outdir", str(outdir), str(REPO)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if proc.returncode != 0:
        pytest.skip(f"wheel build unavailable: {proc.stderr[-500:]}")
    wheels = sorted(outdir.glob("*.whl"))
    assert wheels, "build reported success but produced no wheel"
    return wheels[-1]


@pytest.mark.slow
class TestBuiltWheel:
    """Builds the real artifact. ~15s, hence slow — CI runs the full
    build+install smoke separately."""

    @pytest.fixture(scope="class")
    def names(self, tmp_path_factory) -> set[str]:
        pytest.importorskip("build")
        wheel = _build_wheel(tmp_path_factory.mktemp("wheel"))
        with zipfile.ZipFile(wheel) as zf:
            return set(zf.namelist())

    def test_wheel_contains_frontend_javascript(self, names):
        js = {n for n in names if n.startswith("bgate_ui/static/")
              and n.endswith(".js")}
        on_disk = {f"bgate_ui/static/{p.relative_to(REPO / 'bgate_ui' / 'static')}"
                   .replace("\\", "/")
                   for p in (REPO / "bgate_ui" / "static").rglob("*.js")}
        assert on_disk, "no JS in the source tree?"
        assert on_disk <= js, f"missing from wheel: {sorted(on_disk - js)}"
        assert "bgate_ui/static/index.html" in names

    def test_wheel_contains_scaffold_templates(self, names):
        for expected in ("templates/2d/project.godot",
                         "templates/3d/project.godot",
                         "templates/shared/addons/bgate/bgate_telemetry.gd"):
            assert expected in names, expected

    def test_wheel_contains_engine_schemas(self, names):
        schemas = {n for n in names
                   if n.startswith("bgate_engine/schemas/") and n.endswith(".json")}
        assert len(schemas) >= 5, sorted(names)

    def test_wheel_contains_route_modules(self, names):
        routes = {n for n in names if n.startswith("bgate_ui/routes/")
                  and n.endswith(".py")}
        assert "bgate_ui/routes/__init__.py" in routes
        assert len(routes) > 1, "route modules missing from the wheel"

    def test_wheel_ships_no_bytecode_or_tests(self, names):
        assert not [n for n in names if n.endswith(".pyc")]
        assert not [n for n in names if n.startswith("tests/")]
