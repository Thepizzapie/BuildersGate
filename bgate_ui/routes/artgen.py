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

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from bgate_core import artifacts as _artifacts
from bgate_core import chroma as _chroma
from bgate_core import providers as _providers
from bgate_core import refs as _refs
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


def _out_path(root_dir, filename: str) -> Path:
    """Resolve a caller-supplied filename under the project's art directory.

    CONTAINMENT, not convenience. `filename` arrives from a browser or a panel,
    so `../../` in it would write outside the project entirely. The resolved
    path must still be inside .bgate_out/art or this refuses — the same rule
    the MCP tool's _art_out applies, restated here because this route is
    reachable without a seat.
    """
    name = (filename or "").strip().replace("\\", "/")
    if not name:
        raise HTTPException(400, "filename is required (e.g. 'tommy.png')")
    if not name.lower().endswith(".png"):
        name = f"{name}.png"
    base = (Path(root_dir) / ART_DIR).resolve()
    out = (base / name).resolve()
    if base != out.parent and base not in out.parents:
        raise HTTPException(400, f"filename escapes {ART_DIR.as_posix()}: {filename!r}")
    out.parent.mkdir(parents=True, exist_ok=True)
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

    named = [str(x).strip() for x in (payload.get("refs") or []) if str(x).strip()]
    try:
        resolved = [_refs.resolve(r, name) for name in named]
    except Exception as exc:
        raise HTTPException(400, f"reference: {exc}") from exc

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
    from bgate_core import jobs as _core_jobs
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
