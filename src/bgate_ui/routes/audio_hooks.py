"""The audio seat's Hooks panel — the events the game asks for, and the answer.

The library listing was already served (routes/audio_ws.py) and it is the easy
half: files on disk. This is the half that decides whether anybody hears them.
Both halves of the answer need the project's source tree and ffmpeg, so both
happen here rather than in a browser that has neither.

CACHED, BECAUSE THE WORKSPACE POLLS. A full source scan of a real project is
~500 files and over a second; the loudness pass shells ffmpeg once per bound
file. Neither answer can change while nothing on disk changes, so the scan is
held for ``TTL_S`` and the measurements are held by content in
``bgate_core.audio.loudness``. ``refresh=1`` forces both.

Auto-registers via routes/__init__.py.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from bgate_core.audio import audiohooks as _hooks
from bgate_core.audio import loudness as _loudness
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

#: How long a scan is trusted. Long enough that a 15 s poll costs nothing, short
#: enough that somebody who just wired a cue sees it without a reload.
TTL_S = 45.0

# project root -> (when, payload)
_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/api/audio/hooks")
def audio_hooks(refresh: bool = False, loud: bool = True) -> dict:
    """Every sound the game's own code asks for, and whether a file answers.

    ``{events, dynamic, unresolved_paths, orphans, scanned_files, sound_count,
    unbound, target_lufs, loudness_available, loudness_note}``. Each event
    carries ``{event, family, name, file, state, sites, n, lufs, true_peak,
    loudness_state, loudness_note}``.

    ``lufs`` is None wherever it was not measured — no ffmpeg, or a one-shot
    shorter than the 400 ms EBU gate — and ``loudness_note`` says which. There
    is no default value for this column.
    """
    r = root()
    key = str(r)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < TTL_S:
        scan = hit[1]
    else:
        scan = _hooks.scan(r)
        scan["orphans"] = _hooks.orphans(r, scan["events"])
        _CACHE[key] = (now, scan)

    available = _loudness.available()
    events = []
    for row in scan["events"]:
        e = dict(row)
        e["lufs"] = None
        e["true_peak"] = None
        e["loudness_state"] = ""
        e["loudness_note"] = ""
        if loud and available and e.get("file"):
            m = _loudness.measure(r / str(e["file"]))
            e["lufs"] = m["lufs"]
            e["true_peak"] = m["true_peak"]
            e["loudness_note"] = m["reason"]
            e["loudness_state"] = _loudness.verdict(m["lufs"])
        elif e.get("file") and not available:
            e["loudness_note"] = "no ffmpeg — loudness cannot be measured here"
        events.append(e)

    return api.ok({
        "events": events,
        "dynamic": scan["dynamic"],
        "unresolved_paths": scan["unresolved_paths"],
        "orphans": scan.get("orphans", []),
        "scanned_files": scan["scanned_files"],
        "sound_count": scan["sound_count"],
        "unbound": sum(1 for e in events if e["state"] == "unbound"),
        "target_lufs": _loudness.TARGET_LUFS,
        "tolerance_lu": _loudness.TOLERANCE_LU,
        "loudness_available": available,
        "loudness_note": "" if available else
        "no ffmpeg on this machine — set BGATE_FFMPEG or put one in ~/.bgate/bin",
    })
