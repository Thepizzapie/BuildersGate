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

from bgate_core import project as _project
from bgate_core import scaffold as _scaffold
from bgate_core.util import slugify
from bgate_ui import api
from bgate_ui.deps import root as _root

router = APIRouter()


def _target(name: str, dest: str) -> Path:
    """Where a new project goes. A NEW directory under the cwd by default —
    unpacking a game into whatever directory the server happened to start in is
    a data-loss bug wearing a feature's hat."""
    if dest:
        return Path(dest).expanduser().resolve()
    return (Path.cwd() / slugify(name)).resolve()


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
        "cwd": str(Path.cwd()),
        "templates": _scaffold.list_templates(),
        "kinds": list(_scaffold.KINDS),
        "known": _project.known_projects(),
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
    except (ValueError, FileNotFoundError) as exc:
        raise api.bad_request(str(exc))

    project = _project.init(root, name, pitch=(payload.get("pitch") or "").strip(),
                            engine="godot", dimension=kind)

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
