"""Keep the in-app playable build honest.

The dashboard's /play tab serves export/web/. If that export is older than the
game source, the human plays a stale build and — reasonably — concludes their
changes were ignored. That happened, and it wasted a morning. So the build is
checked for staleness and rebuilt on demand: what you play is always what the
source says.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _godot() -> str | None:
    from bgate_adapters import godot
    try:
        return godot.find_godot()
    except Exception:
        return None


def _newest_source_mtime(game_dir: Path) -> float:
    """Newest mtime among things a build depends on — scripts, scenes, assets,
    project config. Ignores the .godot cache and the export output itself."""
    latest = 0.0
    for sub in ("scripts", "scenes", "assets"):
        for p in (game_dir / sub).rglob("*"):
            if p.is_file():
                latest = max(latest, p.stat().st_mtime)
    cfg = game_dir / "project.godot"
    if cfg.exists():
        latest = max(latest, cfg.stat().st_mtime)
    return latest


def status(root: str | os.PathLike[str]) -> dict:
    """Is there a build, and is it current with the source?"""
    game = Path(root) / "game"
    pck = Path(root) / "export" / "web" / "index.pck"
    if not (game / "project.godot").exists():
        return {"built": False, "stale": True, "reason": "no game project"}
    if not pck.exists():
        return {"built": False, "stale": True, "reason": "never exported"}
    src = _newest_source_mtime(game)
    stale = pck.stat().st_mtime < src
    return {"built": True, "stale": stale,
            "build_mtime": pck.stat().st_mtime, "source_mtime": src}


def rebuild(root: str | os.PathLike[str], timeout: int = 240) -> dict:
    """Export the Web build from current source. What /play serves next."""
    game = Path(root) / "game"
    if not (game / "project.godot").exists():
        return {"ok": False, "error": "no game project at this root"}
    if not (game / "export_presets.cfg").exists():
        return {"ok": False, "error": "no export_presets.cfg — the tech seat "
                                      "must create the Web preset first"}
    godot = _godot()
    if not godot:
        return {"ok": False, "error": "Godot not found (set BGATE_GODOT)"}

    out = Path(root) / "export" / "web"
    out.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [godot, "--headless", "--path", str(game),
             "--export-release", "Web", str(out / "index.html")],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"export timed out after {timeout}s"}

    pck = out / "index.pck"
    if not pck.exists():
        return {"ok": False, "error": "export produced no build",
                "detail": (proc.stderr or proc.stdout or "")[-500:]}
    return {"ok": True, "bytes": pck.stat().st_size,
            "wasm": (out / "index.wasm").stat().st_size
                    if (out / "index.wasm").exists() else 0}
