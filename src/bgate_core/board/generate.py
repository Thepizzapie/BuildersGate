"""Generate nodes — the MODEL is the step, not an agent that talks to a model.

Every image in this tool used to reach the canvas the same way: a workflow step
became a queue item, a Claude session picked it up, and somewhere inside that
session an image tool got called. That is a fine way to make one asset and a
terrible way to ANSWER A QUESTION — "which of these three models draws my
paladin best?" A comparison needs the same prompt fanned into several models at
once, with nothing between the prompt and the provider that can improvise.

So a `generate` node calls the provider directly. No seat, no queue item, no
session. Config says either WHAT is being made and how good it must be
(``{task_kind, tier}``, resolved through :mod:`bgate_core.board.tiers`) or names a
provider and model outright — the override exists precisely so a comparison can
put two rungs of the same ladder side by side.

Two boundaries are load-bearing:

  * :func:`call_provider` is the ONLY place a provider is touched. Everything
    else in the node — budget, artifact registration, spend — is provider
    agnostic, and a test stubs this one function instead of the network.
  * money is checked BEFORE each candidate, not once for the batch. A node that
    asks for six candidates and can afford two must produce two and say why it
    stopped, rather than discovering the ceiling after spending past it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from bgate_adapters import imagegen, kie, krea
from ..art import chroma
from . import activity, spend as _spend, tiers as _tiers
from ..store import artifacts as _artifacts

PROVIDERS = ("krea", "openai", "kie", "local")

# "local" is a real provider and not a special case: it goes through the same
# chroma door, returns the same result shape, and prices at a genuine 0.0 rather
# than at a missing value. What it does NOT have is a model table with published
# prices, because the model is whatever the user's own ComfyUI graph loads —
# which is why the explicit-override branch below stops asking krea about it.

# A node fans out; it does not run a farm. Eight is already more candidates than
# a human will compare in one sitting.
MAX_CANDIDATES = 8

# A provider that never answers must not hold a run open forever. Both adapters
# take a deadline; this is the ceiling on what a node may ask for.
DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 900.0

# Candidates land inside the project (artifacts.register refuses anything
# outside it) but out of the game's source tree, beside every other generated
# image this tool makes.
OUT_ROOT = Path(".bgate_out") / "art" / "workflow"

PRODUCER = "workflow.generate"


class GenerateRefused(ValueError):
    """This node cannot generate, and the reason is something a human can fix.

    ValueError so the route layer's existing 400/409 mapping applies unchanged.
    """


def _slug(text: str, fallback: str = "candidate") -> str:
    out = re.sub(r"[^a-z0-9_-]+", "_", str(text or "").strip().lower()).strip("_")
    return out[:60] or fallback


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# What this node is going to do, and what it will cost
# ---------------------------------------------------------------------------

def plan(config: dict, *, style_refs: int = 0, root: Any = None) -> dict:
    """Resolve a node's config into a concrete provider + model + price.

    Two ways in, on purpose. ``{task_kind, tier}`` is the one a non-engineer
    uses — it cannot name a model that is incapable of the job, because
    ``tiers.resolve`` refuses. ``{provider, model}`` is the escape hatch a
    comparison needs, and it is checked against the adapter's own catalogue so
    a typo fails here rather than as a 422 mid-run.
    """
    config = config if isinstance(config, dict) else {}
    provider = str(config.get("provider") or "").strip().lower()
    model = str(config.get("model") or "").strip()
    # `kind` is what the node card calls this field; `task_kind` is what the
    # engine calls it. Both mean "what am I making" — accept either rather than
    # making the palette and the engine disagree in public.
    task_kind = str(config.get("task_kind") or config.get("kind_of_asset")
                    or config.get("kind") or "").strip()
    tier = str(config.get("tier") or _tiers.DEFAULT_TIER).strip()
    quality = str(config.get("quality") or "medium").strip()

    if provider or model:
        if not provider or not model:
            raise GenerateRefused(
                "an explicit model override needs BOTH provider and model "
                "(got provider=%r model=%r) — or drop both and set "
                "{task_kind, tier} instead" % (provider, model))
        if provider not in PROVIDERS:
            raise GenerateRefused(
                f"unknown provider {provider!r} — known providers are {list(PROVIDERS)}")
        if provider == "krea" and model not in krea.MODELS:
            raise GenerateRefused(
                f"krea has no model {model!r} — known: {sorted(krea.MODELS)}")
        if provider == "krea":
            unit = krea.price_for(model, style_refs=style_refs)
            note = (krea.MODELS.get(model) or {}).get("note", "")
        elif provider == "kie":
            if model not in kie.IMAGE_MODELS:
                raise GenerateRefused(
                    f"kie has no image model {model!r} — known: "
                    f"{sorted(kie.IMAGE_MODELS)}")
            # UNPRICED, AND THAT REFUSES THE NODE RATHER THAN COSTING IT ZERO.
            # kie publishes no per-model price, and plan()'s unit price feeds
            # spend.check — a 0.0 here would let a six-candidate fan-out through
            # a ceiling that exists precisely to stop one. The same reasoning as
            # krea.generate_3d's confirm_unpriced gate, applied at plan time
            # because that is where this engine decides.
            if kie.usd_per_credit() is None:
                raise GenerateRefused(
                    "kie publishes no per-generation price, so this node cannot "
                    "be quoted before it runs and the spend ceiling would read "
                    "it as free. Set BGATE_KIE_USD_PER_CREDIT to your account's "
                    "credit rate, or use a provider with a published price. "
                    + kie.PRICE_NOTE)
            # Still not a quote — it is the ceiling for the largest image job
            # kie's own quickstart describes ("typically 10-50 credits"), which
            # is the honest thing to charge a budget check with.
            unit = kie.cost_usd(50) or 0.0
            note = (kie.MODELS.get(model) or {}).get("note", "")
        elif provider == "local":
            # FREE, AND SAYING SO IS THE ANSWER. The spend gate reads a number
            # as permission, and 0.0 is the correct number for a generation
            # that runs on the user's own card.
            unit = 0.0
            note = "local ComfyUI — no spend, and the licence is the model's"
        else:
            unit = imagegen.price_per_image(quality)
            note = ""
    elif task_kind:
        try:
            resolved = _tiers.resolve(task_kind, tier, root=root)
        except _tiers.NoSuchTier as exc:
            raise GenerateRefused(str(exc)) from exc
        provider, model = resolved["provider"], resolved["model"]
        unit, note = resolved["usd"], resolved.get("note", "")
    else:
        raise GenerateRefused(
            "a generate node needs either {task_kind, tier} (the tier ladder "
            "picks the model) or an explicit {provider, model} override — it "
            "has neither")

    count = _int(config.get("count"), 1, 1, MAX_CANDIDATES)
    timeout = DEFAULT_TIMEOUT_S
    try:
        if config.get("timeout"):
            timeout = max(5.0, min(MAX_TIMEOUT_S, float(config["timeout"])))
    except (TypeError, ValueError):
        pass
    # SEED 0 MEANS RANDOM. The node card defaults this field to 0, and 0 is a
    # perfectly good seed — so every generation ran with the same one and every
    # run produced a byte-identical image. Generating a variant is the entire
    # point of the node, and a fixed default silently removed it. Pass a
    # non-zero seed to pin a result deliberately.
    seed = config.get("seed")
    try:
        seed = int(seed) if seed not in (None, "") else None
    except (TypeError, ValueError):
        seed = None
    if seed == 0:
        seed = None

    return {
        "provider": provider, "model": model, "count": count,
        "size": str(config.get("size") or "1024x1024"),
        "quality": quality,
        "transparent": bool(config.get("transparent", False)),
        "seed": seed, "timeout": timeout,
        "unit_usd": round(float(unit), 4),
        "projected_usd": round(float(unit) * count, 4),
        "task_kind": task_kind, "tier": tier if task_kind else "",
        "note": note,
    }


# ---------------------------------------------------------------------------
# The one seam that touches a provider
# ---------------------------------------------------------------------------

def call_provider(provider: str, model: str, prompt: str, out_path: str, *,
                  size: str = "1024x1024", seed: Optional[int] = None,
                  style_refs: Sequence[tuple[str, float]] = (),
                  quality: str = "medium", transparent: bool = False,
                  timeout: float = DEFAULT_TIMEOUT_S,
                  task_kind: str = "", logical_name: str = "",
                  root: Any = None) -> dict:
    """One image, one provider. Returns the adapters' shared result shape.

    Never raises: a provider failure is a node failure with a reason, and the
    reason has to survive being read by someone who is not watching a traceback.
    """
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider {provider!r}",
                "provider": provider, "model": model}
    try:
        # THROUGH THE CONTRACT, not around it. chroma.generate is the single
        # door that appends the project's locked art direction to the prompt,
        # forces a keyable background, keys it and audits the cut — and it
        # dispatches to both providers itself.
        #
        # This used to call krea.generate / imagegen.generate directly, which
        # meant every workflow generation ran with NO bible and NO alpha: a
        # project whose bible locks "chunky pixel art, isometric 2:1, dark neon
        # office palette" produced a photoreal man in a server room, with three
        # style references wired in and ignored. Two seams built in parallel,
        # and the engine drove past the one that enforces anything.
        return chroma.generate(
            prompt, str(out_path), provider=provider, model=model,
            task_kind=task_kind, size=size, quality=quality, seed=seed,
            ref_paths=[str(path) for path, _ in style_refs],
            ref_strength=(float(style_refs[0][1]) if style_refs else 0.5),
            transparent=transparent, timeout=timeout, root=root,
            logical_name=logical_name)
    except Exception as exc:  # adapters raise for bad shapes; a node must not
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "provider": provider, "model": model}


# ---------------------------------------------------------------------------
# Running one node
# ---------------------------------------------------------------------------

def out_dir(root: str | os.PathLike[str], run_id: int, node_id: str) -> Path:
    return Path(root) / OUT_ROOT / f"run{int(run_id)}" / _slug(node_id, "node")


def _write_prompt(root: str | os.PathLike[str], *, config: dict,
                  prompt: str, label: str = "") -> dict:
    """A node whose output is words, not pixels.

    Returns the same envelope as an image generation so the engine, the run
    view and the wire that carries the value downstream all stay one code path.
    """
    from ..art import promptwriter

    note = str(config.get("ask") or config.get("instruction") or "").strip()
    subject = str(config.get("subject") or "").strip()
    written = promptwriter.expand(note or prompt, subject=subject or prompt,
                                  task_kind=str(config.get("task_kind") or ""),
                                  root=root)
    if not written.get("ok"):
        return {"ok": False, "error": written.get("error", "prompt writing failed"),
                "artifacts": [], "usd": 0.0, "provider": "openai",
                "model": written.get("model", "")}
    _spend.record(root, written.get("estimated_usd", 0.0), kind="other",
                  detail=f"prompt writer ({label or 'llm.prompt'})")
    return {"ok": True, "error": "", "artifacts": [], "provider": "openai",
            "model": written.get("model", ""),
            "usd": round(written.get("estimated_usd", 0.0), 4),
            "output": {"text": written["text"]},
            "seconds": written.get("seconds", 0.0)}


def run(root: str | os.PathLike[str], *, run_id: int, node_id: str,
        label: str = "", config: Optional[dict] = None, prompt: str = "",
        style_refs: Iterable[tuple[str, float]] = (),
        logical_name: str = "", refs: Optional[list[str]] = None,
        work_item_id: Optional[int] = None) -> dict:
    """Produce this node's candidates and register every one of them.

    Returns ``{ok, error, provider, model, artifacts:[...], usd}``. ``ok=False``
    always carries an ``error`` a human can act on — an empty prompt, a budget
    ceiling, a provider that refused — because a generate node that fails
    silently is the failure mode this whole engine exists to remove.
    """
    config = config if isinstance(config, dict) else {}
    prompt = str(prompt or "").strip()
    style_refs = [(str(p), float(s)) for p, s in (style_refs or ())]

    # A text node runs HERE, inline, like every other runnable node — one API
    # call, about two seconds. It used to be an agent step, so clicking "run"
    # queued a Claude session to rewrite a sentence and the node just sat at
    # "queued" doing nothing. Same button, same wait, same shaped result.
    if str(config.get("produces") or "").strip() == "text":
        return _write_prompt(root, config=config, prompt=prompt, label=label)

    if not prompt:
        return {"ok": False, "error":
                "this generate node has no prompt — wire a text output into it "
                "or set config.prompt", "artifacts": [], "usd": 0.0}

    try:
        spec = plan(config, style_refs=len(style_refs), root=root)
    except GenerateRefused as exc:
        return {"ok": False, "error": str(exc), "artifacts": [], "usd": 0.0}

    # Include the node id unless a name was chosen explicitly: two model cards
    # both labelled "Image model" registered every candidate under ONE logical
    # name, so the comparison's two arms became indistinguishable in the
    # registry — and "which model made this" is the only question being asked.
    explicit = logical_name or config.get("logical_name")
    name = (_slug(explicit) if explicit
            else _slug(f"{label or 'candidate'}_{node_id}", _slug(node_id, "candidate")))
    target = out_dir(root, run_id, node_id)
    produced: list[dict] = []
    spent = 0.0
    stopped = ""

    for index in range(spec["count"]):
        # Money first, every single time. The ceiling is a refusal, not a
        # warning, and checking once for the batch would walk straight past it
        # on candidate two.
        verdict = _spend.check(root, projected_usd=spec["unit_usd"])
        if not verdict.get("allowed"):
            stopped = str(verdict.get("reason") or "the spend budget refused this generation")
            break
        out_path = target / f"{name}_{index + 1}.png"
        result = call_provider(
            spec["provider"], spec["model"], prompt, str(out_path),
            size=spec["size"], seed=(None if spec["seed"] is None
                                     else spec["seed"] + index),
            style_refs=style_refs, quality=spec["quality"],
            transparent=spec["transparent"], timeout=spec["timeout"],
            task_kind=spec.get("task_kind", ""), logical_name=name, root=root)
        if not result.get("ok"):
            return {"ok": False, "artifacts": produced, "usd": round(spent, 4),
                    "provider": spec["provider"], "model": spec["model"],
                    "error": f"{spec['provider']}/{spec['model']} failed on "
                             f"candidate {index + 1} of {spec['count']}: "
                             f"{result.get('error') or 'no reason given'}"}

        usd = float(result.get("estimated_usd") or spec["unit_usd"])
        _spend.record(root, usd, kind="image", work_item_id=work_item_id,
                      logical_name=name,
                      detail=f"workflow run {run_id} node {node_id} "
                             f"({spec['provider']}/{spec['model']})")
        spent += usd
        artifact = _artifacts.register(
            root, name, result.get("path") or str(out_path),
            producer=PRODUCER, model=spec["model"], prompt=prompt,
            refs=list(refs or []), work_item_id=work_item_id,
            metadata={"provider": spec["provider"], "model": spec["model"],
                      "run_id": int(run_id), "node_id": node_id,
                      "candidate": index + 1,
                      "tier": spec["tier"], "task_kind": spec["task_kind"],
                      "seconds": result.get("seconds"),
                      "estimated_usd": usd})
        produced.append({
            "artifact_id": int(artifact["id"]), "revision": artifact.get("revision"),
            "logical_name": name, "path": artifact.get("path"),
            "provider": spec["provider"], "model": spec["model"],
            "seconds": result.get("seconds"), "usd": round(usd, 4),
        })

    if not produced:
        return {"ok": False, "artifacts": [], "usd": 0.0,
                "provider": spec["provider"], "model": spec["model"],
                "error": stopped or "nothing was generated"}

    activity.log(root, "workflow",
                 f"run {run_id} node {node_id} generated {len(produced)} "
                 f"candidate(s) with {spec['provider']}/{spec['model']} "
                 f"(~${spent:.3f})", ref=str(run_id))
    return {"ok": True, "error": "", "artifacts": produced,
            "provider": spec["provider"], "model": spec["model"],
            "count": len(produced), "requested": spec["count"],
            "logical_name": name, "prompt": prompt,
            "usd": round(spent, 4), "stopped": stopped}
