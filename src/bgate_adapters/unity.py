"""The Unity engine: adopted, checked, tested and photographed through the editor.

UNITY IS DRIVEN THROUGH ONE BINARY AND ONE FLAG SHAPE. Everything the editor
can do unattended is `Unity -batchmode -quit -projectPath X ...` plus one verb:
nothing for a compile check, `-runTests` for the test runner, `-executeMethod`
for a static C# method the project ships. There is no scripting language to
hand a snippet to and no headless "import" that reports resource errors the
way Godot's does; the compile is the check, and the log is the only place it
says what went wrong. So every call here writes the editor log to a file it
owns and reads it back, because `-logFile -` reaches stdout only from a real
console and this runs from none.

WHAT IS DELIBERATELY NOT HERE: scaffolding. A Unity project is made by the Hub
with a version and a render pipeline the user chose, and stamping one from a
template would have to pick both for them. Unity projects are ADOPTED
(`bgate adopt`), and the adapter installs its two scripts into an existing
Assets/ tree instead: the telemetry MonoBehaviour and the editor capture
script, both under Assets/BGate/ so a project can delete the whole folder.

THE EDITOR HOLDS ITS OWN LOCK. Two editors cannot open one project
(Temp/UnityLockfile), so a check while the editor is open fails with a
sentence about that rather than with the 40-second timeout the log would
otherwise show. That lock is Unity's, not the engine lock in
bgate_core.runtime.enginelock, whose file lives under Godot's cache.

THE PIECES, and which external thing each needs:
  find_binary / available   the editor. BGATE_UNITY, then the Hub's install
                            roots, preferring the version ProjectVersion.txt
                            names. `shutil.which("Unity")` last.
  project_version           what the project was last opened in.
  check_project             a batchmode compile. Errors come from the log.
  test_run                  the Unity Test Framework, EditMode or PlayMode,
                            scored from its NUnit XML.
  execute_method            `-executeMethod Namespace.Class.Method`.
  screenshot                the bundled editor script renders a scene's main
                            camera to a PNG. A still of the scene, not a
                            frame of play: batchmode does not enter play mode.
  install_scripts           puts Assets/BGate/*.cs in place.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Windows: never flash a console window out of a background call.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: The file that makes a directory a Unity project, and says which editor.
MARKER = Path("ProjectSettings") / "ProjectVersion.txt"

#: Where the adapter's own scripts live inside a project. One folder, so the
#: whole footprint is `rm -r Assets/BGate`.
SCRIPTS_REL = Path("Assets") / "BGate"
TELEMETRY_FILE = "BGateTelemetry.cs"
CAPTURE_FILE = str(Path("Editor") / "BGateCapture.cs")   # Editor/ makes it an editor assembly
#: The static method the capture script exposes to -executeMethod.
CAPTURE_METHOD = "BGate.Editor.BGateCapture.Run"

#: The bundled sources, beside the Godot addon in templates/.
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "unity" / "shared" / "Assets" / "BGate"

_VERSION_RE = re.compile(r"\b(\d{4}\.\d+\.\d+[abfp]\d+)\b")
_LOG_ERROR_RE = re.compile(
    r"^(?P<file>[^\r\n(]+\.cs)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>CS\d+): (?P<text>.*)$",
    re.MULTILINE)
_LOG_FATAL = ("Scripts have compiler errors", "Aborting batchmode due to failure",
              "Fatal Error", "Unable to parse", "Multiple Unity instances cannot open")


class UnityNotFound(RuntimeError):
    pass


class UnityProjectError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The binary
# ---------------------------------------------------------------------------
def _hub_roots() -> list[Path]:
    """Where the Unity Hub puts editors, per platform, plus its custom root."""
    roots: list[Path] = []
    if os.name == "nt":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base) / "Unity" / "Hub" / "Editor")
                roots.append(Path(base) / "Unity")
        appdata = os.environ.get("APPDATA")
        if appdata:
            custom = Path(appdata) / "UnityHub" / "secondaryInstallPath.json"
            try:
                text = custom.read_text(encoding="utf-8").strip().strip('"')
                if text:
                    roots.append(Path(text))
            except OSError:
                pass
    elif sys.platform == "darwin":
        roots.append(Path("/Applications/Unity/Hub/Editor"))
        roots.append(Path("/Applications/Unity"))
    else:
        roots.append(Path.home() / "Unity" / "Hub" / "Editor")
        roots.append(Path("/opt/unity/editors"))
    return roots


def _editor_exe(install: Path) -> Optional[Path]:
    """The editor executable inside one Hub install directory, if any."""
    candidates = (
        install / "Editor" / "Unity.exe",
        install / "Editor" / "Unity",
        install / "Unity.app" / "Contents" / "MacOS" / "Unity",
        install / "Unity.exe",
    )
    for cand in candidates:
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
        except OSError:
            continue
    return None


def installed_editors() -> list[dict]:
    """Every editor the Hub roots hold: [{version, path}], newest first."""
    found: dict[str, Path] = {}
    for root in _hub_roots():
        try:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for entry in entries:
            exe = _editor_exe(entry)
            if exe is None:
                continue
            got = _VERSION_RE.search(entry.name)
            found.setdefault(got.group(1) if got else entry.name, exe)
    out = [{"version": ver, "path": str(exe)} for ver, exe in found.items()]
    out.sort(key=lambda item: _version_key(item["version"]), reverse=True)
    return out


def _version_key(text: str) -> tuple:
    got = re.match(r"(\d+)\.(\d+)\.(\d+)([abfp])(\d+)", text or "")
    if not got:
        return (0, 0, 0, "", 0)
    major, minor, patch, kind, build = got.groups()
    return (int(major), int(minor), int(patch), kind, int(build))


def find_unity(project_dir: Optional[str | os.PathLike[str]] = None) -> str:
    """Locate the editor. BGATE_UNITY overrides everything.

    With a project, the editor that project was last opened in is preferred:
    Unity upgrades a project the moment a newer editor opens it, rewriting
    ProjectVersion.txt and every serialized asset it touches, and a "check"
    that did that on the way would be the most expensive check available.
    """
    override = os.environ.get("BGATE_UNITY")
    if override:
        if not Path(override).is_file():
            raise UnityNotFound(f"BGATE_UNITY points at a missing file: {override}")
        return override
    editors = installed_editors()
    wanted = project_version(project_dir) if project_dir else ""
    if wanted:
        for item in editors:
            if item["version"] == wanted:
                return item["path"]
    if editors:
        return editors[0]["path"]
    on_path = shutil.which("Unity") or shutil.which("unity")
    if on_path:
        return on_path
    raise UnityNotFound(
        "Unity editor not found. Install one through the Unity Hub, or set "
        "BGATE_UNITY to the editor executable (Editor/Unity.exe under the "
        "Hub's install root).")


def find_binary() -> str:
    """The engine registry's entrypoint; see bgate_core.runtime.engines.binary."""
    return find_unity()


def available() -> dict:
    try:
        path = find_unity()
    except UnityNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


def version(path: Optional[str] = None) -> dict:
    """Which editor this is. Read off the Hub's directory name, never by
    launching it: an editor start is a 20-second, licence-checking event."""
    try:
        exe = path or find_unity()
    except UnityNotFound as exc:
        return {"available": False, "reason": str(exc)}
    got = None
    for part in reversed(Path(exe).parts):
        got = _VERSION_RE.search(part)
        if got:
            break
    return {"available": True, "path": exe, "version": got.group(1) if got else ""}


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------
def is_project(project_dir: str | os.PathLike[str]) -> bool:
    return (Path(project_dir) / MARKER).is_file()


def project_version(project_dir: str | os.PathLike[str]) -> str:
    """The `m_EditorVersion` the project records, '' when unreadable."""
    try:
        text = (Path(project_dir) / MARKER).read_text(encoding="utf-8",
                                                      errors="replace")
    except OSError:
        return ""
    got = re.search(r"m_EditorVersion:\s*(\S+)", text)
    return got.group(1).strip() if got else ""


def product_name(project_dir: str | os.PathLike[str]) -> str:
    """`productName` from ProjectSettings.asset: what the player window is
    titled, which is the one hint the playtest recorder can use."""
    try:
        text = (Path(project_dir) / "ProjectSettings" / "ProjectSettings.asset"
                ).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    got = re.search(r"^\s*productName:\s*(.+)$", text, re.MULTILINE)
    return got.group(1).strip() if got else ""


def editor_open(project_dir: str | os.PathLike[str]) -> bool:
    """Is an editor holding this project right now?

    Unity writes Temp/UnityLockfile while open and deletes it on exit; a
    crashed editor leaves it behind, which is why this is reported as a
    likely cause rather than acted on.
    """
    return (Path(project_dir) / "Temp" / "UnityLockfile").is_file()


def scripts_installed(project_dir: str | os.PathLike[str]) -> dict:
    base = Path(project_dir) / SCRIPTS_REL
    return {"telemetry": (base / TELEMETRY_FILE).is_file(),
            "capture": (base / CAPTURE_FILE).is_file(),
            "dir": str(base)}


def install_scripts(project_dir: str | os.PathLike[str],
                    which: tuple[str, ...] = ("telemetry", "capture")) -> dict:
    """Copy the bundled C# into Assets/BGate/. Never overwrites an edited copy.

    Unity generates the .meta files on its next import, so none are shipped;
    a hand-written GUID would collide the moment two projects held one.
    """
    base = Path(project_dir)
    if not is_project(base):
        return {"action": "skipped", "why": f"no {MARKER} in {project_dir}"}
    names = {"telemetry": TELEMETRY_FILE, "capture": CAPTURE_FILE}
    written, unchanged, missing = [], [], []
    for key in which:
        name = names.get(key)
        if not name:
            continue
        source = TEMPLATE_DIR / name
        if not source.is_file():
            missing.append(name)
            continue
        target = base / SCRIPTS_REL / name
        if target.is_file():
            unchanged.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(str(target))
    action = "installed" if written else ("unchanged" if unchanged else "skipped")
    out = {"action": action, "written": written, "unchanged": unchanged,
           "dir": str(base / SCRIPTS_REL)}
    if missing:
        out["missing_from_build"] = missing
    return out


# ---------------------------------------------------------------------------
# Running the editor
# ---------------------------------------------------------------------------
def _scratch(project_dir: Path) -> Path:
    out = project_dir / "Temp" / "bgate"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _batch(project_dir: str, verb: list[str], *, timeout: int,
           stem: str, graphics: bool = False,
           env: Optional[dict[str, str]] = None, quit: bool = True) -> dict:
    """One batchmode editor run. Returns {ok, exit_code, seconds, log, log_path, errors}."""
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    if editor_open(project):
        return {"ok": False, "editor_open": True,
                "error": "the Unity editor has this project open "
                         "(Temp/UnityLockfile is present). Close it, or if it "
                         "crashed, delete that file, then retry: two editors "
                         "cannot hold one project."}
    try:
        exe = find_unity(project)
    except UnityNotFound as exc:
        return {"ok": False, "error": str(exc)}
    log_path = _scratch(project) / f"{stem}_{int(time.time())}.log"
    args = [exe, "-batchmode", "-projectPath", str(project),
            "-logFile", str(log_path), *verb]
    # -quit ends the editor as soon as the command line is processed. The
    # Test Framework runs asynchronously after that point, so a test run
    # with -quit writes no results file; the runner exits on its own when
    # the suite finishes. Every other verb wants -quit.
    if quit:
        args.insert(1, "-quit")
    if not graphics:
        args.insert(1, "-nographics")
    started = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=_NO_WINDOW,
                              env={**os.environ, **(env or {})})
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        code, timed_out = -1, True
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    errors = log_errors(log)
    out = {"ok": code == 0 and not errors and not timed_out,
           "exit_code": code, "errors": errors,
           "seconds": round(time.monotonic() - started, 2),
           "log_path": str(log_path), "output": log[-3000:],
           "command": subprocess.list2cmdline(args)}
    if timed_out:
        out["timeout"] = True
        out["error"] = (f"the editor did not exit within {timeout}s; a first "
                        "open imports every asset and can take minutes, so "
                        "retry with a longer timeout before reading this as "
                        "a hang")
    elif errors:
        out["error"] = f"{len(errors)} compile error(s); the first is {errors[0]}"
    elif code != 0:
        out["error"] = (f"the editor exited {code}; read `output` for the "
                        "last lines of its log")
    return out


def log_errors(log: str) -> list[str]:
    """Compile errors and fatal lines out of an editor log, deduplicated."""
    seen: list[str] = []
    for got in _LOG_ERROR_RE.finditer(log):
        line = (f"{got['file']}({got['line']},{got['col']}): {got['code']}: "
                f"{got['text'].strip()}")
        if line not in seen:
            seen.append(line)
    for marker in _LOG_FATAL:
        if marker in log and marker not in seen:
            seen.append(marker)
    return seen[:60]


def check_project(project_dir: str, timeout: int = 600) -> dict:
    """Does it still compile? The Unity answer to 'did I break it'.

    A batchmode open with nothing else asked of it: the editor imports what
    changed, compiles every assembly, and exits 0 or with the compile errors
    in its log. That log is the finding; the exit code alone lies in both
    directions (0 with "Scripts have compiler errors" is a thing).
    """
    return _batch(project_dir, [], timeout=timeout, stem="check")


def execute_method(project_dir: str, method: str, timeout: int = 600,
                   extra_args: Optional[list[str]] = None,
                   graphics: bool = False,
                   env: Optional[dict[str, str]] = None) -> dict:
    """Run `-executeMethod Namespace.Class.Method` in the project.

    The nearest thing Unity has to godot_run: a static method the project
    itself ships, named fully. Nothing is written into the project to make
    it happen, so it can only call what already compiles there.
    """
    method = str(method or "").strip()
    if not re.match(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$", method):
        return {"ok": False, "error": "method must be a fully qualified static "
                                      "method, like BGate.Editor.Tools.Rebuild"}
    verb = ["-executeMethod", method, *(extra_args or [])]
    return _batch(project_dir, verb, timeout=timeout,
                  stem="exec_" + method.rsplit(".", 1)[-1], graphics=graphics,
                  env=env)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
PLATFORMS = ("EditMode", "PlayMode")


def parse_nunit(xml_text: str) -> dict:
    """Totals and per-case results out of the Test Framework's NUnit 3 XML.

    A regex over the two element shapes rather than an XML parse, because a
    run that crashed mid-suite leaves a truncated file, and "3 of 12 cases,
    then nothing" is the finding a strict parser would throw away.
    """
    run = re.search(r"<test-run\b([^>]*)>", xml_text)
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', run.group(1))) if run else {}

    def num(key: str) -> int:
        try:
            return int(attrs.get(key) or 0)
        except ValueError:
            return 0

    cases: list[dict] = []
    for got in re.finditer(r"<test-case\b([^>]*)/?>", xml_text):
        case = dict(re.findall(r'(\w+)="([^"]*)"', got.group(1)))
        if not case.get("fullname") and not case.get("name"):
            continue
        cases.append({"name": case.get("fullname") or case.get("name"),
                      "result": case.get("result", ""),
                      "ok": case.get("result") == "Passed",
                      "seconds": float(case.get("duration") or 0)})
    return {"total": num("total"), "passed": num("passed"),
            "failed": num("failed"), "skipped": num("skipped"),
            "inconclusive": num("inconclusive"),
            "result": attrs.get("result", ""), "cases": cases}


def test_run(project_dir: str, platform: str = "EditMode",
             timeout: int = 900, filter: str = "") -> dict:
    """Run the Unity Test Framework and score its XML.

    PlayMode tests need graphics, so that platform drops -nographics; they
    are also the slow case (a player build per run on some versions), which
    is why the default is EditMode and the timeout is generous.
    """
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"platform must be one of {PLATFORMS}"}
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    results = _scratch(project) / f"tests_{platform}_{int(time.time())}.xml"
    verb = ["-runTests", "-testPlatform", platform,
            "-testResults", str(results)]
    if filter:
        verb += ["-testFilter", filter]
    got = _batch(str(project), verb, timeout=timeout,
                 stem=f"tests_{platform}", graphics=platform == "PlayMode",
                 quit=False)
    got["platform"] = platform
    got["results_path"] = str(results)
    try:
        xml_text = results.read_text(encoding="utf-8", errors="replace")
    except OSError:
        xml_text = ""
    if not xml_text:
        got["no_tests"] = not got.get("errors")
        got["ok"] = False
        got.setdefault("error", "the test runner wrote no results file; with "
                                "no compile errors that usually means the "
                                "Test Framework package is not in the project "
                                "or no tests exist for this platform")
        return got
    scored = parse_nunit(xml_text)
    got.update(scored)
    got["no_tests"] = scored["total"] == 0
    # -runTests exits 0 on green, 2 on a failed test, 3 on a runner error.
    got["ok"] = (scored["total"] > 0 and scored["failed"] == 0
                 and not got.get("errors"))
    if scored["failed"]:
        got["error"] = f"{scored['failed']} of {scored['total']} test(s) failed"
    elif scored["total"] == 0:
        got["error"] = f"no {platform} tests ran"
    return got


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------
def screenshot(project_dir: str, out_path: str, *, scene: str = "",
               width: int = 1280, height: int = 720,
               timeout: int = 600) -> dict:
    """Render a scene's main camera to a PNG through the bundled editor script.

    A STILL OF THE SCENE, NOT A FRAME OF PLAY. Batchmode does not enter play
    mode, so nothing has Start()ed or Update()d: what is photographed is the
    scene as saved, lit by the editor. That is exactly the evidence for "is
    the level laid out and lit" and exactly not the evidence for "does the
    player move", which a playtest recording provides.

    `scene` is a path under Assets/ (Assets/Scenes/Main.unity) or empty for
    the first scene in the build settings.
    """
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    have = scripts_installed(project)
    if not have["capture"]:
        return {"ok": False, "error": f"{CAPTURE_FILE} is not in {have['dir']}; "
                                      "run unity_install_scripts first (one "
                                      "editor script, no scene changes)"}
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The script reads its arguments from environment variables rather than
    # the command line: -executeMethod takes none, and parsing argv inside
    # the editor for a path with spaces is where these things go wrong.
    got = execute_method(str(project), CAPTURE_METHOD, timeout=timeout,
                         graphics=True,
                         env={"BGATE_SHOT_OUT": str(out),
                              "BGATE_SHOT_SCENE": scene or "",
                              "BGATE_SHOT_W": str(int(width)),
                              "BGATE_SHOT_H": str(int(height))})
    got["path"] = str(out)
    got["scene"] = scene
    if got.get("ok") and not out.is_file():
        got["ok"] = False
        got["error"] = ("the editor exited clean but wrote no image; read "
                        "`output` for what BGateCapture said")
    got["note"] = ("a still of the saved scene rendered by the editor, not a "
                   "frame of play: nothing has run Start() or Update()")
    return got


# ---------------------------------------------------------------------------
# Test discovery, for the shared history
# ---------------------------------------------------------------------------
def test_scripts(project_dir: str | os.PathLike[str]) -> list[str]:
    """C# files that look like Test Framework tests: under a Tests/ folder or
    named *Tests.cs, and using [Test]/[UnityTest]. Paths relative to the project."""
    base = Path(project_dir)
    out: set[str] = set()
    patterns = ("Assets/**/Tests/**/*.cs", "Assets/**/*Test.cs",
                "Assets/**/*Tests.cs")
    for pattern in patterns:
        for hit in glob.glob(str(base / pattern), recursive=True):
            path = Path(hit)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "[Test]" in text or "[UnityTest]" in text or "[TestCase" in text:
                out.add(path.relative_to(base).as_posix())
    return sorted(out)


def manifest_packages(project_dir: str | os.PathLike[str]) -> dict:
    try:
        text = (Path(project_dir) / "Packages" / "manifest.json").read_text(
            encoding="utf-8")
        return dict(json.loads(text).get("dependencies") or {})
    except (OSError, ValueError):
        return {}
