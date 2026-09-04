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
(bgate_core.art.chroma). That is not a Krea workaround, it is the whole pipeline's
contract — gpt-image's own background="transparent" was measured returning a
brown gradient — but for Krea it is the ONLY path to a usable sprite, so any
caller here doing sprite/sheet/gear work must go through chroma.generate rather
than calling this module directly.

KREA ALSO GENERATES 3D, and that is the only image-to-3D this product can reach
— see MODELS_3D and generate_3d at the bottom. It runs the same open-weight
models one would otherwise self-host (TRELLIS, TRELLIS 2, Hunyuan3D, Tripo)
behind the key we already load, so it needs no GPU, no CUDA toolchain and no
16 GB of weights. Two things make it unlike the image path and both are load
bearing: its per-generation price is NOT PUBLISHED anywhere, so a 3D call
cannot be quoted before it runs; and what comes back is geometry and texture
with NO RIG, so it is a draft that still owes the pipeline a clean, a scale,
an orientation and a skeleton before it is an asset.

Everything here is stdlib — no SDK. One less dependency to pin, and the surface
we use is four HTTP calls wide.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Optional

from bgate_adapters import imageto3d as _i3d
from bgate_core.store import envfile

from . import _http, _result

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
        "note": "A quarter of Pro's price with the same reference contract. "
                "PINNED for character animation on this provider — see "
                "CHARACTER_MODEL.",
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

# ---------------------------------------------------------------------------
# THE CHARACTER-ANIMATION PIN
# ---------------------------------------------------------------------------
# Sprite and character-animation work on this provider goes to nano-banana-2,
# not to the general default, and the reason is the split this table already
# draws between STYLE models and EDIT models a few entries up.
#
# krea-2-large and krea-2-medium condition on a reference as STYLE: they follow
# a look and owe nothing to a pose. That is the right contract for a plate or a
# concept sweep and the wrong one for "this exact character, now mid-stride" —
# the measured failure is recorded above this table's edit section: krea-2-medium
# drew a FACE in seven of eight frames when four of them were specified as back
# views. A style reference cannot be asked to hold a subject through a pose
# change, because holding the subject is not what it does.
#
# nano-banana-2 takes its references as `image_urls`: plain edit inputs, the same
# contract nano-banana-pro uses, at a quarter of Pro's price. It also keeps
# `styles`, so a project that trained a LoRA still has it ride alongside — which
# is the whole point of training one, per the note further down: the LoRA carries
# the style and the reference slot is freed to carry identity.
#
# It is not a cost decision but it is not a cost REGRESSION either: anchored work
# on krea-2-large bills usd_with_style_refs at $0.065, and this is a flat $0.06
# however many references ride with it.
CHARACTER_MODEL = "nano-banana-2"

# The task kinds that mean "a specific character, holding still through a change
# of pose". Deliberately narrow: an item, a prop, a decal or a VFX key frame has
# no pose continuity to preserve, so none of them needs an edit model and none of
# them should silently change provider behaviour because this constant exists.
#
# `sprite`, `sheet` and `portrait` WERE MISSING, and the gap is the whole reason
# this pin was not working. chroma.KEYED_KINDS has always treated them as
# character art, but they were absent here, so every sprite and every sheet fell
# through model_for() to DEFAULT_MODEL — the style model this comment block was
# written to route AWAY from. The measured failure above ("a FACE in seven of
# eight frames when four were specified as back views") was therefore not fixed
# by pinning nano-banana-2; it just stopped being visible on the two kinds that
# named it, and kept happening on the kind people actually generate most.
#
# Re-measured 2026-08-10 on a 16-frame NE/SE walk sheet from a pinned character,
# same prompt and reference through every reference-capable model on both
# providers: krea-2-large (the default this bypasses) FAILED the alpha audit at
# 14% hollow interior — the key colour landed inside the figure and was cut out
# of it — and drew six near-identical frames per row with no direction change.
# nano-banana-2 returned eight frames per row, correct back-view and front-view
# rows, and a clean key.
CHARACTER_KINDS = frozenset({
    "anchor",     # the canonical character every later frame derives from
    "animation",  # pose frames
    "sprite",     # a character sprite is a character, whatever it is called
    "sheet",      # a sheet is many poses of one identity — the hardest case
    "portrait",   # a face that has to stay the same face
    # AND THE 3D PLATE, which was missing for the same reason `sprite` was:
    # the name at the call site is not the name in this set. blender.character()
    # — the single call that decides what a whole 3D character looks like, and
    # the one conditioning on the humanoid pose template — passes
    # task_kind="character", so it fell through to the STYLE model and threw
    # away the pose reference it had just been handed. Measured cost on this
    # project: two rebuilds of one owner (items #7 and #66) off plates that
    # were never the same figure twice.
    "character",  # the conditioned 3D plate, from blender.character()
})

# The character kinds that are NOT on chroma.KEYED_KINDS, and must not be.
# Every other kind here asks the model for a flat chroma backdrop and lets the
# keyer audit it. The 3D plate deliberately does not: blender.character()
# generates FLAT and then keys with a SWEPT tolerance (blender._key_plate),
# because a mottled backdrop the sprite audit correctly refuses is still a
# usable plate once the tolerance is swept, and the sprite clause turns a
# character sheet into pixel art. Named rather than implied so the invariant
# ("a character kind the keyer does not know comes back opaque") keeps biting
# for every kind added after this one.
SELF_KEYED_KINDS = frozenset({"character"})


def model_for(task_kind: str = "") -> str:
    """The model this KIND of work should use when the caller named none.

    Lives here rather than at the call site because it is a fact about this
    catalogue — which models edit and which merely style — and a caller that had
    to know that would be reimplementing the table.
    """
    if str(task_kind or "").strip().lower() in CHARACTER_KINDS:
        return CHARACTER_MODEL
    return DEFAULT_MODEL

# Krea takes an aspect ratio, not pixels. The rest of this codebase speaks
# WxH, so translate rather than making every call site learn a second vocabulary.
ASPECTS = ("1:1", "4:3", "3:2", "16:9", "2.35:1", "4:5", "2:3", "9:16")
CREATIVITY = ("raw", "low", "medium", "high")


class KreaError(_http.ProviderError):
    """A Krea call failed in a way the caller should surface, not retry blindly."""


def api_key(root: Any = None) -> str:
    """The token, from the project's .env, ~/.bgate/.env, or the shell.
    Never logged.

    THE GLOBAL LAYER USED NOT TO BE READ HERE, and that was the actual
    cause of the provider disagreement three benchmark games reported:
    `bgate doctor` said the art providers were configured while the
    generation gateway said "no key" and offered no alternatives. Both were
    reading the same machine. The difference was that providers.status()
    calls envfile.load_env(), which loads ~/.bgate/.env into os.environ as a
    SIDE EFFECT, and this function did not - so whether generation was
    routable depended on whether a status panel had happened to run first.
    On a machine set up the documented way (`bgate key set <p> --global`,
    which CLAUDE.md recommends as the default) the gateway, the paid tools'
    provider gate and the billing redirect all believed this provider was
    unkeyed.

    load_env is the whole precedence - shell > project .env > ~/.bgate/.env -
    and it is what retrodiffusion always used, which is why RD was the one
    provider the gateway could see. root=None is a supported call: it loads
    the machine-wide layer alone, which is exactly the case the gateway asks
    from (it probes without a project).
    """
    try:
        envfile.load_env(root)
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
        # A pinned anchor is stored as `name.r1`: the revision number IS the
        # extension, so the suffix rejected every pin in the project and the
        # only way to condition on one was to copy it somewhere with a nicer
        # name. The leading bytes say what the file actually is.
        with p.open("rb") as fh:
            head = fh.read(12)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif head.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            mime = "image/webp"
    if not mime:
        raise KreaError(f"unsupported reference type {p.suffix!r}, and the file "
                        "is not a png/jpg/webp by its leading bytes either")
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
             method: str = "GET", timeout: float = 60.0,
             extra_headers: Optional[dict] = None) -> dict:
    """One call through the shared layer, with Krea's own words on failure.

    ``extra_headers`` exists for the ONE caller that needs a header this does
    not send — submit_3d's webhook. It used to hand-roll its own urlopen for
    that, and the copy diverged where it mattered: a 402 through it said
    nothing about the API balance being billed separately from a
    subscription — the single most confusing failure this provider has.
    """
    url = path if path.startswith("http") else API_BASE + path
    headers = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
    try:
        got = _http.request(method, url, headers=headers, json=payload,
                            timeout=timeout, provider="krea")
    except _http.ProviderError as exc:
        raise _krea_error(exc, method, path) from exc
    try:
        return got.json(provider="krea")
    except _http.ProviderError as exc:
        raise KreaError(str(exc), provider="krea", status=exc.status,
                        body=exc.body) from exc


def _krea_error(exc: _http.ProviderError, method: str, path: str) -> KreaError:
    """The three a caller will actually hit, each with the fix rather than
    the status code. 402 is the surprising one: a Krea SUBSCRIPTION does not
    pay for API calls — the API balance is topped up separately, and the raw
    message does not say where to go."""
    code, detail = exc.status, exc.body
    meta = dict(provider="krea", status=code, body=detail, billing=exc.billing)
    if code == 0:
        return KreaError(f"could not reach Krea ({detail})", **meta)
    if code in (401, 403):
        return KreaError(
            "Krea rejected the API key (HTTP %s) — check KREA_API_KEY in the "
            "project's .env; tokens come from krea.ai/settings/api-tokens"
            % code, **meta)
    if code == 402:
        return KreaError(
            "Krea has no API credit — the API balance is billed separately "
            "from a workspace/subscription plan. Top it up at "
            "krea.ai/settings (billing), then retry; nothing was charged.",
            **meta)
    if code == 429:
        return KreaError(
            "Krea rate-limited this request (HTTP 429) — slow the fan-out or "
            "retry in a moment.", **meta)
    if code == 422:
        return KreaError(
            f"Krea refused the request shape for this model: {detail} "
            "(each model has its own parameter schema — see MODELS)", **meta)
    return KreaError(f"Krea HTTP {code} on {method} {path}: {detail}", **meta)


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
    job["_usd"] = price_for(
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


# /assets is the one endpoint here that is not JSON. The encoder lives in
# imageto3d because that module needs the same one for two of its backends, and
# the copy that used to sit here was byte-identical apart from its content-type
# guess — two adapters quietly disagreeing about the MIME of the same .webp.
_multipart = _i3d.multipart


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
    try:
        got = _http.request(
            "POST", API_BASE + "/assets", data=body, timeout=timeout,
            provider="krea", headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": content_type}
        ).json(provider="krea")
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise KreaError(f"could not reach Krea to upload {p.name} "
                            f"({exc.body})", provider="krea") from exc
        raise KreaError(f"Krea rejected the upload of {p.name} "
                        f"(HTTP {exc.status}): {exc.body}", provider="krea",
                        status=exc.status, body=exc.body,
                        billing=exc.billing) from exc
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
        "usd": TRAIN_USD,
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
    """Wait for a job to reach a terminal state. Bounded: a job that never
    finishes must fail the caller rather than hold a seat's agent forever."""
    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])

    last: dict = {}

    def step() -> Optional[dict]:
        nonlocal last
        last = _request(f"/jobs/{job_id}", key, timeout=30.0)
        status = str(last.get("status") or "")
        if status == DONE:
            return last
        if status in DEAD:
            err = last.get("error") or {}
            raise KreaError(
                f"Krea job {status}: {err.get('message') or err.get('code') or 'no reason given'}")
        if status and status not in RUNNING:
            raise _http.PollUnknown(f"Krea job returned unknown status {status!r}")
        return None

    try:
        return _http.poll(step, first=max(0.5, float(interval)),
                          max_wait=max(5.0, float(timeout)), factor=1.25,
                          ceiling=5.0, unknown_is_fatal=True, provider="krea",
                          label=f"job {job_id}")
    except KreaError:
        raise
    except _http.PollUnknown as exc:
        raise KreaError(str(exc), provider="krea") from exc
    except _http.ProviderError as exc:
        raise KreaError(
            f"Krea job {job_id} did not finish within {timeout:.0f}s (last "
            f"status {last.get('status') or 'unknown'})",
            provider="krea") from exc


def download(url: str, out_path: str, *, timeout: float = 120.0,
             accept: str = "image/*") -> int:
    """Fetch the finished file to disk. Returns bytes written.

    `accept` exists because this is also how a .glb comes back, and a server
    that honours Accept would be within its rights to refuse `image/*` for a
    model. Default unchanged, so every image caller is untouched.
    """
    try:
        return _http.download(url, out_path, timeout=timeout,
                              headers={"Accept": accept}, provider="krea")
    except _http.ProviderError as exc:
        raise KreaError(str(exc), provider="krea", status=exc.status) from exc


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
    which provider produced the file: {ok, path, bytes, seconds, usd}.

    ``ref_paths`` are LOCAL anchor files and are the ergonomic half of
    ``style_refs``: they are turned into the reference array here rather than
    every caller learning :func:`style_ref`. The two ADD — a caller already
    holding built refs keeps them, and paths are appended — so this is additive
    for anyone who was already passing ``style_refs``. ``usd`` counts
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
    job_id = ""
    try:
        refs += refs_from_paths(ref_paths, ref_strength)
    except KreaError as exc:
        # NOTHING WAS SUBMITTED — a bad reference file is caught before the
        # network. Zero is the true cost here, and it is the only failure in
        # this function that can honestly say so.
        return {"ok": False, "error": str(exc), "provider": "krea",
                "model": model, "seconds": round(time.monotonic() - started, 2),
                "usd": 0.0}
    try:
        job = submit(prompt, model=model, size=size, seed=seed,
                     style_refs=refs or None, image_url=image_url,
                     strength=strength, creativity=creativity,
                     quality=quality, styles=styles, root=root)
        job_id = str(job.get("job_id") or job.get("id") or "")
        if not job_id:
            raise KreaError(f"Krea did not return a job id: {str(job)[:200]}")
        done = poll(str(job_id), root=root, timeout=timeout)
        urls = ((done.get("result") or {}).get("urls")) or []
        if not urls:
            raise KreaError("Krea reported completed with no image URL")
        written = download(urls[0], out_path)
    except KreaError as exc:
        # CARRY THE JOB, AND DO NOT PRICE IT AT ZERO. generate_3d learned both
        # of these from a live call that failed at the DOWNLOAD, after the job
        # had completed and been charged for — and then this function, the one
        # the whole 2D pipeline runs through, kept doing exactly what that fix
        # was written about. Once there is a job_id the generation is accepted
        # and may already be paid for: dropping the id makes a finished image
        # unrecoverable, and reporting usd 0.0 says a charge that happened
        # did not.
        #
        # None, not the quote: it is not known whether this one billed, and this
        # module's rule about zeros applies to every number a reader might see.
        return {"ok": False, "error": str(exc), "provider": "krea",
                "model": model, "job_id": job_id,
                "seconds": round(time.monotonic() - started, 2),
                "usd": None if job_id else 0.0,
                "recover": (f"the job may be done and paid for — poll "
                            f"/jobs/{job_id} and download from its result")
                           if job_id else "",
                "cost_note": ("this failed after Krea accepted the job, so the "
                              "cost is UNKNOWN rather than zero — it would have "
                              f"quoted ${price_for(model, style_refs=len(refs)):.4f}")
                             if job_id else ""}
    result = _result.shape({
        "ok": True, "path": str(out_path), "bytes": written,
        "provider": "krea", "model": model,
        "job_id": str(job_id), "url": urls[0],
        "seconds": round(time.monotonic() - started, 2),
        "usd": price_for(model, style_refs=len(refs)),
    })
    if tileable:
        # After the download, never instead of it: the image is already paid
        # for, so a post-pass that cannot run must degrade to a note.
        result["tileable"] = make_tileable(str(out_path))
    return result


# ---------------------------------------------------------------------------
# 3D. The same job/poll/download dance, a different family of models.
# ---------------------------------------------------------------------------
#
# This is the only image-to-3D the product can reach, and it arrives through the
# key that is already configured — no GPU, no CUDA toolchain, no weight
# download. Krea runs the open-weight models one would otherwise self-host.
#
# TWO THINGS ARE NOT LIKE THE IMAGE PATH.
#
# 1. THERE IS NO PUBLISHED PRICE. Krea's API price list covers image, video and
#    Topaz upscaling; no 3D model appears in it, and trellis-2's own API
#    reference says so outright. The "compute token" figures in Krea's user
#    guide are the WEB APP's subscription meter, which is a different currency
#    from the API's USD prepaid balance — quoting them here would be inventing
#    a number. So price_for_3d returns None, never 0.0, and generate_3d refuses
#    to spend until a caller says confirm_unpriced=True. An unknown price that
#    reads as free is how a budget gets spent without anyone deciding to.
#
# 2. NOTHING COMES BACK RIGGED. Geometry and texture, no armature, no unit
#    convention, no guaranteed pose. Measured on a real user's character: 940
#    fragmented shells, wrong pose, missing lettering. `decimation_target`
#    (trellis-2) and `face_count` (hunyuan3d-3.1-pro) are the knobs aimed at
#    exactly that, and are worth turning before writing any cleanup code.
#
# Per-model payload shapes differ as much as they do for images — trellis-2
# takes a resolution tier and a decimation target, hunyuan3d-3.1-pro takes PBR
# and seven extra view URLs, tripo takes neither. Sending the wrong pair is a
# 422 "Unrecognized keys", so `supports` is enforced here rather than hoped for.
MODELS_3D: dict[str, dict] = {
    "trellis-2": {
        "path": "/generate/3d/microsoft/trellis-2",
        # MEASURED, not published: two text-to-3D jobs at default settings on
        # 2026-07-31 each billed $0.30 on Krea's usage page, which labels the
        # column "Estimated cost". Both ran the same parameters, so this is the
        # DEFAULT-CONFIG price and nothing is known about how resolution,
        # texture_size or decimation_target move it. Treat it as a floor.
        "usd": 0.30,
        "usd_measured": "2026-07-31, text-to-3D, default parameters",
        "supports": {"seed", "input_mode", "generate_texture", "resolution",
                     "texture_size", "decimation_target", "image_urls"},
        "resolution": ("512", "1024", "1536"),
        "texture_size": ("1024", "2048", "4096"),
        "decimation_target": (100_000, 2_000_000),
        "note": "the current best, and the only one with a decimation target — "
                "the knob for a mesh that arrives as fragmented shells.",
    },
    "trellis": {
        "path": "/generate/3d/microsoft/trellis",
        "supports": {"seed", "input_mode", "generate_texture", "texture_size",
                     "image_urls"},
        "note": "the older TRELLIS. Keep for comparison; prefer trellis-2.",
    },
    "tripo": {
        "path": "/generate/3d/tripo/tripo",
        "supports": {"seed", "input_mode", "generate_texture", "image_urls"},
        "note": "fewest knobs of the five — prompt, seed, texture on or off.",
    },
    "hunyuan3d-2.1": {
        "path": "/generate/3d/tencent/hunyuan3d-2.1",
        "supports": {"seed", "input_mode", "generate_texture", "image_urls"},
        "note": "Tencent's 2.x line.",
    },
    "hunyuan3d-3.1-pro": {
        "path": "/generate/3d/tencent/hunyuan3d-3.1-pro",
        "supports": {"seed", "input_mode", "generate_texture", "enable_pbr",
                     "face_count", "image_urls", "back_image_url",
                     "left_image_url", "right_image_url", "top_image_url",
                     "bottom_image_url", "left_front_image_url",
                     "right_front_image_url"},
        "face_count": (40_000, 1_500_000),
        "views": ("back", "left", "right", "top", "bottom", "left_front",
                  "right_front"),
        "note": "the only MULTI-VIEW model here and the only one taking PBR. "
                "Extra views are how a back nobody photographed stops being "
                "invented — pass them when the subject has a defined back.",
    },
}

DEFAULT_MODEL_3D = "trellis-2"


def models_3d() -> dict:
    """The 3D catalogue, for a caller choosing a model."""
    return {name: {k: v for k, v in spec.items() if k != "supports"}
            for name, spec in MODELS_3D.items()}


def price_for_3d(model: str = DEFAULT_MODEL_3D) -> Optional[float]:
    """USD where it has been MEASURED, None where it is still unknown.

    Krea publishes no 3D price anywhere — not in the API price list, and the
    trellis-2 reference says so outright. The only figures on the docs site are
    "compute tokens", which are the WEB APP's subscription meter and not the
    API's USD balance; quoting those would be inventing a number.

    So a price here comes from a real invoice or it does not exist. None means
    unknown and the caller must say so; 0.0 is never returned, because a reader
    would take that for free.
    """
    spec = MODELS_3D.get(model)
    if not spec:
        raise KreaError(f"unknown 3D model {model!r} — known: {sorted(MODELS_3D)}")
    usd = spec.get("usd")
    return float(usd) if usd is not None else None


def _image_ref(source: str | os.PathLike) -> str:
    """A local file becomes a data URI; a URL is passed through.

    `image_urls` accepts external URLs, base64 data URIs and Krea asset URLs,
    so a caller can hand this a path from disk or a URL it already holds.
    """
    text = str(source)
    if text.startswith(("http://", "https://", "data:")):
        return text
    return data_uri(text)


def submit_3d(prompt: str = "", *, model: str = DEFAULT_MODEL_3D,
              images=(), seed: Optional[int] = None,
              generate_texture: bool = True, resolution: str = "",
              texture_size: str = "", decimation_target: Optional[int] = None,
              face_count: Optional[int] = None,
              enable_pbr: Optional[bool] = None,
              views: Optional[dict] = None, webhook: str = "",
              root: Any = None, timeout: float = 60.0) -> dict:
    """Start a 3D generation. Returns the job envelope with `job_id`.

    `images` are the input plate(s), local paths or URLs. Empty means
    text-to-3D, and input_mode is set from that rather than making every caller
    remember to. `views` maps extra angle names (back, left, right, top,
    bottom, left_front, right_front) to sources and is hunyuan3d-3.1-pro only.

    A parameter the chosen model does not declare is REFUSED, not dropped.
    Dropping it would send the request, charge for it, and hand back the
    default — so a caller who asked for a decimation target on a model that has
    none would get exactly the fragmented mesh they were trying to avoid, with
    nothing saying why.
    """
    spec = MODELS_3D.get(model)
    if not spec:
        raise KreaError(f"unknown 3D model {model!r} — known: {sorted(MODELS_3D)}")

    # SHAPE FIRST, KEY SECOND. Checking the key up here would answer a bad
    # decimation_target with "KREA_API_KEY not set", which names the wrong
    # problem and makes every refusal below untestable without a live key.
    # Building the payload costs nothing and touches no network.
    plates = [_image_ref(p) for p in (images or ())]
    if not plates and not str(prompt).strip():
        raise KreaError("a 3D generation needs an image or a prompt — got neither")

    payload: dict = {"prompt": prompt or "",
                     "input_mode": "image" if plates else "text",
                     "generate_texture": bool(generate_texture)}
    if plates:
        payload["image_urls"] = plates
    if seed is not None:
        payload["seed"] = int(seed)

    def _want(field: str, value, allowed=()) -> None:
        if value in (None, ""):
            return
        if field not in spec["supports"]:
            takes = sorted(n for n, s in MODELS_3D.items()
                           if field in s["supports"])
            raise KreaError(
                f"{model} does not take {field} — it would be ignored and you "
                f"would be charged for the default. Models that do: {takes}")
        if allowed and str(value) not in allowed:
            raise KreaError(
                f"{field}={value!r} is not one of {list(allowed)} for {model}")
        payload[field] = value

    def _ranged(field: str, value) -> None:
        if value is None:
            return
        lo, hi = spec.get(field, (0, 0))
        if lo and not (lo <= int(value) <= hi):
            raise KreaError(f"{field} must be {lo}..{hi} for {model}, got {value}")
        _want(field, int(value))

    _want("resolution", resolution, spec.get("resolution", ()))
    _want("texture_size", texture_size, spec.get("texture_size", ()))
    _ranged("decimation_target", decimation_target)
    _ranged("face_count", face_count)
    if enable_pbr is not None:
        _want("enable_pbr", bool(enable_pbr))
    for name, source in (views or {}).items():
        field = name if name.endswith("_image_url") else f"{name}_image_url"
        _want(field, _image_ref(source))

    key = api_key(root)
    if not key:
        raise KreaError(available(root)["reason"])

    # A webhook is a HEADER, and this used to be a hand-rolled second urlopen
    # because _request had no hook for one. The duplicate then diverged where it
    # mattered: it dropped the response body and all of _request's per-code
    # advice, so the same 402 that tells a webhook-less caller "the API balance
    # is billed separately from a subscription" told a webhook caller nothing at
    # all. One code path now, one extra header.
    if not webhook:
        return _request(spec["path"], key, payload=payload, method="POST",
                        timeout=timeout)
    return _request(spec["path"], key, payload=payload, method="POST",
                    timeout=timeout,
                    extra_headers={"X-Webhook-URL": webhook})


def _model_url(result: dict) -> str:
    """Dig the .glb URL out of a completed 3D job.

    MEASURED on the first live call, which is the only reason this is not one
    line: the image path documents `result.urls` as an array of STRINGS, and
    the 3D path returns an array of OBJECTS. Passing one to urllib raised
    "unknown url type: {'type'" after the generation had already been paid
    for. The 3D job's completed body is not published, so this reads the
    documented shape, the measured shape, and the obvious singular fields,
    and gives up loudly rather than guessing further.
    """
    def _one(entry) -> str:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for field in ("url", "signed_url", "href", "download_url"):
                value = entry.get(field)
                if isinstance(value, str) and value:
                    return value
        return ""

    for entry in (result.get("urls") or []):
        found = _one(entry)
        if found:
            return found
    for field in ("model_url", "glb_url", "url", "asset_url", "model", "glb"):
        found = _one(result.get(field))
        if found:
            return found
    return ""


def generate_3d(out_path: str, *, prompt: str = "", images=(),
                model: str = DEFAULT_MODEL_3D, seed: Optional[int] = None,
                generate_texture: bool = True, resolution: str = "",
                texture_size: str = "", decimation_target: Optional[int] = None,
                face_count: Optional[int] = None,
                enable_pbr: Optional[bool] = None, views: Optional[dict] = None,
                confirm_unpriced: bool = False, timeout: float = 900.0,
                root: Any = None) -> dict:
    """Submit, wait, download a .glb. The 3D twin of :func:`generate`.

    WHAT COMES BACK IS A DRAFT, NOT AN ASSET, and `draft` is True in the result
    to say so. Geometry and texture, no armature, no unit convention, no
    guaranteed pose. It still owes the pipeline a clean, a scale to the
    project's convention, an orientation and a weighting before combine or
    delivery will make anything of it.

    `confirm_unpriced` is not ceremony. Krea publishes no 3D price, so unlike
    every other spend in this module the cost cannot be quoted first and the
    caller has to accept an unknown charge out loud. `usd` stays None
    throughout — never 0.0, which reads as free.

    The timeout defaults high because a 3D job runs in minutes where an image
    runs in seconds.
    """
    started = time.monotonic()
    quote = price_for_3d(model)
    base = {"provider": "krea", "model": model, "kind": "3d",
            "usd": quote, "draft": True}
    job_id = ""
    finished: dict = {}

    # The gate is for an UNKNOWN price, not for spending. A model with a
    # measured rate quotes itself and runs like any other paid call; only the
    # ones nobody has invoiced yet need a caller to accept a blind charge.
    if quote is None and not confirm_unpriced:
        return {**base, "ok": False, "seconds": 0.0,
                "error": f"no measured price for {model} — Krea publishes none "
                         "for 3D, so this call cannot be quoted before it runs "
                         "and nothing has been spent. Pass confirm_unpriced=True "
                         "to accept an unknown charge against the API balance.",
                "price_note": "the compute-token figures in Krea's user guide "
                              "are the web app's subscription meter, not the "
                              "API's USD balance — they do not apply here"}
    try:
        job = submit_3d(prompt, model=model, images=images, seed=seed,
                        generate_texture=generate_texture,
                        resolution=resolution, texture_size=texture_size,
                        decimation_target=decimation_target,
                        face_count=face_count, enable_pbr=enable_pbr,
                        views=views, root=root)
        job_id = job.get("job_id") or job.get("id")
        if not job_id:
            raise KreaError(f"Krea did not return a job id: {str(job)[:200]}")
        finished = poll(str(job_id), root=root, timeout=timeout)
        result = finished.get("result") or {}
        url = _model_url(result)
        if not url:
            raise KreaError("Krea reported completed with no model URL — the "
                            f"result body was {str(result)[:300]}")
        written = download(url, out_path, accept="model/gltf-binary,*/*",
                           timeout=300.0)
    except KreaError as exc:
        # CARRY THE JOB. A 3D generation is minutes of paid compute, and the
        # first live call failed at the DOWNLOAD, after the job had completed
        # and been charged for — dropping the id here made a finished result
        # unrecoverable and cost a second generation to learn the same thing.
        # `GET /jobs/<id>` still has it.
        return {**base, "ok": False, "error": str(exc),
                "job_id": str(job_id), "result": (finished or {}).get("result"),
                "recover": (f"the job is done and paid for — poll /jobs/{job_id} "
                            "and download from its result") if job_id else "",
                "seconds": round(time.monotonic() - started, 2)}
    out = _result.shape({**base, "ok": True, "path": str(out_path),
                         "bytes": written, "job_id": str(job_id), "url": url,
                         "seconds": round(time.monotonic() - started, 2)})
    # Krea publishes no 3D price, so ``usd`` is None here rather than 0.0. The
    # caller shows the gap; nothing invents a figure to fill it.
    return {**out,
            "next_steps": ("merge and clean the shells",
                           "scale to the project's unit convention",
                           "orient forward", "weight to a skeleton",
                           "then combine or deliver")}
