"""The pad server's IDENTITY — its tool names and its registration document.

Everything here is data. There is no MCP import in this module and there must
never be one, because the two places that need this information are the two
places that must not pay for the SDK:

  * bgate_ui.agents.brainsession builds the ``--mcp-config`` document for every room
    turn. It was importing bgate_mcp.padserver to call config(), which
    constructs a FastMCP application at import time and pulls the whole SDK in
    behind it. In the frozen desktop build that chain reached
    mcp -> pydantic_settings -> its OPTIONAL cloud secret providers, and
    because one developer machine happened to have azure-storage-blob installed
    for something unrelated, PyInstaller resolved the Azure Key Vault provider
    and shipped azure.core, opentelemetry, grpc and protobuf inside the
    release. 20 MB, to build a dict of four strings.

  * bgate_ui.agents.runners is loaded by EVERY dispatch, including on machines with no
    MCP extra installed at all. It used to carry its own hand-copied duplicate
    of the tool-name tuple with a comment explaining that importing the real
    one was too expensive, and a test asserting the two had not drifted. The
    duplicate and the test that policed it both exist because of the import
    cost this module removes.

padserver re-exports both names, so ``padserver.config`` and
``padserver.TOOL_NAMES`` keep working for anything that already reaches for
them.
"""
from __future__ import annotations

import sys
from typing import Optional

# The pad server's whole surface, by the full CLI-side names --allowedTools
# matches against.
#
# Every name here is a READ of this project's database, a WRITE to its canon
# tables, or a message into this one room. There is no name here that files
# work, runs a command or touches a file in the game, and adding one would end
# the promise the pad server exists to keep. Keep this list in sight of that
# sentence: it is worth nothing if nobody checks how small it still is.
TOOL_NAMES = ("mcp__pads__pad_read", "mcp__pads__pad_draw",
              "mcp__pads__board_read", "mcp__pads__canon_read",
              "mcp__pads__bible_write", "mcp__pads__lore_write",
              "mcp__pads__lore_fact", "mcp__pads__lore_link",
              "mcp__pads__dialogue_list", "mcp__pads__dialogue_read",
              "mcp__pads__project_files", "mcp__pads__file_read",
              "mcp__pads__scene_tree", "mcp__pads__room_post")


def config(root: str, session_id: int, python: Optional[str] = None,
           seat: str = "") -> dict:
    """The ``--mcp-config`` document that registers THIS server and only this one.

    Built beside the server rather than in the spawner so the registration and
    the module path cannot disagree. The interpreter is the caller's absolute
    path for the same reason the install docs insist on one: a bare `python`
    resolves differently under a spawned CLI than in a shell, and the failure
    reads as "server not connected" with nothing pointing at the interpreter.
    """
    # The seat rides in the config rather than being read off the inherited
    # environment, because whether an MCP child inherits the spawner's env is a
    # property of the CLI, not something this module can promise. Every canon
    # row the server writes is attributed with it, so an unattributed write
    # would be a write nobody can trace back to a voice.
    env = {"BGATE_ROOT": str(root),
           "BGATE_BRAINSTORM_SESSION": str(int(session_id))}
    if str(seat or "").strip():
        env["BGATE_BRAINSTORM_SEAT"] = str(seat).strip()
    return {"mcpServers": {"pads": {
        "command": python or sys.executable,
        "args": ["-m", "bgate_mcp.padserver"],
        "env": env,
    }}}
