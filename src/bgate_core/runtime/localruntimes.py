"""Every generator that runs on the user's own machine, declared once, in data.

WHY THIS EXISTS. ``bgate_core.runtime.providers`` gave the hosted providers a real
setup surface: one entry per provider, a panel that renders it, a doctor row, a
single sentence about where the value is stored. The local path — which is the
one this product's whole thesis rests on, because it is the path that needs no
key and sends nothing anywhere — had none of that. The adapters were finished
and reachable ONLY through environment variables the user had to learn about by
reading source. That is the papercut: not that local generation is missing, but
that there is nowhere to set it up or see whether it worked.

WHY THIS IS NOT :class:`bgate_core.runtime.providers.Provider`, AND THE TWO SIT SIDE BY
SIDE ANYWAY. A provider models exactly one question — *is there a key* — and
everything on it follows from that: ``env`` is singular, ``key_url`` is where
you buy one, ``status`` reports presence and a last-4 fingerprint, and the whole
module exists to make sure the value never comes back out. A local runtime asks
a different question, *is the thing running and pointed at the right files*, and
it has:

  * SEVERAL config values, not one — an address, one or two workflow files, a
    declared model — each of which is separately settable and separately wrong;
  * a value that is safe to display, which inverts the single strongest rule in
    ``providers``: the fingerprint exists there precisely because the value must
    never be shown, and here showing the value IS the feature (a path you cannot
    read is a path you cannot check);
  * more than two states. A key is set or not. A runtime is not configured, or
    configured and nothing is listening, or listening and misconfigured, or
    ready — and collapsing those to a lamp is what made this opaque in the first
    place;
  * no account, no price, no billing, and nothing to buy.

Forcing that into ``Provider`` would mean a ``Provider`` with a list of envs, a
value-returning status, four states and a dead ``key_url``, which is a different
dataclass wearing the old one's name. So: a sibling registry, sharing the one
thing that genuinely is shared — ``providers.CAPABILITIES``, the vocabulary for
what a thing can make. That shared vocabulary is what lets the dashboard put a
hosted card and a local card in the same "2D images" section and have the
grouping mean something.

WHAT IS DELIBERATELY NOT HERE: STARTING AND STOPPING. An earlier cut of this
had the dashboard spawn and supervise the user's ComfyUI. It should not, and the
reasons are worth writing down so it is not re-added:

  * the command is unknowable — a conda env, a portable build, a .bat with a
    dozen flags, a machine on the LAN — so the dashboard would be executing a
    string it was handed and could not validate;
  * every interesting failure is on the far side of that string (the GPU is
    already claimed, the port is in use, the wrong environment is active, a 12 GB
    checkpoint is still loading) and none of them are reportable from here;
  * "Builders Gate started my ComfyUI and now something is holding 8 GB of VRAM
    that I cannot find" is a far worse outcome than "Builders Gate told me to
    start it".

So the loop is: CONFIGURE HERE → START IT YOURSELF → THIS NOTICES. The noticing
is the part that has to be good, which is why :func:`status` probes and why
every failure carries the adapter's own verbatim sentence rather than a lamp.

WHERE THE CONFIG LIVES: the project's ``.env``, written through
``bgate_core.store.envfile``, exactly like a provider key and for a reason that is not
symmetry.

  * These ARE the variables the adapters read. ``localgen.workflow_path`` reads
    ``os.environ``; ``imageto3d.base_url`` reads ``os.environ``. Any other store
    would need a shim in every adapter, and then there would be two answers to
    "what is BGATE_COMFY_URL set to".
  * A second process must see the same answer. ``bgate doctor`` and the MCP
    server are not this process, and the ``.env`` is the only store both of them
    already load.
  * Not the settings registry, even though it is the obvious neighbour.
    ``settings.describe()`` returns every field's value verbatim, which is FINE
    for a filesystem path — a path is not a secret and this module says so
    deliberately rather than by omission. The reason is different: in that
    registry ``env`` means "an environment variable OVERRIDES the stored value",
    whereas here the environment variable IS the value the adapter reads. A
    field there would create two sources of truth for one variable and a
    precedence rule for a case that has none. It is also a fixed field list, and
    this one grows with ``imageto3d.BACKENDS``.

ADDING A LOCAL RUNTIME IS ONE ENTRY. The 3D ones are not even that — they are
generated from ``imageto3d.BACKENDS``' own ``kind == "local"`` rows, so a
backend added there appears here with no edit at all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field as _field
from pathlib import Path
from typing import Any, Callable, Optional

from . import comfyui
from ..store import envfile
from bgate_core.runtime.providers import CAPABILITIES

# The four states, in the order a user walks through them. Kept as data because
# three surfaces render them (Settings, Studio, doctor) and a fourth spelling of
# "configured but not running" would be a fourth thing to keep in step.
STAGES: dict[str, str] = {
    "unconfigured": "not set up",
    # Only ever reported by an UNPROBED read. Saying "not running" without
    # having asked would be the panel guessing, which is the thing this surface
    # exists to stop.
    "configured": "set up, not checked",
    "unreachable": "set up, not running",
    "unhealthy": "running, but something is wrong",
    "ready": "ready",
    # NOT A FAILURE OF THE USER'S SETUP, and kept apart from "unhealthy" for
    # that reason: a backend documented here whose transport this product has
    # not wired cannot be fixed by configuring anything, so a card that offered
    # fields for it would be asking for work that changes nothing.
    "unavailable": "not wired in this build",
}
STAGE_TONE = {"unconfigured": "off", "configured": "off", "unreachable": "warn",
              "unhealthy": "warn", "ready": "good", "unavailable": "off"}


class LocalConfigError(ValueError):
    """A refusal with a sentence worth showing the human."""


# ---------------------------------------------------------------------------
# What one configurable value looks like
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """One environment variable, described the way the settings registry
    describes a switch: what it is FOR, not what the word means.

    ``kind`` decides the control, not the validation — ``path`` gets a wide
    monospace input, ``choice`` gets a select, ``catalogue`` gets a select
    populated from the running server (and degrades to a text input when
    nothing is answering, which is most of the time).
    """

    env: str
    label: str
    help: str
    kind: str = "text"            # text | url | path | choice | catalogue
    placeholder: str = ""
    default: str = ""
    required: bool = False
    choices: tuple[str, ...] = ()
    # For kind == "catalogue": which group of comfyui.CATALOGUE fills the list.
    source: str = ""


@dataclass(frozen=True)
class Runtime:
    """One thing the user runs on their own machine that this product can use."""

    id: str
    label: str
    powers: tuple[str, ...]
    # One paragraph, in the user's terms, answering "what IS this". The single
    # most requested thing about this surface.
    what: str
    fields: tuple[Field, ...]
    url_env: str
    url_default: str
    # How to start it, as lines a human can follow. NOT a command this product
    # runs — see the module docstring.
    start: tuple[str, ...]
    docs_url: str = ""
    # "comfy" means the introspection in bgate_core.runtime.comfyui applies.
    software: str = ""
    # (available, alive) — both borrowed from the adapter that actually runs.
    probe: Callable[[Any], dict] = _field(repr=False, default=lambda root: {})
    alive: Callable[[], dict] = _field(repr=False, default=lambda: {"ok": False})
    note: str = ""
    implemented: bool = True


# ---------------------------------------------------------------------------
# The 2D runtime — one entry, because there is one local 2D path
# ---------------------------------------------------------------------------

def _localgen():
    from bgate_adapters import localgen
    return localgen


def _i3d():
    from bgate_adapters import imageto3d
    return imageto3d


COMFY_START = (
    "Start ComfyUI the way you normally do — the desktop app, or "
    "`python main.py` in your ComfyUI folder.",
    "Leave it running. Builders Gate talks to it over HTTP; it does not "
    "start it, stop it or hold on to it.",
    "This page notices on its own within a few seconds of it coming up — "
    "there is nothing to press.",
)


def _image_fields() -> tuple[Field, ...]:
    lg = _localgen()
    return (
        Field(env="BGATE_COMFY_URL",
              label="Address",
              kind="url",
              default="http://127.0.0.1:8188",
              placeholder="http://127.0.0.1:8188",
              help="Where your ComfyUI is listening. The default is what "
                   "ComfyUI uses out of the box on this machine; change it only "
                   "if you moved the port or it runs on another box on your "
                   "network."),
        Field(env=lg.TXT2IMG_ENV,
              label="Text-to-image workflow",
              kind="path", required=True,
              placeholder=r"C:\Users\you\workflows\bgate-t2i.json",
              help="A ComfyUI graph, exported to a file, that makes an image "
                   "from a text prompt. This is the one that runs when "
                   "something asks for new art from nothing. Builders Gate "
                   "cannot invent this graph — which nodes exist, which "
                   "checkpoint is loaded and which sampler runs are all facts "
                   "about YOUR install — so it runs the one you built. Export "
                   "it with Workflow → Export (API), not the plain Save."),
        Field(env=lg.EDIT_ENV,
              label="Reference / edit workflow",
              kind="path",
              placeholder=r"C:\Users\you\workflows\bgate-edit.json",
              help="A second graph, for when there IS a starting image — a "
                   "pinned character reference, a frame to restyle. This is how "
                   "a character keeps the same face across pictures. Two graphs "
                   "rather than one because ComfyUI genuinely needs two: an "
                   "img2img / IP-Adapter / Qwen-Image-Edit graph has different "
                   "nodes from a text-to-image one. Optional — without it, "
                   "reference-guided local generation is simply unavailable and "
                   "plain text-to-image still works."),
        Field(env=lg.MODEL_ENV,
              label="Which model your graph loads",
              kind="choice",
              choices=tuple(sorted(lg.MODEL_LICENCES)),
              help="Not a setting that changes anything — a declaration. Your "
                   "graph decides which weights run and nothing here can read "
                   "that off the graph, so tell it once and it will tell you "
                   "what that model's licence permits. This matters "
                   "commercially: FLUX.1 [dev] output cannot go in a game you "
                   "sell, SDXL's can, and finding that out after you ship is "
                   "the expensive order to find it out in."),
    )


def _image_runtime() -> Runtime:
    lg = _localgen()
    return Runtime(
        id="comfy-image",
        label="ComfyUI — 2D images",
        powers=("image_2d",),
        software="comfy",
        url_env="BGATE_COMFY_URL",
        url_default="http://127.0.0.1:8188",
        what="ComfyUI is an image generator you run yourself, on your own GPU. "
             "You build a pipeline in it once — load a model, encode a prompt, "
             "sample, save — and Builders Gate sends prompts through that same "
             "pipeline. Nothing leaves this machine and nothing is billed. The "
             "trade is that the pipeline is yours to build: this product cannot "
             "guess which models you downloaded.",
        fields=_image_fields(),
        start=COMFY_START,
        docs_url="https://docs.comfy.org/",
        probe=lambda root: dict(lg.available(probe=False)),
        alive=lambda: dict(_i3d()._alive(lg.BACKEND)),
        note="Local art is free and unlimited, and it is also the only art path "
             "that works with no API key at all.",
    )


# ---------------------------------------------------------------------------
# The 3D runtimes — generated, so imageto3d stays the single source of truth
# ---------------------------------------------------------------------------

# What each local 3D backend IS, in a sentence, keyed by backend id. Only the
# prose lives here; every fact (address, workflow variable, whether it is even
# wired) is read off imageto3d.BACKENDS at call time, so a backend added there
# shows up here with the generic sentence rather than not showing up at all.
_3D_WHAT: dict[str, str] = {
    "comfy": "The same ComfyUI you use for images, with a 3D node pack "
             "installed. It turns one picture into a mesh. Same address, "
             "different graph.",
    "comfy-parts": "ComfyUI again, running a part-aware image-to-3D graph — one "
                   "that produces a mesh split into separate pieces (head, "
                   "arms, a weapon) instead of one fused blob, which is what "
                   "makes it riggable afterwards.",
    "trellis-cpp": "A single prebuilt Windows server that does image-to-3D and "
                   "nothing else. No Python environment to assemble; you "
                   "download it and run the .exe.",
    "hunyuan-local": "Tencent's Hunyuan3D, self-hosted — you clone the repo and "
                     "run its own api_server.py. The most capable of the local "
                     "options and the heaviest to install.",
    "gradio-local": "Any local Gradio demo app (TRELLIS, SF3D, TripoSR) that "
                    "exposes an image-to-3D tab.",
}

_3D_START: dict[str, tuple[str, ...]] = {
    "trellis-cpp": (
        "Download the prebuilt Windows release and unzip it somewhere.",
        "Run its server executable. It prints the address it is listening on.",
        "Put that address in the field above if it is not the default.",
    ),
    "hunyuan-local": (
        "In your Hunyuan3D checkout, run `python api_server.py`.",
        "It takes a while on the first run — the weights download then.",
        "Leave it running; this page notices when it answers.",
    ),
}


def _threed_runtimes() -> tuple[Runtime, ...]:
    i3d = _i3d()
    out: list[Runtime] = []
    for backend in i3d.LOCAL:
        spec = i3d.BACKENDS.get(backend) or {}
        base_env = spec.get("base_env") or ""
        wf_env = spec.get("workflow_env") or ""
        is_comfy = base_env == "BGATE_COMFY_URL"
        fields: list[Field] = [
            Field(env=base_env,
                  label="Address",
                  kind="url",
                  default=str(spec.get("base") or ""),
                  placeholder=str(spec.get("base") or ""),
                  help=("Where this server is listening. Shared with the 2D "
                        "ComfyUI above — it is the same program, so changing "
                        "it here changes it there."
                        if is_comfy else
                        "Where this server is listening. The default is what "
                        "it uses out of the box.")),
        ]
        if wf_env:
            fields.append(Field(
                env=wf_env,
                label="Image-to-3D workflow",
                kind="path", required=True,
                placeholder=r"C:\Users\you\workflows\bgate-3d.json",
                help="A ComfyUI graph, exported with Workflow → Export (API), "
                     "that takes one image and writes a mesh. Same reasoning as "
                     "the 2D workflows: the node names come from whichever 3D "
                     "node pack you installed, so the graph has to be yours."))
        vram = spec.get("vram_gb")
        note = str(spec.get("note") or "")
        if vram:
            note = (f"Needs about {vram} GB of VRAM according to its own docs. "
                    + note).strip()
        out.append(Runtime(
            id=backend,
            label=str(spec.get("label") or backend),
            powers=("model_3d",),
            software="comfy" if is_comfy else "",
            url_env=base_env,
            url_default=str(spec.get("base") or ""),
            what=_3D_WHAT.get(backend,
                              "A local image-to-3D server. Builders Gate posts "
                              "a picture at it and gets a mesh back."),
            fields=tuple(fields),
            start=_3D_START.get(
                backend,
                COMFY_START if is_comfy else (
                    "Start this server however its own instructions say.",
                    "Make sure the address above matches where it says it is "
                    "listening.",
                    "This page notices on its own once it answers.")),
            docs_url=str(spec.get("weights") or "") if str(
                spec.get("weights") or "").startswith("http") else "",
            probe=(lambda b: (lambda root: dict(
                _i3d().available(b, root, probe=False))))(backend),
            alive=(lambda b: (lambda: dict(_i3d()._alive(b))))(backend),
            note=note,
            implemented=bool(spec.get("implemented", True)),
        ))
    return tuple(out)


def runtimes() -> tuple[Runtime, ...]:
    """Every local runtime. Built on call, not at import, so the 3D half stays
    generated from ``imageto3d.BACKENDS`` and an adapter that will not import
    (a broken custom build, a partial install) costs one runtime rather than
    the whole panel."""
    out: list[Runtime] = []
    try:
        out.append(_image_runtime())
    except Exception:                                            # noqa: BLE001
        pass
    try:
        out.extend(_threed_runtimes())
    except Exception:                                            # noqa: BLE001
        pass
    return tuple(out)


def by_id(runtime_id: str) -> Runtime:
    wanted = (runtime_id or "").strip()
    for one in runtimes():
        if one.id == wanted:
            return one
    raise LocalConfigError(
        f"unknown local runtime '{runtime_id}' — known: "
        + ", ".join(r.id for r in runtimes()))


def ids() -> tuple[str, ...]:
    return tuple(r.id for r in runtimes())


# ---------------------------------------------------------------------------
# Reading the configuration
# ---------------------------------------------------------------------------

def _value(one: Field, from_file: dict) -> dict:
    """One field's value AND which layer supplied it.

    The same three-layer question ``providers._one_status`` asks, and it matters
    here for the same reason: ``load_project_env`` lets a shell variable beat the
    file, so a panel reading ``os.environ`` alone would show a path the user just
    saved while a stale ``set BGATE_COMFY_URL=`` in their profile is what the
    adapter actually uses.

    UNLIKE A KEY, THE VALUE IS RETURNED. A filesystem path is not a secret, and
    a path you cannot read back is a path you cannot check for the typo that is
    the whole reason you came to this page.
    """
    live = (os.environ.get(one.env) or "").strip()
    stored = (from_file.get(one.env) or "").strip()
    if live and stored and live == stored:
        source = "env_file"
    elif live:
        source = "environment"
    elif stored:
        source = "shadowed"
    else:
        source = "unset"
    effective = live or (one.default if source == "unset" else "")
    row = {
        "env": one.env,
        "label": one.label,
        "help": one.help,
        "kind": one.kind,
        "placeholder": one.placeholder or one.default,
        "default": one.default,
        "required": one.required,
        "choices": list(one.choices),
        "source": source,
        "value": live,
        "effective": effective,
        "using_default": source == "unset" and bool(one.default),
        "exists": None,
    }
    if one.kind == "path" and live:
        row["exists"] = Path(live).is_file()
    return row


def _stage(one: Runtime, rows: list[dict], root, *, probe: bool) -> dict:
    """Which of the four states this runtime is in, and the verbatim reason.

    THE ORDER IS THE ORDER A USER HITS THEM, and each step defers to the thing
    that actually knows: the required fields are ours, liveness is
    ``imageto3d._alive``, and every other verdict is the adapter's own
    ``available()`` sentence copied out unchanged. A reason rewritten here would
    be a second opinion that disagrees with the tool that runs.
    """
    if not one.implemented:
        return {"stage": "unavailable",
                "reason": (one.note or "this backend is documented as an "
                           "alternative but its transport is not wired yet")}

    missing = [r["label"] for r in rows if r["required"] and not r["effective"]]
    if missing:
        return {"stage": "unconfigured",
                "reason": "still needs " + " and ".join(missing).lower()}

    if not probe:
        # Config-level verdict only. Cheap enough for a list, and honest about
        # what it did not check.
        try:
            verdict = one.probe(root)
        except Exception as exc:                                 # noqa: BLE001
            return {"stage": "unhealthy",
                    "reason": f"{type(exc).__name__}: {exc}"}
        if not verdict.get("available"):
            return {"stage": "unhealthy",
                    "reason": str(verdict.get("reason") or "")}
        return {"stage": "configured", "reason": "",
                "checked": "configuration only — not asked whether it is running"}

    try:
        alive = one.alive()
    except Exception as exc:                                     # noqa: BLE001
        alive = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not alive.get("ok"):
        return {"stage": "unreachable",
                "reason": str(alive.get("reason") or "nothing answered")}

    try:
        verdict = one.probe(root)
    except Exception as exc:                                     # noqa: BLE001
        return {"stage": "unhealthy", "reason": f"{type(exc).__name__}: {exc}"}
    if not verdict.get("available"):
        return {"stage": "unhealthy", "reason": str(verdict.get("reason") or "")}
    return {"stage": "ready", "reason": "", "note": str(alive.get("note") or "")}


def _one_status(one: Runtime, root, from_file: dict, *, probe: bool) -> dict:
    rows = [_value(f, from_file) for f in one.fields]
    url = ""
    for row in rows:
        if row["env"] == one.url_env:
            url = row["effective"] or one.url_default
    staged = _stage(one, rows, root, probe=probe)
    return {
        "id": one.id,
        "label": one.label,
        "powers": list(one.powers),
        "power_labels": [CAPABILITIES.get(p, p) for p in one.powers],
        "what": one.what,
        "note": one.note,
        "software": one.software,
        "docs_url": one.docs_url,
        "url": url or one.url_default,
        "start": list(one.start),
        "fields": rows,
        "implemented": one.implemented,
        "probed": bool(probe),
        "usd": 0.0,
        **staged,
        "stage_label": STAGES.get(staged["stage"], staged["stage"]),
        "tone": STAGE_TONE.get(staged["stage"], "off"),
        "available": staged["stage"] == "ready",
    }


def status(root: Optional[str | os.PathLike[str]] = None, *,
           probe: bool = True) -> list[dict]:
    """Every local runtime: what it is, how it is configured, and its stage.

    ``probe`` costs one short GET per distinct address and is on by default,
    because the entire point of this surface is that "start it yourself and the
    dashboard notices" works without pressing anything. Callers painting a dense
    list that already knows nothing is configured can pass False.
    """
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:                                        # noqa: BLE001
            pass
    from_file = envfile.file_vars(root) if root else {}
    return [_one_status(one, root, from_file, probe=probe) for one in runtimes()]


def status_for(root, runtime_id: str, *, probe: bool = True) -> dict:
    one = by_id(runtime_id)
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:                                        # noqa: BLE001
            pass
    return _one_status(one, root, envfile.file_vars(root) if root else {},
                       probe=probe)


def ready(root: Optional[str | os.PathLike[str]] = None) -> list[str]:
    """The ids that could generate right now."""
    return [row["id"] for row in status(root) if row["available"]]


# ---------------------------------------------------------------------------
# Writing the configuration
# ---------------------------------------------------------------------------

def _field_of(one: Runtime, env: str) -> Field:
    for candidate in one.fields:
        if candidate.env == env:
            return candidate
    raise LocalConfigError(
        f"{one.label} has no setting called '{env}' — it takes "
        + ", ".join(f.env for f in one.fields))


def set_field(root: str | os.PathLike[str], runtime_id: str, env: str,
              value: str, *, actor: str = "") -> dict:
    """Store one local-runtime value in the project's .env and make it live NOW.

    The in-process assignment is the same non-obvious step ``providers.set_key``
    documents: ``load_project_env`` refuses to overwrite a name already in
    ``os.environ``, so after the first save the file can never again update the
    running value. Without it the user sets a path, nothing changes, and they
    conclude the page is broken.

    Paths are allowed to contain spaces (see ``envfile.write_var``) because on
    the supported platform they routinely do.
    """
    one = by_id(runtime_id)
    spec = _field_of(one, env)
    value = (value or "").strip().strip('"').strip("'")
    if not value:
        return clear_field(root, runtime_id, env, actor=actor)

    if spec.kind == "url":
        low = value.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            raise LocalConfigError(
                f"an address has to start with http:// or https:// — "
                f"'{value}' does not. The usual answer is "
                f"{spec.default or 'http://127.0.0.1:8188'}")
    if spec.kind == "choice" and spec.choices and value not in spec.choices:
        raise LocalConfigError(
            f"{spec.label} must be one of {', '.join(spec.choices)}")

    try:
        action = envfile.write_var(root, env, value, allow_spaces=True)
    except envfile.EnvWriteError as exc:
        raise LocalConfigError(str(exc)) from None

    envfile.reset_cache()
    os.environ[env] = value
    _note(root, f"{one.label}: {spec.label} set", ref=env, actor=actor)
    row = status_for(root, runtime_id)
    row["write"] = action
    return row


def clear_field(root: str | os.PathLike[str], runtime_id: str, env: str, *,
                actor: str = "") -> dict:
    """Forget one value — out of the .env and out of this process."""
    one = by_id(runtime_id)
    spec = _field_of(one, env)
    removed = envfile.remove_var(root, env)
    envfile.reset_cache()
    os.environ.pop(env, None)
    if removed:
        _note(root, f"{one.label}: {spec.label} cleared", ref=env, actor=actor)
    row = status_for(root, runtime_id)
    row["write"] = "removed" if removed else "absent"
    return row


def _note(root, summary: str, *, ref: str, actor: str) -> None:
    """Best effort — the write already landed by the time we are here."""
    try:
        from ..board import activity
        activity.log(root, "settings", summary, ref=ref, actor=actor or "")
    except Exception:                                            # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The deep read: what a running ComfyUI can tell us about itself
# ---------------------------------------------------------------------------

def _tokens_for(one: Runtime, kind: str) -> dict[str, str]:
    """``{meaning: placeholder}`` for one workflow, from the adapter's own
    constants so this module cannot invent a token the substituter does not
    honour."""
    if one.software != "comfy":
        return {}
    if one.powers == ("image_2d",):
        lg = _localgen()
        base = {"prompt": lg.PROMPT_TOKEN, "negative": lg.NEGATIVE_TOKEN,
                "seed": lg.SEED_TOKEN, "width": lg.WIDTH_TOKEN,
                "height": lg.HEIGHT_TOKEN}
        if kind == "edit":
            base["image"] = lg.IMAGE_TOKEN
        return base
    i3d = _i3d()
    return {name: token for name, token in (
        ("image", getattr(i3d, "COMFY_IMAGE_TOKEN", "")),
        ("seed", getattr(i3d, "COMFY_SEED_TOKEN", "")),
    ) if token}


def workflows(root, runtime_id: str) -> list[dict]:
    """Every workflow this runtime is pointed at, read and explained.

    This is the answer to "what is actually in that file, and which parts of it
    does Builders Gate overwrite" — a question that previously had no answer
    short of reading the adapter's source.
    """
    one = by_id(runtime_id)
    if root:
        try:
            envfile.load_project_env(root)
        except Exception:                                        # noqa: BLE001
            pass
    out = []
    for spec in one.fields:
        if spec.kind != "path":
            continue
        kind = "edit" if "EDIT" in spec.env.upper() else "generate"
        path = (os.environ.get(spec.env) or "").strip()
        described = comfyui.describe_workflow(path, _tokens_for(one, kind))
        described.update(env=spec.env, label=spec.label, kind=kind,
                         required=spec.required,
                         runs_when=("when something asks for art from a "
                                    "reference image" if kind == "edit"
                                    else "when something asks for art from a "
                                         "prompt alone"))
        out.append(described)
    return out


def inspect(root, runtime_id: str) -> dict:
    """Everything a running ComfyUI will tell us, plus the workflow reading.

    Every section is independently guarded: a build that does not serve
    ``/object_info`` still gets its devices, its queue and its workflows.
    """
    row = status_for(root, runtime_id)
    out: dict[str, Any] = {"runtime": row, "workflows": workflows(root, runtime_id)}
    if row.get("software") != "comfy":
        out["server"] = {"ok": False,
                         "error": "this runtime is not a ComfyUI, so there is "
                                  "nothing here to introspect"}
        return out
    base = row.get("url") or ""
    if not base:
        out["server"] = {"ok": False, "error": "no address configured"}
        return out
    out["server"] = comfyui.system_stats(base)
    # THE STATS READ IS THE GATE FOR THE REST. Without it, an install that is
    # simply not running got four identical timeouts and a panel reporting
    # "this build did not answer the node query", which blames a version
    # difference for a server that is switched off — the exact kind of wrong
    # sentence this surface exists to stop.
    if out["server"].get("ok"):
        out["catalogue"] = comfyui.catalogue(base)
        out["queue"] = comfyui.queue(base)
        out["history"] = comfyui.history(base)
    if row.get("powers") == ["image_2d"]:
        try:
            lg = _localgen()
            out["licence"] = dict(lg.model_licence())
        except Exception:                                        # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def summary(rows: list[dict]) -> dict:
    """One sentence about the whole local fleet — for doctor AND for the panel.

    SHARED SO THE TWO CANNOT DRIFT. ``bgate doctor`` printing "3 not set up; 2
    set up, not running" while the pane says "0 of 5 ready" is two phrasings of
    one fact, and a user reading both has to work out whether they disagree.
    They are now the same string, built here, once.

    The count is staged rather than a yes/no because "nothing configured at all"
    and "configured, nothing running" are different problems with different
    fixes and a single bit cannot tell them apart.
    """
    counts: dict[str, int] = {}
    for row in rows:
        # A backend this build cannot talk to is not a setup problem and does
        # not belong in a count the user is expected to act on.
        if row["stage"] == "unavailable":
            continue
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    live = [r["label"] for r in rows if r["available"]]
    if live:
        return {"available": True, "detail": ", ".join(live) + " ready"}
    ordered = [f"{n} {STAGES.get(stage, stage)}"
               for stage, n in sorted(counts.items()) if n]
    return {
        "available": False,
        "detail": ("no local generator is ready (" + "; ".join(ordered)
                   + ") — hosted providers are unaffected"
                   if ordered else "no local runtimes are registered"),
    }


def doctor_row(root: Optional[str | os.PathLike[str]] = None) -> dict:
    """One row for ``bgate doctor``, in the optional-capability sense.

    Red here means LOCAL generation is unavailable and every hosted path still
    works — the same status ffmpeg and whisper have.
    """
    try:
        rows = status(root, probe=True)
    except Exception as exc:                                     # noqa: BLE001
        return {"name": "local_runtimes", "available": False, "optional": True,
                "detail": f"registry unavailable: {type(exc).__name__}: {exc}"}
    return {"name": "local_runtimes", "optional": True, **summary(rows)}
