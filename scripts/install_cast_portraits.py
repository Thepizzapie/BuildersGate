"""Install the generated cast portraits beside their sprites as face.png.

WHY THE BACKGROUND IS FLOOD-FILLED RATHER THAN COLOUR-KEYED. image_generate's
own keyer runs the flat-chroma contract and these came back rejected by it -
the model ignored the chroma and drew its own field (black on some, white on
others), and the portraits are framed so the shoulders cross the bottom edge,
which the keyer counts as background bleed. Both are correct for a sprite and
neither matters for a face in a circle.

Keying by COLOUR would be wrong here anyway: these characters are drawn with a
near-black outline, so "delete every dark pixel" eats the outline and hollows
the face. A flood from the four corners only ever removes background that is
CONNECTED to the edge, so an outline enclosed by the character is untouched no
matter what colour it is.

Run: python scripts/install_cast_portraits.py [name ...]
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _floorpaths import REPO, sandbox, FLOOR_IMG  # noqa: E402
FLOOR = FLOOR_IMG


RAW = sandbox() / ".bgate_out" / "art" / "portraits"

# How far a pixel may sit from the background colour and still be background.
# Generous because these arrive as JPEG - a "flat" field is a few units of
# ringing either side of one value - and safe because the flood only ever
# reaches pixels connected to the edge.
TOL = 60

# The installed size. The room draws the face small; 128 covers a 2x screen.
OUT_PX = 128


def key_from_edges(im: Image.Image, tol: int = TOL) -> Image.Image:
    """Alpha out every pixel reachable from a corner without crossing the art."""
    im = im.convert("RGBA")
    w, h = im.size
    px = im.load()
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    # The field is whatever the corners agree on; if they disagree the image is
    # not a portrait on a flat field and is left alone rather than half-eaten.
    cols = [px[x, y][:3] for x, y in seeds]
    base = cols[0]
    if any(sum((a - b) ** 2 for a, b in zip(c, base)) > tol ** 2 for c in cols):
        return im
    seen = bytearray(w * h)
    queue = deque(seeds)
    limit = tol ** 2
    while queue:
        x, y = queue.popleft()
        i = y * w + x
        if seen[i]:
            continue
        r, g, b, a = px[x, y]
        if not a:
            seen[i] = 1
            continue
        if (r - base[0]) ** 2 + (g - base[1]) ** 2 + (b - base[2]) ** 2 > limit:
            continue
        seen[i] = 1
        px[x, y] = (r, g, b, 0)
        if x > 0:
            queue.append((x - 1, y))
        if x + 1 < w:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y + 1 < h:
            queue.append((x, y + 1))
    return im


def install(name: str) -> bool:
    src = RAW / f"{name}.png"
    if not src.is_file():
        print(f"{name}: no portrait at {src}")
        return False
    im = key_from_edges(Image.open(src))
    box = im.getbbox()
    if box:
        im = im.crop(box)
    side = max(im.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    # Top-aligned, not centred: the shoulders are cut by the frame, so
    # centring a wide-and-short crop floats the head. Pinning the top keeps
    # the eyes where a reader expects them in a round mask.
    square.alpha_composite(im, ((side - im.width) // 2, 0))
    dest = FLOOR / name / "face.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    square.resize((OUT_PX, OUT_PX), Image.LANCZOS).save(dest)
    print(f"{name}: {dest.relative_to(REPO)}")
    return True


def main(argv: list[str]) -> int:
    names = argv or sorted(p.stem for p in RAW.glob("*.png")
                           if not p.stem.endswith("-anchor"))
    ok = sum(1 for n in names if install(n))
    print(f"\n{ok}/{len(names)} installed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
