"""kie.ai — one key, three capabilities: images, Suno music, Seedance video.

WHY A THIRD IMAGE PROVIDER, AND WHY IT IS NOT ONLY AN IMAGE PROVIDER. Krea was
worth wiring because it is a front door to twenty models rather than one. kie is
worth wiring for a different reason: it is the first credential in this product
that reaches capabilities the pipeline has never had at all. Builders Gate can
mix audio (bgate_core.audiolab) but has never GENERATED a note of it, and it has
never generated a frame of video. Suno and Seedance arrive behind the same key
as the images, which is the whole "one key, easy setup" argument for it.

IT DOES NOT DO 3D. Krea remains the only hosted image-to-3D this product can
reach; nothing here is a substitute for krea.generate_3d, and there is
deliberately no 3D entry point in this module.

TWO API FAMILIES LIVE UNDER ONE KEY, and confusing them is the mistake this
module is shaped to prevent. They agree on the Bearer token and on nothing else:

  * THE MARKET JOBS API — images and video. POST /api/v1/jobs/createTask with
    {model, input, callBackUrl}, then GET /api/v1/jobs/recordInfo?taskId=...
    States are LOWERCASE: waiting / queuing / generating / success / fail. The
    finished URLs arrive in `resultJson`, which is a JSON *string* holding
    {"resultUrls": [...]} (or {"resultObject": {...}} for models that answer
    with a structure rather than files) — it has to be json.loads'd, and a
    caller that treats it as an object gets a TypeError instead of a picture.
  * THE SUNO API — music, and it predates the market surface. POST
    /api/v1/generate, poll GET /api/v1/generate/record-info?taskId=..., statuses
    are UPPERCASE and there are seven of them, and the tracks come back as real
    nested JSON under data.response.sunoData — no string to parse, a different
    field name, and TWO terminal-ish states that are not the end (TEXT_SUCCESS,
    FIRST_SUCCESS) because Suno streams lyrics, then track one, then the rest.

THE STATUS CODE IS IN THE BODY. kie answers HTTP 200 and puts the real verdict
in `code` — 402 for no credit, 422 for a bad shape, 501 for a generation that
failed. Checking only the HTTP status reads "insufficient credits" as a success
with no data, which is the shape of a bug that looks like an empty response. So
:func:`_request` checks both, always.

NOTHING HERE IS PRICED, AND THAT IS DELIBERATE. kie's own API reference publishes
no per-model price: the quickstart says image models are "typically 10-50
credits" and video "typically 100-500", and no credit-to-dollar rate appears in
the reference at all. Third-party blog arithmetic exists and is not a price. So
this follows the precedent krea.TRAIN_USD set — declare it None, never 0.0,
because every budget check in this product reads a number as permission. What
kie DOES give us that Krea's 3D path did not is `creditsConsumed` on the finished
record: the true cost of the call comes back after it runs, so a user who tells
us their rate (BGATE_KIE_USD_PER_CREDIT) gets exact ledger rows rather than
estimates. Without it the result carries the credit count and says plainly that
no dollar figure was recorded.

LOCAL FILES CANNOT BE SENT. Every reference field kie documents is a URI, and
the reference says nothing about base64 data URIs — unlike Krea, whose docs state
outright that a data URI is accepted, which is why krea.data_uri exists. Every
pinned anchor in this tool is a file on disk, so anchored generation through kie
is NOT available, and this module refuses a local path with that sentence rather
than encoding one on a guess and discovering at the 422 that it was wrong.

Everything is stdlib. No SDK, no new dependency.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from bgate_core import envfile

API_BASE = "https://api.kie.ai"

# The market jobs surface: images and video.
JOBS_CREATE = "/api/v1/jobs/createTask"
JOBS_RECORD = "/api/v1/jobs/recordInfo"
# The remaining credit balance. The one endpoint here that is safe to call as a
# health check — it generates nothing and costs nothing.
CREDIT_PATH = "/api/v1/chat/credit"

# The Suno surface, which is a different API that happens to share the key.
SUNO_CREATE = "/api/v1/generate"
SUNO_RECORD = "/api/v1/generate/record-info"

ENV = "KIE_API_KEY"
KEY_URL = "https://kie.ai/api-key"

# Market job states, verbatim from the Get Task Details reference. Lowercase,
# and note that `fail` is singular — "failed" is the Suno spelling and belongs
# to the other table.
JOB_DONE = "success"
JOB_DEAD = {"fail"}
JOB_RUNNING = {"waiting", "queuing", "generating"}

# Suno statuses, verbatim, and the two in the middle are the reason this is not
# a copy of the table above. Suno reports progress through the generation:
# TEXT_SUCCESS means the lyrics exist, FIRST_SUCCESS means one of the tracks is
# playable while the others are still rendering. Treating either as terminal
# hands back half a request that was paid for in full.
SUNO_DONE = "SUCCESS"
SUNO_DEAD = {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR"}
SUNO_RUNNING = {"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"}

# THE EIGHTH STATUS, AND IT BELONGS IN NEITHER SET. `CALLBACK_EXCEPTION` is
# documented alongside the other seven and means kie could not DELIVER the
# webhook — it says nothing about the audio, which has usually rendered and has
# certainly been charged for. Filing it under SUNO_DEAD would throw away paid
# tracks that are sitting in the record; leaving it out entirely (which is what
# this module did) made it an "unknown status" and raised, with the same result.
# So it is terminal, and whether it is a failure depends on whether the record
# carries any audio — which is a question :func:`music_tracks` can answer.
SUNO_CALLBACK_FAILED = "CALLBACK_EXCEPTION"

# Every status the reference lists, for a caller that wants to render one.
SUNO_STATUSES = (
    "PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS", SUNO_DONE,
    "CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", SUNO_CALLBACK_FAILED,
    "SENSITIVE_WORD_ERROR",
)

# WHAT EACH STATUS MEANS TO A PERSON WATCHING A SPINNER, and roughly how far
# through it is. A generation runs one to three minutes; reported as
# "PENDING" — or as nothing at all — that is indistinguishable from a hang, and
# the first thing a user does about a hang is start a second one, which costs
# money. Suno tells us where it is at every step; this is the phrasebook, kept
# beside the status table it describes so the two cannot drift.
#
# The fractions are a PROGRESS SHAPE, not a measurement — Suno publishes no
# percentage. They are monotonic and honestly labelled, which is what a bar
# has to be; a bar that goes backwards is worse than no bar.
SUNO_STAGE = {
    "PENDING": (0.15, "Suno has queued it — the model has not started yet"),
    "TEXT_SUCCESS": (0.40, "lyrics and structure written — rendering audio now"),
    "FIRST_SUCCESS": (0.65, "the first take is rendered — waiting for the rest"),
    SUNO_DONE: (0.80, "every take is rendered — downloading them"),
    SUNO_CALLBACK_FAILED: (0.80, "kie could not deliver its callback — checking "
                                 "whether the audio rendered anyway"),
}
# Before there is a status at all.
SUNO_STAGE_SUBMIT = (0.05, "asking Suno for a batch")

# The business codes kie documents, with what a human should DO about each. The
# status number alone is useless here because it arrives in a 200 response.
_CODE_HELP = {
    401: "kie rejected the API key — check " + ENV + " in the project's .env; "
         "keys come from " + KEY_URL,
    402: "kie has no credit left — top the account up at kie.ai, then retry; "
         "nothing was charged",
    404: "kie has no such endpoint or task — check the model id against "
         "docs.kie.ai/market",
    422: "kie refused the request shape — every model has its own input schema, "
         "see MODELS",
    429: "kie rate-limited this request — slow the fan-out or retry in a moment",
    433: "this sub-key is over its own usage limit — use the parent key or raise "
         "the sub-key's cap",
    455: "kie is in maintenance — retry later; nothing was charged",
    500: "kie hit an internal error",
    501: "the generation itself failed — see failMsg on the record",
    505: "this model is currently disabled on kie",
}


class KieError(RuntimeError):
    """A kie call failed in a way the caller should surface, not retry blindly."""


class MusicCancelled(KieError):
    """A human stopped waiting for a music job that is already paid for.

    A subclass of KieError so nothing that catches the general failure has to
    learn a second name, and its own class so a caller CAN tell the difference —
    because the honest report differs. A failure says something went wrong; this
    says the generation is very likely still running at kie, was charged for,
    and can be collected later with the task id on the result. Raise it from a
    ``on_progress`` callback; see :func:`poll_music`.
    """


# ---------------------------------------------------------------------------
# Models. Data, not branches — see imageto3d.BACKENDS for the same shape.
# ---------------------------------------------------------------------------
# ONLY MODELS WHOSE ID AND SCHEMA WERE READ OFF THEIR OWN REFERENCE PAGE ARE
# HERE. kie's market carries dozens; the ids follow a family/variant convention
# ("google/nano-banana", "flux-2/pro-image-to-image") that is regular enough to
# guess at and wrong often enough to matter — nano-banana-pro's reference lives
# at /market/google/pro-image-to-image, which is not what the pattern predicts.
# A guessed id is a 404 after a round trip; adding a verified one is four lines.
#
# Fields:
#   model      the literal string kie wants in the top-level `model` field
#   kind       "image" | "video". Decides which spend bucket and which default
#              file suffix, nothing else — both go through the same jobs API.
#   required   input keys that must be present. Checked here, before the call.
#   supports   every input key this model accepts. An unknown key is REFUSED
#              rather than dropped: kie answers an unrecognised key with a 422
#              on some models and silently ignores it on others, and the silent
#              one is worse — you pay for a generation that ignored the setting
#              you were relying on.
#   enums      key -> the allowed values, verbatim from the reference
#   ranges     key -> (lo, hi), inclusive
#   caps       key -> maximum array length
#   images     which key carries reference images, "" for none. Documented as
#              URIs; see the module docstring on why a local path is refused.
#   credits    what kie charges. None everywhere, because kie publishes none.
MODELS: dict[str, dict] = {
    "nano-banana": {
        "model": "google/nano-banana",
        "kind": "image",
        "label": "Google Nano Banana (Gemini 2.5 Flash Image)",
        "required": ("prompt",),
        "supports": {"prompt", "output_format", "aspect_ratio", "image_size",
                     "nsfw_checker"},
        "enums": {
            "output_format": ("png", "jpeg"),
            "aspect_ratio": ("1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3",
                             "5:4", "4:5", "21:9", "auto"),
        },
        "ranges": {},
        "caps": {},
        "images": "",
        "credits": None,
        "note": "Text-to-image, prompt only. No reference conditioning, so not "
                "for anchored character work — that stays on Krea.",
    },
    "flux-2-pro-edit": {
        "model": "flux-2/pro-image-to-image",
        "kind": "image",
        "label": "FLUX.2 Pro (image to image)",
        "required": ("prompt", "input_urls"),
        "supports": {"prompt", "input_urls", "aspect_ratio", "resolution",
                     "nsfw_checker"},
        "enums": {
            "aspect_ratio": ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3",
                             "auto"),
            "resolution": ("1K", "2K"),
        },
        "ranges": {"prompt": (3, 5000)},
        "caps": {},
        "images": "input_urls",
        "images_list": True,
        "credits": None,
        "note": "Edits supplied images. Needs HOSTED urls — a pinned anchor on "
                "disk cannot reach it.",
    },
    "qwen-edit": {
        "model": "qwen/image-to-image",
        "kind": "image",
        "label": "Qwen (image to image)",
        "required": ("prompt", "image_url"),
        "supports": {"prompt", "image_url", "strength", "output_format",
                     "acceleration", "negative_prompt", "seed",
                     "num_inference_steps", "guidance_scale",
                     "enable_safety_checker", "nsfw_checker"},
        "enums": {
            "output_format": ("png", "jpeg"),
            "acceleration": ("none", "regular", "high"),
        },
        "ranges": {"strength": (0.0, 1.0), "num_inference_steps": (2, 250),
                   "guidance_scale": (0.0, 20.0), "prompt": (0, 5000),
                   "negative_prompt": (0, 500)},
        "caps": {},
        "images": "image_url",
        "images_list": False,
        "credits": None,
        "note": "The only image model here with a seed and a step count — the "
                "one to reach for when a result has to be reproducible.",
    },
    "seedance-2": {
        "model": "bytedance/seedance-2",
        "kind": "video",
        "label": "ByteDance Seedance 2.0",
        "required": ("prompt",),
        "supports": {"prompt", "first_frame_url", "last_frame_url",
                     "reference_image_urls", "reference_video_urls",
                     "reference_audio_urls", "generate_audio", "resolution",
                     "aspect_ratio", "duration", "web_search", "nsfw_checker"},
        "enums": {
            "resolution": ("480p", "720p", "1080p", "4k"),
            "aspect_ratio": ("1:1", "4:3", "3:4", "16:9", "9:16", "21:9",
                             "adaptive"),
        },
        "ranges": {"duration": (4, 15), "prompt": (3, 20000)},
        "caps": {"reference_image_urls": 9, "reference_video_urls": 3,
                 "reference_audio_urls": 3},
        "images": "reference_image_urls",
        "images_list": True,
        # `generate_audio` DEFAULTS TO TRUE upstream and is left alone here.
        # Recording our own default would mean this table quietly disagrees with
        # kie the day they change theirs, and nothing would say so.
        "credits": None,
        "note": "Text-to-video with optional first/last frame and reference "
                "clips. Generates its own audio unless told not to.",
    },
}

DEFAULT_IMAGE_MODEL = "nano-banana"
DEFAULT_VIDEO_MODEL = "seedance-2"

IMAGE_MODELS = tuple(k for k, v in MODELS.items() if v["kind"] == "image")
VIDEO_MODELS = tuple(k for k, v in MODELS.items() if v["kind"] == "video")

# What a finished job's file is called when the caller did not say. The market
# API hands back a URL whose extension is the truth; these are only the fallback
# for a URL that carries none.
_SUFFIX = {"image": ".png", "video": ".mp4"}


# ---------------------------------------------------------------------------
# Suno. Its own vocabulary, because it is its own API.
# ---------------------------------------------------------------------------
# THE TWO REFERENCE PAGES DISAGREE ON THE MODEL LIST. The Suno quickstart lists
# V3_5, V4, V4_5, V4_5PLUS, V5, V5_5; the generate-music reference lists V4,
# V4_5, V4_5PLUS, V4_5ALL, V5, V5_5 — dropping V3_5 and adding V4_5ALL. Neither
# page is marked stale. The union is accepted here, because refusing a version
# one of kie's own pages documents is a worse failure than forwarding a version
# string kie will reject with a 422 that names the problem exactly.
SUNO_MODELS = ("V3_5", "V4", "V4_5", "V4_5PLUS", "V4_5ALL", "V5", "V5_5")
DEFAULT_SUNO_MODEL = "V5"

# Character ceilings, and they move with BOTH the model and the mode. V4 is the
# odd one out; everything newer is the same. Enforced locally because the cost
# of finding out from kie is a round trip and a 422 whose message does not say
# which of the three fields was too long.
SUNO_CUSTOM_LIMITS = {"V4": {"prompt": 3000, "style": 200, "title": 80}}
SUNO_CUSTOM_DEFAULT = {"prompt": 5000, "style": 1000, "title": 80}
# Simple mode takes a description, not lyrics, and the cap is far tighter.
SUNO_SIMPLE_PROMPT = 500

# 0..1, two decimal places, per the reference. Clamped rather than refused —
# these are dials, and a caller that passes 1.5 meant "as far as it goes".
SUNO_WEIGHTS = ("styleWeight", "weirdnessConstraint", "audioWeight")
# V5_5 ONLY. Sending it to any other version is a 422, so it is gated here.
SUNO_DURATION_RANGE = (10, 360)
SUNO_DURATION_MODELS = ("V5_5",)

# Generated files are retained for FOURTEEN DAYS. That is not a footnote: a
# track referenced by URL from a project manifest is a dead link a fortnight
# later, so generate_music downloads inside the polling loop the way
# imageto3d does for Tripo's five-minute URLs.
SUNO_URL_TTL_DAYS = 14

# TWO TRACKS PER REQUEST, and the number is observed rather than promised. The
# reference's own record-info example shows a `sunoData` ARRAY, the quickstart
# describes variations, and no page states a count — so this is what to SIZE a
# gallery for, never what to assume arrived. Every caller here reads
# len(music_tracks(record)); nothing branches on this constant.
SUNO_TRACKS_HINT = 2

# THE TWO PAGES DISAGREE ABOUT callBackUrl, AND THE API SIDES WITH THE
# REFERENCE. The generate-music reference marks it REQUIRED; the quickstart's
# own example omits it and polls instead. Sending the quickstart's shape was
# tried against the live API and is simply rejected:
#
#     POST /api/v1/generate  {prompt, customMode, instrumental, model}
#     -> HTTP 200  {"code":422,"msg":"Please enter callBackUrl.","data":null}
#
# (Note the verdict is in the BODY's `code`, not the HTTP status — 200 with a
# 422 inside is normal here.) So the field is now always sent.
#
# kie only checks that it is PRESENT, not that it is reachable. Proven with a
# deliberately over-long prompt so no job could start: without the field the
# error is "Please enter callBackUrl"; with it the same request fails on the
# prompt ceiling instead. Nothing was generated either time.
#
# The value therefore has to be a URL we are willing to hand a third party.
# It is the dashboard's own loopback address: semantically honest (that IS
# where a callback would arrive if kie could reach it), and unreachable from
# outside this machine, so kie's delivery attempt resolves to its own localhost
# and reaches nobody. We poll for the result regardless. Anyone who does have a
# public receiver — a tunnel, a relay — can set BGATE_KIE_CALLBACK_URL and get
# the documented push path instead.
SUNO_CALLBACK_ENV = "BGATE_KIE_CALLBACK_URL"
SUNO_CALLBACK_DEFAULT = "http://127.0.0.1:7788/api/music/callback"
SUNO_CALLBACK_NOTE = (
    "kie requires callBackUrl on every music request. This product has no "
    "public receiver (the dashboard is loopback-only), so it sends its own "
    "loopback address to satisfy the field and polls record-info for the "
    "result. Set " + SUNO_CALLBACK_ENV + " if you have a reachable receiver.")


def callback_url(explicit: str = "") -> str:
    """The callBackUrl to send. Never empty — an empty one is a hard 422."""
    return (str(explicit or "").strip()
            or os.environ.get(SUNO_CALLBACK_ENV, "").strip()
            or SUNO_CALLBACK_DEFAULT)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# NOT PUBLISHED, NOT GUESSED. See the module docstring. None propagates all the
# way to the result, and 0.0 is never returned — a spend gate reads 0.0 as free.
USD_PER_CREDIT: Optional[float] = None
USD_PER_CREDIT_ENV = "BGATE_KIE_USD_PER_CREDIT"

PRICE_NOTE = (
    "kie publishes no per-model price in its API reference — the quickstart "
    "says image models are 'typically 10-50 credits' and video '100-500', with "
    "no credit-to-dollar rate anywhere. The finished record reports "
    "creditsConsumed, so set " + USD_PER_CREDIT_ENV + " to your account's rate "
    "and every call lands in the ledger in dollars. Until then the credits are "
    "reported and no dollar figure is recorded.")


def usd_per_credit() -> Optional[float]:
    """The user's own credit rate, or None. Never a default.

    An env var rather than a constant because the rate is a property of the
    account's top-up bundle, not of the API, and this tool has no way to read it.
    """
    raw = (os.environ.get(USD_PER_CREDIT_ENV) or "").strip()
    if not raw:
        return USD_PER_CREDIT
    try:
        rate = float(raw)
    except ValueError:
        return USD_PER_CREDIT
    return rate if rate > 0 else USD_PER_CREDIT


def cost_usd(credits: Optional[int]) -> Optional[float]:
    """What `credits` actually cost, once the human has told us the rate."""
    rate = usd_per_credit()
    if rate is None or credits in (None, ""):
        return None
    try:
        return round(float(credits) * rate, 6)
    except (TypeError, ValueError):
        return None


def price_for(model: str = DEFAULT_IMAGE_MODEL) -> Optional[float]:
    """Always None, and the signature exists so callers can ask.

    Mirrors krea.price_for_3d: a caller enforcing a budget must treat None as
    unknown and ask a human, not fall back to zero.
    """
    _spec(model)
    return None


# ---------------------------------------------------------------------------
# Key and availability
# ---------------------------------------------------------------------------

def api_key(root: Any = None) -> str:
    """The token, from the project's .env or the environment. Never logged."""
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:                                        # noqa: BLE001
            pass
    return (os.environ.get(ENV) or "").strip()


def available(root: Any = None) -> dict:
    """Can we call kie at all? Presence only — a health check that spends money
    is one nobody runs, and the credit endpoint is not called here either
    because availability must not depend on the network being up."""
    if not api_key(root):
        return {
            "available": False,
            "reason": ENV + " not set — put it in the project's .env "
                            "(gitignored, loaded per project) or the "
                            "environment; create a key at " + KEY_URL,
        }
    return {
        "available": True,
        "image_models": sorted(IMAGE_MODELS),
        "video_models": sorted(VIDEO_MODELS),
        "music_models": list(SUNO_MODELS),
        "default_image": DEFAULT_IMAGE_MODEL,
        "default_video": DEFAULT_VIDEO_MODEL,
        "default_music": DEFAULT_SUNO_MODEL,
        "usd_per_credit": usd_per_credit(),
        "price_note": PRICE_NOTE,
    }


def _spec(model: str) -> dict:
    spec = MODELS.get(model)
    if spec is None:
        raise KieError(f"unknown kie model {model!r} — known: {sorted(MODELS)}")
    return spec


def models(kind: str = "") -> dict:
    """The catalogue, for a caller choosing. `supports` is dropped: it is a set,
    which does not survive JSON, and `enums` already says what the knobs are."""
    return {name: {k: (sorted(v) if isinstance(v, set) else v)
                   for k, v in spec.items() if k != "supports"}
            for name, spec in MODELS.items()
            if not kind or spec["kind"] == kind}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _envelope(body: dict, *, what: str) -> dict:
    """Unwrap {code, msg, data}, raising on a business failure.

    THE VERDICT IS IN THE BODY. kie answers HTTP 200 and puts 402 in `code`, so
    a caller checking only the transport reads "no credit" as a success with no
    data. Every response in this module goes through here.
    """
    code = body.get("code")
    if code in (200, "200", None):
        data = body.get("data")
        return data if isinstance(data, dict) else {"data": data}
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    help_text = _CODE_HELP.get(code, "")
    msg = str(body.get("msg") or "").strip()
    raise KieError(f"kie refused {what} (code {code})"
                   + (f": {msg}" if msg else "")
                   + (f" — {help_text}" if help_text else ""))


def _request(path: str, key: str, *, payload: Optional[dict] = None,
             params: Optional[dict] = None, method: str = "GET",
             timeout: float = 60.0) -> dict:
    """One call, unwrapped. The key rides in the header and nowhere else."""
    url = path if path.startswith("http") else API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    what = f"{method} {path}"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:                                        # noqa: BLE001
            pass
        # A transport-level failure carries the SAME numbers as the body-level
        # one, so the same advice applies and there is no second table.
        help_text = _CODE_HELP.get(exc.code, "")
        raise KieError(f"kie HTTP {exc.code} on {what}"
                       + (f": {detail}" if detail else "")
                       + (f" — {help_text}" if help_text else "")) from exc
    except urllib.error.URLError as exc:
        raise KieError(f"could not reach kie ({exc.reason})") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KieError(f"kie returned something that is not JSON on {what}: "
                       f"{raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise KieError(f"kie returned {type(parsed).__name__}, not an object, "
                       f"on {what}")
    return _envelope(parsed, what=what)


def credits(root: Any = None, *, timeout: float = 30.0) -> dict:
    """What is left in the account. The one call here that costs nothing.

    Kept out of :func:`available` on purpose — availability must answer offline,
    and a probe that needs the network turns "no key" and "no wifi" into the
    same red row.
    """
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    got = _request(CREDIT_PATH, key, timeout=timeout)
    # The reference does not pin the field name for the balance itself, so the
    # envelope is handed back whole rather than reshaped into a number that
    # might be the wrong one.
    return {"credits": got.get("data", got), "raw": got}


def credit_balance(root: Any = None, *, timeout: float = 15.0) -> Optional[float]:
    """The balance as a NUMBER, or None if it could not be read.

    Separate from :func:`credits` because the two answer different questions.
    That one is a health check whose whole value is handing back exactly what
    kie said; this one is arithmetic input, and arithmetic on a shape you
    guessed at is worse than no arithmetic. Every failure — no key, no network,
    a body shaped differently from the one guessed here — returns None, which
    every caller must treat as "unknown", never as zero.
    """
    try:
        value: Any = credits(root, timeout=timeout).get("credits")
    except Exception:                                            # noqa: BLE001
        return None
    if isinstance(value, dict):
        for key in ("credits", "credit", "balance", "remaining", "left"):
            if key in value:
                value = value[key]
                break
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Building a market-jobs request
# ---------------------------------------------------------------------------

def _reject_local(value: Any, field: str) -> None:
    """Refuse a path where kie documents a URI.

    Not pedantry — the alternative is encoding a data URI on a guess. Krea's
    reference says outright that a base64 data URI is accepted in a reference
    field; kie's says "Image URL" and nothing more, and a guess here costs a
    round trip, a 422, and the belief that anchored generation works.
    """
    for one in (value if isinstance(value, (list, tuple)) else [value]):
        text = str(one)
        if text.startswith(("http://", "https://")):
            continue
        raise KieError(
            f"{field} must be a public https URL — kie's reference documents "
            "these as URIs and says nothing about base64 data URIs, so "
            f"{text[:60]!r} cannot be sent. Anchored generation from local "
            "pinned refs is a Krea capability, not a kie one.")


def build_input(model: str, **fields: Any) -> dict:
    """The `input` object for one model, validated against its own schema.

    Every check here happens BEFORE any money moves, and every one of them is a
    thing kie's reference states. An unsupported key is refused rather than
    dropped for the reason imageto3d.submit_3d gives: a dropped parameter still
    bills you, hands back the default, and leaves nothing saying why the setting
    you passed did not apply.
    """
    spec = _spec(model)
    given = {k: v for k, v in fields.items() if v is not None and v != ""}

    unknown = sorted(set(given) - spec["supports"])
    if unknown:
        raise KieError(
            f"{model} does not take {', '.join(unknown)} — it accepts "
            f"{', '.join(sorted(spec['supports']))}. Passing it would be "
            "ignored and you would be charged for the default.")
    missing = [k for k in spec["required"] if k not in given]
    if missing:
        raise KieError(f"{model} needs {', '.join(missing)}")

    for field, allowed in spec.get("enums", {}).items():
        if field in given and str(given[field]) not in allowed:
            raise KieError(f"{field}={given[field]!r} is not one of "
                           f"{list(allowed)} for {model}")
    for field, (lo, hi) in spec.get("ranges", {}).items():
        if field not in given:
            continue
        value = given[field]
        # A string field's "range" is its length. Same table, because the
        # reference states both as bounds and a second one would drift.
        measure = len(value) if isinstance(value, str) else value
        try:
            measure = float(measure)
        except (TypeError, ValueError):
            raise KieError(f"{field} must be a number for {model}") from None
        if not lo <= measure <= hi:
            unit = " characters" if isinstance(value, str) else ""
            raise KieError(f"{field} must be {lo}..{hi}{unit} for {model}, "
                           f"got {measure:g}")
    for field, cap in spec.get("caps", {}).items():
        if field in given and len(given[field]) > cap:
            raise KieError(f"{model} takes at most {cap} {field}, got "
                           f"{len(given[field])}")

    for field in ("input_urls", "image_url", "first_frame_url", "last_frame_url",
                  "reference_image_urls", "reference_video_urls",
                  "reference_audio_urls"):
        if field in given:
            _reject_local(given[field], field)
    return given


def submit(model: str, *, root: Any = None, callback: str = "",
           timeout: float = 60.0, **fields: Any) -> dict:
    """Start an image or video generation. Returns {task_id, model, kind}.

    SHAPE FIRST, KEY SECOND — the same ordering imageto3d.submit_3d settled on.
    Checking the key up top would answer a bad aspect ratio with "KIE_API_KEY not
    set", which names the wrong problem and makes every refusal above
    untestable without a live key.
    """
    spec = _spec(model)
    payload: dict[str, Any] = {"model": spec["model"],
                               "input": build_input(model, **fields)}
    if callback:
        payload["callBackUrl"] = callback

    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    got = _request(JOBS_CREATE, key, payload=payload, method="POST",
                   timeout=timeout)
    task_id = str(got.get("taskId") or "")
    if not task_id:
        raise KieError(f"kie accepted the request but returned no taskId: "
                       f"{str(got)[:200]}")
    return {"task_id": task_id, "model": model, "kind": spec["kind"],
            "kie_model": spec["model"]}


def record(task_id: str, *, root: Any = None, timeout: float = 30.0) -> dict:
    """One poll of a market job. Returns the record as kie sends it."""
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    return _request(JOBS_RECORD, key, params={"taskId": str(task_id)},
                    timeout=timeout)


def result_urls(rec: dict) -> list[str]:
    """The finished files, dug out of `resultJson`.

    `resultJson` IS A STRING. The reference's own examples are
    '{"resultUrls":["https://..."]}' and '{"resultObject":{...}}' — a caller that
    reads rec["resultJson"]["resultUrls"] gets a TypeError, and one that reads
    rec["resultUrls"] gets nothing at all. Both shapes are handled, and a
    resultObject with no files returns [] rather than pretending.
    """
    raw = rec.get("resultJson")
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, dict):
        return []
    urls = raw.get("resultUrls")
    if isinstance(urls, list):
        return [str(u) for u in urls if str(u).strip()]
    obj = raw.get("resultObject")
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, list) and value and isinstance(value[0], str):
                return [str(u) for u in value]
    return []


def poll(task_id: str, *, root: Any = None, timeout: float = 900.0,
         interval: float = 3.0) -> dict:
    """Wait for a market job to reach a terminal state.

    Bounded on purpose: a job that never finishes must fail the caller rather
    than hold a seat's agent forever. The backoff and the ceiling are kie's own
    advice — "start with 2-3 second intervals, increase gradually" and "stop
    polling after 10-15 minutes".
    """
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    deadline = time.monotonic() + max(5.0, float(timeout))
    wait = max(1.0, float(interval))
    last: dict = {}
    while time.monotonic() < deadline:
        last = record(task_id, root=root)
        state = str(last.get("state") or "")
        if state == JOB_DONE:
            return last
        if state in JOB_DEAD:
            raise KieError(
                f"kie job failed ({last.get('failCode') or 'no code'}): "
                f"{last.get('failMsg') or 'no reason given'}")
        if state and state not in JOB_RUNNING:
            # An unknown state is not an excuse to spin — say so and stop.
            raise KieError(f"kie job returned unknown state {state!r}")
        time.sleep(wait)
        wait = min(10.0, wait * 1.25)
    raise KieError(f"kie job {task_id} did not finish within {timeout:.0f}s "
                   f"(last state {last.get('state') or 'unknown'})")


# kie serves finished files from a Cloudflare-fronted host that BLOCKS
# urllib's default User-Agent. Measured against a real finished track:
#
#   no header  (Python-urllib/3.x) -> HTTP 403, body "error code: 1010"
#   User-Agent: Mozilla/5.0        -> HTTP 200, audio/mpeg, ID3 header
#   Bearer key, no User-Agent      -> HTTP 403  (so it is the UA, not auth)
#
# 1010 is Cloudflare's "browser integrity" refusal, which fires on known bot
# agents. Every generation would submit fine, finish fine, and then die at the
# download — after the credits were spent — with a 403 that looks like an auth
# problem and is not. So a browser-shaped UA is sent on file fetches. This is
# not evasion of a rate limit or a paywall: the file is ours, already paid for,
# and the URL was handed to us by the API for this purpose.
DOWNLOAD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) BuildersGate/1.0 "
               "(+https://github.com/Thepizzapie/BuildersGate)")


def download(url: str, out_path: str | os.PathLike[str], *,
             timeout: float = 300.0, accept: str = "*/*") -> int:
    """Fetch a finished file to disk. Returns bytes written."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(str(url), headers={"Accept": accept,
                                                    "User-Agent": DOWNLOAD_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:                        # noqa: BLE001
        hint = ""
        if exc.code == 403:
            hint = (" — a 403 here is usually the CDN refusing the request "
                    "shape rather than an expired link; kie's URLs live "
                    f"{SUNO_URL_TTL_DAYS} days")
        raise KieError(
            f"could not download the finished file: HTTP {exc.code}{hint}") from exc
    except Exception as exc:                                     # noqa: BLE001
        raise KieError(f"could not download the finished file: {exc}") from exc
    if not data:
        raise KieError("kie returned an empty file")
    out.write_bytes(data)
    return len(data)


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def _account(result: dict, root: Any, *, kind: str, logical_name: str = "",
             work_item_id: Optional[int] = None, detail: str = "") -> dict:
    """Write what this call cost to the ledger, if it can be known.

    Best effort by construction — losing a ledger row must never lose the file
    that was paid for; that is imagegen._account's rule and it holds here.

    THE HONEST GAP IS STATED ON THE RESULT. spend.record ignores a zero, so with
    no credit rate configured there is no row to write and the ledger silently
    under-counts a real charge. Rather than invent a dollar figure, the result
    says `accounted: false` and carries the credit count, so the number is
    recoverable and the omission is visible.
    """
    result["credits_consumed"] = result.get("credits_consumed")
    usd = result.get("estimated_usd")
    if not root or not result.get("ok") or not usd:
        result["accounted"] = False
        if result.get("ok") and not usd:
            result["cost_note"] = PRICE_NOTE
        return result
    try:
        from bgate_core import spend

        spend.record(root, float(usd), kind=kind, work_item_id=work_item_id,
                     logical_name=logical_name or "",
                     detail=detail or f"kie {kind}",
                     model=str(result.get("model") or ""))
        result["accounted"] = True
    except Exception:                                            # noqa: BLE001
        result["accounted"] = False
    return result


def _finish(rec: dict, *, model: str, kind: str) -> dict:
    """The bits of a completed record every generate() reports the same way."""
    consumed = rec.get("creditsConsumed")
    return {
        "provider": "kie", "model": model, "kind": kind,
        "task_id": str(rec.get("taskId") or ""),
        "credits_consumed": consumed,
        "estimated_usd": cost_usd(consumed),
    }


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def generate_image(prompt: str, out_path: str | os.PathLike[str], *,
                   model: str = DEFAULT_IMAGE_MODEL, size: str = "",
                   aspect_ratio: str = "", seed: Optional[int] = None,
                   image_urls: Optional[list] = None, root: Any = None,
                   logical_name: str = "", work_item_id: Optional[int] = None,
                   timeout: float = 300.0, task_kind: str = "",
                   tileable: bool = False, **extra: Any) -> dict:
    """Submit, wait, download. Shaped to match imagegen/krea's result exactly —
    {ok, path, bytes, seconds, estimated_usd} — so the art pipeline does not
    care which provider produced the file.

    ``size`` is WxH because that is what the rest of this codebase speaks; it is
    translated to the nearest aspect ratio the chosen model accepts, the way
    krea.aspect_for does. Passing ``aspect_ratio`` directly wins.
    """
    started = time.monotonic()
    spec = MODELS.get(model)
    base = {"ok": False, "provider": "kie", "model": model, "kind": "image",
            "estimated_usd": None}
    if spec is None or spec["kind"] != "image":
        return {**base, "error": f"{model!r} is not a kie image model — "
                                 f"known: {sorted(IMAGE_MODELS)}",
                "seconds": 0.0}

    from bgate_adapters.imagegen import make_tileable, size_for

    fields: dict[str, Any] = dict(extra)
    fields["prompt"] = prompt
    ratio = aspect_ratio or ""
    if not ratio and size:
        ratio = aspect_for(size_for(size, task_kind=task_kind),
                           spec.get("enums", {}).get("aspect_ratio", ()))
    if ratio and "aspect_ratio" in spec["supports"]:
        fields["aspect_ratio"] = ratio
    if seed is not None and "seed" in spec["supports"]:
        fields["seed"] = int(seed)
    if image_urls:
        field = spec.get("images")
        if not field:
            return {**base, "seconds": 0.0,
                    "error": f"{model} takes no reference images — the kie "
                             f"models that do are "
                             f"{sorted(k for k, v in MODELS.items() if v.get('images'))}"}
        fields[field] = (list(image_urls) if spec.get("images_list")
                         else str(image_urls[0]))

    task_id = ""
    try:
        job = submit(model, root=root, timeout=60.0, **fields)
        task_id = job["task_id"]
        rec = poll(task_id, root=root, timeout=timeout)
        urls = result_urls(rec)
        if not urls:
            raise KieError("kie reported success with no result URL — the "
                           f"record's resultJson was {str(rec.get('resultJson'))[:300]}")
        written = download(urls[0], out_path, accept="image/*")
    except KieError as exc:
        # CARRY THE TASK. A completed job is paid for whether or not the
        # download worked; krea.generate_3d learned this by losing one.
        return {**base, "error": str(exc), "task_id": task_id,
                "recover": (f"poll {JOBS_RECORD}?taskId={task_id} — the job may "
                            "be finished and already charged") if task_id else "",
                "seconds": round(time.monotonic() - started, 2)}

    result = {**_finish(rec, model=model, kind="image"), "ok": True,
              "path": str(out_path), "bytes": written, "url": urls[0],
              "seconds": round(time.monotonic() - started, 2)}
    if tileable:
        # After the download, never instead of it: the image is already paid
        # for, so a post-pass that cannot run must degrade to a note.
        result["tileable"] = make_tileable(str(out_path))
    return _account(result, root, kind="image", logical_name=logical_name,
                    work_item_id=work_item_id,
                    detail=f"kie image ({model})")


def aspect_for(size: str, allowed) -> str:
    """WxH -> the nearest aspect ratio this model accepts.

    Per model, like krea.aspect_for and for the same reason: nano-banana takes
    5:4 and 21:9, flux-2 does not, and sending one it does not know is a 422.
    """
    allowed = tuple(a for a in (allowed or ()) if ":" in a)
    if not allowed:
        return ""
    try:
        w, h = (int(v) for v in str(size).lower().split("x"))
    except Exception:                                            # noqa: BLE001
        return "1:1" if "1:1" in allowed else allowed[0]
    if not w or not h:
        return "1:1" if "1:1" in allowed else allowed[0]
    target = w / h
    best, gap = allowed[0], 1e9
    for a in allowed:
        try:
            aw, ah = (float(v) for v in a.split(":"))
        except ValueError:
            continue
        d = abs(target - aw / ah)
        if d < gap:
            best, gap = a, d
    return best


# ---------------------------------------------------------------------------
# Video — new ground for this codebase
# ---------------------------------------------------------------------------

def generate_video(prompt: str, out_path: str | os.PathLike[str], *,
                   model: str = DEFAULT_VIDEO_MODEL, duration: Optional[int] = None,
                   resolution: str = "", aspect_ratio: str = "",
                   first_frame_url: str = "", last_frame_url: str = "",
                   reference_image_urls: Optional[list] = None,
                   generate_audio: Optional[bool] = None,
                   root: Any = None, logical_name: str = "",
                   work_item_id: Optional[int] = None,
                   timeout: float = 1800.0, **extra: Any) -> dict:
    """Submit, wait, download one clip. The video twin of :func:`generate_image`.

    THE TIMEOUT DEFAULTS TO HALF AN HOUR because a video job runs in minutes
    where an image runs in seconds, and kie's own guidance is to stop polling at
    10-15 minutes on the assumption of a callback. There is no callback surface
    in this product yet, so the loop has to outlast the job.

    NOTHING DOWNSTREAM CONSUMES A VIDEO YET. There is no importer, no gate and
    no manifest kind for one — this returns a file on disk and says so. Treat it
    as a reference clip a human watches, not as an asset the pipeline can carry.
    """
    started = time.monotonic()
    spec = MODELS.get(model)
    base = {"ok": False, "provider": "kie", "model": model, "kind": "video",
            "estimated_usd": None}
    if spec is None or spec["kind"] != "video":
        return {**base, "error": f"{model!r} is not a kie video model — "
                                 f"known: {sorted(VIDEO_MODELS)}",
                "seconds": 0.0}

    fields: dict[str, Any] = dict(extra)
    fields["prompt"] = prompt
    if duration is not None:
        fields["duration"] = int(duration)
    if resolution:
        fields["resolution"] = resolution
    if aspect_ratio:
        fields["aspect_ratio"] = aspect_ratio
    if first_frame_url:
        fields["first_frame_url"] = first_frame_url
    if last_frame_url:
        fields["last_frame_url"] = last_frame_url
    if reference_image_urls:
        fields["reference_image_urls"] = list(reference_image_urls)
    if generate_audio is not None:
        fields["generate_audio"] = bool(generate_audio)

    task_id = ""
    try:
        job = submit(model, root=root, timeout=60.0, **fields)
        task_id = job["task_id"]
        rec = poll(task_id, root=root, timeout=timeout, interval=5.0)
        urls = result_urls(rec)
        if not urls:
            raise KieError("kie reported success with no result URL — the "
                           f"record's resultJson was {str(rec.get('resultJson'))[:300]}")
        written = download(urls[0], out_path, accept="video/*", timeout=600.0)
    except KieError as exc:
        return {**base, "error": str(exc), "task_id": task_id,
                "recover": (f"poll {JOBS_RECORD}?taskId={task_id} — the job may "
                            "be finished and already charged") if task_id else "",
                "seconds": round(time.monotonic() - started, 2)}

    result = {**_finish(rec, model=model, kind="video"), "ok": True,
              "path": str(out_path), "bytes": written, "url": urls[0],
              "seconds": round(time.monotonic() - started, 2),
              "consumes": "nothing downstream imports video yet — this is a "
                          "reference clip for a human, not a pipeline asset"}
    return _account(result, root, kind="video", logical_name=logical_name,
                    work_item_id=work_item_id,
                    detail=f"kie video ({model})")


# ---------------------------------------------------------------------------
# Music — also new ground, and a different API
# ---------------------------------------------------------------------------

def music_limits(model: str = DEFAULT_SUNO_MODEL) -> dict:
    """Every ceiling that applies to one Suno model, in both modes.

    Exists so a UI can SHOW the limit instead of discovering it as a 422 after
    the user has typed 900 characters. The numbers come out of the same two
    tables :func:`build_music` enforces against — a second copy typed into a
    form is a second copy that goes stale the day V6 lands.
    """
    name = str(model or "").strip().upper() or DEFAULT_SUNO_MODEL
    if name not in SUNO_MODELS:
        raise KieError(f"unknown Suno model {name!r} — known: {list(SUNO_MODELS)}")
    return {
        "model": name,
        "custom": dict(SUNO_CUSTOM_LIMITS.get(name, SUNO_CUSTOM_DEFAULT)),
        "simple": {"prompt": SUNO_SIMPLE_PROMPT, "style": 0, "title": 0},
        # None, not a range, when the model does not take one — the caller must
        # be able to HIDE the control rather than offer a field that 422s.
        "duration": (list(SUNO_DURATION_RANGE)
                     if name in SUNO_DURATION_MODELS else None),
    }


def music_options() -> dict:
    """The whole Suno surface as data, for a form or a tool description."""
    return {
        "models": list(SUNO_MODELS),
        "default_model": DEFAULT_SUNO_MODEL,
        "limits": {name: music_limits(name) for name in SUNO_MODELS},
        "weights": list(SUNO_WEIGHTS),
        "duration_models": list(SUNO_DURATION_MODELS),
        "duration_range": list(SUNO_DURATION_RANGE),
        "vocal_genders": ["m", "f"],
        "statuses": list(SUNO_STATUSES),
        "tracks_hint": SUNO_TRACKS_HINT,
        "retention_days": SUNO_URL_TTL_DAYS,
        # A game default, not an API one — see build_music.
        "instrumental_default": True,
        "usd_per_credit": usd_per_credit(),
        "price_note": PRICE_NOTE,
        "callback_note": SUNO_CALLBACK_NOTE,
    }


def build_music(prompt: str, *, model: str = DEFAULT_SUNO_MODEL,
                custom: bool = False, instrumental: bool = True,
                style: str = "", title: str = "", negative_tags: str = "",
                vocal_gender: str = "", duration: Optional[int] = None,
                callback: str = "", **weights: Any) -> dict:
    """The /api/v1/generate body, validated against Suno's own limits.

    ``instrumental`` DEFAULTS TO TRUE here where Suno requires it explicitly.
    That is a game-tool default, not an API one: background music with a
    vocalist singing over the dialogue is the wrong asset almost every time, and
    a caller that wants a title song can say so.
    """
    model = str(model).strip().upper()
    if model not in SUNO_MODELS:
        raise KieError(f"unknown Suno model {model!r} — known: {list(SUNO_MODELS)}")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise KieError("a music generation needs a prompt")

    limits = (SUNO_CUSTOM_LIMITS.get(model, SUNO_CUSTOM_DEFAULT) if custom
              else {"prompt": SUNO_SIMPLE_PROMPT, "style": 0, "title": 0})
    if len(prompt) > limits["prompt"]:
        raise KieError(
            f"prompt is {len(prompt)} characters; {model} allows "
            f"{limits['prompt']} in "
            + ("custom" if custom else "simple")
            + " mode" + ("" if custom else " — pass custom=True for the longer "
                                           "ceiling and for style/title"))

    payload: dict[str, Any] = {
        "prompt": prompt, "customMode": bool(custom),
        "instrumental": bool(instrumental), "model": model,
    }
    if custom:
        # In custom mode `style` and `title` are how Suno is steered at all, so
        # a caller that turns custom on and passes neither has asked for the
        # long prompt ceiling and nothing else. That is legal; it is just worth
        # not silently dropping the two fields when they ARE given.
        if style:
            if len(style) > limits["style"]:
                raise KieError(f"style is {len(style)} characters; {model} "
                               f"allows {limits['style']}")
            payload["style"] = style
        if title:
            if len(title) > limits["title"]:
                raise KieError(f"title is {len(title)} characters; Suno allows "
                               f"{limits['title']}")
            payload["title"] = title
    elif style or title:
        raise KieError("style and title only apply in custom mode — pass "
                       "custom=True, or fold them into the prompt")

    if negative_tags:
        payload["negativeTags"] = str(negative_tags)
    if vocal_gender:
        if vocal_gender not in ("m", "f"):
            raise KieError("vocalGender is 'm' or 'f'")
        if instrumental:
            raise KieError("vocalGender on an instrumental track has nothing to "
                           "apply to — pass instrumental=False")
        payload["vocalGender"] = vocal_gender
    if duration is not None:
        if model not in SUNO_DURATION_MODELS:
            raise KieError(f"duration is {'/'.join(SUNO_DURATION_MODELS)} only "
                           f"— {model} does not take it and would be charged "
                           "for its default length")
        lo, hi = SUNO_DURATION_RANGE
        if not lo <= int(duration) <= hi:
            raise KieError(f"duration must be {lo}..{hi} seconds")
        payload["duration"] = int(duration)
    for name in SUNO_WEIGHTS:
        value = weights.pop(name, None)
        if value is not None:
            payload[name] = round(max(0.0, min(1.0, float(value))), 2)
    for name in ("personaId", "personaModel"):
        value = weights.pop(name, None)
        if value:
            payload[name] = str(value)
    if weights:
        raise KieError(f"unknown Suno field(s): {', '.join(sorted(weights))}")
    # ALWAYS sent — see SUNO_CALLBACK_NOTE. Omitting it is a hard 422 from the
    # live API, whatever the quickstart's example shows.
    payload["callBackUrl"] = callback_url(callback)
    return payload


def submit_music(prompt: str, *, root: Any = None, timeout: float = 60.0,
                 **options: Any) -> dict:
    """Start a music generation. Returns {task_id}.

    Shape first, key second — same ordering as :func:`submit`, same reason.
    """
    payload = build_music(prompt, **options)
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    try:
        got = _request(SUNO_CREATE, key, payload=payload, method="POST",
                       timeout=timeout)
    except KieError as exc:
        # The one refusal whose cause is documented two ways. See
        # SUNO_CALLBACK_NOTE — say it here rather than let the next person
        # rediscover the contradiction from a 422 that names a field they were
        # told was optional.
        if "422" in str(exc) and "callBackUrl" not in payload:
            raise KieError(f"{exc} — {SUNO_CALLBACK_NOTE}") from exc
        raise
    task_id = str(got.get("taskId") or "")
    if not task_id:
        raise KieError(f"kie accepted the music request but returned no "
                       f"taskId: {str(got)[:200]}")
    return {"task_id": task_id, "model": payload["model"]}


def music_record(task_id: str, *, root: Any = None,
                 timeout: float = 30.0) -> dict:
    """One poll of a Suno task. A DIFFERENT endpoint from :func:`record`."""
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    return _request(SUNO_RECORD, key, params={"taskId": str(task_id)},
                    timeout=timeout)


def music_tracks(rec: dict) -> list[dict]:
    """The finished tracks: [{id, audioUrl, title, duration, imageUrl}].

    Real nested JSON under data.response.sunoData — NOT the string-encoded
    `resultJson` the market API uses. The two surfaces do not share a result
    shape and a helper that pretended they did would be wrong for one of them.
    """
    response = rec.get("response")
    items = (response or {}).get("sunoData") if isinstance(response, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("audioUrl") or "")
        if not url:
            continue
        out.append({"id": str(item.get("id") or ""), "audio_url": url,
                    "title": str(item.get("title") or ""),
                    "duration": item.get("duration"),
                    # WHICH MODEL ACTUALLY MADE IT. Dropping this was fine while
                    # the only caller already knew what it had asked for; a
                    # recovery does not — it is handed a task id and nothing
                    # else, so without this the take is filed with no model and
                    # the card shows a question mark.
                    "model_name": str(item.get("modelName") or ""),
                    "tags": str(item.get("tags") or ""),
                    "image_url": str(item.get("imageUrl") or "")})
    return out


def poll_music(task_id: str, *, root: Any = None, timeout: float = 900.0,
               interval: float = 3.0, on_progress: Any = None) -> dict:
    """Wait for every track, not the first one.

    TEXT_SUCCESS and FIRST_SUCCESS are progress, not completion — Suno streams
    lyrics, then track one, then the rest. Stopping at FIRST_SUCCESS returns
    half of a request that was billed in full, which is exactly the mistake
    krea.RUNNING's `intermediate-complete` entry exists to prevent there.

    ``on_progress(fraction, stage, status)`` IS CALLED ON EVERY CHANGE, and it
    is the difference between a minute of silence and a minute of waiting.
    Suno reports where it is; this loop used to know and tell nobody, so the UI
    could only draw a spinner — and a spinner that has not moved for ninety
    seconds is indistinguishable from a hang, which is what makes people fire a
    second paid generation at the same prompt.

    RAISING FROM on_progress IS HOW A CALLER CANCELS. There is no other way to
    stop this loop, and the exception travels out of poll_music to whoever asked
    — with the task id already in hand, so what was paid for is recoverable.
    """
    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])
    deadline = time.monotonic() + max(5.0, float(timeout))
    wait = max(1.0, float(interval))
    last: dict = {}
    seen = ""

    def announce(state: str) -> None:
        nonlocal seen
        if not on_progress or state == seen:
            return
        seen = state
        fraction, words = SUNO_STAGE.get(state, (0.25, f"Suno reports {state}"))
        on_progress(fraction, words, state)

    while time.monotonic() < deadline:
        last = music_record(task_id, root=root)
        status = str(last.get("status") or "").upper()
        announce(status or "PENDING")
        if status == SUNO_DONE:
            return last
        if status == SUNO_CALLBACK_FAILED:
            # kie could not deliver its webhook. That is a fact about the
            # NOTIFICATION, not about the audio — and the audio is already
            # charged for. If the record carries tracks this run succeeded and
            # only the doorbell broke; if it does not, there is nothing to save.
            if music_tracks(last):
                return {**last, "callback_failed": True}
            raise KieError(
                f"kie/Suno job {status} and the record carries no audio"
                + (f": {last.get('errorMessage')}"
                   if last.get("errorMessage") else "")
                + " — kie failed to deliver its callback and produced nothing "
                  "to recover")
        if status in SUNO_DEAD:
            hint = (" — the prompt tripped Suno's content filter; reword it"
                    if status == "SENSITIVE_WORD_ERROR" else "")
            raise KieError(f"kie/Suno job {status}"
                           + (f": {last.get('errorMessage')}"
                              if last.get("errorMessage") else "")
                           + hint)
        if status and status not in SUNO_RUNNING:
            raise KieError(f"kie/Suno returned unknown status {status!r}")
        time.sleep(wait)
        wait = min(10.0, wait * 1.25)
    raise KieError(f"kie/Suno task {task_id} did not finish within "
                   f"{timeout:.0f}s (last status {last.get('status') or 'unknown'})")


def download_tracks(tracks: list, out_dir: str | os.PathLike[str], *,
                    stem: str = "track", on_progress: Any = None) -> list[dict]:
    """Pull every track in a record onto disk. Returns them with path + bytes.

    SHARED BY THE TWO WAYS A BATCH ARRIVES — the generation that waited for it,
    and the recovery of one whose download died after the credits were spent.
    That second path is not hypothetical: kie's CDN answered urllib's default
    User-Agent with a 403 for every generation this product made, so every
    batch finished, was charged, and was then thrown away at exactly this step.
    A downloader with one caller would have had to be written twice.
    """
    target = Path(out_dir)
    total = len(tracks)
    written = []
    for index, track in enumerate(tracks, start=1):
        url = str(track.get("audio_url") or track.get("audioUrl") or "")
        if not url:
            continue
        if on_progress:
            on_progress(0.80 + 0.15 * (index - 1) / max(1, total),
                        f"downloading take {index} of {total}", SUNO_DONE)
        suffix = Path(urllib.parse.urlparse(url).path).suffix
        path = target / f"{stem}_{index}{suffix or '.mp3'}"
        size = download(url, path, accept="audio/*")
        written.append({**track, "path": str(path), "bytes": size})
    return written


def generate_music(prompt: str, out_dir: str | os.PathLike[str], *,
                   name: str = "", root: Any = None, logical_name: str = "",
                   work_item_id: Optional[int] = None,
                   timeout: float = 900.0, **options: Any) -> dict:
    """Submit, wait, download every track. Returns {ok, tracks:[{path,...}]}.

    ONE REQUEST IS SEVERAL FILES, which is why this returns a list where the
    image path returns a path. Suno generates "multiple variations for each
    request" and the reference never says how many — so the caller is handed
    what actually arrived rather than a first-track shape that silently discards
    the rest of what it paid for.

    THE DOWNLOAD HAPPENS INSIDE THIS CALL for the same reason imageto3d's Tripo
    path does it: kie retains generated files for fourteen days, so a URL filed
    on the board and fetched later is a dead link with a deadline.

    WHAT IT COST IS MEASURED, NOT ESTIMATED — and it may still be unknown. The
    Suno record has NO `creditsConsumed`; that field belongs to the market jobs
    surface and this is the other API. The only number kie will tell us about a
    music run is the account balance, so the balance is read either side of the
    call and the difference is the charge. That is a MEASUREMENT WITH A CAVEAT,
    recorded as such: another generation running on the same key at the same
    time lands in the same delta. So the result carries `credits_source` and a
    caller comparing two runs can see which number it is looking at. When the
    balance cannot be read at all the credits stay None and `accounted` is
    false — never 0.0, which every budget check in this product reads as free.
    """
    started = time.monotonic()
    base = {"ok": False, "provider": "kie", "kind": "audio",
            "model": str(options.get("model") or DEFAULT_SUNO_MODEL),
            "estimated_usd": None}
    task_id = ""
    # BEFORE THE SUBMIT, not before the download: the charge lands when the job
    # is accepted. Best effort — a balance that cannot be read must not stop a
    # generation, it only makes the run unpriced.
    before = credit_balance(root)
    on_progress = options.pop("on_progress", None)
    try:
        # INSIDE the try. This announcement sat above it, so a callback that
        # raised here — which is exactly what pressing cancel does — escaped
        # uncaught instead of returning the cancelled result every other stage
        # produces. Cancelling in the first second was the one moment the
        # feature crashed, and it is the likeliest moment for someone to change
        # their mind. Nothing has been submitted yet at this point, so the
        # result carries no task id and is correctly NOT recoverable: there is
        # nothing to recover and nothing was charged.
        if on_progress:
            on_progress(*SUNO_STAGE_SUBMIT, "")
        job = submit_music(prompt, root=root, **options)
        task_id = job["task_id"]
        base["model"] = job["model"]
        rec = poll_music(task_id, root=root, timeout=timeout,
                         on_progress=on_progress)
        tracks = music_tracks(rec)
        if not tracks:
            raise KieError("kie/Suno reported SUCCESS with no audio URL — the "
                           f"record was {str(rec)[:300]}")
        stem = (name or logical_name or "track").strip() or "track"
        written = download_tracks(tracks, out_dir, stem=stem,
                                  on_progress=on_progress)
    except (KieError, MusicCancelled) as exc:
        # THE TASK ID TRAVELS OUT WITH THE FAILURE, always. Whatever went wrong
        # after the submit — a download, a timeout, a human pressing cancel —
        # the batch was paid for and kie holds it for fourteen days. Losing the
        # id here is losing the money; carrying it makes the failure recoverable
        # by a single call (see recover(), and the note below).
        return {**base, "error": str(exc), "task_id": task_id,
                "cancelled": isinstance(exc, MusicCancelled),
                "recoverable": bool(task_id),
                "recover": (f"the tracks may already exist and be charged for — "
                            f"recover them with task_id {task_id}; kie keeps "
                            f"them for {SUNO_URL_TTL_DAYS} days")
                           if task_id else "",
                "seconds": round(time.monotonic() - started, 2)}

    after = credit_balance(root)
    spent: Optional[float] = None
    source = "unavailable"
    if before is not None and after is not None:
        delta = before - after
        if delta > 0:
            spent, source = delta, "balance_delta"
        else:
            # A zero or negative delta is not "it was free" — it is a top-up
            # mid-run, a cached balance, or an account this key does not own.
            source = "balance_delta_unusable"

    result = {**base, "ok": True, "task_id": task_id, "tracks": written,
              "count": len(written),
              "credits_consumed": spent,
              "credits_source": source,
              "credits_note":
                  "the Suno record carries no creditsConsumed field — this is "
                  "the account balance before minus after, so a concurrent "
                  "generation on the same key would be counted here too",
              "estimated_usd": cost_usd(spent),
              "retention_days": SUNO_URL_TTL_DAYS,
              "expires_at": _expires_at(SUNO_URL_TTL_DAYS),
              "callback_failed": bool(rec.get("callback_failed")),
              "seconds": round(time.monotonic() - started, 2)}
    return _account(result, root, kind="audio", logical_name=logical_name,
                    work_item_id=work_item_id, detail="kie music (Suno)")


def _expires_at(days: int) -> str:
    """When kie stops serving these URLs. Recorded so a stored provenance URL
    is stamped with its own death date rather than looking permanent."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc)
            + timedelta(days=int(days))).isoformat(timespec="seconds")


def doctor_row() -> dict:
    """One row for `bgate doctor`, in the optional-capability sense: absent means
    three paths are unavailable and nothing else breaks."""
    got = available()
    return {
        "name": "kie",
        "available": bool(got.get("available")),
        "detail": ("images, Suno music and Seedance video"
                   if got.get("available") else (got.get("reason") or "")),
        "optional": True,
    }
