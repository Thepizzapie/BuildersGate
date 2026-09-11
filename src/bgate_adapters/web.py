"""The web engine — a TypeScript game that ships to a URL.

WHY THIS EXISTS, measured rather than assumed. A finished Godot 3D game
(Tommy Tomato Golf, 2026-09-08) was exported to the web to see how close it was:
it ran, and the payload was 661 MB — a 622 MB .pck plus a 38 MB .wasm, holding
1.27 GB of heap after load. Nothing in the pipeline had ever asked what a build
weighed, because for a desktop export nobody cares. On the web the payload IS
the product: past a few tens of megabytes there is no game, only a spinner.

That is the fact this adapter is shaped around. :func:`build` refuses a payload
over a declared budget the same way the Godot side refuses an unimported asset —
before it is somebody's surprise rather than after.

WHAT THIS ADAPTER IS NOT: a framework. The templates are vite + TypeScript with
no runtime dependency beyond what the game itself pulls in, because the thing
being verified here is "does the project build, run and weigh what it should",
and every layer between this and the browser is a layer that can lie about it.

THE PIECES, and which external thing each needs:
  find_binary / available   node on PATH. Nothing else works without it.
  check_project             `npm run build` (or `tsc --noEmit`). Needs node and
                            an installed node_modules.
  run_script                node, running a .ts/.js file through the project.
  dev_server                vite (or whatever `npm run dev` starts), held under
                            the engine lock like any other engine process.
  screenshot                Playwright, which is TWO installs — the Python
                            package and its browser build. They fail
                            differently and are reported differently; see
                            :func:`browser_available`.
  test_run                  `npm test` (vitest in the templates).
  build                     `npm run build` plus the payload measurement.

NODE IS NOT OPTIONAL AND NOT SEARCHED FOR. Unlike Godot, which ships as a
portable .exe that people extract anywhere (hence godot._SEARCH_GLOBS and the
0-byte-stub check), node installs itself onto PATH on every platform it
supports. shutil.which is the whole search, and BGATE_NODE overrides it for the
nvm/volta case where the shell's node is not the one this process inherited.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

# Windows: never flash a console window out of a background call.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: Manifest that identifies a web project, and the file every call reads first.
MANIFEST = "package.json"

#: Where a vite build lands unless the project says otherwise.
DEFAULT_DIST = "dist"

#: THE PAYLOAD BUDGET, in bytes of the compressed transfer a first visit costs.
#:
#: 25 MB is not a preference, it is roughly where a game stops being openable by
#: someone who was not already committed to opening it: ~20 s on a 10 Mbit
#: connection, which is the point most measured bounce curves have already
#: given up. It is a DEFAULT and a project sets its own — the number matters far
#: less than the fact that some number is checked at all, which is what the
#: 661 MB export did not have.
DEFAULT_BUDGET_BYTES = 25 * 1024 * 1024

#: Files a browser fetches to start the game. Everything else in dist/ (source
#: maps, licence text, the stats.html a bundler analyser leaves behind) is
#: shipped but never downloaded by a player, and counting it would make the
#: budget lie in the direction that causes needless work.
_PAYLOAD_SUFFIXES = (".html", ".js", ".mjs", ".css", ".wasm", ".json",
                     ".png", ".jpg", ".jpeg", ".webp", ".avif", ".svg", ".ico",
                     ".glb", ".gltf", ".bin", ".ktx2", ".basis",
                     ".mp3", ".ogg", ".wav", ".m4a", ".woff", ".woff2")
_PAYLOAD_IGNORE_SUFFIXES = (".map",)


class NodeNotFound(RuntimeError):
    pass


class WebProjectError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The binary
# ---------------------------------------------------------------------------
def find_node() -> str:
    """Locate node. BGATE_NODE overrides everything."""
    override = os.environ.get("BGATE_NODE")
    if override:
        if not Path(override).is_file():
            raise NodeNotFound(
                f"BGATE_NODE points at a missing file: {override}")
        return override
    found = shutil.which("node")
    if not found:
        raise NodeNotFound(
            "node not found. Install Node.js 20 or newer (nodejs.org, or your "
            "platform's package manager) and reopen the shell, or set "
            "BGATE_NODE to the interpreter you want used.")
    return found


def find_binary() -> str:
    """The engine registry's entrypoint — see bgate_core.runtime.engines.binary."""
    return find_node()


def available() -> dict:
    try:
        path = find_node()
    except NodeNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


# A binary's version cannot change unless the FILE changes, so the cache is
# keyed on the path and its mtime rather than on a clock — the same rule, and
# the same reasoning, as godot._VERSION_CACHE.
_VERSION_CACHE: dict[tuple[str, float], str] = {}


def version() -> dict:
    """Which node this is, asked of the binary rather than of its path."""
    try:
        path = find_node()
    except NodeNotFound as exc:
        return {"available": False, "reason": str(exc)}
    try:
        key = (path, Path(path).stat().st_mtime)
    except OSError:
        key = (path, 0.0)
    if key not in _VERSION_CACHE:
        try:
            got = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=20,
                                 creationflags=_NO_WINDOW)
            _VERSION_CACHE[key] = (got.stdout or got.stderr).strip().lstrip("v")
        except (OSError, subprocess.SubprocessError):
            _VERSION_CACHE[key] = ""
    return {"available": True, "path": path, "version": _VERSION_CACHE[key]}


def _npm() -> str:
    """npm, which on Windows is a .cmd shim rather than an executable.

    shutil.which finds the shim; subprocess must be told it is a shell script,
    which is what shell=False plus the full shim path already handles on every
    platform this runs on.
    """
    found = shutil.which("npm")
    if not found:
        raise NodeNotFound(
            "npm not found — it ships with Node.js. If node is on PATH and npm "
            "is not, the install is incomplete.")
    return found


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------
def manifest(project_dir: str | os.PathLike[str]) -> dict:
    """The project's package.json as a dict. Raises when it is not readable.

    A manifest that exists and does not parse is a HARDER failure than one that
    is missing: a missing one means "this is not a web project", a broken one
    means the project is broken and every later step would fail with a stranger
    message.
    """
    path = Path(project_dir) / MANIFEST
    if not path.is_file():
        raise WebProjectError(f"no {MANIFEST} in {project_dir}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WebProjectError(f"{path} is not readable JSON: {exc}") from exc


def scripts(project_dir: str | os.PathLike[str]) -> dict:
    try:
        return dict(manifest(project_dir).get("scripts") or {})
    except WebProjectError:
        return {}


def installed(project_dir: str | os.PathLike[str]) -> bool:
    """Has `npm install` been run here?

    Asked before anything that would otherwise fail with npm's own error, which
    names a missing binary rather than the missing install that caused it.
    """
    return (Path(project_dir) / "node_modules").is_dir()


def _npm_run(project_dir: str, script: str, timeout: int,
             extra: Optional[list[str]] = None) -> dict:
    started = time.monotonic()
    try:
        cmd = [_npm(), "run", script, *(extra or [])]
        got = subprocess.run(cmd, cwd=str(project_dir), capture_output=True,
                             text=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
    except NodeNotFound as exc:
        return {"ok": False, "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "seconds": timeout,
                "error": f"`npm run {script}` did not finish within {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": got.returncode == 0,
        "exit_code": got.returncode,
        "stdout": got.stdout or "",
        "stderr": got.stderr or "",
        "seconds": round(time.monotonic() - started, 2),
        "script": script,
    }


def check_project(project_dir: str, timeout: int = 300) -> dict:
    """Does it still build? The web answer to 'did I break it'.

    Prefers the project's own `build` script, because that is the thing whose
    output ships. A project with no build script falls back to a typecheck,
    which is a weaker claim and says so in the result rather than passing
    quietly as though it had built.
    """
    root = Path(project_dir)
    if not (root / MANIFEST).is_file():
        return {"ok": False, "error": f"no {MANIFEST} in {project_dir}"}
    if not installed(root):
        return {"ok": False, "installed": False,
                "error": "node_modules is not there — run `npm install` in "
                         f"{project_dir} first. Builders Gate does not install "
                         "dependencies on your behalf."}
    have = scripts(root)
    if "build" in have:
        got = _npm_run(str(root), "build", timeout)
        return {**got, "installed": True, "checked": "build"}
    if "typecheck" in have:
        got = _npm_run(str(root), "typecheck", timeout)
        return {**got, "installed": True, "checked": "typecheck",
                "note": "no `build` script — this typechecked but never "
                        "produced the bundle a player would download."}
    return {"ok": False, "installed": True,
            "error": "package.json defines neither a `build` nor a "
                     "`typecheck` script, so there is nothing to check."}


def test_run(project_dir: str, timeout: int = 300) -> dict:
    """Run the project's own test script."""
    root = Path(project_dir)
    if not installed(root):
        return {"ok": False, "installed": False,
                "error": "node_modules is not there — run `npm install` first."}
    if "test" not in scripts(root):
        return {"ok": False, "error": "package.json defines no `test` script."}
    return {**_npm_run(str(root), "test", timeout), "installed": True}


def run_script(script: str, project_dir: Optional[str] = None,
               timeout: int = 120) -> dict:
    """Run a JavaScript file under node, in the project's directory.

    The web sibling of godot.run_script, and deliberately narrower: it takes a
    PATH, never source. Godot's version accepts source because a GDScript
    snippet has nowhere else to live; a node script that needs the project's
    modules has to be a file inside the project for its imports to resolve, so
    accepting source would only produce a script that cannot import anything.
    """
    path = Path(script)
    if project_dir and not path.is_absolute():
        path = Path(project_dir) / script
    if not path.is_file():
        return {"ok": False, "error": f"{script} is not a readable file. "
                                      "run_script takes a path, not source."}
    started = time.monotonic()
    try:
        got = subprocess.run([find_node(), str(path)],
                             cwd=str(project_dir or path.parent),
                             capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW)
    except NodeNotFound as exc:
        return {"ok": False, "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True,
                "error": f"{path.name} did not exit within {timeout}s"}
    return {"ok": got.returncode == 0, "exit_code": got.returncode,
            "stdout": got.stdout or "", "stderr": got.stderr or "",
            "seconds": round(time.monotonic() - started, 2),
            "ran_from": str(path)}


# ---------------------------------------------------------------------------
# The payload budget — the reason this adapter is shaped the way it is
# ---------------------------------------------------------------------------
def _gzip_size(path: Path) -> int:
    """What this file costs over the wire, not what it costs on disk.

    Every static host worth using serves .js/.css/.html compressed, so on-disk
    bytes overstate a JS bundle by 3-4x and understate nothing. Measuring the
    wrong one in the safe direction still produces a budget people learn to
    ignore.
    """
    import gzip

    try:
        raw = path.read_bytes()
    except OSError:
        return 0
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".avif",
                               ".woff", ".woff2", ".mp3", ".ogg", ".m4a",
                               ".ktx2", ".basis"):
        # Already compressed; gzip would spend time to gain nothing and
        # occasionally reports MORE than the original.
        return len(raw)
    return len(gzip.compress(raw, 6))


def payload(dist_dir: str | os.PathLike[str],
            budget_bytes: int = DEFAULT_BUDGET_BYTES) -> dict:
    """What a first visit downloads, and whether that is within budget.

    Reports the biggest contributors whether or not the budget is blown,
    because "you are at 80% of budget and one texture is half of it" is the
    moment the information is cheap to act on.
    """
    base = Path(dist_dir)
    if not base.is_dir():
        return {"ok": False, "error": f"no build output at {dist_dir} — build "
                                      "before measuring."}
    files: list[dict] = []
    total = 0
    for item in base.rglob("*"):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix in _PAYLOAD_IGNORE_SUFFIXES:
            continue
        if suffix not in _PAYLOAD_SUFFIXES:
            continue
        wire = _gzip_size(item)
        total += wire
        files.append({"file": str(item.relative_to(base)).replace("\\", "/"),
                      "bytes": wire,
                      "disk_bytes": item.stat().st_size})
    files.sort(key=lambda f: f["bytes"], reverse=True)
    within = total <= budget_bytes
    return {
        "ok": within,
        "bytes": total,
        "mb": round(total / (1024 * 1024), 2),
        "budget_bytes": budget_bytes,
        "budget_mb": round(budget_bytes / (1024 * 1024), 2),
        "within_budget": within,
        "share_of_budget": round(total / budget_bytes, 3) if budget_bytes else 0,
        "file_count": len(files),
        "biggest": files[:15],
        "error": "" if within else (
            f"payload is {round(total / (1024 * 1024), 2)} MB over the wire, "
            f"budget is {round(budget_bytes / (1024 * 1024), 2)} MB. The "
            f"biggest contributors are listed in `biggest` — this is the check "
            f"a 661 MB web export did not have."),
    }


def build(project_dir: str, timeout: int = 600,
          budget_bytes: int = DEFAULT_BUDGET_BYTES,
          dist: str = "") -> dict:
    """Build the game and measure what it weighs, in one call.

    The two halves are deliberately not separable at this level: a build whose
    payload nobody looked at is the exact thing that shipped at 661 MB.
    """
    root = Path(project_dir)
    got = check_project(str(root), timeout=timeout)
    if not got.get("ok"):
        return {**got, "built": False}
    out = root / (dist or DEFAULT_DIST)
    measured = payload(out, budget_bytes=budget_bytes)
    return {"ok": bool(got.get("ok") and measured.get("ok")),
            "built": True, "dist": str(out),
            "build": {k: got.get(k) for k in ("seconds", "exit_code", "stderr")},
            "payload": measured}


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------
def browser_available() -> dict:
    """Is Playwright usable — BOTH the package and a browser build?

    TWO INSTALLS THAT FAIL DIFFERENTLY, and conflating them is the mistake the
    art_key doctor row already had to unlearn: the row that asked "is the
    variable set" went green on a machine where nothing could actually generate.
    `pip install playwright` puts the package there; `playwright install
    chromium` downloads the browser, which is a separate several-hundred-MB step
    that a fresh install has NOT done. Reporting "playwright: yes" off the
    import alone would promise screenshots on a machine that cannot take one.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"available": False, "package": False, "browser": False,
                "reason": "the playwright package is not installed — "
                          "`pip install builders-gate[web]`."}
    try:
        with sync_playwright() as play:
            exe = play.chromium.executable_path
    except Exception as exc:                                      # noqa: BLE001
        return {"available": False, "package": True, "browser": False,
                "reason": f"playwright could not start: {exc}"}
    if not exe or not Path(exe).exists():
        return {"available": False, "package": True, "browser": False,
                "path": exe or "",
                "reason": "the playwright package is installed but its browser "
                          "build is not — run `playwright install chromium`. "
                          "(Builders Gate does not download it for you: it is a "
                          "few hundred MB and that is your decision.)"}
    return {"available": True, "package": True, "browser": True, "path": exe}


def screenshot(url: str, out_path: str, *, at: float = 1.0,
               width: int = 1280, height: int = 720,
               timeout: int = 60) -> dict:
    """Load a running web game and capture the canvas after `at` seconds.

    `at` is the same knob godot.screenshot has and means the same thing: a game
    that has not finished its first frame photographs as a blank canvas, and the
    honest fix is to wait rather than to retry until a frame happens to land.

    RETURNS THE CONSOLE AND THE FAILED REQUESTS ALONGSIDE THE IMAGE. A web game
    that renders nothing is almost always a 404 or a thrown exception, and both
    are invisible in the picture — which is exactly how a blank screenshot gets
    reported as "the shader is wrong".
    """
    probe = browser_available()
    if not probe["available"]:
        return {"ok": False, **probe, "error": probe["reason"]}
    from playwright.sync_api import sync_playwright

    console: list[dict] = []
    failed: list[dict] = []
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height})
                page.on("console", lambda m: console.append(
                    {"type": m.type, "text": m.text[:500]}))
                page.on("requestfailed", lambda r: failed.append(
                    {"url": r.url[:300],
                     "reason": (r.failure or "") if isinstance(r.failure, str)
                     else getattr(r.failure, "error_text", "")}))
                page.on("response", lambda r: (
                    failed.append({"url": r.url[:300], "status": r.status})
                    if r.status >= 400 else None))
                page.goto(url, timeout=int(timeout * 1000),
                          wait_until="load")
                page.wait_for_timeout(int(max(0.0, at) * 1000))
                page.screenshot(path=str(out))
            finally:
                browser.close()
    except Exception as exc:                                      # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "console": console[-40:], "failed_requests": failed[:20]}
    errors = [c for c in console if c["type"] == "error"]
    return {
        "ok": True,
        "path": str(out),
        "url": url,
        "seconds": round(time.monotonic() - started, 2),
        "viewport": {"width": width, "height": height},
        "console": console[-40:],
        "console_errors": errors,
        "failed_requests": failed[:20],
        # SAID ON EVERY RESULT, not only when it looks wrong — the same rule
        # godot.screenshot's `focus` caveat follows. A clean-looking capture
        # of a page that threw is read as evidence long before anyone thinks
        # to check the console separately.
        "note": ("a web game that renders nothing is usually a thrown exception "
                 "or a 404, and neither shows in the image — read "
                 "console_errors and failed_requests before concluding "
                 "anything about the picture."
                 if (errors or failed) else ""),
    }


# ---------------------------------------------------------------------------
# The dev server
# ---------------------------------------------------------------------------
# A LONG-LIVED PROCESS IS NOT AN ENGINE CALL, and the difference decides how it
# is locked. bgate_core.runtime.enginelock serialises ONE invocation: hold it,
# spawn, wait, release. A dev server runs for as long as someone is looking at
# the game, so holding the engine lock for its lifetime would block every
# screenshot and build of the same project — the lock would be protecting the
# project from the thing it exists to serve.
#
# So the server is tracked in a pidfile beside the project instead, and the
# engine lock is taken only around the calls that actually contend (build,
# check, test). Vite already refuses a port it cannot bind, so the pidfile is
# bookkeeping for "which port did we start it on and is it still alive",
# not a mutex.
_PIDFILE = ".bgate_web_dev.json"

#: The address the dev server binds and the URL we hand back — one value, so
#: they cannot disagree. See dev_start for what happened when they did.
_HOST = "127.0.0.1"


def _kill_tree(pid: int) -> None:
    """Kill a process and its children. Best-effort; never raises."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


def _pidfile(project_dir: str | os.PathLike[str]) -> Path:
    return Path(project_dir) / _PIDFILE


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            got = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15,
                creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (got.stdout or "")
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def dev_status(project_dir: str | os.PathLike[str]) -> dict:
    """Is a dev server running for this project, and on what URL?"""
    path = _pidfile(project_dir)
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    pid = int(got.get("pid") or 0)
    if not _alive(pid):
        # A STALE PIDFILE IS CLEANED, NOT REPORTED. A killed server that leaves
        # its file behind otherwise makes dev_start refuse forever, which is
        # the same class of bug the engine lock's TTL exists to prevent.
        try:
            path.unlink()
        except OSError:
            pass
        return {"running": False, "was": got, "stale": True}
    return {"running": True, **got}


def dev_start(project_dir: str, port: int = 5173, timeout: int = 60) -> dict:
    """Start the project's dev server and wait until it actually answers.

    RETURNS ONLY ONCE THE PORT ANSWERS, or fails saying why. Returning as soon
    as the process spawned would hand the caller a URL that is not up yet, and
    the screenshot taken against it photographs a connection error — which then
    reads as "the game is broken".
    """
    root = Path(project_dir)
    if not (root / MANIFEST).is_file():
        return {"ok": False, "error": f"no {MANIFEST} in {project_dir}"}
    if not installed(root):
        return {"ok": False, "installed": False,
                "error": "node_modules is not there — run `npm install` first."}
    if "dev" not in scripts(root):
        return {"ok": False, "error": "package.json defines no `dev` script."}
    already = dev_status(root)
    if already.get("running"):
        return {"ok": True, "already_running": True, **already}

    url = f"http://{_HOST}:{port}/"
    try:
        # --host IS NOT OPTIONAL AND IT IS NOT ABOUT EXPOSURE. Vite's default
        # host is the string "localhost", which on Windows binds ::1 ONLY —
        # measured: the server was up and serving, and every probe of
        # http://127.0.0.1:<port>/ failed for the full 90s timeout. Binding the
        # address we are about to hand back makes the URL in the result true.
        # It stays loopback, which is the same posture `bgate serve` takes.
        proc = subprocess.Popen(
            [_npm(), "run", "dev", "--",
             "--host", _HOST, "--port", str(port), "--strictPort"],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=_NO_WINDOW)
    except NodeNotFound as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else "") or ""
            return {"ok": False, "exit_code": proc.returncode,
                    "error": "the dev server exited immediately",
                    "output": out[-2000:]}
        if _answers(url):
            _pidfile(root).write_text(
                json.dumps({"pid": proc.pid, "port": port, "url": url,
                            "at": time.time()}), encoding="utf-8")
            return {"ok": True, "pid": proc.pid, "port": port, "url": url,
                    "seconds": round(timeout - (deadline - time.monotonic()), 2)}
        time.sleep(0.25)
    # KILL THE TREE, NOT THE SHIM. npm spawns vite as a CHILD, so terminating
    # the npm process leaves the actual server holding the port — measured: a
    # timed-out start orphaned a vite that then made every later dev_start fail
    # on --strictPort, for a server nothing was tracking and nobody could stop.
    _kill_tree(proc.pid)
    return {"ok": False, "timeout": True,
            "error": f"the dev server did not answer on {url} within {timeout}s"}


def _answers(url: str) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2) as got:
            return got.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500          # a 404 still means something is serving
    except Exception:                                             # noqa: BLE001
        return False


def dev_stop(project_dir: str | os.PathLike[str]) -> dict:
    """Stop this project's dev server."""
    got = dev_status(project_dir)
    if not got.get("running"):
        return {"ok": True, "was_running": False, **got}
    pid = int(got.get("pid") or 0)
    _kill_tree(pid)
    try:
        _pidfile(project_dir).unlink()
    except OSError:
        pass
    return {"ok": True, "was_running": True, "pid": pid}
