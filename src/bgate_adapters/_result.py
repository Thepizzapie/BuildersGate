"""The one result shape every generation entry point returns.

``{ok, path, usd, provider, model, seconds, ...extras}`` — so a caller
downstream (chroma, the art UI) reads one key set whichever
adapter drew the thing. ``usd`` was ``estimated_usd`` until 0.1.44; the old
key is still written as an alias for ONE release so a dashboard or script
reading it keeps working, and is removed in the next.
"""
from __future__ import annotations

KEYS = ("ok", "path", "usd", "provider", "model", "seconds")


def shape(result: dict, **defaults) -> dict:
    """``result`` with every shared key present (``defaults`` fill the gaps)
    and the one-release ``estimated_usd`` alias mirroring ``usd``."""
    out = {"ok": False, "path": "", "usd": None, "provider": "", "model": "",
           "seconds": 0.0}
    out.update(defaults)
    out.update(result)
    out["estimated_usd"] = out["usd"]
    return out
