"""Independent art-QA reviewer endpoints.

The 'art' seat produces candidate images; it must NOT self-approve them (agents
judging a frame in isolation have approved off-style drift three times). This
router dispatches an INDEPENDENT 'qa' seat session whose only job is to compare
each candidate against its reference and record a strict pass/fail verdict via
the `art_qa_verdict` MCP tool. The verdict lands in the artifact's
metadata.qa_review so the art workspace can badge each candidate.

Kept in its own module so it auto-registers (routes/__init__.py) without an
edit to app.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from bgate_core import artifacts as _artifacts, queue as _queue
from bgate_ui import dispatch as _dispatch
from bgate_ui.deps import root

router = APIRouter()


def _ref_paths(art: dict) -> list[str]:
    """Reference image paths a candidate was drawn against — prefer the resolved
    absolute paths in metadata, fall back to the raw refs list."""
    meta = art.get("metadata") or {}
    resolved = meta.get("resolved_refs") or []
    out = [r for r in resolved if isinstance(r, str) and r.strip()]
    if not out:
        out = [r for r in (art.get("refs") or []) if isinstance(r, str) and r.strip()]
    return out


def _gather_candidates(r, logical_name: Optional[str],
                       work_item_id: Optional[int]) -> list[dict]:
    """The candidate artifacts to review: by logical_name, else by work item,
    else the newest candidates across the project."""
    if logical_name:
        return _artifacts.list_revisions(r, logical_name=logical_name,
                                         status="candidate", limit=200)
    cands = _artifacts.list_revisions(r, status="candidate", limit=200)
    if work_item_id is not None:
        cands = [c for c in cands if c.get("work_item_id") == work_item_id]
    return cands


def _reviewer_brief(target: str, candidates: list[dict]) -> str:
    """The brief handed to the independent 'qa' reviewer session. Lists the exact
    artifact ids + candidate paths + reference paths so the agent LOOKS at each
    pairing rather than reasoning from memory."""
    lines: list[str] = []
    for c in candidates:
        refs = _ref_paths(c)
        ref_str = "; ".join(refs) if refs else "(no reference on record — flag this)"
        lines.append(
            f"- artifact_id={c['id']}  logical_name={c['logical_name']}  "
            f"revision={c.get('revision')}  model={c.get('model') or 'unknown'}\n"
            f"    candidate_path: {c['path']}\n"
            f"    reference(s):   {ref_str}"
        )
    listing = "\n".join(lines) if lines else "(no candidates were found to review)"
    return (
        "You are an INDEPENDENT art-consistency reviewer — you did NOT make these "
        "images, and you must not trust the art seat's own judgement (it tends to "
        "self-approve drift). Your job is to look at each candidate beside its "
        "reference and decide, strictly, whether it is on-model.\n\n"
        f"REVIEW TARGET: {target}\n\n"
        "CANDIDATES TO REVIEW (exact ids + paths):\n"
        f"{listing}\n\n"
        "For EACH candidate above, in order:\n"
        "1. Call the MCP tool `consistency_check(candidate_path, character)` — it "
        "builds a reference|candidate composite on a checkerboard and returns a "
        "palette-drift number plus the profile checklist. Use the logical_name / "
        "reference to pick the character (e.g. 'tommy' or 'scoville').\n"
        "2. Then use the Read tool to open BOTH the produced image (candidate_path) "
        "AND its reference image(s), and JUDGE BY LOOKING: does the candidate match "
        "the reference's character design, silhouette, palette, and 16-bit "
        "cel-shaded style? Be strict — a lost headband, a painterly/gritty finish, a "
        "drifted palette, bared monster teeth, or a wrong face all mean FAIL.\n"
        "3. Record the verdict by calling the MCP tool `art_qa_verdict("
        "artifact_id=<the id>, verdict='pass'|'fail', score=<0-100>, "
        "reasons='<specific, e.g. lost the headband, style went painterly>')`. "
        "Call it once per candidate. Do NOT approve anything that is off-model.\n\n"
        "When every candidate has a verdict, call `queue_complete` with a one-"
        "paragraph summary (how many passed / failed and the standout problems)."
    )


@router.post("/api/art-qa/review")
def art_qa_review(payload: Optional[dict] = None) -> dict:
    """Dispatch an independent 'qa' reviewer over a set of candidate artifacts.

    body: {logical_name?: str, work_item_id?: int}. Gathers the candidates,
    creates a 'qa' work item carrying the reviewer brief (with the exact ids +
    paths embedded), and dispatches a real Claude session against it.
    """
    payload = payload or {}
    r = root()
    logical_name = (payload.get("logical_name") or "").strip() or None
    work_item_id = payload.get("work_item_id")
    if work_item_id is not None:
        try:
            work_item_id = int(work_item_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "work_item_id must be an integer")

    candidates = _gather_candidates(r, logical_name, work_item_id)
    if not candidates:
        raise HTTPException(404, "no candidate artifacts to review for that target")

    if logical_name:
        target = logical_name
    elif work_item_id is not None:
        target = f"work item #{work_item_id}"
    else:
        target = f"{len(candidates)} newest candidates"

    try:
        item = _queue.add(
            r, "qa",
            title=f"Art-QA: independent consistency review of {target}",
            brief=_reviewer_brief(target, candidates),
            source="art-qa", source_ref=str(target))
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc))

    dispatched = _dispatch.dispatch(str(r), item["id"])
    return {
        "ok": bool(dispatched.get("ok")),
        "review_item_id": item["id"],
        "dispatched": dispatched.get("pid"),
        "candidate_count": len(candidates),
        "candidate_ids": [c["id"] for c in candidates],
        "error": dispatched.get("error"),
    }


@router.get("/api/art-qa/verdicts")
def art_qa_verdicts(logical_name: Optional[str] = None) -> dict:
    """Each candidate's recorded QA verdict (metadata.qa_review), if present.

    Convenience for the art workspace: badge candidates with their independent
    pass/fail without re-deriving it from the full artifact list.
    """
    r = root()
    arts = _artifacts.list_revisions(r, logical_name=logical_name, limit=500)
    verdicts = []
    for a in arts:
        qa = (a.get("metadata") or {}).get("qa_review")
        if qa:
            verdicts.append({
                "artifact_id": a["id"],
                "logical_name": a["logical_name"],
                "revision": a.get("revision"),
                "status": a.get("status"),
                "qa_review": qa,
            })
    return {"verdicts": verdicts}
