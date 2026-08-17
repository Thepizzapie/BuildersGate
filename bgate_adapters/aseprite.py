"""Headless Aseprite adapter — palette conform, animation masters, exact export.

Three operations, all driven through ``aseprite -b``:

  * conform()  — snap a PNG to a fixed palette (or quantize to N colors) in
                 indexed mode with dithering off. This is what turns a
                 9,000-color "pixel art" generation into actual pixel art.
  * master()   — sheet PNG + animation timing -> a tagged .aseprite file, so a
                 human opens a playable, onion-skinnable animation instead of a
                 flat strip.
  * export()   — .aseprite -> sheet PNG + JSON with EXACT frame rects,
                 durations and tags. bgate_core.asejson turns that JSON into a
                 SpriteFrames .tres built from facts rather than a grid guess.

Aseprite is a paid product, so unlike ffmpeg there is no toolbin fetch entry:
it is discovered from BGATE_ASEPRITE, then PATH, then the usual install dirs,
and a red doctor row is an instruction to the human, not a button.

Lua sources live in this file as string constants. They are written to a
tempfile per run because ``--script`` takes a path; shipping them as package
data would drag packaging/build_exe.py into every script edit for no gain.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from glob import glob
from pathlib import Path
from typing import Sequence

# Windows: keep every subprocess from flashing a console window.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

#: The Lua scripting surface this adapter uses — json global, tag.repeats,
#: script params — is a 1.3 feature set. 1.2 installs exist and will not run
#: these scripts; report them as too old rather than as broken scripts.
MIN_VERSION = (1, 3)

_SEARCH_GLOBS = (
    r"C:\Program Files\Aseprite\Aseprite.exe",
    r"C:\Program Files (x86)\Aseprite\Aseprite.exe",
    r"C:\Program Files (x86)\Steam\steamapps\common\Aseprite\Aseprite.exe",
    r"C:\Program Files\Steam\steamapps\common\Aseprite\Aseprite.exe",
    "/Applications/Aseprite.app/Contents/MacOS/aseprite",
    "/usr/bin/aseprite",
    "/usr/local/bin/aseprite",
    os.path.expanduser("~/.steam/steam/steamapps/common/Aseprite/aseprite"),
)


class AsepriteNotFound(RuntimeError):
    pass


class AsepriteError(RuntimeError):
    """A run that failed, in words a panel or an agent can show."""


def find_aseprite() -> str:
    """Locate the Aseprite executable.

    An override naming something absent does NOT fall through to PATH —
    falling through hands back the very binary the override existed to avoid
    and reports success. Same rule as toolbin.resolve and find_blender, for
    the same reason.
    """
    override = os.environ.get("BGATE_ASEPRITE")
    if override:
        if not Path(override).exists():
            raise AsepriteNotFound(
                f"BGATE_ASEPRITE points at a missing file: {override}")
        return override

    on_path = shutil.which("aseprite")
    if on_path:
        return on_path

    for pattern in _SEARCH_GLOBS:
        hits = glob(pattern) if "*" in pattern else (
            [pattern] if Path(pattern).exists() else [])
        if hits:
            return hits[0]

    raise AsepriteNotFound(
        "Aseprite not found. Install it, put it on PATH, or set "
        "BGATE_ASEPRITE to the executable path.")


def available() -> dict:
    """Probe without running anything — for health checks and tool errors."""
    try:
        path = find_aseprite()
    except AsepriteNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def version() -> dict:
    exe = find_aseprite()
    proc = _spawn([exe, "-b", "--version"], timeout=60)
    first = (proc.stdout or "").strip().splitlines()
    return {"path": exe, "version": first[0] if first else "unknown"}


def parsed_version(text: str) -> tuple[int, ...]:
    """(1, 3, 18, 2) out of "Aseprite 1.3.18.2-x64", or () if unreadable."""
    match = _VERSION.search(text or "")
    if not match:
        return ()
    return tuple(int(g) for g in match.groups() if g is not None)


# See blender._spawn: stdin=DEVNULL is load-bearing under an MCP server whose
# stdin is the protocol channel — a child that inherits it can hang forever or
# steal bytes off the wire.
def _spawn(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


# ── running Lua ─────────────────────────────────────────────────────────────
#: Scripts report their result as one line: BGATE:{...json...}. Everything
#: else on stdout is Aseprite's own chatter and is ignored on success.
_RESULT = "BGATE:"


def _run_script(lua: str, params: dict[str, str], *, timeout: int = 120) -> dict:
    """Run one Lua script headless and return its BGATE: json line.

    Params travel as ``--script-param key=value``; the script reads them from
    ``app.params``. Values are strings on the Lua side — numbers are re-parsed
    there, and anything structured goes through as json text.
    """
    exe = find_aseprite()
    fd, path = tempfile.mkstemp(suffix=".lua", prefix="bgate-ase-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(lua)
        cmd = [exe, "-b"]
        for key, value in params.items():
            cmd += ["--script-param", f"{key}={value}"]
        cmd += ["--script", path]
        try:
            proc = _spawn(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise AsepriteError(f"aseprite timed out after {timeout}s") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(_RESULT):
            try:
                return json.loads(line[len(_RESULT):])
            except json.JSONDecodeError as exc:
                raise AsepriteError(f"unreadable result line: {line!r}") from exc
    tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    raise AsepriteError(
        f"aseprite exited {proc.returncode} without a result: {tail[-500:]}")


# ── conform: PNG -> indexed against a palette ───────────────────────────────
# Dithering stays OFF on purpose: dither noise is exactly the per-pixel
# unevenness this pass exists to remove.
_CONFORM_LUA = r"""
local p = app.params
local spr = app.open(p.src)
if not spr then print("BGATE:" .. json.encode({ok=false, error="unreadable: " .. p.src})) return end
if p.palette ~= nil and p.palette ~= "" then
  local pal = Palette(0)
  local n = 0
  for hex in string.gmatch(p.palette, "[^,]+") do
    pal:resize(n + 1)
    pal:setColor(n, Color{ r=tonumber(hex:sub(1,2),16),
                           g=tonumber(hex:sub(3,4),16),
                           b=tonumber(hex:sub(5,6),16) })
    n = n + 1
  end
  spr:setPalette(pal)
else
  app.command.ColorQuantization{ ui=false, maxColors=tonumber(p.colors) }
end
app.command.ChangePixelFormat{ format="indexed", dithering="none" }
local out = {}
local pal = spr.palettes[1]
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  out[#out + 1] = string.format("%02x%02x%02x", c.red, c.green, c.blue)
end
spr:saveCopyAs(p.out)
print("BGATE:" .. json.encode({ok=true, colors=#pal, palette=out}))
"""


def conform(src: str, out: str, *,
            palette: Sequence[Sequence[int]] = (),
            max_colors: int = 32, timeout: int = 120) -> dict:
    """Snap ``src`` to a palette, write ``out`` (PNG, alpha preserved).

    With ``palette`` (RGB triples): every opaque pixel maps to its nearest
    entry — the project-palette conform. Without: Aseprite quantizes to at
    most ``max_colors`` — the derive path palette_pin uses on style refs.

    Returns {ok, colors, palette: [hex...]}.
    """
    hexes = ",".join(f"{r:02x}{g:02x}{b:02x}" for r, g, b in palette)
    return _run_script(_CONFORM_LUA, {
        "src": str(src), "out": str(out),
        "palette": hexes, "colors": str(int(max_colors)),
    }, timeout=timeout)


# ── master: sheet + timing -> tagged .aseprite ──────────────────────────────
# Frames are ALL created before any tag is: Aseprite shifts existing tag
# ranges when frames are appended after them, so tags made mid-build come out
# spanning the whole timeline (measured on 1.3.18).
_MASTER_LUA = r"""
local p = app.params
local sheet = app.open(p.sheet)
if not sheet then print("BGATE:" .. json.encode({ok=false, error="unreadable: " .. p.sheet})) return end
local cw, ch = tonumber(p.cw), tonumber(p.ch)
local cols = sheet.width // cw
local src = Image(sheet.spec)
src:drawSprite(sheet, 1)

local spr = Sprite(cw, ch, sheet.colorMode)
-- anims spec: name:loop:d1,d2,...|name:loop:d1,...  (durations in ms, one
-- per frame; frame count is the duration count)
local ranges = {}
local frame_i = 0
local first = true
for entry in string.gmatch(p.anims, "[^|]+") do
  local name, loop, durs = string.match(entry, "([^:]+):(%w+):([%d,]+)")
  local from = first and 1 or (#spr.frames + 1)
  for dur in string.gmatch(durs, "[^,]+") do
    local fr
    if first then fr = spr.frames[1]; first = false
    else fr = spr:newEmptyFrame() end
    fr.duration = tonumber(dur) / 1000
    local img = Image(cw, ch, sheet.colorMode)
    local sx = (frame_i % cols) * cw
    local sy = (frame_i // cols) * ch
    for y = 0, ch - 1 do
      for x = 0, cw - 1 do
        img:drawPixel(x, y, src:getPixel(sx + x, sy + y))
      end
    end
    spr:newCel(spr.layers[1], fr, img, Point(0, 0))
    frame_i = frame_i + 1
  end
  ranges[#ranges + 1] = {name=name, from=from, to=#spr.frames, loop=(loop == "loop")}
end
local tags = 0
for _, r in ipairs(ranges) do
  local tag = spr:newTag(r.from, r.to)
  tag.name = r.name
  if not r.loop then tag.repeats = 1 end
  tags = tags + 1
end
spr:saveAs(p.out)
print("BGATE:" .. json.encode({ok=true, frames=#spr.frames, tags=tags}))
"""


def master(sheet: str, out: str, *, cell: tuple[int, int],
           anims: Sequence[dict], timeout: int = 180) -> dict:
    """Build a tagged .aseprite from a stitched sheet.

    ``anims``: [{"name", "durations_ms": [..per frame..], "loop": bool}] in
    sheet order — reading order over the grid, which is how every emitter in
    bgate_adapters/sprites.py lays frames out.

    Returns {ok, frames, tags}.
    """
    parts = []
    for anim in anims:
        durs = [str(max(1, int(d))) for d in anim["durations_ms"]]
        if not durs:
            raise AsepriteError(f"animation {anim.get('name')!r} has no frames")
        name = str(anim["name"])
        if ":" in name or "|" in name:
            raise AsepriteError(f"animation name {name!r} cannot carry ':' or '|'")
        loop = "loop" if anim.get("loop", True) else "once"
        parts.append(f"{name}:{loop}:{','.join(durs)}")
    return _run_script(_MASTER_LUA, {
        "sheet": str(sheet), "out": str(out),
        "cw": str(int(cell[0])), "ch": str(int(cell[1])),
        "anims": "|".join(parts),
    }, timeout=timeout)


# ── export: .aseprite -> sheet PNG + exact JSON ─────────────────────────────
def export(master_path: str, sheet_out: str, data_out: str, *,
           timeout: int = 120) -> dict:
    """Re-export a master to sheet + frame-data JSON. Returns the parsed JSON.

    ``--sheet-type rows`` keeps the layout an artist can read; the .tres never
    guesses anyway because every rect is in the JSON.
    """
    exe = find_aseprite()
    src = Path(master_path)
    if not src.is_file():
        raise AsepriteError(f"no such master: {master_path}")
    proc = _spawn([exe, "-b", str(src),
                   "--sheet", str(sheet_out), "--data", str(data_out),
                   "--sheet-type", "rows", "--list-tags", "--list-slices",
                   "--format", "json-array"], timeout=timeout)
    if proc.returncode != 0 or not Path(data_out).is_file():
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        raise AsepriteError(f"export failed ({proc.returncode}): {tail[-500:]}")
    with open(data_out, encoding="utf-8") as fh:
        return json.load(fh)
