"""The dependency report, over HTTP.

`bgate doctor` answers "can this machine actually do the work" in one pass
without opening the microphone or spending money. The dashboard needs the same
answer for the same reason, and needed it badly: the playtest preflight was
polling every 15 seconds forever, opening the mic and spawning a whisper probe
each time, purely to decide whether to grey out the record button.

This is the cheap path the frontend tries first. The expensive preflight stays
for the pre-*session* check, where a muted mic genuinely is invisible any other
way.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from bgate_core.runtime import doctor as _doctor
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/doctor")
def dependency_report(refresh: bool = Query(False)) -> dict:
    """Every external dependency, one dict, no side effects.

    Cached a few seconds inside `doctor.check`, so a poll costs nothing —
    `refresh=1` forces the probes to run again after the user installs
    something and wants to see it turn green.
    """
    try:
        project_root = str(root())
    except Exception:
        project_root = ""  # no project yet: the toolchain question still stands

    report = _doctor.check(project_root or None, refresh=refresh)
    missing = [name for name, row in report.items() if not row["available"]]
    return api.ok(report, summary={
        "ok": not missing,
        "missing": missing,
        "text": _doctor.summary(report),
    })
