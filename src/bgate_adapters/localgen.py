"""Local 2D image generation — the art path with no key, no bill, no network.

WHY THIS EXISTS. Every art path in this product is hosted. Setup asks "do you
have an image API key?" and without one, art generation is simply dead — which
cuts against everything else here, because the rest of the product is local-first
by construction: loopback dashboard, SQLite state, a .env at the game project
root, no Docker, no cloud storage. The one place a user had to rent something was
the one place they were most likely to refuse.

AND 2D IS THE BIGGER PRIZE THAN 3D. The 2D path is the strongest thing in the
product — anchored generation, trained styles, chroma keying, an audited cut —
and it is also the one that is 100% hosted. A user with a 12 GB card can run SDXL
comfortably and a quantised Flux or Qwen-Image-Edit with room to spare; what they
could not do was reach any of it from here.

THE SUBSTRATE ALREADY EXISTED AND IS DELIBERATELY NOT REBUILT. imageto3d.py
established the loopback-ComfyUI contract for meshes: probe with a short HTTP GET,
never import torch, treat the workflow graph as the user's configuration rather
than as code, and read the licence off the declared model rather than off the
server. This module is the same contract pointed at images, and it shares the
transport, the URL, the health probe and the upload endpoint with it. Building a
second half would have produced two incompatible ones.

WHAT IS AND IS NOT DECIDED HERE:

  * THE WORKFLOW IS THE USER'S. ComfyUI takes an entire graph as the request
    body, and that graph names node classes, checkpoints, samplers and LoRAs that
    belong to one installation. Hardcoding a graph would break on somebody else's
    upgrade AND would silently pick a model, and therefore a licence, on their
    behalf. So: two API-format JSON files the user exports from their own
    ComfyUI, with placeholders this module substitutes.

  * KEYING IS NOT DONE HERE. bgate_core.art.chroma owns the keyable-background
    contract and audits the cut, and it must own it for local generations
    exactly as it does for hosted ones — otherwise a locally-made sprite is a
    different kind of file from a Krea one, which is the whole thing the
    contract exists to prevent. This module returns a flat PNG.

  * THE PRICE IS ZERO AND SAYING SO IS THE HONEST ANSWER, not a missing value.
    `usd: 0.0` is a fact about a local generation, and a caller should read
    it as one.

  * NOTHING HEAVY IS IMPORTED, EVER. No torch, no diffusers, no model library,
    at any point in this process. The server is probed with an HTTP GET. A user
    with no GPU is completely unaffected by this module's existence.

No key is read, logged, or needed. Everything here is stdlib.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from bgate_adapters import imageto3d as _i3d

from . import _http
from . import _result as _shape


class LocalGenError(_http.ProviderError):
    """A local generation could not be made. Carries what to fix."""


# The backend row this shares with imageto3d — same server, same URL env, same
# upload endpoint. Reaching into that table rather than restating it is what
# keeps "the user's ComfyUI" a single fact.
BACKEND = "comfy"

# ---------------------------------------------------------------------------
# The two workflows, and the placeholders they must carry
# ---------------------------------------------------------------------------
# TWO, NOT ONE, because text-to-image and image-conditioned editing are different
# graphs in ComfyUI — different loaders, different samplers, usually a different
# checkpoint — and pretending one graph can do both would mean asking the user to
# build something their UI does not naturally produce.
TXT2IMG_ENV = "BGATE_COMFY_T2I_WORKFLOW"
EDIT_ENV = "BGATE_COMFY_EDIT_WORKFLOW"
MODEL_ENV = "BGATE_LOCAL_IMAGE_MODEL"

PROMPT_TOKEN = "__BGATE_PROMPT__"
NEGATIVE_TOKEN = "__BGATE_NEGATIVE__"
SEED_TOKEN = "__BGATE_SEED__"
WIDTH_TOKEN = "__BGATE_WIDTH__"
HEIGHT_TOKEN = "__BGATE_HEIGHT__"
IMAGE_TOKEN = "__BGATE_IMAGE__"

IMAGE_SUFFIXES = ("png", "jpg", "jpeg", "webp")

# What the declared model means for a shipped game asset. Same three-valued
# scheme as imageto3d.MODEL_LICENCES and the same rule: an undeclared model is
# NOT permissive by omission, and this table never guesses.
FREE, CONDITIONAL, UNKNOWN = "FREE", "CONDITIONAL", "UNKNOWN"
MODEL_LICENCES: dict[str, dict] = {
    "sdxl": {"code": FREE, "summary": "CreativeML Open RAIL++-M — commercial use "
                                      "permitted, use restrictions attached",
             "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0"},
    "sd15": {"code": FREE, "summary": "CreativeML Open RAIL-M — commercial use "
                                      "permitted, use restrictions attached",
             "url": "https://huggingface.co/runwayml/stable-diffusion-v1-5"},
    "sd35-medium": {"code": CONDITIONAL,
                    "summary": "Stability Community Licence — free under a "
                               "revenue threshold, paid above it. YOUR revenue "
                               "is not a thing this tool can know",
                    "url": "https://stability.ai/community-license-agreement"},
    "flux-schnell": {"code": FREE, "summary": "Apache-2.0 (FLUX.1-schnell)",
                     "url": "https://huggingface.co/black-forest-labs/FLUX.1-schnell"},
    "flux-dev": {"code": CONDITIONAL,
                 "summary": "FLUX.1 [dev] non-commercial licence — NOT usable "
                            "in a game you sell without a separate agreement",
                 "url": "https://huggingface.co/black-forest-labs/FLUX.1-dev"},
    "qwen-image-edit": {"code": FREE, "summary": "Apache-2.0 (Qwen-Image-Edit)",
                        "url": "https://huggingface.co/Qwen/Qwen-Image-Edit"},
    "z-image-turbo": {"code": UNKNOWN,
                      "summary": "check the model card before shipping — this "
                                 "table has no verified entry",
                      "url": ""},
}


def declared_model() -> str:
    return (os.environ.get(MODEL_ENV) or "").strip().lower()


def model_licence(name: str = "") -> dict:
    """What the declared model permits. UNKNOWN when nothing was declared, and
    UNKNOWN is a state to report rather than a reason to refuse — the user may
    be running something this table has never heard of."""
    key = (name or declared_model()).strip().lower()
    if not key:
        return {"code": UNKNOWN, "model": "",
                "summary": f"no model declared. Set {MODEL_ENV} to the model "
                           "your workflow loads and this will state its terms. "
                           "The graph decides which model runs and this adapter "
                           "cannot read the graph",
                "url": ""}
    row = MODEL_LICENCES.get(key)
    if not row:
        return {"code": UNKNOWN, "model": key,
                "summary": "not in this table — read the model card before "
                           "shipping anything it made",
                "url": ""}
    return {"model": key, **row}


# ---------------------------------------------------------------------------
# Availability, without contacting anything at import time
# ---------------------------------------------------------------------------

def base_url() -> str:
    return _i3d.base_url(BACKEND)


def workflow_path(kind: str) -> str:
    env = TXT2IMG_ENV if kind == "generate" else EDIT_ENV
    return (os.environ.get(env) or "").strip()


def available(*, probe: bool = False) -> dict:
    """Can a local generation be made right now, and if not, what is missing.

    Three independent things must be true, and they fail in this order because
    that is the order a user hits them: a server, a workflow, and (only if asked)
    the server actually answering.
    """
    t2i, edit = workflow_path("generate"), workflow_path("edit")
    missing = []
    if not t2i:
        missing.append(f"{TXT2IMG_ENV} (text-to-image workflow, API format)")
    if t2i and not Path(t2i).is_file():
        missing.append(f"{TXT2IMG_ENV} points at a file that does not exist: {t2i}")
    out = {
        "backend": BACKEND,
        "url": base_url(),
        "workflows": {"generate": t2i, "edit": edit},
        "model": declared_model(),
        "licence": model_licence(),
        "usd": 0.0,
    }
    if missing:
        out.update(available=False, reason="; ".join(missing))
        return out
    if not probe:
        out.update(available=True, reason="", probed=False)
        return out
    alive = _i3d._alive(BACKEND)
    out["probed"] = True
    out["server"] = alive
    if not alive.get("alive"):
        out.update(available=False,
                   reason=f"no ComfyUI answering at {base_url()} — "
                          f"{alive.get('reason') or 'no response'}. Start it, or "
                          f"point {_i3d.BACKENDS[BACKEND]['base_env']} at where "
                          "it actually listens")
        return out
    out.update(available=True, reason="")
    return out


def status(*, probe: bool = False) -> dict:
    """Everything a caller needs to decide whether to use this, in one dict."""
    got = available(probe=probe)
    got["tokens"] = {"prompt": PROMPT_TOKEN, "negative": NEGATIVE_TOKEN,
                     "seed": SEED_TOKEN, "width": WIDTH_TOKEN,
                     "height": HEIGHT_TOKEN, "image": IMAGE_TOKEN}
    got["how"] = [
        "run ComfyUI on this machine (loopback is the default: "
        + base_url() + ")",
        "build a text-to-image graph there and 'Save (API format)'",
        f"put {PROMPT_TOKEN} in the positive CLIPTextEncode's text, "
        f"{SEED_TOKEN} in the sampler's seed, and optionally {WIDTH_TOKEN} / "
        f"{HEIGHT_TOKEN} in the latent's dimensions",
        f"point {TXT2IMG_ENV} at that file",
        f"for reference-conditioned work, do the same with a graph whose "
        f"LoadImage names {IMAGE_TOKEN} and point {EDIT_ENV} at it",
        f"declare what it loads with {MODEL_ENV} so the licence is on the record",
    ]
    return got


def doctor_row() -> dict:
    """One row for `bgate doctor`, in the optional-capability sense: absent means
    one path is unavailable and NOTHING else breaks."""
    got = available(probe=True)
    return {
        "name": "local_image",
        "available": bool(got.get("available")),
        "detail": (f"ComfyUI at {got['url']}"
                   + (f", model {got['model']}" if got.get("model") else "")
                   if got.get("available") else (got.get("reason") or "")),
        "optional": True,
    }


# ---------------------------------------------------------------------------
# The graph: config, substituted, never authored here
# ---------------------------------------------------------------------------

def build_prompt(kind: str, *, prompt: str, seed: Optional[int] = None,
                 width: int = 1024, height: int = 1024,
                 negative: str = "", image_name: str = "",
                 path: str = "") -> dict:
    """The /prompt body: the user's graph with this request substituted in.

    Raises with the whole setup instruction rather than a KeyError. What is
    missing is a file the user has to export from an application, and "no such
    file" would send them looking in the wrong place entirely.
    """
    src = path or workflow_path(kind)
    env = TXT2IMG_ENV if kind == "generate" else EDIT_ENV
    if not src:
        raise LocalGenError(
            f"no local {kind} workflow configured — export one from ComfyUI "
            f"with 'Save (API format)', put {PROMPT_TOKEN} where the positive "
            f"prompt goes"
            + (f" and {IMAGE_TOKEN} where the LoadImage names its file"
               if kind == "edit" else "")
            + f", then point {env} at it")
    p = Path(src)
    if not p.is_file():
        raise LocalGenError(f"the configured {kind} workflow does not exist: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalGenError(f"could not read the workflow at {p}: {exc}") from exc

    # THE PROMPT PLACEHOLDER IS NOT OPTIONAL. A graph without it generates
    # whatever was baked into the node when the user hit save — every time,
    # for every request, silently, and looking exactly like a working feature.
    if PROMPT_TOKEN not in raw:
        raise LocalGenError(
            f"the workflow at {p} has no {PROMPT_TOKEN} placeholder, so there "
            "is nowhere to put the prompt — every generation would come back "
            "as whatever text was baked into the graph")
    if kind == "edit" and IMAGE_TOKEN not in raw:
        raise LocalGenError(
            f"the edit workflow at {p} has no {IMAGE_TOKEN} placeholder, so "
            "the reference image would never reach it")

    def _swap(text: str, token: str, value: str) -> str:
        # JSON-escaped and spliced INSIDE the existing quotes, so a prompt
        # containing a quote or a backslash cannot break the document.
        return text.replace(token, json.dumps(value)[1:-1])

    def _number(text: str, token: str, value: int) -> str:
        """Substitute a NUMERIC token, quotes and all.

        JSON cannot hold a bare placeholder where an integer goes, so a user
        writing this into their exported graph has to write it as a STRING:
        "seed": "__BGATE_SEED__". Replacing only the token leaves "seed": "42",
        and ComfyUI validates that node input as an INT and rejects the whole
        graph with a 400 that names a node id and nothing else. So the quotes
        around the token go too. The bare form is still handled, for a graph
        that was hand-edited rather than exported.
        """
        return (text.replace('"%s"' % token, str(value))
                    .replace(token, str(value)))

    raw = _swap(raw, PROMPT_TOKEN, prompt)
    raw = _swap(raw, NEGATIVE_TOKEN, negative)
    if image_name:
        raw = _swap(raw, IMAGE_TOKEN, image_name)
    raw = _number(raw, SEED_TOKEN,
                  int(seed) if seed is not None
                  else int(time.time()) % 2_147_483_647)
    raw = _number(raw, WIDTH_TOKEN, int(width))
    raw = _number(raw, HEIGHT_TOKEN, int(height))
    try:
        graph = json.loads(raw)
    except ValueError as exc:
        raise LocalGenError(
            f"the workflow at {p} is not valid JSON after substitution: {exc}. "
            "It must be the API format, not the editor format.") from exc
    if isinstance(graph, dict) and "nodes" in graph and "class_type" not in str(
            next(iter(graph.values()), "")):
        raise LocalGenError(
            f"the workflow at {p} looks like the EDITOR format (it has a "
            "top-level 'nodes' list). ComfyUI's /prompt endpoint needs the API "
            "format — re-export with 'Save (API format)'.")
    return {"prompt": graph, "client_id": "builders-gate"}


def parse_size(size: str) -> tuple[int, int]:
    """"1024x1024" -> (1024, 1024). Anything unparseable is a square 1024,
    because a wrong aspect is a worse failure than a wrong resolution."""
    try:
        w, h = str(size).lower().split("x", 1)
        return max(64, int(w)), max(64, int(h))
    except Exception:
        return 1024, 1024


# ---------------------------------------------------------------------------
# Submit, wait, collect
# ---------------------------------------------------------------------------

def _spec() -> dict:
    return _i3d.BACKENDS[BACKEND]


def _call(path: str, *, payload: Optional[dict] = None, method: str = "GET",
          timeout: float = 60.0) -> dict:
    """One ComfyUI call, with the local-server words on failure: there is no
    key to check, there is a process to start."""
    url = _i3d.base_url(BACKEND) + path
    try:
        got = _http.request(method, url, json=payload, timeout=timeout,
                            provider="local")
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise LocalGenError(
                f"could not reach ComfyUI at {_i3d.base_url(BACKEND)} "
                f"({exc.body}) — is it running? Point "
                f"{_spec().get('base_env') or 'the base URL'} at it if it is on "
                "another host or port.", provider="local") from exc
        raise LocalGenError(
            f"ComfyUI returned HTTP {exc.status} on {method} {path}: {exc.body}",
            provider="local", status=exc.status, body=exc.body) from exc
    try:
        return got.json(provider="local")
    except _http.ProviderError as exc:
        raise LocalGenError(str(exc), provider="local") from exc


def _fetch_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    try:
        return _http.request("GET", url, headers={"Accept": "*/*"},
                             timeout=timeout, provider="local").body
    except _http.ProviderError as exc:
        if exc.status == 0:
            raise LocalGenError(f"could not reach ComfyUI at {url}: {exc.body}",
                                provider="local") from exc
        raise LocalGenError(
            f"ComfyUI returned HTTP {exc.status} fetching {url}",
            provider="local", status=exc.status) from exc


def _images(history: dict, task: str) -> list[dict]:
    """Every image this run wrote, scanned rather than looked up by node id.

    Node ids and saver class names are per-graph and per-plugin-release, so
    keying on either would break on somebody else's edit to their own workflow.
    Scanning for a usable suffix is stable against all of it — see
    ``imageto3d.comfy_scan``, which is the same walk over the same history shape
    and is shared so a fix to one scanner cannot miss the other.
    """
    found = _i3d.comfy_scan(history, task, IMAGE_SUFFIXES)
    # SAVED OUTPUTS BEAT PREVIEWS. A typical graph has a PreviewImage as well as
    # a SaveImage, and the preview is a temp-type entry at whatever resolution
    # the preview node felt like. Taking the last saved output is the closest
    # thing to "what the user meant by running this graph".
    saved = [f for f in found if f["type"] == "output"]
    return saved or found


def submit(body: dict, *, timeout: float = 60.0) -> str:
    got = _call(_spec()["submit_path"], payload=body, method="POST",
                timeout=timeout)
    task = str(got.get(_spec().get("task_key", "prompt_id")) or "")
    if not task:
        raise LocalGenError(f"ComfyUI accepted the graph but returned no "
                            f"prompt id: {json.dumps(got)[:200]}")
    return task


def wait(task: str, *, timeout: float = 600.0, poll: float = 1.0) -> dict:
    """Block until the run appears in history with outputs, or give up saying so."""
    path = _spec()["poll_path"].format(task=task)

    def step() -> Optional[dict]:
        last = _call(path, timeout=30.0)
        run = (last or {}).get(task) or {}
        if run.get("outputs"):
            return last
        status_obj = (run.get("status") or {})
        if status_obj.get("status_str") == "error":
            raise LocalGenError(
                "ComfyUI reported an error running the graph: "
                + json.dumps(status_obj)[:400])
        return None

    try:
        return _http.poll(step, first=poll, max_wait=timeout, factor=1.0,
                          ceiling=poll, provider="local", label="run")
    except LocalGenError:
        raise
    except _http.ProviderError as exc:
        raise LocalGenError(
            f"ComfyUI did not finish within {timeout:.0f}s. A first run "
            "downloads and loads weights and can be much slower than the ones "
            "after it — raise the timeout, or watch the ComfyUI console.",
            provider="local") from exc


def _result(out: Path, *, seconds: float, size: str, model: str,
            workflow: str, task: str, extra: Optional[dict] = None) -> dict:
    """The same shape imagegen returns, because chroma reads both."""
    got = _shape.shape({
        "ok": True,
        "path": str(out),
        "bytes": out.stat().st_size,
        "seconds": round(seconds, 2),
        # ZERO IS A PRICE, NOT A MISSING VALUE.
        "usd": 0.0,
        "provider": "local",
        "model": model or "(undeclared)",
        "size": size,
        "backend": BACKEND,
        "workflow": workflow,
        "task": task,
        "licence": model_licence(),
    })
    got.update(extra or {})
    return got


def _run(kind: str, prompt: str, out_path: str | os.PathLike[str], *,
         size: str, seed: Optional[int], negative: str,
         ref_paths: tuple = (), timeout: float, workflow: str = "") -> dict:
    started = time.monotonic()
    ready = available()
    if not ready.get("available") and kind == "generate" and not workflow:
        return {"ok": False, "provider": "local", "error": ready.get("reason"),
                "usd": 0.0}

    width, height = parse_size(size)
    image_name = ""
    uploaded = []
    if kind == "edit":
        if not ref_paths:
            return {"ok": False, "provider": "local", "usd": 0.0,
                    "error": "the edit path needs at least one reference image"}
        for ref in ref_paths[:1]:
            # ONE reference. A graph can only name as many LoadImage nodes as it
            # has, and this module cannot know how many that is; sending more
            # than the single documented token would silently drop the rest,
            # which is worse than saying so.
            got = _i3d.upload(BACKEND, ref, timeout=timeout)
            image_name = got.get("name") or Path(ref).name
            if got.get("subfolder"):
                image_name = f"{got['subfolder']}/{image_name}"
            uploaded.append(image_name)

    body = build_prompt(kind, prompt=prompt, seed=seed, width=width,
                        height=height, negative=negative,
                        image_name=image_name, path=workflow)
    task = submit(body, timeout=min(timeout, 120.0))
    history = wait(task, timeout=timeout)
    images = _images(history, task)
    if not images:
        return {"ok": False, "provider": "local", "usd": 0.0,
                "task": task,
                "error": "the graph ran but wrote no image — it needs a "
                         "SaveImage (or any node that reports an image output) "
                         "at the end"}
    pick = images[-1]
    query = urllib.parse.urlencode({"filename": pick["filename"],
                                    "subfolder": pick["subfolder"],
                                    "type": pick["type"]})
    blob = _fetch_bytes(base_url() + _spec()["view_path"] + "?" + query,
                        timeout=timeout)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    return _result(out, seconds=time.monotonic() - started, size=size,
                   model=declared_model(), workflow=kind, task=task,
                   extra={"outputs": len(images),
                          "uploaded": uploaded,
                          "seed": seed})


def generate(prompt: str, out_path: str | os.PathLike[str], *,
             size: str = "1024x1024", quality: str = "medium",
             seed: Optional[int] = None, negative: str = "",
             transparent: bool = False, timeout: float = 600.0,
             root: Any = None, logical_name: str = "",
             work_item_id: Optional[int] = None, task_kind: str = "",
             tileable: bool = False, workflow: str = "") -> dict:
    """One image from the local model, on the same result shape as imagegen.

    `quality` is accepted and ignored: the graph decides the sampler and step
    count, and pretending otherwise would put a knob in front of an agent that
    turns nothing. `transparent` is likewise accepted and ignored — local
    checkpoints do not paint alpha, and bgate_core.art.chroma is what makes the
    cut-out. Both stay in the signature so this is a drop-in for the hosted
    adapters rather than a special case every caller has to branch on.
    """
    try:
        got = _run("generate", prompt, out_path, size=size, seed=seed,
                   negative=negative, timeout=timeout, workflow=workflow)
    except LocalGenError as exc:
        return {"ok": False, "provider": "local", "usd": 0.0,
                "error": str(exc)}
    if got.get("ok") and tileable:
        try:
            from bgate_adapters import imagegen as _ig
            got["tileable"] = _ig.make_tileable(got["path"])
        except Exception as exc:      # a failed post-pass must not lose the art
            got["tileable"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    got.setdefault("transparent", False)
    got["quality"] = quality
    return got


def edit(prompt: str, ref_paths: list, out_path: str | os.PathLike[str], *,
         size: str = "1024x1024", quality: str = "medium",
         seed: Optional[int] = None, negative: str = "",
         transparent: bool = False, timeout: float = 600.0,
         root: Any = None, logical_name: str = "",
         work_item_id: Optional[int] = None, task_kind: str = "",
         tileable: bool = False, workflow: str = "") -> dict:
    """Generate conditioned on a reference image, through the edit graph.

    This is how identity is held locally: the pinned reference goes up, the
    graph does whatever the user built it to do with it (img2img, IP-Adapter,
    Qwen-Image-Edit, ControlNet), and the result comes back on the same shape.
    """
    try:
        got = _run("edit", prompt, out_path, size=size, seed=seed,
                   negative=negative, ref_paths=tuple(ref_paths or ()),
                   timeout=timeout, workflow=workflow)
    except LocalGenError as exc:
        return {"ok": False, "provider": "local", "usd": 0.0,
                "error": str(exc)}
    if got.get("ok") and tileable:
        try:
            from bgate_adapters import imagegen as _ig
            got["tileable"] = _ig.make_tileable(got["path"])
        except Exception as exc:
            got["tileable"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    got.setdefault("transparent", False)
    got["quality"] = quality
    return got
