"""Install the external tools the app needs, from inside the app.

The rule this serves, in one line: IF THE APP CAN NAME WHAT IS MISSING, THE APP
CAN FETCH IT. A panel that prints `pip install` or "run from a source checkout"
at somebody who installed a .exe has told them their install was a mistake.

See bgate_core/toolbin for what is fetched, from where, and why every URL and
digest is written down rather than discovered.

NOTHING HERE IS SPECULATIVE. There is no "install everything" call and no
install on first run: a tool is fetched when a human presses the button on the
panel that needs it, which is what keeps the download small for the people who
never open the cutscene tools.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter

from bgate_core.runtime import toolbin
from bgate_ui import api as _api

router = APIRouter()

# One install at a time, and what it is doing, so the panel can show progress
# without holding a request open for a 90-second download.
_lock = threading.Lock()
_running: dict[str, dict] = {}


@router.get("/api/tools")
def tools_list() -> dict:
    """Every managed tool, whether it is here, and where it came from."""
    return {"ok": True, "tools": toolbin.statuses(),
            "dir": str(toolbin.bin_dir()),
            "running": dict(_running)}


@router.get("/api/tools/{name}")
def tool_status(name: str) -> dict:
    if name not in toolbin.TOOLS:
        return {"ok": False, "error": f"no such tool: {name}"}
    return {"ok": True, **toolbin.status(name), "running": _running.get(name)}


@router.post("/api/tools/{name}/install")
def tool_install(name: str) -> dict:
    """Start fetching a tool. Returns immediately; poll /api/tools/{name}.

    HUMAN-GATED. This downloads an executable and puts it on the machine, which
    is not something an agent should be able to do on its own behalf — the same
    reasoning that keeps provider keys out of the MCP surface.
    """
    _api.require_human("tools", "install")
    if name not in toolbin.TOOLS:
        return {"ok": False, "error": f"no such tool: {name}"}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "another tool is installing"}

    _running[name] = {"done": 0, "total": 0, "state": "downloading"}

    def work():
        try:
            def progress(done: int, total: int) -> None:
                _running[name] = {"done": done, "total": total,
                                  "state": "downloading"}
            toolbin.install(name, progress=progress)
            _running[name] = {"state": "done"}
        except Exception as exc:                                # noqa: BLE001
            # The message is written for the panel, not for a log: toolbin
            # raises ToolError with text a non-technical user can act on.
            _running[name] = {"state": "failed", "error": str(exc)}
        finally:
            _lock.release()

    threading.Thread(target=work, name=f"bgate-install-{name}",
                     daemon=True).start()
    return {"ok": True, "name": name, "state": "downloading",
            "size_mb": toolbin.TOOLS[name].size_mb}
