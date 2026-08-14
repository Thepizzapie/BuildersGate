"""Is the delivered cut actually Ogg Theora, and has anybody watched it?

TWO CLAIMS THE VIDEO SEAT MAKES THAT NOTHING WAS CHECKING.

1. "TRANSCODED". ``cinematic._view`` answers `playable` from the FILENAME —
   the install target ends in .ogv, so the engine could open it. That is the
   honest read of "is this take the one that plays" and it is not a read of the
   bytes at all. The failure this project has already shipped is precisely the
   one a suffix cannot see: a Gyan ffmpeg build whose libtheora writes malformed
   bitstreams produced .ogv files that Godot opens and cannot decode. So this
   module asks ffprobe what the container and the video stream really ARE, and
   an unmeasurable file is reported as unmeasurable rather than as a pass.

2. "WATCHED". The seat brief ends "a clip is not delivered until it is
   transcoded and a human has watched the assembled cut" — a sentence with no
   record behind it anywhere in the database. A human opening the file is the
   only event that can satisfy it, and it is recorded here, in
   ``.bgate/cine-watched.json``, next to causal_specs.json and tunables.json
   which are kept the same way. It is deliberately NOT a database table: the
   schema is migration-managed and a viewing note is not worth a migration.

NOTHING HERE ESTIMATES. When ffprobe is absent every row says so and the
untranscoded count is None, because "0 untranscoded" out of nothing measured is
the same lie as a green regression gate that ran no tests.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from bgate_core import cinematic as _cine

WATCH_FILE = "cine-watched.json"

# Windows: the same flag cinecut.py spawns ffprobe with, so a dashboard read
# does not flash a console window on every poll.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ffprobe() -> Optional[str]:
    """The ffprobe this machine has, or None. Sibling of ffmpegbin.resolve()."""
    return shutil.which("ffprobe")


def probe(path: str | os.PathLike[str], *, timeout: int = 30) -> dict:
    """What this file actually IS, per ffprobe. Never raises.

    ``demuxed`` is the load-bearing field: it means ffprobe walked the container
    and found streams. A file that exists, ends in .ogv and does not demux is
    the broken-libtheora case, and it is the one worth being loud about.
    """
    p = Path(path)
    out: dict[str, Any] = {
        "path": str(path), "exists": p.is_file(), "bytes": 0,
        "demuxed": False, "container": "", "video_codec": "",
        "audio_codec": "", "duration_s": None, "measured": False, "why": "",
    }
    if not out["exists"]:
        out["why"] = "the file is not on disk"
        return out
    try:
        out["bytes"] = p.stat().st_size
    except OSError:
        pass
    exe = ffprobe()
    if not exe:
        out["why"] = ("ffprobe is not on PATH, so nothing here is measured — "
                      "install ffmpeg and this becomes a real answer")
        return out
    out["measured"] = True
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        out["why"] = f"ffprobe could not be run: {exc}"
        out["measured"] = False
        return out
    if proc.returncode != 0:
        # THE INTERESTING FAILURE. ffprobe refusing a file that exists means the
        # bytes are not a container it can parse — which is what a malformed
        # Theora bitstream looks like from outside.
        out["why"] = ((proc.stderr or "").strip().splitlines() or
                      ["ffprobe rejected the file"])[-1]
        return out
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        out["why"] = "ffprobe returned output that is not JSON"
        return out

    streams = data.get("streams") or []
    out["demuxed"] = bool(streams)
    out["container"] = str((data.get("format") or {}).get("format_name") or "")
    for s in streams:
        kind = str(s.get("codec_type") or "")
        if kind == "video" and not out["video_codec"]:
            out["video_codec"] = str(s.get("codec_name") or "")
        elif kind == "audio" and not out["audio_codec"]:
            out["audio_codec"] = str(s.get("codec_name") or "")
    try:
        out["duration_s"] = round(float((data.get("format") or {})
                                        .get("duration") or 0), 3) or None
    except (TypeError, ValueError):
        pass
    if not streams:
        out["why"] = "ffprobe read the file and found no streams in it"
    return out


def _watch_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / WATCH_FILE


def watched(root: str | os.PathLike[str]) -> dict:
    """artifact id (as a string) -> {at, by}. Empty when nobody has watched."""
    try:
        raw = json.loads(_watch_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def mark_watched(root: str | os.PathLike[str], artifact_id: int, *,
                 actor: str = "") -> dict:
    """Record that a human opened this cut. Returns the new entry.

    The actor is stored because "somebody watched it" and "the agent that made
    it watched it" are different claims and only one of them is the gate.
    """
    log = watched(root)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "by": str(actor or "")}
    log[str(int(artifact_id))] = entry
    path = _watch_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")
    return entry


def survey(root: str | os.PathLike[str], *, sequence: str = "",
           limit: int = 50) -> dict:
    """Every kept cut, measured — not inferred from its file extension.

    `untranscoded` counts only files that were actually measured and are not
    Theora. When ffprobe is missing it is None, and the caller must say
    "unmeasured" rather than draw a zero.
    """
    rows: list[dict] = []
    have_probe = bool(ffprobe())
    log = watched(root)
    for item in _cine.kept(root, limit=limit):
        if sequence and str(item.get("sequence") or "") != sequence:
            continue
        target = str(item.get("installed_path") or "") or str(item.get("path") or "")
        full = Path(root) / target if target and not os.path.isabs(target) else Path(target)
        m = probe(full) if target else {
            "exists": False, "measured": False, "demuxed": False,
            "why": "this take was never installed, so there is no file to measure",
            "bytes": 0, "container": "", "video_codec": "", "audio_codec": "",
            "duration_s": None, "path": ""}
        aid = int(item.get("artifact_id") or 0)
        seen = log.get(str(aid))
        theora = bool(m.get("demuxed")) and m.get("video_codec") == "theora"
        rows.append({
            "artifact_id": aid,
            "logical_name": item.get("logical_name") or "",
            "sequence": item.get("sequence") or "",
            "kind": item.get("kind") or "",
            "installed": bool(item.get("installed")),
            "install_stale": bool(item.get("install_stale")),
            "godot_res": item.get("godot_res") or "",
            "target": target,
            # The three states a delivery can be in, and they are not the same:
            # measured-and-Theora, measured-and-not, and never measured.
            "theora": theora,
            "watched_at": (seen or {}).get("at") or "",
            "watched_by": (seen or {}).get("by") or "",
            **{k: m.get(k) for k in ("exists", "bytes", "demuxed", "container",
                                     "video_codec", "audio_codec",
                                     "duration_s", "measured", "why")},
        })

    measured = [r for r in rows if r["measured"] and r["exists"]]
    # `cinematic.assemble` registers the joined sequence as kind "cutscene";
    # individual takes are "shot". Falling back to every row keeps the count
    # honest on a project that has kept shots but never assembled them.
    cuts = [r for r in rows if r["kind"] == "cutscene"] or rows
    return {
        "probe": have_probe,
        "why": ("" if have_probe else
                "ffprobe is not on PATH — nothing below is measured, and a "
                "filename ending in .ogv is not evidence that Godot can decode it"),
        "rows": rows,
        "measured": len(measured),
        "untranscoded": (sum(1 for r in measured if not r["theora"])
                         if have_probe else None),
        # "Nobody has watched the assembled cut" is about the CUT, not about
        # every shot take, so the count is over the assembled artifacts when
        # there are any.
        "unwatched": sum(1 for r in cuts if not r["watched_at"]),
        "cuts": len(cuts),
    }
