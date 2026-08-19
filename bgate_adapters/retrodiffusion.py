"""Retro Diffusion — the animation model that knows what a walk cycle is.

The motion testbed's verdict, measured: general image models produce clean
FRAMES and no GAIT; RD's advanced-animation models produce the gait. Fed one
of downsizing's own character frames it returned a full stride with zero
battery findings and the identity intact — choreography is its training data.
Fed a large out-of-distribution character it redraws at ~70%; the sprite
contract's start-frame-per-direction flow is built around that boundary.

API facts from the vendor's own agent reference (api-examples llms.txt,
verified live 2026-08-17): base https://api.retrodiffusion.ai/v1, header
X-RD-Token, prompts describe the SUBJECT only (never say "pixel art" — the
style carries it), input_image is raw base64 RGB WITHOUT transparency,
animations want the async job flow, check_cost dry-runs for free, failures
are auto-refunded. Also here because they are free and useful: pixel_fixer
(native-grid reconstruction) and palette conversion tools.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from bgate_core import envfile

BASE = "https://api.retrodiffusion.ai/v1"
ENV_KEY = "RETRO_DIFFUSION_API_KEY"

#: The advanced-animation actions RD ships. custom_action takes its motion
#: from the prompt and costs more; everything else is a named cycle.
ACTIONS = ("walking", "idle", "jump", "crouch", "attack", "destroy",
           "custom_action", "subtle_motion")

#: Flat prices, from the published table. RD bills prepaid USD and refunds
#: failures; check_cost() is the free authoritative quote.
ACTION_COST = {"custom_action": 0.25, "subtle_motion": 0.25}
DEFAULT_ACTION_COST = 0.14

FRAME_COUNTS = (4, 6, 8, 10, 12, 16)

#: Job poll cadence. Animations run tens of seconds; the vendor suggests ~2s.
POLL_SECONDS = 2.0


class RetroDiffusionError(RuntimeError):
    """A call that failed in words worth surfacing. Charges on failed jobs
    are refunded upstream, so an error here is annoyance, not loss."""


def api_key(root: Any = None) -> str:
    """The token, from the project .env, ~/.bgate/.env, or the shell."""
    try:
        envfile.load_env(root)
    except Exception:
        pass
    return (os.environ.get(ENV_KEY) or "").strip()


def available(root: Any = None) -> dict:
    """Presence only — the free health check is balance(), run on demand."""
    key = api_key(root)
    if not key:
        return {"available": False,
                "reason": f"{ENV_KEY} not set — put it in the project's .env "
                          "or ~/.bgate/.env; create a key (rdpk-...) at "
                          "retrodiffusion.ai/app/devtools"}
    return {"available": True, "actions": list(ACTIONS)}


def balance(root: Any = None) -> dict:
    """Prepaid USD remaining. The one probe that touches the network."""
    got = _get("/inferences/credits", root=root)
    return {"balance": got.get("balance"), "credits": got.get("credits")}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _headers(root: Any) -> dict:
    key = api_key(root)
    if not key:
        raise RetroDiffusionError(available(root)["reason"])
    return {"X-RD-Token": key, "Content-Type": "application/json"}


def _request(method: str, path: str, payload: Optional[dict], root: Any,
             timeout: float) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=None if payload is None else json.dumps(payload).encode(),
        method=method, headers=_headers(root))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read())
            raw = body.get("detail")
            if isinstance(raw, list):
                detail = "; ".join(str(e.get("msg", e)) for e in raw)
            elif isinstance(raw, dict):
                detail = str(raw.get("message") or raw)
            else:
                detail = str(raw or body)
        except Exception:
            pass
        raise RetroDiffusionError(
            f"RD {method} {path} -> {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RetroDiffusionError(f"RD unreachable: {exc.reason}") from exc


def _get(path: str, *, root: Any = None, timeout: float = 30.0) -> dict:
    return _request("GET", path, None, root, timeout)


def _post(path: str, payload: dict, *, root: Any = None,
          timeout: float = 120.0) -> dict:
    return _request("POST", path, payload, root, timeout)


def _b64_rgb(path: str | os.PathLike[str], bg: tuple[int, int, int]) -> str:
    """A file as the raw base64 RGB PNG the API wants — alpha flattened onto
    a flat backdrop, because 'RGB without transparency' is the input contract
    and a transparent PNG arrives as undefined colour under the alpha."""
    import io

    from PIL import Image

    img = Image.open(path).convert("RGBA")
    base = Image.new("RGBA", img.size, (*bg, 255))
    base.alpha_composite(img)
    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------
def check_cost(action: str, size: tuple[int, int], *, root: Any = None) -> dict:
    """The authoritative price for one animation, free of charge.

    A dry run validates price, not pixels, so no input image is sent; if the
    style insists on one anyway (422), the published flat table answers.
    """
    body = _animation_body("cost probe", action, "", size, 4)
    body["check_cost"] = True
    try:
        got = _post("/inferences", body, root=root)
        return {"usd": got.get("balance_cost"),
                "balance": got.get("remaining_balance")}
    except RetroDiffusionError:
        return {"usd": ACTION_COST.get(action, DEFAULT_ACTION_COST),
                "balance": None}


def _animation_body(prompt: str, action: str, image_b64: str,
                    size: tuple[int, int], frames: int) -> dict:
    if action not in ACTIONS:
        raise RetroDiffusionError(
            f"unknown action {action!r} — one of {ACTIONS}")
    if frames not in FRAME_COUNTS:
        raise RetroDiffusionError(
            f"frames must be one of {FRAME_COUNTS}, got {frames}")
    w, h = int(size[0]), int(size[1])
    if not (32 <= w <= 256 and 32 <= h <= 256):
        raise RetroDiffusionError(f"size {w}x{h} outside RD's 32..256")
    # remove_bg deliberately OFF. RD's server-side keying eats any interior
    # colour near the background — a white undershirt, pale jeans — and hands
    # back sprites with transparent HOLES through the body, discovered on a
    # 45-strip run viewed against a light background. The caller keys the
    # flat background itself (see key_background): a corner-seeded flood only
    # removes background CONNECTED to the outside, so pale garments survive.
    body = {
        "prompt": prompt,
        "prompt_style": f"rd_advanced_animation__{action}",
        "width": w, "height": h, "num_images": 1,
        "frames_duration": frames,
        "return_spritesheet": True,
    }
    if image_b64:
        body["input_image"] = image_b64
    return body


def animate(start_frame: str | os.PathLike[str], action: str, *,
            frames: int = 8, size: Optional[tuple[int, int]] = None,
            prompt: str = "", palette: Optional[str | os.PathLike[str]] = None,
            bg: tuple[int, int, int] = (255, 255, 255),
            root: Any = None, timeout: float = 360.0) -> dict:
    """One character frame -> one animation spritesheet. Blocking.

    ``prompt`` describes the MOTION, not the art ("confident, steady steps");
    the style id carries the pixel rendering. ``size`` defaults to the start
    frame's own dimensions — RD wants them to match. ``palette`` optionally
    pins output colours (their input_palette). Uses the async job flow the
    vendor recommends for animations, polling until done or ``timeout``.

    Returns {ok, sheet_b64, usd, balance, frames, size}. The caller decodes
    and slices; this adapter does not write files.
    """
    from PIL import Image

    src = Path(start_frame)
    if not src.is_file():
        raise RetroDiffusionError(f"no start frame at {src}")
    if size is None:
        with Image.open(src) as im:
            size = im.size
    body = _animation_body(prompt or "smooth, natural motion", action,
                           _b64_rgb(src, bg), size, frames)
    if palette:
        body["input_palette"] = _b64_rgb(palette, (0, 0, 0))
    body["async"] = True

    got = _post("/inferences", body, root=root)
    task = got.get("task_id")
    if not task:
        raise RetroDiffusionError(f"job not accepted: {got}")
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        status = _get(f"/inferences/tasks/{task}", root=root)
        if status.get("status") in ("succeeded", "failed"):
            break
    if status.get("status") != "succeeded":
        err = status.get("error") or {"detail": f"still {status.get('status')!r} "
                                                f"after {timeout:.0f}s"}
        raise RetroDiffusionError(
            f"animation job {task} did not succeed: {err}")
    result = status.get("result") or {}
    images = result.get("base64_images") or []
    if not images:
        raise RetroDiffusionError("job succeeded but returned no images")
    return {"ok": True, "sheet_b64": images[0],
            "usd": result.get("balance_cost"),
            "balance": result.get("remaining_balance"),
            "frames": frames, "size": [int(size[0]), int(size[1])]}


def key_background(img, *, tolerance: int = 28) -> "object":
    """Flood-key an RD sheet's flat background to transparency. Returns RGBA.

    Seeded from all four borders, spreading only through pixels within
    ``tolerance`` of the border's own dominant colour — background connected
    to the outside vanishes, interior regions of a similar colour (a white
    shirt, pale jeans) DO NOT, because the flood cannot reach them through
    the figure's outline. This is the client-side answer to remove_bg's
    holes; the two differ exactly on enclosed pale pixels, which is the
    difference that matters.
    """
    from collections import Counter, deque

    import numpy as np
    from PIL import Image

    rgba = img.convert("RGBA")
    a = np.array(rgba)
    rgb = a[..., :3].astype(int)
    h, w = rgb.shape[:2]
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    bg = Counter(map(tuple, border)).most_common(1)[0][0]
    near = (np.abs(rgb - np.array(bg)).sum(axis=2) <= tolerance * 3)
    seen = np.zeros((h, w), bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True; q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True; q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and near[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True; q.append((ny, nx))
    a[seen] = 0
    return Image.fromarray(a)


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------
#: The tile styles, with the size range each accepts. `tileset` returns a
#: TERRAIN SET — two terrains and the edges between them — which is the
#: autotile family, not one texture; measured on a $0.10 call that came back
#: as a 4x5 grass/stone sheet.
TILE_STYLES = {
    "tileset": {"style": "rd_tile__tileset", "usd": 0.10, "px": (16, 32)},
    "tileset_advanced": {"style": "rd_tile__tileset_advanced", "usd": 0.10,
                         "px": (16, 32), "extra_prompt": True},
    "single": {"style": "rd_tile__single_tile", "usd": 0.02, "px": (16, 64)},
    "variation": {"style": "rd_tile__tile_variation", "usd": 0.02,
                  "px": (16, 128), "needs_input": True},
    "object": {"style": "rd_tile__tile_object", "usd": 0.02, "px": (16, 96)},
    "scene_object": {"style": "rd_tile__scene_object", "usd": 0.02,
                     "px": (64, 384)},
}


def tileset(prompt: str, *, kind: str = "tileset", tile_px: int = 32,
            extra_prompt: str = "", input_image: Optional[str] = None,
            palette: Optional[str] = None, root: Any = None,
            timeout: float = 300.0) -> dict:
    """A tile sheet from one of RD's tile styles. Returns {ok, png, usd, ...}.

    ``prompt`` describes the MATERIAL ("mossy grey stone dungeon floor"), never
    the rendering — the style carries the pixel art, same contract as the
    animation call. ``kind`` picks from :data:`TILE_STYLES`; ``variation``
    needs ``input_image`` and is how a missing mask gets filled from a tile
    that already exists.

    Synchronous: tile calls return in seconds, unlike the animation jobs.
    """
    spec = TILE_STYLES.get(kind)
    if spec is None:
        raise RetroDiffusionError(
            f"unknown tile kind {kind!r} — one of {sorted(TILE_STYLES)}")
    lo, hi = spec["px"]
    if not (lo <= tile_px <= hi):
        raise RetroDiffusionError(
            f"{kind} draws {lo}-{hi}px tiles, not {tile_px}")
    if spec.get("needs_input") and not input_image:
        raise RetroDiffusionError(f"{kind} needs an input_image to vary")

    body = {"prompt": prompt, "prompt_style": spec["style"],
            "width": int(tile_px), "height": int(tile_px), "num_images": 1}
    if extra_prompt and spec.get("extra_prompt"):
        body["extra_prompt"] = extra_prompt
    if input_image:
        body["input_image"] = _b64_rgb(input_image, (255, 255, 255))
    if palette:
        body["input_palette"] = _b64_rgb(palette, (0, 0, 0))

    got = _post("/inferences", body, root=root, timeout=timeout)
    images = got.get("base64_images") or []
    if not images:
        raise RetroDiffusionError("tile call returned no images")
    return {"ok": True, "png_b64": images[0],
            "usd": got.get("balance_cost"),
            "balance": got.get("remaining_balance"),
            "kind": kind, "tile_px": int(tile_px)}


# ---------------------------------------------------------------------------
# Free utilities
# ---------------------------------------------------------------------------
def pixel_fixer(image: str | os.PathLike[str], *, neural: bool = False,
                root: Any = None) -> bytes:
    """Native-grid reconstruction, free. Returns PNG bytes.

    The vendor's answer to exactly the uneven-pixel problem this project
    measured: a generation that pretends to be pixel art gets rebuilt onto a
    real grid. Rate limited at 10/min per token; sizes 16px..16MP.
    """
    body = {"input_image": base64.b64encode(Path(image).read_bytes()).decode()}
    path = "/pixel-fixer/neural" if neural else "/pixel-fixer/standard"
    got = _post(path, body, root=root)
    images = got.get("base64_images") or []
    if not images:
        raise RetroDiffusionError("pixel fixer returned no image")
    return base64.b64decode(images[0])
