"""Screen audit - the composition faults a screenshot shows and no asset
check measures.

Reads a `godot_evidence` manifest (screen-space bounds per node, plus the
texture size behind every sprite and the string width of every label) and
answers four questions about the FRAME, not the files:

  1. Is the party the size the bible says, and are enemies within a sane
     ratio of it? scale_check measures one asset alone; on boswell the party
     shipped at ~40px against 200-300px enemies with every asset "in scale".
  2. Does every label's text fit its rect? "El Nino Flood (The Return of"
     was clipped in the enemy window and nothing said so.
  3. Is a bitmap font drawn at a non-integer multiple of its native size?
     That is the blurry text - an 8px .fnt at 12px is resampled.
  4. Do the sprites and the backdrop share a pixel density? Plates painted
     1:1 under sprites drawn at 2x read as two games glued together.

Pure functions over the manifest dict, so the QA seat can run it on evidence
it already captured, and a test can feed it a synthetic frame.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

PARTY_PATTERN = r"(?i)party|player|hero|leader|follower|ally"
ENEMY_PATTERN = r"(?i)enem|boss|foe|monster|mob"
SPRITE_CLASSES = ("Sprite2D", "AnimatedSprite2D", "TextureRect")
TEXT_CLASSES = ("Label", "Button", "RichTextLabel", "CheckBox", "LinkButton")

#: Party height tolerance around the bible's number, as a fraction.
PARTY_TOLERANCE = 0.25
#: Enemy-to-party height ratio above which the frame reads as a scale clash.
#: Bosses are meant to loom, so this is generous; 5x is a party of ants.
MAX_ENEMY_RATIO = 4.0
#: Sprite-to-plate pixel-density ratio band that still reads as one game.
DENSITY_BAND = (0.67, 1.5)
#: A textured node covering at least this fraction of the viewport is the
#: backdrop plate, whatever it is named.
PLATE_COVERAGE = 0.5


def _height(entry: dict) -> float:
    b = entry.get("screen_bounds") or [0, 0, 0, 0]
    return float(b[3]) - float(b[1])


def _width(entry: dict) -> float:
    b = entry.get("screen_bounds") or [0, 0, 0, 0]
    return float(b[2]) - float(b[0])


def _area(entry: dict) -> float:
    return max(0.0, _height(entry)) * max(0.0, _width(entry))


def _rows(section: Any) -> Iterable[tuple[str, dict]]:
    if isinstance(section, dict):
        return section.items()
    if isinstance(section, list):
        return ((str(r.get("path") or r.get("name") or i), r)
                for i, r in enumerate(section))
    return ()


def audit(manifest: dict, *, party_height: float = 0.0,
          party: str = PARTY_PATTERN, enemy: str = ENEMY_PATTERN,
          max_enemy_ratio: float = MAX_ENEMY_RATIO) -> dict:
    """Findings over one evidence manifest. `party_height` is the bible's
    on-screen party height in viewport pixels; 0 skips that check but still
    measures the enemy ratio against whatever the party is."""
    vp = manifest.get("viewport") or [0, 0]
    vp_area = float(vp[0]) * float(vp[1]) if vp and vp[0] and vp[1] else 0.0
    party_re, enemy_re = re.compile(party), re.compile(enemy)
    entities = [(k, e) for k, e in _rows(manifest.get("entities"))
                if e.get("visible", True)]
    ui = [(k, e) for k, e in _rows(manifest.get("ui")) if e.get("visible", True)]

    findings: list[dict] = []
    measured: dict[str, Any] = {}

    # 1. relative scale --------------------------------------------------
    sprites = [(k, e) for k, e in entities if e.get("class") in SPRITE_CLASSES]
    party_nodes = [(k, e) for k, e in sprites
                   if party_re.search(e.get("path", k)) and _height(e) > 0]
    enemy_nodes = [(k, e) for k, e in sprites
                   if enemy_re.search(e.get("path", k)) and _height(e) > 0
                   and not party_re.search(e.get("path", k))]
    if party_nodes:
        heights = sorted(_height(e) for _, e in party_nodes)
        median = heights[len(heights) // 2]
        measured["party_height_px"] = round(median, 1)
        measured["party_nodes"] = [k for k, _ in party_nodes]
        if party_height and abs(median - party_height) > PARTY_TOLERANCE * party_height:
            findings.append({
                "code": "party_scale", "severity": "fail",
                "node": ", ".join(k for k, _ in party_nodes[:4]),
                "measured": round(median, 1), "expected": party_height,
                "detail": (f"party renders {median:.0f}px tall; the bible says "
                           f"{party_height:.0f}px (±{int(PARTY_TOLERANCE * 100)}%)")})
        if enemy_nodes:
            tallest = max(enemy_nodes, key=lambda kv: _height(kv[1]))
            ratio = _height(tallest[1]) / max(median, 1.0)
            measured["enemy_to_party_ratio"] = round(ratio, 2)
            measured["tallest_enemy"] = {"node": tallest[0],
                                         "height_px": round(_height(tallest[1]), 1)}
            if ratio > max_enemy_ratio:
                findings.append({
                    "code": "scale_clash", "severity": "fail",
                    "node": tallest[0], "measured": round(ratio, 2),
                    "expected": f"<= {max_enemy_ratio}x",
                    "detail": (f"{tallest[0]} is {ratio:.1f}x the party's height "
                               f"({_height(tallest[1]):.0f}px vs {median:.0f}px)")})

    # 2. label overflow & 3. bitmap font scale ---------------------------
    for key, e in ui:
        if e.get("class") not in TEXT_CLASSES:
            continue
        if e.get("fits") is False and not e.get("autowrap"):
            text = str((e.get("value") or {}).get("text", ""))[:60]
            findings.append({
                "code": "label_overflow", "severity": "fail", "node": key,
                "measured": e.get("text_px"), "expected": round(_width(e), 1),
                "detail": (f"{key!r} text {text!r} needs {e.get('text_px')}px and "
                           f"has {_width(e):.0f}px - it clips")})
        fixed = e.get("font_fixed_size") or 0
        size = e.get("font_size") or 0
        if fixed and size and size % fixed:
            findings.append({
                "code": "font_scale", "severity": "fail", "node": key,
                "measured": size, "expected": f"a multiple of {fixed}",
                "detail": (f"{key!r} draws a {fixed}px bitmap font at {size}px - "
                           "resampled, which is the blur")})

    # 4. pixel density ----------------------------------------------------
    textured = [(k, e) for k, e in sprites
                if e.get("texture_px") and float(e["texture_px"][1]) > 0
                and _height(e) > 0]
    if textured and vp_area:
        def density(e: dict) -> float:
            return _height(e) / float(e["texture_px"][1])
        plates = [(k, e) for k, e in textured if _area(e) / vp_area >= PLATE_COVERAGE]
        if plates:
            plate_k, plate_e = max(plates, key=lambda kv: _area(kv[1]))
            pd = density(plate_e)
            measured["plate"] = {"node": plate_k, "density": round(pd, 3)}
            off = []
            for k, e in textured:
                if k == plate_k:
                    continue
                r = density(e) / pd if pd else 0.0
                if r and not (DENSITY_BAND[0] <= r <= DENSITY_BAND[1]):
                    off.append({"node": k, "ratio": round(r, 2)})
            if off:
                findings.append({
                    "code": "pixel_density", "severity": "warn",
                    "node": ", ".join(o["node"] for o in off[:6]),
                    "measured": off[:6], "expected": f"within {DENSITY_BAND} of the plate",
                    "detail": (f"{len(off)} sprite(s) are drawn at a different pixel "
                               f"density than the backdrop {plate_k!r} - two "
                               "resolutions in one frame")})

    fails = [f for f in findings if f["severity"] == "fail"]
    return {"ok": not fails, "findings": findings, "measured": measured,
            "counts": {"fail": len(fails), "warn": len(findings) - len(fails)},
            "scene": manifest.get("scene", ""), "frame": manifest.get("frame", "")}


_SIZE_RE = re.compile(
    r'(?:theme_override_font_sizes/font_size|default_font_size)\s*=\s*(\d+)'
    r'|add_theme_font_size_override\(\s*"font_size"\s*,\s*(\d+)\s*\)')
_FNT_SIZE_RE = re.compile(r"\bsize=(\d+)")


def font_check(project_dir: str) -> dict:
    """Static half of the audit: bitmap fonts used at sizes they cannot draw
    crisply, and a non-nearest default texture filter under a pixel font.
    Reads project.godot, every .fnt, and every font_size in .tres/.tscn/.gd.
    """
    from pathlib import Path

    root = Path(project_dir)
    findings: list[dict] = []
    fnts: dict[str, int] = {}
    for fnt in root.rglob("*.fnt"):
        if ".godot" in fnt.parts:
            continue
        try:
            head = fnt.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        m = _FNT_SIZE_RE.search(head)
        if m:
            fnts[str(fnt.relative_to(root)).replace("\\", "/")] = int(m.group(1))
    if not fnts:
        return {"ok": True, "findings": [], "bitmap_fonts": {}}
    native = sorted(set(fnts.values()))
    try:
        cfg = (root / "project.godot").read_text(encoding="utf-8", errors="replace")
    except OSError:
        cfg = ""
    m = re.search(r"default_texture_filter\s*=\s*(\d+)", cfg)
    if m and m.group(1) != "0":
        findings.append({"code": "texture_filter", "severity": "fail",
                         "node": "project.godot",
                         "detail": ("rendering/textures/canvas_textures/default_texture_filter"
                                    f" is {m.group(1)}; a pixel font needs 0 (nearest)")})
    for f in list(root.rglob("*.tres")) + list(root.rglob("*.tscn")) + list(root.rglob("*.gd")):
        if ".godot" in f.parts or ".bgate" in f.parts or "addons" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _SIZE_RE.finditer(text):
            size = int(m.group(1) or m.group(2))
            if not any(size % n == 0 for n in native):
                findings.append({
                    "code": "font_scale", "severity": "fail",
                    "node": str(f.relative_to(root)).replace("\\", "/"),
                    "measured": size, "expected": f"a multiple of {native}",
                    "detail": (f"font_size {size} is not an integer multiple of the "
                               f"bitmap font's {native}px - resampled, blurry")})
    return {"ok": not findings, "findings": findings, "bitmap_fonts": fnts}
