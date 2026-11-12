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

SCAFFOLDING IS UNITY'S OWN, NOT A COPIED TREE. `bgate init --engine unity`
asks the editor to make the project (`-createProject`), so the version,
ProjectSettings and package lock are whatever that editor writes, never a
hand-kept copy that drifts a release behind. The template then lays down C#
only, and a second batchmode run executes BGateScaffold.Build to save the
starter scene from code. Hand-written .unity YAML and .meta GUIDs are the two
things a Unity scaffold must never ship; this one ships neither. An existing
project is still ADOPTED (`bgate adopt`), which installs only the two scripts
under Assets/BGate/: the telemetry MonoBehaviour and the editor capture script.

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
  import_asset / place_prefab / set_field
                            the bundled BGateEdit editor script: copy a file
                            into Assets/ and import it, instantiate a prefab
                            into a scene, set one serialized field. Every
                            write is AssetDatabase, PrefabUtility or
                            EditorSceneManager; no YAML is written by hand.
"""
from __future__ import annotations

import glob
import hashlib
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
#: The bot's runtime half (BGateAgent + BGateInput) and the player builder.
AGENT_FILE = "BGateAgent.cs"
BUILD_FILE = str(Path("Editor") / "BGateBuild.cs")
BUILD_METHOD = "BGate.Editor.BGateBuild.Player"
#: Asset import, prefab placement and component edits, through the editor API.
EDIT_FILE = str(Path("Editor") / "BGateEdit.cs")
EDIT_CLASS = "BGate.Editor.BGateEdit"
#: Where the player build lands. Builds/ is in every Unity .gitignore. Not
#: Library/ (BuildPlayer refuses its own cache) and not Temp/ (cleared on close).
PLAYER_REL = Path("Builds") / "BGatePlayer"
PLAYER_EXE = "Player.exe"

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


def broken_installs() -> list[str]:
    """Hub install folders that hold an Editor/ directory but no editor binary.

    A partial download or an interrupted update leaves 7 GB of Data/ and DLLs
    with no Unity.exe; `installed_editors` rightly skips it, and without this
    the user is told to install an editor they can see in the Hub.
    """
    out: list[str] = []
    for root in _hub_roots():
        try:
            entries = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for entry in entries:
            if (entry / "Editor").is_dir() and _editor_exe(entry) is None:
                out.append(str(entry))
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
    broken = broken_installs()
    if broken:
        raise UnityNotFound(
            f"Unity is installed at {broken[0]} but its editor binary is "
            "missing (no Editor/Unity.exe): the install is incomplete. In the "
            "Unity Hub, uninstall that version and install it again.")
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

    THE FILE BEING THERE PROVES NOTHING. Unity 6 leaves Temp/UnityLockfile
    behind after every clean batchmode exit (measured: -createProject, then
    -executeMethod, and the file stays), so reading existence as "open"
    refused every call after the first one. The editor holds the file open
    with no sharing while it runs; whether WE can open it is the real test.
    """
    lock = Path(project_dir) / "Temp" / "UnityLockfile"
    return lock.is_file() and _lock_held(lock)


def _lock_held(lock: Path) -> bool:
    """True when another process holds the lockfile (the editor, running)."""
    try:
        if os.name == "nt":
            fd = os.open(lock, os.O_RDWR)
            os.close(fd)
        else:
            import fcntl
            with open(lock, "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def scripts_installed(project_dir: str | os.PathLike[str]) -> dict:
    base = Path(project_dir) / SCRIPTS_REL
    return {"telemetry": (base / TELEMETRY_FILE).is_file(),
            "capture": (base / CAPTURE_FILE).is_file(),
            "agent": (base / AGENT_FILE).is_file(),
            "build": (base / BUILD_FILE).is_file(),
            "edit": (base / EDIT_FILE).is_file(),
            "dir": str(base)}


#: sha256[:16] (CRLF folded to LF) of EVERY version of each bundled script
#: Builders Gate has shipped. A copy in a project that matches one of these is
#: ours and unedited, so a newer bundled version replaces it; anything else is
#: the human's edit and is kept. Without this an install was frozen at the
#: day it happened: a project scaffolded before BGateBuild.WebGL existed could
#: never build for the web. test_every_bundled_script_is_a_known_version
#: fails when a template changes and its hash is not added here.
SHIPPED_HASHES: dict[str, frozenset[str]] = {
    TELEMETRY_FILE: frozenset({"a3f26bc0ce697f88", "d72dd60abc9b83ae"}),
    CAPTURE_FILE: frozenset({"b669a83849984b27"}),
    AGENT_FILE: frozenset({"d16c470f99aac312"}),
    BUILD_FILE: frozenset({"6da1fda34fb2eaf9", "e6732f248718e849"}),
    EDIT_FILE: frozenset({"9d47cbdda3435ede", "339b84d04d7a64f3"}),  # 339b: #127's verbs
}
_SCRIPT_NAMES = {"telemetry": TELEMETRY_FILE, "capture": CAPTURE_FILE,
                 "agent": AGENT_FILE, "build": BUILD_FILE, "edit": EDIT_FILE}


def script_hash(path: str | os.PathLike[str]) -> str:
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:16]


def install_scripts(project_dir: str | os.PathLike[str],
                    which: tuple[str, ...] = ("telemetry", "capture")) -> dict:
    """Copy the bundled C# into Assets/BGate/, and bring every BGate script
    already there up to the bundled version. Never overwrites an edited copy:
    only a file whose content is a version Builders Gate shipped
    (SHIPPED_HASHES) is replaced; an edited one is listed under `edited`.

    Unity generates the .meta files on its next import, so none are shipped;
    a hand-written GUID would collide the moment two projects held one.
    Replacing a .cs keeps its .meta, so the GUID survives the update.
    """
    base = Path(project_dir)
    if not is_project(base):
        return {"action": "skipped", "why": f"no {MARKER} in {project_dir}"}
    written, updated, unchanged, edited, missing = [], [], [], [], []
    # Asked-for keys install; every BGate script already present refreshes.
    keys = list(dict.fromkeys(list(which) + [
        k for k, n in _SCRIPT_NAMES.items() if (base / SCRIPTS_REL / n).is_file()]))
    for key in keys:
        name = _SCRIPT_NAMES.get(key)
        if not name:
            continue
        source = TEMPLATE_DIR / name
        if not source.is_file():
            missing.append(name)
            continue
        target = base / SCRIPTS_REL / name
        if target.is_file():
            have = script_hash(target)
            if have == script_hash(source):
                unchanged.append(str(target))
            elif have in SHIPPED_HASHES.get(name, ()):
                shutil.copyfile(source, target)
                updated.append(str(target))
            else:
                edited.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        written.append(str(target))
    action = ("installed" if written else "updated" if updated
              else "unchanged" if (unchanged or edited) else "skipped")
    # `written` carries every file this call changed, so the callers that
    # record tool writes see the updates too.
    out = {"action": action, "written": written + updated, "updated": updated,
           "unchanged": unchanged, "dir": str(base / SCRIPTS_REL)}
    if edited:
        out["edited"] = edited
        out["edited_note"] = ("these differ from every version Builders Gate "
                              "shipped, so they were kept; delete one to get "
                              "the current bundled copy")
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


#: The static method the scaffold template ships, which saves the starter scene.
SCAFFOLD_METHOD = "BGate.Editor.BGateScaffold.Build"
TEST_FRAMEWORK = "com.unity.test-framework"


def _bundled_package_version(exe: str, package: str) -> str:
    """The version of a package this editor bundles, read from its install.

    Read, not hardcoded: a pinned version that the editor does not bundle sends
    the package manager to the network on the first open, and fails offline.
    """
    manifest = (Path(exe).parent / "Data" / "Resources" / "PackageManager"
                / "BuiltInPackages" / package / "package.json")
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return ""


def scaffold_prepare(dest: str | os.PathLike[str], *, timeout: int = 1200) -> dict:
    """Have the editor create an empty project at dest. The engine hook
    scaffold.new_project calls before it copies the template.

    A no-op on a directory that is already a Unity project, so a `--force`
    top-up never re-creates anything. `-createProject` makes ProjectSettings,
    Packages and the Library import; the Test Framework is then added to the
    manifest because an empty project leaves it out and `unity_test_run`
    needs it.
    """
    target = Path(dest)
    if is_project(target):
        return {"ok": True, "action": "exists", "path": str(target)}
    try:
        exe = find_unity()
    except UnityNotFound as exc:
        return {"ok": False, "error": str(exc)}
    import tempfile
    # The log cannot live in the project's Temp/: there is no project yet.
    log_path = Path(tempfile.gettempdir()) / f"bgate_unity_create_{int(time.time())}.log"
    args = [exe, "-batchmode", "-nographics", "-quit",
            "-createProject", str(target), "-logFile", str(log_path)]
    started = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=_NO_WINDOW)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True, "log_path": str(log_path),
                "error": f"the editor did not finish creating the project "
                         f"within {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    out = {"ok": code == 0 and is_project(target), "exit_code": code,
           "seconds": round(time.monotonic() - started, 2),
           "log_path": str(log_path), "editor": exe,
           "version": project_version(target) if is_project(target) else ""}
    if not out["ok"]:
        out["error"] = (f"Unity -createProject exited {code} without making "
                        f"{MARKER}; read the log at {log_path}")
        out["output"] = log[-3000:]
        return out
    manifest = target / "Packages" / "manifest.json"
    wanted = _bundled_package_version(exe, TEST_FRAMEWORK)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        deps = data.setdefault("dependencies", {})
        if wanted and TEST_FRAMEWORK not in deps:
            deps[TEST_FRAMEWORK] = wanted
            manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            out["added_packages"] = {TEST_FRAMEWORK: wanted}
    except (OSError, ValueError) as exc:
        out["warning"] = f"could not add {TEST_FRAMEWORK} to the manifest: {exc}"
    out["action"] = "created"
    return out


def scaffold_finish(project_dir: str | os.PathLike[str], kind: str = "2d",
                    *, timeout: int = 1200) -> dict:
    """Run the template's BGateScaffold.Build: saves Assets/Scenes/Main.unity
    and puts it first in the build settings. Leaves an existing scene alone,
    so it is safe to run again."""
    got = execute_method(str(project_dir), SCAFFOLD_METHOD, timeout=timeout,
                         env={"BGATE_KIND": kind})
    scene = Path(project_dir) / "Assets" / "Scenes" / "Main.unity"
    got["scene"] = str(scene)
    if got.get("ok") and not scene.is_file():
        got["ok"] = False
        got["error"] = ("BGateScaffold.Build exited clean but saved no scene; "
                        "read `output` for what it logged")
    return got


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
# Edits: import, place, set a field. Always the editor's API, never YAML.
# ---------------------------------------------------------------------------
#: What import_asset copies into Assets/. A .unity, .prefab or .meta is the
#: editor's own serialized output: copying one in from elsewhere carries a
#: foreign GUID, which is the hand-written-YAML failure by another door.
IMPORTABLE = frozenset({
    ".png", ".jpg", ".jpeg", ".tga", ".psd", ".exr", ".hdr", ".bmp", ".tif", ".tiff",
    ".fbx", ".obj", ".dae", ".blend", ".3ds",
    ".wav", ".ogg", ".mp3", ".aif", ".aiff", ".flac",
    ".ttf", ".otf", ".txt", ".json", ".bytes", ".csv", ".mp4", ".webm",
})


def _assets_rel(project: Path, rel: str, *, suffixes: tuple[str, ...] = ()) -> tuple[str, str]:
    """(posix path under Assets/, error). Refuses anything that leaves Assets/."""
    text = str(rel or "").strip().replace("\\", "/")
    if not text:
        return "", "a path under Assets/ is required"
    if Path(text).is_absolute():
        try:
            text = Path(text).resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            return "", f"{rel} is not inside {project}"
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or parts[0] != "Assets" or ".." in parts or len(parts) < 2:
        return "", f"{rel} must be a path under Assets/"
    clean = "/".join(parts)
    if suffixes and not clean.lower().endswith(suffixes):
        return "", f"{rel} must end with {' or '.join(suffixes)}"
    return clean, ""


def _vec(value, what: str) -> tuple[str, str]:
    """A 3-vector as the "x,y,z" the editor script parses, or an error."""
    if value is None or value == "" or value == []:
        return "", ""
    items = value if isinstance(value, (list, tuple)) else str(value).strip("[]() ").split(",")
    try:
        nums = [float(x) for x in items]
    except (TypeError, ValueError):
        return "", f"{what} must be three numbers, got {value!r}"
    if len(nums) != 3:
        return "", f"{what} must be three numbers, got {len(nums)}"
    return ",".join(repr(n) for n in nums), ""


def _edit(project_dir: str | os.PathLike[str], verb: str, env: dict[str, str],
          *, timeout: int) -> dict:
    """Run BGateEdit.<verb> and merge the JSON it writes into the result."""
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    installed = install_scripts(project, which=("edit",))
    # Not the project's Temp/: the editor clears files it did not make there
    # as it quits (measured: the result was gone by the time the call returned).
    import tempfile
    result = Path(tempfile.gettempdir()) / f"bgate_unity_edit_{verb.lower()}_{time.time_ns()}.json"
    got = execute_method(str(project), f"{EDIT_CLASS}.{verb}", timeout=timeout,
                         env={**env, "BGATE_EDIT_RESULT": str(result)})
    got["install"] = installed
    try:
        reported = json.loads(result.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        reported = None
    try:
        result.unlink()
    except OSError:
        pass
    if reported is None:
        if got.get("ok"):
            got["ok"] = False
            got["error"] = (f"BGateEdit.{verb} exited clean but wrote no result; "
                            "read `output` for what it logged")
        return got
    run_ok = got.get("ok", False)
    run_error = got.get("error")
    got.update(reported)
    got["ok"] = bool(reported.get("ok")) and run_ok
    if not got["ok"] and not reported.get("error") and run_error:
        got["error"] = run_error
    return got


def import_asset(project_dir: str | os.PathLike[str], source: str | os.PathLike[str],
                 dest: str = "", *, sprite: bool = False, pixels_per_unit: float = 0,
                 point_filter: bool = False, overwrite: bool = False,
                 timeout: int = 600) -> dict:
    """Copy an image, model or sound into Assets/ and have the editor import it.

    `dest` is the path under Assets/ (a folder ending in "/" keeps the file's
    name; empty is Assets/Imported/<name>). The editor writes the .meta and
    reports the GUID and what the asset loaded as; `sprite` makes a texture a
    Sprite, `point_filter` turns off filtering and compression for pixel art.
    """
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    src = Path(source)
    if not src.is_file():
        return {"ok": False, "error": f"source file not found: {source}"}
    suffix = src.suffix.lower()
    if suffix not in IMPORTABLE:
        return {"ok": False, "error": (
            f"{suffix or 'a file with no extension'} is not importable here. "
            ".unity, .prefab and .meta files are the editor's own output and "
            "are made in place (unity_place_prefab, unity_set_field), never "
            "copied in.")}
    wanted = dest.strip() or "Assets/Imported/"
    if wanted.replace("\\", "/").endswith("/"):
        wanted = wanted.replace("\\", "/") + src.name
    rel, err = _assets_rel(project, wanted)
    if err:
        return {"ok": False, "error": err}
    if Path(rel).suffix.lower() != suffix:
        return {"ok": False, "error": f"{rel} must keep the source's {suffix} extension; "
                                      "Unity picks the importer by extension"}
    target = project / rel
    if target.exists() and not overwrite:
        return {"ok": False, "exists": True, "path": rel,
                "error": f"{rel} already exists; pass overwrite=True to replace it "
                         "(its .meta and GUID are kept, so references survive)"}
    if editor_open(project):
        return {"ok": False, "editor_open": True,
                "error": "the Unity editor has this project open; close it first"}
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    env = {"BGATE_IMPORT_PATH": rel,
           "BGATE_IMPORT_SPRITE": "1" if sprite else "",
           "BGATE_IMPORT_POINT": "1" if point_filter else "",
           "BGATE_IMPORT_PPU": str(float(pixels_per_unit)) if pixels_per_unit else ""}
    got = _edit(project, "Import", env, timeout=timeout)
    got.setdefault("path", rel)
    if got.get("ok"):
        got["copied"] = str(target)
    elif not existed:
        # A failed import of a NEW file leaves nothing behind: no half-imported
        # copy for the next editor open to trip over, no orphan .meta.
        removed = []
        for leftover in (target, target.with_name(target.name + ".meta")):
            try:
                leftover.unlink()
                removed.append(str(leftover))
            except OSError:
                pass
        got["removed"] = removed
    return got


def place_prefab(project_dir: str | os.PathLike[str], prefab: str, *, scene: str = "",
                 name: str = "", parent: str = "", position=None, rotation=None,
                 scale=None, timeout: int = 600) -> dict:
    """Instantiate a prefab (or an imported model) into a scene and save it.

    PrefabUtility.InstantiatePrefab keeps the prefab link, so later edits to
    the prefab reach this instance. `scene` empty is the first Build Settings
    scene; `parent` is a hierarchy path ("Level/Props"); position, rotation
    (euler degrees) and scale are local, as [x, y, z].
    """
    project = Path(project_dir)
    rel, err = _assets_rel(project, prefab)
    if err:
        return {"ok": False, "error": f"prefab: {err}"}
    env = {"BGATE_PREFAB": rel, "BGATE_NAME": name or "", "BGATE_PARENT": parent or ""}
    if scene:
        srel, err = _assets_rel(project, scene, suffixes=(".unity",))
        if err:
            return {"ok": False, "error": f"scene: {err}"}
        env["BGATE_SCENE"] = srel
    for key, value, what in (("BGATE_POS", position, "position"),
                             ("BGATE_ROT", rotation, "rotation"),
                             ("BGATE_SCALE", scale, "scale")):
        text, err = _vec(value, what)
        if err:
            return {"ok": False, "error": err}
        env[key] = text
    return _edit(project, "Place", env, timeout=timeout)


def _value_text(value) -> str:
    """The text BGateEdit.Assign parses: lists become "a,b,c", bools lower."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_value_text(v) for v in value)
    if value is None:
        return "null"
    return str(value)


def set_field(project_dir: str | os.PathLike[str], component: str, field: str, value,
              *, object_path: str = "", scene: str = "", prefab: str = "",
              add: bool = False, timeout: int = 600) -> dict:
    """Set one serialized field on a component, in a scene or a prefab asset.

    Goes through SerializedObject, the inspector's own path, so the result
    is exactly what typing it in the inspector would save. `field` is the
    serialized property path (a script's field name, or m_LocalPosition on a
    Transform). The result carries `before` and `after`; a wrong field name
    comes back with the component's real `fields`.
    """
    project = Path(project_dir)
    if not str(component or "").strip() or not str(field or "").strip():
        return {"ok": False, "error": "component and field are required"}
    if scene and prefab:
        return {"ok": False, "error": "pass scene or prefab, not both"}
    env = {"BGATE_COMPONENT": component.strip(), "BGATE_FIELD": field.strip(),
           "BGATE_VALUE": _value_text(value), "BGATE_OBJECT": object_path or "",
           "BGATE_ADD": "1" if add else ""}
    if prefab:
        rel, err = _assets_rel(project, prefab, suffixes=(".prefab",))
        if err:
            return {"ok": False, "error": f"prefab: {err}"}
        env["BGATE_PREFAB"] = rel
    else:
        if not object_path:
            return {"ok": False, "error": "object_path is required for a scene edit "
                                          "(the hierarchy path, like Player)"}
        if scene:
            rel, err = _assets_rel(project, scene, suffixes=(".unity",))
            if err:
                return {"ok": False, "error": f"scene: {err}"}
            env["BGATE_SCENE"] = rel
    return _edit(project, "SetField", env, timeout=timeout)


# ---------------------------------------------------------------------------
# The player: a build that runs, for the bot and for unity_play
# ---------------------------------------------------------------------------
#: Lines in a player log that mean the game itself went wrong.
_PLAYER_ERROR_RE = re.compile(
    r"^(?:\w+Exception\b.*|.*\bError\b.*|Crash!!!.*)$", re.MULTILINE)
#: Player-log noise that matches the shape and is not the game's fault.
_PLAYER_NOISE = ("Fallback handler could not load library",
                 "d3d11: failed to create", "Shader Error in 'Hidden/",
                 "FMOD failed to initialize the output device")


def player_exe(project_dir: str | os.PathLike[str]) -> Path:
    return Path(project_dir) / PLAYER_REL / PLAYER_EXE


def _newest_source(project: Path) -> float:
    """The newest mtime under what a build reads: Assets/, ProjectSettings/
    and the package manifest."""
    newest = 0.0
    for sub in ("Assets", "ProjectSettings"):
        for dirpath, _dirs, files in os.walk(project / sub):
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    continue
    try:
        newest = max(newest, (project / "Packages" / "manifest.json").stat().st_mtime)
    except OSError:
        pass
    return newest


def build_player(project_dir: str | os.PathLike[str], *, timeout: int = 1800,
                 force: bool = False) -> dict:
    """A Windows standalone player of the enabled build scenes, through the
    bundled BGateBuild editor script, into Builds/BGatePlayer/.

    CACHED: a player newer than everything under Assets/ and ProjectSettings/
    is reused (`cached: true`), because a build is minutes and a bot run
    straight after a check should not pay it twice. `force` rebuilds.
    Installs the agent, telemetry and build scripts first when missing, since
    a player without them is a game no bot can drive.
    """
    project = Path(project_dir)
    if not is_project(project):
        return {"ok": False, "error": f"no {MARKER} in {project_dir}"}
    if os.name != "nt":
        return {"ok": False, "error": "player builds are Windows-only for now "
                                      "(StandaloneWindows64)"}
    installed = install_scripts(project, which=("telemetry", "agent", "build"))
    exe = player_exe(project)
    if not force and exe.is_file() and exe.stat().st_mtime >= _newest_source(project):
        return {"ok": True, "cached": True, "exe": str(exe), "install": installed}
    got = execute_method(str(project), BUILD_METHOD, timeout=timeout,
                         env={"BGATE_BUILD_OUT": str(exe)})
    got.update({"exe": str(exe), "cached": False, "install": installed})
    if got.get("ok") and not exe.is_file():
        got["ok"] = False
        got["error"] = ("BGateBuild exited clean but wrote no player; read "
                        "`output` for what it logged")
    elif not got.get("ok") and "no enabled scene" in got.get("output", ""):
        got["error"] = ("no enabled scene in Build Settings; add the scene to "
                        "boot (File > Build Profiles) and build again")
    return got


def player_args(exe: str | os.PathLike[str], *, log_path: str | os.PathLike[str],
                headless: bool = False) -> list[str]:
    """The command line for a player run. Headless is -batchmode -nographics:
    every Update() and physics step runs, nothing is drawn, no window opens."""
    args = [str(exe), "-logFile", str(log_path)]
    if headless:
        args += ["-batchmode", "-nographics"]
    else:
        args += ["-screen-fullscreen", "0", "-screen-width", "1280",
                 "-screen-height", "720"]
    return args


def player_log_errors(log: str) -> list[str]:
    """Exceptions and errors out of a player log, noise dropped, deduplicated."""
    out: list[str] = []
    for got in _PLAYER_ERROR_RE.finditer(log):
        line = got.group(0).strip()
        if not line or any(n in line for n in _PLAYER_NOISE) or line in out:
            continue
        out.append(line[:300])
    return out[:20]


def play(project_dir: str | os.PathLike[str], *, seconds: float = 10.0,
         scene: str = "", headless: bool = True, build_timeout: int = 1800) -> dict:
    """Build (or reuse) the player, run it for `seconds` of game time with
    telemetry on, and report what the game emitted and what it threw.

    The Unity answer to godot_run / web_run: proof the game RUNS, as
    against engine_check's proof that it compiles. `scene` is a Build
    Settings scene path or name to load first; empty boots the first.
    """
    project = Path(project_dir)
    built = build_player(project, timeout=build_timeout)
    if not built.get("ok"):
        return {**built, "ran": False}
    out_dir = _scratch(project)
    stamp = int(time.time())
    telemetry = out_dir / f"play_{stamp}.jsonl"
    log_path = out_dir / f"play_{stamp}.log"
    env = {**os.environ, "BGATE_TELEMETRY": str(telemetry),
           "BGATE_AUTOQUIT": str(float(seconds))}
    if scene:
        # BGateAgent switches scenes; with no BGATE_AGENT_URL it does only that.
        env["BGATE_SCENE"] = scene
    wall_cap = float(seconds) * 4 + 60
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(player_args(built["exe"], log_path=log_path,
                                            headless=headless),
                                cwd=str(Path(built["exe"]).parent), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError as exc:
        return {"ok": False, "ran": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        code = proc.wait(timeout=wall_cap)
    except subprocess.TimeoutExpired:
        proc.kill()
        code, timed_out = proc.wait(), True
    events: list[dict] = []
    try:
        for line in telemetry.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    errors = player_log_errors(log)
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    quit_clean = any(e.get("kind") == "autoquit" for e in events)
    out = {"ok": quit_clean and not errors and not timed_out, "ran": True,
           "exit_code": code, "seconds": round(time.monotonic() - started, 2),
           "game_seconds": seconds, "events": kinds, "errors": errors,
           "telemetry_path": str(telemetry), "log_path": str(log_path),
           "exe": built["exe"], "cached_build": built.get("cached", False),
           "headless": headless}
    if timed_out:
        out["error"] = (f"the player was still running after {wall_cap:.0f}s "
                        "of wall time and was killed")
    elif not events:
        out["error"] = ("the player wrote no telemetry; is "
                        f"Assets/BGate/{TELEMETRY_FILE} in the build?")
    elif not quit_clean:
        out["error"] = "the player exited before its autoquit (a crash?); read log_path"
    elif errors:
        out["error"] = f"{len(errors)} error line(s) in the player log; the first is {errors[0]}"
    return out


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
