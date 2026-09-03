"""The wheel has to contain the product — not just the .py files.

The shipped wheel used to declare `package-data = {bgate_ui = ["static/*.html"]}`
and nothing else, which meant `pip install builders-gate` produced:

* a dashboard whose every ``/static/**/*.js`` 404'd (seat panels, flows, wf,
  nodecanvas, atlas — the entire frontend),
* a scaffolder that raised ``FileNotFoundError`` because ``templates/`` was not
  package data at all.

Nothing caught it because nothing ever installed the wheel. These tests are the
fast guard (declaration coverage, no build), and ``test_wheel_contains_*`` is
the slow one that actually builds the artifact and looks inside it. The full
build-install-import loop lives in CI (.github/workflows/ci.yml, wheel-smoke).
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"

# (package dir, source tree, path prefix inside the package, extensions that
# MUST ship). Anything the runtime reads at request/scaffold time and cannot
# regenerate.
#
# bgate_ui/static IS BUILD OUTPUT: the Vite build copies frontend/public into it
# verbatim and adds dist/bgate.{js,css}. So the files that must be COVERED by
# package-data are named by the source tree, and land under `static/`.
SHIPPED_TREES = (
    ("bgate_ui", REPO / "frontend" / "public", "static/",
     {".js", ".html", ".css", ".svg", ".png"}),
    ("templates", REPO / "src" / "templates", "", {".gd", ".tscn", ".godot", ".svg", ".cfg"}),
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


class TestGitCarriesWhatShips:
    """A shipped file that git does not track is invisible until CI.

    THE FAILURE THIS EXISTS FOR: `templates/shared/.gitignore` is a template —
    it is COPIED INTO scaffolded game projects, where ignoring
    `export_presets.cfg` is exactly right, because that file holds per-machine
    export config and can carry an Android signing password. But it also sits
    inside this repository, so git applied it here and the Web export preset
    that every scaffolded project is supposed to ship was never committed.

    Everything looked fine on the machine that wrote it: the file is on disk, so
    the wheel built locally contained it and the tests passed. A fresh clone —
    CI, a contributor, the release build — got a wheel with no export preset, so
    `bgate publish` would fail on the one step it exists to remove. Package-data
    globs cannot save you from a file that is not in the checkout.

    Run against the working tree's git index, skipped when there is no checkout
    (an installed wheel has no .git).
    """

    def _untracked(self, tree: Path) -> list[str]:
        # -uall, or an entirely-untracked DIRECTORY collapses to one entry
        # ending in "/" — which the filter below drops, so a whole vendored
        # tree could go missing from the wheel and still pass this test. That
        # is the exact shape of the bug the class exists to catch.
        out = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--ignored=matching",
             "--", str(tree)],
            cwd=REPO, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        bad = []
        for line in out.stdout.splitlines():
            status, _, path = line.partition(" ")
            path = path.strip().strip('"')
            if status in ("!!", "??") and not path.endswith("/"):
                bad.append(path)
        return bad

    @pytest.mark.parametrize("tree", [
        REPO / "src" / "templates",
        # The frontend SOURCE (bgate_ui/static is where the build puts it), plus
        # the committed bundle: neither has a node step at install time, so both
        # have to be in the checkout.
        REPO / "frontend" / "public",
        REPO / "src" / "bgate_ui" / "static" / "dist",
        REPO / "src" / "bgate_site" / "theme",
    ], ids=lambda p: p.name)
    def test_every_shipped_file_is_in_the_checkout(self, tree):
        if not (REPO / ".git").exists():
            pytest.skip("not a git checkout")
        if not tree.is_dir():
            pytest.skip(f"{tree.name} not present in this checkout")
        stray = self._untracked(tree)
        assert not stray, (
            "these ship in the wheel but git does not carry them, so a fresh "
            "clone builds without them:\n  " + "\n  ".join(stray)
            + "\n(if one is ignored on purpose, `git add -f` it — a package-data "
            "glob cannot include a file that is not in the checkout)")


class TestTheShellsAssetsExist:
    """Every local file index.html points at must actually BE there.

    THE GAP THIS CLOSES, and it cost a whole feature. `_untracked` above asks
    "is everything on disk also in git", which cannot see a file that is on
    NEITHER — and that is exactly what happened to the 3D model viewer: a bare
    `build/` in .gitignore matched the vendored three/build/ directory, so
    three.js's library file was never committed, never on a fresh clone, and the
    import map pointed at a 404. Eighteen sibling files were tracked, which made
    the tree look vendored. The viewer could not load its own engine and nothing
    said why.

    Checking the REFERENCES rather than the directory is what makes this
    catchable: a missing file is only a bug if something asks for it, and the
    shell is where the asking is written down.
    """

    # Source shell. A /static/<x> reference resolves to frontend/public/<x>,
    # except /static/dist/* which the Vite build writes into bgate_ui/static.
    SHELL = REPO / "frontend" / "public" / "index.html"
    PUBLIC = REPO / "frontend" / "public"
    BUILT = REPO / "src" / "bgate_ui" / "static"

    @classmethod
    def _resolve(cls, ref: str) -> Path:
        rel = ref[len("/static/"):]
        root = cls.BUILT if rel.split("/", 1)[0] == "dist" else cls.PUBLIC
        return root / rel

    def _referenced(self) -> list[str]:
        import json
        import re

        html = self.SHELL.read_text(encoding="utf-8")
        out = set()
        # <script src>, <link href>, and anything an import map maps to. All
        # three are ways to name a file the browser will demand.
        for pattern in (r'<script[^>]+src="(/static/[^"]+)"',
                        r'<link[^>]+href="(/static/[^"]+)"'):
            out.update(re.findall(pattern, html))
        for block in re.findall(r'<script type="importmap">(.*?)</script>',
                                html, re.S):
            try:
                out.update(json.loads(block).get("imports", {}).values())
            except (ValueError, AttributeError):
                continue
        return sorted(one for one in out if one.startswith("/static/"))

    def test_the_shell_references_something(self):
        """A guard on the guard: a regex that matched nothing would pass this
        whole class silently, which is the failure it exists to prevent."""
        assert len(self._referenced()) > 10

    def test_every_referenced_asset_is_on_disk(self):
        missing = [ref for ref in self._referenced()
                   if not self._resolve(ref).is_file()]
        assert not missing, (
            "index.html points at files that do not exist, so the browser gets "
            "a 404 and the feature they belong to is silently dead:\n  "
            + "\n  ".join(missing))

    def test_every_referenced_asset_is_tracked_by_git(self):
        """On disk is not enough — a fresh clone gets only what git carries."""
        if not (REPO / ".git").exists():
            pytest.skip("not a git checkout")
        import subprocess

        untracked = []
        for ref in self._referenced():
            path = self._resolve(ref)
            if not path.is_file():
                continue        # the test above owns that failure
            found = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(path)],
                cwd=REPO, capture_output=True, text=True)
            if found.returncode != 0:
                untracked.append(ref)
        assert not untracked, (
            "these exist here but git does not carry them, so a fresh clone "
            "gets a 404 (check .gitignore for a pattern that matches them, and "
            "`git add -f` if it is deliberate):\n  " + "\n  ".join(untracked))


class TestDeclarations:
    def test_data_only_trees_are_installed_packages(self, cfg):
        """templates/ carries no .py, but scaffold.py resolves
        ``__file__/../../templates`` — so it must land in site-packages next
        to the code packages or the scaffolder cannot find them. It lives in
        src/ beside them for exactly that reason."""
        find = cfg["tool"]["setuptools"]["packages"]["find"]
        assert find["where"] == ["src"]
        assert "templates" in find["include"]
        # __init__-less dirs are only collectable as namespace packages.
        assert find.get("namespaces") is True

    def test_route_subpackages_are_included(self, cfg):
        """bgate_ui.routes is auto-discovered at runtime; a wildcard include is
        what keeps a NEW route module from silently missing the wheel."""
        include = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
        assert "bgate_ui*" in include
        assert (REPO / "src" / "bgate_ui" / "routes" / "__init__.py").is_file()

    def test_tests_are_not_shipped(self, cfg):
        exclude = cfg["tool"]["setuptools"]["packages"]["find"].get("exclude", [])
        assert any(p.startswith("tests") for p in exclude)

    @pytest.mark.parametrize("pkg,tree,prefix,exts", SHIPPED_TREES,
                             ids=[t[0] for t in SHIPPED_TREES])
    def test_every_runtime_data_file_is_declared(self, cfg, pkg, tree, prefix, exts):
        patterns = _package_data(cfg)[pkg]
        missed = [
            prefix + str(p.relative_to(tree)).replace("\\", "/")
            for p in tree.rglob("*")
            if p.is_file() and p.suffix in exts and "__pycache__" not in p.parts
            and not _matches_any(
                prefix + str(p.relative_to(tree)).replace("\\", "/"), patterns)
        ]
        assert not missed, f"{pkg} package-data {patterns} misses: {missed}"

    def test_nested_static_seats_covered(self, cfg):
        """The regression that motivated all of this: ``static/*.html`` matched
        one file and nothing under ``static/seats/``. Named from the source tree
        (frontend/public/seats), which the build copies to static/seats."""
        seats = list((REPO / "frontend" / "public" / "seats").glob("*.js"))
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
        on_disk = {f"bgate_ui/static/{p.relative_to(REPO / 'src' / 'bgate_ui' / 'static')}"
                   .replace("\\", "/")
                   for p in (REPO / "src" / "bgate_ui" / "static").rglob("*.js")}
        assert on_disk, "no JS in the source tree?"
        assert on_disk <= js, f"missing from wheel: {sorted(on_disk - js)}"
        assert "bgate_ui/static/index.html" in names

    def test_wheel_contains_scaffold_templates(self, names):
        for expected in ("templates/2d/project.godot",
                         "templates/3d/project.godot",
                         "templates/shared/addons/bgate/bgate_telemetry.gd"):
            assert expected in names, expected

    def test_wheel_contains_route_modules(self, names):
        routes = {n for n in names if n.startswith("bgate_ui/routes/")
                  and n.endswith(".py")}
        assert "bgate_ui/routes/__init__.py" in routes
        assert len(routes) > 1, "route modules missing from the wheel"

    def test_wheel_ships_no_bytecode_or_tests(self, names):
        assert not [n for n in names if n.endswith(".pyc")]
        assert not [n for n in names if n.startswith("tests/")]


# ---------------------------------------------------------------------------
# Declared dependencies vs. what the code actually imports
# ---------------------------------------------------------------------------
# numpy was declared ONLY by the `record` extra while bgate_core/art/propsheet.py,
# retrodiffusion's background keying and the wall-tile tone match all imported
# it unguarded. Every developer machine had it — sounddevice pulls it in, and
# so does half of scientific Python — so the art pipeline worked everywhere it
# was written and would have raised ImportError on the first clean install.
# Linux CI caught it, which is luck: CI installs `.[dev]` and happened not to
# drag numpy in. This test is the guard that does not depend on luck.
#
# It is the same failure the `comtypes` comment in pyproject.toml describes, so
# it has now happened twice.

#: Third-party top-level modules the shipped packages may import WITHOUT the
#: import being guarded by try/except ImportError. Each maps to why it is
#: guaranteed to be installed. A new name here is a decision — declare it in
#: `dependencies`, guard the import, or add it with a reason.
ALLOWED_UNGUARDED = {
    # direct, in [project.dependencies]
    "mcp": "dependencies", "fastapi": "dependencies", "uvicorn": "dependencies",
    "PIL": "dependencies (Pillow)", "openai": "dependencies",
    "numpy": "dependencies",
    # guaranteed BY a direct dependency rather than named itself. Kept honest
    # rather than moved into `dependencies`: pinning a transitive here would
    # be a second floor to keep in step with the package that really owns it.
    "anyio": "installed by mcp and by fastapi/starlette",
    "pydantic": "installed by fastapi and by mcp",
    "starlette": "installed by fastapi",
    # NOT pip-installable: bpy exists only inside Blender's own interpreter,
    # which is the only thing that ever runs these modules.
    "bpy": "provided by Blender, never by pip",
    # optional FEATURE modules, whose extra is what installs them. Importing
    # unguarded inside the feature's own module is correct: the feature cannot
    # work without it, and its available() probe is what reports the absence.
    "sounddevice": "extra: record", "websockets": "extra: voice",
}

#: Files exempt from the rule above because they ARE the optional feature.
OPTIONAL_MODULES = {
    "bgate_adapters/recorder.py": "record",
    "bgate_adapters/deepgram.py": "voice",
    "bgate_adapters/_blender_runner.py": "runs inside Blender",
    "bgate_adapters/_whisper_runner.py": "stt",
}

SHIPPED_PACKAGES = ("bgate_core", "bgate_adapters", "bgate_mcp", "bgate_ui",
                    "bgate_cli")


def _unguarded_imports() -> dict[str, list[str]]:
    """Third-party modules imported outside a try/except, by file."""
    import ast

    out: dict[str, list[str]] = {}
    for pkg in SHIPPED_PACKAGES:
        for path in sorted((REPO / pkg).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel in OPTIONAL_MODULES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            spans = [(t.lineno, t.end_lineno or t.lineno)
                     for t in ast.walk(tree) if isinstance(t, ast.Try)]
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                if any(a <= node.lineno <= b for a, b in spans):
                    continue          # guarded: its absence is handled
                for name in names:
                    if (not name or name in sys.stdlib_module_names
                            or name.startswith("bgate")):
                        continue
                    out.setdefault(name, []).append(f"{rel}:{node.lineno}")
    return out


def test_every_unguarded_import_is_guaranteed_to_be_installed():
    """An import nothing declares is an ImportError on somebody else's machine.

    This is the check that would have caught numpy: present everywhere it was
    written, declared only by an extra nobody installs for art.
    """
    found = _unguarded_imports()
    undeclared = {name: sites for name, sites in found.items()
                  if name not in ALLOWED_UNGUARDED}
    assert not undeclared, (
        "these modules are imported without a try/except and are not known to "
        "be installed: "
        + "; ".join(f"{n} ({', '.join(s[:2])})" for n, s in
                    sorted(undeclared.items()))
        + ". Declare it in [project.dependencies], guard the import, or add it "
          "to ALLOWED_UNGUARDED with the reason it is always there.")


def test_the_core_dependencies_actually_declare_what_they_claim(cfg):
    """Everything ALLOWED_UNGUARDED calls `dependencies` really is one."""
    declared = {re.split(r"[<>=!\[]", d, maxsplit=1)[0].strip().lower()
                for d in cfg["project"]["dependencies"]}
    aliases = {"pil": "pillow"}
    for name, why in ALLOWED_UNGUARDED.items():
        if not why.startswith("dependencies"):
            continue
        key = aliases.get(name.lower(), name.lower())
        assert key in declared, (
            f"{name} is listed as a core dependency in ALLOWED_UNGUARDED but "
            f"[project.dependencies] does not name it — declared: "
            f"{sorted(declared)}")


# ---------------------------------------------------------------------------
# The frozen bundle, which has its own way of not containing the product
# ---------------------------------------------------------------------------

SPEC = REPO / "packaging" / "bgate.spec"

# `Path(__file__).with_name("x.py")` in an adapter means "there is a real file
# beside me". Nothing about being importable provides that.
_SIBLING = re.compile(r"""with_name\(\s*["']([^"']+\.py)["']\s*\)""")


def _adapter_siblings() -> dict[str, list[str]]:
    """Every .py an adapter resolves beside itself at run time, and where."""
    out: dict[str, list[str]] = {}
    for source in sorted((REPO / "src" / "bgate_adapters").glob("*.py")):
        for name in _SIBLING.findall(source.read_text(encoding="utf-8")):
            out.setdefault(name, []).append(source.name)
    return out


def test_every_adapter_read_from_disk_is_shipped_in_the_frozen_bundle():
    """A module PyInstaller can import is not a file PyInstaller wrote down.

    `collect_submodules` compiles bgate_adapters into the archive and puts no
    .py on disk, and four adapters are never imported for their behaviour —
    three are handed to another interpreter by PATH (Blender's `--python`, a
    whisper subprocess) and one has its text spliced into a generated script.

    Verified against a shipped bundle before the spec listed them:
    dist/BuildersGate/_internal held no bgate_adapters directory at all, so
    every `Path(__file__).with_name(...)` in that package resolved to nothing
    and all of modelling, rigging, sprite baking and transcription were dead in
    the packaged app. Nothing caught it because nothing ran the frozen binary —
    the same reason the wheel shipped without its own static/ and templates/,
    which is what the rest of this file exists for.
    """
    spec = SPEC.read_text(encoding="utf-8")
    missing = {name: readers for name, readers in _adapter_siblings().items()
               if name not in spec}
    assert not missing, (
        "these files are resolved beside their module at run time but are not "
        "named in packaging/bgate.spec, so the frozen app will not have them: "
        + "; ".join(f"{name} (read by {', '.join(readers)})"
                    for name, readers in sorted(missing.items()))
        + ". Add them to the `datas` list that ships bgate_adapters sources, "
          "with destination 'bgate_adapters' so with_name() resolves.")


def test_the_shipped_adapter_sources_still_exist():
    """The other direction: a spec naming a file nobody kept is a build that
    fails late, on a machine that is not the developer's."""
    spec = SPEC.read_text(encoding="utf-8")
    block = spec.split("datas += [", 1)[-1].split("]", 1)[0]
    for name in re.findall(r"""["']([^"']+\.py)["']""", block):
        assert (REPO / "src" / "bgate_adapters" / name).is_file(), (
            f"packaging/bgate.spec ships bgate_adapters/{name} and no such "
            "file exists")


ISS = REPO / "packaging" / "installer.iss"

# One optional component's payload: Source: "{#SourceDir}\_internal\<what>\*"
_ISS_SOURCE = re.compile(
    r'Source:\s*"\{#SourceDir\}\\_internal\\([^"*]+?)\\\*"\s*;')
# The core row's Excludes list, which must name exactly the same paths.
_ISS_EXCLUDES = re.compile(r'Excludes:\s*"([^"]+)"')


def _iss_component_paths() -> set[str]:
    """The `_internal` subpaths the installer ships as optional components."""
    return {one.strip("\\").lower()
            for one in _ISS_SOURCE.findall(ISS.read_text(encoding="utf-8"))}


def _iss_excluded_paths() -> set[str]:
    """The `_internal` subpaths the core row carves out for those components."""
    out: set[str] = set()
    for blob in _ISS_EXCLUDES.findall(ISS.read_text(encoding="utf-8")):
        for one in blob.split(","):
            one = one.strip().lstrip("\\").lower()
            if one.startswith("_internal\\"):
                out.add(one[len("_internal\\"):])
    return out


def test_the_installers_component_payloads_and_its_exclusions_agree():
    """Two lists, one fact, and nothing compared them.

    A component in packaging/installer.iss is TWO edits: the core row excludes
    the path, and the component's own row ships it. Drop the first and the
    payload is installed for everyone, so the component is a lie; drop the
    second and it is installed for nobody.
    """
    shipped, excluded = _iss_component_paths(), _iss_excluded_paths()
    assert shipped == excluded, (
        "packaging/installer.iss ships and excludes different paths — shipped "
        f"by a component but not excluded from core: {sorted(shipped - excluded)}; "
        f"excluded from core but shipped by nothing: {sorted(excluded - shipped)}")


def test_every_installer_component_payload_is_something_the_spec_ships():
    """The failure this exists for, observed on a real pull request.

    The floor's art moved out of the dashboard's static tree into its own
    distribution. packaging/bgate.spec learned about it; installer.iss did not,
    so ISCC matched zero files for two `Source:` wildcards and failed the build
    at the very last step — after a nine-minute PyInstaller run, with no error
    text naming the paths. The spec is the only thing that decides what exists
    under `_internal`, so this asks it directly.
    """
    spec = SPEC.read_text(encoding="utf-8").replace("\\", "/")
    for path in sorted(_iss_component_paths()):
        top = path.split("\\")[0]
        # Third-party packages land under _internal by being imported, not by
        # a datas entry; only our own payloads are the spec's to name.
        if not top.startswith(("bgate", "builders_gate")):
            continue
        assert path.replace("\\", "/") in spec, (
            f"installer.iss ships _internal\\{path} as a component payload "
            "and packaging/bgate.spec puts nothing there, so ISCC will match "
            "no files and fail the build")


def test_the_floor_assets_the_installer_ships_exist_in_the_checkout():
    """The floor component's payload is the assets package — a real directory
    in this repo, not a pip install that happens at build time."""
    pkg = REPO / "packaging" / "floor-assets" / "builders_gate_floor_assets"
    assert (pkg / "__init__.py").is_file()
    for sub in ("img", "audio"):
        assert (pkg / sub).is_dir(), f"the floor pack has no {sub}/"
        assert any((pkg / sub).rglob("*.*")), f"the floor pack's {sub}/ is empty"
