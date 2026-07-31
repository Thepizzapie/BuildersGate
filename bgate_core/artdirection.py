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
           limit: int = MAX_CLAUSE_CHARS, tileable: bool = False) -> str:
    """The art-direction text to append to an image prompt.

    Locked constraints are stated as requirements; the rest as direction. Kept
    short — a prompt that is 80% boilerplate stops steering the model, so this
    is budgeted and truncated rather than allowed to grow without limit.

    Two clauses come back joined, and they have different provenance. The FORM
    clause (see :func:`form_clause`) says what the asset physically is and is
    true whether or not the project ever wrote a bible; the STYLE clause is
    read out of the bible every time. Form goes first, because a style clause
    is a nudge and the form is the shape of the file.

    ``tileable`` only means anything to a texture kind, and is ignored
    elsewhere. Every kind that is not a texture or a decal gets a clause
    byte-identical to the one it got before these existed.
    """
    kind = str(task_kind or "").strip().lower()
    form = form_clause(kind, tileable=tileable)

    sections = constraints(root)
    if not sections:
        return form

    blob = " ".join(str(s.get("body") or "") for s in sections).lower()
    # Two gates, and they answer different questions. The probe asks "did the
    # project ASK for this?"; the scope asks "is it TRUE of the thing being
    # drawn?". A bible that says chibi still means it — just not about a spark.
    said = [directive for probe, directive, scope in _VOCABULARY
            if probe.search(blob) and _in_scope(scope, kind)]
    if not said:
        return form

    # The failure this whole function exists to prevent: bible prose handed to
    # an image model gets DRAWN. It is appended AFTER the budget is applied,
    # because truncation used to cut the one directive that matters most.
    #
    # THE ONE EXEMPTION, and it is not a loophole: on a texture or a decal the
    # lettering is frequently the asset — a stencilled crate, a jersey number,
    # the team logo the 3D brief asks for as its own layer. A blanket ban made
    # that impossible to ASK for, so the tool forbade by construction the thing
    # the brief instructed. It stays exactly as it was everywhere else.
    no_text = "" if _drops_no_text(kind) else _NO_TEXT
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
    return f"{form}\n\nStyle: {style}.{no_text}"


_NO_TEXT = (" No text, letters, words, numbers, labels or signage anywhere "
            "in the image.")


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

# ── SCOPE: which KINDS of asset a directive is true of ───────────────────────
#
# Every directive used to go on every prompt, and that is wrong in a way that is
# invisible until you look at the art. MEASURED, on a corporate-satire project:
# "chibi proportions, large head, short body" is exactly right for that game's
# characters and is what its bible asks for — and it was being appended to a
# request for a MUZZLE SPARK. Four generations in a row came back as a
# big-headed figure with the spark drawn beside it. Nothing in the prompt could
# outvote it, because the clause is appended last; removing every actor word
# from the description did not help, and neither did negation.
#
# A directive about BODIES is not a directive about a spark, a floor tile or a
# health bar. So each entry now carries the kinds it is true of, and a kind the
# entry does not name never sees it. `None` means "true of all art", which most
# of them are: how the pixels are drawn is universal, what the subject's
# ANATOMY is is not.
#
# Kinds in use across the tools: anchor, animation, background, gear, icon,
# item, portrait, prop, sheet, sprite, texture, decal, tile, ui, vfx —
# KNOWN_KINDS below is the machine-readable list. An unknown or empty kind
# gets the unscoped directives only — the safe subset, since a caller that did
# not say what it was making must not be told to draw a body.
_CHARACTERS = frozenset({"anchor", "animation", "portrait", "sheet", "sprite"})
_WORLD = frozenset({"anchor", "animation", "background", "gear", "item", "prop",
                    "sheet", "sprite", "tile"})

# ── FORM: what the asset physically IS, before any style is applied ──────────
#
# A texture map is not a picture of a surface, it is data a shader multiplies.
# It gets sampled flat across a UV island, so anything the model painted INTO
# it — a highlight, a cast shadow, a rim light — is multiplied a second time by
# the scene's own lights when it lands on the mesh. MEASURED on the layered 3D
# path: a "great looking" plank came back as a lit illustration with baked
# highlights, a soft shadow and a background behind it, and on the mesh it read
# as muddy. None of those three things survive contact with a UV, and no amount
# of style direction fixes them, because they are not style.
#
# Two more things went wrong at the same door and both are here:
#
#   * the character/world directives (chibi anatomy, isometric projection) were
#     reaching texture prompts. They are excluded the same way every other
#     out-of-scope kind is — by not appearing in _CHARACTERS or _WORLD — so
#     this adds no special case to _in_scope.
#   * the blanket no-text ban made a LOGO LAYER impossible to ask for. See the
#     exemption in clause().
#
# The form clause is NOT sourced from the bible. It is true of the asset kind
# whether or not a project ever wrote one down, so it is emitted even when
# clause() would otherwise return "", and it goes FIRST.
#
# Aliases are included because an agent naming this itself will reach for
# "material" or "albedo" as readily as "texture", and a near-miss kind silently
# means "unknown" — which is the failure mode this whole file exists to close.
TEXTURE_KINDS = frozenset({"texture", "material", "albedo"})
DECAL_KINDS = frozenset({"decal", "logo", "insignia", "emblem", "sticker"})

# Every kind any tool in this repo actually asks for, named rather than implied.
# It exists so a directive can be scoped to "things that have a subject and a
# silhouette" without that quietly coming to mean "everything except whatever
# kind was added last". Adding a kind here is how it opts IN.
_SUBJECTS = frozenset({
    "anchor", "animation", "background", "backdrop", "concept", "gear", "icon",
    "item", "plate", "portrait", "prop", "sheet", "splash", "sprite", "tile",
    "ui", "vfx",
}) | DECAL_KINDS

KNOWN_KINDS: tuple[str, ...] = tuple(sorted(_SUBJECTS | TEXTURE_KINDS))

_TEXTURE_FORM = (
    "\n\nTEXTURE MAP (mandatory): this is a flat albedo / base-colour map, not "
    "a picture of a surface. Orthographic and straight-on — no perspective, no "
    "camera angle, no depth of field, no foreshortening. Completely even "
    "ambient illumination: no light source, no directional light, no "
    "highlights, no specular, no gloss, no reflections, no cast shadow, no "
    "ambient occlusion, no baked shading, no bevel and no emboss. The material "
    "fills the entire frame edge to edge — no background, no border, no "
    "vignette, no drop shadow, no props, no scene, no mock-up and nothing "
    "resting on top of the surface."
)

_TILEABLE_FORM = (
    " Seamlessly tileable: the pattern runs off every edge and continues, so "
    "the left edge matches the right and the top matches the bottom. No "
    "border, no framing element, no single hero feature centred in the frame — "
    "an even, repeating field."
)

_DECAL_FORM = (
    "\n\nDECAL (mandatory): the lettering and marks ARE the subject. Render "
    "them crisply — legible, high contrast, exact spelling, even stroke weight, "
    "sharp edges, correctly kerned, nothing cropped or cut off. Flat "
    "vector-like colour, straight-on, no perspective, no lighting, no gloss, "
    "no bevel, no drop shadow, no photographic texture, no mock-up and no "
    "product shot. The graphic is isolated on a completely flat, uniform, "
    "single-colour background with a clear margin on all four sides, and "
    "nothing else is in the frame."
)


def is_texture_kind(task_kind: str) -> bool:
    """Is this a flat map sampled across a UV, rather than a picture?

    Exported because two other decisions key off it — the square-size
    constraint in the imagegen adapter and the tiling post-pass — and each of
    them re-deriving its own list is how the lists drift apart.
    """
    return str(task_kind or "").strip().lower() in TEXTURE_KINDS


def is_decal_kind(task_kind: str) -> bool:
    """Is this a graphic whose whole point is the text or mark it carries?"""
    return str(task_kind or "").strip().lower() in DECAL_KINDS


def _drops_no_text(kind: str) -> bool:
    return kind in TEXTURE_KINDS or kind in DECAL_KINDS


def form_clause(task_kind: str = "", *, tileable: bool = False) -> str:
    """What the asset physically is — bible-independent, and empty for the
    kinds that had no form clause before this existed.

    ``tileable`` asks the model for a repeating field. It is an ASK: the
    guarantee that the edges actually join lives in the mirrored post-pass
    (``bgate_adapters.imagegen.make_tileable``), not in this sentence.
    """
    kind = str(task_kind or "").strip().lower()
    if kind in TEXTURE_KINDS:
        return _TEXTURE_FORM + (_TILEABLE_FORM if tileable else "")
    if kind in DECAL_KINDS:
        return _DECAL_FORM
    return ""


_VOCABULARY: list[tuple[re.Pattern, str, Optional[frozenset]]] = [
    (re.compile(r"pixel art|pixel[- ]grid|chunky pixel", re.I),
     "true chunky pixel art with a visible pixel grid, hard-edged, no "
     "anti-aliased or painterly rendering", None),
    # PROJECTION is about things that sit on the world's floor. A radial impact
    # burst has no projection to be wrong about, and telling it to be isometric
    # gets a burst drawn on a diamond of ground.
    (re.compile(r"isometric|2:1|3/4", re.I),
     "angled 3/4 isometric view, 2:1 tile geometry, never flat top-down and "
     "never straight-on front view", _WORLD),
    # ANATOMY. The one that cost four generations — see the note above.
    (re.compile(r"chibi", re.I),
     "chibi proportions, large head, short body", _CHARACTERS),
    (re.compile(r"16-bit|snes", re.I),
     "16-bit SNES-era sprite work", None),
    (re.compile(r"dark neon|neon", re.I),
     "dark palette with neon accents", None),
    (re.compile(r"greyscale|grayscale|monochrome", re.I),
     "greyscale only, no hue", None),
    # A SILHOUETTE is a property of a thing standing against a background. A
    # tiling floor material has neither, and "bold outlines readable at small
    # size" on a flat map gets an outlined tile — a black border baked into the
    # albedo, repeated across the mesh. Scoped to the kinds that were already
    # getting it, so nothing existing changes.
    (re.compile(r"bold outline|readable silhouette|silhouette", re.I),
     "bold outlines and a silhouette readable at small size", _SUBJECTS),
]


def _in_scope(scope: Optional[frozenset], kind: str) -> bool:
    """SCOPING ONLY EVER SUBTRACTS WHEN THE CALLER SAID WHAT IT IS MAKING.

    An empty `task_kind` means "unspecified", not "none of the above", and the
    honest reading of it is the historic one: give it everything the bible asked
    for. Treating unspecified as out-of-scope was the first cut of this and it
    silently stripped the isometric directive from every caller that had never
    needed to name its kind — narrowing a hundred working paths to fix one
    broken one. A directive is withheld only from a kind that has explicitly
    identified itself as something the directive is not about.
    """
    return scope is None or not kind or kind in scope


def directives_for(task_kind: str = "") -> list[str]:
    """The directives in scope for a kind — exposed so a caller can ASK rather
    than discover by generating a picture and looking at it."""
    kind = str(task_kind or "").strip().lower()
    return [d for _, d, scope in _VOCABULARY if _in_scope(scope, kind)]


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
