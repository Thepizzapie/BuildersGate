"""Audio seat workspace endpoints.

The shared preview endpoint only serves images, so the audio workspace needs its
own file server for .wav/.ogg/.mp3 (so the browser can play sounds inline) plus a
listing of the project's sound assets. Everything is fail-safe: a missing audio
directory yields an empty list, never a 500.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from bgate_ui.deps import root

router = APIRouter()

# The only suffixes we will list or serve. Keep this narrow — this endpoint hands
# a raw file to the browser, so it must never serve anything but audio.
AUDIO_SUFFIXES = {".wav", ".ogg", ".mp3"}
_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}


def _safe_audio(root_dir: Path, rel: str) -> Path:
    """Resolve a project-relative path, refusing anything that escapes root or is
    not an audio file. Mirrors deps.safe_under but with the audio allow-list."""
    target = (root_dir / rel).resolve()
    try:
        target.relative_to(root_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path escapes the project root")
    if target.suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(415, "not an audio file")
    return target


def _audio_dirs(root_dir: Path) -> list[Path]:
    """Where sound assets live: <root>/game/assets/audio, plus <root>/audio if it
    exists. Only directories that actually exist are returned."""
    candidates = [root_dir / "game" / "assets" / "audio", root_dir / "audio"]
    return [d for d in candidates if d.is_dir()]


@router.get("/api/audio/file")
def audio_file(rel: str):
    """Serve a single audio file (project-relative `rel`) so the browser can play
    it. Refuses path traversal and non-audio suffixes."""
    r = root()
    target = _safe_audio(r, rel)
    if not target.is_file():
        raise HTTPException(404, "audio file not found")
    media = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    # no-cache, not no-store: without it the browser applies heuristic freshness
    # and can hand the lab a pre-edit copy of a file it just saved. The ETag
    # FileResponse already sets keeps the revalidation cheap.
    return FileResponse(target, media_type=media,
                        headers={"Cache-Control": "no-cache"})


@router.get("/api/audio/list")
def audio_list() -> dict:
    """Walk the project's audio directories for .wav/.ogg/.mp3 and return
    [{rel, name, bytes}]. Fail-safe: empty list if the directories are missing or
    anything goes wrong walking them."""
    sounds: list[dict] = []
    try:
        r = root()
        root_resolved = r.resolve()
        seen: set[str] = set()
        for d in _audio_dirs(r):
            for p in sorted(d.rglob("*")):
                try:
                    if not p.is_file() or p.suffix.lower() not in AUDIO_SUFFIXES:
                        continue
                    rel = p.resolve().relative_to(root_resolved).as_posix()
                    if rel in seen:
                        continue
                    seen.add(rel)
                    sounds.append({
                        "rel": rel,
                        "name": p.name,
                        "bytes": p.stat().st_size,
                    })
                except Exception:
                    continue
    except HTTPException:
        raise
    except Exception:
        return {"sounds": []}
    return {"sounds": sounds}
