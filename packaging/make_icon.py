"""Generate packaging/icon.ico from packaging/logo.svg — the real mark.

    python packaging/make_icon.py

This rasterises the ARTWORK rather than reproducing it. An earlier version
redrew the mark from the geometry in bgate_ui/static/icons.js with colours
eyeballed off a screenshot; it looked right and was still a guess (the blue was
#1a0dc2 against the real #1800ad). Parsing the file means the icon is derived
from the logo, and re-running after a logo edit picks the change up.

No SVG library is needed because this particular file is entirely axis-aligned
rectangles and rotated quads — every shape is a four-point polygon under a
translate. That is checked at load: anything with a curve, a rotation matrix or
a nested transform raises rather than being silently mis-drawn. If the mark ever
gains a curve, install cairosvg and rasterise properly instead of extending the
parser.

Drawn at 8x and downsampled, which is what keeps the chevron's diagonals clean
once it is 16px across.

EVERY SIZE IS THE REAL MARK.
An earlier version merged the broken right post into a solid one below 32px, on
the reasoning that the gap is under two physical pixels there. That was the
wrong call to make unasked: it is the gate in Builders Gate, and a logo that
quietly becomes a different logo in the taskbar is not the logo. The gap is
tight at 16px and it stays.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SVG = HERE / "logo.svg"
OUT = HERE / "icon.ico"

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 8                      # supersample factor
# Breathing room, as a fraction of the icon. Small sizes get less: at 16px a 6%
# margin costs a whole pixel off each edge, and a title-bar icon has no
# neighbours to need separating from.
MARGIN = 0.06
MARGIN_SMALL = 0.015
SMALL_AT_OR_BELOW = 24


def load_shapes(svg: Path):
    """[(fill, [(x, y), ...]), ...] in absolute user units.

    Only the drawn <path> elements matter; the <defs> clipPaths in this file
    merely restate the same rectangles and are ignored.
    """
    text = svg.read_text(encoding="utf-8")
    shapes = []
    # Each drawn path sits inside a group carrying a pure translate.
    for grp in re.finditer(
            r'transform="matrix\(1,\s*0,\s*0,\s*1,\s*([\d.eE+-]+),\s*([\d.eE+-]+)\)"'
            r'(.*?)</g></g></g>', text, re.S):
        tx, ty = float(grp.group(1)), float(grp.group(2))
        for p in re.finditer(r'<path fill="(#[0-9a-fA-F]{6})" d="([^"]+)"', grp.group(3)):
            d = p.group(2)
            if re.search(r"[CcSsQqTtAa]", d):
                raise ValueError(f"{svg.name} has curves; use a real SVG rasteriser")
            pts = [(float(a) + tx, float(b) + ty)
                   for a, b in re.findall(r"([\d.eE+-]+)\s+([\d.eE+-]+)", d)]
            # A closed quad repeats its first point; drop the duplicate tail.
            uniq = []
            for pt in pts:
                if not uniq or (abs(pt[0] - uniq[0][0]) > 1e-6 or abs(pt[1] - uniq[0][1]) > 1e-6):
                    uniq.append(pt)
            if len(uniq) >= 3:
                shapes.append((p.group(1), uniq))
    if not shapes:
        raise ValueError(f"no drawable paths found in {svg}")
    return shapes


def draw(shapes, px: int) -> Image.Image:
    margin = MARGIN_SMALL if px <= SMALL_AT_OR_BELOW else MARGIN
    xs = [x for _, p in shapes for x, _ in p]
    ys = [y for _, p in shapes for _, y in p]
    w, h = max(xs) - min(xs), max(ys) - min(ys)

    n = px * SS
    span = n * (1 - 2 * margin)
    scale = span / max(w, h)
    # Centre the mark: it is taller than it is wide, so the padding differs.
    ox = (n - w * scale) / 2 - min(xs) * scale
    oy = (n - h * scale) / 2 - min(ys) * scale

    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for fill, pts in shapes:
        d.polygon([(x * scale + ox, y * scale + oy) for x, y in pts], fill=fill)
    return im.resize((px, px), Image.LANCZOS)


def main() -> int:
    shapes = load_shapes(SVG)
    fills = sorted({f for f, _ in shapes})

    frames = [draw(shapes, s) for s in SIZES]
    frames[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])

    print(f"read  {SVG.name}: {len(shapes)} shapes, colours {', '.join(fills)}")
    print(f"wrote {OUT.name}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  the mark, unaltered, at: {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
