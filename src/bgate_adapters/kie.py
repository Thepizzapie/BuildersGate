"""kie.ai — one key, three capabilities: images, Suno music, Seedance video.

WHY A THIRD IMAGE PROVIDER, AND WHY IT IS NOT ONLY AN IMAGE PROVIDER. Krea was
worth wiring because it is a front door to twenty models rather than one. kie is
worth wiring for a different reason: it is the first credential in this product
that reaches capabilities the pipeline has never had at all. Builders Gate can
mix audio (bgate_core.audio.audiolab) but has never GENERATED a note of it, and it has
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

MEASURING AFTER THE FACT IS NOT ENOUGH FOR A GATE, which is what VIDEO_CREDITS
and :func:`estimate_usd` are for. A budget check runs BEFORE the spend, so a
pipeline holding only a post-hoc number had to hand it projected_usd=0.0 and
could never refuse ONE expensive shot — only a project already over its ceiling.
The estimate is explicitly an upper bound derived from kie's published band
rather than a price, it says so on every value it returns, and a model it has no
rate for yields known=False and NO NUMBER: a fabricated figure in front of a
spend gate does not read as "unpriced", it reads as "free".

LOCAL FILES ARE SENT BY UPLOADING THEM FIRST — A THIRD API FAMILY. Every
reference field on a generation endpoint is a URI and none of them takes inline
bytes, so a pinned anchor on disk cannot go straight into a request; this module
used to stop there and say anchored generation through kie was unavailable. That
was a true statement about /api/v1/jobs and a false one about kie, which serves a
file-upload API from a DIFFERENT HOST (kieai.redpandaai.co) under the same Bearer
token. :func:`upload_file` posts base64 to it and gets back an https URL the
generation endpoints accept, and :func:`generate_video` now does that
automatically for any local path handed to a frame or reference field.

The uploaded copy DIES IN THREE DAYS (UPLOAD_TTL_DAYS), which is shorter than
Suno's fourteen and short enough that no minted URL may ever be cached or stored
as if it were an asset — every one is stamped with the day it stops working.
Krea remains the better anchor for a STILL, where a data URI travels inline with
no upload, no expiry and no copy left on someone else's disk; the upload path
exists because video has no such alternative anywhere in this product.

Everything is stdlib. No SDK, no new dependency.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from bgate_core.store import envfile

from . import _http, _result

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

# THE UPLOAD SURFACE, AND IT IS ON A DIFFERENT HOST. Not api.kie.ai — kie serves
# its file endpoints from kieai.redpandaai.co, which is why this is a full URL
# and not a path appended to API_BASE like everything else here. It takes the
# same Bearer token.
#
# WHAT THIS CHANGES, AND IT IS THE WHOLE REASON CINEMATICS ARE POSSIBLE. Every
# reference field on a kie model is documented as a URI, and every pinned anchor
# in this product is a file on disk, so this module's own docstring said flatly
# that anchored generation through kie is not available. That was true of the
# GENERATION endpoints and it was never true of kie as a whole: there is an
# upload API, it takes base64, and it hands back an https URL the generation
# endpoints accept. The limitation was one missing call, not a missing
# capability.
#
# It matters far more for video than it ever did for images, because there is a
# Krea to fall back to for an anchored still and there is nothing to fall back to
# for an anchored SHOT. A cutscene whose character cannot be conditioned on the
# approved character is a cutscene starring somebody else.
UPLOAD_BASE64 = "https://kieai.redpandaai.co/api/file-base64-upload"

# Three days, from kie's own file-upload reference. Shorter than Suno's fourteen
# and short enough to matter: a first-frame URL minted for a shot is dead by the
# time anyone re-runs that shot next week, so nothing may cache one. Callers get
# an `expires_at` and re-upload rather than reuse.
UPLOAD_TTL_DAYS = 3

# Where uploads are filed in the account's own space. One directory for this
# product so a user looking at kie's dashboard can tell what put them there.
UPLOAD_DIR = "images/builders-gate"

# What may be uploaded as a conditioning frame. Deliberately not "any file kie
# would take": these are the types every model here documents for its image
# fields, and a .bmp that uploads fine and 422s at generation costs a round trip
# to discover.
UPLOAD_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp"}


def _upload_mime(source: Path) -> str | None:
    """The MIME of a file, by suffix first and then by its own leading bytes.

    A pinned anchor is stored as `name.r1`: the revision number IS the
    extension. Keying off the suffix alone therefore refused every pin in the
    project, which is the exact thing this upload exists to carry, and the
    error read like a bad file rather than a naming convention. The magic bytes
    are the better authority anyway, because a .png that is really a JPEG
    uploads happily and then 422s at generation, after the round trip is spent.
    """
    mime = UPLOAD_MIME.get(source.suffix.lower())
    if mime:
        return mime
    try:
        with source.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None

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
    # THE ROW THE 1010 SHOULD HAVE HAD. Cloudflare fronts both kie hosts and
    # answers a banned agent with HTTP 403 and a body reading "error code:
    # 1010" — no `code` field, so it never reaches _envelope and lands here
    # instead, where the table had nothing to say. Every anchored shot died on
    # file-base64-upload with a bare "kie HTTP 403", which reads as an auth
    # problem and is not: the key was valid and the request was refused for its
    # User-Agent. _request now always sends one (see DOWNLOAD_UA there); this
    # row is what tells the next person what a 403 means if it ever comes back.
    403: "Cloudflare refused the request, not kie — a body reading 'error code: "
         "1010' is the browser-integrity rule firing on the User-Agent, NOT a "
         "bad key. Every request from this module sends a browser-shaped one; a "
         "403 here means something bypassed _request or the rule widened",
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


class KieError(_http.ProviderError):
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
                "for anchored character work. Use nano-banana-2 for that.",
    },
    # READ OFF THE REFERENCE PAGES, NOT GUESSED, and the guess would have been
    # wrong twice over. The docs for this model live at
    # /market/google/nanobanana2 rather than the /nano-banana-2 the URL pattern
    # predicts, and BOTH new Google models take a BARE model id where the older
    # entry above takes "google/nano-banana" - so "google/nano-banana-2" is a
    # 404 after a round trip. Verified against both pages before adding.
    #
    # WHY IT MATTERS BEYOND BEING NEWER: 14 reference images. The note above
    # sends anchored character work elsewhere because nano-banana has no
    # reference conditioning at all, and a consistent cast is exactly the job
    # that needs it.
    "nano-banana-2": {
        "model": "nano-banana-2",
        "kind": "image",
        "label": "Google Nano Banana 2 (Gemini 3.1 Flash Image)",
        "required": ("prompt",),
        "supports": {"prompt", "image_input", "aspect_ratio", "resolution",
                     "output_format"},
        "enums": {
            "output_format": ("png", "jpg"),
            "resolution": ("1K", "2K", "4K"),
            "aspect_ratio": ("1:1", "2:3", "3:2", "1:4", "4:1", "3:4", "4:3",
                             "4:5", "5:4", "1:8", "8:1", "9:16", "16:9", "21:9",
                             "auto"),
        },
        "ranges": {"prompt": (1, 20000)},
        "caps": {"image_input": 14},
        "images": "image_input",
        "images_list": True,
        "credits": None,
        "note": "Text-to-image AND reference-conditioned editing, up to 14 "
                "reference images. The one to reach for when a cast has to stay "
                "consistent across many generations.",
    },
    # READ OFF ITS OWN DOC PAGE, and the id is why every guess failed: the
    # family name is not the model, "bytedance/seedream-v4-text-to-image" is,
    # with the task spelled out in the id. Probing "seedream-4.5",
    # "bytedance/seedream-4.5" and friends returned 422 "model name not
    # supported" for all of them, which reads like a shape complaint and is
    # not one.
    #
    # WHY IT IS HERE: it is the text-to-image model that is NOT Google's. The
    # nano-banana family shares one safety filter, and that filter refuses
    # plain material descriptions at random — an office carpet three times in
    # a row, the same words that had generated minutes earlier. A texture
    # pipeline cannot be built on a coin toss, so tileset_generate reaches
    # for this first.
    "seedream-4-t2i": {
        "model": "bytedance/seedream-v4-text-to-image",
        "kind": "image",
        "label": "ByteDance Seedream 4.0 (text to image)",
        "required": ("prompt",),
        "supports": {"prompt", "image_size", "image_resolution", "max_images",
                     "seed", "nsfw_checker"},
        "enums": {
            "image_size": ("square_hd", "square", "portrait_4_3",
                           "portrait_16_9", "landscape_4_3",
                           "landscape_16_9"),
            "image_resolution": ("1K", "2K", "4K"),
        },
        "ranges": {"max_images": (1, 6)},
        "caps": {},
        "images": "",
        "credits": None,
        "note": "Text-to-image, prompt only. The non-Google option, which is "
                "what makes it the default for material and texture work.",
    },
    "nano-banana-pro": {
        "model": "nano-banana-pro",
        "kind": "image",
        "label": "Google Nano Banana Pro (Gemini 3 Pro Image)",
        "required": ("prompt",),
        "supports": {"prompt", "image_input", "aspect_ratio", "resolution",
                     "output_format"},
        "enums": {
            "output_format": ("png", "jpg"),
            "resolution": ("1K", "2K", "4K"),
            "aspect_ratio": ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
                             "9:16", "16:9", "21:9", "auto"),
        },
        # Shorter prompt ceiling and fewer references than nano-banana-2, both
        # off its own page. Neither is a typo of the other.
        "ranges": {"prompt": (1, 10000)},
        "caps": {"image_input": 8},
        "images": "image_input",
        "images_list": True,
        "credits": None,
        "note": "The Pro tier. Fewer references than nano-banana-2 (8 against "
                "14) and a shorter prompt ceiling, so it is not a strict "
                "upgrade for cast work.",
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
        # kie answers "resolution is required" with a 500 despite its reference
        # listing this as optional. Measured, not guessed.
        "defaults": {"resolution": "2K"},
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
        # THE INTENT MAP — see VIDEO_INTENT below for why this exists.
        "intent": {
            "seconds": "duration",
            "shape": "aspect_ratio",
            "quality": "resolution",
            "first_frame": "first_frame_url",
            "last_frame": "last_frame_url",
            "refs": "reference_image_urls",
            "audio": "generate_audio",
        },
        # SEEDANCE TAKES AN ANCHOR FRAME OR REFERENCE IMAGES, NEVER BOTH. Its
        # own words on the 422: "The reference image and the first and last
        # frames are mutually exclusive, and only one scene can be selected."
        # Nothing in supports/caps/ranges can express that — they describe
        # fields one at a time — so a shot list carrying a storyboard still AND
        # a pinned cast built a payload that is individually valid in every
        # field and refused as a whole, after the anchors had been uploaded.
        #
        # Ordered most-specific-first: the group named first is the one KEPT.
        # first_frame wins over refs because it is a composed, approved still of
        # this exact beat that was itself drawn against the cast, so the
        # characters are already inside it; dropping the anchor to keep the
        # references would throw away the framing to re-state something the
        # frame already says.
        "exclusive": ((("first_frame", "last_frame"), ("refs",)),),
    },
}

# ---------------------------------------------------------------------------
# THE INTENT VOCABULARY, and why a second video model made it necessary.
#
# bgate_core.cine.cinematic used to call generate_video(duration=..., resolution=...,
# aspect_ratio=..., first_frame_url=...). Those are not "the video parameters" —
# they are SEEDANCE'S parameters, and a pipeline that speaks them is a pipeline
# with exactly one model in it forever.
#
# MEASURED AGAINST kie's OWN CATALOGUE: Sora 2's market entry takes `n_frames`
# where Seedance takes `duration`, and its aspect ratio is the WORD "landscape"
# where Seedance wants the ratio "16:9". Same capability, same API family, same
# vendor — three incompatible spellings. A caller passing Seedance's names to it
# gets `build_input`'s unknown-key refusal at best and a silently ignored setting
# it was charged for at worst.
#
# So the pipeline speaks INTENT — how long, what shape, how sharp, what to open
# on — and each model's table entry says what it calls those. Adding a model is
# still a table entry; it is now a table entry that the rest of the product
# needs no knowledge of.
#
# WHAT IS DELIBERATELY NOT HERE: any model whose reference page has not been
# read. That rule predates this map and this map does not weaken it — the ids
# and the spellings above ARE the thing that has to be verified, and inventing
# an intent map is inventing the model. `register_video_model` is the door for a
# model a USER has read the page for; see it for why that is not the same thing
# as guessing.
VIDEO_INTENT = ("seconds", "shape", "quality", "first_frame", "last_frame",
                "refs", "audio")

def video_input(model: str, **intent: Any) -> dict:
    """Translate intent into one model's own field names and value vocabulary.

    Unknown intent keys are refused rather than dropped, for the reason
    build_input already gives about unsupported inputs: a silently ignored
    setting still bills you and leaves nothing saying why it did not apply. An
    intent this model has NO field for is also refused, and that is the honest
    answer — "this model cannot be told how long to be" is information the caller
    needs before it spends, not after.

    Three declarative transforms, each earned by a real difference in kie's own
    catalogue rather than invented for symmetry:
      * ``intent``        name -> the model's field name
      * ``intent_values`` name -> {canonical value: this model's spelling}
      * ``intent_scale``  name -> multiplier (seconds -> frame counts)
    """
    spec = _spec(model)
    if spec["kind"] != "video":
        raise KieError(f"{model!r} is not a video model — "
                       f"known: {sorted(VIDEO_MODELS)}")
    table = spec.get("intent") or {}
    if not table:
        raise KieError(
            f"{model!r} is registered without an intent map, so nothing can "
            "tell what it calls duration or aspect ratio. Re-register it with "
            "`intent` filled in from its reference page.")

    unknown = sorted(set(intent) - set(VIDEO_INTENT))
    if unknown:
        raise KieError(f"unknown intent {', '.join(unknown)} — the vocabulary "
                       f"is {', '.join(VIDEO_INTENT)}")

    out: dict[str, Any] = {}
    for name, value in intent.items():
        if value is None or value == "" or value == []:
            continue
        field = table.get(name)
        if not field:
            raise KieError(
                f"{model} has no parameter for {name!r} — it accepts "
                f"{', '.join(sorted(table))}. Asking for it anyway would be a "
                "setting you paid for and did not get.")
        if name in spec.get("intent_scale", {}):
            value = int(round(float(value) * spec["intent_scale"][name]))
        mapped = (spec.get("intent_values") or {}).get(name)
        if mapped:
            if str(value) not in mapped:
                raise KieError(
                    f"{model} cannot do {name}={value!r} — it offers "
                    f"{sorted(mapped)}")
            value = mapped[str(value)]
        out[field] = value
    return out


def image_ref_cap(model: str) -> int:
    """How many reference images this IMAGE model accepts. 0 if it takes none.

    Read from the model's own table entry rather than assumed, because the two
    shapes here are genuinely different: `qwen-edit` takes a single `image_url`,
    while a list-shaped model declares `reference_image_urls` with a cap. A
    caller that guessed "probably a list" would build a payload the model
    rejects, after the upload has already happened.

    This exists so chroma can UPLOAD local anchors and condition a kie image on
    them. That path used to be refused outright on the grounds that kie's image
    fields are URIs and a pinned ref is a local file — true, but upload_file has
    always been able to bridge exactly that gap, and the refusal left a project
    whose only funded account is kie unable to draw an anchored frame at all.
    """
    spec = MODELS.get(model)
    if not spec or spec.get("kind") != "image":
        return 0
    field = str(spec.get("images") or "")
    if not field:
        return 0
    if spec.get("images_list"):
        return int((spec.get("caps") or {}).get(field, 0)) or 9
    return 1


def video_capabilities(model: str) -> dict:
    """What one video model can be asked for, in intent terms.

    A form or an agent choosing a model needs this BEFORE it plans a sequence:
    the seconds range is what decides how many shots a 90-second cutscene is,
    and it moves per model.
    """
    spec = _spec(model)
    table = spec.get("intent") or {}
    values, ranges = spec.get("intent_values") or {}, {}
    for name, field in table.items():
        if name in values:
            ranges[name] = sorted(values[name])
        elif field in spec.get("enums", {}):
            ranges[name] = list(spec["enums"][field])
        elif field in spec.get("ranges", {}):
            lo, hi = spec["ranges"][field]
            scale = (spec.get("intent_scale") or {}).get(name)
            ranges[name] = [lo / scale, hi / scale] if scale else [lo, hi]
    return {
        "model": model,
        "label": spec.get("label", model),
        "id": spec["model"],
        "supports": sorted(table),
        "options": ranges,
        "max_refs": (spec.get("caps") or {}).get(table.get("refs", ""), 0),
        # WHICH SETTINGS CANNOT RIDE TOGETHER. `options` answers one field at a
        # time and cannot express "an anchor frame OR reference images, never
        # both" — so a planner reading only that builds a shot list whose every
        # value is legal and whose payload is refused. Reported in intent names,
        # first group first, which is the one build_input keeps.
        "exclusive": [[list(g) for g in rule]
                      for rule in (spec.get("exclusive") or ())],
        "note": spec.get("note", ""),
        "source": spec.get("source", "built-in"),
        # WHETHER THE ID HAS EVER BEEN CONFIRMED AGAINST kie. Separate from
        # `source` because they answer different questions — source says who
        # typed it, this says whether anything checked it. See
        # :func:`register_video_model` for why an unchecked id is a paid 404.
        "verified": model_id_verified(model),
        "verified_note": ("" if model_id_verified(model) else UNVERIFIED_NOTE),
    }


# The stamp a registered model carries until something confirms its id exists.
# Nothing in this module can confirm one for free (see :func:`probe_model_id`),
# so this is the normal state of a user-registered entry rather than an alarm.
UNVERIFIED_NOTE = (
    "this model id has never been confirmed against the provider. kie serves no "
    "catalogue endpoint this adapter can read, so a typo in the id survives "
    "registration and surfaces as a PAID 404 at the first generation. Check it "
    "against the model's own page on docs.kie.ai, or call probe_model_id.")


def model_id_verified(model: str) -> bool:
    """Has this model's id been checked against kie, or only typed in?

    Built-in entries are True by construction: MODELS carries only ids read off
    their own reference page, which is the rule that table exists to keep.
    """
    spec = MODELS.get(str(model or "").strip()) or {}
    return bool(spec.get("verified", spec.get("source", "built-in") != "registered"))


def register_video_model(name: str, spec: dict) -> dict:
    """Add a video model at RUNTIME, from a reference page a human has read.

    WHY THIS EXISTS, AND WHY IT IS NOT A HOLE IN THE NO-GUESSING RULE. kie's
    market carries dozens of video models and this file can only carry the ones
    whose reference page was actually read. Before this, that ceiling was the
    PRODUCT's ceiling: a user looking at the Kling page in another tab, with the
    exact ids and enums in front of them, still could not use it without a
    release.

    The rule was never "few models". It was "nothing in here is a guess", and
    that rule is kept, not bent: this refuses a spec that does not carry its own
    ids, ranges and intent map, and every model registered through it is stamped
    `source: "registered"` so no surface can confuse a user's entry for a
    verified one. What it moves is WHO does the reading — which was always the
    honest answer, because the person with the page open knows more than this
    table does.

    WHAT THIS CANNOT CHECK, AND IT IS THE EXPENSIVE ONE. The `model` id is a
    string handed to kie and there is no catalogue endpoint here to hold it
    against — `available()` and `models()` read this file's own dicts. A typo
    therefore passes registration and is discovered at the first generation, as
    a 404 that arrives AFTER the conditioning frames have been uploaded. So
    every registered entry is stamped `verified: False` (see UNVERIFIED_NOTE)
    and everything that CAN be checked locally is, before the id is trusted:
    the shape of the id itself, the structure of every limit table, and whether
    this registration silently redefines a model something already planned
    against. Those are cheap; the 404 is not.
    """
    key = str(name or "").strip()
    if not key:
        raise KieError("a model needs a name")
    required = ("model", "intent")
    missing = [f for f in required if not spec.get(f)]
    if missing:
        raise KieError(
            f"cannot register {key!r} without {', '.join(missing)} — "
            "`model` is the literal id kie wants and `intent` says what this "
            "model calls seconds/shape/quality/first_frame/last_frame/refs/"
            "audio. Both come off the model's reference page; guessing either "
            "buys a 404 or a setting that silently did not apply.")
    if not isinstance(spec["intent"], dict):
        raise KieError(f"{key}: intent must be a mapping of "
                       f"{'/'.join(VIDEO_INTENT)} to this model's own field "
                       f"names, got {type(spec['intent']).__name__}")
    bad = sorted(set(spec["intent"]) - set(VIDEO_INTENT))
    if bad:
        raise KieError(f"{key}: unknown intent name(s) {bad} — the vocabulary "
                       f"is {', '.join(VIDEO_INTENT)}")

    model_id = _check_model_id(key, spec["model"])
    intent = _check_intent_fields(key, spec["intent"])
    supports = set(spec.get("supports")
                   or ({"prompt"} | set(intent.values())))
    supports = {str(s).strip() for s in supports if str(s).strip()}
    entry = {
        "model": model_id,
        "kind": "video",
        "label": str(spec.get("label") or key),
        "required": tuple(spec.get("required") or ("prompt",)),
        "supports": supports,
        "enums": _check_enums(key, spec.get("enums"), supports),
        "ranges": _check_ranges(key, spec.get("ranges"), supports),
        "caps": _check_caps(key, spec.get("caps"), supports),
        "images": str(spec.get("images") or intent.get("refs", "")),
        "images_list": bool(spec.get("images_list", True)),
        "credits": _rate_numbers(spec.get("credits")) and dict(spec["credits"]),
        "note": str(spec.get("note") or ""),
        "intent": intent,
        "intent_values": _check_intent_values(key, spec.get("intent_values"),
                                              intent),
        "intent_scale": _check_intent_scale(key, spec.get("intent_scale"),
                                            intent),
        # THE TWO THIS ENTRY USED TO SWALLOW, and both were added because a
        # built-in model needed them — which means the next model to need them
        # is exactly the kind a user registers. `defaults` came from flux-2
        # answering "resolution is required" with a 500 on a field its own
        # reference calls optional; `exclusive` came from Seedance's 422 on an
        # anchor frame sent alongside reference images. Neither was in the key
        # set this dict is built from, so a registration carrying either had it
        # DROPPED IN SILENCE and bought the identical failure the mechanisms
        # exist to prevent — the module's own cardinal sin, committed by the
        # door that was supposed to be the safe way in.
        "defaults": _check_defaults(key, spec.get("defaults"), supports),
        "exclusive": _check_exclusive(key, spec.get("exclusive"), intent,
                                      supports),
        # The stamp that keeps this honest on every surface that lists models.
        "source": "registered",
        # AND THE ONE THAT SAYS WHAT NOBODY CHECKED. `source` says a human typed
        # it; this says nothing has ever held the id against kie.
        "verified": False,
    }
    for field, value in entry["defaults"].items():
        allowed = entry["enums"].get(field)
        if allowed and str(value) not in allowed:
            raise KieError(
                f"{key}: defaults[{field!r}] is {value!r}, which this same "
                f"registration says is not one of {list(allowed)}. build_input "
                "would fill the hole with it and then refuse its own value.")
    missing_required = sorted(set(entry["required"]) - supports)
    if missing_required:
        raise KieError(
            f"{key}: {', '.join(missing_required)} is required but not in "
            "`supports`, so build_input would refuse every request this model "
            "could ever be sent")
    if entry["images"] and entry["images"] not in supports:
        raise KieError(
            f"{key}: images field {entry['images']!r} is not in `supports`, so "
            "a reference frame handed to it would be refused before it was sent")
    _check_collision(key, entry)

    MODELS[key] = entry
    _refresh_model_kinds()
    return video_capabilities(key)


# WHAT A kie MODEL ID LOOKS LIKE, measured off the ids in MODELS rather than
# imagined: "google/nano-banana", "flux-2/pro-image-to-image",
# "bytedance/seedance-2". Family/variant, lowercase, dots and dashes, sometimes
# a colon for a version. This is a PLAUSIBILITY check and not a validity one —
# it cannot know whether kie serves the id, only that this one could not
# possibly be served, which catches the paste that brought a whole URL or a
# trailing newline with it.
_MODEL_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*[A-Za-z0-9]$")
_MODEL_ID_MAX = 120


def _check_model_id(key: str, value: Any) -> str:
    """The literal id kie will be sent, proven to be shaped like one."""
    text = str(value or "")
    if text != text.strip():
        raise KieError(
            f"{key}: the model id {text!r} has whitespace around it, which kie "
            "would send verbatim and answer with a 404 after the upload. Paste "
            "it without the newline.")
    if not text:
        raise KieError(f"{key}: the model id is empty")
    if len(text) > _MODEL_ID_MAX:
        raise KieError(f"{key}: {len(text)} characters is not a model id — kie's "
                       "are family/variant strings like 'bytedance/seedance-2'")
    if text.startswith(("http://", "https://")):
        raise KieError(
            f"{key}: {text!r} is a URL, not a model id. The id is the literal "
            "string in the `model` field of kie's own example request — on a "
            "reference page at docs.kie.ai/market/<family>/<variant> that is "
            "usually '<family>/<variant>', but not always, which is exactly why "
            "it has to be read rather than derived from the URL.")
    if not _MODEL_ID_OK.match(text):
        raise KieError(
            f"{key}: {text!r} is not shaped like a kie model id — they are "
            "lowercase family/variant strings such as 'bytedance/seedance-2', "
            "with no spaces. A malformed one costs a round trip and a 404 to "
            "discover.")
    return text


def _check_intent_fields(key: str, table: dict) -> dict:
    """The intent map, with each field name proven usable as one."""
    out: dict[str, str] = {}
    for name, field in table.items():
        text = str(field or "").strip()
        if not text or text != str(field):
            raise KieError(
                f"{key}: intent {name!r} maps to {field!r}, which is not a "
                "field name. It has to be the literal key this model's own "
                "input object uses, e.g. seconds -> 'duration'.")
        if text in out.values():
            # Two intents on one field means the second silently overwrites the
            # first in video_input's output — a setting paid for and not applied.
            clash = [n for n, f in out.items() if f == text]
            raise KieError(
                f"{key}: {name} and {clash[0]} both map to {text!r}. One field "
                "cannot carry two settings — whichever is translated second "
                "would overwrite the other and you would be charged for the "
                "one that did not apply.")
        out[name] = text
    return out


def _check_enums(key: str, given: Any, supports: set) -> dict:
    """`enums` as {field: (values,)}, refusing the shapes that fail silently."""
    out: dict[str, tuple] = {}
    for field, values in dict(given or {}).items():
        if field not in supports:
            raise KieError(
                f"{key}: enums names {field!r}, which is not in `supports`, so "
                "nothing would ever be checked against it")
        if isinstance(values, str) or not isinstance(values, (list, tuple, set)):
            # A bare string tuple()s into its characters, and the resulting
            # "enum" refuses every real value with a message listing letters.
            raise KieError(
                f"{key}: enums[{field!r}] is {type(values).__name__}, not a "
                "list of the values this model accepts. A single allowed value "
                "still goes in a list.")
        cleaned = tuple(values)
        if not cleaned:
            raise KieError(f"{key}: enums[{field!r}] is empty, which would "
                           "refuse every possible value for that field")
        out[field] = cleaned
    return out


def _check_ranges(key: str, given: Any, supports: set) -> dict:
    """`ranges` as {field: (lo, hi)}, both numbers and in that order."""
    out: dict[str, tuple] = {}
    for field, bounds in dict(given or {}).items():
        if field not in supports:
            raise KieError(
                f"{key}: ranges names {field!r}, which is not in `supports`, so "
                "the bound would never be applied to anything")
        if isinstance(bounds, str) or not isinstance(bounds, (list, tuple)) \
                or len(bounds) != 2:
            raise KieError(
                f"{key}: ranges[{field!r}] must be (low, high), got {bounds!r}")
        try:
            lo, hi = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError):
            raise KieError(f"{key}: ranges[{field!r}] must be two numbers, got "
                           f"{bounds!r}") from None
        if lo > hi:
            raise KieError(
                f"{key}: ranges[{field!r}] is ({lo:g}, {hi:g}) — the low bound "
                "is above the high one, which refuses every value")
        out[field] = (bounds[0], bounds[1])
    return out


def _check_caps(key: str, given: Any, supports: set) -> dict:
    """`caps` as {field: max array length}, positive whole numbers only."""
    out: dict[str, int] = {}
    for field, cap in dict(given or {}).items():
        if field not in supports:
            raise KieError(
                f"{key}: caps names {field!r}, which is not in `supports`")
        try:
            value = int(cap)
        except (TypeError, ValueError):
            raise KieError(f"{key}: caps[{field!r}] must be a whole number of "
                           f"items, got {cap!r}") from None
        if value <= 0:
            raise KieError(f"{key}: caps[{field!r}] is {value}, which would "
                           "refuse every request that used the field")
        out[field] = value
    return out


def _check_intent_values(key: str, given: Any, intent: dict) -> dict:
    """`intent_values` as {intent: {canonical: this model's spelling}}."""
    out: dict[str, dict] = {}
    for name, mapping in dict(given or {}).items():
        if name not in intent:
            raise KieError(
                f"{key}: intent_values names {name!r}, which this model has no "
                f"intent entry for — it maps {', '.join(sorted(intent))}")
        if not isinstance(mapping, dict) or not mapping:
            raise KieError(
                f"{key}: intent_values[{name!r}] must be a non-empty mapping of "
                "the canonical value to this model's own spelling, e.g. "
                "{'16:9': 'landscape'}")
        out[name] = {str(k): v for k, v in mapping.items()}
    return out


def _check_intent_scale(key: str, given: Any, intent: dict) -> dict:
    """`intent_scale` as {intent: multiplier}, positive numbers only."""
    out: dict[str, float] = {}
    for name, factor in dict(given or {}).items():
        if name not in intent:
            raise KieError(
                f"{key}: intent_scale names {name!r}, which this model has no "
                f"intent entry for — it maps {', '.join(sorted(intent))}")
        try:
            value = float(factor)
        except (TypeError, ValueError):
            raise KieError(f"{key}: intent_scale[{name!r}] must be a number, "
                           f"got {factor!r}") from None
        if value <= 0:
            raise KieError(
                f"{key}: intent_scale[{name!r}] is {value:g} — a zero or "
                "negative multiplier turns every duration into nonsense before "
                "it is sent")
        out[name] = value
    return out


def _check_defaults(key: str, given: Any, supports: set) -> dict:
    """`defaults` as {field: value}, for a field this model actually takes.

    A default on a field outside `supports` is worse than useless: build_input
    fills the hole and then refuses its own filling as an unknown key, so every
    request the model could ever be sent dies on a value nobody passed.
    """
    out: dict[str, Any] = {}
    for field, value in dict(given or {}).items():
        if field not in supports:
            raise KieError(
                f"{key}: defaults names {field!r}, which is not in `supports` — "
                "build_input would add it and then refuse the request for "
                "carrying a key this model does not take")
        if value is None or value == "":
            raise KieError(
                f"{key}: defaults[{field!r}] is empty, which fills nothing. "
                "Leave the field out if the model has no required value for it.")
        out[field] = value
    return out


def _check_exclusive(key: str, given: Any, intent: dict, supports: set) -> tuple:
    """`exclusive` as ((group, group, ...), ...) — settings legal apart, not together.

    Each group is a tuple of INTENT names (or of raw field names, for a model
    that states the rule in its own vocabulary). The first group in a rule is
    the one kept when both are present; see :func:`_exclusive_refusal` and
    bgate_core.cine.cinematic._fit_intent, which apply that precedence in opposite
    ways for the same reason.

    A rule naming something this model has no field for cannot ever fire, so it
    is refused rather than kept as decoration that reads like protection.
    """
    out: list[tuple] = []
    for rule in (given or ()):
        if isinstance(rule, str) or not isinstance(rule, (list, tuple)):
            raise KieError(
                f"{key}: exclusive must be a sequence of RULES, each a sequence "
                "of groups, e.g. ((('first_frame',), ('refs',)),) — got "
                f"{rule!r}")
        groups: list[tuple] = []
        for group in rule:
            if isinstance(group, str) or not isinstance(group, (list, tuple)):
                raise KieError(
                    f"{key}: exclusive group {group!r} must be a sequence of "
                    "names. A bare string iterates into its characters and the "
                    "rule then guards fields called 'f', 'i', 'r'.")
            names = tuple(str(n).strip() for n in group if str(n).strip())
            if not names:
                raise KieError(f"{key}: exclusive has an empty group, which "
                               "can never conflict with anything")
            for name in names:
                if name not in intent and name not in supports:
                    raise KieError(
                        f"{key}: exclusive names {name!r}, which is neither an "
                        f"intent this model maps ({', '.join(sorted(intent))}) "
                        "nor a field in `supports`. The rule would never fire, "
                        "so the payload it was written to refuse would be sent "
                        "and paid for.")
            groups.append(names)
        if len(groups) < 2:
            raise KieError(
                f"{key}: an exclusive rule needs at least two groups — one "
                "group is not exclusive with anything")
        out.append(tuple(groups))
    return tuple(out)


def _check_collision(key: str, entry: dict) -> None:
    """Refuse a registration that quietly redefines something already in use.

    TWO COLLISIONS, AND NEITHER ERRORS ANYWHERE ELSE. Overwriting a BUILT-IN
    means every sequence already planned against that name silently starts
    buying from a different model — the ids and enums in MODELS were read off a
    reference page and a runtime entry has no such standing, so this is a
    downgrade with no error attached. Two NAMES pointing at one id with
    different specs is the same problem wearing a disguise: the generations look
    like two models and are one, and only the stricter entry's limits are true.
    """
    existing = MODELS.get(key)
    if existing is not None and existing.get("source", "built-in") != "registered":
        raise KieError(
            f"{key!r} is a built-in model whose id and schema were read off "
            "kie's own reference page. Registering over it would repoint every "
            "sequence already planned against that name at an unverified entry, "
            "with nothing saying so. Pick another name.")
    for other, spec in MODELS.items():
        if other == key or spec.get("model") != entry["model"]:
            continue
        # `defaults` and `exclusive` are compared too, and normalised on the way
        # in because a built-in that declares neither carries no key at all
        # while a registration always carries an empty one. Two names for one id
        # that disagree about a REQUIRED default, or about which settings cannot
        # ride together, are the same failure as disagreeing about a range: the
        # request planned against the looser entry is refused at the provider.
        if spec.get("intent") != entry["intent"] or \
                spec.get("enums") != entry["enums"] or \
                spec.get("ranges") != entry["ranges"] or \
                (spec.get("defaults") or {}) != (entry.get("defaults") or {}) or \
                (spec.get("exclusive") or ()) != (entry.get("exclusive") or ()):
            raise KieError(
                f"{key!r} and {other!r} are both {entry['model']!r} but "
                "describe it differently. One of the two is wrong about this "
                "model's limits, and a shot planned against the wrong one is "
                "refused at the provider after the frames are uploaded — or "
                "worse, accepted with a setting that did not apply.")


# ---------------------------------------------------------------------------
# CONFIRMING AN ID EXISTS WITHOUT BUYING ANYTHING, and the caveat on it
# ---------------------------------------------------------------------------
# There is no free lookup. kie serves no catalogue endpoint this adapter can
# read, /api/v1/chat/credit takes no model, and recordInfo needs a task that only
# a generation creates. What DOES discriminate is the business code on a
# deliberately malformed createTask: the model is resolved before the input is
# validated, so a bad id answers 404 ("no such endpoint or task") and a good one
# answers 422 ("refused the request shape"). An empty input cannot render
# anything — every model here documents `prompt` as required.
#
# WHY IT IS OPT-IN ANYWAY. That reasoning is inference from kie's own error
# table, not a documented contract, and the branch it cannot rule out is a model
# that accepts an empty input and STARTS A JOB. That would be a real charge, so
# nothing calls this on your behalf: it is a flag a human sets, and if a taskId
# ever comes back the result says loudly that one may have been created and
# carries the id so it is recoverable rather than lost.
def probe_model_id(model: str, *, root: Any = None,
                   timeout: float = 30.0) -> dict:
    """Ask kie whether a model id exists, by sending it something unusable.

    Returns ``{exists: True|False|None, checked, reason, task_id}``. ``None``
    means the question could not be answered — no key, no network, or an answer
    this cannot interpret — and must never be read as either verdict.
    """
    name = str(model or "").strip()
    spec = MODELS.get(name)
    if spec is None:
        raise KieError(f"unknown kie model {name!r} — known: {sorted(MODELS)}")
    key = api_key(root)
    if not key:
        return {"exists": None, "checked": False, "model": name,
                "id": spec["model"], "task_id": "",
                "reason": available(root)["reason"]}
    try:
        got = _request(JOBS_CREATE, key, method="POST", timeout=timeout,
                       payload={"model": spec["model"], "input": {}})
    except KieError as exc:
        text = str(exc)
        if "code 404" in text or "HTTP 404" in text:
            return {"exists": False, "checked": True, "model": name,
                    "id": spec["model"], "task_id": "",
                    "reason": f"kie has no model {spec['model']!r} — it answered "
                              "404 for it. Check the id on the model's own page; "
                              "generating with it would 404 after the "
                              "conditioning frames were uploaded."}
        if "code 422" in text or "HTTP 422" in text:
            _mark_verified(name)
            return {"exists": True, "checked": True, "model": name,
                    "id": spec["model"], "task_id": "",
                    "reason": "kie refused the request SHAPE rather than the "
                              "model, which it could only do after resolving "
                              "the id. Nothing was generated or charged."}
        return {"exists": None, "checked": True, "model": name,
                "id": spec["model"], "task_id": "",
                "reason": f"kie answered something this cannot read as a "
                          f"verdict on the id: {text}"}
    # THE BRANCH THE OPT-IN EXISTS FOR. kie accepted an empty input, which means
    # a job may be running and billed.
    task_id = str(got.get("taskId") or "")
    _mark_verified(name)
    return {"exists": True, "checked": True, "model": name, "id": spec["model"],
            "task_id": task_id,
            "reason": "the id exists — and kie ACCEPTED an empty input rather "
                      "than refusing it, so a generation may have started and "
                      "may be charged for. "
                      + (f"Poll {JOBS_RECORD}?taskId={task_id}."
                         if task_id else "No task id came back.")}


def _mark_verified(model: str) -> None:
    """Record that something held this id against the provider and it stood."""
    spec = MODELS.get(str(model or "").strip())
    if spec is not None:
        spec["verified"] = True


def _refresh_model_kinds() -> None:
    """Rebuild the kind tuples after MODELS changes. They are module-level
    caches of a derived fact, and a registration that left them stale would add
    a model that `video_models` cannot see."""
    global IMAGE_MODELS, VIDEO_MODELS
    IMAGE_MODELS = tuple(k for k, v in MODELS.items() if v["kind"] == "image")
    VIDEO_MODELS = tuple(k for k, v in MODELS.items() if v["kind"] == "video")

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
# THE FORWARD ESTIMATE — what a shot will cost BEFORE anybody buys it
# ---------------------------------------------------------------------------
# WHY THIS EXISTS. Everything above measures cost AFTER the fact:
# `creditsConsumed` arrives on the finished record, which is the truth and is
# useless to a budget gate, because a gate runs before the spend.
# bgate_core.cine.cinematic._budget_refusal therefore passed projected_usd=0.0 into
# spend.check and could only ever catch "this project is already over" — never
# "this ONE shot is expensive", which for a fifteen-second clip is the larger
# number and the one a sequence of eight multiplies.
#
# WHERE THESE NUMBERS COME FROM, AND IT IS NOT A PRICE PAGE. kie publishes no
# per-model price. The whole of the public record is the quickstart's "image
# models typically 10-50 credits, video 100-500", and docs.kie.ai was not
# reachable from the machine this was written on, so — unlike MODELS, where the
# no-guessing rule is absolute — not one number below was read off a model's own
# reference page. That is stated on every value this produces rather than buried
# here, because an estimate a caller mistakes for a quote is worse than none.
#
# SO THE TABLE IS SHAPED TO BE HONEST IN THREE WAYS:
#
#   * IT IS CONSERVATIVE. Each entry spreads the CEILING of kie's published band
#     across the model's own documented duration range, so the longest shot it
#     will generate quotes at the top of the band and a shorter one quotes less.
#     A gate that under-quotes lets through exactly the spend it exists to stop,
#     so an upper bound is the only safe direction to be wrong in.
#   * AN UNKNOWN MODEL YIELDS NO NUMBER. `known: False` and `credits: None`,
#     never 0 — a fabricated credit count that reaches spend.check does not read
#     as "unpriced", it reads as "free", which is the failure this whole module
#     was written to avoid.
#   * EVERY ENTRY IS OVERRIDABLE, because a user with a month of invoices knows
#     more than this table does. BGATE_KIE_VIDEO_CREDITS takes JSON keyed by the
#     model name here: {"seedance-2": {"per_second": 30, "per_call": 0}}. A
#     model registered at runtime may also carry its own `credits` block, which
#     beats the table and loses to the environment.
VIDEO_CREDITS: dict[str, dict] = {
    "seedance-2": {
        # 500 credits — the ceiling of kie's published video band — over the 15s
        # ceiling of this model's own documented duration range, with the band's
        # FLOOR as a minimum so no shot is ever quoted below the cheapest video
        # generation kie describes.
        "per_call": 0,
        "per_second": round(500 / 15, 2),
        "minimum": 100,
        "source": "kie quickstart, 'video models typically 100-500 credits'. No "
                  "per-model price is published and no reference page was "
                  "reachable to read one, so this is that band's ceiling spread "
                  "across seedance-2's own 4..15s range: an upper bound, not a "
                  "rate.",
    },
}
VIDEO_CREDITS_ENV = "BGATE_KIE_VIDEO_CREDITS"

ESTIMATE_NOTE = (
    "This is an ESTIMATE and kie publishes no per-model price. It is an upper "
    "bound derived from kie's own 100-500 credit band for video, spread across "
    "the model's documented duration range — not a quote, and not read off a "
    "price page. Override it with " + VIDEO_CREDITS_ENV + " (JSON, keyed by "
    "model name) once your invoices say what a shot really costs.")

# WHAT THE ESTIMATE DELIBERATELY DOES NOT MODEL. Resolution almost certainly
# moves the price — 4k is not 480p — and no published rate says by how much, so
# inventing a multiplier would put a fabricated number in front of a budget gate
# wearing the same label as a derived one. It is named as a caveat instead.
_ESTIMATE_UNMODELLED = (
    "resolution, reference frames and generated audio are not modelled — kie "
    "publishes no rate for any of them, so a 4k shot quotes the same as a 480p "
    "one and may cost more")


def _credit_overrides() -> dict:
    """BGATE_KIE_VIDEO_CREDITS, parsed. Junk is ignored, never guessed at."""
    raw = (os.environ.get(VIDEO_CREDITS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rate_numbers(entry: Any) -> Optional[dict]:
    """One rate block as three non-negative numbers, or None if it says nothing.

    An all-zero or unparseable block is None rather than a zero rate, for the
    reason the whole module gives: a zero that reaches a budget gate is
    permission, not an absence.
    """
    if not isinstance(entry, dict):
        return None
    try:
        per_call = float(entry.get("per_call") or 0)
        per_second = float(entry.get("per_second") or 0)
        minimum = float(entry.get("minimum") or 0)
    except (TypeError, ValueError):
        return None
    if min(per_call, per_second, minimum) < 0:
        return None
    if max(per_call, per_second, minimum) <= 0:
        return None
    return {"per_call": per_call, "per_second": per_second, "minimum": minimum}


def video_credit_rate(model: str) -> Optional[dict]:
    """The credit rate in force for one model, or None when there is none.

    Most specific first, the same precedence keys follow: the environment beats
    what a registered model declared, which beats this file's table.
    """
    name = str(model or "").strip()
    spec = MODELS.get(name) or {}
    for entry, origin in (
            (_credit_overrides().get(name), VIDEO_CREDITS_ENV),
            (spec.get("credits"), "the model's own registration"),
            (VIDEO_CREDITS.get(name), "kie's published band")):
        rate = _rate_numbers(entry)
        if rate is None:
            continue
        rate["origin"] = origin
        rate["source"] = str((entry or {}).get("source") or "")
        return rate
    return None


def _seconds_ceiling(model: str) -> Optional[float]:
    """The longest shot this model documents, in seconds. None if it says none."""
    try:
        options = video_capabilities(model)["options"]
    except Exception:                                            # noqa: BLE001
        return None
    band = options.get("seconds")
    if isinstance(band, list) and len(band) == 2:
        try:
            return float(band[1])
        except (TypeError, ValueError):
            return None
    return None


def estimate_credits(model: str = "", seconds: Optional[float] = None,
                     **intent: Any) -> dict:
    """What one video generation is likely to cost in CREDITS, before it runs.

    Returns ``{model, seconds, credits, known, basis, caveats, rate}``.
    ``known=False`` means no rate is configured and ``credits`` is None — which
    a caller must carry as "unknown", never fold to zero.

    ``intent`` is accepted in the pipeline's own vocabulary and recorded rather
    than priced: see _ESTIMATE_UNMODELLED for what no published number covers.
    """
    name = str(model or "").strip() or DEFAULT_VIDEO_MODEL
    spec = MODELS.get(name)
    base = {"model": name, "seconds": seconds, "credits": None, "known": False,
            "rate": None, "caveats": [], "note": ESTIMATE_NOTE}
    if spec is None or spec.get("kind") != "video":
        return {**base,
                "basis": f"{name!r} is not a registered kie video model, so "
                         f"nothing here can price it — known: "
                         f"{sorted(VIDEO_MODELS)}"}

    rate = video_credit_rate(name)
    if rate is None:
        return {**base,
                "basis": f"no credit rate is configured for {name}, and kie "
                         "publishes none. The cost of this generation is "
                         f"UNKNOWN, not zero — set {VIDEO_CREDITS_ENV} to what "
                         "your invoices say, or register the model with a "
                         "`credits` block."}

    length, caveats = seconds, [_ESTIMATE_UNMODELLED]
    if length in (None, ""):
        length = _seconds_ceiling(name)
        if length is None:
            return {**base, "rate": rate,
                    "basis": f"no duration was given and {name} documents no "
                             "seconds range, so there is nothing to multiply "
                             f"the rate by. Cost UNKNOWN, not zero."}
        caveats.append(f"no duration was given, so this is quoted at {name}'s "
                       f"ceiling of {length:g}s")
    try:
        length = float(length)
    except (TypeError, ValueError):
        return {**base, "rate": rate,
                "basis": f"{seconds!r} is not a number of seconds, so this "
                         "generation cannot be quoted. Cost UNKNOWN, not zero."}

    # Rounded UP. A budget gate handed the fractional truth of an upper bound is
    # a gate that lets a shot through on a rounding error.
    import math

    raw = rate["per_call"] + rate["per_second"] * length
    credits = int(math.ceil(max(rate["minimum"], raw)))
    asked = sorted(k for k, v in intent.items()
                   if v not in (None, "", [], False))
    if asked:
        caveats.append("not varied by " + ", ".join(asked))
    return {**base, "seconds": length, "credits": credits, "known": True,
            "rate": rate, "caveats": caveats,
            "basis": f"{credits} credits: {rate['per_second']:g}/s over "
                     f"{length:g}s"
                     + (f" plus {rate['per_call']:g} per call"
                        if rate["per_call"] else "")
                     + f", floor {rate['minimum']:g}, from "
                     + (rate["origin"] or "an unnamed source")
                     + (f" — {rate['source']}" if rate["source"] else "")}


def estimate_usd(model: str = "", seconds: Optional[float] = None,
                 **intent: Any) -> dict:
    """The same estimate in DOLLARS, which needs one more thing to be knowable.

    Two independent unknowns, and folding them together is how a caller ends up
    unable to say which is missing: the CREDIT count needs a rate for the model
    (VIDEO_CREDITS), and the DOLLAR figure needs the account's own credit rate
    (BGATE_KIE_USD_PER_CREDIT). ``credits_known`` and ``known`` answer those
    separately, and ``usd`` is None — never 0.0 — whenever either is missing.
    """
    got = estimate_credits(model, seconds, **intent)
    rate = usd_per_credit()
    usd = None
    if got["known"] and rate is not None:
        usd = round(float(got["credits"]) * rate, 6)
    if usd is None:
        why = (got["basis"] if not got["known"] else
               "no credit-to-dollar rate is set, so the credit estimate cannot "
               "be turned into money — set " + USD_PER_CREDIT_ENV + " to your "
               "account's rate. " + PRICE_NOTE)
        basis = f"cost UNKNOWN, not zero: {why}"
    else:
        basis = f"about ${usd:.4f} — {got['basis']}, at ${rate:g}/credit"
    return {**got, "credits_known": got["known"], "known": usd is not None,
            "usd": usd, "usd_per_credit": rate, "basis": basis}


# ---------------------------------------------------------------------------
# Key and availability
# ---------------------------------------------------------------------------

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
    except Exception:                                            # noqa: BLE001
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
                   + (f" — {help_text}" if help_text else ""),
                   provider="kie", status=code, body=msg,
                   billing=_http.is_billing(code, msg))


# THE RATE LIMIT, AS KIE STATED IT: 20 new requests per 10 seconds, with 100+
# concurrent tasks supported (kie support, 2026-08-15).
#
# NOTE WHICH NUMBER IS THE BINDING ONE. The concurrency figure is not a
# constraint on this product at all - nothing here fans out to anything like a
# hundred simultaneous jobs, and a cap that cannot be reached is not worth code.
# The RATE is reachable: a board dispatching several seats at once, each
# submitting and then POLLING its own task, adds requests faster than the job
# count suggests, because a poll is a new request too. So the window below
# counts EVERY call through _request, not only submits.
#
# EIGHTEEN, NOT TWENTY, and the two spare are deliberate. This limiter is
# PER PROCESS: seat agents run as separate processes and share no window, so
# whatever this file promises is a promise about one of them. Leaving headroom
# means two processes drifting into the same window are still likely to land
# under the real limit rather than exactly on it. The 429 retry above is the
# backstop for when they do not, and it is why this being imperfect is
# acceptable rather than dangerous.
_RATE_WINDOW_SECONDS = 10.0
_RATE_WINDOW_MAX = 18
_GATE = _http.RateGate(_RATE_WINDOW_SECONDS, _RATE_WINDOW_MAX)

# HOW LONG TO WAIT WHEN KIE SAYS SLOW DOWN, and how many times to try. The
# 429 row in _CODE_HELP says "retry in a moment", and this is the retry: three
# attempts, doubling waits long enough to clear a per-minute bucket, kie's
# own Retry-After winning (capped) whenever it sends one. A 429 is SAFE ON A
# SUBMIT because the request was refused, so nothing was charged - which is
# exactly why a 500 is NOT retried on one: an internal error can land on
# either side of the charge, and a blind retry there is how one submit
# becomes two paid jobs. The shared layer encodes both rules.
_RATE_LIMIT_TRIES = 3
_RATE_LIMIT_BACKOFF = (5.0, 15.0)


def _request(path: str, key: str, *, payload: Optional[dict] = None,
             params: Optional[dict] = None, method: str = "GET",
             timeout: float = 60.0) -> dict:
    """One call, unwrapped. The key rides in the header and nowhere else."""
    url = path if path.startswith("http") else API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    what = f"{method} {path}"
    try:
        got = _http.request(
            method, url, json=payload, timeout=timeout, provider="kie",
            retries=_RATE_LIMIT_TRIES, backoff=_RATE_LIMIT_BACKOFF, gate=_GATE,
            headers={
                "Authorization": f"Bearer {key}",
                # THE SAME 1010 THAT ALREADY BIT THE DOWNLOAD PATH, one door
                # further up: kie's Cloudflare front refuses urllib's default
                # agent on some API endpoints too (measured on
                # file-base64-upload). Our own key, our own credits, a
                # documented endpoint - urllib simply announces itself in a
                # way a generic rule bans.
                "User-Agent": DOWNLOAD_UA,
            })
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise KieError(f"could not reach kie ({exc.body})",
                           provider="kie") from exc
        # A transport-level failure carries the SAME numbers as the
        # body-level one, so the same advice applies and there is no second
        # table.
        help_text = _CODE_HELP.get(exc.status, "")
        raise KieError(f"kie HTTP {exc.status} on {what}"
                       + (f": {exc.body}" if exc.body else "")
                       + (f" — {help_text}" if help_text else ""),
                       provider="kie", status=exc.status, body=exc.body,
                       billing=exc.billing) from exc
    raw = got.text() or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KieError(f"kie returned something that is not JSON on {what}: "
                       f"{raw[:200]}", provider="kie") from exc
    if not isinstance(parsed, dict):
        raise KieError(f"kie returned {type(parsed).__name__}, not an object, "
                       f"on {what}", provider="kie")
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
# Uploading — how a local anchor becomes something a model can be given
# ---------------------------------------------------------------------------

def upload_file(path: str | os.PathLike[str], *, root: Any = None,
                name: str = "", timeout: float = 120.0) -> dict:
    """Put one local image in kie's file store and return the URL it minted.

    Returns ``{url, expires_at, bytes, name, mime}``. The URL is what goes into
    ``first_frame_url`` / ``reference_image_urls`` on a generation call.

    THIS IS THE ONE CALL THAT MAKES AN ANCHORED SHOT POSSIBLE, so it is worth
    being exact about what it does and does not buy. It does NOT make kie
    equivalent to Krea for anchored still work: Krea takes a data URI inline, in
    the same request, with no second round trip and no expiry. This is a separate
    POST whose product dies in three days. For a still image that is strictly
    worse and image_generate should keep preferring Krea. For a SHOT it is the
    only path there is, because no other provider wired here generates video at
    all.

    THE FILE IS UPLOADED TO A THIRD PARTY. That is not a footnote — it is the
    operation. Every frame handed to this leaves the machine and sits on kie's
    storage for three days, so callers pass conditioning frames the user has
    already chosen to generate through kie's own models, never arbitrary paths,
    and never anything from outside the project.
    """
    source = Path(path)
    if not source.is_file():
        raise KieError(f"nothing on disk at {source}")
    # Sniffed, not assumed: a pinned anchor's extension is its revision number,
    # so the suffix answers this question wrong for every pin in the project.
    mime = _upload_mime(source)
    if not mime:
        # JOINED, NOT A REPR. Interpolating the list itself put
        # "it is not a ['image/jpeg', 'image/png', 'image/webp'] image" in front
        # of the user, which is a Python literal wearing a sentence, and it also
        # dropped the words the test for this refusal matches on.
        allowed = ", ".join(sorted(set(UPLOAD_MIME.values())))
        raise KieError(
            f"cannot upload {source.name}: its type is not one of {allowed}, "
            "by extension or by its own leading bytes. A type kie accepts here "
            "but no model accepts downstream would fail at generation instead, "
            "after the upload had already succeeded.")

    key = api_key(root)
    if not key:
        raise KieError(available(root)["reason"])

    import base64

    payload = {
        # NO `data:` PREFIX. The reference names this field base64Data and
        # documents it as the encoded bytes; a data URI would be encoded a
        # second time by the server's decoder and land as a corrupt image that
        # still uploads with a 200.
        "base64Data": base64.b64encode(source.read_bytes()).decode("ascii"),
        "uploadPath": UPLOAD_DIR,
        "fileName": (name.strip() or source.name),
    }
    got = _request(UPLOAD_BASE64, key, payload=payload, method="POST",
                   timeout=timeout)
    url = str(got.get("downloadUrl") or got.get("fileUrl") or "").strip()
    if not url:
        raise KieError(
            "kie accepted the upload and returned no downloadUrl — got "
            f"{sorted(got)[:8]}. Nothing can be conditioned on this frame.")
    return {
        "url": url,
        "bytes": int(got.get("fileSize") or source.stat().st_size),
        "name": str(got.get("fileName") or payload["fileName"]),
        "mime": str(got.get("mimeType") or mime),
        # STAMPED, NOT ASSUMED. A caller that stores this URL anywhere has to be
        # able to tell that it is dead without calling it.
        "expires_at": _expiry(UPLOAD_TTL_DAYS),
        "ttl_days": UPLOAD_TTL_DAYS,
        "source": str(source),
    }


def _expiry(days: int) -> str:
    """The day a minted URL stops working, as an ISO date."""
    import datetime as _dt

    return (_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Building a market-jobs request
# ---------------------------------------------------------------------------

def _reject_local(value: Any, field: str) -> None:
    """Refuse a path where kie documents a URI.

    Still a refusal, and still at this layer, because ``build_input`` runs before
    any money moves and must not perform network calls of its own — an upload
    hidden inside validation would mean a shape check that costs a round trip and
    leaks a file. What changed is the ADVICE: there is a route now
    (:func:`upload_file`), so the message names it instead of telling the caller
    the capability does not exist. It said "anchored generation from local pinned
    refs is a Krea capability, not a kie one", which was a true statement about
    the generation endpoints and a false one about kie.
    """
    for one in (value if isinstance(value, (list, tuple)) else [value]):
        text = str(one)
        if text.startswith(("http://", "https://")):
            continue
        raise KieError(
            f"{field} must be a URL — kie's generation endpoints document these "
            f"as URIs and take no inline bytes, so {text[:60]!r} cannot be sent "
            "as it stands. Upload it first: kie.upload_file(path) returns a URL "
            f"valid for {UPLOAD_TTL_DAYS} days that this field accepts.")


def _exclusive_refusal(spec: dict, given: dict) -> str:
    """The one refusal no field-by-field check can reach, or "" when clear.

    WHY THIS IS NOT ALREADY COVERED. `supports`, `enums`, `ranges` and `caps`
    each describe ONE field, and Seedance's constraint is about a pair: "The
    reference image and the first and last frames are mutually exclusive, and
    only one scene can be selected." Every value in such a payload is
    individually legal and the payload is refused as a whole — with a 422 that
    arrives AFTER upload_file has already put both anchors on kie's storage.

    WHY IT IS ENFORCED HERE AND NOT ONLY IN THE PIPELINE. bgate_core.cine.cinematic
    resolves the same groups in _fit_intent, which is right for it — a shot list
    should lose the weaker anchor and say so rather than fail. But that left the
    rule stated in THIS table and enforced nowhere in this module, so
    kie.generate_video, kie.submit and every other entry point still built the
    illegal payload and paid the round trip to learn it. A constraint that lives
    in a capability table has to bite at the layer that owns the table; a caller
    with no resolver of its own gets a refusal before the spend instead of a 422
    after the upload.

    Groups are written in INTENT names for a video model (they are what a caller
    reasons in) and translated through `intent` here; a model with no intent map
    may state raw field names and they pass through unchanged. The group listed
    FIRST is the one to keep, which is the same precedence _fit_intent applies.
    """
    table = spec.get("intent") or {}
    for groups in (spec.get("exclusive") or ()):
        used = []
        for group in groups:
            hit = [table.get(name, name) for name in group
                   if given.get(table.get(name, name)) not in (None, "", [])]
            if hit:
                used.append((group, hit))
        if len(used) < 2:
            continue
        keep, drop = used[0], used[1:]
        return (
            "this model takes " + " or ".join("/".join(g) for g, _ in used)
            + ", never both — every one of those fields is legal on its own and "
            "the payload is refused as a whole, which kie only says in a 422 "
            "after any conditioning frames have already been uploaded. Send "
            + ", ".join(sorted(keep[1])) + " and drop "
            + ", ".join(sorted(f for _, fields in drop for f in fields)) + ".")
    return ""


def build_input(model: str, **fields: Any) -> dict:
    """The `input` object for one model, validated against its own schema.

    Every check here happens BEFORE any money moves, and every one of them is a
    thing kie's reference states. An unsupported key is refused rather than
    dropped for the reason imageto3d.submit_3d gives: a dropped parameter still
    bills you, hands back the default, and leaves nothing saying why the setting
    you passed did not apply.
    """
    spec = _spec(model)
    # AN EMPTY LIST IS AN ABSENT FIELD, not a value. `video_input` has always
    # dropped one (`value == []` is in its skip test) and this did not, so the
    # two halves of the same module disagreed about whether
    # reference_image_urls=[] is a reference field that is present. That matters
    # exactly where the mutual-exclusion rule below reads presence: a local
    # check that calls it absent while the payload still carries the key is a
    # check that passes and a request that may be refused at the provider.
    given = {k: v for k, v in fields.items()
             if v is not None and v != "" and v != []}

    # DEFAULTS FOR FIELDS THE MODEL DEMANDS AND THE CALLER HAS NO OPINION ON.
    # flux-2-pro-edit answers "resolution is required" with a 500 — a field this
    # table listed as merely supported, so every anchored call through it died
    # before generating and the error read as kie being broken. A caller-supplied
    # value always wins; this only fills a hole that would otherwise be a refusal.
    for field, value in (spec.get("defaults") or {}).items():
        given.setdefault(field, value)

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

    conflict = _exclusive_refusal(spec, given)
    if conflict:
        raise KieError(f"{model}: {conflict}")

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

    last: dict = {}

    def step() -> Optional[dict]:
        nonlocal last
        last = record(task_id, root=root)
        state = str(last.get("state") or "")
        if state == JOB_DONE:
            return last
        if state in JOB_DEAD:
            raise KieError(
                f"kie job failed ({last.get('failCode') or 'no code'}): "
                f"{last.get('failMsg') or 'no reason given'}")
        if state and state not in JOB_RUNNING:
            raise _http.PollUnknown(f"kie job returned unknown state {state!r}")
        return None

    try:
        return _http.poll(step, first=max(1.0, float(interval)),
                          max_wait=max(5.0, float(timeout)), factor=1.25,
                          ceiling=10.0, unknown_is_fatal=True, provider="kie",
                          label=f"job {task_id}")
    except KieError:
        raise
    except _http.PollUnknown as exc:
        raise KieError(str(exc), provider="kie") from exc
    except _http.ProviderError as exc:
        raise KieError(f"kie job {task_id} did not finish within "
                       f"{timeout:.0f}s (last state "
                       f"{last.get('state') or 'unknown'})",
                       provider="kie") from exc


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
    try:
        return _http.download(str(url), out_path, timeout=timeout,
                              provider="kie",
                              headers={"Accept": accept,
                                       "User-Agent": DOWNLOAD_UA})
    except _http.ProviderError as exc:
        hint = ""
        if exc.status == 403:
            hint = (" — a 403 here is usually the CDN refusing the request "
                    "shape rather than an expired link; kie's URLs live "
                    f"{SUNO_URL_TTL_DAYS} days")
        raise KieError(f"{exc}{hint}", provider="kie", status=exc.status,
                       body=exc.body) from exc


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def _account(result: dict, root: Any, *, kind: str, logical_name: str = "",
             work_item_id: Optional[int] = None, detail: str = "") -> dict:
    """Write what this call cost to the ledger, if it can be known.

    Best effort by construction — losing a ledger row must never lose the file
    that was paid for; that is imagegen._account's rule and it holds here.

    THE HONEST GAP IS STATED ON THE RESULT — AND NOW ON THE LEDGER. With no
    credit rate configured there is no dollar figure to write, and this used to
    mean no row at all: the totals silently under-counted a real charge, which
    with budgets off by default is the whole report reading low. Rather than
    invent a dollar figure, the result says `accounted: false` and carries the
    credit count, AND spend.record_unpriced writes a zero-dollar marker row so
    spend.totals can report "+ N unpriced kie rows" instead of nothing.
    """
    result["credits_consumed"] = result.get("credits_consumed")
    usd = result.get("usd")
    if not root or not result.get("ok") or not usd:
        result["accounted"] = False
        if result.get("ok") and not usd:
            result["cost_note"] = PRICE_NOTE
            if root:
                try:
                    from bgate_core.board import spend

                    spend.record_unpriced(
                        root, result.get("credits_consumed"), kind=kind,
                        work_item_id=work_item_id,
                        logical_name=logical_name or "",
                        detail=detail or f"kie {kind}",
                        model=str(result.get("model") or ""))
                    result["unpriced_recorded"] = True
                except Exception:                                # noqa: BLE001
                    result["unpriced_recorded"] = False
        return result
    try:
        from bgate_core.board import spend

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
    return _result.shape({
        "provider": "kie", "model": model, "kind": kind,
        "task_id": str(rec.get("taskId") or ""),
        "credits_consumed": consumed,
        "usd": cost_usd(consumed),
    })


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
    {ok, path, bytes, seconds, usd} — so the art pipeline does not
    care which provider produced the file.

    ``size`` is WxH because that is what the rest of this codebase speaks; it is
    translated to the nearest aspect ratio the chosen model accepts, the way
    krea.aspect_for does. Passing ``aspect_ratio`` directly wins.
    """
    started = time.monotonic()
    spec = MODELS.get(model)
    base = {"ok": False, "provider": "kie", "model": model, "kind": "image",
            "usd": None}
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
    elif size and "image_size" in spec["supports"]:
        # SEEDREAM SPEAKS NAMED SHAPES, NOT RATIOS. Without this branch a
        # caller's size was silently dropped for any model whose supports
        # carry image_size/image_resolution instead of aspect_ratio — the
        # request went out prompt-only at the provider's default, and
        # tileset_generate's square resize then squashed whatever came back.
        try:
            w, h = (int(v) for v in str(size).lower().split("x", 1))
        except (ValueError, TypeError):
            w = h = 0
        if w and h:
            shapes = spec.get("enums", {}).get("image_size", ())
            if w == h:
                shape = "square_hd" if "square_hd" in shapes else "square"
            elif w < h:
                shape = ("portrait_16_9" if h >= w * 1.6 else "portrait_4_3")
            else:
                shape = ("landscape_16_9" if w >= h * 1.6
                         else "landscape_4_3")
            if shape in shapes:
                fields["image_size"] = shape
            if "image_resolution" in spec["supports"]:
                res = ("1K" if max(w, h) <= 1024
                       else "2K" if max(w, h) <= 2048 else "4K")
                if res in spec.get("enums", {}).get("image_resolution", ()):
                    fields["image_resolution"] = res
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

def _as_url(value: str, root: Any, uploads: list) -> str:
    """A frame argument as something kie will accept, uploading it if it is local.

    Appends what it uploaded to ``uploads`` so the caller can record what the
    shot was conditioned on and when those copies expire.

    Empty in, empty out — an absent anchor is not an error, it is a text-to-video
    shot. A value that is already a URL is passed through untouched and never
    re-uploaded: callers legitimately reuse one minted URL across the shots of a
    sequence, and paying a round trip per shot to mint duplicates of the same
    frame would be pure waste.
    """
    text = str(value or "").strip()
    if not text or text.startswith(("http://", "https://")):
        return text
    record = upload_file(text, root=root)
    uploads.append(record)
    return record["url"]


def generate_video(prompt: str, out_path: str | os.PathLike[str], *,
                   model: str = "", seconds: Optional[float] = None,
                   quality: str = "", shape: str = "",
                   first_frame: str = "", last_frame: str = "",
                   refs: Optional[list] = None,
                   audio: Optional[bool] = None,
                   root: Any = None, logical_name: str = "",
                   work_item_id: Optional[int] = None,
                   timeout: float = 1800.0, on_submit: Any = None,
                   **extra: Any) -> dict:
    """Submit, wait, download one clip. The video twin of :func:`generate_image`.

    ``on_submit`` IS CALLED WITH THE TASK ID THE INSTANT THERE IS ONE, and it is
    the difference between a lost generation and a recoverable one. The charge
    happens at submit and the poll loop then runs for minutes; a caller that
    only learns the task id from the RETURN VALUE learns it never if the process
    dies in between, and a paid clip with no handle cannot be collected by
    anything. Its failure is swallowed on purpose — bookkeeping must not lose
    the file it was bookkeeping.

    THE ARGUMENTS ARE INTENT, NOT ONE MODEL'S FIELD NAMES. They used to be
    Seedance's — ``duration``, ``aspect_ratio``, ``first_frame_url`` — which made
    every caller a Seedance caller and this function unable to drive a second
    model even though kie serves several behind the same key. Sora 2 counts
    `n_frames` and spells its shape "landscape"; Seedance takes `duration` and
    "16:9". :func:`video_input` translates, per model, from its own table entry.

    THE TIMEOUT DEFAULTS TO HALF AN HOUR because a video job runs in minutes
    where an image runs in seconds, and kie's own guidance is to stop polling at
    10-15 minutes on the assumption of a callback. There is no callback surface
    in this product yet, so the loop has to outlast the job.

    LOCAL PATHS ARE UPLOADED, NOT REFUSED. Any of the three frame/reference
    arguments may be a file on disk; each one is put through :func:`upload_file`
    first and replaced with the URL kie minted. That upload happens BEFORE the
    generation and its failure aborts the call, because a shot generated without
    the anchor it was supposed to hold is a full-price clip of the wrong
    character — the expensive failure, not the cheap one. What was uploaded comes
    back in ``uploads`` so the caller can record what the shot was conditioned on.

    NOTHING DOWNSTREAM CONSUMES A RAW .mp4. bgate_core.cine.cinematic is what turns one
    of these into an asset the game can load — it registers the clip as a
    candidate revision and TRANSCODES a kept one to Ogg Theora, which is the only
    format Godot plays. This function returns a file on disk; treat it as a shot
    awaiting a decision, not as something to copy into the engine project.
    """
    started = time.monotonic()
    model = model or DEFAULT_VIDEO_MODEL
    spec = MODELS.get(model)
    base = {"ok": False, "provider": "kie", "model": model, "kind": "video",
            "usd": None}
    if spec is None or spec["kind"] != "video":
        return {**base, "error": f"{model!r} is not a kie video model — "
                                 f"known: {sorted(VIDEO_MODELS)}",
                "seconds": 0.0}

    # Anchors first. Nothing is submitted until every frame this shot is meant
    # to be conditioned on has a URL kie will accept.
    uploads: list[dict] = []
    try:
        first_frame = _as_url(first_frame, root, uploads)
        last_frame = _as_url(last_frame, root, uploads)
        refs = [_as_url(one, root, uploads) for one in (refs or [])]
    except KieError as exc:
        return {**base, "error": f"conditioning frame could not be uploaded: "
                                 f"{exc}", "uploads": uploads,
                "seconds": round(time.monotonic() - started, 2)}

    # Intent -> this model's own spelling. Refuses BEFORE the spend when the
    # model has no parameter for something that was asked for, rather than
    # dropping it and billing for a setting that did not apply.
    try:
        fields: dict[str, Any] = dict(extra)
        fields.update(video_input(model, seconds=seconds, quality=quality,
                                  shape=shape, first_frame=first_frame,
                                  last_frame=last_frame, refs=refs,
                                  audio=audio))
    except KieError as exc:
        return {**base, "error": str(exc), "uploads": uploads,
                "seconds": round(time.monotonic() - started, 2)}
    fields["prompt"] = prompt

    task_id = ""
    try:
        job = submit(model, root=root, timeout=60.0, **fields)
        task_id = job["task_id"]
        if on_submit:
            try:
                on_submit(task_id)
            except Exception:                                    # noqa: BLE001
                pass
        rec = poll(task_id, root=root, timeout=timeout, interval=5.0)
        urls = result_urls(rec)
        if not urls:
            raise KieError("kie reported success with no result URL — the "
                           f"record's resultJson was {str(rec.get('resultJson'))[:300]}")
        written = download(urls[0], out_path, accept="video/*", timeout=600.0)
    except KieError as exc:
        return {**base, "error": str(exc), "task_id": task_id, "uploads": uploads,
                "recover": (f"poll {JOBS_RECORD}?taskId={task_id} — the job may "
                            "be finished and already charged") if task_id else "",
                "seconds": round(time.monotonic() - started, 2)}

    result = {**_finish(rec, model=model, kind="video"), "ok": True,
              "path": str(out_path), "bytes": written, "url": urls[0],
              "uploads": uploads,
              "seconds": round(time.monotonic() - started, 2),
              "consumes": "a raw .mp4 that Godot cannot play — bgate_core."
                          "cinematic registers it as a candidate shot and "
                          "transcodes a kept one to Ogg Theora"}
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
    last: dict = {}
    seen = ""

    def announce(state: str) -> None:
        nonlocal seen
        if not on_progress or state == seen:
            return
        seen = state
        fraction, words = SUNO_STAGE.get(state, (0.25, f"Suno reports {state}"))
        on_progress(fraction, words, state)

    def step() -> Optional[dict]:
        nonlocal last
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
        return None

    try:
        return _http.poll(step, first=max(1.0, float(interval)),
                          max_wait=max(5.0, float(timeout)), factor=1.25,
                          ceiling=10.0, provider="kie", label="Suno task")
    except KieError:
        raise
    except _http.PollUnknown as exc:
        raise KieError(str(exc), provider="kie") from exc
    except _http.ProviderError as exc:
        raise KieError(
            f"kie/Suno task {task_id} did not finish within {timeout:.0f}s "
            f"(last status {last.get('status') or 'unknown'})",
            provider="kie") from exc


def _free_path(path: Path) -> Path:
    """``path``, or the next name beside it that is not already taken.

    NOTHING GENERATED IS EVER WRITTEN OVER SOMETHING GENERATED, because the file
    that would be destroyed was paid for and its destruction errors nowhere.
    :func:`download_tracks` names takes by their INDEX in the batch, so a second
    generation under the same logical name lands on `<stem>_1.mp3` and
    `<stem>_2.mp3` again — the first batch's bytes are gone and the revisions
    registered against them now describe audio nobody generated. The same shape
    as the cutscene slug collision (bgate_core.cine.cinematic._occupied), and the same
    cost.

    It STEPS ASIDE rather than refusing, which is the opposite of what the
    cutscene path does, and the difference is which layer can act on it. A
    generation there is one call with an `overwrite` argument a human can pass;
    this is a download loop three layers under a caller that has no such flag,
    so a refusal here would turn an ordinary second take into an error nobody
    can clear. A new filename loses nothing and costs nobody a decision.
    """
    if not path.exists():
        return path
    for bump in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{bump}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{int(time.time())}{path.suffix}")


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
        path = _free_path(target / f"{stem}_{index}{suffix or '.mp3'}")
        size = download(url, path, accept="audio/*")
        written.append({**track, "path": str(path), "bytes": size})
    return written


def generate_music(prompt: str, out_dir: str | os.PathLike[str], *,
                   name: str = "", root: Any = None, logical_name: str = "",
                   work_item_id: Optional[int] = None,
                   timeout: float = 900.0, on_submit: Any = None,
                   **options: Any) -> dict:
    """Submit, wait, download every track. Returns {ok, tracks:[{path,...}]}.

    ``on_submit`` IS CALLED WITH THE TASK ID THE INSTANT THERE IS ONE, the same
    hook and for the same reason as :func:`generate_video`. The charge lands at
    submit and the poll loop then runs for MINUTES; a caller that only learns
    the task id from the RETURN VALUE learns it never if the process dies in
    between, and a paid batch with no handle cannot be collected by anything —
    not :func:`bgate_core.audio.music.recover`, which needs the id, and not the sweep
    that looks for uncollected work. Measured: every failure mode this module
    already documents (the CDN 403 on download, a cancel, a timeout) carried the
    id out on the RESULT, which survives a return and not a kill.

    Its failure is swallowed on purpose — bookkeeping must not lose the file it
    was bookkeeping.

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
    # UNKNOWN, STATED, ON EVERY PATH — not absent, and never 0.0. The success
    # path below has always said `credits_source` and left `usd` None
    # when the rate is unconfigured, but a FAILED run returned none of these
    # keys at all, so a caller reading `result.get("credits_consumed", 0)` or
    # summing `usd or 0` scored a charge that may well have happened
    # as free. Same rule estimate_usd holds for video: the two unknowns are
    # named separately and neither folds to zero.
    base = {"ok": False, "provider": "kie", "kind": "audio",
            "model": str(options.get("model") or DEFAULT_SUNO_MODEL),
            "credits_consumed": None, "credits_source": "unavailable",
            "accounted": False, "usd": None}
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
        if on_submit:
            try:
                on_submit(task_id)
            except Exception:                                    # noqa: BLE001
                pass
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

    result = _result.shape({**base, "ok": True, "task_id": task_id,
              "tracks": written, "count": len(written),
              "credits_consumed": spent,
              "credits_source": source,
              "credits_note":
                  "the Suno record carries no creditsConsumed field — this is "
                  "the account balance before minus after, so a concurrent "
                  "generation on the same key would be counted here too",
              "usd": cost_usd(spent),
              "retention_days": SUNO_URL_TTL_DAYS,
              "expires_at": _expires_at(SUNO_URL_TTL_DAYS),
              "callback_failed": bool(rec.get("callback_failed")),
              "seconds": round(time.monotonic() - started, 2)})
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
