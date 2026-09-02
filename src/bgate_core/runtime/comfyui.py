"""Read-only introspection of a running ComfyUI, and of the graphs we send it.

WHY THIS EXISTS. The local 2D path works and is completely opaque. A user with
ComfyUI running has, from inside Builders Gate, no way to answer any of the
questions they actually have:

  * is my GPU being used, or is it quietly on CPU?
  * which checkpoints does this install even have? (today the only way to name
    one is to type a filename correctly, blind, into a graph)
  * what is a "workflow", and why are there two of them?
  * this graph has a prompt typed into it — does mine get used, or that one?
  * it looks frozen. Is it frozen, or is it third in a queue?
  * where did the last image go?

Every one of those is answerable from HTTP GETs that cost milliseconds on
loopback, and from the workflow file we already read. None of it was surfaced.

WHAT THIS MODULE WILL AND WILL NOT DO.

  * GET ONLY. Nothing here submits, queues, interrupts, frees memory or deletes.
    The one mutating endpoint the product uses lives in
    :mod:`bgate_adapters.localgen`, which is the module that owns generation.
  * NOTHING IS REQUIRED TO BE THERE. ComfyUI is a fast-moving application with
    custom nodes from a dozen authors, and its JSON shapes differ between
    builds. Every read below navigates with ``.get`` and degrades to an empty
    answer, never to an exception and never to a wrong claim. A field this
    build does not recognise is reported as absent, not invented.
  * SHORT TIMEOUTS. A panel poll must not block on a socket nobody is listening
    on — that is the common case, not the exceptional one.
  * THE /api PREFIX, for the reason ``imageto3d.BACKENDS`` already records:
    ComfyUI registers every route twice, bare and under ``/api``, and the bare
    ones share a namespace with the web UI's own routing.

THE WORKFLOW READER IS THE OTHER HALF, and it is the part that answers the
question a user cannot answer any other way: Builders Gate REWRITES specific
values inside their graph before submitting it. Which ones, and with what, was
knowable only by reading ``localgen.build_prompt``. :func:`describe_workflow`
turns the file into a short legible summary with the rewritten inputs marked.

Stdlib only, no heavy imports, ever.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

# Loopback, so this is generous rather than tight — but still short enough that
# a dead server costs a panel one blink instead of a spinner.
TIMEOUT = 2.5

# The node classes worth asking about, and the input on each that carries the
# list of files the install can actually see. This is the whole "show me what I
# have instead of making me type it" mechanism, and it is a table so a build
# that has none of these simply reports nothing rather than failing.
#
# The shape being read: /api/object_info/<Class> answers
#   {"<Class>": {"input": {"required": {"ckpt_name": [[<options>], {...}]}}}}
# where a combo input's first element is the list of legal values. Anything that
# is not that shape is skipped — a custom node pack is free to disagree.
CATALOGUE: tuple[tuple[str, str, str, str], ...] = (
    ("checkpoints", "CheckpointLoaderSimple", "ckpt_name",
     "the model file a graph loads — this is what actually decides how your "
     "art looks"),
    ("loras", "LoraLoader", "lora_name",
     "small style add-ons layered on top of a checkpoint"),
    ("vae", "VAELoader", "vae_name",
     "the decoder that turns the model's internal image into pixels; most "
     "graphs use the one baked into the checkpoint"),
    ("samplers", "KSampler", "sampler_name",
     "the algorithm that does the denoising steps"),
    ("schedulers", "KSampler", "scheduler",
     "how the denoising strength is spread across those steps"),
)


class ComfyReadError(RuntimeError):
    """A read did not happen. Carries a sentence, never a traceback."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _get(base: str, path: str, *, timeout: float = TIMEOUT) -> Any:
    url = (base or "").rstrip("/") + path
    req = urllib.request.Request(url, method="GET", headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ComfyReadError(
            f"{url} answered HTTP {exc.code} — the server is up but does not "
            f"serve that path; this ComfyUI build may be older or newer than "
            f"the one this panel was written against") from exc
    except Exception as exc:                                     # noqa: BLE001
        reason = getattr(exc, "reason", None) or exc
        raise ComfyReadError(f"nothing answered at {url} ({reason})") from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ComfyReadError(f"{url} answered something that is not JSON") from exc


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gb(value: Any) -> Optional[float]:
    """Bytes -> GB, or None. ComfyUI reports VRAM in bytes; a panel that prints
    17179869184 has told the user nothing."""
    got = _num(value)
    return round(got / (1024 ** 3), 1) if got and got > 0 else None


# ---------------------------------------------------------------------------
# /system_stats — "is my GPU actually being used"
# ---------------------------------------------------------------------------

def system_stats(base: str, *, timeout: float = TIMEOUT) -> dict:
    """Versions and devices, flattened into something worth putting on screen.

    Returns ``{"ok": False, "error": ...}`` rather than raising, because every
    caller here is painting a panel and a health read that can take the panel
    down is worse than one that says it failed.
    """
    try:
        raw = _get(base, "/api/system_stats", timeout=timeout)
    except ComfyReadError as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "system_stats answered an unexpected shape"}
    sysinfo = raw.get("system") if isinstance(raw.get("system"), dict) else {}
    devices = []
    for one in (raw.get("devices") or []):
        if not isinstance(one, dict):
            continue
        devices.append({
            "name": str(one.get("name") or "")[:120],
            "type": str(one.get("type") or ""),
            "vram_total_gb": _gb(one.get("vram_total")),
            "vram_free_gb": _gb(one.get("vram_free")),
            "torch_vram_free_gb": _gb(one.get("torch_vram_free")),
        })
    # THE ANSWER TO THE QUESTION, not the raw row. "type": "cuda" is the fact
    # that matters and it is the one a user cannot read off their own screen.
    accel = [d for d in devices if d["type"] and d["type"].lower() != "cpu"]
    return {
        "ok": True,
        "comfyui_version": str(sysinfo.get("comfyui_version") or ""),
        # The banner is "3.13.1 (tags/…) [MSC …]" — the first word is the part
        # anybody reads.
        "python_version": str(sysinfo.get("python_version") or "").split(" ")[0],
        "pytorch_version": str(sysinfo.get("pytorch_version") or ""),
        "os": str(sysinfo.get("os") or ""),
        "devices": devices,
        "accelerated": bool(accel),
        "verdict": (
            f"running on {accel[0]['name'] or accel[0]['type']}"
            + (f", {accel[0]['vram_free_gb']} GB of "
               f"{accel[0]['vram_total_gb']} GB free"
               if accel[0]["vram_total_gb"] else "")
            if accel else
            "no GPU device reported — this install is on the CPU, which will "
            "generate, very slowly"),
    }


# ---------------------------------------------------------------------------
# /object_info — "what does this install actually have"
# ---------------------------------------------------------------------------

def _combo(payload: Any, node_class: str, field: str) -> list[str]:
    """The option list off one combo input, or [] for any shape we do not know."""
    if not isinstance(payload, dict):
        return []
    node = payload.get(node_class)
    if not isinstance(node, dict):
        return []
    inputs = node.get("input")
    if not isinstance(inputs, dict):
        return []
    for bucket in ("required", "optional"):
        group = inputs.get(bucket)
        if not isinstance(group, dict) or field not in group:
            continue
        spec = group[field]
        # [[options], {meta}] is the documented combo shape.
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return [str(v) for v in spec[0] if isinstance(v, (str, int, float))]
    return []


def catalogue(base: str, *, timeout: float = TIMEOUT) -> dict:
    """Everything this install can enumerate: checkpoints, LoRAs, VAEs, samplers.

    One GET per node class rather than one GET of the whole ``/object_info``
    document, which on an install with a few custom node packs is megabytes and
    is not worth moving to answer "which checkpoints do I have".

    A class this install does not have (no ``LoraLoader``, say) contributes an
    empty list and a reason, not a failure.
    """
    out: dict[str, dict] = {}
    errors: list[str] = []
    # One GET per CLASS, not per row: samplers and schedulers are two fields on
    # the same KSampler document and fetching it twice is a wasted round trip on
    # every repaint.
    seen: dict[str, Any] = {}
    for key, node_class, field, help_text in CATALOGUE:
        if node_class not in seen:
            try:
                seen[node_class] = _get(
                    base, "/api/object_info/" + urllib.parse.quote(node_class),
                    timeout=timeout)
            except ComfyReadError as exc:
                seen[node_class] = exc
        payload = seen[node_class]
        if isinstance(payload, ComfyReadError):
            message = str(payload)
            if message not in errors:
                errors.append(message)
            out[key] = {"items": [], "node": node_class, "field": field,
                        "help": help_text, "error": message}
            continue
        out[key] = {"items": _combo(payload, node_class, field),
                    "node": node_class, "field": field,
                    "help": help_text, "error": ""}
    return {"ok": not errors or any(v["items"] for v in out.values()),
            "groups": out, "errors": errors}


# ---------------------------------------------------------------------------
# /queue and /history — "is it frozen, or is it busy"
# ---------------------------------------------------------------------------

def queue(base: str, *, timeout: float = TIMEOUT) -> dict:
    try:
        raw = _get(base, "/api/queue", timeout=timeout)
    except ComfyReadError as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "queue answered an unexpected shape"}
    running = raw.get("queue_running") or []
    pending = raw.get("queue_pending") or []
    running = running if isinstance(running, list) else []
    pending = pending if isinstance(pending, list) else []
    return {
        "ok": True,
        "running": len(running),
        "pending": len(pending),
        "verdict": (
            "idle — nothing queued" if not running and not pending
            else f"{len(running)} running"
                 + (f", {len(pending)} waiting behind it" if pending else "")),
    }


def _image_rows(outputs: Any, base: str) -> list[dict]:
    """Every image an entry reports, as viewable URLs. Same walk
    ``imageto3d.comfy_scan`` does, kept independent because this one wants the
    URL for an <img> and that one wants bytes."""
    rows: list[dict] = []
    if not isinstance(outputs, dict):
        return rows
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        for item in (node_out.get("images") or []):
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            query = urllib.parse.urlencode({
                "filename": str(item.get("filename")),
                "subfolder": str(item.get("subfolder") or ""),
                "type": str(item.get("type") or "output"),
            })
            rows.append({
                "filename": str(item.get("filename")),
                "type": str(item.get("type") or "output"),
                "url": base.rstrip("/") + "/api/view?" + query,
            })
    return rows


def history(base: str, *, limit: int = 6, timeout: float = TIMEOUT) -> dict:
    """The last few runs THIS SERVER did — ours and the user's own, undivided.

    Deliberately undivided: the question this answers is "did the thing I just
    pressed actually reach ComfyUI", and filtering to runs Builders Gate
    submitted would hide the case where it did not.
    """
    try:
        raw = _get(base, f"/api/history?max_items={max(1, min(int(limit), 24))}",
                   timeout=timeout)
    except ComfyReadError as exc:
        return {"ok": False, "error": str(exc), "runs": []}
    if not isinstance(raw, dict):
        return {"ok": False, "error": "history answered an unexpected shape",
                "runs": []}
    runs = []
    for prompt_id, entry in list(raw.items())[-max(1, int(limit)):]:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        images = _image_rows(entry.get("outputs"), base)
        runs.append({
            "id": str(prompt_id)[:40],
            "status": str(status.get("status_str") or
                          ("done" if images else "")) or "unknown",
            "completed": bool(status.get("completed")),
            "images": images[:4],
            "image_count": len(images),
        })
    runs.reverse()          # newest first, which is the order a human scans
    return {"ok": True, "runs": runs}


# ---------------------------------------------------------------------------
# The workflow file — "which bits of my graph does Builders Gate overwrite"
# ---------------------------------------------------------------------------

# What each placeholder means, in the user's terms rather than the adapter's.
# Keyed by token so this table and localgen's constants cannot drift into two
# different vocabularies — see localruntimes, which builds the map from
# localgen's own token names.
TOKEN_MEANING: dict[str, str] = {
    "prompt": "the prompt you (or an agent) asked for, on every single run",
    "negative": "the negative prompt, when one is given — blank otherwise",
    "seed": "a fresh random seed per run, unless a run asks for a specific one",
    "width": "the width of the size you asked for",
    "height": "the height of the size you asked for",
    "image": "the reference image, after Builders Gate uploads it to ComfyUI",
}


def _title(node: dict, class_type: str) -> str:
    meta = node.get("_meta")
    if isinstance(meta, dict) and meta.get("title"):
        return str(meta["title"])[:80]
    return class_type


# Inputs whose value names a weights file. Scanning for these is how the panel
# answers "which model is this graph actually running", which is a different
# question from BGATE_LOCAL_IMAGE_MODEL (that one is a LICENCE declaration and
# the user has to make it themselves — nothing can read a licence off a graph).
_WEIGHT_INPUTS = ("ckpt_name", "unet_name", "lora_name", "vae_name",
                  "model_name", "clip_name", "control_net_name")


def describe_workflow(path: str, tokens: dict[str, str]) -> dict:
    """A legible summary of one exported ComfyUI graph.

    ``tokens`` is ``{meaning_key: placeholder}`` — passed in rather than
    imported so this module never depends on the adapter, and so the 3D
    workflows (which use a different placeholder set) get the same reader.

    Answers, in order of how often it is the actual problem:
      * is the file there at all;
      * is it the API format, or the editor format somebody exported by
        pressing the wrong Save (the single most common mistake — see the same
        check in ``localgen.build_prompt`` and ``imageto3d``);
      * which nodes it has;
      * WHICH INPUTS BUILDERS GATE WILL OVERWRITE, and with what;
      * which weights files it names;
      * which placeholders are missing, which is why a run comes back looking
        like it ignored you.
    """
    out: dict[str, Any] = {"path": path, "exists": False, "format": "",
                           "nodes": [], "node_count": 0, "injected": [],
                           "missing": [], "weights": [], "warnings": [],
                           "error": ""}
    if not (path or "").strip():
        out["error"] = "not set"
        return out
    file = Path(path)
    if not file.is_file():
        out["error"] = (f"{path} is not a file — if you moved or renamed the "
                        "export, point this at the new one")
        return out
    out["exists"] = True
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        out["error"] = f"could not read it: {exc}"
        return out
    try:
        graph = json.loads(raw)
    except ValueError as exc:
        out["format"] = "invalid"
        out["error"] = f"this file is not valid JSON: {exc}"
        return out

    # THE EDITOR-FORMAT TRAP. ComfyUI's plain Save writes a document with a
    # top-level "nodes" LIST and link records; its "Save (API format)" writes a
    # flat map of node id -> {class_type, inputs}. Only the second one can be
    # POSTed. They are both .json, they are both a workflow, and the error you
    # get from sending the wrong one names a node id and nothing else.
    if isinstance(graph, dict) and isinstance(graph.get("nodes"), list):
        out["format"] = "editor"
        out["node_count"] = len(graph.get("nodes") or [])
        out["error"] = (
            "this is the EDITOR format, not the API format. In ComfyUI use "
            "Workflow → Export (API) — the plain Save writes a file that looks "
            "the same and cannot be submitted.")
        return out
    if not isinstance(graph, dict):
        out["format"] = "invalid"
        out["error"] = "the top level of this file is not an object"
        return out

    out["format"] = "api"
    found: set[str] = set()
    nodes = []
    weights: list[str] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not node.get("class_type"):
            continue
        class_type = str(node.get("class_type"))
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        marks = []
        for field, value in inputs.items():
            if not isinstance(value, (str, int, float)):
                continue      # a [node_id, slot] link, not a literal
            text = str(value)
            for meaning, token in tokens.items():
                if token and token in text:
                    found.add(meaning)
                    marks.append({
                        "field": str(field),
                        "meaning": meaning,
                        "what": TOKEN_MEANING.get(
                            meaning, f"replaced with the run's {meaning}"),
                    })
            if (field in _WEIGHT_INPUTS and isinstance(value, str)
                    and value and not any(t and t in value
                                          for t in tokens.values())):
                weights.append(f"{field}: {value}")
        row = {"id": str(node_id), "class_type": class_type,
               "title": _title(node, class_type), "injected": marks}
        nodes.append(row)
        if marks:
            out["injected"].append(row)

    # Stable order: injected nodes first (they are the answer to the question
    # this view exists for), then the rest by node id as exported.
    nodes.sort(key=lambda r: (not r["injected"], r["id"]))
    out["nodes"] = nodes
    out["node_count"] = len(nodes)
    out["weights"] = sorted(set(weights))[:12]
    out["missing"] = [m for m in tokens if m not in found]

    if "prompt" in out["missing"]:
        out["warnings"].append(
            f"there is no {tokens.get('prompt', 'prompt placeholder')} anywhere "
            "in this graph, so every run would use whatever prompt text was "
            "typed into the node when you exported it — the same image, every "
            "time, looking exactly like a working feature")
    if "seed" in out["missing"]:
        out["warnings"].append(
            "no seed placeholder — every run reuses the seed baked into the "
            "export, so asking for a variation gives you the same picture")
    if not out["nodes"]:
        out["warnings"].append(
            "no nodes with a class_type — this does not look like an exported "
            "ComfyUI graph")
    return out
