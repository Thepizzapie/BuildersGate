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

DEFAULT_MODEL = "gpt-image-1"
SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
QUALITIES = ("low", "medium", "high", "auto")


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
    return {"available": True, "model": os.environ.get("BGATE_IMAGE_MODEL", DEFAULT_MODEL)}


def generate(prompt: str, out_path: str, *, size: str = "1024x1024",
             quality: str = "medium", transparent: bool = False,
             timeout: float = 300.0) -> dict:
    """Generate one image to out_path. Returns {ok, path, bytes, ...} or an error.

    transparent=True requests a transparent background (PNG alpha) — right for
    portraits/cards that composite over game art; wrong for full backdrops.
    """
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {QUALITIES}, got {quality!r}")
    probe = available()
    if not probe["available"]:
        return {"ok": False, "error": probe["reason"]}

    from openai import OpenAI

    client = OpenAI(timeout=timeout)
    kwargs = {
        "model": probe["model"],
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
        "model": probe["model"],
        "size": size,
        "quality": quality,
        "transparent": transparent,
        "revised_prompt": getattr(datum, "revised_prompt", None),
    }
