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
from typing import Any, Optional

# Model routing. DIRECTOR DIRECTIVE (2026-07-20): gpt-image-2 is BANNED —
# gpt-image-1 for everything (gpt-image-1-mini acceptable for cheap drafts via
# the env overrides below). gpt-image-2 also REJECTS background=transparent
# (400) and proved flaky on sprite work, so this is consistency AND policy.
DEFAULT_OPAQUE_MODEL = "gpt-image-1"
DEFAULT_TRANSPARENT_MODEL = "gpt-image-1"
SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
QUALITIES = ("low", "medium", "high", "auto")

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
             work_item_id: Optional[int] = None) -> dict:
    """Generate one image to out_path. Returns {ok, path, bytes, ...} or an error.

    transparent=True requests a transparent background (PNG alpha) — right for
    portraits/cards that composite over game art; wrong for full backdrops.

    Every result carries ``seconds`` (wall clock the call actually took) and
    ``estimated_usd`` (from IMAGE_PRICE_USD — the one price table). Pass ``root``
    to also append the spend to the project ledger, keyed by ``logical_name`` so
    the art lab can show a running total per asset.
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
    _account(saved, root, logical_name or Path(out_path).stem, work_item_id,
             f"generate {size} {quality}" + (" transparent" if transparent else ""))
    return saved


def edit(prompt: str, ref_paths: list[str], out_path: str, *,
         size: str = "1024x1024", quality: str = "medium",
         transparent: bool = False, allow_multi: bool = False,
         timeout: float = 300.0, root: Any = None, logical_name: str = "",
         work_item_id: Optional[int] = None) -> dict:
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
    started = time.monotonic()
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
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - started, 2),
                    "estimated_usd": 0.0}
    finally:
        for handle in handles:
            handle.close()

    saved = _save(result, out_path, model, size, quality, transparent,
                  seconds=round(time.monotonic() - started, 2))
    _account(saved, root, logical_name or Path(out_path).stem, work_item_id,
             f"edit {size} {quality} ({len(ref_paths)} ref)")
    return saved


def _save(result, out_path: str, model: str, size: str, quality: str,
          transparent: bool, seconds: Optional[float] = None) -> dict:
    datum = result.data[0]
    if not getattr(datum, "b64_json", None):
        return {"ok": False, "error": "API returned no image payload",
                "seconds": seconds, "estimated_usd": 0.0}

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
