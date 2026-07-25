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
           creativity: str = "", root: Any = None,
           timeout: float = 60.0) -> dict:
    """Start a generation. Returns the job envelope with `job_id`."""
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
        payload[field] = [
            {"url": r["url"],
             "strength": max(lo, min(hi, float(r.get("strength", default))))}
            for r in usable[:cap]
        ]
        dropped = len(usable) - cap
        if dropped > 0:
            payload["_dropped_refs"] = dropped  # stripped below; caller-visible
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
             timeout: float = 300.0, root: Any = None) -> dict:
    """Submit, wait, download. The whole three-step dance as one call.

    Shaped to match imagegen.generate's return so the art pipeline does not care
    which provider produced the file: {ok, path, bytes, seconds, estimated_usd}.
    """
    started = time.monotonic()
    try:
        job = submit(prompt, model=model, size=size, seed=seed,
                     style_refs=style_refs, image_url=image_url,
                     strength=strength, creativity=creativity, root=root)
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
    return {
        "ok": True, "path": str(out_path), "bytes": written,
        "provider": "krea", "model": model,
        "job_id": str(job_id), "url": urls[0],
        "seconds": round(time.monotonic() - started, 2),
        "estimated_usd": price_for(model, style_refs=len(style_refs or [])),
    }
