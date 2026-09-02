"""Build BuildersGate.exe, then prove the thing actually works.

    python packaging/build_exe.py [--skip-smoke] [--no-isolate]

A PyInstaller build that "succeeds" tells you almost nothing: the failure mode
for this app is a binary that starts, serves index.html, and 404s every asset —
because the data trees are resolved by walking up from __file__ and the bundle
laid them out somewhere else. So the build is followed by a smoke test that
boots the real server out of the frozen exe and fetches real files.

THE BUILD RUNS IN ITS OWN VENV. PyInstaller bundles what it can reach from the
interpreter that runs it, and that interpreter is normally a developer's daily
one with years of unrelated packages in it. Measured on one such machine: the
same commit produced a 59 MB zip locally and 37 MB from CI, because an installed
azure-core dragged in the whole opentelemetry exporter stack and with it grpc
and protobuf. Nothing in the spec was wrong and nothing looked broken — the
release was simply 22 MB of somebody else's SDK.

So the default is to build inside build/venv, holding exactly the dependencies
pyproject.toml declares. `--no-isolate` uses the current interpreter, which is
faster for iterating on the spec and is NOT what a release should be cut with.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "bgate.spec"
DIST = ROOT / "dist"
# onedir: PyInstaller writes dist/BuildersGate/ with the exe inside it, not a
# single dist/BuildersGate.exe. See the note at the top of bgate.spec for why
# onefile was abandoned (Defender's ML quarantined it).
APPDIR = DIST / "BuildersGate"
EXE = APPDIR / "BuildersGate.exe"
ZIP = DIST / "BuildersGate-windows.zip"

def _routes_ok(body: bytes) -> str:
    """Every route module imported inside the frozen bundle.

    THE FAILURE THIS EXISTS FOR: routes/__init__.py discovers its modules with
    pkgutil.iter_modules, which walks a directory. PyInstaller freezes imports,
    so a module nothing statically imports is simply absent from the bundle —
    and the discovery loop records the miss and carries on, by design, so half
    the API can be gone while the dashboard looks perfectly healthy. Fetching a
    page cannot see that. Asking the registry can.
    """
    import json
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return f"unreadable: {exc}"
    if data.get("failed"):
        return "route modules failed to import: " + ", ".join(
            f"{f.get('module')} ({f.get('error')})" for f in data["failed"])
    registered = set(data.get("registered") or [])
    # Named explicitly rather than counted: a count passes while the wrong
    # module is missing.
    missing = {"console", "orchestrator", "workspace_doc", "library"} - registered
    if missing:
        return f"missing route modules: {sorted(missing)}"
    return ""


# Every one of these is a real past failure: a wheel with no JS, a wheel with
# no templates, a bundle that dropped a route module. The exe can regress the
# same way. A third element is an extra check on the body — status 200 with the
# right content type is not proof for anything that reports on itself.
SMOKE_PATHS = [
    ("/", "text/html"),
    ("/static/app.css", "text/css"),
    ("/static/index.html", "text/html"),
    ("/static/bgselect.js", "javascript"),
    # _core.js, not a per-seat panel: the eight seat workspaces are React now
    # (frontend/src/shell/seats/) and their classic modules are deleted. This
    # one stayed because it also exports BGWS and SeatStage, which nine other
    # views use — see the comment above its script tag in index.html.
    ("/static/seats/_core.js", "javascript"),
    # The Agents console: the React bundle that renders it, the graph module it
    # mounts, and a binary asset — none of which any other smoke path touches.
    # (agents_console.js was the classic console and is deleted; the console is
    # frontend/src/shell/agents, shipped inside dist/bgate.js.)
    ("/static/dist/bgate.js", "javascript"),
    ("/static/agents_graph.js", "javascript"),
    ("/static/img/mascot.png", "image/png"),
    ("/api/state", "json"),
    ("/api/routes/status", "json", _routes_ok),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# bgate_ui/static is GENERATED — `npm run build` in frontend/ copies
# frontend/public/* into it and emits dist/bgate.{js,css}. bgate.spec bundles
# that directory wholesale, and PyInstaller is perfectly happy to bundle an
# empty one: the exe then starts, serves nothing, and only the smoke test at
# the far end of a multi-minute build says so. Check it before spending the
# build. The list is the smoke paths' on-disk counterparts.
BUILT_ASSETS = [
    "app.css",
    "index.html",
    "bgselect.js",
    "seats/_core.js",
    "agents_graph.js",
    "img/mascot.png",
    "dist/bgate.js",
    "dist/bgate.css",
]


def check_frontend_built() -> None:
    static = ROOT / "src" / "bgate_ui" / "static"
    missing = [rel for rel in BUILT_ASSETS
               if not (static / rel).is_file() or (static / rel).stat().st_size == 0]
    if missing:
        sys.exit(
            "bgate_ui/static is not built — missing/empty: " + ", ".join(missing)
            + "\nThat tree is BUILD OUTPUT (source is frontend/public + frontend/src)."
            + "\nRun:  cd frontend && npm ci && npm run build"
        )


VENV = ROOT / "build" / "venv"
# What the frozen app actually needs: the project, the native window, the
# builder, and mic capture.
#
# `record` IS INCLUDED AND WAS THE BUG. It was left out as "an optional extra",
# which read as reasonable and was not: bgate_adapters/recorder.py captures
# playtest audio through sounddevice — not through ffmpeg, deliberately, because
# ffmpeg's dshow enumeration finds no devices on the machines this was built on.
# So excluding the extra did not make audio optional, it made PLAYTEST RECORDING
# IMPOSSIBLE in every packaged build ever shipped. The Playtests screen sat
# permanently on "record unavailable", advising a `pip install` that a .exe user
# cannot run.
#
# It costs ~27 MB, almost all of it numpy's bundled OpenBLAS. That is the price
# of a feature with its own screen in the navigation working at all.
#
# Still NOT included: `dev` (test-only), `stt` (torch + CUDA, hundreds of MB,
# and genuinely optional — a recording without a transcript is still a
# recording), `voice` (websockets, a separate feature).
#
# EDITABLE, so the analysis reads the working tree rather than a copy pip made
# at install time. A non-editable install would freeze bgate_ui/static as it was
# when the venv was last populated, which is the one directory that changes on
# every front-end build — and the failure would be a release quietly shipping
# last week's dashboard.
VENV_INSTALL = ["-e", ".[desktop,build,record,stt]"]
LOCK = ROOT / "packaging" / "build-requirements.lock"


def venv_python() -> Path:
    d = VENV / ("Scripts" if sys.platform == "win32" else "bin")
    return d / ("python.exe" if sys.platform == "win32" else "python")


def isolated_python(*, refresh: bool) -> Path:
    """The interpreter a release is built with: only the declared dependencies.

    Reused across builds — creating it costs a minute of downloads and its
    contents are pinned by pyproject.toml, not by what happened to be cached.
    `--refresh-venv` rebuilds it from nothing, which is what to reach for after
    changing a dependency.
    """
    py = venv_python()
    if refresh and VENV.exists():
        print(f"removing {VENV.relative_to(ROOT)} …")
        shutil.rmtree(VENV, ignore_errors=True)
    if not py.is_file():
        print(f"creating build venv at {VENV.relative_to(ROOT)} …")
        venv.create(VENV, with_pip=True, clear=True)
    # HASH-PINNED, IN TWO STEPS, AND THE ORDER MATTERS.
    #
    # The build venv is the only thing that decides what is in the release —
    # bgate.spec bundles whatever that interpreter can import. Resolving
    # `.[extras]` against PyPI at build time meant the same commit could
    # produce different binaries on different days, and a compromised release
    # of any transitive dependency would be packaged and shipped under our name
    # with nothing downstream able to notice (the output is unsigned).
    #
    # --require-hashes refuses any artifact whose bytes do not match the lock
    # AND refuses anything not listed at all, so a dependency appearing out of
    # nowhere fails the build instead of joining the bundle.
    #
    # The project itself goes in second with --no-deps: an editable directory
    # has no artifact to hash, and --require-hashes applies to the whole
    # command, so the two cannot share an invocation.
    if not LOCK.is_file():
        sys.exit(f"missing {LOCK.relative_to(ROOT)} — "
                 f"run: python scripts/lock_build_deps.py")

    print(f"installing {LOCK.name} (hash-pinned) into the build venv …")
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--require-hashes",
         "-r", str(LOCK)],
        cwd=ROOT,
    )
    if r.returncode != 0:
        sys.exit(
            f"the pinned dependency install failed (pip exit {r.returncode}).\n"
            "If pyproject.toml changed, regenerate the lock and READ THE DIFF:\n"
            "    python scripts/lock_build_deps.py")

    print("installing the project itself (--no-deps) …")
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--no-deps", "-e", "."],
        cwd=ROOT,
    )
    if r.returncode != 0:
        sys.exit(f"could not install the project (pip exit {r.returncode})")
    return py


def report_environment(py: Path) -> None:
    """Print the dependency closure the bundle will be cut from.

    A build log that does not say what was installed cannot answer the only
    question that matters when a release comes out unexpectedly large.
    """
    r = subprocess.run([str(py), "-m", "pip", "list", "--format=freeze"],
                       capture_output=True, text=True)
    names = sorted(line.split("==")[0] for line in r.stdout.splitlines() if line)
    print(f"build environment: {len(names)} packages")
    print("  " + ", ".join(names))


def build(python: Path) -> None:
    if not SPEC.is_file():
        sys.exit(f"missing spec: {SPEC}")
    check_frontend_built()
    print(f"building from {SPEC.relative_to(ROOT)} …")
    r = subprocess.run(
        [str(python), "-m", "PyInstaller", str(SPEC), "--noconfirm",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyi")],
        cwd=ROOT,
    )
    if r.returncode != 0:
        sys.exit(f"PyInstaller failed (exit {r.returncode})")
    if not EXE.is_file():
        sys.exit(f"build reported success but {EXE} is not there")
    total = sum(p.stat().st_size for p in APPDIR.rglob("*") if p.is_file())
    print(f"built {APPDIR.relative_to(ROOT)}/ — "
          f"{total / (1024 * 1024):.1f} MB across "
          f"{sum(1 for p in APPDIR.rglob('*') if p.is_file())} files")


def package() -> None:
    """Zip the app directory — a folder is not a download.

    Note this is the ONLY compression in the pipeline: the binaries themselves
    are left uncompressed on disk (no UPX), because re-packing them into
    high-entropy blobs is what got the onefile build quarantined.
    """
    if ZIP.exists():
        ZIP.unlink()
    shutil.make_archive(str(ZIP.with_suffix("")), "zip",
                        root_dir=str(DIST), base_dir=APPDIR.name)
    mb = ZIP.stat().st_size / (1024 * 1024)
    print(f"packaged {ZIP.relative_to(ROOT)} — {mb:.1f} MB")
    # Publish this next to the download so anyone can check what they got.
    import hashlib
    h = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    (DIST / "BuildersGate-windows.zip.sha256").write_text(
        f"{h}  {ZIP.name}\n", encoding="utf-8")
    print(f"sha256 {h}")


ISS = ROOT / "packaging" / "installer.iss"
SETUP = DIST / "BuildersGate-setup.exe"

# Inno Setup's command-line compiler. Never on PATH after any of its installs,
# so the three places it actually lands are checked before giving up. The
# LOCALAPPDATA one is not exotic: it is where `winget install
# JRSoftware.InnoSetup` puts it, because winget prefers a per-user install and
# the installer obliges. Checking only the two Program Files paths reported "not
# found" on a machine where it had just been installed successfully.
_ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
)


def find_iscc() -> Path | None:
    for p in _ISCC_CANDIDATES:
        if p.is_file():
            return p
    found = shutil.which("iscc") or shutil.which("ISCC")
    return Path(found) if found else None


def project_version() -> str:
    """The version out of pyproject.toml.

    Read rather than restated so the installer, the wheel and the exe cannot
    claim three different versions — which matters more than it sounds, because
    Windows uninstalls and upgrades BY version against a fixed AppId.
    """
    import tomllib
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def installer() -> None:
    """Compile dist/BuildersGate-setup.exe from the built app directory."""
    iscc = find_iscc()
    if iscc is None:
        sys.exit(
            "Inno Setup 6 not found — cannot build the installer.\n"
            "  winget install JRSoftware.InnoSetup\n"
            "or drop --installer to publish the zip alone."
        )
    if not APPDIR.is_dir():
        sys.exit(f"nothing to package: {APPDIR} is not there")
    version = project_version()
    print(f"compiling installer {version} with {iscc.name} …")
    r = subprocess.run(
        [str(iscc), f"/DAppVersion={version}", str(ISS)],
        cwd=str(ISS.parent),
    )
    if r.returncode != 0:
        sys.exit(f"Inno Setup failed (exit {r.returncode})")
    if not SETUP.is_file():
        sys.exit(f"compiler reported success but {SETUP} is not there")
    import hashlib
    h = hashlib.sha256(SETUP.read_bytes()).hexdigest()
    (DIST / "BuildersGate-setup.exe.sha256").write_text(
        f"{h}  {SETUP.name}\n", encoding="utf-8")
    print(f"built {SETUP.relative_to(ROOT)} — "
          f"{SETUP.stat().st_size / (1024 * 1024):.1f} MB")
    print(f"sha256 {h}")


def serve_and_fetch(cmd: list[str], cwd: Path, label: str) -> None:
    """Boot ``cmd`` as a dashboard server and fetch SMOKE_PATHS out of it.

    Split out of smoke() because the wheel can regress in exactly the same way
    the exe can, for a different reason (package-data patterns rather than
    PyInstaller's import graph) and with the same symptom — a server that starts
    and 404s its own assets. packaging/smoke_wheel.py is the other caller. One
    path list and one fetch loop, so a check added for one artifact is not
    silently absent from the other.
    """
    port = free_port()
    print(f"smoke test: {label} serve --port {port}")
    proc = subprocess.Popen(
        [*cmd, "serve", "--port", str(port)],
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                sys.exit(f"{label} exited early (code {proc.returncode}):\n{out}")
            try:
                urllib.request.urlopen(base + "/", timeout=1).read()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.25)
        else:
            sys.exit(f"{label} never started serving within 60s")

        bad = []
        for entry in SMOKE_PATHS:
            path, want = entry[0], entry[1]
            check = entry[2] if len(entry) > 2 else None
            try:
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    body = r.read()
                    ctype = r.headers.get("content-type", "")
                    ok = r.status == 200 and len(body) > 0 and want in ctype
                    why = check(body) if (ok and check) else ""
                    ok = ok and not why
                    print(f"  {'ok  ' if ok else 'FAIL'} {path:28s} "
                          f"{r.status} {len(body):>8,}B  {ctype}"
                          + (f"  {why}" if why else ""))
                    if not ok:
                        bad.append(f"{path} -> "
                                   + (why or f"{r.status} {ctype} {len(body)}B"))
            except Exception as exc:                            # noqa: BLE001
                print(f"  FAIL {path:28s} {type(exc).__name__}: {exc}")
                bad.append(f"{path} -> {exc}")

        if bad:
            sys.exit("smoke test FAILED:\n  " + "\n  ".join(bad))
        print(f"smoke test passed — {label} serves its own assets")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def smoke_detached() -> None:
    """Boot the exe THE WAY WINDOWS DOES and prove it still serves.

    Every other check here spawns the binary with subprocess pipes, which hands
    it a real stdout — and that is exactly the thing a double-click does not do.
    A console=False build launched from Explorer or a Start Menu shortcut gets
    ``sys.stdout is None``, and uvicorn's log formatter calls .isatty() on it
    during startup. The app died there, before binding a port, in both `serve`
    and window mode. The smoke test passed the whole time, because the harness
    was supplying the missing handle itself.

    DETACHED_PROCESS is the reproduction: no console, no inherited std handles.
    Nothing can be read back from the child, so success is measured the only
    way it can be — the port opens and answers.
    """
    if sys.platform != "win32":
        print("detached smoke: skipped (Windows-only failure mode)")
        return

    port = free_port()
    print(f"detached smoke: no console, no std handles, port {port}")
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(port)],
        cwd=str(ROOT),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=None, stderr=None, close_fds=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                crash = Path(os.environ.get("LOCALAPPDATA")
                             or Path.home()) / "BuildersGate-crash.log"
                detail = ""
                if crash.is_file():
                    detail = "\n" + crash.read_text(encoding="utf-8",
                                                    errors="replace")[-1500:]
                sys.exit(f"detached launch FAILED — exited {proc.returncode} "
                         f"with no console. This is what a double-click "
                         f"does.{detail}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/",
                                            timeout=1) as r:
                    body = r.read()
                print(f"  ok   served {len(body):,}B with no stdout at all")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.25)
        sys.exit("detached launch never served within 60s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def selftest() -> None:
    """Run the frozen exe's own window-stack check.

    `serve` proves the server and its data trees. It proves NOTHING about the
    desktop window, which is a separate import graph — pywebview, its
    EdgeChromium backend, pythonnet, the WebView2 loader DLL — that no HTTP
    request touches. The excludes in bgate.spec exist to shrink the download
    and every one of them is a chance to cut that graph instead, so it gets
    checked rather than reasoned about. See _selftest() in launcher.py.
    """
    print(f"selftest: {EXE.name} selftest")
    r = subprocess.run([str(EXE), "selftest"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    print("  " + "\n  ".join(out.splitlines()[:40]) if out else "  (no output)")
    if r.returncode != 0:
        sys.exit(f"selftest FAILED (exit {r.returncode})\n{r.stderr.strip()}")
    print("selftest passed — the window stack is in the bundle")


def smoke() -> None:
    """Boot the frozen exe in server mode and fetch real files out of it."""
    serve_and_fetch([str(EXE)], ROOT, EXE.name)
    smoke_detached()
    selftest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-smoke", action="store_true",
                    help="build only; do not boot the exe")
    ap.add_argument("--clean", action="store_true",
                    help="remove build/pyi and dist/ first (keeps the venv)")
    ap.add_argument("--no-isolate", action="store_true",
                    help="build with the current interpreter instead of a "
                         "dedicated venv — faster to iterate on the spec, and "
                         "bundles whatever else that interpreter can import")
    ap.add_argument("--refresh-venv", action="store_true",
                    help="rebuild the build venv from nothing first")
    ap.add_argument("--installer", action="store_true",
                    help="also compile dist/BuildersGate-setup.exe "
                         "(needs Inno Setup 6)")
    a = ap.parse_args()
    if a.clean:
        for d in (ROOT / "build" / "pyi", DIST):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    if a.no_isolate:
        python = Path(sys.executable)
        print("NOT ISOLATED — bundling from the current interpreter. Anything "
              "installed here that the app can reach ships in the release.")
    else:
        python = isolated_python(refresh=a.refresh_venv)
    report_environment(python)

    build(python)
    # Smoke FIRST, then zip — never package something that could not serve.
    if not a.skip_smoke:
        smoke()
    package()
    # Installer LAST, and only after the smoke test: an installer is a much
    # better way to spread a broken build than a zip is.
    if a.installer:
        installer()
    print(f"\n{ZIP}")
    if a.installer:
        print(SETUP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
