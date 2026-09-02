"""Tunables, and what was measured at each value.

The gameplay seat's rule is *the measured number sits next to the knob — read it
before you turn it*, and until now the dashboard could show the knob and nothing
else. `bgate_core/design/tunables.py` joins three things that were already recorded and
never put together: the tunable snapshot every iteration takes, the playtest
sessions that ran while it was open, and the telemetry those sessions emitted.

READ ONLY. Changing a tunable is a source edit in the game's own scripts (or
`.bgate/tunables.json`), which belongs to the seat that holds that lane and to
the tools that respect it. A dashboard that could write them would be a fourth
way to change a number nobody could then attribute.
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core.design import tunables as _tunables
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/tunables")
def tunables_index(measured_only: bool = False) -> dict:
    """Every tunable the iterations have captured, with its history.

    `measured_only` drops the knobs nobody has played the game at. On a project
    with three hundred exported constants and four recorded sessions that is the
    difference between a page you can read and a wall — but it is off by
    default, because "nothing was measured here" is itself the answer most of
    the time and hiding it would make the panel look better than the evidence.
    """
    body = _tunables.measured(root())
    if measured_only:
        body["tunables"] = [t for t in body["tunables"] if t["sessions"]]
    return api.ok(body)
