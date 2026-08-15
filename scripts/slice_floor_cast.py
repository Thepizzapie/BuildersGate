"""Slice the studio cast sheets into one PNG per state, keyed to transparency.

Each sheet is a 3x2 grid of six poses on the flat navy field the sheets were
generated on. Two things have to come off with the background: the field
itself, and the soft ellipse under each figure. The ellipse is drawn INTO the
field, so leaving it in would put a navy smudge on top of whatever floor tile
the room paints. The floor pane draws its own shadow instead.

The flood fill walks in from the cell border and accepts a pixel when it is
either close to the sampled background colour or a DARKER, still blue tinted
version of it - which is what the ellipse is, and what a near black character
outline is not.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

CAST = Path(r"C:\Users\adria\Desktop\bg-testbed\.bgate_out\art\cast")
STATES = ["idle", "sitting", "walk-a", "walk-b", "working", "handoff"]
NAMES = ["art", "audio", "narrative", "gameplay", "qa", "cinematic", "tech",
         "director", "generic"]


def key_cell(cell: Image.Image) -> Image.Image:
    """Flood the background (and its shadow ellipse) out to alpha 0."""
    im = cell.convert("RGB")
    w, h = im.size
    px = im.load()
    bg = px[2, 2]
    seen = bytearray(w * h)
    q = deque()

    def ok(c):
        # BACKGROUND ONLY. A wider rule that also swallowed the ground shadow
        # was tried and ate the characters' dark denim with it - the ellipse
        # and a navy trouser leg are the same colour to within a few levels,
        # and the ink outline has enough gaps at this pixel size for the fill
        # to get inside. The shadow stays; the floor it is composited onto is
        # the same dark navy the sheets were drawn on, so it does not show.
        d = abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2])
        return d < 40

    for x in range(w):
        for y in (0, h - 1):
            q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            q.append((x, y))
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        if not ok(px[x, y]):
            continue
        seen[i] = 1
        if x > 0:
            q.append((x - 1, y))
        if x < w - 1:
            q.append((x + 1, y))
        if y > 0:
            q.append((x, y - 1))
        if y < h - 1:
            q.append((x, y + 1))

    out = im.convert("RGBA")
    op = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if seen[row + x]:
                op[x, y] = (0, 0, 0, 0)
    return out


def main() -> int:
    for name in NAMES:
        sheet = Image.open(CAST / f"{name}-sheet.png").convert("RGB")
        W, H = sheet.size
        cw, ch = W // 3, H // 2
        dest = CAST / name
        dest.mkdir(parents=True, exist_ok=True)
        cut = {}
        for i, state in enumerate(STATES):
            box = ((i % 3) * cw, (i // 3) * ch, (i % 3 + 1) * cw, (i // 3 + 1) * ch)
            keyed = key_cell(sheet.crop(box))
            bbox = keyed.getbbox()
            if not bbox:
                print(f"{name}/{state}: EMPTY after keying")
                continue
            trimmed = keyed.crop(bbox)
            trimmed.save(dest / f"{state}.png")
            cut[state] = trimmed
            print(f"{name}/{state}: {trimmed.size[0]}x{trimmed.size[1]}")
        # Walking right is the mirror of walking left. Generating it instead
        # costs a call AND a chance for the model to redraw the character.
        for src, dst in (("walk-a", "walk-right-a"), ("walk-b", "walk-right-b")):
            if src in cut:
                cut[src].transpose(Image.FLIP_LEFT_RIGHT).save(dest / f"{dst}.png")
        if "walk-a" in cut:
            (dest / "walk-a.png").replace(dest / "walk-left-a.png")
        if "walk-b" in cut:
            (dest / "walk-b.png").replace(dest / "walk-left-b.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
