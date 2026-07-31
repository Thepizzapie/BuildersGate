"""Prove the WHEEL is the product — not just that the .py files import.

`pip install builders-gate` has shipped broken before: once with no JavaScript
under `bgate_ui/static` (every dashboard asset 404'd), once with no `templates/`
at all (`bgate init` raised FileNotFoundError), once with no engine schemas.
Nothing caught any of them, because nothing in CI ever installed the wheel — the
suite runs against the checkout, where every one of those trees is simply there
on disk whether or not the packaging declares it.

tests/test_packaging.py is the fast half of that guard: it reads pyproject and
asserts every shipped tree is covered by a package-data pattern, without
building anything. This is the slow half, and the half that cannot be fooled by
a pattern that looks right — it runs against an installed wheel in a clean
interpreter with the checkout deliberately off sys.path, and then drives the
installed console script the way a user would.

    python -m build --wheel
    <clean venv>/python -m pip install dist/builders_gate-*.whl
    <clean venv>/python packaging/smoke_wheel.py

RUN IT WITH THE VENV'S INTERPRETER, not the checkout's. It is invoked as a
script (not `-m`) on purpose: that puts packaging/ on sys.path[0] and leaves the
repo root off it, so `import bgate_core` can only resolve to site-packages. The
first check refuses to run if it resolved anywhere else, because a smoke test
that silently tested the checkout is worse than no smoke test.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_exe import serve_and_fetch                          # noqa: E402

# Every entry is a file the runtime READS at request or scaffold time and cannot
# regenerate — the ones whose absence is invisible until a user hits that exact
# surface. Named individually rather than counted: a count stays green while the
# wrong file is missing.
REQUIRED_FILES = [
    # The dashboard. app.css and index.html went missing together once; the
    # seat panel and the agents console are separate globs under static/.
    "bgate_ui/static/app.css",
    "bgate_ui/static/index.html",
    "bgate_ui/static/bgselect.js",
    "bgate_ui/static/seats/art.js",
    "bgate_ui/static/agents_console.js",
    "bgate_ui/static/img/mascot.png",
    # THE DOTFILE. `**/*` does not match it, so it has to be named explicitly in
    # package-data, and without it the project this scaffolds does not ignore
    # .env — which is the failure that has already put an API key in a commit.
    "templates/shared/.gitignore",
    # Greets the user inside their own game project.
    "templates/shared/CLAUDE.md",
]

# (directory, extension, how many at least). Trees too large to name file by
# file, where "empty" is the only failure that matters.
REQUIRED_TREES = [
    ("bgate_engine/schemas", ".json", 1),
    ("bgate_site/theme", "", 1),
    ("bgate_ui/static", ".js", 10),
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}" if detail else label)


def installed_root() -> Path:
    """The site-packages directory the wheel unpacked into.

    bgate_engine/ and templates/ carry no .py at all, so they are not importable
    and can only be found relative to a package that is — the same walk
    bgate_core/scaffold.py does at runtime (``__file__/../../templates``). If
    that walk lands somewhere without them, the user's install is broken in
    exactly the way this script exists to catch.
    """
    import bgate_core

    pkg = Path(bgate_core.__file__).resolve().parent
    if "site-packages" not in pkg.parts:
        sys.exit(
            f"REFUSING TO RUN: bgate_core resolved to {pkg}\n"
            "That is a source checkout, not an installed wheel, and every check\n"
            "below would pass for the wrong reason. Run this with the clean\n"
            "venv's interpreter."
        )
    return pkg.parent


def bgate_script() -> Path:
    """The installed console script, which is itself part of what ships."""
    bindir = Path(sys.executable).resolve().parent
    for name in ("bgate.exe", "bgate"):
        cand = bindir / name
        if cand.is_file():
            return cand
    sys.exit(f"[project.scripts] did not install a bgate entry point into {bindir}")


def main() -> int:
    root = installed_root()
    bgate = bgate_script()
    print(f"wheel smoke: {root}")
    print(f"entry point: {bgate}\n")

    print("shipped files")
    for rel in REQUIRED_FILES:
        p = root / rel
        ok = p.is_file() and p.stat().st_size > 0
        check(rel, ok, "" if ok else ("missing" if not p.exists() else "empty"))

    print("\nshipped trees")
    for rel, ext, least in REQUIRED_TREES:
        d = root / rel
        found = [p for p in d.rglob(f"*{ext}") if p.is_file()] if d.is_dir() else []
        check(f"{rel}/*{ext or '*'} >= {least}", len(found) >= least,
              f"found {len(found)}")

    # ignore_cleanup_errors because the scaffolded project's SQLite handle
    # outlives the server process on Windows, and a WinError 32 from the
    # teardown would fail a run whose every actual check passed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)

        print("\ndoctor")
        # THE EXIT CODE IS NOT THE SIGNAL. doctor exits 1 when anything on its
        # list is absent, and a CI runner has neither Blender nor an API key, so
        # a non-zero exit here is the normal, healthy answer. What is being
        # tested is that it runs at all out of the wheel and reports structured
        # rows — the CONTRIBUTING bug-report flow depends on --json working on a
        # machine where nothing else does.
        r = subprocess.run([str(bgate), "doctor", "--json"], cwd=work,
                           capture_output=True, text=True, timeout=180)
        try:
            rows = json.loads(r.stdout)
        except ValueError as exc:
            rows = {}
            check("doctor --json is JSON", False, f"{exc}: {r.stdout[:200]!r}")
        else:
            check("doctor --json is JSON", True)
        check("doctor reports python", bool(rows.get("python", {}).get("available")),
              json.dumps(rows.get("python", "absent"))[:120])

        print("\nscaffold")
        # The one check that proves templates/ shipped AND is reachable by the
        # runtime's own resolution, rather than merely present in the archive.
        r = subprocess.run([str(bgate), "init", "smoketest", "--kind", "2d"],
                           cwd=work, capture_output=True, text=True, timeout=300)
        proj = work / "smoketest"
        check("bgate init exits 0", r.returncode == 0,
              (r.stdout + r.stderr)[-400:] if r.returncode else "")
        for rel in ("project.godot", ".bgate/game.db", ".gitignore"):
            check(f"scaffolded {rel}", (proj / rel).exists())
        scenes = list(proj.rglob("*.tscn")) if proj.is_dir() else []
        check("scaffolded at least one scene", bool(scenes), f"found {len(scenes)}")

        if proj.is_dir():
            print("\nserve")
            # Same path list the exe is held to, so a dashboard asset added to
            # one artifact's smoke test cannot be missing from the other's.
            serve_and_fetch([str(bgate)], proj, "the installed wheel")

    if failures:
        print("\nwheel smoke FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nwheel smoke passed — the wheel contains the product")
    return 0


if __name__ == "__main__":
    sys.exit(main())
