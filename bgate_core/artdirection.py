"""The design bible, reaching the image prompt.

A project's bible can say ART DIRECTION LOCKED in capitals and it changes
nothing, because until now no generation path read it. Narrative writes pass
through ``canon.check`` — a deterministic gate that refuses prose contradicting
canon — and art passed through nothing at all. So a project whose bible demands
"true chunky pixel art, no painterly rendering, isometric 2:1, dark neon office
palette" cheerfully produced a painterly straight-on fantasy paladin, and the
tool had no way to notice.

That is the same failure the QA audit called "gates that do not gate", in the
one place the audit could not see it: the defect is in the OUTPUT, not the code.

Two halves, and the split matters:

  * :func:`clause` — the constraints go INTO the prompt. Sourced from the bible
    every time, so editing the bible changes the art. A hardcoded style string
    would be the same bug wearing a different hat.
  * :func:`check` — the result is measured on the way OUT, against the pinned
    style anchors. Advisory by default and hard only where the bible itself said
    LOCKED, because a false rejection that eats a good frame costs real money.

What is deliberately NOT here: taste. This cannot tell you the art is good. It
tells you the art disagrees with what the project wrote down.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from bgate_core import bible

# A constraint whose title shouts this is one the project meant. Everything else
# is advice we still pass to the model but never fail a frame over.
_LOCK_MARKERS = ("LOCKED", "CONTRACT", "MUST")

# Bible kinds that describe how art should look. `pillar`/`loop`/`scope_tier`
# are about what the game IS, not how it renders, and stuffing them into an
# image prompt buys nothing but tokens.
_ART_KINDS = ("constraint", "reference")

# Words that mean "this constraint is about rendering" — a Determinism
# constraint is real and locked and has nothing to do with a picture.
_ART_WORDS = ("art", "style", "pixel", "palette", "colour", "color", "sprite",
              "projection", "isometric", "render", "silhouette", "outline",
              "tile", "anim", "facing", "proportion", "shading", "ui")

MAX_CLAUSE_CHARS = 420


def is_locked(section: dict) -> bool:
    title = str(section.get("title") or "")
    return any(marker in title for marker in _LOCK_MARKERS)


def is_art(section: dict) -> bool:
    """Does this constraint describe how things should LOOK?

    Cheap keyword test on purpose. The cost of a false positive is a few wasted
    tokens; the cost of a false negative is the bug this module exists to fix.
    """
    blob = f"{section.get('title', '')} {section.get('body', '')}".lower()
    return any(word in blob for word in _ART_WORDS)


def constraints(root: str | os.PathLike[str], *, locked_only: bool = False) -> list[dict]:
    """The bible sections that govern how art looks, most important first."""
    out: list[dict] = []
    for kind in _ART_KINDS:
        try:
            out.extend(bible.list_sections(root, kind))
        except Exception:
            continue
    art = [s for s in out if is_art(s)]
    if locked_only:
        art = [s for s in art if is_locked(s)]
    # Locked first, then by the rank the director set.
    art.sort(key=lambda s: (not is_locked(s), int(s.get("rank") or 0)))
    return art


def clause(root: str | os.PathLike[str], *, task_kind: str = "",
           limit: int = MAX_CLAUSE_CHARS) -> str:
    """The art-direction text to append to an image prompt.

    Locked constraints are stated as requirements; the rest as direction. Kept
    short — a prompt that is 80% boilerplate stops steering the model, so this
    is budgeted and truncated rather than allowed to grow without limit.
    """
    sections = constraints(root)
    if not sections:
        return ""

    blob = " ".join(str(s.get("body") or "") for s in sections).lower()
    said = [directive for probe, directive in _VOCABULARY if probe.search(blob)]
    if not said:
        return ""

    # The failure this whole function exists to prevent: bible prose handed to
    # an image model gets DRAWN. It is appended AFTER the budget is applied,
    # because truncation used to cut the one directive that matters most.
    no_text = (" No text, letters, words, numbers, labels or signage anywhere "
               "in the image.")
    # Fill to the budget rather than to a fixed count: a caller that asks for
    # more room wants more direction, and a whole directive is worth more than
    # a truncated one.
    room = max(60, limit - len(no_text))
    kept: list[str] = []
    for directive in said:
        candidate = "; ".join(kept + [directive])
        if kept and len(candidate) > room:
            break
        kept.append(directive)
    style = "; ".join(kept)[:room]
    return f"\n\nStyle: {style}.{no_text}"


# DETECT what the bible asks for; EMIT a clean directive. Salvaging sentence
# fragments out of the bible was tried and produced mush like "the / / / are
# the and for all art and ui: true chunky pixel art , dark" — because the prose
# is written for a person, with pin names, editorial and engine detail woven
# through it. A model cannot act on a fragment; it draws it.
#
# So the bible is READ, not quoted. Each entry is (what the project asked for,
# what to tell the model). Adding a rule is one line, and nothing a project
# writes can leak into the image as literal text.
#
# Small on purpose: a style clause is a nudge. Past a couple of hundred
# characters the model starts treating it as subject matter — the budget in
# MAX_CLAUSE_CHARS is what enforces that.

_VOCABULARY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pixel art|pixel[- ]grid|chunky pixel", re.I),
     "true chunky pixel art with a visible pixel grid, hard-edged, no "
     "anti-aliased or painterly rendering"),
    (re.compile(r"isometric|2:1|3/4", re.I),
     "angled 3/4 isometric view, 2:1 tile geometry, never flat top-down and "
     "never straight-on front view"),
    (re.compile(r"chibi", re.I),
     "chibi proportions, large head, short body"),
    (re.compile(r"16-bit|snes", re.I),
     "16-bit SNES-era sprite work"),
    (re.compile(r"dark neon|neon", re.I),
     "dark palette with neon accents"),
    (re.compile(r"greyscale|grayscale|monochrome", re.I),
     "greyscale only, no hue"),
    (re.compile(r"bold outline|readable silhouette|silhouette", re.I),
     "bold outlines and a silhouette readable at small size"),
]


def anchors_for(root: str | os.PathLike[str], *, kinds: Sequence[str] = ("style",),
                limit: int = 4) -> list[str]:
    """Pinned style anchors, as paths — what "on-model" is measured against.

    The bible says which pins are the rendering target; these are the files.
    """
    try:
        from bgate_core import refs
    except Exception:
        return []
    out: list[str] = []
    try:
        for pin in refs.list_refs(root):
            if kinds and str(pin.get("kind") or "") not in kinds:
                continue
            path = str(pin.get("path") or "")
            if path and Path(path).is_file():
                out.append(path)
    except Exception:
        return []
    return out[:limit]


def check(root: str | os.PathLike[str], path: str | os.PathLike[str], *,
          anchors: Optional[Sequence[str]] = None,
          max_palette_distance: float = 190.0) -> dict:
    """Does this image agree with the project's art direction?

    Measured, not judged: palette distance from the pinned style anchors, plus
    a pixel-grid test when the bible demands pixel art. Both are evidence a
    human can argue with, which is the only kind worth reporting.

    Returns ``{ok, flags[], measured{}, locked}``. ``ok`` is False only when a
    LOCKED constraint is contradicted — advisory drift is reported, never fatal.
    """
    from bgate_core import chroma  # palette machinery already lives there

    sections = constraints(root)
    locked_blob = " ".join(str(s.get("body") or "") for s in sections
                           if is_locked(s)).lower()
    flags: list[dict] = []
    measured: dict[str, Any] = {}

    anchor_paths = list(anchors) if anchors is not None else anchors_for(root)
    measured["anchors"] = len(anchor_paths)

    # 1. Palette agreement with the pinned style set.
    if anchor_paths:
        try:
            art = chroma.palette_of(path)
            ref: list[tuple[int, int, int]] = []
            for a in anchor_paths:
                ref.extend(chroma.palette_of(a))
            if art and ref:
                worst = max(chroma.distance_to(c, ref) for c in art)
                measured["palette_distance"] = round(worst, 1)
                if worst > max_palette_distance:
                    flags.append({
                        "flag": "palette_drift",
                        "locked": "palette" in locked_blob,
                        "detail": f"a colour sits {worst:.0f} from anything in the "
                                  f"pinned style set (limit {max_palette_distance:.0f})",
                    })
        except Exception as exc:
            measured["palette_error"] = str(exc)

    # 2. Pixel-grid test, only when the bible actually asked for pixel art.
    # 2. Pixel-grid measurement — REPORTED, NEVER FATAL. See _pixel_block.
    wants_pixel = "pixel art" in locked_blob or "chunky pixel" in locked_blob
    measured["pixel_required"] = wants_pixel
    if wants_pixel:
        try:
            measured["pixel_block"] = _pixel_block(path)
        except Exception as exc:
            measured["pixel_error"] = str(exc)

    hard = [f for f in flags if f.get("locked")]
    return {"ok": not hard, "flags": flags, "measured": measured,
            "locked": [s.get("title") for s in sections if is_locked(s)]}


def _pixel_block(path: str | os.PathLike[str], max_block: int = 16) -> int:
    """The apparent pixel size, in screen pixels — REPORTED, never a verdict.

    This detects UPSCALED pixel art (a small grid blown up), which is not the
    same thing as pixel art. It was tried as a gate and withdrawn, because it
    rejected the project's own on-model sprites. Measured on Corporate Quest:

        fantasy paladin (off-model, painterly)  block=1  uniq/opaque=0.36
        pm_paladin_idle (on-model, pixel)       block=1  uniq/opaque=0.69
        pixel-combat pin (the stated target)    block=1  uniq/opaque=0.38

    Native-resolution pixel art has a block size of 1, and these assets carry
    anti-aliasing and wide palettes, so neither block size nor colour count
    separates on-model from off. Colour-count and edge-hardness were tried too
    and ordered the samples WRONG — the real sprite scored worse than the reject
    on both.

    A gate that fails the project's own art is worse than no gate: it throws
    away work that has already been paid for. So this stays a number a human can
    look at. If someone finds a signal that actually separates, gate on that —
    but calibrate it against these three files first.
    """
    from PIL import Image

    im = Image.open(path).convert("RGB")
    # Work on a bounded copy: this is a heuristic, not a reason to chew 4K.
    if max(im.size) > 512:
        im.thumbnail((512, 512), Image.NEAREST)
    w, h = im.size
    best = 1
    for block in range(2, max_block + 1):
        if w % block or h % block:
            continue
        small = im.resize((w // block, h // block), Image.NEAREST)
        if list(small.resize((w, h), Image.NEAREST).getdata()) == list(im.getdata()):
            best = block
    return best
