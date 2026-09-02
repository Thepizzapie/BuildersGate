"""First run over HTTP: make a project from the dashboard.

The audit's single blocker was that nothing in the product could CREATE a
project — the shell assumed one that already existed, built by someone who had
already registered an MCP server and knew to call project_init. This is the same
two steps ``bgate init`` runs (scaffold a runnable game, then stamp the store)
behind the one endpoint the first-run screen posts to.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request

from bgate_core.board import activity as _activity
from bgate_core.store import modules as _modules
from bgate_core.store import project as _project
from bgate_core.store import scaffold as _scaffold
from bgate_core.store import settings as _settings
from bgate_core.store.util import slugify
from bgate_ui import api
from bgate_ui.deps import root as _root

router = APIRouter()


def _unsuitable(d: Path) -> bool:
    """True if `d` is somewhere a game project must never be created.

    `bgate serve` is run from a terminal you are already standing in, so the cwd
    is the right default there. A double-clicked executable has no such cwd: a
    shortcut without a "Start in", or a launch from the Run dialog, inherits
    C:\\Windows\\system32. The first-run screen offered to unpack a Godot game
    into it, and the create failed with a raw PermissionError repr in a red box.

    Being unwritable is not the only disqualifier — an elevated process CAN
    write to system32, and that is worse than the failure, not better.
    """
    try:
        d = d.resolve()
    except OSError:
        return True
    if d.parent == d:                       # a drive root, C:\ or /
        return True
    parts = {p.lower() for p in d.parts}
    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramData"):
        base = os.environ.get(var)
        if base:
            try:
                d.relative_to(Path(base).resolve())
                return True
            except ValueError:
                pass
    if {"windows", "system32", "syswow64"} & parts:
        return True
    return not os.access(d, os.W_OK)


def default_parent() -> Path:
    """The directory a new project is created under.

    The cwd when that is a real working directory, and ~/BuildersGate when it is
    not. The fallback is created lazily by the scaffolder, not here — reading
    the first-run form must not have side effects on disk.
    """
    cwd = Path.cwd()
    if not _unsuitable(cwd):
        return cwd
    return Path.home() / "BuildersGate"


def _target(name: str, dest: str) -> Path:
    """Where a new project goes. A NEW directory under `default_parent()` —
    unpacking a game into whatever directory the server happened to start in is
    a data-loss bug wearing a feature's hat."""
    if dest:
        return Path(dest).expanduser().resolve()
    return (default_parent() / slugify(name)).resolve()


@router.get("/api/project")
def project_read() -> dict:
    """The active project (or null), plus everything the create form needs.

    ``cwd`` is here so the form can tell the user the exact directory it is about
    to write to before they commit — the audit's other complaint about
    project_init was that it never said where.
    """
    body: dict = {
        "project": None,
        "root": None,
        # NOT Path.cwd(): the form renders this as "will be created at
        # <cwd>\<slug>", and a double-clicked exe has a cwd of system32.
        "cwd": str(default_parent()),
        "templates": _scaffold.list_templates(),
        "kinds": list(_scaffold.KINDS),
        "known": _project.known_projects(),
        # The optional-feature checklist the first-run card renders — every
        # module with its blurb and the pip command that lights it up fully,
        # so "what gets installed" is a choice made where the choosing is.
        "modules": _modules.catalog(),
    }
    try:
        found = _root()
        body["root"] = str(found)
        body["project"] = _project.get(found)
    except Exception:
        pass  # no project yet is the whole reason this endpoint exists
    return api.ok(body)


@router.post("/api/project")
def project_create(request: Request, payload: dict) -> dict:
    """Scaffold a game and initialise its store. Returns the absolute root.

    Creating the studio's project is a human act, not something an agent may do
    on its own initiative — same rule as editing the bible that bounds it.
    """
    api.require_human(api.current_actor(request), "create a project")

    name = (payload.get("name") or "").strip()
    if not name:
        raise api.bad_request("a project needs a name")
    kind = (payload.get("kind") or "2d").strip()
    if kind not in _scaffold.KINDS:
        raise api.bad_request(
            f"kind must be one of {'|'.join(_scaffold.KINDS)}", kind=kind)

    root = _target(name, (payload.get("path") or "").strip())
    try:
        made = _scaffold.new_project(root, name, kind=kind,
                                     force=bool(payload.get("force")))
    except FileExistsError as exc:
        # Not a bad request — the request was fine, the directory is occupied.
        # `force` is offered in the detail so the UI can render the choice.
        raise api.conflict(str(exc), path=str(root), force_available=True)
    except PermissionError as exc:
        # Reachable even with the default_parent() guard: the operator can type
        # any path into the form. Say where and what to do instead of leaking
        # "[WinError 5] Access is denied: 'C:\\\\Windows\\\\System32\\\\...'".
        raise api.bad_request(
            f"cannot write to {root.parent} — choose somewhere you own, "
            f"such as {Path.home() / 'BuildersGate'}",
            path=str(root), suggested=str(Path.home() / "BuildersGate"),
        ) from exc
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise api.bad_request(str(exc))

    project = _project.init(root, name, pitch=(payload.get("pitch") or "").strip(),
                            engine="godot", dimension=kind)

    # THE PROJECT'S MODULE CHOICE IS SEEDED, NOT ASKED. The first-run card
    # asked (a checklist between the template cards and Create) and the owner
    # called it what it was: an installer question on a create-a-game form.
    # What gets installed is the setup wizard's component page, which writes
    # the MACHINE defaults; a new project inherits those, plus the 2D rule
    # (a 2D game does not open Blender), plus anything an API caller passed
    # explicitly. Settings > Modules is where a project changes its mind.
    off_set = _modules.machine_defaults()
    off_set |= {str(m).strip() for m in (payload.get("modules_off") or [])
                if str(m).strip()}
    if kind == "2d":
        off_set.add("three_d")
    off = sorted(off_set)
    if off:
        try:
            _settings.set(root, "modules.disabled", off)
        except Exception as exc:
            # The project exists and works; a failed preference write must not
            # roll that back. Say so instead.
            _activity.log(root, "project",
                          f"could not store module choices ({exc}) — "
                          "set them in Settings", seat="director")

    # Point the RUNNING server at what it just made. _root() reads BGATE_ROOT
    # first and otherwise walks up from the cwd — and the new project is a
    # directory below the cwd, which walking up will never find. Without this
    # the dashboard would create a project and then keep reporting none.
    os.environ["BGATE_ROOT"] = str(root)

    return api.ok({
        "root": str(root),
        "project": project,
        "kind": kind,
        "files": len(made["files"]),
        # The dashboard token is minted per project, and this page was served
        # before the project existed — the client has to reload to get one.
        "reload": True,
    })


@router.post("/api/project/select")
def project_select(request: Request, payload: dict) -> dict:
    """Point the running dashboard at a project that already exists.

    THE FIRST-RUN SCREEN COULD ONLY CREATE. Every other route into this product
    — `bgate use`, `bgate adopt`, the MCP project_select tool — has always been
    able to pick up a project already on disk, but the one screen a new user
    actually meets offered a name field and a Create button, so someone with six
    registered games who opened the dashboard from the wrong directory was told
    there was no project and invited to make a seventh. The registry the screen
    needs was already in the GET's ``known`` and simply had no button attached.

    Takes ``root`` (a path) or ``name`` (a registry key). The registry is the
    convenience, not the authority: a path is validated on its own merits, so a
    project that was never registered can still be opened.

    Two writes, deliberately:

    · ``BGATE_ROOT`` in this process, because deps.root() reads it first and
      otherwise WALKS UP FROM THE CWD. Without it a server started inside one
      project — or inside this repository, which has a .bgate of its own —
      would keep serving that one however hard the user clicked, since the walk
      wins over the remembered pointer. Same reason project_create sets it.
    · the machine-wide active pointer, so `bgate use` agrees and the choice
      survives a restart. Best-effort: a read-only home directory is not a
      reason to refuse a switch that already worked in memory.

    Human-gated like creation. An agent that can repoint the dashboard at
    another game can write into it through every other route on the server.
    """
    api.require_human(api.current_actor(request), "switch projects")

    raw = (payload.get("root") or "").strip()
    if not raw:
        name = (payload.get("name") or "").strip()
        known = _project.known_projects()
        if name not in known:
            raise api.bad_request(
                f"no project named {name!r} is registered", known=sorted(known))
        raw = known[name]

    target = Path(raw).expanduser()
    try:
        target = target.resolve()
    except OSError as exc:
        raise api.bad_request(f"cannot resolve {raw} — {exc}")

    try:
        _project.set_active(target)
    except LookupError as exc:
        # set_active refuses a directory with no store, and its message already
        # names adopt vs init. That IS the answer for "I typed a game folder
        # that Builders Gate has never seen"; do not paper over it.
        raise api.bad_request(str(exc), path=str(target))
    except OSError:
        pass  # the pointer is a convenience; the switch below is the feature

    os.environ["BGATE_ROOT"] = str(target)

    try:
        selected = _project.get(target)
    except Exception:                                            # noqa: BLE001
        selected = None

    return api.ok({
        "root": str(target),
        "project": selected,
        # Same as creation: the page was served against the old root, token and
        # all. Everything on it is now stale.
        "reload": True,
    })
