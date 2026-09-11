"""Web engine MCP tools — the second engine's surface.

Carved out as its own module for the same reason tools_level and tools_blender
are: a domain that never touches the others lives apart. The contract is
unchanged — the shared plumbing (_tool, _root, the gates) stays in server, this
module imports it back, and server star-imports this at its BOTTOM.

EVERY TOOL HERE IS ENGINE-OWNED (bgate_core.runtime.engines.ENGINE_TOOLS["web"]),
so none of them register on a Godot project. That is the whole point of the gate:
a Godot agent should not be carrying seven schemas for an engine its game is not
written in, and a web agent should not be offered scene_wire.

THE ODD ONE OUT IS web_build, and it is the reason this engine exists at all. A
finished Godot 3D game exported to the web at 661 MB — it ran, and no player
would ever have waited for it. Nothing in the pipeline had asked what a build
weighed, because for a desktop export nobody cares. web_build asks, every time,
and fails when the answer is too big.
"""
from pathlib import Path as _Path

from bgate_adapters import web as _web
from bgate_core.runtime import engines as _engines
from bgate_mcp.server import (  # noqa: F401
    Optional, _archive_preview, _contained_path, _log, _note_tool_write,
    _project, _root, _run_tag, _tool,
)


def _project_dir(given: str = "") -> str:
    """Where this project's web game lives, or '' when there is none.

    Defaulting rather than demanding the path is deliberate: `godot_project` is
    the argument the Godot tools most often get wrong, and every one of these
    tools would have inherited the same trap.
    """
    if given.strip():
        return given.strip()
    where = _project.game_dir(_root(), engine="web")
    return str(where) if where is not None else ""


def _need_project(given: str = "") -> tuple[str, Optional[dict]]:
    target = _project_dir(given)
    if not target:
        return "", {"ok": False, "error":
                    f"no web project found under {_root()} — looked for "
                    f"{_web.MANIFEST} in <root>/game and <root>. Pass "
                    "web_project explicitly, or check project_set_engine."}
    _contained_path(target, "web_project")
    return target, None


@_tool
def web_status(web_project: str = "") -> dict:
    """Is the web toolchain usable here — node, the install, and the browser?

    Answers the four questions that each fail differently: is node on PATH, is
    there a package.json, has `npm install` been run, and is Playwright's
    BROWSER (not merely its Python package) present. Conflating the last two is
    how a machine reports "screenshots available" and then cannot take one.
    """
    target = _project_dir(web_project)
    out = {"node": _web.version(), "browser": _web.browser_available(),
           "project": target}
    if target:
        out["installed"] = _web.installed(target)
        out["scripts"] = sorted(_web.scripts(target))
        if not out["installed"]:
            out["next"] = f"run `npm install` in {target}"
    else:
        out["installed"] = False
        out["error"] = "no web project found — is this project's engine 'web'?"
    out["ok"] = bool(out["node"].get("available") and target and out["installed"])
    return out


@_tool
def web_build(web_project: str = "", budget_mb: float = 0,
              dist: str = "", timeout: int = 600) -> dict:
    """Build the game AND measure what a first visit downloads. Fails if over budget.

    THE TWO HALVES ARE NOT SEPARABLE HERE ON PURPOSE. A build whose payload
    nobody looked at is exactly what shipped at 661 MB. `budget_mb` overrides
    the 25 MB default; when the payload is over it, read `payload.biggest` —
    it is almost always one texture, one model or an uncompressed audio file.

    Sizes are the COMPRESSED transfer, not bytes on disk: every static host
    serves .js and .css gzipped, so disk bytes overstate a bundle by 3-4x and a
    budget measured against them is one people learn to ignore.
    """
    target, refused = _need_project(web_project)
    if refused:
        return refused
    budget = int(budget_mb * 1024 * 1024) if budget_mb > 0 \
        else _web.DEFAULT_BUDGET_BYTES
    got = _web.build(target, timeout=timeout, budget_bytes=budget, dist=dist)
    payload = got.get("payload") or {}
    if got.get("built"):
        _log("web", f"built {_Path(target).name}: "
                    f"{payload.get('mb', '?')} MB over the wire "
                    f"({'within' if payload.get('ok') else 'OVER'} budget)")
    return got


@_tool
def web_payload(web_project: str = "", budget_mb: float = 0,
                dist: str = "") -> dict:
    """Measure an EXISTING build's download cost without rebuilding it.

    The read-only half of web_build, for when you want the number after a build
    somebody else ran, or want to compare two budgets against one bundle.
    """
    target, refused = _need_project(web_project)
    if refused:
        return refused
    budget = int(budget_mb * 1024 * 1024) if budget_mb > 0 \
        else _web.DEFAULT_BUDGET_BYTES
    out = _Path(target) / (dist or _web.DEFAULT_DIST)
    return _web.payload(out, budget_bytes=budget)


@_tool
def web_test_run(web_project: str = "", timeout: int = 300) -> dict:
    """Run the project's own test script (vitest in the templates)."""
    target, refused = _need_project(web_project)
    if refused:
        return refused
    return _web.test_run(target, timeout=timeout)


@_tool
def web_run(script: str, web_project: str = "", timeout: int = 120) -> dict:
    """Run a JavaScript file under node, inside the project.

    TAKES A PATH, NEVER SOURCE — narrower than godot_run and deliberately so. A
    GDScript snippet has nowhere else to live, but a node script that needs the
    project's modules must be a file inside the project for its imports to
    resolve; accepting source would only ever produce a script that cannot
    import anything.
    """
    target, refused = _need_project(web_project)
    if refused:
        return refused
    return _web.run_script(script, project_dir=target, timeout=timeout)


@_tool
def web_dev(web_project: str = "", port: int = 5173, timeout: int = 60) -> dict:
    """Start the dev server and wait until the URL actually answers.

    RETURNS ONLY ONCE THE PORT ANSWERS. Returning as soon as the process spawned
    would hand back a URL that is not up yet, and a screenshot taken against it
    photographs a connection error — which then gets read as "the game is
    broken". Idempotent: an already-running server is reported, not restarted.
    """
    target, refused = _need_project(web_project)
    if refused:
        return refused
    got = _web.dev_start(target, port=port, timeout=timeout)
    if got.get("ok") and not got.get("already_running"):
        _log("web", f"dev server up on {got.get('url')}")
    return got


@_tool
def web_dev_stop(web_project: str = "") -> dict:
    """Stop this project's dev server."""
    target, refused = _need_project(web_project)
    if refused:
        return refused
    return _web.dev_stop(target)


# ---------------------------------------------------------------------------
# The neutral screenshot — now that there is a second adapter to justify it
# ---------------------------------------------------------------------------
# THE PLAN DEFERRED THIS TO PHASE 3 RATHER THAN RENAMING godot_screenshot IN
# PHASE 2, and the deferral was the right call for a measured reason: the rename
# touched 161 references across forty files, including built JS bundles, the
# decision records in docs/decisions, and templates/shared/CLAUDE.md — which is
# stamped into every user's game project, so existing games would have gone on
# instructing their agents to call a tool that no longer existed.
#
# So godot_screenshot keeps its name and this sits beside it, dispatching on the
# project's engine. The gallery archiving and the "read the console before you
# believe the picture" caveat are the same on both sides, because they are the
# same lesson: a capture that LOOKS fine is read as evidence long before anyone
# thinks to check what the engine was saying at the time.
@_tool
def engine_screenshot(at: float = 1.0, label: str = "", url: str = "",
                      scene: str = "", width: int = 1280, height: int = 720,
                      timeout: int = 120) -> dict:
    """Photograph the running game, whichever engine this project is built in.

    Godot runs the actual game and captures the viewport; web loads the dev
    server in a headless browser and captures the page, starting the server
    first if it is not already up. `at` means the same thing on both: a game
    that has not finished its first frame photographs blank, and waiting is the
    honest fix rather than retrying until a frame happens to land.

    ON THE WEB RESULT, READ console_errors AND failed_requests BEFORE THE IMAGE.
    A web game that renders nothing is almost always a thrown exception or a
    404, and neither of those is visible in the picture.
    """
    root = _root()
    engine = _project.engine_of(root)
    if engine == "godot":
        # One door, one implementation: hand straight to the Godot tool rather
        # than growing a second copy of its archiving and focus caveat.
        from bgate_mcp.tools_level import godot_screenshot as _shot

        where = _project.game_dir(root, engine="godot")
        if where is None:
            return {"ok": False, "engine": engine,
                    "error": f"no project.godot under {root}"}
        return {"engine": engine,
                **_shot(str(where), at=at, scene=scene or None, label=label,
                        timeout=timeout)}
    if engine != "web":
        return {"ok": False, "engine": engine,
                "error": f"EngineUnsupported: {_engines.label(engine)} projects "
                         "have no adapter, so there is nothing to photograph."}

    target, refused = _need_project()
    if refused:
        return refused
    target_url = url.strip()
    started = None
    if not target_url:
        started = _web.dev_start(target, timeout=60)
        if not started.get("ok"):
            return {"ok": False, "engine": engine, "dev": started,
                    "error": "could not start the dev server to photograph: "
                             + str(started.get("error") or "")}
        target_url = str(started.get("url") or "")

    try:
        out = str(_Path(root) / ".bgate_out" / "shots" /
                  f"{_run_tag(label or 'web')}.png")
    except Exception:                                             # noqa: BLE001
        out = f"bgate_shot_{_run_tag()}.png"

    got = _web.screenshot(target_url, out, at=at, width=width, height=height,
                          timeout=timeout)
    if got.get("ok"):
        _note_tool_write(root, got["path"])
        archived = _archive_preview(got["path"], f"shot-{label or 'web'}")
        if archived:
            got["preview"] = archived
        _log("screenshot", f"captured the running web game at t={at}s"
                           + (f" ({label})" if label else ""),
             ref=archived or got["path"])
    got["engine"] = engine
    if started is not None:
        got["dev"] = {"url": started.get("url"),
                      "already_running": started.get("already_running", False)}
    return got
