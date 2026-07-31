"""Krea image generation — a second provider beside gpt-image.

Why bother having two: Krea is a front door to twenty-odd models (Flux, Imagen,
Nano Banana, its own Krea-2) rather than one, so the interesting question stops
being "is this model good" and becomes "which model is right for this asset".
Keeping OpenAI alongside it is the only way to answer that honestly.

The two APIs disagree on nearly everything, and this module owns the disagreement:

  * gpt-image returns the bytes on the response. Krea returns a JOB — you POST,
    get a `job_id`, poll `GET /jobs/<id>` until it reaches a terminal state, then
    download `result.urls[0]` yourself. Three round trips, not one.
  * gpt-image conditions on reference images by EDITING them. Krea takes
    `image_style_references: [{url, strength}]` as first-class input, which is
    what the art seat's pinned anchors actually are. That is the better fit and
    the reason this is worth wiring up.
  * gpt-image prices by quality tier. Krea prices per model, and the price
    CHANGES with the request: krea-2-large is $0.06 plain, $0.065 once you attach
    style references, $0.07 with a moodboard. So the estimate has to read the
    request, not just the model name.

NO MODEL HERE RETURNS ALPHA. Not one of them takes a transparency parameter —
check `supports` below, there is nothing to pass. So a Krea sprite is only
possible one way: generate on a flat chroma backdrop and key it out afterwards
(bgate_core.chroma). That is not a Krea workaround, it is the whole pipeline's
contract — gpt-image's own background="transparent" was measured returning a
brown gradient — but for Krea it is the ONLY path to a usable sprite, so any
caller here doing sprite/sheet/gear work must go through chroma.generate rather
than calling this module directly.

Everything here is stdlib — no SDK. One less dependency to pin, and the surface
we use is four HTTP calls wide.
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

API_BASE = "https://api.krea.ai"

# Terminal states from the job schema. `intermediate-complete` is deliberately
# NOT terminal: it means a preview frame exists while sampling continues, and
# taking it would ship a half-cooked image.
DONE = "completed"
DEAD = {"failed", "cancelled"}
RUNNING = {"backlogged", "queued", "scheduled", "processing", "sampling",
           "intermediate-complete"}

# model key -> {path, usd, aspect ratios, notes}. The key is what a caller
# passes as `model=`; the path is where Krea actually serves it.
#
# `sizing` is the one that bites: krea-2 wants an aspect ratio and a resolution
# tier, flux and imagen want pixel width/height, and sending the wrong pair is a
# 422 with "Unrecognized keys" rather than a polite default. There is no shared
# payload shape here — each model gets built for what it actually accepts.
#
# `ref_range` differs too: krea-2 clamps style-reference strength to 0..1, flux
# to -2..2 (negative pushes AWAY from the reference).
#
# Prices are per REQUEST and come from each model's own API reference, not the
# features page — that page lists flux at $0.04 and the reference says $0.007.
MODELS: dict[str, dict] = {
    "krea-2-large": {
        "path": "/generate/image/krea/krea-2/large",
        "usd": 0.06,
        "usd_with_style_refs": 0.065,
        "usd_with_moodboard": 0.07,
        "sizing": "aspect",
        "style_refs": True,
        "ref_range": (0.0, 1.0),
        "supports": {"seed", "creativity", "intensity", "complexity", "movement",
                     "styles", "moodboards", "image_url", "strength"},
        "note": "Krea's own model. Takes style references natively — the best "
                "fit for anchored character work.",
    },
    "flux-1-dev": {
        "path": "/generate/image/bfl/flux-1-dev",
        "usd": 0.007,
        "sizing": "pixels",
        "style_refs": True,
        "ref_range": (-2.0, 2.0),
        "supports": {"seed", "steps", "guidance_scale", "styles", "style_images",
                     "image_url", "strength"},
        "note": "Cheapest by an order of magnitude. Good for concept sweeps "
                "where identity does not have to hold.",
    },
    "imagen-4": {
        "path": "/generate/image/google/imagen-4",
        "usd": 0.042,
        "sizing": "pixels",
        "style_refs": False,
        "supports": {"seed"},
        "note": "Strong prompt adherence, prompt-only — no reference "
                "conditioning, so not for anchored work.",
    },
    "krea-2-medium": {
        "path": "/generate/image/krea/krea-2/medium",
        "usd": 0.03,
        "usd_with_style_refs": 0.03,
        "sizing": "aspect",
        "style_refs": True,
        "ref_range": (0.0, 1.0),
        "supports": {"seed", "creativity", "intensity", "complexity", "movement",
                     "styles", "moodboards", "image_url", "strength"},
        "note": "Half the price of large and takes the same 10 style "
                "references — the workhorse for anchored work.",
    },
    "imagen-4-fast": {
        "path": "/generate/image/google/imagen-4-fast",
        "usd": 0.021,
        "sizing": "pixels",
        "style_refs": False,
        "supports": {"seed"},
        "note": "Cheap and prompt-only. Fine for UI plates, useless for "
                "anything that must stay on-model.",
    },
    "z-image": {
        "path": "/generate/image/z-image/z-image",
        "usd": 0.003,
        "sizing": "aspect",
        "style_refs": True,
        # Its own field name, its own cap, its own range. Nothing about the
        # reference contract is shared across vendors here.
        "ref_field": "style_images",
        "ref_max": 1,
        "ref_range": (-2.0, 2.0),
        "aspects": ("1:1", "4:3", "2:3", "16:9", "9:16"),
        "supports": {"seed", "styles", "image_url", "skip_prompt_expansion"},
        "note": "Cheapest by 20x and the fastest. Realistic, low diversity — "
                "a sweep model, not a finishing one.",
    },

    # ---- reference EDIT models -------------------------------------------
    # The two above condition on a reference as STYLE: they follow a look but
    # owe nothing to a pose. Measured on the party idles, krea-2-medium drew a
    # face in seven of eight frames when four of them were specified as back
    # views. The models below EDIT a supplied image instead, which is what
    # "same character, now facing away" actually needs.
    "gpt-image": {
        # gpt-image through Krea, so it bills the Krea key rather than a
        # separate OpenAI one. Same model that holds character identity best in
        # the reference-first chain.
        "path": "/generate/image/openai/gpt-image",
        "usd": 0.03,
        "sizing": "pixels",
        "style_refs": True,
        # A PLAIN array of urls, not {url, strength} objects — the only model
        # here that does it that way, hence `ref_plain`.
        "ref_field": "image_urls",
        "ref_plain": True,
        "ref_max": 15,
        "supports": {"quality", "styles", "style_images"},
        "note": "Edits the reference rather than styling from it: the best "
                "identity hold for anchored pose work, and cheaper here than "
                "calling OpenAI directly.",
    },
    "nano-banana-pro": {
        "path": "/generate/image/google/nano-banana-pro",
        "usd": 0.15,
        "sizing": "pixels",
        "style_refs": True,
        # Takes BOTH: image_urls (plain, edit-style) and style_images
        # (weighted). The docs are explicit that image_urls WINS and
        # style_images is ignored when both are sent, so only one is used.
        "ref_field": "image_urls",
        "ref_plain": True,
        "supports": {"styles", "style_images", "resolution"},
        "note": "Dearest here by 2x. Image prompts override style images.",
    },
    "nano-banana-2": {
        "path": "/generate/image/google/nano-banana-2",
        "usd": 0.06,
        "sizing": "pixels",
        "style_refs": True,
        "ref_field": "image_urls",
        "ref_plain": True,
        "supports": {"styles", "style_images", "resolution"},
        "note": "A quarter of Pro's price with the same reference contract.",
    },
    "seedream-5-lite": {
        "path": "/generate/image/bytedance/seedream-5-lite",
        "usd": 0.04,
        "sizing": "pixels",
        "style_refs": True,
        "ref_field": "style_images",
        "ref_max": 14,
        "ref_range": (-2.0, 2.0),
        "supports": {"seed", "style_images"},
        "note": "Weighted style refs only — styling, not editing.",
    },
    "ideogram-3": {
        "path": "/generate/image/ideogram/ideogram-3",
        # $0.063 plain, $0.1575 once character references are attached — the
        # only model here that charges 2.5x for the thing we actually want.
        "usd": 0.063,
        "usd_with_style_refs": 0.1575,
        "sizing": "pixels",
        "style_refs": True,
        # The ONLY model in this catalogue with a field meaning "keep THIS
        # character", as opposed to "borrow this look". Worth testing against
        # the party idles for exactly that reason.
        "ref_field": "character_reference_images",
        "ref_plain": True,
        "supports": {"seed", "style_images", "character_reference_images"},
        "note": "Dedicated character-reference field; priced 2.5x when used.",
    },
    "flux-1.1-pro": {
        "path": "/generate/image/bfl/flux-1.1-pro",
        "usd": 0.06,
        "sizing": "pixels",
        "style_refs": False,
        # 256..1440 per side, tighter than everything else here.
        "supports": {"seed"},
        "note": "Prompt-only, and capped at 1440px a side. No anchoring.",
    },
    "imagen-4-ultra": {
        "path": "/generate/image/google/imagen-4-ultra",
        "usd": 0.063,
        "sizing": "pixels",
        "style_refs": False,
        "supports": {"seed"},
        "note": "Prompt-only. Imagen-4's finish tier, still no references.",
    },
    "flux-kontext": {
        "path": "/generate/image/bfl/flux-1-kontext-dev",
        "usd": 0.04,
        "sizing": "pixels",
        "style_refs": True,
        "ref_field": "style_images",
        "ref_range": (-2.0, 2.0),
        "supports": {"seed", "steps", "guidance_scale", "image_url", "strength"},
        "note": "Kontext is an EDIT model — pass the frame to change as "
                "image_url with a low strength to keep the character.",
    },
}
DEFAULT_MODEL = "krea-2-large"

# Krea takes an aspect ratio, not pixels. The rest of this codebase speaks
# WxH, so translate rather than making every call site learn a second vocabulary.
ASPECTS = ("1:1", "4:3", "3:2", "16:9", "2.35:1", "4:5", "2:3", "9:16")
CREATIVITY = ("raw", "low", "medium", "high")


class KreaError(RuntimeError):
    """A Krea call failed in a way the caller should surface, not retry blindly."""


def api_key(root: Any = None) -> str:
    """The token, from the project's .env or the environment. Never logged."""
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:
            pass
    return (os.environ.get("KREA_API_KEY") or "").strip()


def available(root: Any = None) -> dict:
    """Can we call Krea at all? Presence only — a health check that spends money
    is one nobody runs."""
    key = api_key(root)
    if not key:
        return {"available": False,
                "reason": "KREA_API_KEY not set — put it in the project's .env "
                          "(gitignored, loaded per project) or the environment; "
                          "create a token at krea.ai/settings/api-tokens"}
    return {"available": True, "models": sorted(MODELS), "default": DEFAULT_MODEL}


def aspect_for(size: str, allowed: tuple[str, ...] = ASPECTS) -> str:
    """WxH -> the nearest aspect this model accepts.

    Model-specific because the lists differ: krea-2 takes 2.35:1 and 4:5,
    z-image does not, and sending one it does not know is a 422.
    """
    try:
        w, h = (int(v) for v in str(size).lower().split("x"))
    except Exception:
        return "1:1"
    if not w or not h:
        return "1:1"
    target = w / h
    best, gap = "1:1", 1e9
    for a in allowed:
        try:
            aw, ah = (float(v) for v in a.split(":"))
        except ValueError:
            continue
        d = abs(target - aw / ah)
        if d < gap:
            best, gap = a, d
    return best


def data_uri(path: str | os.PathLike) -> str:
    """A local file as a base64 data URI.

    Krea has no upload endpoint — a reference image travels inline, either as a
    public https URL or as one of these. Every pinned anchor in this tool is a
    local file, so this is the only way an anchor reaches the model.
    """
    p = Path(path)
    if not p.is_file():
        raise KreaError(f"reference image not found: {p}")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(p.suffix.lower())
    if not mime:
        raise KreaError(f"unsupported reference type {p.suffix!r} — png/jpg/webp only")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def style_ref(path: str | os.PathLike, strength: float = 0.5) -> dict:
    """A pinned anchor shaped the way the style-reference array wants it."""
    return {"url": data_uri(path), "strength": strength}


def refs_from_paths(paths, strength: float = 0.5) -> list[dict]:
    """Local anchor FILES -> the style-reference array. The whole bridge.

    Every pinned ref in this tool is a path on disk and every model here wants
    a url, so a caller that has anchors and wants them conditioned on needs
    exactly this and nothing else. A missing or wrong-typed file raises
    KreaError HERE, before any money moves, rather than surfacing as a 422
    three round trips later.
    """
    return [style_ref(p, strength) for p in (paths or []) if str(p).strip()]


def supports_style_refs(model: str = DEFAULT_MODEL) -> bool:
    """Can this model condition on reference images at all?

    Worth asking BEFORE quoting a price: imagen and flux-1.1-pro are
    prompt-only, so "generate conditioned on the pinned refs" is not a slower
    or dearer version of the request on those — it is not the request.
    """
    return bool((MODELS.get(model) or {}).get("style_refs"))


def pixels_for(size: str, *, lo: int = 512, hi: int = 2368) -> tuple[int, int]:
    """WxH clamped to what the pixel-sized models accept."""
    try:
        w, h = (int(v) for v in str(size).lower().split("x"))
    except Exception:
        return 1024, 1024
    clamp = lambda v: max(lo, min(hi, int(v)))
    return clamp(w), clamp(h)


def price_for(model: str = DEFAULT_MODEL, *, style_refs: int = 0,
              moodboard: bool = False) -> float:
    """What this REQUEST costs, not just this model.

    Krea's price moves with the payload, so an estimate that reads only the
    model name under-quotes every anchored generation the art seat makes.
    """
    spec = MODELS.get(model) or MODELS[DEFAULT_MODEL]
    if moodboard and spec.get("usd_with_moodboard"):
        return float(spec["usd_with_moodboard"])
    if style_refs and spec.get("usd_with_style_refs"):
        return float(spec["usd_with_style_refs"])
    return float(spec["usd"])


def _request(path: str, key: str, *, payload: Optional[dict] = None,
             method: str = "GET", timeout: float = 60.0) -> dict:
    url = path if path.startswith("http") else API_BASE + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        # The three a caller will actually hit, each with the fix rather than
        # the status code. 402 is the surprising one: a Krea SUBSCRIPTION does
        # not pay for API calls — the API balance is topped up separately, and
        # the raw message does not say where to go.
        if exc.code in (401, 403):
            raise KreaError(
                "Krea rejected the API key (HTTP %s) — check KREA_API_KEY in the "
                "project's .env; tokens come from krea.ai/settings/api-tokens"
                % exc.code) from exc
        if exc.code == 402:
            raise KreaError(
                "Krea has no API credit — the API balance is billed separately "
                "from a workspace/subscription plan. Top it up at "
                "krea.ai/settings (billing), then retry; nothing was charged.") from exc
        if exc.code == 429:
            raise KreaError(
                "Krea rate-limited this request (HTTP 429) — slow the fan-out or "
                "retry in a moment.") from exc
        if exc.code == 422:
            raise KreaError(
                f"Krea refused the request shape for this model: {detail} "
                "(each model has its own parameter schema — see MODELS)") from exc
        raise KreaError(f"Krea HTTP {exc.code} on {method} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise KreaError(f"could not reach Krea ({exc.reason})") from exc


def submit(prompt: str, *, model: str = DEFAULT_MODEL, size: str = "1024x1024",
           seed: Optional[int] = None, style_refs: Optional[list[dict]] = None,
           image_url: str = "", strength: Optional[float] = None,
           creativity: str = "", quality: str = "",
           styles: Optional[list[dict]] = None, root: Any = None,
           timeout: float = 60.0) -> dict:
    """Start a generation. Returns the job envelope with `job_id`.

    ``styles`` is trained LoRAs — [{id, strength}], from style() — and is a
    DIFFERENT axis from ``style_refs``: a reference is an image sent with this
    request, a style is a model that already learned the look. Sending both is
    the intended combination, not a conflict: the LoRA carries the style so the
    reference slot is free to carry identity.
    """
    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])
    spec = MODELS.get(model)
    if spec is None:
        raise KreaError(f"unknown model {model!r} — known: {sorted(MODELS)}")

    payload: dict[str, Any] = {"prompt": prompt}

    # Sizing is per model — sending aspect_ratio to flux is a 422, not a default.
    if spec["sizing"] == "aspect":
        payload["aspect_ratio"] = aspect_for(size, spec.get("aspects", ASPECTS))
        payload["resolution"] = "1K"
    else:
        w, h = pixels_for(size)
        payload["width"], payload["height"] = w, h

    if seed is not None and "seed" in spec["supports"]:
        payload["seed"] = int(seed)
    if quality and "quality" in spec["supports"]:
        payload["quality"] = quality
    if creativity:
        if "creativity" not in spec["supports"]:
            raise KreaError(f"{model} has no creativity control — that is krea-2 only")
        if creativity not in CREATIVITY:
            raise KreaError(f"creativity must be one of {CREATIVITY}")
        payload["creativity"] = creativity

    if style_refs:
        if not spec.get("style_refs"):
            raise KreaError(
                f"{model} does not take style references — use krea-2-large or "
                "flux-1-dev, which condition on them natively")
        lo, hi = spec.get("ref_range", (0.0, 1.0))
        default = 0.5 if hi <= 1.0 else 1.0
        # Field name, cap and range are all per model — z-image takes ONE
        # reference under `style_images`, krea-2 takes ten under
        # `image_style_references`. Silently truncating is better than a 422,
        # but the caller is told which ones were dropped.
        field = spec.get("ref_field", "image_style_references")
        cap = int(spec.get("ref_max", 10))
        usable = [r for r in style_refs if r.get("url")]
        if spec.get("ref_plain"):
            # gpt-image takes bare urls under `image_urls` and has no per-image
            # strength: they are edit inputs, not weighted style hints. Sending
            # {url, strength} objects here is a 422.
            payload[field] = [r["url"] for r in usable[:cap]]
        else:
            payload[field] = [
                {"url": r["url"],
                 "strength": max(lo, min(hi, float(r.get("strength", default))))}
                for r in usable[:cap]
            ]
        dropped = len(usable) - cap
        if dropped > 0:
            payload["_dropped_refs"] = dropped  # stripped below; caller-visible
    if styles:
        if "styles" not in spec["supports"]:
            raise KreaError(
                f"{model} cannot use a trained style — the models that can are "
                + ", ".join(sorted(k for k, v in MODELS.items()
                                   if "styles" in v["supports"])))
        payload["styles"] = [
            {"id": str(s["id"]), "strength": max(0.0, min(1.0, float(s.get("strength", 0.85))))}
            for s in styles if s.get("id")
        ]
    if image_url:
        if "image_url" not in spec["supports"]:
            raise KreaError(f"{model} cannot take a source image (img2img)")
        payload["image_url"] = image_url
        if strength is not None:
            payload["strength"] = max(0.0, min(1.0, float(strength)))

    dropped = payload.pop("_dropped_refs", 0)
    ref_field = spec.get("ref_field", "image_style_references")
    job = _request(spec["path"], key, payload=payload, method="POST", timeout=timeout)
    job["_model"] = model
    job["_estimated_usd"] = price_for(
        model, style_refs=len(payload.get(ref_field) or []))
    if dropped:
        job["_warning"] = (f"{model} accepts {spec.get('ref_max', 10)} style "
                           f"reference(s); {dropped} were dropped")
    return job


# ---------------------------------------------------------------------------
# Trained styles (LoRA). The other half of "stay on model".
#
# WHY THIS EXISTS, in the words of the art seat's own rule: "A style reference
# and an identity reference cannot share a weight. At equal strength the style
# ref transfers the SUBJECT and the whole cast comes back as one person." That is
# the ceiling on reference-conditioned work — one slot, two jobs. Training the
# STYLE into a model empties the slot: style comes from the LoRA, and the
# reference is free to carry identity alone.
#
# Three calls, and the first one is new: /assets takes multipart and hands back a
# hosted URL, /styles/train takes those URLs, and /jobs/<id> is the same poll
# generation already uses. The data URIs the rest of this module sends are NOT
# accepted here — training wants URLs.
# ---------------------------------------------------------------------------
STYLE_TYPES = ("Style", "Character", "Object", "Default")
TRAIN_MODELS = ("flux_dev",)

# The floor Krea documents, and the reason most of a pixel-art project's pinned
# anchors cannot be training data as they stand: measured on a real board, 6 of
# 27 pins cleared it. A nearest-neighbour upscale of a 64x32 tile is not a
# 1024px training image, so this refuses rather than quietly sending mush.
TRAIN_MIN_SIDE = 1024
# HOW FAR SHORT IS STILL FIXABLE, as a fraction of the floor.
#
# The first cut of this refused anything under 1024 outright, and that was a
# tool being pedantic rather than careful: a concept plate at 1602x981 is 43
# pixels short, and the difference between it and a legal one is a 4% resize
# nobody can see. Measured on a real board, that rule threw away FOURTEEN of the
# project's best plates for being 83px short, and the human's reaction was the
# correct one.
#
# So: anything down to this fraction is upscaled to reach the floor and used,
# with the resize declared per image. Below it the image is genuinely too small
# — a 48px item icon carries no style at 1024 and an upscale there is inventing
# detail, which is the thing the original rule was actually right about.
# 0.6 caps the enlargement at 1.67x.
TRAIN_NEAR_FLOOR = 0.6
TRAIN_MIN_IMAGES = 5          # Krea's hard minimum
TRAIN_GOOD_IMAGES = 10        # below this it trains, but coverage is thin
TRAIN_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# Krea publishes no price for a training run. NOT guessed at: an under-quote is
# worse than a missing quote, because the spend ceiling would let it through.
# Callers that enforce a budget must treat this as unknown and ask the human.
TRAIN_USD: Optional[float] = None


def _multipart(fields: dict, filename: str, blob: bytes,
               field: str = "file") -> tuple[bytes, str]:
    """A multipart/form-data body, stdlib only.

    /assets is the one endpoint here that is not JSON, and pulling in requests
    for one call would put a dependency on the critical path of a tool whose
    whole HTTP surface is otherwise four urllib calls wide.
    """
    boundary = "----bgate" + base64.urlsafe_b64encode(os.urandom(9)).decode()
    out = bytearray()
    for name, value in (fields or {}).items():
        if value is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(Path(filename).suffix.lower(),
                                       "application/octet-stream")
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n').encode()
    out += f"Content-Type: {mime}\r\n\r\n".encode()
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def prepare_training_image(path: str | os.PathLike, *,
                           floor: int = TRAIN_MIN_SIDE) -> tuple:
    """The image as it should be UPLOADED, plus what was done to it.

    Returns ``(path, note)``. A file already over the floor comes back
    untouched and with an empty note; one under it is resized — aspect kept,
    LANCZOS — into a temp file, and the note says by how much.

    Separated from :func:`check_training_set` because judging a dataset must not
    write files: the panel calls that on every poll, and a validator with a side
    effect is one nobody can call twice.
    """
    src = Path(path)
    try:
        from PIL import Image
    except ImportError:
        return str(src), ""
    try:
        with Image.open(src) as im:
            w, h = im.size
            short = min(w, h)
            if short >= floor:
                return str(src), ""
            scale = floor / short
            size = (round(w * scale), round(h * scale))
            import tempfile

            out = Path(tempfile.gettempdir()) / f"bgate-train-{src.stem}-{size[0]}x{size[1]}.png"
            im.convert("RGB").resize(size, Image.LANCZOS).save(out)
    except Exception as exc:                                   # noqa: BLE001
        # An unreadable image is the caller's problem at check time; here it
        # just goes up as-is and Krea decides.
        return str(src), f"could not resize ({type(exc).__name__}: {exc})"
    return str(out), f"upscaled {w}x{h} -> {size[0]}x{size[1]} ({scale:.2f}x)"


def upload(path: str | os.PathLike, *, description: str = "",
           root: Any = None, timeout: float = 180.0) -> dict:
    """Put one local image on Krea's asset store. Returns {id, image_url}.

    Training takes URLs, and every anchor in this tool is a local file, so this
    is the bridge. Uploading is not generating: nothing is charged and nothing is
    trained until /styles/train is called with the URLs this hands back.
    """
    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])
    p = Path(path)
    if not p.is_file():
        raise KreaError(f"no such image: {p}")
    if p.suffix.lower() not in TRAIN_SUFFIXES:
        raise KreaError(f"unsupported training image type {p.suffix!r} — "
                        f"{'/'.join(sorted(s.lstrip('.') for s in TRAIN_SUFFIXES))} only")
    body, content_type = _multipart({"description": description or p.stem},
                                    p.name, p.read_bytes())
    req = urllib.request.Request(API_BASE + "/assets", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": content_type,
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise KreaError(f"Krea rejected the upload of {p.name} "
                        f"(HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise KreaError(f"could not reach Krea to upload {p.name} ({exc.reason})") from exc
    url = got.get("image_url") or got.get("url")
    if not url:
        raise KreaError(f"Krea accepted {p.name} but returned no image_url: "
                        f"{str(got)[:200]}")
    return {"id": got.get("id") or "", "image_url": url, "path": str(p)}


def check_training_set(paths: list) -> dict:
    """Judge a dataset BEFORE anything is uploaded or charged.

    Every check here is one Krea documents, and the point is to fail on this side
    of the network: a training run is 5-15 minutes and an unknown amount of money,
    and finding out afterwards that one image was 297px wide costs both.

    Returns {ok, usable, rejected: [{path, why}], warnings}. Pillow is optional —
    without it the resolution floor cannot be checked, and that is REPORTED
    rather than assumed to pass.
    """
    try:
        from PIL import Image            # noqa: PLC0415 — optional dependency
    except ImportError:
        Image = None                     # type: ignore[assignment]

    usable: list[str] = []
    upscaled: list[dict] = []
    rejected: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for raw in paths or []:
        p = Path(raw)
        keyed = str(p.resolve()).lower() if p.exists() else str(p).lower()
        if keyed in seen:
            rejected.append({"path": str(p), "why": "the same image twice"})
            continue
        seen.add(keyed)
        if not p.is_file():
            rejected.append({"path": str(p), "why": "not a file"})
            continue
        if p.suffix.lower() not in TRAIN_SUFFIXES:
            rejected.append({"path": str(p), "why": f"{p.suffix} is not png/jpg/webp"})
            continue
        if Image is None:
            usable.append(str(p))
            continue
        try:
            with Image.open(p) as img:
                w, h = img.size
        except Exception as exc:
            rejected.append({"path": str(p), "why": f"unreadable ({exc})"})
            continue
        short = min(w, h)
        if short < int(TRAIN_MIN_SIDE * TRAIN_NEAR_FLOOR):
            rejected.append({
                "path": str(p),
                "why": f"{w}x{h} — too small to reach {TRAIN_MIN_SIDE}px "
                       "without inventing detail. Upscaling this far trains "
                       "the upscaler's artefacts, not the art"})
            continue
        if short < TRAIN_MIN_SIDE:
            # Usable, and honest about what will happen to it. The resize is
            # applied at upload (prepare_training_image), not here: judging a
            # dataset must not write files.
            upscaled.append({"path": str(p), "size": [w, h],
                             "scale": round(TRAIN_MIN_SIDE / short, 3),
                             "to": [round(w * TRAIN_MIN_SIDE / short),
                                    round(h * TRAIN_MIN_SIDE / short)]})
        usable.append(str(p))

    if Image is None:
        warnings.append("Pillow is not installed, so image sizes were NOT "
                        "checked — Krea will refuse anything under "
                        f"{TRAIN_MIN_SIDE}px on the short side")
    if len(usable) < TRAIN_GOOD_IMAGES and len(usable) >= TRAIN_MIN_IMAGES:
        warnings.append(f"{len(usable)} images is above Krea's minimum but below "
                        f"{TRAIN_GOOD_IMAGES}; coverage will be thin and the LoRA "
                        "will over-fit what it did see")
    if upscaled:
        warnings.append(
            f"{len(upscaled)} image(s) are under {TRAIN_MIN_SIDE}px and will be "
            "upscaled to reach it — the largest is "
            f"{max(u['scale'] for u in upscaled):.2f}x")
    return {
        "ok": len(usable) >= TRAIN_MIN_IMAGES,
        "usable": usable, "upscaled": upscaled,
        "rejected": rejected, "warnings": warnings,
        "reason": "" if len(usable) >= TRAIN_MIN_IMAGES else (
            f"only {len(usable)} usable image(s); Krea needs at least "
            f"{TRAIN_MIN_IMAGES}"),
    }


def train_style(name: str, urls: list, *, model: str = "flux_dev",
                kind: str = "Style", trigger_word: str = "",
                max_train_steps: Optional[int] = None,
                learning_rate: Optional[float] = None,
                batch_size: Optional[int] = None,
                root: Any = None, timeout: float = 60.0) -> dict:
    """Start a LoRA training job. Returns the envelope with `job_id`.

    ``kind`` is Krea's `type` field, renamed because `type` shadows the builtin
    in every caller that would pass it as a keyword.
    """
    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])
    if not str(name).strip():
        raise KreaError("a trained style needs a name — it is how you find it again")
    urls = [str(u) for u in (urls or []) if str(u).strip()]
    if len(urls) < TRAIN_MIN_IMAGES:
        raise KreaError(f"{len(urls)} training image(s) — Krea needs at least "
                        f"{TRAIN_MIN_IMAGES}")
    if any(u.startswith("data:") for u in urls):
        # The trap this module invites: every other call here sends anchors as
        # data URIs, and /styles/train takes hosted URLs only.
        raise KreaError("training takes hosted URLs, not data URIs — upload each "
                        "image with upload() first and pass the image_url values")
    if kind not in STYLE_TYPES:
        raise KreaError(f"type must be one of {STYLE_TYPES}")
    if model not in TRAIN_MODELS:
        raise KreaError(f"unknown training base {model!r} — known: {TRAIN_MODELS}")

    payload: dict[str, Any] = {"name": str(name).strip(), "urls": urls,
                              "model": model, "type": kind}
    if trigger_word:
        payload["trigger_word"] = str(trigger_word).strip()
    if max_train_steps is not None:
        steps = int(max_train_steps)
        if not 1 <= steps <= 2000:
            raise KreaError("max_train_steps must be between 1 and 2000")
        payload["max_train_steps"] = steps
    if learning_rate is not None:
        rate = float(learning_rate)
        if not 0.0 < rate <= 0.01:
            raise KreaError("learning_rate outside anything sane — Krea "
                            "recommends 0.0001 to 0.001")
        payload["learning_rate"] = rate
    if batch_size is not None:
        payload["batch_size"] = int(batch_size)

    job = _request("/styles/train", key, payload=payload, method="POST",
                   timeout=timeout)
    job["_images"] = len(urls)
    job["_style_name"] = payload["name"]
    return job


def train(name: str, paths: list, *, kind: str = "Style", model: str = "flux_dev",
          trigger_word: str = "", max_train_steps: Optional[int] = None,
          learning_rate: Optional[float] = None, root: Any = None,
          timeout: float = 1800.0, wait: bool = True) -> dict:
    """Validate, upload, train, wait. The whole thing as one call.

    Order is deliberate: EVERY image is judged before ANY is uploaded, so a
    dataset with one 297px anchor in it fails for free instead of halfway through
    an upload loop with a half-formed set on Krea's side.

    `wait=False` returns after submitting — training is 5-15 minutes, which is
    longer than an interactive caller should block for.
    """
    started = time.monotonic()
    verdict = check_training_set(paths)
    if not verdict["ok"]:
        return {"ok": False, "error": verdict["reason"], "check": verdict,
                "seconds": round(time.monotonic() - started, 2)}
    try:
        uploaded = []
        resized = []
        for original in verdict["usable"]:
            ready, note = prepare_training_image(original)
            got = upload(ready, description=name, root=root)
            got["path"] = original          # the ANCHOR, not the temp file
            if note:
                got["note"] = note
                resized.append({"path": original, "note": note})
            uploaded.append(got)
        job = train_style(name, [u["image_url"] for u in uploaded], model=model,
                          kind=kind, trigger_word=trigger_word,
                          max_train_steps=max_train_steps,
                          learning_rate=learning_rate, root=root)
        job_id = str(job.get("job_id") or job.get("id") or "")
        if not job_id:
            raise KreaError(f"Krea did not return a training job id: {str(job)[:200]}")
        if not wait:
            return {"ok": True, "job_id": job_id, "style_id": "", "pending": True,
                    "images": len(uploaded), "check": verdict,
                    "seconds": round(time.monotonic() - started, 2)}
        done = poll(job_id, root=root, timeout=timeout, interval=10.0)
        style_id = str((done.get("result") or {}).get("style_id") or "")
        if not style_id:
            raise KreaError("training completed with no style_id: "
                            f"{str(done)[:200]}")
    except KreaError as exc:
        return {"ok": False, "error": str(exc), "check": verdict,
                "seconds": round(time.monotonic() - started, 2)}
    return {
        "ok": True, "style_id": style_id, "job_id": job_id, "name": name,
        "kind": kind, "model": model, "trigger_word": trigger_word,
        "images": len(uploaded), "sources": [u["path"] for u in uploaded],
        "resized": resized,
        "check": verdict, "pending": False,
        # Deliberately not a number: see TRAIN_USD.
        "estimated_usd": TRAIN_USD,
        "seconds": round(time.monotonic() - started, 2),
    }


def style(style_id: str, strength: float = 0.85) -> dict:
    """A trained style shaped the way the `styles` array wants it.

    0.85 because Krea recommends 0.8-0.9 as the starting point, and a default of
    1.0 is how a trained style stops being a style and starts being a stamp.
    """
    if not str(style_id).strip():
        raise KreaError("a trained style needs its style_id")
    return {"id": str(style_id).strip(),
            "strength": max(0.0, min(1.0, float(strength)))}


def poll(job_id: str, *, root: Any = None, timeout: float = 300.0,
         interval: float = 2.0) -> dict:
    """Wait for a job to reach a terminal state.

    Bounded on purpose: a job that never finishes must fail the caller rather
    than hold a seat's agent forever. The docs suggest 2s; we back off gently so
    a slow model does not turn into a poll storm.
    """
    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])
    deadline = time.monotonic() + max(5.0, float(timeout))
    wait = max(0.5, float(interval))
    last: dict = {}
    while time.monotonic() < deadline:
        last = _request(f"/jobs/{job_id}", key, timeout=30.0)
        status = str(last.get("status") or "")
        if status == DONE:
            return last
        if status in DEAD:
            err = last.get("error") or {}
            raise KreaError(
                f"Krea job {status}: {err.get('message') or err.get('code') or 'no reason given'}")
        if status and status not in RUNNING:
            # An unknown state is not an excuse to spin — say so and stop.
            raise KreaError(f"Krea job returned unknown status {status!r}")
        time.sleep(wait)
        wait = min(5.0, wait * 1.25)
    raise KreaError(
        f"Krea job {job_id} did not finish within {timeout:.0f}s (last status "
        f"{last.get('status') or 'unknown'})")


def download(url: str, out_path: str, *, timeout: float = 120.0) -> int:
    """Fetch the finished image to disk. Returns bytes written."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"Accept": "image/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        raise KreaError(f"could not download the finished image: {exc}") from exc
    if not data:
        raise KreaError("Krea returned an empty image")
    out.write_bytes(data)
    return len(data)


def generate(prompt: str, out_path: str, *, model: str = DEFAULT_MODEL,
             size: str = "1024x1024", seed: Optional[int] = None,
             style_refs: Optional[list[dict]] = None, image_url: str = "",
             strength: Optional[float] = None, creativity: str = "",
             quality: str = "", styles: Optional[list[dict]] = None,
             timeout: float = 300.0, root: Any = None,
             ref_paths=(), ref_strength: float = 0.5,
             task_kind: str = "", tileable: bool = False) -> dict:
    """Submit, wait, download. The whole three-step dance as one call.

    Shaped to match imagegen.generate's return so the art pipeline does not care
    which provider produced the file: {ok, path, bytes, seconds, estimated_usd}.

    ``ref_paths`` are LOCAL anchor files and are the ergonomic half of
    ``style_refs``: they are turned into the reference array here rather than
    every caller learning :func:`style_ref`. The two ADD — a caller already
    holding built refs keeps them, and paths are appended — so this is additive
    for anyone who was already passing ``style_refs``. ``estimated_usd`` counts
    the merged set, because Krea charges more for a request WITH references
    (krea-2-large is $0.06 plain and $0.065 anchored) and a quote that reads
    only the model name under-quotes every anchored generation the art seat
    makes.

    ``task_kind`` forces a texture kind square (see imagegen.size_for) and
    ``tileable`` runs the mirrored post-pass over the downloaded file. Both
    default off; neither changes anything for existing 2D work.
    """
    started = time.monotonic()
    from bgate_adapters.imagegen import make_tileable, size_for

    size = size_for(size, task_kind=task_kind)
    refs = list(style_refs or [])
    try:
        refs += refs_from_paths(ref_paths, ref_strength)
    except KreaError as exc:
        return {"ok": False, "error": str(exc), "provider": "krea",
                "model": model, "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": 0.0}
    try:
        job = submit(prompt, model=model, size=size, seed=seed,
                     style_refs=refs or None, image_url=image_url,
                     strength=strength, creativity=creativity,
                     quality=quality, styles=styles, root=root)
        job_id = job.get("job_id") or job.get("id")
        if not job_id:
            raise KreaError(f"Krea did not return a job id: {str(job)[:200]}")
        done = poll(str(job_id), root=root, timeout=timeout)
        urls = ((done.get("result") or {}).get("urls")) or []
        if not urls:
            raise KreaError("Krea reported completed with no image URL")
        written = download(urls[0], out_path)
    except KreaError as exc:
        return {"ok": False, "error": str(exc), "provider": "krea",
                "model": model, "seconds": round(time.monotonic() - started, 2),
                "estimated_usd": 0.0}
    result = {
        "ok": True, "path": str(out_path), "bytes": written,
        "provider": "krea", "model": model,
        "job_id": str(job_id), "url": urls[0],
        "seconds": round(time.monotonic() - started, 2),
        "estimated_usd": price_for(model, style_refs=len(refs)),
    }
    if tileable:
        # After the download, never instead of it: the image is already paid
        # for, so a post-pass that cannot run must degrade to a note.
        result["tileable"] = make_tileable(str(out_path))
    return result
