"""Generate packaging/icon.ico from the Builders Gate mark.

    python packaging/make_icon.py

The mark is three primitives — a solid left post, a BROKEN right post, and a
chevron passing between them — so it is drawn directly with Pillow rather than
pulling in an SVG rasteriser for one file. The geometry is copied from the
canonical definition in bgate_ui/static/icons.js (BGIcon.logo), on the same
64x64 grid, so the icon and the in-app mark cannot drift apart.

Everything is drawn at 8x and downsampled, which is what keeps the diagonals of
the chevron clean once it is 16px across.

THE SMALL SIZES USE A DIFFERENT DRAWING, DELIBERATELY.
bgate_ui/static/favicon.svg already records why: "at 16px the broken right post
turns to mush". The gap in the right post is 16 units on a 64 grid — four
physical pixels at 16px, less than that after antialiasing — so below 32px the
icon falls back to the favicon's simplification: one post, and the chevron
through it. Windows picks per-size out of the .ico, so both ship.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icon.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 8                      # supersample factor
FULL_MARK_ABOVE = 24        # sizes above this get the broken post

# Brand colours, sampled from the supplied mark. The posts are a saturated
# indigo and the chevron a warmer orange than the UI's --accent (#ff6a3d),
# which is deliberate: the app tints its own mark with currentColor, but the
# icon is the brand as drawn.
POST = "#1a0dc2"
CHEVRON = "#ff7518"


def _mark(d, u, broken: bool):
    """Draw the mark on a grid where `u` is one unit of the 64x64 viewBox."""
    post_w = int(5 * u)
    chev_w = int(6 * u)

    # Left post — full height.
    d.line([(16 * u, 8 * u), (16 * u, 56 * u)], fill=POST, width=post_w)

    # Right post — broken, which is the gate. Below 32px the gap closes up into
    # a smudge, so the small sizes drop it entirely rather than render mush.
    if broken:
        d.line([(48 * u, 8 * u), (48 * u, 26 * u)], fill=POST, width=post_w)
        d.line([(48 * u, 42 * u), (48 * u, 56 * u)], fill=POST, width=post_w)
    else:
        d.line([(48 * u, 8 * u), (48 * u, 56 * u)], fill=POST, width=post_w)

    # The chevron, drawn as two segments plus a joint — a polyline leaves a
    # notch at the vertex at this stroke weight.
    d.line([(28 * u, 22 * u), (42 * u, 34 * u)], fill=CHEVRON, width=chev_w)
    d.line([(42 * u, 34 * u), (28 * u, 46 * u)], fill=CHEVRON, width=chev_w)
    r = chev_w / 2 - 1
    d.ellipse([42 * u - r, 34 * u - r, 42 * u + r, 34 * u + r], fill=CHEVRON)


def draw(px: int) -> Image.Image:
    n = px * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))   # transparent, as the mark is
    _mark(ImageDraw.Draw(im), n / 64.0, broken=px > FULL_MARK_ABOVE)
    return im.resize((px, px), Image.LANCZOS)


def main() -> int:
    frames = [draw(s) for s in SIZES]
    frames[-1].save(OUT, format="ICO",
                    sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    full = [s for s in SIZES if s > FULL_MARK_ABOVE]
    simple = [s for s in SIZES if s <= FULL_MARK_ABOVE]
    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  full mark   : {', '.join(map(str, full))}")
    print(f"  simplified  : {', '.join(map(str, simple))}  (broken post drops out)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
