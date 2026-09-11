"""Unity engine MCP tools: the third engine's surface.

Same contract as tools_web: the shared plumbing stays in server, this module
imports it back, and server star-imports this at its bottom. Every tool here
is engine-owned (bgate_core.runtime.engines.ENGINE_TOOLS["unity"]), so none
of them register on a Godot or web project.

WHAT IS DIFFERENT ABOUT UNITY, and shapes every tool: the editor is the only
way in, it takes tens of seconds to start, it refuses to open a project
another editor holds, and a newer editor silently upgrades a project the
moment it opens it. So every call reports which editor it used, refuses when
Temp/UnityLockfile is present, and prefers the version ProjectVersion.txt
names. None of that is optional politeness; each is a measured way a batch
call ruins an afternoon.
"""
from pathlib import Path as _Path

from bgate_adapters import unity as _unity
from bgate_mcp.server import (  # noqa: F401
    Optional, _actor, _archive_preview, _contained_path, _log,
    _note_tool_write, _project, _root, _run_tag, _tool,
)


def _project_dir(given: str = "") -> str:
    if given.strip():
        return given.strip()
    where = _project.game_dir(_root(), engine="unity")
    return str(where) if where is not None else ""


def _need_project(given: str = "") -> tuple[str, Optional[dict]]:
    target = _project_dir(given)
    if not target:
        return "", {"ok": False, "error":
                    f"no Unity project found under {_root()}: looked for "
                    f"{_unity.MARKER.as_posix()} in <root>/game and <root>. "
                    "Pass unity_project explicitly, or check project_set_engine."}
    _contained_path(target, "unity_project")
    return target, None


@_tool
def unity_status(unity_project: str = "") -> dict:
    """Is the editor installed, which version, and does it match the project?

    Answers the questions that each fail differently: is an editor found at
    all, which versions the Hub has, which one the project was last opened
    in, whether that exact one is installed (a mismatch means the first open
    UPGRADES the project), whether the editor currently holds the project,
    and whether the two BGate scripts are in Assets/.
    """
    target = _project_dir(unity_project)
    out: dict = {"editors": _unity.installed_editors(), "project": target}
    try:
        exe = _unity.find_unity(target or None)
        out.update(_unity.version(exe))
    except _unity.UnityNotFound as exc:
        out.update({"available": False, "reason": str(exc)})
    if target:
        wanted = _unity.project_version(target)
        out["project_version"] = wanted
        out["version_matches"] = bool(wanted) and out.get("version") == wanted
        out["editor_open"] = _unity.editor_open(target)
        out["scripts"] = _unity.scripts_installed(target)
        out["product_name"] = _unity.product_name(target)
        out["test_files"] = _unity.test_scripts(target)
        packages = _unity.manifest_packages(target)
        out["test_framework"] = "com.unity.test-framework" in packages
        if wanted and not out["version_matches"] and out.get("available"):
            out["warning"] = (f"the project was last opened in {wanted} and "
                              f"the editor found is {out.get('version')}; "
                              "the first batchmode open will upgrade the "
                              "project. Install the matching editor, or set "
                              "BGATE_UNITY to it.")
    else:
        out["error"] = "no Unity project found; is this project's engine 'unity'?"
    out["ok"] = bool(out.get("available") and target)
    return out


@_tool
def unity_check(unity_project: str = "", timeout: int = 600) -> dict:
    """Compile the project in batchmode: the 'does it still build' check.

    The result's `errors` are the compile errors out of the editor log, file
    and line each. A first open imports every asset and can take minutes;
    raise `timeout` before reading a slow first run as a hang. Refused while
    the editor has the project open.
    """
    target, refused = _need_project(unity_project)
    if refused:
        return refused
    got = _unity.check_project(target, timeout=timeout)
    _log("unity", f"compile check {'passed' if got.get('ok') else 'FAILED'}: "
                  f"{_Path(target).name}"
                  + (f", {len(got['errors'])} error(s)" if got.get("errors") else ""))
    return got


@_tool
def unity_test_run(unity_project: str = "", platform: str = "EditMode",
                   filter: str = "", timeout: int = 900) -> dict:
    """Run the Unity Test Framework and RECORD the score.

    platform: EditMode (default, fast) | PlayMode (needs graphics, slow).
    `filter` is passed to -testFilter (a test name or namespace). Scored into
    the same history godot_test_run and web_test_run write, so the Tests tab
    and the QA seat see it. No tests is `no_tests`, not a pass.
    """
    from bgate_core.runtime import enginetests as _tests

    target, refused = _need_project(unity_project)
    if refused:
        return refused
    return _tests.run(_root(), timeout=timeout, actor=_actor(),
                      platform=platform, filter=filter)


@_tool
def unity_execute(method: str, unity_project: str = "", timeout: int = 600,
                  graphics: bool = False) -> dict:
    """Run a static C# method the project ships: -executeMethod Ns.Class.Method.

    The nearest thing Unity has to godot_run. Nothing is written into the
    project to make it happen, so it can only call what already compiles
    there; `graphics=True` drops -nographics for a method that renders.
    """
    target, refused = _need_project(unity_project)
    if refused:
        return refused
    got = _unity.execute_method(target, method, timeout=timeout,
                                graphics=graphics)
    _log("unity", f"executed {method}: {'ok' if got.get('ok') else 'failed'}")
    return got


@_tool
def unity_install_scripts(unity_project: str = "") -> dict:
    """Put Assets/BGate/BGateTelemetry.cs and Editor/BGateCapture.cs in place.

    Two files, never overwritten once present, no .meta files (the editor
    generates them on import). The telemetry script boots itself on the
    first scene load; the capture script is what engine_screenshot calls.
    """
    target, refused = _need_project(unity_project)
    if refused:
        return refused
    got = _unity.install_scripts(target)
    for path in got.get("written", []):
        _note_tool_write(_root(), path)
    if got.get("written"):
        _log("unity", f"installed {len(got['written'])} BGate script(s)",
             ref=got.get("dir", ""))
    return got


@_tool
def unity_screenshot(scene: str = "", label: str = "", unity_project: str = "",
                     width: int = 1280, height: int = 720,
                     timeout: int = 600) -> dict:
    """Render a scene's camera to a PNG through the bundled editor script.

    A STILL OF THE SAVED SCENE, NOT A FRAME OF PLAY: batchmode never enters
    play mode, so nothing has run Start() or Update(). Right for "is the
    level laid out and lit"; for "does the player move", record a playtest.
    `scene` is a path under Assets/ or empty for the first enabled scene in
    Build Settings. Needs unity_install_scripts once.
    """
    target, refused = _need_project(unity_project)
    if refused:
        return refused
    root = _root()
    out = str(_Path(root) / ".bgate_out" / "shots"
              / f"{_run_tag(label or 'unity')}.png")
    got = _unity.screenshot(target, out, scene=scene, width=width,
                            height=height, timeout=timeout)
    if got.get("ok"):
        _note_tool_write(root, got["path"])
        archived = _archive_preview(got["path"], f"shot-{label or 'unity'}")
        if archived:
            got["preview"] = archived
        _log("screenshot", f"rendered {scene or 'the first build scene'} "
                           "in the Unity editor"
                           + (f" ({label})" if label else ""),
             ref=archived or got["path"])
    return got
