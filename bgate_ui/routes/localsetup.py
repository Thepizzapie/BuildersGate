"""Local generators and coding-agent CLIs: read the setup, fix the setup.

TWO SECTIONS, ONE SURFACE, and they are here together because they answer the
same question from three directions. "Can I generate a 2D image right now" is
answered by a hosted key (``/api/providers``), by a local runtime (here), or not
at all; "can I dispatch an agent right now" is answered by a CLI being installed
and wired (also here). Three registries, one page, no page that owns two of them
and hides the third.

WHAT IS DELIBERATELY ABSENT: any endpoint that starts a process. This dashboard
does not run the user's ComfyUI and does not launch their CLI. See
``bgate_core.localruntimes``' module docstring for the argument; the short
version is that the command is unknowable, every interesting failure is on the
far side of it, and an orphaned process holding 8 GB of VRAM is a worse outcome
than a sentence telling somebody to start it themselves.

WRITING IS HUMAN-ONLY, on the same mechanism and for the same shape of reason as
``PATCH /api/settings`` and ``/api/providers/{id}/key``. The two writes here are
not the same size and both are gated anyway:

  * a runtime config value is a path in the project's .env — small, but an agent
    that can repoint BGATE_COMFY_T2I_WORKFLOW can silently change what every
    subsequent generation runs;
  * an MCP registration changes what EVERY future session of that CLI can do, on
    the whole machine, outside this project. Nothing dispatched gets to do that.

THE READS ARE GETs AND CARRY NO SECRET. A runtime's config is addresses and
filesystem paths, which is exactly why they can be returned verbatim where a key
cannot — see localruntimes' docstring, which makes that choice deliberately
rather than by omission. The CLI section returns interpreter paths and the
contents of an ``mcp add`` command line, which is the same string CLAUDE.md
prints.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from bgate_core import localruntimes as _local
from bgate_core import providers as _providers
from bgate_ui import agentcli as _agentcli
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _payload(probe: bool = True) -> dict:
    rows = _local.status(root(), probe=probe)
    return {
        "runtimes": rows,
        "stages": _local.STAGES,
        # THE SAME SENTENCE `bgate doctor` PRINTS, built by the same function.
        # Two phrasings of one fact is its own kind of confusion.
        "summary": _local.summary(rows),
        # Shared with /api/providers on purpose: the dashboard groups a local
        # runtime and a hosted provider into the same "2D images" section, and
        # that grouping only means something if both name the capability out of
        # one vocabulary.
        "capabilities": _providers.CAPABILITIES,
        "ready": [r["id"] for r in rows if r["available"]],
    }


@router.get("/api/local/runtimes")
def local_runtimes(probe: int = Query(1, ge=0, le=1)) -> dict:
    """Every local generator, its configuration and its stage.

    ``probe=1`` (the default) does one short GET per address. That is what makes
    "start it yourself and this notices" true without a reload — the panel polls
    this gently and the stage flips on its own.
    """
    return api.ok(_payload(probe=bool(probe)))


@router.get("/api/local/runtimes/{runtime_id}")
def local_runtime(runtime_id: str) -> dict:
    try:
        return api.ok(_local.status_for(root(), runtime_id))
    except _local.LocalConfigError as exc:
        raise api.not_found(str(exc), runtime=runtime_id)


@router.get("/api/local/runtimes/{runtime_id}/inspect")
def local_inspect(runtime_id: str) -> dict:
    """Everything the running server and the configured graphs will tell us.

    Deliberately a separate endpoint from the list: it is several HTTP reads
    against the user's server and belongs behind an expanded card, not on every
    poll of a page that may not even be open at this section.
    """
    try:
        return api.ok(_local.inspect(root(), runtime_id))
    except _local.LocalConfigError as exc:
        raise api.not_found(str(exc), runtime=runtime_id)


@router.post("/api/local/runtimes/{runtime_id}/config")
def local_set_config(runtime_id: str, request: Request, payload: dict) -> dict:
    """Set one value. An empty string clears it — that is what the UI sends when
    a field is emptied, and routing it to the clear path is less surprising than
    refusing it or storing a blank."""
    api.require_human(api.current_actor(request), "change local generator setup")
    body = payload if isinstance(payload, dict) else {}
    env = body.get("env")
    value = body.get("value")
    if not isinstance(env, str) or not env.strip():
        raise api.bad_request('send {"env": "BGATE_...", "value": "..."}')
    if value is not None and not isinstance(value, str):
        raise api.bad_request("value must be a string")
    try:
        row = _local.set_field(root(), runtime_id, env.strip(), value or "",
                               actor=api.current_actor(request))
    except _local.LocalConfigError as exc:
        raise api.bad_request(str(exc), runtime=runtime_id, env=env)
    except OSError as exc:
        raise api.unavailable(
            f"could not write the project's .env: {type(exc).__name__}: {exc}",
            runtime=runtime_id)
    return api.ok({**_payload(), "applied": row})


@router.delete("/api/local/runtimes/{runtime_id}/config")
def local_clear_config(runtime_id: str, request: Request,
                       env: str = Query(...)) -> dict:
    api.require_human(api.current_actor(request), "change local generator setup")
    try:
        row = _local.clear_field(root(), runtime_id, env,
                                 actor=api.current_actor(request))
    except _local.LocalConfigError as exc:
        raise api.bad_request(str(exc), runtime=runtime_id, env=env)
    except OSError as exc:
        raise api.unavailable(
            f"could not write the project's .env: {type(exc).__name__}: {exc}",
            runtime=runtime_id)
    return api.ok({**_payload(), "applied": row})


# ---------------------------------------------------------------------------
# Coding-agent CLIs
# ---------------------------------------------------------------------------


@router.get("/api/local/agents")
def agent_clis() -> dict:
    """Which coding-agent CLIs are installed, and whether Builders Gate is
    actually wired into each one's own interactive sessions."""
    return api.ok(_agentcli.payload())


@router.post("/api/local/agents/{runner_id}/register")
def agent_register(runner_id: str, request: Request) -> dict:
    """Register the Builders Gate MCP server with one CLI, pinned to this
    interpreter.

    HUMAN-ONLY, and this is the strongest case for the gate on this page: the
    write lands OUTSIDE the project, in the user's home directory, and changes
    what every future session of that CLI can reach — including sessions that
    have nothing to do with this game. An agent that could do this could widen
    its own successors' capabilities.
    """
    api.require_human(api.current_actor(request), "register an MCP server")
    got = _agentcli.register(runner_id)
    if not got.get("ok"):
        raise api.bad_request(str(got.get("error") or "registration failed"),
                              runner=runner_id,
                              command_line=got.get("command_line", ""))
    _log(f"registered the Builders Gate MCP server with {runner_id}",
         ref=runner_id, request=request)
    return api.ok({**_agentcli.payload(), "applied": got})


@router.delete("/api/local/agents/{runner_id}/register")
def agent_unregister(runner_id: str, request: Request) -> dict:
    api.require_human(api.current_actor(request), "remove an MCP registration")
    got = _agentcli.unregister(runner_id)
    if not got.get("ok"):
        raise api.bad_request(
            str(got.get("output") or "the CLI would not remove it"),
            runner=runner_id)
    _log(f"removed the Builders Gate MCP registration from {runner_id}",
         ref=runner_id, request=request)
    return api.ok({**_agentcli.payload(), "applied": got})


@router.post("/api/local/agents/{runner_id}/verify")
def agent_verify(runner_id: str, request: Request) -> dict:
    """Ask the registered interpreter whether it can import the server.

    Human-only because it executes the command the config names — a one-line
    import with a bounded timeout, not the server itself. It is the only check
    that tells a working registration apart from one that merely looks right.
    """
    api.require_human(api.current_actor(request), "verify an MCP registration")
    return api.ok(_agentcli.verify(runner_id))


def _log(summary: str, *, ref: str, request: Request) -> None:
    """Best effort. A machine-wide config change is worth a ledger row; a
    project whose activity table will not take it must still get the change."""
    try:
        from bgate_core import activity
        activity.log(root(), "settings", summary, ref=ref,
                     actor=api.current_actor(request))
    except Exception:                                            # noqa: BLE001
        pass
