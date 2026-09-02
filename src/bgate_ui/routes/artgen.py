"""Painted art over HTTP — the one generator the dashboard could not reach.

Music, sprites, storyboard frames and cinematic shots all had routes. A plain
image — a portrait, a prop, an item, a texture, a title plate — did not: it
existed only as the ``image_generate`` MCP tool, so anything outside an agent
session (a Pulsiron panel, a curl, the dashboard itself) had no way to ask for
one. This is that tool's body as a route, and nothing more: the same
``chroma.generate`` contract, the same artifact registration, so a picture made
here is indistinguishable from a picture an agent made.

GENERATION IS A BACKGROUND JOB, for the reason routes/music.py gives at
length — a provider call runs for tens of seconds and holding a worker open for
it turns a dropped connection into a paid-for image nobody can find. POST
answers 202 with a job id and the caller polls /api/jobs/{id}.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from bgate_core.store import artifacts as _artifacts
from bgate_core.art import chroma as _chroma
from bgate_core.runtime import providers as _providers
from bgate_core.art import refs as _refs
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobs

router = APIRouter()

JOB_KIND = "art_generate"

# WHAT IS BEING MADE, which changes real decisions rather than wording — see
# chroma.needs_key. Offered as a list so a panel or a form can show it without
# hardcoding a vocabulary that lives in the core module.
TASK_KINDS = ("sprite", "anchor", "animation", "item", "prop", "portrait",
              "texture", "tile", "decal", "background", "ui", "concept")

SIZES = ("1024x1024", "1024x1536", "1536x1024")
QUALITIES = ("low", "medium", "high")

# .bgate_out/art/ is where image_generate puts its output and where the gallery
# looks. Kept as one constant because the filename check below has to know it.
ART_DIR = Path(".bgate_out") / "art"


# ONE NAME, NOT A PATH. A caller-supplied filename is joined onto a directory
# and written to, so anything that can express a separator or a parent can
# express somewhere else on the disk. Rather than resolve a path and argue
# about whether it escaped, the name is reduced to its last segment and must
# then match this: letters, digits, dot, dash, underscore, and nothing else.
# "../../.ssh/authorized_keys" survives neither step.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def _out_path(root_dir, filename: str) -> Path:
    """Resolve a caller-supplied filename under the project's art directory.

    CONTAINMENT, not convenience. `filename` arrives from a browser or a panel,
    and this route is reachable without a seat, so the MCP tool's rule is
    restated here — except stricter: the MCP side resolves and contains,
    this side never lets a path exist in the first place.
    """
    name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        raise HTTPException(400, "filename is required (e.g. 'tommy.png')")
    if not name.lower().endswith(".png"):
        name = f"{name}.png"
    if not SAFE_NAME.fullmatch(name):
        raise HTTPException(
            400, f"filename must be a plain name — letters, digits, dot, dash "
                 f"and underscore, at most 120 characters — refusing "
                 f"{filename!r}")
    base = (Path(root_dir) / ART_DIR).resolve()
    out = base / name
    # Belt and braces: the regex already forbids a separator, so this can only
    # fire if the rule above is ever loosened without thinking it through.
    if out.parent != base:
        raise HTTPException(400, f"filename escapes {ART_DIR.as_posix()}")
    base.mkdir(parents=True, exist_ok=True)
    return out


@router.get("/api/art/generate/options")
def art_generate_options() -> dict:
    """What this project can actually be asked for: the configured providers,
    the task kinds, the sizes and the pinned references a caller can condition
    on. A form that hardcodes any of these goes stale the day a key is added."""
    r = root()
    pins = [str(p.get("name") or "") for p in _refs.list_refs(r)]
    return {"ok": True, "providers": _providers.configured(r),
            "task_kinds": list(TASK_KINDS), "sizes": list(SIZES),
            "qualities": list(QUALITIES), "refs": [p for p in pins if p]}


@router.post("/api/art/generate")
def art_generate(payload: dict, request: Request = None) -> dict:
    """Generate one painted image. 202 + job id; poll /api/jobs/{job_id}.

    Everything is validated BEFORE the job exists so a bad request answers now
    rather than becoming a failed job in a panel the user has walked away from.
    """
    r = root()
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    out = _out_path(r, payload.get("filename") or "")
    task_kind = (payload.get("task_kind") or "").strip()
    size = (payload.get("size") or "1024x1024").strip()
    quality = (payload.get("quality") or "medium").strip()
    transparent = bool(payload.get("transparent"))
    tileable = bool(payload.get("tileable"))
    model = (payload.get("model") or "").strip()
    asked = (payload.get("provider") or "").strip()

    # A PINNED NAME, NEVER A PATH. refs.resolve() lets an existing path pass
    # through untouched, which is right for an MCP tool — an agent holds a
    # seat and may legitimately name a file — and wrong here: this route is
    # reachable without one, so a path would condition the generation on any
    # file the server can read and echo it back in resolved_refs. So resolve()
    # is not called at all. A caller's name selects an entry from the
    # project's own pin table and the PIN'S name is what gets looked up; the
    # only thing of the caller's that survives into a path is the choice of
    # which pin, and (as an int) which revision of it.
    named = [str(x).strip() for x in (payload.get("refs") or []) if str(x).strip()]
    pins = {str(p.get("name") or ""): p for p in _refs.list_refs(r)}
    pins.pop("", None)
    resolved: list[str] = []
    for asked_ref in named:
        at = _refs._AT_REVISION.match(asked_ref)
        base = at.group("name") if at else asked_ref
        pin = pins.get(base) or pins.get(_refs.slugify(base))
        if pin is None:
            raise HTTPException(
                400, f"not a pinned reference: {base!r} — pin it with ref_pin "
                     f"first. Pinned: {', '.join(sorted(pins)) or '(none)'}")
        try:
            if at:
                entry = _refs.get_revision(r, str(pin["name"]),
                                           int(at.group("revision")))
            else:
                entry = _refs.get(r, str(pin["name"]))
        except LookupError as exc:
            raise HTTPException(400, f"reference: {exc}") from exc
        resolved.append(str(entry["path"]))

    try:
        provider = _providers.provider_for(task_kind, asked=asked, root=r)
    except Exception as exc:
        # No key configured, or a provider named that this build does not have.
        raise HTTPException(400, str(exc)) from exc

    logical = out.stem

    def _work(_job_id: int) -> dict:
        result = _chroma.generate(
            prompt, str(out), provider=provider, model=model,
            task_kind=task_kind,
            # keyed=None hands the decision to task_kind, exactly as the MCP
            # tool does; True only when the caller explicitly asked for a cut.
            keyed=True if transparent else None,
            size=size, quality=quality, transparent=False,
            ref_paths=resolved, tileable=tileable, root=r,
            logical_name=logical)
        if not result.get("ok"):
            return result
        # WHAT HAPPENED, NOT WHAT WAS ASKED FOR: the mirror pass reports its own
        # verdict and it can fail, so a map that never tiled must not be filed
        # as a tileable map.
        tiled = result.get("tileable")
        tiled_ok = bool(tiled.get("ok")) if isinstance(tiled, dict) else bool(tiled)
        if tileable and not tiled_ok:
            result["warning"] = ("tileable was requested and did not happen — "
                                 "this map will seam where it repeats")
        try:
            result["artifact"] = _artifacts.register(
                r, logical, result["path"], producer="art_generate",
                model=result.get("model", ""), prompt=prompt, refs=named,
                metadata={"size": size, "quality": quality,
                          "task_kind": task_kind, "transparent": transparent,
                          "tileable_requested": tileable, "tileable": tiled_ok,
                          "provider": provider, "resolved_refs": resolved,
                          "keyed": result.get("keyed")})
        except Exception as exc:
            # The picture exists; only the bookkeeping failed. Say so rather
            # than reporting a successful generation as a failure.
            result["artifact_error"] = str(exc)
        return result

    started = _jobs.start(JOB_KIND, _work,
                          request_body={"prompt": prompt, "filename": out.name,
                                        "task_kind": task_kind, "size": size,
                                        "quality": quality,
                                        "provider": provider},
                          request=request)
    return {**started, "provider": provider,
            "path": str(out.relative_to(Path(r)))}


@router.get("/api/art/generate/jobs")
def art_generate_jobs(limit: int = 12) -> dict:
    """The recent art jobs, newest first — what a panel polls to show progress
    without having to hold a job id from a previous session.

    Each row is read back through jobs.get() rather than used raw: list_jobs
    hands back undecoded request_json/result_json, which is how the music panel
    shipped with an empty prompt on every card.
    """
    from bgate_core.board import jobs as _core_jobs
    project = root()
    rows = _core_jobs.list_jobs(project, kind=JOB_KIND,
                                limit=max(1, min(int(limit), 50)))
    out = []
    for row in rows:
        job = _core_jobs.get(project, int(row["id"])) or row
        view = _jobs.view(project, job)
        request = view.get("request") or {}
        view["prompt"] = str(request.get("prompt") or "")
        view["filename"] = str(request.get("filename") or "")
        result = view.get("result") or {}
        view["path"] = str(result.get("path") or "")
        out.append(view)
    return api.ok({"jobs": out, "kind": JOB_KIND})
