"""Image to 3D — a DRAFT MESH from a picture, generated locally.

WHY THIS EXISTS. The 3D path in this product models from primitives (bg_box,
bg_cyl, bg_ball, mirrored and tapered), and the ceiling of that approach is a
blockout: a correctly-proportioned 1.8 m mannequin with paddle arms, mitten
hands and no face, which every gate here reports green because the gates measure
well-formedness and not resemblance. Meanwhile the 2D path — anchored
generation, trained styles, chroma keying — is the strongest thing in the
product, and until now it terminated in a PNG. A real user drove an excellent
stylised character through it and believed they had a 3D model. They did not.

WHAT THIS IS NOT. It is not a finished-asset generator, and there is deliberately
no path from here to godot.deliver_asset. What comes back is a DRAFT MESH:
unpredictable topology, no armature, no scale convention, no orientation
guarantee, and quite possibly the background baked in as geometry. It enters the
existing pipeline at the draft stage and goes through the same conditioning and
the same gates as anything modelled by hand — scaled to 1.8 m, oriented to +Y,
bg_clean'd, decimated, weighted to the canonical skeleton, assembled by
blender_combine. Builders Gate is the harness that turns an inconsistent
generated mesh into an inspected, engine-ready asset; it is not trying to be the
best raw generator. Nothing here skips validation.

LOCAL FIRST, AND THAT IS THE POINT. This product is local-first everywhere else
— loopback dashboard, SQLite state, .env at the game project root, no Docker —
and renting a mesh generator would have been the one place it stopped being
true. So the primary backends are open-weight models running on the user's own
GPU, reached over loopback HTTP. The hosted APIs are kept behind the same
interface as a fallback and as the comparison baseline, not as the design
centre.

FOUR THINGS THIS MODULE OWES ITS CALLERS, in the order they bite:

  * IT IMPORTS NOTHING HEAVY. No torch, no CUDA, no model library, ever, at any
    point in this process. The GPU is probed with `nvidia-smi` (a hundred
    milliseconds, always present with the driver) and a local server is probed
    with a short HTTP GET. transcribe.py established this rule for
    faster-whisper and the reason is the same: the MCP server must not host an
    inference stack, and a user with no GPU must be completely unaffected.

  * IT WORKS WITH NOTHING CONFIGURED. status() reports every backend and why
    each is or is not usable, the way blender.available() and
    imagegen.available() do. No key is read at import time; no server is
    contacted at import time.

  * IT PRICES A REQUEST, NOT A BACKEND. Locally the price is zero and saying so
    is the honest answer. On a hosted backend, texture, PBR, rigging and quad
    remeshing are separately billed, so a quote that reads only the backend
    name under-quotes the request the art seat actually makes — the lesson
    krea.price_for learned from usd_with_style_refs, and the same shape.

  * IT STATES THE LICENCE BEFORE IT GENERATES. A game asset produced under a
    non-commercial, revenue-capped or region-restricted licence is a legal
    problem, not a technical one, and this tool does not know its user's
    revenue, territory or monthly actives. Every backend carries its terms in
    the table, status() surfaces them, and generate() puts them on the result so
    the manifest records what the asset was made under. A backend whose licence
    has conditions NEVER becomes an automatic choice.

No key is ever logged, returned, or put on a command line. Everything here is
stdlib.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from bgate_core.store import envfile

from . import _http
from . import _result as _shape

# Windows: keep every subprocess from flashing a console window. Same constant,
# same reason, as blender.py and transcribe.py.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ImageTo3DError(_http.ProviderError):
    """A generation failed in a way the caller should surface, not retry blindly."""


# ---------------------------------------------------------------------------
# Licences. A first-class field, not a note.
# ---------------------------------------------------------------------------
# COMMERCIAL USE IS A GATE. Builders Gate is a tool other people ship games
# with; it does not know their revenue, their territory or their monthly
# actives, so it cannot decide a conditional licence on their behalf. Three
# codes, and the middle one is the dangerous one because it looks like the first
# right up until someone's game succeeds.
FREE = "unrestricted"         # MIT / Apache / an explicit grant. Ship anything.
CONDITIONAL = "conditional"   # commercial, but only under stated limits
FORBIDDEN = "non-commercial"  # cannot be used for a shipped asset at all

# Only a FREE backend may be picked automatically. Anything CONDITIONAL has to
# be named by a human who has read the condition — see choose().
AUTO_LICENCES = (FREE,)


# THE MODEL'S LICENCE, NOT THE RUNNER'S. A local backend is a transport: ComfyUI
# is MIT and so is its 3D node pack, but what actually generated the mesh is
# whichever model the graph loaded, and THAT is what decides whether a shipped
# game asset is clear. The two are routinely conflated, and the conflation is
# always in the permissive direction.
#
# So a user running a local server DECLARES which model is behind it
# (BGATE_LOCAL_MODEL), and this table turns that into a licence the manifest can
# record. Undeclared is not assumed permissive — it resolves to CONDITIONAL with
# the reason, because "we do not know" and "it is fine" are different answers.
MODEL_LICENCES: dict[str, dict] = {
    "trellis": {
        "code": FREE, "label": "TRELLIS (Microsoft)",
        "summary": "MIT, code and weights. No revenue cap, no territory "
                   "restriction, no user threshold.",
        "url": "https://github.com/microsoft/TRELLIS",
    },
    "trellis2": {
        "code": FREE, "label": "TRELLIS.2-4B (Microsoft)",
        "summary": "MIT, code and weights. Note that the nvdiffrast/nvdiffrec "
                   "rendering dependencies carry NVIDIA non-commercial CODE "
                   "licences — generated output is unaffected, but embedding "
                   "those libraries in a shipped product is a separate "
                   "question.",
        "url": "https://huggingface.co/microsoft/TRELLIS.2-4B",
    },
    "triposr": {
        "code": FREE, "label": "TripoSR",
        "summary": "MIT, code and weights. Weakest quality here — a draft pass.",
        "url": "https://github.com/VAST-AI-Research/TripoSR",
    },
    "instantmesh": {
        "code": FREE, "label": "InstantMesh",
        "summary": "Apache-2.0. Unconditional for generated assets. (Its "
                   "multi-view stage is Zero123++-derived, so check the "
                   "specific weights you run.)",
        "url": "https://github.com/TencentARC/InstantMesh",
    },
    "hunyuan3d": {
        "code": CONDITIONAL, "label": "Hunyuan3D 2.x (Tencent)",
        "summary": "Tencent Hunyuan 3D Community License. The grant EXCLUDES "
                   "the European Union, the United Kingdom and South Korea "
                   "entirely, and above 1 million monthly active users you "
                   "must request written permission from Tencent. Tencent "
                   "claims no rights in outputs, but the AUP requires AI "
                   "content to be labelled and forbids using outputs to train "
                   "another model. For a game sold worldwide on Steam the "
                   "territory clause is a real distribution problem.",
        "url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE",
    },
    "sf3d": {
        "code": CONDITIONAL, "label": "Stable Fast 3D (Stability)",
        "summary": "Stability AI Community License. Free below US$1,000,000 in "
                   "annual organisation-wide revenue, 'regardless of the "
                   "source of that revenue'; above it an Enterprise licence is "
                   "required, and the licence explicitly covers OUTPUTS. Also "
                   "gated on HuggingFace — the weights need an accepted "
                   "licence and a read token.",
        "url": "https://stability.ai/license",
    },
    "spar3d": {
        "code": CONDITIONAL, "label": "SPAR3D (Stability)",
        "summary": "Stability AI Community License, same US$1,000,000 "
                   "threshold, outputs explicitly covered. HuggingFace-gated.",
        "url": "https://stability.ai/license",
    },
    "partpacker": {
        "code": FORBIDDEN, "label": "PartPacker (NVIDIA)",
        "summary": "NVIDIA Non-Commercial licence. Cannot be used for a "
                   "shipped game asset at all, however good the Windows "
                   "install story is.",
        "url": "https://huggingface.co/nvidia/PartPacker",
    },
    "zero123plus": {
        "code": FORBIDDEN, "label": "Zero123++ / Stable Zero123",
        "summary": "CC-BY-NC weights. Non-commercial — not shippable.",
        "url": "https://github.com/SUDO-AI-3D/zero123plus",
    },
}

# Which model is behind the local server. "" means undeclared.
MODEL_ENV = "BGATE_LOCAL_MODEL"

_UNDECLARED = {
    "code": CONDITIONAL,
    "label": "undeclared",
    "summary": "the model behind this server has not been declared, so its "
               "licence is unknown and the mesh cannot be cleared for a "
               "shipped asset. Set " + MODEL_ENV + " to one of: "
               + ", ".join(sorted(MODEL_LICENCES)),
    "url": "",
}


def declared_model() -> str:
    """Which open-weight model the local server is running, per the user."""
    return (os.environ.get(MODEL_ENV) or "").strip().lower()


def model_licence(name: str = "") -> dict:
    """The licence of a named model, or the undeclared answer.

    An unknown NAME is treated exactly like an undeclared one — a typo must not
    silently become permission.
    """
    key = (name or declared_model()).strip().lower()
    found = MODEL_LICENCES.get(key)
    if not found:
        return dict(_UNDECLARED)
    return {**found, "model": key}


def effective_licence(spec: dict) -> dict:
    """The licence a backend's OUTPUT actually carries.

    A transport backend (ComfyUI, a Gradio app) has no licence of its own — the
    model behind it does. Resolve from the declared model when the user has
    said, and keep the row's "we cannot tell you" wording when they have not.

    This is one function because it used to be two: available() resolved, and
    generate()'s result copied the raw spec row. So the report an agent read
    before generating named Hunyuan3D's region exclusions, and the manifest
    recording what the mesh was MADE under said "this adapter cannot clear the
    licence for you" — the uninformative one, written to the durable record.
    """
    licence = dict(spec.get("licence") or {})
    if spec.get("licence_from_model"):
        declared = model_licence()
        if declared.get("model"):
            licence = declared
    return licence


# ---------------------------------------------------------------------------
# What the pipeline can take
# ---------------------------------------------------------------------------
# .glb and nothing else by default: blender.COMBINE_SUFFIXES accepts
# .glb/.gltf/.blend and godot.deliver_asset takes a .glb. A format that would
# need a conversion step nobody wrote is not a supported format.
DEFAULT_FORMAT = "glb"
USABLE_FORMATS = ("glb", "gltf")

# What an input plate must be to have any chance. Checked locally, before
# anything is generated — see check_input().
INPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
INPUT_MIN_SIDE = 256          # below this there is nothing to reconstruct from
INPUT_GOOD_SIDE = 1024        # below this, detail is invented rather than read


# ---------------------------------------------------------------------------
# The GPU, probed the cheap way
# ---------------------------------------------------------------------------
# NEVER `import torch` HERE. Not in a try, not behind a flag, not lazily inside
# a function this process calls. torch is a multi-second import that pins memory
# and, on a CPU-only wheel, answers a question nobody asked. The two facts that
# actually decide whether local generation is possible — is there an NVIDIA GPU
# and how much VRAM does it have — come from nvidia-smi, which ships with the
# driver, lives on PATH, and answers in about a tenth of a second.
#
# MEASURED on the target box (2026-07-30): `nvidia-smi --query-gpu=...` returns
# "NVIDIA GeForce RTX 3060, 12288 MiB, 595.95" instantly, while the same
# machine's default interpreter has torch 2.8.0+cpu — CUDA unavailable. Probing
# via torch would have reported "no GPU" on a machine with a perfectly good one.
_SMI_QUERY = "name,memory.total,driver_version"
_SMI_TIMEOUT = 10.0

# Enough VRAM to be worth trying at all, in GB. Below this every open-weight
# image-to-3D model is out — the smallest shape-only configurations documented
# want ~6 GB, and adding texture roughly doubles it.
MIN_VRAM_GB = 6.0

_gpu_cache: Optional[dict] = None


def gpu(*, refresh: bool = False) -> dict:
    """What GPU this machine has, via nvidia-smi. Never imports torch.

    Returns {available, name, vram_gb, driver, reason}. An absent nvidia-smi is
    reported as "no NVIDIA GPU", which is the useful answer — it is installed by
    the driver, so its absence and the driver's absence are the same fact.

    Cached, because status() may be polled by a dashboard and spawning a process
    per poll is how a health check becomes a load source.
    """
    global _gpu_cache
    if _gpu_cache is not None and not refresh:
        return dict(_gpu_cache)
    out = {"available": False, "name": "", "vram_gb": 0.0, "driver": "",
           "reason": ""}
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_SMI_QUERY}",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=_SMI_TIMEOUT,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        out["reason"] = ("nvidia-smi not found — no NVIDIA driver on this "
                         "machine, so local generation is not available. "
                         "Everything else in Builders Gate is unaffected.")
        _gpu_cache = out
        return dict(out)
    except Exception as exc:                                     # noqa: BLE001
        out["reason"] = f"could not query the GPU ({type(exc).__name__}: {exc})"
        _gpu_cache = out
        return dict(out)
    line = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not line:
        out["reason"] = ((proc.stderr or "").strip()[:200]
                         or "nvidia-smi returned nothing")
        _gpu_cache = out
        return dict(out)
    parts = [p.strip() for p in line[0].split(",")]
    out["name"] = parts[0] if parts else ""
    if len(parts) > 1:
        match = re.search(r"(\d+)", parts[1])
        if match:
            out["vram_gb"] = round(int(match.group(1)) / 1024.0, 1)
    out["driver"] = parts[2] if len(parts) > 2 else ""
    out["available"] = bool(out["name"])
    if out["available"] and out["vram_gb"] and out["vram_gb"] < MIN_VRAM_GB:
        out["reason"] = (
            f"{out['name']} has {out['vram_gb']} GB of VRAM; every open-weight "
            f"image-to-3D model documented needs at least {MIN_VRAM_GB} GB for "
            "shape alone, and roughly double that with texture")
    return dict(_gpu_cache := out)


def fits_vram(need_gb: Optional[float], *, refresh: bool = False) -> dict:
    """Would a model wanting `need_gb` fit on this machine?

    Returns {ok, reason, vram_gb}. An unknown requirement (None) is NOT treated
    as a pass — it comes back ok=True with the uncertainty stated, because
    refusing on a number nobody published would block a model that might run
    perfectly well, and claiming it fits would be a guess dressed as a check.
    """
    card = gpu(refresh=refresh)
    if not card["available"]:
        return {"ok": False, "vram_gb": 0.0, "reason": card["reason"]}
    have = float(card["vram_gb"] or 0.0)
    if need_gb is None:
        return {"ok": True, "vram_gb": have,
                "reason": "this model publishes no VRAM figure — it was not "
                          "checked, only reported"}
    if have and have + 0.001 < float(need_gb):
        return {"ok": False, "vram_gb": have,
                "reason": f"needs about {need_gb} GB of VRAM and this machine "
                          f"has {have} GB ({card['name']})"}
    return {"ok": True, "vram_gb": have, "reason": ""}


def runner_python() -> str:
    """The interpreter that would run a local model, if a caller wants one.

    BGATE_IMAGETO3D_PYTHON, and it defaults to nothing rather than to
    sys.executable. That default is deliberate and it is the opposite of
    transcribe.whisper_python's: faster-whisper installs beside this tool, and
    an inference stack does not. MEASURED on the target box — Python 3.13 and
    3.12 installed, torch 2.8.0+cpu in the default one, and every open-weight
    image-to-3D repo pinning 3.10/3.11 with a CUDA build. Pointing this at
    sys.executable would produce a confident failure instead of an honest
    "not configured".
    """
    return (os.environ.get("BGATE_IMAGETO3D_PYTHON") or "").strip()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
# Keyed by what a caller passes as ``backend=``. LOCAL FIRST — the hosted rows
# exist as a fallback and as the honest comparison baseline, not as the design
# centre.
#
# Fields, and why each exists:
#
#   kind         "local" | "hosted". Decides almost everything else: a local
#                backend has no key, no price and no rate limit, and a hosted
#                one has no VRAM requirement and no install story.
#   env          the environment variable holding the key, or "" for a backend
#                that needs none. "" is NOT the same as missing.
#   base         API root. Local backends declare `base_env` too, because the
#                whole point is that it is the operator's own box and port.
#   submit_path  where a generation is POSTed.
#   poll_path    GET template with {task}; "" means the backend answers
#                synchronously and there is nothing to poll.
#   image_mode   how the plate travels: "field" (inline in the JSON body),
#                "file_token" (a separate upload endpoint hands back a token),
#                "multipart" (the bytes go up with the request itself).
#   fields       our option name -> theirs. The backends agree on nothing:
#                `texture` vs `should_texture`, `face_limit` vs
#                `target_polycount` vs `face_count`. Renaming in a table beats a
#                builder function per backend, because the differences are all
#                naming and nesting.
#   credits      what each part of a request costs in the backend's OWN unit.
#   usd_per_credit  the PUBLISHED conversion, or None. None propagates:
#                price_for returns None and the caller must ask a human rather
#                than spend. krea.TRAIN_USD settled this — an invented price is
#                worse than a missing one, because a reader treats a number as
#                a quote.
#   vram_gb      local only. What the docs say inference needs.
#   weights      local only. Where the weights come from and how big they are.
#   windows      local only. Whether it installs on native Windows, which is the
#                question that actually decides feasibility on the target box.
#   licence      {code, summary, url}. code is FREE / CONDITIONAL / FORBIDDEN.
#   implemented  False for a backend that is documented here as an alternative
#                but whose transport is not wired. status() says so plainly
#                rather than letting a caller discover it at request time.
#
# EVERY NUMBER CAME OFF A PUBLIC DOCUMENT, and nothing here has been paid for or
# installed. Where a document stated none, the field is None and stays None.
BACKENDS: dict[str, dict] = {
    # ---- LOCAL ----------------------------------------------------------
    #
    # A ComfyUI instance on loopback with a 3D custom-node pack installed. This
    # is the realistic Windows path, for a reason that has nothing to do with
    # model quality: every open-weight repo here leans on custom CUDA extensions
    # (nvdiffrast, diffoctreerast, diff-gaussian-rasterization, custom
    # rasterizers, spconv, diso) that must be COMPILED against MSVC and a
    # matching CUDA toolkit, and a ComfyUI node pack is where a Windows user
    # gets those as prebuilt wheels instead. A model that is technically better
    # but does not install is worse than one that does.
    #
    # WHICH PACK, AND THE CORRECTION WORTH RECORDING: the obvious answer is
    # ComfyUI-3D-Pack, and as of this writing it is a trap for a fresh install.
    # Its prebuilt wheel matrix tops out at Python 3.12 with torch 2.7.0 and has
    # no Python 3.13 entry at all, its last commit was 2025-11-17, and when no
    # prebuild matches the runtime its installer falls back to compiling from
    # source — which is precisely the thing it was chosen to avoid. A current
    # ComfyUI ships a newer Python and torch than anything in that matrix.
    #
    # The pack that does work without a compiler is visualbruno/ComfyUI-Trellis2
    # (TRELLIS.2, all five CUDA extensions shipped as win_amd64 wheels in-repo,
    # Python 3.11 and 3.13, torch 2.7/2.8/2.10, current to mid-2026) — and
    # TRELLIS.2 is MIT, which makes it the only local option whose output is
    # unconditionally shippable. See MODEL_LICENCES.
    #
    # THE WORKFLOW IS THE USER'S, NOT OURS. ComfyUI takes a whole graph as the
    # request body, and that graph names node classes whose exact spelling
    # changes with every 3D-Pack release. Hardcoding one would make this adapter
    # break on somebody else's upgrade. So the workflow is config: an
    # API-format JSON exported from their own ComfyUI, with two placeholders
    # this module substitutes. That is not a cop-out — a workflow graph IS a
    # per-installation artefact, and treating it as one is the correct model.
    "comfy": {
        "kind": "local",
        "label": "ComfyUI (local, ComfyUI-3D-Pack)",
        "env": "",
        "base": "http://127.0.0.1:8188",
        "base_env": "BGATE_COMFY_URL",
        "workflow_env": "BGATE_COMFY_WORKFLOW",
        # /api-PREFIXED, deliberately. ComfyUI's server registers every route
        # twice — bare and under /api — and the /api form is the stable one; the
        # bare paths share a namespace with the web UI's own SPA routing.
        "submit_path": "/api/prompt",
        "poll_path": "/api/history/{task}",
        "health_path": "/api/system_stats",
        "upload_path": "/api/upload/image",
        "view_path": "/api/view",
        "upload_field": "image",
        "auth": "none",
        "image_mode": "workflow",
        "task_key": "prompt_id",
        # THE PROTOCOL, NAMED. ComfyUI has no status field: /history is empty
        # until the run is done and populated afterwards, so "the key appeared"
        # IS the completion signal. This used to be a `backend == "comfy"`
        # branch in poll(), which meant the second ComfyUI row polled a status
        # field that does not exist, read it as "still running", and sat there
        # until the timeout on a job that had finished in seconds.
        "poll_style": "history",
        "usd": 0.0,
        # Whatever the graph does. The adapter cannot know, and pretending
        # otherwise would put a false capability list in front of an agent.
        "supports": {"seed"},
        "formats": ("glb",),
        "rigged": False,
        "vram_gb": None,
        "weights": "whatever the workflow's nodes load, into the HuggingFace "
                   "cache of the ComfyUI interpreter — TRELLIS is ~3.3 GB, "
                   "TRELLIS.2-4B ~16 GB, Hunyuan3D-2 25 GB and up",
        "windows": "the only realistic zero-compile route. Use "
                   "visualbruno/ComfyUI-Trellis2, which ships win_amd64 wheels "
                   "for every CUDA extension it needs. ComfyUI-3D-Pack has no "
                   "Python 3.13 wheels, nothing above torch 2.7.0, and silently "
                   "falls back to compiling when nothing matches",
        "latency_s": None,
        # The graph decides which model runs, so the licence comes from the
        # declared model rather than from this row. ComfyUI and its node packs
        # being MIT says nothing about the weights.
        "licence_from_model": True,
        "licence": {
            "code": CONDITIONAL,
            "summary": "ComfyUI and its 3D node packs are MIT, but the LICENCE "
                       "THAT MATTERS IS THE MODEL'S and the graph decides which "
                       "model runs. This adapter cannot read the graph, so it "
                       "cannot clear the licence for you — declare the model "
                       "with " + MODEL_ENV + " and it will.",
            "url": "https://github.com/visualbruno/ComfyUI-Trellis2",
        },
        "note": "Needs a running ComfyUI with a 3D node pack and an API-format "
                "workflow exported to BGATE_COMFY_WORKFLOW.",
    },
    # PART-AWARE GENERATION, and it is the single biggest lever on rig quality
    # in this whole pipeline — which is why it gets its own row rather than a
    # flag on the one above.
    #
    # A monolithic generated character is ONE watertight-ish blob, or (measured
    # on a real user's asset) 940 fragmented shells with no relationship to
    # anatomy. Bone heat then has to guess where the arm stops and the torso
    # starts, and the loose islands weight to whichever bone is nearest — which
    # is how a character ends up with fingers bound to the hip. A part-aware
    # model (PartCrafter, OmniPart, FullPart and the rest of that 2025-26 line)
    # emits SEMANTICALLY SEPARATE meshes from the same single image: a head, a
    # torso, two arms, two legs. Every downstream step in this module gets
    # easier at once — landmarks are measured per part instead of inferred from
    # a vertex cloud, weighting can be per part, and blender_combine already
    # takes exactly that shape as its input.
    #
    # SAME SERVER, SAME TRANSPORT, DIFFERENT GRAPH. This is not a second
    # integration: it is the ComfyUI row with a different workflow env and a
    # collector that keeps every mesh instead of the first. Splitting it out
    # means `supports` can answer "parts" honestly, and a user who has one graph
    # and not the other gets a straight answer about which.
    "comfy-parts": {
        "kind": "local",
        "label": "ComfyUI (local, part-aware image-to-3D)",
        "env": "",
        "base": "http://127.0.0.1:8188",
        "base_env": "BGATE_COMFY_URL",
        "workflow_env": "BGATE_COMFY_PARTS_WORKFLOW",
        "submit_path": "/api/prompt",
        "poll_path": "/api/history/{task}",
        "health_path": "/api/system_stats",
        "upload_path": "/api/upload/image",
        "view_path": "/api/view",
        "upload_field": "image",
        "auth": "none",
        "image_mode": "workflow",
        "task_key": "prompt_id",
        "poll_style": "history",
        "usd": 0.0,
        "supports": {"seed", "parts"},
        "formats": ("glb",),
        "rigged": False,
        "vram_gb": None,
        "weights": "whatever the graph loads — PartCrafter is ~2.5 GB on top "
                   "of its base, and the part-aware models are generally "
                   "SMALLER than the monolithic ones because they generate "
                   "each part at lower resolution",
        "windows": "same story as the row above: a node pack shipping prebuilt "
                   "wheels is the only realistic zero-compile route",
        "latency_s": None,
        "licence_from_model": True,
        "licence": {
            "code": CONDITIONAL,
            "summary": "the graph decides which model runs and therefore which "
                       "licence applies. PartCrafter and OmniPart are research "
                       "releases — READ THE MODEL CARD before shipping a "
                       "character made from one. Declare it with " + MODEL_ENV
                       + " and this row will state the terms.",
            "url": "https://github.com/wgsxm/PartCrafter",
        },
        "note": "Needs a running ComfyUI and a part-aware workflow exported to "
                "BGATE_COMFY_PARTS_WORKFLOW whose saver writes ONE FILE PER "
                "PART. A graph that merges the parts before saving works, and "
                "produces exactly the monolith this row exists to avoid.",
    },
    # A prebuilt Windows EXECUTABLE. No Python, no CUDA toolkit, no compilation
    # of anything — the release zip carries trellis-server.exe and the CUDA
    # runtime DLLs. POST multipart, get raw GLB bytes back. It is by a distance
    # the cleanest contract and the easiest install surveyed, and at q8
    # quantisation it wants about 9.5 GB, which fits a 12 GB card.
    #
    # AND IT HAS NO DECLARED LICENCE, which is why it is not the default. An
    # unlicensed repository is not permissive by omission — the absence of a
    # LICENSE file means all rights reserved, and that is a worse position for a
    # shipped game asset than any of the restrictive licences here, because
    # there is nothing to read. The MODEL it runs (TRELLIS.2) is MIT; the SERVER
    # is not licensed at all. Those are different questions and only the second
    # one is unanswered.
    "trellis-cpp": {
        "kind": "local",
        "label": "trellis.cpp (prebuilt Windows server, TRELLIS.2 GGUF)",
        "env": "",
        "base": "http://127.0.0.1:8080",
        "base_env": "BGATE_TRELLIS_CPP_URL",
        "submit_path": "/generate",
        "poll_path": "",                 # synchronous: the response IS the GLB
        "health_path": "/health",
        "auth": "none",
        "image_mode": "multipart",
        "upload_field": "image",
        "response": "binary",
        "fields": {"seed": "seed", "resolution": "resolution"},
        "usd": 0.0,
        "supports": {"seed", "resolution"},
        "formats": ("glb",),
        "rigged": False,
        # q8 is the 12 GB target: f16 ~16.5 GB, q8 ~9.5 GB and near-lossless,
        # q4 ~6 GB.
        "vram_gb": 9.5,
        "weights": "ilintar/trellis2-gguf on HuggingFace, fetched by the "
                   "installer. The Windows release zip itself is ~728 MB and "
                   "bundles the CUDA runtime, so no CUDA toolkit is needed",
        "windows": "the easiest install surveyed — a prebuilt "
                   "trellis-cuda-windows-x64.zip with trellis-server.exe and "
                   "the CUDA DLLs. Nothing is compiled",
        "latency_s": None,
        "licence": {
            "code": CONDITIONAL,
            "summary": "THE SERVER HAS NO DECLARED LICENCE. No LICENSE file "
                       "means all rights reserved, not permissive-by-default, "
                       "and that is a worse position than an explicit "
                       "restriction because there is nothing to read. The model "
                       "it runs (TRELLIS.2) is MIT and its outputs are clear; "
                       "the question is the server binary, not the mesh. Ask "
                       "upstream before depending on it.",
            "url": "https://github.com/pwilkin/trellis.cpp",
        },
        "note": "Synchronous — POST the plate, get GLB bytes. Default "
                "127.0.0.1:8080. Requests are serialised behind a mutex, so "
                "one at a time.",
    },
    # Tencent's own FastAPI server — the first open-weight image-to-3D repo to
    # ship a documented, non-Gradio HTTP contract.
    #
    # TARGETED AT 2.0, NOT 2.1, AND THAT IS THE OPPOSITE OF THE OBVIOUS CHOICE.
    # Three reasons, all from reading the two servers:
    #   * 2.0 needs NO compiled CUDA extension for shape-only generation, and 6
    #     GB of VRAM. 2.1 wants 10 GB for shape and 21 GB for texture, and its
    #     documented install runs a bash script.
    #   * 2.1's worker READS EVERY PARAMETER AND USES NONE OF THEM. Its generate
    #     method touches params["image"] and calls the pipeline with that alone
    #     — seed, octree_resolution, steps, guidance, face_count and texture are
    #     all accepted, validated, and discarded.
    #   * 2.1's /send can deadlock: on a texture failure the worker falls back
    #     to the untextured mesh and succeeds, but /status only reports
    #     "completed" once the TEXTURED file exists, so the job reports
    #     "texturing" forever.
    # On 2.1, use the blocking POST /generate instead of /send.
    "hunyuan-local": {
        "kind": "local",
        "label": "Hunyuan3D 2.0 (self-hosted api_server.py)",
        "env": "",
        "base": "http://127.0.0.1:8081",
        "base_env": "BGATE_HUNYUAN3D_URL",
        "submit_path": "/send",
        "poll_path": "/status/{task}",
        "health_path": "/health",
        "auth": "none",
        "image_mode": "field",
        "task_key": "uid",
        "status_key": "status",
        "done": ("completed",),
        "dead": ("error", "failed"),
        "running": ("processing", "pending", "queued", "texturing"),
        # /status hardcodes a .glb filename while the worker writes
        # <uid>.<type>, so type="obj" polls for a file that will never appear.
        # `formats` below advertises glb only for exactly that reason.
        # The finished model comes back INLINE as base64 on the status
        # response, not as a URL. download() has to know the difference.
        "result_b64_keys": ("model_base64",),
        "static": {"type": "glb"},
        "fields": {"image": "image", "texture": "texture",
                   "face_count": "face_count", "seed": "seed",
                   "steps": "num_inference_steps",
                   "guidance": "guidance_scale",
                   "octree_resolution": "octree_resolution"},
        "usd": 0.0,
        "supports": {"texture", "face_count", "seed", "steps", "guidance",
                     "octree_resolution"},
        # glb ONLY, not because the server refuses obj but because its /status
        # route cannot see an obj it just wrote. See above.
        "formats": ("glb",),
        "rigged": False,
        # 2.0: 6 GB shape-only, 16 GB with texture. Texture also needs the
        # compiled rasteriser and the server started with --enable_tex, so on a
        # 12 GB card the supported configuration is shape-only.
        "vram_gb": 16.0,
        "vram_gb_shape_only": 6.0,
        "weights": "tencent/Hunyuan3D-2.1 on HuggingFace, fetched on first run "
                   "into the server's own cache. Not small: the 2.x model "
                   "repos run 25 GB for mini and up to ~75 GB for the full "
                   "set, so first run is a long download, not a pause",
        "windows": "SHAPE-ONLY NEEDS NO COMPILER on 2.0 — its requirements are "
                   "all plain wheels and it ships a pure-Python fallback "
                   "renderer plus a .bat (2.1 ships only a .sh). TEXTURE is "
                   "the part that needs custom_rasterizer built with MSVC and "
                   "a matching CUDA toolkit, and is the part that commonly "
                   "fails on Windows. YanWenKun/Hunyuan3D-2-WinPortable is the "
                   "zero-compile bundle",
        "latency_s": (30, 180),
        "licence": {
            "code": CONDITIONAL,
            "summary": "Tencent Hunyuan 3D Community License. The grant "
                       "EXCLUDES the European Union, the United Kingdom and "
                       "South Korea entirely, and above 1 million monthly "
                       "active users you must request a licence from Tencent, "
                       "granted at their discretion. Tencent claims no rights "
                       "in generated outputs, and outputs may not be used to "
                       "train another 3D model.",
            "url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1/blob/main/LICENSE",
        },
        "note": "Start it with `python api_server.py --host 127.0.0.1 --port "
                "8081` in the Hunyuan3D-2 checkout — it binds 0.0.0.0 by "
                "default, so pass --host. Leave --enable_tex off for the "
                "shape-only path that needs no compiler. Free to run; see the "
                "licence.",
    },
    # The escape hatch for everything that ships only a Gradio demo — TRELLIS,
    # Stable Fast 3D, SPAR3D, TripoSR. Gradio auto-exposes a REST API, so this
    # is reachable; what it is NOT is a stable contract, because the endpoint
    # names are whatever that repo's app.py declared this week. Implemented as
    # a named, configurable thing rather than pretended into a first-class
    # backend.
    "gradio-local": {
        "kind": "local",
        "label": "a local Gradio app (TRELLIS / SF3D / TripoSR)",
        "env": "",
        "base": "http://127.0.0.1:7860",
        "base_env": "BGATE_GRADIO3D_URL",
        "api_name_env": "BGATE_GRADIO3D_ENDPOINT",
        "submit_path": "/gradio_api/call/{api_name}",
        "poll_path": "/gradio_api/call/{api_name}/{task}",
        "health_path": "/config",
        "auth": "none",
        "image_mode": "field",
        "usd": 0.0,
        "supports": {"seed"},
        "formats": ("glb",),
        "rigged": False,
        "vram_gb": None,
        "weights": "whichever app is running, into its own HuggingFace cache "
                   "on first run — TripoSR ~1.7 GB, TRELLIS ~3.3 GB, Stable "
                   "Fast 3D ~4 GB, SPAR3D ~7.3 GB. Stability's two are GATED: "
                   "the licence has to be accepted on HuggingFace and a read "
                   "token is needed at runtime",
        "windows": "depends entirely on the app. TRELLIS is documented "
                   "Linux-only upstream; Stable Fast 3D calls its own Windows "
                   "support experimental and compiles texture_baker and "
                   "uv_unwrapper from source; TripoSR needs torchmcubes built "
                   "from git, which silently builds CPU-only when CUDA is not "
                   "found and then fails at run time",
        "latency_s": None,
        "implemented": False,
        "licence_from_model": True,
        "licence": {
            "code": CONDITIONAL,
            "summary": "Whatever the running app's model is licensed under — "
                       "declare it with " + MODEL_ENV + ". TRELLIS and TripoSR "
                       "are MIT and unrestricted; Stable Fast 3D and SPAR3D are "
                       "the Stability Community License, free below US$1M "
                       "annual revenue with outputs explicitly covered.",
            "url": "",
        },
        "note": "Documented, not wired. Gradio's endpoint names are per-app "
                "and per-version, so this needs the app named before it can be "
                "called — see the design note.",
    },

    # ---- HOSTED (fallback and baseline) ---------------------------------
    #
    # Kept behind the same interface deliberately. They are the comparison the
    # local decision was made against, and the answer for a user with no GPU.
    #
    # STABILITY IS FIRST HERE ON PURPOSE. It is the cheapest, the simplest
    # (synchronous — POST the image, get GLB bytes back, no polling at all) and
    # it has the cleanest terms of any hosted option: the API terms say you own
    # what you generate, with NO revenue threshold. The US$1M figure people
    # quote belongs to the Community License, which governs SELF-HOSTING the
    # weights — a different transaction from paying per credit for the hosted
    # endpoint. Worth writing down because conflating the two is the obvious
    # mistake.
    "stability": {
        "kind": "hosted",
        "label": "Stability AI — Stable Fast 3D",
        "env": "STABILITY_API_KEY",
        "base": "https://api.stability.ai",
        "submit_path": "/v2beta/3d/stable-fast-3d",
        "poll_path": "",                 # synchronous: the response IS the GLB
        "auth": "bearer",
        "image_mode": "multipart",
        "upload_field": "image",
        "response": "binary",
        "fields": {"texture_resolution": "texture_resolution",
                   "foreground_ratio": "foreground_ratio",
                   "quad": "remesh", "face_count": "vertex_count"},
        "credits": {"base": 10},
        "usd_per_credit": 0.01,          # published
        "supports": {"quad", "face_count", "texture_resolution",
                     "foreground_ratio"},
        "formats": ("glb",),
        "rigged": False,
        "latency_s": (1, 3),
        "licence": {
            "code": FREE,
            "summary": "The hosted API's terms state you own the content you "
                       "generate, with no revenue threshold and no territory "
                       "restriction. The US$1,000,000 cap in the Stability "
                       "Community License governs SELF-HOSTING the weights, "
                       "which is a different transaction — do not conflate "
                       "them.",
            "url": "https://platform.stability.ai/legal/terms-of-service",
        },
        "note": "$0.10 a generation, synchronous, no job to poll. The cleanest "
                "hosted terms of the four and the cheapest.",
    },
    "tripo": {
        "kind": "hosted",
        "label": "Tripo (v2 openapi)",
        "env": "TRIPO_API_KEY",
        # v2 DELIBERATELY. Tripo runs two API surfaces on two doc sites with
        # INCOMPATIBLE response schemas (v2 `data.output.model`, v3
        # `output.model_url`), and v2 is the better documented. Pinning one is
        # the difference between a working poller and one reading a field the
        # server never sends.
        "base": "https://api.tripo3d.ai/v2/openapi",
        "submit_path": "/task",
        "poll_path": "/task/{task}",
        "upload_path": "/upload",
        "upload_field": "file",
        "upload_token_key": "data.image_token",
        "auth": "bearer",
        # Tripo does NOT accept a base64 data URI. The plate goes to /upload
        # first and the task references the token — a two-hop dance for every
        # local file, and a 400 if you skip it.
        "image_mode": "file_token",
        "file_type": "png",
        "task_key": "data.task_id",
        "status_key": "data.status",
        "done": ("success",),
        "dead": ("failed", "banned", "expired", "cancelled"),
        "running": ("queued", "running", "unknown"),
        # pbr_model first: with pbr=true the textured result is there and
        # `model` is the plain one. Taking `model` would silently ship the
        # cheaper output nobody asked for.
        "result_keys": ("data.output.pbr_model", "data.output.model",
                        "data.output.base_model"),
        # FIVE MINUTES, and it shapes the integration. A finished URL cannot be
        # filed on the board and fetched later — the download must happen inside
        # the polling loop, in the same run. generate() does exactly that.
        "url_ttl_s": 300,
        "static": {"type": "image_to_model"},
        "default_model": "v2.5-20250123",
        "fields": {"file": "file", "texture": "texture", "pbr": "pbr",
                   "quad": "quad", "face_count": "face_limit",
                   "seed": "model_seed", "model": "model_version",
                   "orientation": "orientation"},
        "credits": {"base": 30, "untextured": 20, "quad": 5,
                    "detailed_texture": 10, "rig": 25, "rig_check": 0,
                    "retarget": 10},
        "usd_per_credit": 0.01,          # published
        "supports": {"texture", "pbr", "quad", "face_count", "seed",
                     "orientation", "rig"},
        "formats": ("glb", "fbx"),
        # Rigging is a SEPARATE, separately-billed task chained off the
        # generation's task id — not a flag. `spec` picks tripo-native or
        # mixamo bone naming, and neither is this pipeline's
        # SkeletonProfileHumanoid convention, so a generated rig is a retarget
        # source at best.
        "rigged": "separate-task",
        "rig_types": ("biped", "quadruped", "hexapod", "octopod", "avian",
                      "serpentine", "aquatic"),
        "latency_s": None,               # undocumented; the poll reports
                                         # running_left_time at runtime
        "licence": {
            "code": CONDITIONAL,
            "summary": "PAID PLAN ONLY for commercial use. Paid users get full "
                       "rights to outputs with no attribution, and Tripo "
                       "commits to not training on inputs or outputs. FREE "
                       "users get no commercial rights at all — Tripo retains "
                       "the right to use, copy, modify and distribute "
                       "free-tier outputs.",
            "url": "https://www.tripo3d.ai/terms",
        },
        "note": "~$0.30 a textured generation, pay-as-you-go with no monthly "
                "floor. Rigs seven skeleton archetypes. Download URLs expire "
                "in FIVE MINUTES.",
    },
    "meshy": {
        "kind": "hosted",
        "label": "Meshy (openapi v1)",
        "env": "MESHY_API_KEY",
        "base": "https://api.meshy.ai/openapi/v1",
        "submit_path": "/image-to-3d",
        "poll_path": "/image-to-3d/{task}",
        "auth": "bearer",
        # Takes a data URI directly in image_url — no upload hop, which for a
        # pipeline whose every input is a local PNG is materially simpler.
        "image_mode": "field",
        "task_key": "result",
        "status_key": "status",
        "done": ("SUCCEEDED",),
        "dead": ("FAILED", "CANCELED"),
        "running": ("PENDING", "IN_PROGRESS"),
        "result_keys": ("model_urls.glb",),
        "url_ttl_s": 259200,             # three days; `expires_at` is on the
                                         # response
        "static": {},
        "default_model": "meshy-6",
        "fields": {"image": "image_url", "texture": "should_texture",
                   "pbr": "enable_pbr", "face_count": "target_polycount",
                   "model": "ai_model", "pose": "pose_mode", "seed": "seed"},
        # `quad` is not a boolean here, and `should_remesh` defaults to FALSE on
        # meshy-6 — so topology and polycount are SILENTLY IGNORED unless it is
        # switched on. Handled in _quirks_meshy, because a silently dropped
        # request is exactly the failure this module exists not to have.
        "quirks": "meshy",
        "credits": {"base": 30, "untextured": 30, "rig": 5, "animation": 3},
        # NOT PUBLISHED. The Pro bundle is $20 for 1000 credits, which divides
        # to $0.02, but that is arithmetic on a no-rollover bundle rather than a
        # rate Meshy states. price_for returns None here on purpose.
        "usd_per_credit": None,
        "usd_per_credit_note": (
            "Meshy publishes no per-credit dollar rate. The Pro plan divides "
            "to about $0.02/credit ($20 for 1000, no rollover), making a "
            "textured generation roughly $0.60 — twice Tripo — but that is "
            "derived, not quoted, so it is not treated as a price."),
        "supports": {"texture", "pbr", "face_count", "quad", "pose", "seed",
                     "rig"},
        "formats": ("glb", "fbx", "obj", "usdz", "stl", "3mf"),
        "rigged": "separate-task",
        # HUMANOID ONLY, and three constraints the docs state outright: over
        # 300,000 faces unsupported, the character's face must point at +Z, and
        # an untextured or anatomically-unclear mesh is refused. The +Z one is
        # worth reading twice against this pipeline's own convention —
        # BG_FORWARD is +Y in Blender.
        "rig_types": ("biped",),
        "rig_max_faces": 300_000,
        "rig_facing": "+Z",
        "latency_s": None,
        "licence": {
            "code": CONDITIONAL,
            "summary": "API access requires Pro or above, and every paid plan "
                       "gives full ownership of outputs with no attribution. "
                       "The free plan cannot reach the API at all, and its "
                       "web-app outputs are owned by Meshy and licensed back "
                       "under CC BY 4.0 — commercial use permitted WITH "
                       "attribution. So an API integration is on the clean "
                       "side of this by construction.",
            "url": "https://www.meshy.ai/terms-of-use",
        },
        "note": "Takes a data URI directly, keeps result URLs for three days, "
                "and can be asked for a T-pose at generation time "
                "(pose='t-pose') — the single most useful parameter here for a "
                "riggable character. $20/mo floor.",
    },
    "rodin": {
        "kind": "hosted",
        "label": "Rodin / Hyper3D (Gen-2.5)",
        "env": "RODIN_API_KEY",
        "base": "https://api.hyper3d.com/api/v2",
        "submit_path": "/rodin",
        "poll_path": "/status",
        "download_path": "/download",
        "auth": "bearer",
        "image_mode": "multipart",
        "upload_field": "images",
        "usd": None,
        "credits": {"base": 0.5, "extreme_high": 1.0, "highpack": 1.0},
        # Only the FREE plan's "$1.5 per credit" is published. API access needs
        # the Business plan, whose credit rate Hyper3D never states — the
        # ~$0.23/model figure people quote is arithmetic on a bundle. So: None.
        "usd_per_credit": None,
        "usd_per_credit_note": (
            "Hyper3D publishes $1.50/credit for the FREE plan only. API access "
            "requires the Business plan ($96-120/mo), whose credit rate is not "
            "published anywhere."),
        "supports": {"quad", "face_count", "pose", "seed"},
        "formats": ("glb", "obj", "fbx", "usdz", "stl"),
        "rigged": False,
        "latency_s": (20, 90),           # Sketch ~20s, Regular ~70s, Gen-2 ~90s
        "implemented": False,
        "licence": {
            "code": FREE,
            "summary": "The terms grant an explicit unrestricted licence to "
                       "generated Output, and carve Output out of the "
                       "'Content' whose resale is otherwise forbidden. "
                       "Commercial use of generated models is clear. Note that "
                       "ChatAvatar output on the same site is NOT — it is "
                       "non-commercial unless separately licensed.",
            "url": "https://hyper3d.ai/legal/terms",
        },
        "note": "Documented, not wired: a three-call dance (multipart submit, "
                "POST /status, POST /download), API gated behind a $96-120/mo "
                "Business plan, and no published credit rate. Up to 5 input "
                "images, quad topology to 200k, TAPose for rig-ready "
                "geometry — but no rigging.",
    },
    # DELEGATES TO krea.py RATHER THAN DESCRIBING THE HTTP, which every other
    # hosted entry does. krea.generate_3d already exists, already knows the
    # model table, the multipart quirks, the poll shape and the price floor —
    # re-describing all of that here would be a second implementation to keep
    # in step with the first.
    #
    # It is here because it SHIPPED UNREACHABLE. krea.generate_3d landed as a
    # Python function that no MCP tool called, so a user whose only key is
    # KREA_API_KEY — the key the setup docs tell them to configure, the one
    # already in .env — could not produce a mesh from a session at all. They
    # needed a Stability/Tripo/Meshy key or a local GPU server instead. Wiring
    # it as a backend rather than a bespoke tool means it inherits choose(),
    # the licence gate, the price quote and the common result shape, which a
    # standalone tool would each have had to reimplement.
    "krea": {
        "kind": "hosted",
        "label": "Krea (TRELLIS / TRELLIS.2 / Hunyuan3D / Tripo)",
        "env": "KREA_API_KEY",
        "delegate": "krea",
        "base": "https://api.krea.ai",
        "submit_path": "", "poll_path": "",
        "response": "delegate",
        # imageto3d's option names on the left, krea.generate_3d's on the right.
        "fields": {"texture": "generate_texture", "seed": "seed",
                   "resolution": "resolution", "texture_size": "texture_size",
                   "face_count": "decimation_target"},
        "supports": {"texture", "seed", "resolution", "texture_size",
                     "face_count"},
        "formats": ["glb"],
        "rigged": False,
        "latency_s": [90, 600],
        "licence": {
            "code": CONDITIONAL,
            "summary": "Krea runs the same open-weight models you could "
                       "self-host, so TWO sets of terms apply and they are not "
                       "the same question: Krea's own for the service, and the "
                       "model's for the mesh. TRELLIS.2 is MIT and its output "
                       "is clear; the others are not uniformly so. Name the "
                       "model deliberately and read its terms before shipping "
                       "an asset commercially.",
            "url": "https://krea.ai/terms",
        },
        "note": "Measured $0.30 on trellis-2 at DEFAULT settings — a floor, "
                "not a rate. Nothing is known about how resolution, "
                "texture_size or decimation_target move it, and a run at "
                "1536/4096 was not re-measured. Models without a measured "
                "price refuse rather than guess.",
    },
}

# Deliberately empty. Every backend surveyed is either CONDITIONAL, or needs a
# key, or needs a server somebody has to start. A default that quietly picks one
# is a licence decision made for a stranger — see choose().
DEFAULT_BACKEND = ""

LOCAL = tuple(k for k, v in BACKENDS.items() if v["kind"] == "local")
HOSTED = tuple(k for k, v in BACKENDS.items() if v["kind"] == "hosted")


def _spec(backend: str) -> dict:
    spec = BACKENDS.get(backend)
    if spec is None:
        raise ImageTo3DError(f"unknown backend {backend!r} — known: "
                             f"{', '.join(sorted(BACKENDS))}")
    return spec


# ---------------------------------------------------------------------------
# Keys, endpoints, availability
# ---------------------------------------------------------------------------
def api_key(backend: str, root: Any = None) -> str:
    """The token for this backend, from the project's .env or the environment.

    Never logged, never returned in a result, never put on a command line. A
    local backend needs no key and returns "" — a legitimate answer, which is
    why available() asks the table rather than this.
    """
    name = (BACKENDS.get(backend) or {}).get("env") or ""
    if not name:
        return ""
    # The machine-wide layer too, not the project alone: a key set with
    # `bgate key set <p> --global` lives in ~/.bgate/.env, and reading only
    # the project file made a correctly configured machine look unkeyed to
    # everything that probes without a project in hand.
    try:
        envfile.load_env(root)
    except Exception:                                            # noqa: BLE001
        pass
    return (os.environ.get(name) or "").strip()


def base_url(backend: str) -> str:
    """Where this backend lives. Overridable only where it should be.

    Local backends declare ``base_env`` because the host and port are the
    operator's. A hosted API's base URL is not a knob, and making it one is how
    a key ends up POSTed somewhere nobody intended.
    """
    spec = BACKENDS.get(backend) or {}
    override = spec.get("base_env")
    if override:
        return (os.environ.get(override) or spec.get("base") or "").rstrip("/")
    return str(spec.get("base") or "").rstrip("/")


def available(backend: str, root: Any = None, *, probe: bool = False) -> dict:
    """Can this backend be used? Presence only, unless ``probe``.

    A health check that spends money — or that loads a 5 GB model — is one
    nobody runs, so nothing here generates. ``probe=False`` (the default) never
    touches the network at all: for a local backend it reports the GPU and where
    the server is expected, and for a hosted one whether the key is present.
    ``probe=True`` additionally does one short GET against a local backend's
    health endpoint, which is the only way to distinguish "configured" from
    "actually running".
    """
    spec = BACKENDS.get(backend)
    if spec is None:
        return {"available": False, "backend": backend,
                "reason": f"unknown backend {backend!r} — known: "
                          f"{', '.join(sorted(BACKENDS))}"}
    common = {
        "backend": backend, "kind": spec["kind"], "label": spec["label"],
        "licence": effective_licence(spec),
        "rigged": spec.get("rigged") or False,
        "formats": list(spec.get("formats") or ()),
        "implemented": spec.get("implemented", True),
        # WHICH KNOBS THIS BACKEND TAKES, because a caller had no way to find
        # out. The option names differ per backend and the difference decides
        # what a user can control: hunyuan-local accepts face_count, steps,
        # octree_resolution and guidance, while trellis-cpp accepts only seed
        # and resolution — so on trellis-cpp there is NO way to ask the
        # generator for less geometry and post-generation decimation in
        # blender_rig is the only density lever that exists. An agent that
        # cannot see this passes an option that is silently dropped, or never
        # learns the one that would have helped. Sorted so the answer is
        # stable to diff.
        "supports": sorted(spec.get("supports") or ()),
        "note": spec.get("note", ""),
    }
    if not common["implemented"]:
        return {**common, "available": False,
                "reason": "documented as an alternative, but its transport is "
                          "not wired in this adapter — see the note"}

    if spec["kind"] == "local":
        # A BACKEND THAT NEEDS A GRAPH IS NOT USABLE WITHOUT ONE. comfy takes a
        # whole ComfyUI workflow as its unit of work and can only substitute two
        # placeholders into it — this adapter cannot invent the graph. Without
        # BGATE_COMFY_WORKFLOW it used to report available=True, so choose()
        # could hand a caller a backend that fails at generation time, after the
        # server is up and the plate is made. Say it here, where it is cheap.
        wf_env = spec.get("workflow_env")
        if wf_env:
            wf = os.environ.get(wf_env, "").strip()
            if not wf:
                return {**common, "available": False,
                        "reason": f"{wf_env} is not set — this backend runs "
                                  "YOUR ComfyUI graph and cannot invent one. "
                                  "Build an image-to-3D workflow in ComfyUI, "
                                  "Save (API format), and point "
                                  f"{wf_env} at that .json"}
            if not Path(wf).is_file():
                return {**common, "available": False,
                        "reason": f"{wf_env} points at {wf}, which is not a "
                                  "file — export the workflow again with Save "
                                  "(API format), not the plain Save"}

        card = gpu()
        fit = fits_vram(spec.get("vram_gb"))
        out = {**common, "base": base_url(backend),
               "gpu": card,
               "vram_gb_needed": spec.get("vram_gb"),
               "weights": spec.get("weights", ""),
               "windows": spec.get("windows", "")}
        if not card["available"]:
            return {**out, "available": False, "reason": card["reason"]}
        if not fit["ok"]:
            # NOT fatal by itself — a shape-only run may still fit, and the
            # server may be on another machine entirely. Reported as the reason
            # while staying available, so a caller can decide.
            out["vram_warning"] = fit["reason"]
        if not probe:
            return {**out, "available": True, "reason": "",
                    "checked": "configuration only — pass probe=True to ask "
                               "whether the server is actually up"}
        alive = _alive(backend)
        return {**out, "available": bool(alive["ok"]),
                "reason": "" if alive["ok"] else alive["reason"],
                "checked": "health endpoint"}

    name = spec.get("env") or ""
    if not api_key(backend, root):
        return {**common, "available": False,
                "reason": f"{name} not set — put it in the project's .env "
                          "(gitignored, loaded per project) or the environment"}
    return {**common, "available": True, "reason": ""}


def _alive(backend: str, *, timeout: float = 1.5) -> dict:
    """One short GET against a local backend's health endpoint.

    Short on purpose: a caller polling status() must not block for a TCP
    timeout on a server nobody started, which is the common case.
    """
    spec = BACKENDS.get(backend) or {}
    path = spec.get("health_path")
    if not path:
        return {"ok": False, "reason": "this backend declares no health endpoint"}
    url = base_url(backend) + path
    try:
        _http.request("GET", url, headers={"Accept": "*/*"}, timeout=timeout,
                      retries=1, provider=backend)
        return {"ok": True, "reason": ""}
    except _http.ProviderError as exc:
        if exc.status == 0:
            return {"ok": False,
                    "reason": f"nothing answered at {url} ({exc.body}) — start "
                              f"the server, or point "
                              f"{spec.get('base_env') or 'the base URL'} at it"}
        # ANY HTTP STATUS PROVES A SERVER IS THERE, INCLUDING 404. urlopen
        # raises on 4xx/5xx, so a blanket except reported "nothing answered"
        # for a backend that was running perfectly — reported from the field
        # against a trellis.cpp release whose build serves the submit and poll
        # paths but not /health. available(probe=True) called it dead and
        # choose() could never select it, while naming the backend by hand
        # worked, which is the signature of a probe wrong about liveness rather
        # than a server that is down.
        #
        # Liveness is "something is listening and speaking HTTP". Whether that
        # something implements this particular path is a different question, and
        # not one a reachability check should refuse a working server over.
        return {"ok": True, "reason": "",
                "note": f"{url} answered {exc.status} rather than a health body — "
                        f"the server is up; this build does not implement that "
                        f"path"}


def status(root: Any = None, *, probe: bool = False) -> dict:
    """Every backend, usable or not, and why. The nothing-configured answer.

    Shaped for the job blender_status and image_status do: a caller with nothing
    set up gets a complete, actionable picture rather than an exception. ``ok``
    is False when nothing is usable, which is a fact and not an error — every
    other path in this product still works.
    """
    rows = [available(name, root, probe=probe) for name in sorted(BACKENDS)]
    usable = [r["backend"] for r in rows if r["available"]]
    return {
        "ok": bool(usable),
        "gpu": gpu(),
        "runner_python": runner_python(),
        "backends": rows,
        "usable": usable,
        "local": [r["backend"] for r in rows
                  if r["available"] and r["kind"] == "local"],
        "hosted": [r["backend"] for r in rows
                   if r["available"] and r["kind"] == "hosted"],
        # The distinction that decides whether an asset can ship: reachable is
        # not the same as clear to use.
        "unconditional_licence": [r["backend"] for r in rows
                                  if r["available"]
                                  and r["licence"]["code"] in AUTO_LICENCES],
        "default": DEFAULT_BACKEND,
        "reason": "" if usable else (
            "no image-to-3D backend is usable. Locally, that means no NVIDIA "
            "GPU or no server running (see BGATE_COMFY_URL / "
            "BGATE_HUNYUAN3D_URL); for the hosted fallback, set one of "
            + ", ".join(sorted(s["env"] for s in BACKENDS.values()
                               if s.get("env")))
            + " in the project's .env. Nothing else in Builders Gate is "
              "affected by this."),
    }


def choose(root: Any = None, *, prefer: str = "local",
           probe: bool = True) -> dict:
    """Pick a backend, or explain why nothing can be picked.

    LOCAL FIRST, and only ever an UNCONDITIONALLY-LICENSED backend
    automatically. A conditional licence — region-restricted, revenue-capped,
    paid-plan-only — has to be named by a human who has read the condition,
    because this tool does not know its user's revenue, territory or monthly
    actives and must not decide that for them.

    Returns {backend, reason, candidates}. ``backend`` is "" when the only
    reachable options are conditional, and ``candidates`` then names them so the
    caller can offer the choice rather than silently making it.
    """
    rows = [available(name, root, probe=probe) for name in sorted(BACKENDS)]
    live = [r for r in rows if r["available"]]
    order = (LOCAL, HOSTED) if prefer == "local" else (HOSTED, LOCAL)
    ranked = [r for group in order for name in group
              for r in live if r["backend"] == name]
    clear = [r for r in ranked if r["licence"]["code"] in AUTO_LICENCES]
    if clear:
        return {"backend": clear[0]["backend"], "reason": "",
                "candidates": [r["backend"] for r in ranked]}
    if ranked:
        return {
            "backend": "", "candidates": [r["backend"] for r in ranked],
            "reason": "every reachable backend has a conditional licence, so "
                      "none can be chosen automatically. Name one deliberately "
                      "after reading its terms: "
                      + "; ".join(f"{r['backend']} — {r['licence']['summary']}"
                                  for r in ranked)}
    return {"backend": "", "candidates": [],
            "reason": status(root, probe=probe)["reason"]}


def supports(backend: str, feature: str) -> bool:
    """Does this backend do `feature` at all?

    Worth asking BEFORE quoting: "generate it rigged" on a backend with no
    rigging is not a dearer version of the request, it is not the request.
    """
    return feature in ((BACKENDS.get(backend) or {}).get("supports") or set())


def capabilities(backend: str) -> dict:
    """Everything needed to decide whether this backend fits the job."""
    spec = _spec(backend)
    return {
        "backend": backend, "kind": spec["kind"], "label": spec["label"],
        "supports": sorted(spec.get("supports") or ()),
        "formats": list(spec.get("formats") or ()),
        "rigged": spec.get("rigged") or False,
        "async": bool(spec.get("poll_path")),
        "latency_s": list(spec.get("latency_s") or ()),
        "vram_gb": spec.get("vram_gb"),
        "windows": spec.get("windows", ""),
        "weights": spec.get("weights", ""),
        "licence": effective_licence(spec),
        "implemented": spec.get("implemented", True),
        "note": spec.get("note", ""),
    }


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def credits_for(backend: str, *, texture: bool = True, quad: bool = False,
                detailed_texture: bool = False, rig: bool = False,
                animations: int = 0) -> Optional[float]:
    """The request's cost in the BACKEND'S OWN unit, or None if it has none.

    Priced in credits first and converted once, because that is the unit both
    hosted backends actually publish and the only form in which the arithmetic
    can be checked against their tables. A local backend has no credits and
    returns None — which is not "unknown", it is "not billed"; price_for says
    0.0 for those.
    """
    spec = _spec(backend)
    table = spec.get("credits")
    if not table:
        return None
    total = float(table.get("base", 0.0))
    if not texture and "untextured" in table:
        total = float(table["untextured"])
    if quad:
        total += float(table.get("quad", 0.0))
    if detailed_texture:
        total += float(table.get("detailed_texture", 0.0))
    if rig:
        total += float(table.get("rig", 0.0))
    if animations:
        total += float(table.get("retarget", table.get("animation", 0.0))) * int(animations)
    return round(total, 4)


def price_for(backend: str, *, texture: bool = True, quad: bool = False,
              detailed_texture: bool = False, rig: bool = False,
              animations: int = 0, count: int = 1) -> Optional[float]:
    """What THIS REQUEST costs in dollars, or None when nobody published a rate.

    Three distinct answers, and the difference between the last two matters:

      0.0   a local backend. Your own electricity, and the honest number.
      float a hosted backend with a PUBLISHED credit rate.
      None  a hosted backend whose rate is not published. NOT 0.0 and not a
            guess — a reader treats a number as a quote, so an invented one is
            worse than a missing one (krea.TRAIN_USD settled this). A caller
            that gets None must ask the human.
    """
    spec = _spec(backend)
    if spec["kind"] == "local":
        return 0.0
    if spec.get("delegate") == "krea":
        # The rate lives in krea.MODELS_3D, not here — one price table, so a
        # measurement added there reaches this quote without being copied. It
        # returns None for a model nobody has been invoiced for, which is the
        # answer this function wants anyway.
        from bgate_adapters import krea
        return krea.price_for_3d()
    credits = credits_for(backend, texture=texture, quad=quad,
                          detailed_texture=detailed_texture, rig=rig,
                          animations=animations)
    rate = spec.get("usd_per_credit")
    if credits is None or rate is None:
        return None
    return round(float(credits) * float(rate) * max(1, int(count)), 4)


def price_note(backend: str) -> str:
    """Why a price is missing, when it is. Empty when there is a real price."""
    spec = _spec(backend)
    if spec["kind"] == "local":
        return ""
    return spec.get("usd_per_credit_note", "") if spec.get("usd_per_credit") is None else ""


# ---------------------------------------------------------------------------
# The input plate
# ---------------------------------------------------------------------------
def check_input(path: str | os.PathLike[str]) -> dict:
    """Judge the source image BEFORE generating anything.

    Cheap, local, and worth doing because the plate is the single biggest
    determinant of output quality and every failure is visible from here.
    Returns {ok, reason, warnings, ...} rather than raising, so a caller can
    show a human what is wrong with their reference and let them decide.

    Pillow is optional. Without it the size checks cannot run, and that is
    REPORTED rather than assumed to pass — the contract krea.check_training_set
    already uses.
    """
    p = Path(path)
    warnings: list[str] = []
    if not p.is_file():
        return {"ok": False, "path": str(p), "reason": f"no such image: {p}",
                "warnings": warnings}
    if p.suffix.lower() not in INPUT_SUFFIXES:
        return {"ok": False, "path": str(p), "warnings": warnings,
                "reason": f"unsupported input type {p.suffix!r} — "
                          + "/".join(sorted(s.lstrip(".") for s in INPUT_SUFFIXES))
                          + " only"}
    try:
        from PIL import Image                    # noqa: PLC0415 — optional
    except ImportError:
        warnings.append("Pillow is not installed, so the image was NOT measured "
                        f"— a plate under {INPUT_MIN_SIDE}px carries nothing to "
                        "reconstruct from")
        return {"ok": True, "path": str(p), "reason": "", "warnings": warnings}
    try:
        with Image.open(p) as img:
            w, h = img.size
            bands = img.getbands()
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "path": str(p), "warnings": warnings,
                "reason": f"unreadable image ({type(exc).__name__}: {exc})"}
    short = min(w, h)
    if short < INPUT_MIN_SIDE:
        return {"ok": False, "path": str(p), "size": [w, h], "warnings": warnings,
                "reason": f"{w}x{h} — under {INPUT_MIN_SIDE}px there is nothing "
                          "for the model to read, and it invents the whole "
                          "subject rather than reconstructing it"}
    if short < INPUT_GOOD_SIDE:
        warnings.append(f"{w}x{h} is under {INPUT_GOOD_SIDE}px on the short "
                        "side — detail in the result is invented, not read")
    if "A" not in bands:
        # Not fatal anywhere, but it is the failure that produces geometry made
        # of background. bgate_core.art.chroma already produces the keyed plate for
        # the sprite path, and it is the right input here for the same reason.
        #
        # MEASURED, same prompt and model, alpha the only difference:
        #   opaque plate   605s, 21% non-manifold after adopt — quality REFUSED
        #   keyed plate    216s, 16% — passes
        # 2.8x the wall clock spent reconstructing a backdrop, and a mesh the
        # gate then throws out. Worth saying louder than "or expect loose
        # parts", because the cost lands ten minutes after the mistake.
        #
        # NOTE task_kind: chroma.needs_key("character") is False, so a character
        # plate generated for this path arrives opaque unless keyed=True is
        # passed. That is the common case, not an edge one.
        warnings.append("the plate has no alpha channel — background pixels "
                        "become geometry on several backends. Key it out first "
                        "(bgate_core.art.chroma, keyed=True) or expect loose parts: "
                        "measured 2.8x slower and 21% non-manifold against 16% "
                        "for the same subject keyed")
    return {"ok": True, "path": str(p), "size": [w, h], "reason": "",
            "warnings": warnings}


def data_uri(path: str | os.PathLike[str]) -> str:
    """A local plate as a base64 data URI."""
    p = Path(path)
    if not p.is_file():
        raise ImageTo3DError(f"input image not found: {p}")
    mime = mimetypes.guess_type(p.name)[0]
    if not mime or not mime.startswith("image/"):
        raise ImageTo3DError(f"unsupported input type {p.suffix!r} — png/jpg/webp only")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def image_b64(path: str | os.PathLike[str]) -> str:
    """The plate as BARE base64, no data-URI prefix.

    Both spellings exist in the wild and they are not interchangeable: sending a
    data URI where bare base64 is expected is a decode error on the server,
    which surfaces as a useless 500. Hunyuan3D's api_server wants this one;
    Meshy wants the other.
    """
    p = Path(path)
    if not p.is_file():
        raise ImageTo3DError(f"input image not found: {p}")
    return base64.b64encode(p.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _headers(backend: str, key: str) -> dict:
    spec = BACKENDS.get(backend) or {}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if spec.get("auth") == "bearer" and key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _error(backend: str, exc: _http.ProviderError, method: str,
           path: str) -> ImageTo3DError:
    """One message per failure mode, naming the FIX rather than the status code.

    An agent that reads "HTTP 402" retries. An agent that reads where the
    balance lives does something useful. A transport failure (status 0) on a
    LOCAL backend gets a completely different message: there is no key to
    check, there is a process to start.
    """
    spec = BACKENDS.get(backend) or {}
    label = spec.get("label", backend)
    code, detail = exc.status, exc.body
    meta = dict(provider=backend, status=code, body=detail, billing=exc.billing)
    if code == 0:
        if spec.get("kind") == "local":
            return ImageTo3DError(
                f"could not reach {label} at {base_url(backend)} ({detail}) — "
                f"is the server running? Point "
                f"{spec.get('base_env') or 'the base URL'} at it if it is on "
                "another host or port.", **meta)
        return ImageTo3DError(f"could not reach {label} ({detail})", **meta)
    if code in (401, 403):
        return ImageTo3DError(
            f"{label} rejected the API key (HTTP {code}) — check "
            f"{spec.get('env') or 'the credentials'} in the project's .env",
            **meta)
    if code == 402:
        return ImageTo3DError(
            f"{label} has no credit for this request (HTTP 402) — top up the "
            "account's API balance and retry; nothing was charged.", **meta)
    if code == 429:
        return ImageTo3DError(
            f"{label} rate-limited this request (HTTP 429) — slow the fan-out "
            "or retry in a moment.", **meta)
    if code in (400, 422):
        return ImageTo3DError(
            f"{label} refused the request shape: {detail} (each backend has "
            "its own schema — see BACKENDS[...]['supports'])", **meta)
    if code == 404 and spec.get("kind") == "local":
        return ImageTo3DError(
            f"{label} answered 404 on {method} {path} — something IS running at "
            f"{base_url(backend)}, but it is not the server this backend "
            "expects. Check the port.", **meta)
    return ImageTo3DError(f"{label} HTTP {code} on {method} {path}: {detail}",
                          **meta)


def _request(backend: str, path: str, key: str = "", *,
             payload: Optional[dict] = None, method: str = "GET",
             timeout: float = 60.0) -> dict:
    url = path if path.startswith("http") else base_url(backend) + path
    try:
        got = _http.request(method, url, headers=_headers(backend, key),
                            json=payload, timeout=timeout, provider=backend)
    except _http.ProviderError as exc:
        raise _error(backend, exc, method, path) from exc
    try:
        return got.json(provider=backend)
    except _http.ProviderError as exc:
        raise ImageTo3DError(str(exc), provider=backend) from exc


# The image types this pipeline actually uploads, resolved without asking the
# machine. mimetypes.guess_type() consults the Windows registry, where a stray
# file association can decide that a .png is `image/x-png` — for an upload whose
# acceptance is the backend's call, that is a per-machine failure with no local
# symptom. The registry is only consulted for suffixes not listed here.
_UPLOAD_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".glb": "model/gltf-binary",
                ".gltf": "model/gltf+json"}


def multipart(fields: dict, filename: str, blob: bytes,
              field: str = "file") -> tuple[bytes, str]:
    """A multipart/form-data body, stdlib only.

    Some endpoints here and in krea are not JSON, and pulling in `requests` for
    them would put a dependency on the critical path of modules whose whole HTTP
    surface is otherwise a handful of urllib calls.

    Public and shared: krea carried a byte-identical copy that differed only in
    how it guessed the content type, so the two adapters disagreed about the MIME
    of the same file.
    """
    boundary = "----bgate" + base64.urlsafe_b64encode(os.urandom(9)).decode()
    out = bytearray()
    for name, value in (fields or {}).items():
        if value is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    suffix = Path(filename).suffix.lower()
    mime = (_UPLOAD_MIME.get(suffix)
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream")
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n').encode()
    out += f"Content-Type: {mime}\r\n\r\n".encode()
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _dig(blob: Any, dotted: str) -> Any:
    """Read a dotted path out of a response. Every backend nests differently and
    every one of them nests; this is one line instead of a branch each."""
    node = blob
    for part in str(dotted).split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
# THE BACKENDS AGREE ON NOTHING. One wants `texture`, another `should_texture`;
# one takes a bare base64 string, one a data URI, one a token from a separate
# upload, one the raw bytes as multipart; the polycount is `face_count`,
# `face_limit`, `target_polycount` or `vertex_count` depending on who you ask.
# krea.py learned this the expensive way — aspect_ratio sent to flux was a 422
# on the first live call — and landed on the right fix: no shared payload shape,
# each backend built for what it actually accepts.
#
# Table-driven rather than a builder per backend, because the differences are
# all naming and nesting. `fields` renames, `static` is always sent, `quirks`
# handles the two places where a rename is not enough.
def build_payload(backend: str, *, image: str = "", image_token: str = "",
                  texture: bool = True, pbr: bool = False, quad: bool = False,
                  face_count: Optional[int] = None, seed: Optional[int] = None,
                  steps: Optional[int] = None, guidance: Optional[float] = None,
                  octree_resolution: Optional[int] = None,
                  pose: str = "", model: str = "", orientation: str = "",
                  texture_resolution: Optional[int] = None,
                  foreground_ratio: Optional[float] = None,
                  out_format: str = DEFAULT_FORMAT) -> dict:
    """The request body this backend actually accepts.

    Separated from :func:`submit` so it can be tested and inspected without a
    network — the payload is the part that broke first for Krea, and the part a
    caller most wants to see before committing to a run.

    Every option is checked against ``supports`` and refused HERE, naming the
    backends that do offer it. An unsupported option silently dropped is how
    "generate it in a T-pose" produces an A-pose and a green result.
    """
    spec = _spec(backend)
    if spec.get("image_mode") == "multipart":
        raise ImageTo3DError(
            f"{spec['label']} sends the image as multipart form data, not JSON "
            "— build_payload does not apply; submit() handles it")

    asked = {"texture": texture, "pbr": pbr, "quad": quad,
             "face_count": face_count, "seed": seed, "steps": steps,
             "guidance": guidance, "octree_resolution": octree_resolution,
             "pose": pose, "orientation": orientation,
             "texture_resolution": texture_resolution,
             "foreground_ratio": foreground_ratio}
    for name, value in asked.items():
        # Falsy means "not asked for"; only a positive ask can be unsupported.
        # texture defaults True, so a backend that always textures need not
        # list it.
        if not value or name == "texture" or supports(backend, name):
            continue
        others = sorted(k for k, v in BACKENDS.items()
                        if name in (v.get("supports") or set()))
        raise ImageTo3DError(
            f"{spec['label']} does not offer {name!r}"
            + (f" — the backends that do are {', '.join(others)}" if others
               else " — no backend here does"))

    formats = spec.get("formats") or (DEFAULT_FORMAT,)
    if out_format not in formats:
        raise ImageTo3DError(
            f"{spec['label']} does not return {out_format!r} — it returns "
            + ", ".join(formats))

    payload: dict[str, Any] = dict(spec.get("static") or {})
    fields = spec.get("fields") or {}
    mode = spec.get("image_mode", "field")

    if mode == "field":
        if not image:
            raise ImageTo3DError("no input image — pass the plate's base64, "
                                 "data URI or URL")
        payload[fields.get("image", "image")] = image
    elif mode == "file_token":
        # An upload endpoint hands back a token and the task references it
        # under a nested object. Sending bytes inline here is a 400.
        if not image_token:
            raise ImageTo3DError(
                f"{spec['label']} takes an uploaded file token, not inline "
                "bytes — call upload() first and pass image_token")
        payload[fields.get("file", "file")] = {
            "type": spec.get("file_type", "png"), "file_token": image_token}
    elif mode == "workflow":
        raise ImageTo3DError(
            f"{spec['label']} takes a whole workflow graph as its body — "
            "build_comfy_prompt() builds it, not this")
    else:                                                        # pragma: no cover
        raise ImageTo3DError(f"unknown image mode {mode!r} for {backend}")

    def put(name: str, value: Any) -> None:
        key = fields.get(name)
        if key and value is not None:
            payload[key] = value

    put("texture", bool(texture))
    if pbr:
        put("pbr", True)
    if quad:
        put("quad", True)
    for name, value in (("face_count", face_count), ("seed", seed),
                        ("steps", steps), ("octree_resolution", octree_resolution),
                        ("texture_resolution", texture_resolution)):
        if value is not None:
            put(name, int(value))
    for name, value in (("guidance", guidance),
                        ("foreground_ratio", foreground_ratio)):
        if value is not None:
            put(name, float(value))
    for name, value in (("pose", pose), ("orientation", orientation)):
        if value:
            put(name, value)
    put("model", model or spec.get("default_model"))

    quirk = spec.get("quirks")
    if quirk == "meshy":
        _quirks_meshy(payload, quad=quad, face_count=face_count)
    return payload


def _quirks_meshy(payload: dict, *, quad: bool, face_count: Optional[int]) -> None:
    """Two Meshy traps that a rename cannot fix.

    `topology` is an ENUM ("quad" | "triangle"), not the boolean every other
    backend uses — so the generic `quad: True` would have written a bool into a
    string field.

    And `should_remesh` defaults to FALSE on meshy-6, which means `topology` and
    `target_polycount` are both SILENTLY IGNORED unless it is switched on. A
    request that quietly does none of what was asked is exactly the failure this
    module exists not to have, so asking for either turns it on.
    """
    if quad:
        payload["topology"] = "quad"
        payload.pop("quad", None)
    if quad or face_count is not None:
        payload["should_remesh"] = True


def upload(backend: str, path: str | os.PathLike[str], *, root: Any = None,
           timeout: float = 120.0) -> dict:
    """Put the plate on the backend's store. Returns {token, name, subfolder}.

    Uploading is not generating: nothing is charged and nothing is computed
    until submit() is called with what this hands back.
    """
    spec = _spec(backend)
    endpoint = spec.get("upload_path")
    if not endpoint:
        raise ImageTo3DError(
            f"{spec['label']} takes the image inline — there is nothing to "
            "upload")
    p = Path(path)
    if not p.is_file():
        raise ImageTo3DError(f"input image not found: {p}")
    key = api_key(backend, root)
    body, content_type = multipart({}, p.name, p.read_bytes(),
                                    field=spec.get("upload_field", "file"))
    headers = {"Accept": "application/json", "Content-Type": content_type}
    if spec.get("auth") == "bearer" and key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        got = _http.request("POST", base_url(backend) + endpoint, data=body,
                            headers=headers, timeout=timeout,
                            provider=backend).json(provider=backend)
    except _http.ProviderError as exc:
        raise _error(backend, exc, "POST", endpoint) from exc
    token = _dig(got, spec.get("upload_token_key", "")) if spec.get(
        "upload_token_key") else ""
    # ComfyUI answers {name, subfolder, type} and the workflow references the
    # NAME; Tripo answers a token. Both come back here so the caller does not
    # have to know which.
    return {"token": str(token or ""), "name": str(got.get("name") or ""),
            "subfolder": str(got.get("subfolder") or ""),
            "raw": got}


# ---------------------------------------------------------------------------
# ComfyUI: the workflow is config, not code
# ---------------------------------------------------------------------------
# ComfyUI's request body is a whole graph, and that graph names node CLASSES
# whose spelling changes with every ComfyUI-3D-Pack release. Hardcoding one here
# would make this adapter break on somebody else's upgrade, and would encode a
# choice of model — and therefore of LICENCE — that is not this module's to
# make.
#
# So the workflow is the user's: an API-format JSON exported from their own
# ComfyUI ("Save (API format)"), with two placeholders substituted here. That is
# not a shortcut. A workflow graph genuinely IS a per-installation artefact, and
# treating it as configuration is the correct model rather than a concession.
COMFY_IMAGE_TOKEN = "__BGATE_IMAGE__"
COMFY_SEED_TOKEN = "__BGATE_SEED__"


def comfy_workflow_path(backend: str = "comfy") -> str:
    env = (BACKENDS.get(backend) or {}).get("workflow_env") or ""
    return (os.environ.get(env) or "").strip() if env else ""


def build_comfy_prompt(image_name: str, *, seed: Optional[int] = None,
                       workflow_path: str = "", backend: str = "comfy") -> dict:
    """The /prompt body: the user's graph with the plate and seed substituted.

    Raises with the whole setup instruction rather than a KeyError, because the
    thing that is missing is a file the user has to export from an application,
    and "no such file" would send them looking in the wrong place.
    """
    path = workflow_path or comfy_workflow_path(backend)
    if not path:
        raise ImageTo3DError(
            "no ComfyUI workflow configured — export one from ComfyUI with "
            "'Save (API format)', put "
            f"{COMFY_IMAGE_TOKEN!r} where the LoadImage node names its file "
            f"and optionally {COMFY_SEED_TOKEN!r} where the seed goes, then "
            f"point {(BACKENDS.get(backend) or {}).get('workflow_env', '')} at it")
    p = Path(path)
    if not p.is_file():
        raise ImageTo3DError(f"the configured ComfyUI workflow does not exist: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageTo3DError(f"could not read the ComfyUI workflow at {p}: {exc}") from exc
    if COMFY_IMAGE_TOKEN not in raw:
        raise ImageTo3DError(
            f"the workflow at {p} has no {COMFY_IMAGE_TOKEN} placeholder, so "
            "there is nowhere to put the input image — it would generate from "
            "whatever was baked into the graph, every time")
    raw = raw.replace(COMFY_IMAGE_TOKEN, json.dumps(str(image_name))[1:-1])
    raw = raw.replace(COMFY_SEED_TOKEN, str(int(seed)) if seed is not None
                      else str(int(time.time()) % 2_147_483_647))
    try:
        graph = json.loads(raw)
    except ValueError as exc:
        raise ImageTo3DError(
            f"the ComfyUI workflow at {p} is not valid JSON after "
            f"substitution: {exc}. It must be the API format, not the editor "
            "format.") from exc
    # The editor format has a top-level "nodes" list; the API format is a flat
    # map of id -> {class_type, inputs}. Telling them apart here saves a
    # baffling 400 from ComfyUI.
    if isinstance(graph, dict) and "nodes" in graph and "class_type" not in str(
            next(iter(graph.values()), "")):
        raise ImageTo3DError(
            f"the workflow at {p} looks like the EDITOR format (it has a "
            "top-level 'nodes' list). ComfyUI's /prompt endpoint needs the API "
            "format — re-export with 'Save (API format)'.")
    return {"prompt": graph, "client_id": "builders-gate"}


def comfy_scan(history: dict, task: str, suffixes) -> list[dict]:
    """Every file a ComfyUI run wrote whose suffix is in ``suffixes``.

    SCANNED, not looked up by node id or class name. 3D-Pack's saver nodes are
    renamed between releases and differ per model, so keying on one would break
    on an upgrade of somebody else's plugin. Scanning for a usable suffix is
    stable against all of that.

    Public and suffix-parameterised because localgen's image scan was the same
    four nested type-guards over the same history shape with a different suffix
    set — the one thing about a ComfyUI response that actually varies by caller.
    """
    run = (history or {}).get(task) or {}
    found: list[dict] = []
    for node_id, out in (run.get("outputs") or {}).items():
        if not isinstance(out, dict):
            continue
        for entries in out.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("filename") or "")
                if name.lower().rsplit(".", 1)[-1] in suffixes:
                    found.append({"node": str(node_id), "filename": name,
                                  "subfolder": str(entry.get("subfolder") or ""),
                                  "type": str(entry.get("type") or "output")})
    return found


def _comfy_outputs(history: dict, task: str) -> list[dict]:
    """Every file this run wrote that the 3D pipeline could use."""
    return comfy_scan(history, task, USABLE_FORMATS)


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------
# THREE SHAPES, and pretending they are one would be the mistake:
#
#   json-job   POST a JSON body, get a task id, poll, download a URL or decode
#              an inline base64 blob. hunyuan-local, tripo, meshy.
#   sync       POST multipart, the RESPONSE BODY IS THE MODEL. stability. No
#              task, no poll, nothing to expire.
#   comfy      upload the plate, POST a graph, poll /history, fetch /view.
#
# generate() is the one call worth using; the pieces are exported because a
# caller that wants to quote, submit and walk away needs them — with the
# five-minute-URL caveat on tripo firmly in mind.
def submit(backend: str, image_path: str | os.PathLike[str], *,
           root: Any = None, timeout: float = 120.0, **options) -> dict:
    """Start a generation. Returns {task, backend, payload}.

    Raises before anything is sent if the plate is unusable, the backend does
    not offer an option that was asked for, or the backend is not wired.
    """
    spec = _spec(backend)
    if not spec.get("implemented", True):
        raise ImageTo3DError(
            f"{spec['label']} is documented here as an alternative but its "
            f"transport is not wired: {spec.get('note', '')}")
    verdict = check_input(image_path)
    if not verdict["ok"]:
        raise ImageTo3DError(verdict["reason"])
    key = api_key(backend, root)
    if spec.get("env") and not key:
        raise ImageTo3DError(available(backend, root)["reason"])

    mode = spec.get("image_mode", "field")
    if mode == "workflow":
        put = upload(backend, image_path, root=root, timeout=timeout)
        body = build_comfy_prompt(put["name"] or Path(image_path).name,
                                  seed=options.get("seed"), backend=backend)
        got = _request(backend, spec["submit_path"], key, payload=body,
                       method="POST", timeout=timeout)
        task = str(_dig(got, spec["task_key"]) or "")
        if not task:
            raise ImageTo3DError(
                f"ComfyUI accepted nothing back: {str(got)[:200]}")
        return {"task": task, "backend": backend, "payload": body}

    if mode == "file_token":
        put = upload(backend, image_path, root=root, timeout=timeout)
        options["image_token"] = put["token"]
    elif mode == "field":
        # Bare base64 or a data URI, and they are NOT interchangeable — see
        # image_b64. Hunyuan's server decodes bare; Meshy wants the URI.
        options.setdefault(
            "image",
            image_b64(image_path) if spec["kind"] == "local"
            else data_uri(image_path))

    payload = build_payload(backend, **options)
    got = _request(backend, spec["submit_path"], key, payload=payload,
                   method="POST", timeout=timeout)
    task = str(_dig(got, spec.get("task_key", "")) or "")
    if not task:
        raise ImageTo3DError(
            f"{spec['label']} returned no task id: {str(got)[:200]}")
    return {"task": task, "backend": backend, "payload": payload, "raw": got}


def poll(backend: str, task: str, *, root: Any = None, timeout: float = 900.0,
         interval: float = 3.0) -> dict:
    """Wait for a job to reach a terminal state. Returns the final envelope.

    Bounded on purpose: a job that never finishes must fail the caller rather
    than hold a seat's agent forever. Backs off gently so a slow local model
    does not turn into a poll storm on a machine that is busy generating.

    The default is fifteen minutes because a local textured generation on a
    consumer card is minutes, not seconds — a hosted-API timeout would abandon a
    perfectly healthy local run.
    """
    spec = _spec(backend)
    key = api_key(backend, root)
    path = spec.get("poll_path")
    if not path:
        raise ImageTo3DError(f"{spec['label']} is synchronous — there is no "
                             "job to poll")
    def step() -> Optional[dict]:
        last = _request(backend, path.format(task=task), key, timeout=60.0)
        if spec.get("poll_style") == "history":
            # /history is EMPTY until the run is done and populated afterwards.
            # Treating a missing key as "not finished" is the whole protocol.
            return last if last.get(task) else None
        state = str(_dig(last, spec.get("status_key", "status")) or "")
        if state in (spec.get("done") or ()):
            return last
        if state in (spec.get("dead") or ()):
            raise ImageTo3DError(
                f"{spec['label']} job {state}: "
                + str(last.get("error") or last.get("message")
                      or "no reason given")[:300])
        if state and state not in (spec.get("running") or ()):
            # An unknown state is not an excuse to spin — say so and stop.
            raise _http.PollUnknown(
                f"{spec['label']} returned unknown status {state!r}")
        return None

    try:
        return _http.poll(step, first=max(0.5, float(interval)),
                          max_wait=max(10.0, float(timeout)), factor=1.2,
                          ceiling=8.0, unknown_is_fatal=True, provider=backend,
                          label=f"job {task}")
    except ImageTo3DError:
        raise
    except _http.PollUnknown as exc:
        raise ImageTo3DError(str(exc), provider=backend) from exc
    except _http.ProviderError as exc:
        raise ImageTo3DError(
            f"{spec['label']} job {task} did not finish within {timeout:.0f}s",
            provider=backend) from exc


def download(url: str, out_path: str | os.PathLike[str], *,
             timeout: float = 300.0) -> int:
    """Fetch a finished model to disk. Returns bytes written."""
    try:
        return _http.download(url, out_path, timeout=timeout,
                              provider="image-to-3d")
    except _http.ProviderError as exc:
        raise ImageTo3DError(
            f"could not download the finished model: {exc}",
            status=exc.status) from exc


def _write_b64(blob: str, out_path: str | os.PathLike[str]) -> int:
    """A model that came back INLINE rather than as a URL — Hunyuan's server
    does this, and a caller expecting a URL would find none."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = base64.b64decode(blob)
    except Exception as exc:                                     # noqa: BLE001
        raise ImageTo3DError(f"the returned model was not valid base64: {exc}") from exc
    if not data:
        raise ImageTo3DError("the returned model was empty")
    out.write_bytes(data)
    return len(data)


def _result(backend: str, **fields) -> dict:
    """Every return from generate() in one shape, success or failure.

    Deliberately close to blender.combine()'s: {ok, checks, warnings, notes}
    plus the file, so a caller downstream does not branch on where the geometry
    came from. `stage` is the extra one — a generation can fail at four
    distinguishable points and "it failed" is not actionable.
    """
    spec = BACKENDS.get(backend) or {}
    base = {
        "ok": False, "path": "", "bytes": 0,
        "backend": backend, "kind": spec.get("kind", ""),
        "label": spec.get("label", backend),
        "task": "", "seconds": 0.0, "usd": None,
        "format": DEFAULT_FORMAT,
        # WHAT THE MESH IS, which is what the conditioning steps need to know.
        # `draft=True` is not decoration: nothing downstream may treat this as
        # a finished asset.
        "draft": True, "textured": False, "rigged": False,
        "licence": effective_licence(spec),
        "source_image": "", "stage": "",
        "checks": [], "warnings": [], "notes": [],
    }
    base.update(fields)
    return _shape.shape(base, provider=backend, model=str(
        base.get("model") or spec.get("model") or ""))


# What every generated mesh needs before anything downstream may trust it. Put
# on the result rather than in a docstring, because the caller acting on it is
# an agent that will not have read the docstring.
NEXT_STEPS = (
    "orient it — nothing here knows which way it faces, and the pipeline's "
    "convention is +Y in Blender; check with blender_turnaround and a human eye",
    "scale it to convention — bg_rescale(obj, height=1.8) also drops it to the "
    "ground",
    "clean and decimate — bg_clean(obj), then bring the triangle count down "
    "before weighting, never after",
    "weight it to the canonical skeleton — bg_human_skeleton, then the GROW "
    "repair path (bgate_rig_repair), because a generated mesh has no rest-pose "
    "record to restore from",
    "assemble with blender_combine and deliver with godot.deliver_asset — the "
    "existing gates apply unchanged",
)


def generate(image_path: str | os.PathLike[str],
             out_path: str | os.PathLike[str], *, backend: str = "",
             root: Any = None, timeout: float = 900.0,
             logical_name: str = "", work_item_id: Optional[int] = None,
             **options) -> dict:
    """Plate in, DRAFT MESH out. The whole dance as one call.

    Returns the shape above; never raises for an expected failure, the way
    krea.generate does not — a failed generation is a result with ok=False and a
    stage, because the caller is usually an agent that has to report rather than
    crash.

    ``backend=""`` asks choose() — local first, and only an unconditionally
    licensed backend automatically. If every reachable backend is conditional
    this REFUSES and names them, rather than picking one on the user's behalf.

    The download happens inside this call by construction. That is not laziness:
    one hosted backend expires its result URL after five minutes, so a design
    that hands a URL back to be fetched later works in testing and fails in
    production.
    """
    started = time.monotonic()
    if not backend:
        picked = choose(root)
        if not picked["backend"]:
            return _result("", stage="choose", error=picked["reason"],
                           notes=list(picked["candidates"]),
                           seconds=round(time.monotonic() - started, 2))
        backend = picked["backend"]
    try:
        spec = _spec(backend)
    except ImageTo3DError as exc:
        return _result(backend, stage="backend", error=str(exc))

    # QUOTE FIRST, BEFORE THE PLATE IS EVEN LOOKED AT. Pricing does not need the
    # image, and a caller asking "what would this cost" must get an answer even
    # when the run is going to be refused — otherwise the only way to learn the
    # price is to have everything else already correct.
    out = _result(backend, source_image=str(image_path),
                  usd=price_for(
                      backend, texture=bool(options.get("texture", True)),
                      quad=bool(options.get("quad"))),
                  format=str(options.get("out_format", DEFAULT_FORMAT)))
    note = price_note(backend)
    if note:
        out["warnings"].append(note)

    verdict = check_input(image_path)
    out["warnings"] += list(verdict["warnings"])
    if not verdict["ok"]:
        out["stage"] = "input"
        out["error"] = verdict["reason"]
        out["seconds"] = round(time.monotonic() - started, 2)
        return out

    try:
        if spec.get("delegate") == "krea":
            written, task = _run_krea(image_path, out_path, root=root,
                                      timeout=timeout, options=options)
        elif spec.get("response") == "binary":
            written, task = _run_sync(backend, image_path, out_path, root=root,
                                      timeout=timeout, options=options)
        else:
            written, task = _run_job(backend, image_path, out_path, root=root,
                                     timeout=timeout, options=options)
    except ImageTo3DError as exc:
        out["stage"] = "generate"
        out["error"] = str(exc)
        out["seconds"] = round(time.monotonic() - started, 2)
        return out

    out.update({
        "ok": True, "path": str(out_path), "bytes": written, "task": task,
        "seconds": round(time.monotonic() - started, 2),
        "textured": bool(options.get("texture", True)),
        "rigged": False,
        "stage": "draft",
        "notes": list(NEXT_STEPS),
    })
    out["checks"].append({
        "check": "is_a_draft",
        "detail": "a generated mesh is unconditioned geometry — unknown "
                  "orientation, unknown scale, no armature, possibly "
                  "background residue. It is not an asset until it has been "
                  "through the conditioning steps in `notes`.",
        "fix": "run the conditioning sequence before blender_combine",
    })
    return out


def generate_parts(image_path: str | os.PathLike[str],
                   out_dir: str | os.PathLike[str], *,
                   backend: str = "comfy-parts", root: Any = None,
                   timeout: float = 900.0, stem: str = "part",
                   logical_name: str = "", work_item_id: Optional[int] = None,
                   **options) -> dict:
    """Plate in, SEVERAL draft meshes out — one per semantic part.

    THE SAME DRAFT CONTRACT AS generate(), MULTIPLIED. Nothing here is an asset:
    each part is unconditioned geometry with unknown orientation and scale, and
    the notes say so. What it buys is the thing a monolith cannot give you — an
    arm that is an arm, as a separate mesh — which is what makes weighting,
    landmark measurement and per-part re-generation possible at all.

    The result carries `combine` ready to hand to blender_combine: names in the
    order the graph saved them, every path absolute. Names come from the graph's
    own filenames when they are meaningful, because a part-aware workflow that
    bothers to call its output "left_arm" knows more than this module does.

    A ONE-PART RESULT IS A WARNING, NOT A SUCCESS. A graph that merges before
    saving produces exactly the monolith this path exists to avoid, and it
    reports as a clean run of one file. Say so.
    """
    started = time.monotonic()
    try:
        spec = _spec(backend)
    except ImageTo3DError as exc:
        return _result(backend, stage="backend", error=str(exc))
    if not supports(backend, "parts"):
        return _result(backend, stage="backend",
                       error=f"{spec['label']} does not generate parts — "
                             f"use generate() for a single mesh, or a backend "
                             f"whose capabilities include 'parts'")

    out = _result(backend, source_image=str(image_path),
                  usd=price_for(backend), format="glb")
    verdict = check_input(image_path)
    out["warnings"] += list(verdict["warnings"])
    if not verdict["ok"]:
        out["stage"] = "input"
        out["error"] = verdict["reason"]
        out["seconds"] = round(time.monotonic() - started, 2)
        return out

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        job = submit(backend, image_path, root=root,
                     timeout=min(180.0, timeout), **options)
        task = job["task"]
        left = max(30.0, timeout - (time.monotonic() - started))
        done = poll(backend, task, root=root, timeout=left)
        files = _comfy_outputs(done, task)
        if not files:
            raise ImageTo3DError(
                "the part-aware run finished but reported no .glb or .gltf. "
                "Either the graph has no node that SAVES the meshes, or it uses "
                "one whose output ComfyUI does not record — use core ComfyUI's "
                "SaveGLB, which does, and save EACH PART rather than a merge.")
        written = []
        for index, entry in enumerate(files, start=1):
            source = Path(entry["filename"])
            # The graph's own name when it carries meaning, our index when it
            # does not. "ComfyUI_00017_.glb" is not a part name.
            label = re.sub(r"[^A-Za-z0-9_]+", "_", source.stem).strip("_")
            if not label or re.fullmatch(r"(?i)comfyui[_0-9]*", label):
                label = f"{stem}{index:02d}"
            dest = directory / f"{label}{source.suffix or '.glb'}"
            query = urllib.parse.urlencode({"filename": entry["filename"],
                                            "subfolder": entry["subfolder"],
                                            "type": entry["type"]})
            view = spec.get("view_path", "/api/view")
            size = download(f"{base_url(backend)}{view}?{query}", dest,
                            timeout=300.0)
            written.append({"name": label, "path": str(dest.resolve()),
                            "bytes": size, "source": entry["filename"]})
    except ImageTo3DError as exc:
        out["stage"] = "generate"
        out["error"] = str(exc)
        out["seconds"] = round(time.monotonic() - started, 2)
        return out

    out.update({
        "ok": True, "task": task, "parts": written, "count": len(written),
        "out_dir": str(directory), "bytes": sum(p["bytes"] for p in written),
        "seconds": round(time.monotonic() - started, 2),
        "textured": bool(options.get("texture", True)),
        "rigged": False, "stage": "draft",
        "combine": [{"name": p["name"], "path": p["path"]} for p in written],
        "notes": list(NEXT_STEPS),
    })
    if len(written) < 2:
        out["warnings"].append(
            "this graph produced ONE mesh. A part-aware workflow that merges "
            "before saving gives you a monolith with extra steps — save each "
            "part separately, or use the plain comfy backend and stop paying "
            "for a capability you are not getting.")
    out["checks"].append({
        "check": "parts_are_drafts",
        "detail": "each part is unconditioned geometry — unknown orientation, "
                  "unknown scale, no armature. Assembly does not confer any of "
                  "those; blender_combine puts them in one file and the "
                  "conditioning still has to happen.",
        "fix": "condition, then blender_combine(parts=result['combine']), then "
               "blender_rig, then blender_flex",
    })
    return out


def _run_krea(image_path, out_path, *, root, timeout: float,
              options: dict) -> tuple[int, str]:
    """Hand the plate to krea.generate_3d and report what it wrote.

    The other hosted backends are described declaratively and driven by this
    module's own HTTP. Krea is not, because krea.py already implements the
    whole of it — the model table, the multipart submit, the poll shape, the
    price floor and the refusal on an unpriced model. Two implementations of
    one API is one too many, and the second would drift.

    Imported inside the function on purpose: this module is careful to pull in
    nothing it does not need, and a caller who never touches Krea should not
    load it to ask which backends exist.
    """
    from bgate_adapters import krea

    spec = _spec("krea")
    kwargs = {}
    for ours, theirs in (spec.get("fields") or {}).items():
        if ours in options and options[ours] is not None:
            kwargs[theirs] = options[ours]
    model = options.get("model") or krea.DEFAULT_MODEL_3D
    # THE UNPRICED GATE IS FORWARDED NOW, AND DEFAULTS TO OPEN. It used to be
    # withheld here on the reasoning that a blind charge is the payer's call —
    # true as far as it goes, and it produced this: krea lists five 3D models,
    # exactly one has ever been invoiced on this account, and asking for any of
    # the other four dead-ended the agent on a flag no layer beneath the payer
    # could set. No retry, no options key and no fallback reached it, so a seat
    # spent its run hunting for a switch that did not exist. USER DIRECTIVE,
    # 2026-09-03: an unmeasured rate is not a reason to stop an agent. Builders
    # Gate does not meter money — the only spending figure it shows is the
    # provider's own — so a price this table has not measured yet is a gap in
    # the table, not a spend control.
    #
    # A caller that must not risk an unknown charge still closes it by passing
    # confirm_unpriced=False, and price_for() still returns None, so the quote
    # stays honest about not knowing.
    got = krea.generate_3d(str(out_path), images=[str(image_path)],
                           model=model, timeout=timeout, root=root,
                           confirm_unpriced=bool(
                               options.get("confirm_unpriced", True)),
                           **kwargs)
    if not got.get("ok"):
        raise ImageTo3DError(got.get("error") or "krea returned no mesh")
    written = int(got.get("bytes") or 0)
    if not written:
        raise ImageTo3DError("krea reported success but wrote no bytes")
    return written, str(got.get("job_id") or "")


def _run_job(backend: str, image_path, out_path, *, root, timeout: float,
             options: dict) -> tuple[int, str]:
    """submit -> poll -> fetch, for every backend that hands back a task id."""
    spec = _spec(backend)
    started = time.monotonic()
    job = submit(backend, image_path, root=root,
                 timeout=min(180.0, timeout), **options)
    task = job["task"]
    left = max(30.0, timeout - (time.monotonic() - started))
    done = poll(backend, task, root=root, timeout=left)

    if spec.get("poll_style") == "history":
        files = _comfy_outputs(done, task)
        if not files:
            # NAME THE LIKELY CAUSE, because the generic version of this
            # message sends people to look at their GPU. ComfyUI only records a
            # node's outputs in /history when that node returns a `ui` dict, and
            # ComfyUI-3D-Pack's Save_3D_Mesh returns a plain tuple — so a run
            # that worked perfectly and wrote a file on disk reports
            # "outputs": {} and is undiscoverable over HTTP. Core ComfyUI's own
            # SaveGLB does return one, under the key "3d".
            raise ImageTo3DError(
                "the ComfyUI run finished but reported no .glb or .gltf. Either "
                "the workflow has no node that SAVES the mesh, or it uses one "
                "whose output ComfyUI does not record — ComfyUI-3D-Pack's "
                "Save_3D_Mesh returns no UI entry, so its file exists on disk "
                "but never appears in /history. Use core ComfyUI's SaveGLB "
                "node, which does.")
        first = files[0]
        query = urllib.parse.urlencode({"filename": first["filename"],
                                        "subfolder": first["subfolder"],
                                        "type": first["type"]})
        view = spec.get("view_path", "/api/view")
        return download(f"{base_url(backend)}{view}?{query}", out_path,
                        timeout=300.0), task

    for key in spec.get("result_b64_keys") or ():
        blob = _dig(done, key)
        if blob:
            return _write_b64(str(blob), out_path), task
    for key in spec.get("result_keys") or ():
        url = _dig(done, key)
        if url:
            return download(str(url), out_path, timeout=300.0), task
    raise ImageTo3DError(
        f"{spec['label']} reported the job finished but returned no model: "
        f"{str(done)[:300]}")


def _run_sync(backend: str, image_path, out_path, *, root, timeout: float,
              options: dict) -> tuple[int, str]:
    """POST the plate, get the model back in the same response. No task, no poll.

    Stability's 3D endpoints work this way, and it is the simplest transport
    here by a distance — there is no job to lose, no URL to expire, and no
    status enum to misread.
    """
    spec = _spec(backend)
    key = api_key(backend, root)
    if spec.get("env") and not key:
        raise ImageTo3DError(available(backend, root)["reason"])
    fields = spec.get("fields") or {}
    form: dict[str, Any] = {}
    for name, value in options.items():
        target = fields.get(name)
        if target and value is not None:
            form[target] = value
    p = Path(image_path)
    body, content_type = multipart(form, p.name, p.read_bytes(),
                                    field=spec.get("upload_field", "image"))
    try:
        data = _http.request(
            "POST", base_url(backend) + spec["submit_path"], data=body,
            timeout=timeout, provider=backend,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": content_type,
                     # model/* is what these endpoints answer with; asking
                     # for JSON gets a JSON-wrapped base64 instead, which is
                     # a second decode for no reason.
                     "Accept": "*/*"}).body
    except _http.ProviderError as exc:
        raise _error(backend, exc, "POST", spec["submit_path"]) from exc
    if not data:
        raise ImageTo3DError(f"{spec['label']} returned an empty model")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return len(data), ""


# ---------------------------------------------------------------------------
# The health row
# ---------------------------------------------------------------------------
def doctor_row() -> dict:
    """One row in bgate_core.runtime.doctor's shape: {available, path, version,
    min_required, reason}.

    AN ABSENT ROW HERE IS NOT A FAILURE. Local 3D generation is an optional
    capability exactly like Blender, ffmpeg and whisper: without it that one
    path is unavailable and every other thing in the product works. The reason
    string says so, because `bgate doctor` exits non-zero on any missing row and
    a user reading the exit code instead of the rows will otherwise think their
    setup is broken.

    Cheap by construction — nvidia-smi and nothing else. No torch import, no
    model load, no network.
    """
    card = gpu()
    if not card["available"]:
        return {"available": False, "path": "", "version": "",
                "min_required": f"{MIN_VRAM_GB} GB VRAM",
                "reason": card["reason"] + " Optional: only local image-to-3D "
                                           "is affected."}
    if card["vram_gb"] and card["vram_gb"] < MIN_VRAM_GB:
        return {"available": False, "path": card["name"],
                "version": f"{card['vram_gb']} GB, driver {card['driver']}",
                "min_required": f"{MIN_VRAM_GB} GB VRAM",
                "reason": card["reason"] + " Optional: only local image-to-3D "
                                           "is affected."}
    return {"available": True, "path": card["name"],
            "version": f"{card['vram_gb']} GB, driver {card['driver']}",
            "min_required": f"{MIN_VRAM_GB} GB VRAM",
            "reason": ""}
