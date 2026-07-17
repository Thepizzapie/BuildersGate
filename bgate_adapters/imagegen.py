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
from pathlib import Path
from typing import Optional

# Model routing, learned in production: gpt-image-2 paints opaque pieces well
# but REJECTS background=transparent (400) and proved flaky on sprite work;
# gpt-image-1 is the reliable choice wherever alpha matters. So the mode picks
# the model — agents never have to remember which is which.
DEFAULT_OPAQUE_MODEL = "gpt-image-2"
DEFAULT_TRANSPARENT_MODEL = "gpt-image-1"
SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
QUALITIES = ("low", "medium", "high", "auto")


def _model_for(transparent: bool) -> str:
    """BGATE_IMAGE_MODEL forces one model for everything; otherwise the mode
    routes: transparent -> gpt-image-1, opaque -> gpt-image-2 (overridable via
    BGATE_IMAGE_MODEL_TRANSPARENT / BGATE_IMAGE_MODEL_OPAQUE)."""
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
# (which is one call per frame, chained). allow_multi exists for legitimately
# multi-subject art (crowds, rosters, backdrops with cast).
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
                         "character consistency dies. Generate ONE frame per "
                         "call: use image_sprites (per-pose, chained), or pass "
                         "allow_multi=true only for genuinely multi-subject "
                         "art (crowds, rosters, backdrops)."}
    return None


def generate(prompt: str, out_path: str, *, size: str = "1024x1024",
             quality: str = "medium", transparent: bool = False,
             allow_multi: bool = False, timeout: float = 300.0) -> dict:
    """Generate one image to out_path. Returns {ok, path, bytes, ...} or an error.

    transparent=True requests a transparent background (PNG alpha) — right for
    portraits/cards that composite over game art; wrong for full backdrops.
    """
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {QUALITIES}, got {quality!r}")
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

    try:
        result = client.images.generate(**kwargs)
    except Exception as exc:
        # API errors (quota, content policy, bad key) come back as facts the
        # agent can act on — sanitized by the SDK, no key material inside.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return _save(result, out_path, model, size, quality, transparent)


def edit(prompt: str, ref_paths: list[str], out_path: str, *,
         size: str = "1024x1024", quality: str = "medium",
         transparent: bool = False, allow_multi: bool = False,
         timeout: float = 300.0) -> dict:
    """Generate an image CONDITIONED ON reference image(s) — the consistency
    primitive. A fresh generation invents a new character every time; an edit
    against a reference keeps the same one. This is how sprite poses stay the
    same fighter: one approved reference, then every pose derived from it.
    ONE frame per call — multi-pose prompts are refused (see _reject_multi_pose).
    """
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
    handles = [open(ref, "rb") for ref in ref_paths]
    try:
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
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        for handle in handles:
            handle.close()

    return _save(result, out_path, model, size, quality, transparent)


def _save(result, out_path: str, model: str, size: str, quality: str,
          transparent: bool) -> dict:
    datum = result.data[0]
    if not getattr(datum, "b64_json", None):
        return {"ok": False, "error": "API returned no image payload"}

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(datum.b64_json))
    return {
        "ok": True,
        "path": str(out),
        "bytes": out.stat().st_size,
        "model": model,
        "size": size,
        "quality": quality,
        "transparent": transparent,
        "revised_prompt": getattr(datum, "revised_prompt", None),
    }
