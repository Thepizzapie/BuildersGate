"""Painted-art leg of the asset pipeline — gpt-image via the OpenAI API.

Division of labor, stated plainly: Blender owns anything needing GEOMETRIC
CONSISTENCY (sprite flipbooks — the same rig every frame), this owns one-off
PAINTED art: portraits, select-screen cards, title splashes, stage paint-overs.
An image model cannot hold a character rig steady across twelve poses; Blender
cannot paint like a splash screen. Use each for what it is.

The key comes from OPENAI_API_KEY (the project's .env is loaded by the server).
It is read at call time and never appears in results, logs, or the ledger.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence

# Model routing. DIRECTOR DIRECTIVE (2026-07-20): gpt-image-2 is BANNED —
# gpt-image-1 for everything (gpt-image-1-mini acceptable for cheap drafts via
# the env overrides below). gpt-image-2 also REJECTS background=transparent
# (400) and proved flaky on sprite work, so this is consistency AND policy.
DEFAULT_OPAQUE_MODEL = "gpt-image-1"
DEFAULT_TRANSPARENT_MODEL = "gpt-image-1"
SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
QUALITIES = ("low", "medium", "high", "auto")

# The only square this API offers, and the one every UV-sampled map has to use.
SQUARE_SIZE = "1024x1024"


def size_for(size: str = SQUARE_SIZE, *, task_kind: str = "") -> str:
    """The size a generation should ACTUALLY use for this kind of asset.

    Texture maps are forced square and everything else is returned untouched,
    so this is a no-op for every path that already works.

    Why it is not optional: the sizes above are 1:1, 3:2 and 2:3, a UV island is
    unit square, and a 1536x1024 map sampled across one is stretched 1.5x in a
    single axis. Nothing downstream can undo that — the mesh gets a wood grain
    wider than it is deep and the only symptom is that the material "looks
    off", which is not a symptom anyone can act on. A caller that genuinely
    wants a non-square map can still pass one by not naming a texture kind.
    """
    try:
        from bgate_core.artdirection import is_texture_kind
    except Exception:                                          # pragma: no cover
        return size
    return SQUARE_SIZE if is_texture_kind(task_kind) else size


def make_tileable(path: str, out_path: Optional[str] = None) -> dict:
    """Make an image tile against itself by MIRRORING it. In place by default.

    NOT a seamless-texture synthesiser, and this does not claim to be one. It
    builds the 2x2 mirrored composite — original, h-flip, v-flip, both — and
    scales it back down to the original size. That makes the left edge the
    exact mirror of the right and the top of the bottom, so the tile joins with
    no seam BY CONSTRUCTION rather than by the model having been lucky.

    What you pay for the guarantee, stated so nobody is surprised by it on a
    mesh: the result is bilaterally symmetric, which reads as a repeating
    butterfly on anything with strong directional structure — wood grain,
    brickwork, planking, lettering. It is right for noise, plaster, dirt, rock,
    rust and fabric weave; it is wrong for anything laid out in rows. Halving
    the composite also costs about half the fine detail.

    The prompt-side ask (``artdirection.form_clause(..., tileable=True)``) is
    what gets a genuinely seamless map when the model can manage one; this is
    the floor underneath it. Returns ``{ok, path, method, note}`` and never
    raises — a texture that failed to tile is still a texture.

    THE SAVE FORMAT IS NAMED EXPLICITLY, and that is a bug fix, not a tidy-up.
    ``Image.save`` dispatches on the destination's SUFFIX, so a file written
    without one — which is what a caller passing ``filename="litter_albedo"``
    gets — raised ``ValueError: unknown file extension:`` on every call. The
    failure was real and completely invisible: four texture generations came back
    reporting a tileable map, and the terrain seamed visibly at 2.4m tiling
    across a full-screen floor. Nothing downstream re-checks, so the first
    evidence was the render.
    """
    src = Path(path)
    dst = Path(out_path or path)
    try:
        from PIL import Image
    except ImportError:
        return {"ok": False, "path": str(src), "method": "",
                "note": "Pillow is not installed — the image was left as it is, "
                        "so its edges are only as seamless as the model made them"}
    try:
        with Image.open(src) as im:
            # The SOURCE knows what it is even when the destination path does
            # not: Pillow read it, so `im.format` is the real container. Falling
            # back to PNG rather than raising is right for the one case that
            # reaches here — a generated plate whose name lost its extension.
            fmt = im.format or "PNG"
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            w, h = im.size
            flip_h = im.transpose(Image.FLIP_LEFT_RIGHT)
            canvas = Image.new(im.mode, (w * 2, h * 2))
            canvas.paste(im, (0, 0))
            canvas.paste(flip_h, (w, 0))
            canvas.paste(im.transpose(Image.FLIP_TOP_BOTTOM), (0, h))
            canvas.paste(flip_h.transpose(Image.FLIP_TOP_BOTTOM), (w, h))
            tiled = canvas.resize((w, h), Image.LANCZOS)
            if tiled.mode == "RGBA" and fmt in ("JPEG", "JPG"):
                # A JPEG cannot hold the alpha the convert above may have kept.
                tiled = tiled.convert("RGB")
            tiled.save(dst, format=fmt)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "path": str(src), "method": "",
                "note": f"could not mirror-tile ({type(exc).__name__}: {exc}) — "
                        "the image was left as it is"}
    return {"ok": True, "path": str(dst), "method": "mirror-2x2",
            "note": "edges join by mirroring, so the map is bilaterally "
                    "symmetric — right for noise and plaster, wrong for grain "
                    "or brick laid in rows"}

# Approximate per-image spend (gpt-image-1, 1024x1024) by quality — every leg
# that spends through this adapter estimates from THIS table, so tools can
# surface dollars (not counts) before a batch is confirmed. "auto" is billed
# as whatever the API picks; estimate it as medium.
IMAGE_PRICE_USD: dict[str, float] = {
    "low": 0.011, "medium": 0.042, "high": 0.167, "auto": 0.042,
}


def price_per_image(quality: str = "medium") -> float:
    """Estimated $ for one generation at `quality`; unknown values price as
    medium rather than raising — an estimate must never block the work."""
    return IMAGE_PRICE_USD.get(quality, IMAGE_PRICE_USD["medium"])


def cost_meta(result: dict) -> dict:
    """The two numbers a caller pins into artifact metadata at register time.

    Kept here so every leg spells them the same way — the art UI reads exactly
    these keys off ``metadata`` and renders "18.4s · ~$0.042" per candidate.
    """
    return {"seconds": (result or {}).get("seconds"),
            "estimated_usd": (result or {}).get("estimated_usd")}


def _account(result: dict, root: Any, logical_name: str,
             work_item_id: Optional[int], detail: str) -> None:
    """Write the estimated spend to the ledger. Best-effort by construction:
    losing a ledger row must never lose the image that was paid for."""
    if not root or not result.get("ok"):
        return
    try:
        from bgate_core import spend

        spend.record(root, float(result.get("estimated_usd") or 0.0),
                     kind="image", work_item_id=work_item_id,
                     logical_name=logical_name or "", detail=detail)
    except Exception:
        pass


def _model_for(transparent: bool) -> str:
    """BGATE_IMAGE_MODEL forces one model for everything; otherwise BOTH modes
    route to gpt-image-1 (the 2026-07-20 ban above — this used to send opaque
    work to gpt-image-2 and the docstring outlived the code). The per-mode hook
    survives only as BGATE_IMAGE_MODEL_TRANSPARENT / BGATE_IMAGE_MODEL_OPAQUE."""
    forced = os.environ.get("BGATE_IMAGE_MODEL")
    if forced:
        return forced
    if transparent:
        return os.environ.get("BGATE_IMAGE_MODEL_TRANSPARENT",
                              DEFAULT_TRANSPARENT_MODEL)
    return os.environ.get("BGATE_IMAGE_MODEL_OPAQUE", DEFAULT_OPAQUE_MODEL)


def available() -> dict:
    """Is the painted-art leg usable? Reports presence, never the key itself."""
    if not os.environ.get("OPENAI_API_KEY"):
        return {"available": False,
                "reason": "OPENAI_API_KEY not set — put it in the project's .env "
                          "(gitignored) or the machine environment"}
    try:
        import openai  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "openai package not installed"}
    return {"available": True,
            "model_transparent": _model_for(True),
            "model_opaque": _model_for(False)}


# USER RULE (enforced, not advised): character frames are generated ONE per
# API call. Multi-pose sheet generations are where the model loses the
# character — poses drift, cells misalign, identity mutates. Prompts that ask
# for sheets/rows/multiple poses are refused with a pointer to image_sprites
# (which is one call per frame, chained).
#
# allow_multi is a PYTHON-ONLY escape hatch for in-process callers doing
# legitimately multi-subject art (crowds, rosters, backdrops with cast) — no MCP
# tool exposes it, so the refusal below must never tell an agent to pass it. An
# error naming a parameter the caller cannot set is an error it can only retry
# verbatim; the message names prompt edits and image_sprites instead.
import re as _re

_MULTI_POSE = _re.compile(
    r"sprite\s*sheet|pose\s*(row|sheet|grid)|multiple\s+poses|"
    r"\b(two|three|four|five|six|\d+)\s+(poses|frames|stances)\b|"
    r"turn\s*around\s*sheet|animation\s+frames", _re.I)


def _reject_multi_pose(prompt: str, allow_multi: bool) -> dict | None:
    if allow_multi:
        return None
    match = _MULTI_POSE.search(prompt)
    if match:
        return {"ok": False,
                "error": f"prompt asks for multiple poses in one image "
                         f"({match.group(0)!r}) — sheet generations are where "
                         "character consistency dies. Two ways forward: rewrite "
                         "the prompt to describe ONE frame (drop "
                         f"{match.group(0)!r} and name a single stance), or call "
                         "image_sprites with one entry in `poses` per frame — it "
                         "generates a reference, then one edit per pose, and "
                         "stitches the sheet for you."}
    return None


def generate(prompt: str, out_path: str, *, size: str = "1024x1024",
             quality: str = "medium", transparent: bool = False,
             allow_multi: bool = False, timeout: float = 300.0,
             root: Any = None, logical_name: str = "",
             work_item_id: Optional[int] = None,
             ref_paths: Sequence[str] = (), task_kind: str = "",
             tileable: bool = False) -> dict:
    """Generate one image to out_path. Returns {ok, path, bytes, ...} or an error.

    ``ref_paths`` conditions the generation on reference image(s) — the pinned
    style anchors. gpt-image has no separate reference input, so this DELEGATES
    to :func:`edit`, which is that model's only way to hold an anchor. It is
    here so that "generate, conditioned on the pinned refs" is one call at the
    adapter surface for both providers instead of a branch every caller has to
    know about. Empty by default: an existing caller is unaffected.

    ``task_kind`` is advisory except for texture kinds, which are forced square
    (see :func:`size_for`). ``tileable`` runs the mirrored post-pass over the
    finished file (see :func:`make_tileable`) and is meaningful only on a
    texture. Both default off.

    transparent=True REQUESTS a transparent background (PNG alpha). Requests it —
    does not get it. MEASURED 2026-07-25 on a 4-frame character sheet:
    background="transparent" came back with a brown gradient behind the
    character, and on the runs where it does key, white interiors (eye whites)
    come back punched to holes. Anything that must actually composite over game
    art goes through bgate_core.chroma instead — flat keyable backdrop, keyed
    out, audited — and this flag stays only for callers that want to try the
    provider's own mode and can live with whatever arrives.

    Every result carries ``seconds`` (wall clock the call actually took) and
    ``estimated_usd`` (from IMAGE_PRICE_USD — the one price table). Pass ``root``
    to also append the spend to the project ledger, keyed by ``logical_name`` so
    the art lab can show a running total per asset.
    """
    size = size_for(size, task_kind=task_kind)
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {QUALITIES}, got {quality!r}")
    if ref_paths:
        return edit(prompt, [str(p) for p in ref_paths], out_path, size=size,
                    quality=quality, transparent=transparent,
                    allow_multi=allow_multi, timeout=timeout, root=root,
                    logical_name=logical_name, work_item_id=work_item_id,
                    task_kind=task_kind, tileable=tileable)
    rejected = _reject_multi_pose(prompt, allow_multi)
    if rejected:
        return rejected
    probe = available()
    if not probe["available"]:
        return {"ok": False, "error": probe["reason"]}

    from openai import OpenAI

    client = OpenAI(timeout=timeout)
    model = _model_for(transparent)
    kwargs = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }
    if transparent:
        kwargs["background"] = "transparent"
        kwargs["output_format"] = "png"

    started = time.monotonic()
    try:
        result = client.images.generate(**kwargs)
    except Exception as exc:
        # API errors (quota, content policy, bad key) come back as facts the
        # agent can act on — sanitized by the SDK, no key material inside.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": 0.0}

    saved = _save(result, out_path, model, size, quality, transparent,
                  seconds=round(time.monotonic() - started, 2))
    _tile(saved, tileable)
    _account(saved, root, logical_name or Path(out_path).stem, work_item_id,
             f"generate {size} {quality}" + (" transparent" if transparent else ""))
    return saved


def _tile(result: dict, tileable: bool) -> None:
    """Run the mirrored pass over a finished file and record what it did.

    Best-effort in the same sense the ledger is: the generation has already been
    paid for, and a post-pass that could not run must not turn a good image into
    an error. The result carries the note so a human can see which files are
    only tiling because they were mirrored.
    """
    if not tileable or not result.get("ok") or not result.get("path"):
        return
    result["tileable"] = make_tileable(result["path"])


def edit(prompt: str, ref_paths: list[str], out_path: str, *,
         size: str = "1024x1024", quality: str = "medium",
         transparent: bool = False, allow_multi: bool = False,
         timeout: float = 300.0, root: Any = None, logical_name: str = "",
         work_item_id: Optional[int] = None, task_kind: str = "",
         tileable: bool = False) -> dict:
    """Generate an image CONDITIONED ON reference image(s) — the consistency
    primitive. A fresh generation invents a new character every time; an edit
    against a reference keeps the same one. This is how sprite poses stay the
    same fighter: one approved reference, then every pose derived from it.
    ONE frame per call — multi-pose prompts are refused (see _reject_multi_pose).

    ``task_kind`` and ``tileable`` mean exactly what they mean on
    :func:`generate`, and are here so an anchored TEXTURE (edit against the
    pinned refs) gets the same square constraint and the same tiling pass a
    fresh one does.
    """
    size = size_for(size, task_kind=task_kind)
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {QUALITIES}, got {quality!r}")
    rejected = _reject_multi_pose(prompt, allow_multi)
    if rejected:
        return rejected
    if not ref_paths:
        raise ValueError("edit() needs at least one reference image")
    for ref in ref_paths:
        if not Path(ref).is_file():
            raise FileNotFoundError(f"reference image not found: {ref}")
    probe = available()
    if not probe["available"]:
        return {"ok": False, "error": probe["reason"]}

    from openai import OpenAI

    client = OpenAI(timeout=timeout)
    model = _model_for(transparent)  # same routing as generate() — keep in sync
    # Appended one at a time inside the try, not built as a comprehension above
    # it: a comprehension that raises on the fourth reference never binds the
    # list, so the three handles it had already opened were unreachable and the
    # finally below could not close them.
    handles: list = []
    started = time.monotonic()
    try:
        for ref in ref_paths:
            handles.append(open(ref, "rb"))
        kwargs = {
            "model": model,
            "image": handles if len(handles) > 1 else handles[0],
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "n": 1,
        }
        if transparent:
            kwargs["background"] = "transparent"
        try:
            result = client.images.edit(**kwargs)
        except TypeError:
            # Older SDK/model rejecting a kwarg — retry with the minimal set.
            result = client.images.edit(model=model,
                                        image=kwargs["image"], prompt=prompt,
                                        size=size, n=1)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - started, 2),
                    "estimated_usd": 0.0}
    finally:
        for handle in handles:
            handle.close()

    saved = _save(result, out_path, model, size, quality, transparent,
                  seconds=round(time.monotonic() - started, 2))
    _tile(saved, tileable)
    _account(saved, root, logical_name or Path(out_path).stem, work_item_id,
             f"edit {size} {quality} ({len(ref_paths)} ref)")
    return saved


def _save(result, out_path: str, model: str, size: str, quality: str,
          transparent: bool, seconds: Optional[float] = None) -> dict:
    datum = result.data[0]
    if not getattr(datum, "b64_json", None):
        # NOT ZERO. Reaching here means the call SUCCEEDED and came back without
        # bytes we can use — the request was generated and billed, and only the
        # payload is missing. The other two failure paths in this module return
        # 0.0 because they raised out of the SDK before anything was made; this
        # one used to copy them, which reported a real charge as free to
        # anything reading the number. Unknown, and it says which one it is.
        return {"ok": False, "error": "API returned no image payload",
                "seconds": seconds, "estimated_usd": None,
                "cost_note": "the generation call succeeded and returned no "
                             "image, so it was almost certainly billed at about "
                             f"${price_per_image(quality):.3f} — unknown rather "
                             "than zero, because a zero here reads as free"}

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(datum.b64_json))
    return {
        "ok": True,
        "path": str(out),
        "bytes": out.stat().st_size,
        "seconds": seconds,
        "estimated_usd": price_per_image(quality),
        "model": model,
        "size": size,
        "quality": quality,
        "transparent": transparent,
        "revised_prompt": getattr(datum, "revised_prompt", None),
    }
