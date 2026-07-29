"""Build BuildersGate.exe, then prove the thing actually works.

    python packaging/build_exe.py [--skip-smoke]

A PyInstaller build that "succeeds" tells you almost nothing: the failure mode
for this app is a binary that starts, serves index.html, and 404s every asset —
because the data trees are resolved by walking up from __file__ and the bundle
laid them out somewhere else. So the build is followed by a smoke test that
boots the real server out of the frozen exe and fetches real files.
"""
from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
    ("/static/seats/art.js", "javascript"),
    # The Agents console: two modules and a binary asset, none of which any
    # other smoke path would touch.
    ("/static/agents_console.js", "javascript"),
    ("/static/agents_graph.js", "javascript"),
    ("/static/img/mascot.png", "image/png"),
    ("/api/state", "json"),
    ("/api/routes/status", "json", _routes_ok),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build() -> None:
    if not SPEC.is_file():
        sys.exit(f"missing spec: {SPEC}")
    print(f"building from {SPEC.relative_to(ROOT)} …")
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm",
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


def smoke() -> None:
    """Boot the frozen exe in server mode and fetch real files out of it."""
    port = free_port()
    print(f"smoke test: {EXE.name} serve --port {port}")
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                sys.exit(f"exe exited early (code {proc.returncode}):\n{out}")
            try:
                urllib.request.urlopen(base + "/", timeout=1).read()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.25)
        else:
            sys.exit("exe never started serving within 60s")

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
        print("smoke test passed — the exe serves its own assets")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-smoke", action="store_true",
                    help="build only; do not boot the exe")
    ap.add_argument("--clean", action="store_true",
                    help="remove build/ and dist/ first")
    a = ap.parse_args()
    if a.clean:
        for d in (ROOT / "build" / "pyi", DIST):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
    build()
    # Smoke FIRST, then zip — never package something that could not serve.
    if not a.skip_smoke:
        smoke()
    package()
    print(f"\n{ZIP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
