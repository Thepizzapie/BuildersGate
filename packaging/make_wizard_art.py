"""Render the installer's wizard images from the app icon.

    python packaging/make_wizard_art.py

Inno Setup wants BMP — no PNG, no alpha channel — at fixed sizes, and it takes
a comma-separated list so it can pick the right one for the display's scaling.
Writing them by hand in an image editor means they drift from the icon the
moment the icon changes, so they are generated from packaging/icon.ico and
committed.

WHY BOTHER. The stock Inno wizard is grey, unbranded, and identical to the
installer of every piece of bundled adware Windows users have been taught to
back out of. An unsigned installer already starts from a position of distrust
(SmartScreen calls the publisher unknown); showing up with no branding at all
is the other half of looking like something to close.

This does NOT make the installer trusted. That is code signing, and nothing
else. This only stops it looking careless.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ICON = HERE / "icon.ico"

# WHITE, NOT THE APP'S GROUND. The first version of this drew the mark on the
# dashboard's near-black #0a0a0c, which looked right in isolation and shipped a
# black rectangle sitting in the middle of Inno's white header — the wizard
# does not theme, so anything that is not near-white reads as a hole in the
# page.
#
# This is the same reasoning already written down in frontend/public/favicon.svg:
# the mark's posts are #1800ad, which all but disappears on a dark field, so the
# badge carries its own light plate. A surface that is not ours to theme gets
# the plate.
BG = (255, 255, 255)
EDGE = (222, 222, 228)

# (name, width, height) — Inno's documented sizes at 100%, and the 2x it uses
# on a scaled display. The names are what installer.iss lists.
LARGE = [("wizard-large.bmp", 164, 314), ("wizard-large@2x.bmp", 328, 628)]
SMALL = [("wizard-small.bmp", 55, 58), ("wizard-small@2x.bmp", 110, 116)]


def _icon(px: int) -> Image.Image:
    """The app icon at `px`, from the largest frame in the .ico.

    Pillow opens an .ico at its default (smallest) frame unless told otherwise,
    which produced a 16x16 blown up to 100px and a sidebar that looked worse
    than no sidebar at all.
    """
    im = Image.open(ICON)
    sizes = sorted(im.info.get("sizes") or [(im.width, im.height)])
    im.size = sizes[-1]                      # ask for the biggest frame
    im = im.convert("RGBA")
    return im.resize((px, px), Image.LANCZOS)


def _compose(w: int, h: int, *, icon_frac: float, offset_y: float) -> Image.Image:
    canvas = Image.new("RGB", (w, h), BG)
    side = max(16, int(min(w, h) * icon_frac))
    ic = _icon(side)
    x = (w - side) // 2
    y = int(h * offset_y) - side // 2
    # RGBA over RGB needs the alpha handed in explicitly or the transparent
    # corners composite as black squares on a nearly-black field — invisible on
    # this palette, and very visible on any other.
    canvas.paste(ic, (x, y), ic)
    return canvas


def main() -> int:
    if not ICON.is_file():
        sys.exit(f"missing {ICON}")
    for name, w, h in LARGE:
        im = _compose(w, h, icon_frac=0.58, offset_y=0.34)
        # A hairline down the inside edge, where the sidebar meets the white
        # page. Without it the dark panel looks like a rendering failure.
        for y in range(h):
            im.putpixel((w - 1, y), EDGE)
        im.save(HERE / name, "BMP")
        print(f"  {name:24s} {w}x{h}")
    for name, w, h in SMALL:
        im = _compose(w, h, icon_frac=0.86, offset_y=0.5)
        im.save(HERE / name, "BMP")
        print(f"  {name:24s} {w}x{h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
