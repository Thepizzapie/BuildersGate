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
from pathlib import Path
from typing import Any, Optional

from bgate_core.store import envfile

from . import _http, _result

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

#: MOTION PROMPT CEILING, in characters. Measured on this account, twice and
#: independently: a ~140-char motion prompt returns in about two minutes, a
#: ~700-char one hung for 1800s and produced nothing, and ~900 hung the same
#: way. The job is accepted and then never completes, so the caller sees a
#: timeout rather than a rejection and reasonably concludes the provider is
#: down. The animation prompt describes MOTION only ("confident, steady
#: steps") — the style id carries the rendering and the input image carries
#: the subject — so there is nothing a long prompt buys that is worth a dead
#: job. Trimmed on a word boundary rather than refused: a caller that wrote
#: an over-long prompt still wants its animation.
#: Set to 140 rather than a round 200 because 140 is the length actually
#: MEASURED to return; 200 was interpolation between a working 140 and a
#: hanging 700, and the provider's own job list shows long-prompt jobs
#: failing beside short-prompt ones that succeed.
PROMPT_MAX_CHARS = 140


class RetroDiffusionError(_http.ProviderError):
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
    try:
        got = _http.request(method, BASE + path, headers=_headers(root),
                            json=payload, timeout=timeout,
                            provider="retrodiffusion")
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise RetroDiffusionError(f"RD unreachable: {exc.body}",
                                      provider="retrodiffusion") from exc
        detail = _detail(exc.body)
        raise RetroDiffusionError(
            f"RD {method} {path} -> {exc.status}: {detail or exc.body}",
            provider="retrodiffusion", status=exc.status, body=exc.body,
            billing=exc.billing or _http.is_billing(0, detail)) from exc
    try:
        return got.json(provider="retrodiffusion")
    except _http.ProviderError as exc:
        raise RetroDiffusionError(str(exc), provider="retrodiffusion") from exc


def _detail(body: str) -> str:
    """RD answers errors as {"detail": {...}} AND {"detail": [...]}; both
    become one sentence."""
    try:
        parsed = json.loads(body)
        raw = parsed.get("detail")
    except Exception:                                            # noqa: BLE001
        return ""
    if isinstance(raw, list):
        return "; ".join(str(e.get("msg", e)) if isinstance(e, dict) else str(e)
                         for e in raw)
    if isinstance(raw, dict):
        return str(raw.get("message") or raw)
    return str(raw or parsed)


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


def _trim_prompt(prompt: str, limit: int = PROMPT_MAX_CHARS) -> str:
    """Motion prompt, bounded. See PROMPT_MAX_CHARS for why this exists.

    Cuts on the last word boundary at or before ``limit`` so the trimmed text
    still reads as an instruction rather than half a word.
    """
    text = " ".join(str(prompt or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(" ,;:.")


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
        "prompt": _trim_prompt(prompt),
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
            out_path: Optional[str | os.PathLike[str]] = None,
            root: Any = None, timeout: float = 600.0) -> dict:
    """One character frame -> one animation spritesheet, written to disk.

    ``prompt`` describes the MOTION, not the art ("confident, steady steps");
    the style id carries the pixel rendering. ``size`` defaults to the start
    frame's own dimensions — RD wants them to match. ``palette`` optionally
    pins output colours (their input_palette). Uses the async job flow the
    vendor recommends for animations, polling until done or ``timeout``.

    TIMEOUT IS SIZED AGAINST THE MCP TOOL IDLE CEILING, NOT AGAINST
    PATIENCE. animation_generate calls this once per DRAWN DIRECTION - three
    for a four_dir contract - inside a SINGLE MCP tool call that reports no
    progress while it runs. The client aborts a silent tool at 1800s
    (CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT). At the old 360s this budgeted
    3 x (120s POST + 360s poll) = 1440s before any conform, import or
    stitching, so one hung job took the whole call down and the caller saw
    "sent no response or progress for 1800s; aborting" with nothing written
    and no error to act on - measured, twice, on this project.
    THE COST OF GIVING UP TOO EARLY IS NOT ZERO — IT IS THE WHOLE JOB.
    RD bills when the job runs, so a poll that expires before the job flips
    to succeeded pays full price and discards the result: the provider's
    dashboard shows SUCCEEDED and nothing reaches disk. Measured here at
    200s with four agents running concurrently: $0.70 charged, zero files.
    A single agent returns in about 120s, but RD queues concurrent work and
    that figure does not survive a fan-out, so the timeout is sized for the
    slow case, not the fast one. 600s costs nothing on a healthy job (the
    poll exits the moment status flips) and stops paying for results we
    then throw away. Surviving the MCP idle ceiling is the resume check's
    job in animation_generate, not this timeout's — a killed call now
    re-adopts finished directions instead of re-buying them.

    Returns the shared result shape {ok, path, usd, provider, model, seconds,
    balance, frames, size, sheet_b64}. ``out_path`` defaults to a sibling of
    the start frame; ``sheet_b64`` stays for callers that want bytes.
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

    started = time.monotonic()
    got = _post("/inferences", body, root=root)
    task = got.get("task_id")
    if not task:
        raise RetroDiffusionError(f"job not accepted: {got}")

    def step() -> Optional[dict]:
        status = _get(f"/inferences/tasks/{task}", root=root)
        if status.get("status") in ("succeeded", "failed"):
            return status
        return None

    try:
        status = _http.poll(step, first=POLL_SECONDS, max_wait=timeout,
                            factor=1.0, ceiling=POLL_SECONDS,
                            provider="retrodiffusion", label=f"job {task}")
    except RetroDiffusionError:
        raise
    except _http.ProviderError as exc:
        raise RetroDiffusionError(
            f"animation job {task} did not succeed: "
            f"{{'detail': 'still running after {timeout:.0f}s'}}",
            provider="retrodiffusion") from exc
    if status.get("status") != "succeeded":
        raise RetroDiffusionError(
            f"animation job {task} did not succeed: {status.get('error')}",
            provider="retrodiffusion",
            billing=_http.is_billing(0, str(status.get("error"))))
    result = status.get("result") or {}
    images = result.get("base64_images") or []
    if not images:
        raise RetroDiffusionError("job succeeded but returned no images")
    dest = Path(out_path) if out_path else src.with_name(
        f"{src.stem}_{action}_{frames}.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(images[0]))
    return _result.shape({
        "ok": True, "path": str(dest), "sheet_b64": images[0],
        "usd": result.get("balance_cost"),
        "provider": "retrodiffusion",
        "model": body.get("prompt_style", ""),
        "seconds": round(time.monotonic() - started, 2),
        "balance": result.get("remaining_balance"),
        "frames": frames, "size": [int(size[0]), int(size[1])]})


def submit(start_frame: str | os.PathLike[str], action: str, *,
           frames: int = 8, size: Optional[tuple[int, int]] = None,
           prompt: str = "", palette: Optional[str | os.PathLike[str]] = None,
           bg: tuple[int, int, int] = (255, 255, 255),
           root: Any = None) -> dict:
    """POST the job and return its task id. DOES NOT WAIT.

    The blocking half of animate() is what made this pipeline fragile: a
    tool that holds an MCP call open for minutes is killed by the client's
    idle ceiling, orphaned by any server restart, and billed either way -
    RD charges when the job runs, so a call that dies after submitting pays
    full price and throws the result away. Submitting and collecting
    separately means no call is ever long enough to be killed.

    Returns {ok, task_id}. Pair with collect().
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
    return {"ok": True, "task_id": task,
            "size": [int(size[0]), int(size[1])], "frames": frames}


def collect(task_id: str, *, root: Any = None) -> dict:
    """Read a submitted job ONCE. Never blocks.

    Returns {ok: True, ...} when the sheet is ready, {ok: False,
    pending: True} while it is still running, and raises only when the job
    genuinely failed. The caller decides how long to keep asking, which is
    the point - patience belongs to the agent, not to a socket.
    """
    status = _get(f"/inferences/tasks/{task_id}", root=root)
    state = status.get("status")
    if state not in ("succeeded", "failed"):
        return {"ok": False, "pending": True, "status": state,
                "task_id": task_id}
    if state == "failed":
        raise RetroDiffusionError(
            f"animation job {task_id} failed: {status.get('error')}",
            provider="retrodiffusion",
            billing=_http.is_billing(0, str(status.get("error"))))
    result = status.get("result") or {}
    images = result.get("base64_images") or []
    if not images:
        raise RetroDiffusionError(
            f"job {task_id} succeeded but returned no images")
    return {"ok": True, "pending": False, "sheet_b64": images[0],
            "usd": result.get("balance_cost"),
            "balance": result.get("remaining_balance"),
            "task_id": task_id}


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


#: THE UI STYLES. Distinct from :data:`TILE_STYLES` because they are a
#: different model family with a different size contract, and because a UI
#: element is not a tile: a nine-slice panel, a socket frame and an item icon
#: all want to be drawn as INTERFACE, flat and readable at 1x, not as a piece
#: of a world seen in perspective.
#:
#: `px` is the size range the style accepts. rd_pro's two are 256-only, which
#: is the vendor's constraint and not a choice made here.
UI_STYLES = {
    "ui": {"style": "rd_fast__ui", "usd": 0.01, "px": (64, 384)},
    "ui_element": {"style": "rd_plus__ui_element", "usd": 0.02, "px": (64, 384)},
    "item_sheet": {"style": "rd_plus__item_sheet", "usd": 0.02, "px": (64, 384)},
    "ui_panel": {"style": "rd_pro__ui_panel", "usd": 0.04, "px": (256, 256)},
    "inventory_items": {"style": "rd_pro__inventory_items", "usd": 0.04,
                        "px": (256, 256)},
}


def ui(prompt: str, *, kind: str = "ui_element", px: int = 128,
       height: Optional[int] = None, count: int = 1,
       palette: Optional[str] = None, input_image: Optional[str] = None,
       strength: Optional[float] = None, root: Any = None,
       timeout: float = 180.0) -> dict:
    """One or more UI elements from RD's interface styles.

    Returns ``{ok, png_b64_list, usd, balance, kind, size}``.

    WHY THIS EXISTS AND WHY IT IS NOT `tileset`. This adapter shipped with
    animation and tile styles only, and the project's standing rule was that RD
    ANIMATES and never generates. The director lifted that for interface art
    specifically: RD is the only provider here trained on pixel UI, and a
    button drawn by a general image model is a picture of a button.

    ``prompt`` describes the ELEMENT ("a bevelled slot frame for an ability
    card, empty centre"), never the rendering - the style carries the pixel
    art, the same contract `animate` and `tileset` follow. Saying "pixel art"
    in the prompt makes the output worse, per the vendor's own reference.

    Synchronous, like the tile calls.
    """
    spec = UI_STYLES.get(kind)
    if spec is None:
        raise RetroDiffusionError(
            f"unknown ui kind {kind!r} - one of {sorted(UI_STYLES)}")
    lo, hi = spec["px"]
    h = int(height if height is not None else px)
    for name, val in (("width", int(px)), ("height", h)):
        if not (lo <= val <= hi):
            raise RetroDiffusionError(
                f"{kind} draws {lo}-{hi}px, not {val} ({name})")
    body = {"prompt": prompt, "prompt_style": spec["style"],
            "width": int(px), "height": h, "num_images": max(1, int(count))}
    if palette:
        body["input_palette"] = _b64_rgb(palette, (0, 0, 0))
    # THE CONCEPT ART, AS THE ACTUAL REFERENCE. Prompting a UI style blind gets
    # you a plausible button; handing it the project's own pinned concept gets
    # you THIS project's button. `.bgate/refs/` is full of them and generating
    # without one is leaving the art direction on the table.
    if input_image:
        body["input_image"] = _b64_rgb(input_image, (255, 255, 255))
        if strength is not None:
            body["strength"] = float(strength)
    got = _post("/inferences", body, root=root, timeout=timeout)
    images = got.get("base64_images") or []
    if not images:
        raise RetroDiffusionError("ui call returned no images")
    return {"ok": True, "png_b64_list": images,
            "usd": got.get("balance_cost"),
            "balance": got.get("remaining_balance"),
            "kind": kind, "size": (int(px), h)}


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
