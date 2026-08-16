"""Immutable generated-artifact revisions and their review state.

The asset registry answers "did this path drift?" Artifact revisions answer the
iteration questions: what produced this candidate, what was approved, and which
older candidate it superseded. Paths may be replaced; revision rows never are.

Approval is the one decision in this pipeline that a model may not make. An
agent can generate, judge, annotate and reject; promoting a candidate to canon
takes a human, and the human's identity is stamped on the row. review() is
where that is enforced — not in any one caller, because the audit's finding was
precisely that a second caller walked around the first one's gate.

Approving also has to MOVE A FILE. Every generation overwrites the same stable
sheet path, so the bytes at ``path`` are whatever was rendered last — which,
after a rejected r3, is the rejected art the game keeps loading. Flipping a
status column while the build still loads r3 is a gate that does not gate, so
review() reinstalls the approved revision's archived render over the live path
and reports the transition as ``integrated``. See :func:`_promote`.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from . import activity, assets, db, iterations
from .util import rows

STATUSES = ("candidate", "approved", "rejected", "integrated", "superseded")

# Statuses that put a candidate into the build. Only a human may set these.
HUMAN_ONLY_STATUSES = ("approved", "integrated")


def register(root: str | os.PathLike[str], logical_name: str,
             path: str | os.PathLike[str], *, producer: str = "",
             model: str = "", prompt: str = "", refs: Optional[list[str]] = None,
             metadata: Optional[dict] = None,
             work_item_id: Optional[int] = None) -> dict:
    """Record a new immutable candidate revision for an existing output file."""
    name = logical_name.strip()
    if not name:
        raise ValueError("an artifact needs a logical name")
    rel = assets.normalize_path(root, path)
    absolute = Path(root) / rel
    if not absolute.is_file():
        raise FileNotFoundError(f"nothing on disk at {rel}")

    tracked = assets.track(root, rel)
    digest = tracked["hash"]
    size = tracked["bytes"]
    iteration_id = iterations.active_id(root)
    # Freeze WHICH revision of each pinned reference this was drawn against.
    # Pins are versioned now; without the hash, a re-pin silently rewrites the
    # history of every artifact that claims to have been generated against it.
    metadata = dict(metadata or {})
    pins = _pin_snapshot(root, refs or [])
    if pins:
        metadata.setdefault("ref_pins", pins)
    with db.tx(root) as conn:
        revision = int(conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM artifact_revision "
            "WHERE logical_name = ?", (name,)).fetchone()[0])
        cur = conn.execute(
            """
            INSERT INTO artifact_revision (
                logical_name, revision, path, kind, hash, bytes, producer,
                model, prompt, refs_json, metadata_json, work_item_id, iteration_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, revision, rel, assets.kind_of(rel), digest, size,
             producer.strip(), model.strip(), prompt,
             json.dumps(refs or []), json.dumps(metadata),
             work_item_id, iteration_id),
        )
        artifact_id = int(cur.lastrowid)
    activity.log(root, "artifact",
                 f"candidate {name} r{revision} ({producer or 'unknown'})",
                 ref=str(artifact_id))
    if iteration_id:
        iterations.add_event(
            root, iteration_id, "asset_revision", "artifact", str(artifact_id),
            f"Created {name} r{revision}", {"path": rel, "producer": producer})

    # THE DECISION IS NOW PENDING, AND THE HEARTBEAT HAS TO SAY SO.
    #
    # notify.jsonl carried work-item status transitions and nothing else, so a
    # batch of candidates waiting on a human produced ZERO lines while the
    # dashboard drew an approval card for each one. Measured: five species
    # candidates generated inside two minutes, not one line on the stream. A
    # human decision nobody can see is the same failure as a dead agent — silence
    # that is indistinguishable from an agent still working — arriving through a
    # different door, and it is worse here because the thing on the other side of
    # the silence is a person who does not know they are being waited on.
    #
    # Emitted BEFORE the auto-approve below so the ordering on the stream is the
    # ordering of what happened: the candidate existed, then something cleared
    # it. A consumer that sees only the pair learns the gate was off; one that
    # sees the first line alone knows a human is owed an answer.
    _announce_candidate(root, artifact_id, name, revision, rel, work_item_id)

    # AUTO-APPROVE AT REGISTRATION, when the project has asked for it.
    #
    # Gating `review()` alone was not enough and the reason is worth stating: an
    # agent never CALLS review() — it registers, and a registration lands as a
    # candidate. So a project with art.auto_approve on still queued an approval
    # card for every render, which is the exact complaint the switch exists to
    # answer. The wall was removed and nothing walked through it.
    #
    # Approving here rather than leaving it to a later sweep keeps the promise the
    # setting makes: no card, and the live file IS this revision (review() calls
    # _promote, which reinstalls the archived render over the stable path).
    if _auto_approve(root):
        try:
            return review(root, artifact_id, "approved",
                          note="auto-approved: art.auto_approve is on for this "
                               "project, so no human gate was applied",
                          actor="setting:art.auto_approve")
        except Exception as exc:
            # A failed auto-approval must not lose the registration — the
            # revision row already exists and is the thing that matters.
            activity.log(root, "artifact",
                         f"auto-approve failed for {name} r{revision}: "
                         f"{type(exc).__name__}: {exc}", ref=str(artifact_id))
    return get(root, artifact_id)


def _notify_line(root: str | os.PathLike[str], line: dict) -> None:
    """Append one JSON line to ``.bgate/notify.jsonl``. Never raises.

    THE SAME FILE queue._notify writes, deliberately, and in the same shape:
    ``{ts, item_id, status, seat, title}`` plus a ``kind``. An orchestrator
    already tails this one file — telling it to also tail a second one, with a
    second schema, to learn about the other half of the gates is how a heartbeat
    stops being a heartbeat.

    ``kind`` is additive: every line queue writes is a work-item transition and
    now says so, and a consumer that only reads ``status`` sees exactly what it
    saw before. Losing a ping must never cost the registration that caused it.
    """
    try:
        import json as _json
        from datetime import datetime, timezone

        path = os.path.join(str(root), ".bgate", "notify.jsonl")
        from bgate_core.queue import _rotate_notify

        _rotate_notify(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **line,
            }) + "\n")
    except Exception:
        pass


def _announce_candidate(root, artifact_id: int, name: str, revision: int,
                        rel: str, work_item_id: Optional[int]) -> None:
    """Put a pending human decision on the heartbeat and on the event bus.

    Two surfaces because they answer different questions and neither substitutes
    for the other: the jsonl is what a shell can tail with no database and no
    MCP, and the bus is what the notifier, the bell and the webhook read through
    a cursor that survives a restart. Both guarded — an art run must not fail
    because a notification could not be filed.
    """
    _notify_line(root, {
        "kind": "artifact.candidate",
        "item_id": int(work_item_id) if work_item_id else None,
        # 'candidate' is a real status in STATUSES, not a word invented here, so
        # a consumer switching on `status` reads something true.
        "status": "candidate",
        "seat": "art",
        "title": f"{name} r{revision} awaiting approval"[:120],
        "artifact_id": int(artifact_id),
        "path": rel,
    })
    try:
        from . import events as _events

        _events.emit(root, "artifact.candidate", ref=str(artifact_id), payload={
            "artifact_id": int(artifact_id), "logical_name": name,
            "revision": revision, "path": rel,
            "work_item_id": int(work_item_id) if work_item_id else None,
        })
    except Exception:
        pass


def get(root: str | os.PathLike[str], artifact_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM artifact_revision WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise LookupError(f"no artifact revision {artifact_id}")
    return _decode(dict(row))


def list_revisions(root: str | os.PathLike[str], *,
                   logical_name: Optional[str] = None,
                   status: Optional[str] = None,
                   limit: int = 100) -> list[dict]:
    conn = db.connect(root)
    sql, params = "SELECT * FROM artifact_revision WHERE 1=1", []
    if logical_name:
        sql += " AND logical_name = ?"
        params.append(logical_name)
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [_decode(row) for row in rows(conn.execute(sql, params))]


def archived_render(root: str | os.PathLike[str], artifact: dict) -> Optional[Path]:
    """The per-revision snapshot of this candidate, if one was archived.

    ``metadata.preview`` is written by the generators as an immutable copy of
    what this revision actually looked like; ``path`` is the shared sheet the
    next generation overwrites. Only the archive can put an older revision back.
    """
    raw = (artifact.get("metadata") or {}).get("preview")
    if not raw:
        return None
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    return candidate if candidate.is_file() else None


def _promote(root: str | os.PathLike[str], artifact: dict) -> dict:
    """Install this revision at the live path the game loads.

    Returns a record, never raises: an approval whose file could not be moved
    must still land as a decision, but it must land LOUDLY — the record says
    what is actually on disk so the workspace can show "approved but not live"
    rather than a green badge over the wrong image.
    """
    live = Path(root) / artifact["path"]
    live_hash = assets.file_hash(live) if live.is_file() else ""
    if live_hash and live_hash == (artifact["hash"] or ""):
        # The build already loads exactly these bytes — nothing to install.
        return {"ok": True, "promoted": False, "path": artifact["path"],
                "detail": "already the live file"}
    archive = archived_render(root, artifact)
    if archive is None:
        return {"ok": False, "promoted": False, "path": artifact["path"],
                "detail": "no archived render for this revision — the live file "
                          "is a different image and cannot be reinstalled"}
    try:
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, live)
        assets.track(root, artifact["path"])   # keep the registry hash honest
    except OSError as exc:
        return {"ok": False, "promoted": False, "path": artifact["path"],
                "detail": f"could not write {artifact['path']}: {exc}"}
    return {"ok": True, "promoted": True, "path": artifact["path"],
            "from": str(archive), "detail": "installed at the live path"}


def _auto_approve(root: str | os.PathLike[str]) -> bool:
    """Is this project letting agents promote their own artifacts?

    TWO WAYS TO SAY YES, and the second one is why this docstring is long.

    ``art.auto_approve`` (default False) is the narrow switch: art specifically,
    left alone by everything else. The APPROVAL GATE (``gate.mode``) is the broad
    one, and ``none`` is a project-wide instruction — "an agent's own word closes
    its item, nobody is checking" — that this path used to ignore completely.

    The consequence was reported from the field and it is worse than a stray
    card. With the gate at ``none`` every generated candidate still landed as a
    'candidate', so the dashboard drew an APPROVAL card reading *only a human can
    approve a candidate* for work the human had explicitly stopped asking about.
    Suppressing the card alone would have been the wrong fix twice over: the
    revision would sit un-promoted with the live path still holding the previous
    image, and the one surface that said so would be the one that had just been
    hidden. A gate that is off has to be off at the DECISION, not at the drawing
    of it.

    Deliberately not extended to ``agent``. That mode means a machine verifies
    the claim, and the art path's machine verdict (:func:`qa_verdict`) is defined
    to leave the revision a candidate — an agent approving art is exactly the
    drift the art-QA router exists to stop, and a second agent doing it is the
    same failure with an extra hop.

    Imported inside the function because ``settings`` and ``gates`` are higher
    layers than this module and a top-level import would make the dependency
    circular.

    FAILS CLOSED, both ways. A registry that cannot be read is not permission —
    it is an unknown, and the safe reading of an unknown here is "a human still
    decides".
    """
    try:
        from . import settings as _settings
        if bool(_settings.get(root, "art.auto_approve")):
            return True
    except Exception:
        return False
    try:
        from . import gates as _gates
        return _gates.mode(root) == _gates.NONE
    except Exception:
        return False


def review(root: str | os.PathLike[str], artifact_id: int, status: str,
           note: str = "", *, actor: Optional[str] = None) -> dict:
    """Approve/reject/integrate a candidate and preserve the decision as case law.

    Promotion is human-only and the reviewer is recorded. An agent calling this
    with 'approved' is refused by name — the art-QA router exists to stop the
    art seat approving its own drift, and a second agent doing it instead is the
    same failure with an extra hop. Agents record their judgement with
    :func:`qa_verdict`, which leaves the revision a candidate awaiting a human.
    """
    if status not in STATUSES[1:]:
        raise ValueError(f"review status must be one of {STATUSES[1:]}")
    who = actor if actor is not None else activity.current_actor()
    if (status in HUMAN_ONLY_STATUSES and not activity.is_human(who)
            and not _auto_approve(root)):
        raise PermissionError(
            f"{who or 'an unidentified caller'} is an agent and may not set "
            f"status {status!r} — promoting a candidate to the build is a "
            "human's call. Record the machine judgement with "
            "artifacts.qa_verdict(root, artifact_id, passed=..., note=...) "
            "(it stores metadata.qa_review and leaves the revision a candidate "
            "for a human to approve), or review(..., 'rejected') to fail it."
        )
    artifact = get(root, artifact_id)
    promotion = None
    metadata = artifact["metadata"]
    if status in ("approved", "integrated"):
        # Put the bytes where the build reads them. A revision that had to be
        # reinstalled is 'integrated'; one whose file was already live stays
        # 'approved' — both mean the live file IS this revision.
        promotion = _promote(root, artifact)
        metadata = {**metadata, "integration": promotion}
        if promotion["promoted"]:
            status = "integrated"
    with db.tx(root) as conn:
        if status in ("approved", "integrated"):
            conn.execute(
                "UPDATE artifact_revision SET status = 'superseded', "
                "reviewed_at = COALESCE(reviewed_at, datetime('now')) "
                "WHERE logical_name = ? AND id <> ? "
                "AND status IN ('approved','integrated')",
                (artifact["logical_name"], artifact_id),
            )
        conn.execute(
            "UPDATE artifact_revision SET status = ?, review_note = ?, "
            "reviewed_by = ?, reviewed_at = datetime('now'), metadata_json = ? "
            "WHERE id = ?",
            (status, note.strip()[:1000], (who or "")[:120],
             json.dumps(metadata), artifact_id),
        )
    tail = ""
    if promotion and promotion["promoted"]:
        tail = f" (installed at {promotion['path']})"
    elif promotion and not promotion["ok"]:
        tail = f" (NOT live: {promotion['detail']})"
    activity.log(root, "artifact_review",
                 f"{artifact['logical_name']} r{artifact['revision']} -> {status}{tail}",
                 ref=str(artifact_id), actor=who)
    iteration_id = artifact.get("iteration_id") or iterations.active_id(root)
    if iteration_id:
        iterations.add_event(
            root, int(iteration_id), "asset_decision", "artifact", str(artifact_id),
            f"{artifact['logical_name']} r{artifact['revision']} -> {status}{tail}",
            {"status": status, "reason": note.strip(), "integration": promotion})
    # THE DECISION CLOSING IS AS MUCH NEWS AS THE DECISION OPENING. Without this
    # a consumer that saw the candidate line has no way to learn it was answered,
    # so its pending list only ever grows and it goes on telling the human they
    # owe a decision they already made.
    _announce_review(root, artifact, status, who, promotion)
    return get(root, artifact_id)


def _announce_review(root, artifact: dict, status: str, who: str,
                     promotion: Optional[dict]) -> None:
    """The pending decision is settled. Same two surfaces as the candidate."""
    _notify_line(root, {
        "kind": "artifact.reviewed",
        "item_id": artifact.get("work_item_id"),
        "status": status,
        "seat": "art",
        "title": f"{artifact['logical_name']} r{artifact['revision']} "
                 f"-> {status}"[:120],
        "artifact_id": int(artifact["id"]),
        "by": (who or "")[:120],
    })
    try:
        from . import events as _events

        _events.emit(root, "artifact.reviewed", ref=str(artifact["id"]), payload={
            "artifact_id": int(artifact["id"]),
            "logical_name": artifact["logical_name"],
            "revision": artifact["revision"], "status": status,
            "by": (who or "")[:120],
            # Whether the BUILD actually loads these bytes now. 'approved' with a
            # failed promotion is the case where the badge and the disk disagree,
            # and a subscriber acting on the badge alone would be acting on the
            # wrong image.
            "live": bool(promotion and promotion.get("ok")) if promotion else None,
        })
    except Exception:
        pass


def qa_verdict(root: str | os.PathLike[str], artifact_id: int, *, passed: bool,
               note: str = "", score: int = 0, detail: Optional[dict] = None,
               actor: Optional[str] = None) -> dict:
    """An agent reviewer's judgement — the ceiling on what a model may decide.

    A pass is NOT an approval: it records metadata.qa_review and leaves the
    revision a candidate, so the dashboard can show 'machine-checked, awaiting
    a human'. A fail is a real rejection, because refusing to ship something is
    a decision an agent is allowed to make on its own.
    """
    artifact = get(root, artifact_id)
    who = actor if actor is not None else activity.current_actor()
    verdict = {"verdict": "pass" if passed else "fail",
               "score": max(0, min(int(score or 0), 100)),
               "reasons": note.strip()[:1000], "actor": who,
               **(detail or {})}
    metadata = artifact["metadata"]
    metadata["qa_review"] = verdict
    with db.tx(root) as conn:
        conn.execute("UPDATE artifact_revision SET metadata_json = ? WHERE id = ?",
                     (json.dumps(metadata), artifact_id))
    activity.log(root, "artifact_qa",
                 f"{artifact['logical_name']} r{artifact['revision']} qa "
                 f"{verdict['verdict']} ({verdict['score']}/100)",
                 ref=str(artifact_id), actor=who)
    if not passed:
        return review(root, artifact_id, "rejected", note, actor=who)
    return get(root, artifact_id)


# ---------------------------------------------------------------------------
# Reference provenance
# ---------------------------------------------------------------------------
def _pin_snapshot(root: str | os.PathLike[str], names: list[str]) -> list[dict]:
    """{name, revision, path, hash} for every ref that resolves to a pin."""
    from . import refs as _refs

    out = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            pin = _refs.get(root, name)
        except (LookupError, ValueError):
            continue  # a raw path, not a pin — nothing to version
        out.append({"name": pin["name"], "revision": int(pin["revision"] or 1),
                    "path": pin["path"], "hash": pin["hash"] or ""})
    return out


def _pin_index(root: str | os.PathLike[str]) -> dict[str, dict]:
    """Every pin by name, in one read. Drift is asked per revision; asking the
    database per revision made a 500-revision workspace 500 queries deep."""
    return {row["name"]: dict(row) for row in
            db.connect(root).execute("SELECT * FROM ref_pin")}


def ref_drift(root: str | os.PathLike[str], artifact: dict, *,
              index: Optional[dict[str, dict]] = None) -> list[dict]:
    """References that have moved on since this artifact was generated.

    An artifact card whose anchor was re-pinned is no longer evidence of what it
    claims: it was drawn against an image that is not what that name means now.

    ``index`` is the pins already read by a caller looping over many revisions
    (see :func:`workspace`); omit it and one is read for this call.
    """
    from .util import slugify

    pins = _pin_index(root) if index is None else index
    stale = []
    for pin in (artifact.get("metadata") or {}).get("ref_pins") or []:
        current = pins.get(slugify(str(pin.get("name") or "")))
        if current is None:
            stale.append({**pin, "current_revision": None,
                          "detail": "the pin was removed"})
            continue
        if (current["hash"] or "") != (pin.get("hash") or ""):
            stale.append({**pin, "current_revision": int(current["revision"] or 1),
                          "detail": f"{pin.get('name')} is now r"
                                    f"{int(current['revision'] or 1)} — this was "
                                    f"generated against r{pin.get('revision', 1)}"})
    return stale


def _chunks(values: list, size: int = 400) -> list[list]:
    """SQLite caps host parameters (999 by default); an IN () list has to fit."""
    return [values[i:i + size] for i in range(0, len(values), size)]


def workspace(root: str | os.PathLike[str]) -> list[dict]:
    """Logical assets with every revision and the state needed to review them.

    Every lookup this needs is read in a fixed number of queries, not one per
    revision: the dashboard polls this, and a per-row work-item/feedback/pin
    query turned a 500-revision project into ~1500 round trips per tick.
    """
    conn = db.connect(root)
    revisions = list_revisions(root, limit=500)
    tracked = {item["path"]: item for item in assets.list_assets(root)}
    latest_iteration = conn.execute(
        "SELECT active_artifact_ids_json FROM iteration ORDER BY id DESC LIMIT 1"
    ).fetchone()
    used_ids: set[int] = set()
    if latest_iteration:
        try:
            used_ids = {int(value) for value in
                        json.loads(latest_iteration["active_artifact_ids_json"])}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    work_ids = sorted({int(r["work_item_id"]) for r in revisions
                       if r.get("work_item_id")})
    work_by_id: dict[int, dict] = {}
    for batch in _chunks(work_ids):
        marks = ",".join("?" * len(batch))
        work_by_id.update({int(row["id"]): dict(row) for row in conn.execute(
            "SELECT id, seat, title, status, result, updated_at "
            f"FROM work_item WHERE id IN ({marks})", tuple(batch))})
    pins = _pin_index(root)

    groups: dict[str, dict] = {}
    for revision in revisions:
        asset = tracked.get(revision["path"], {})
        work = (work_by_id.get(int(revision["work_item_id"]))
                if revision.get("work_item_id") else None)
        revision.update({
            "profile": revision["metadata"].get("profile", ""),
            "consistency": revision["metadata"].get("consistency", {}),
            "engine_import": revision["metadata"].get("engine_import", {}),
            "integration": revision["metadata"].get("integration", {}),
            "used_in_current_build": int(revision["id"]) in used_ids,
            "ref_drift": ref_drift(root, revision, index=pins),
            "lock": {
                "seat": asset.get("lock_seat"),
                "owner": asset.get("lock_owner", ""),
                "work_item_id": asset.get("work_item_id"),
                "heartbeat_at": asset.get("heartbeat_at"),
                "lease_expires_at": asset.get("lease_expires_at"),
            } if asset else None,
            "work_item": work,
        })
        group = groups.setdefault(revision["logical_name"], {
            "logical_name": revision["logical_name"],
            "approved": None,
            "candidates": [],
            "revisions": [],
            "feedback": [],
        })
        group["revisions"].append(revision)
        if revision["status"] in ("approved", "integrated") and group["approved"] is None:
            group["approved"] = revision
        if revision["status"] == "candidate":
            group["candidates"].append(revision)

    # Feedback for every group in one pass, bucketed here. Groups with no
    # linked playtest items keep the empty list they were created with.
    names = sorted(groups)
    for batch in _chunks(names):
        marks = ",".join("?" * len(batch))
        for row in rows(conn.execute(
                "SELECT l.logical_name, i.id, i.session_id, i.t, i.kind, i.text, "
                "i.seat, i.status, l.confidence FROM playtest_item_asset l "
                "JOIN playtest_item i ON i.id = l.item_id "
                f"WHERE l.logical_name IN ({marks}) ORDER BY i.id DESC",
                tuple(batch))):
            groups[row.pop("logical_name")]["feedback"].append(row)
    return sorted(groups.values(), key=lambda item: item["logical_name"].lower())


def regenerate(root: str | os.PathLike[str], artifact_id: int,
               reason: str = "") -> dict:
    """Queue a new revision using the exact provenance of an existing one."""
    from . import queue

    artifact = get(root, artifact_id)
    brief = (
        f"Regenerate {artifact['logical_name']} from revision "
        f"{artifact['revision']}. Original producer={artifact['producer'] or 'unknown'}, "
        f"model={artifact['model'] or 'unknown'}, prompt={artifact['prompt']!r}, "
        f"refs={artifact['refs']}. Review request: {reason or 'produce a stronger candidate'}. "
        "Register the result as a new immutable artifact revision."
    )
    return queue.add(
        root, "art", f"Regenerate {artifact['logical_name']}",
        brief=brief, priority=2, source="artifact",
        source_ref=str(artifact_id))


def link_feedback(root: str | os.PathLike[str], artifact_id: int,
                  item_id: int, confidence: float = 1.0) -> dict:
    artifact = get(root, artifact_id)
    with db.tx(root) as conn:
        exists = conn.execute(
            "SELECT 1 FROM playtest_item WHERE id = ?", (item_id,)).fetchone()
        if not exists:
            raise LookupError(f"no playtest item {item_id}")
        conn.execute(
            "INSERT INTO playtest_item_asset (item_id, logical_name, confidence) "
            "VALUES (?, ?, ?) ON CONFLICT(item_id, logical_name) DO UPDATE SET "
            "confidence = excluded.confidence",
            (item_id, artifact["logical_name"], max(0.0, min(float(confidence), 1.0))))
    return {"artifact_id": artifact_id, "logical_name": artifact["logical_name"],
            "item_id": item_id}


def record_check(root: str | os.PathLike[str], path: str | os.PathLike[str],
                 key: str, result: dict) -> Optional[dict]:
    """Attach consistency/import evidence to the newest revision for a path."""
    rel = assets.normalize_path(root, path)
    row = db.connect(root).execute(
        "SELECT * FROM artifact_revision WHERE path = ? "
        "ORDER BY revision DESC LIMIT 1", (rel,)).fetchone()
    if row is None:
        return None
    artifact = _decode(dict(row))
    metadata = artifact["metadata"]
    metadata[key] = result
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE artifact_revision SET metadata_json = ? WHERE id = ?",
            (json.dumps(metadata), artifact["id"]))
    return get(root, int(artifact["id"]))


def _decode(row: dict) -> dict:
    for source, target, fallback in (
        ("refs_json", "refs", []),
        ("metadata_json", "metadata", {}),
    ):
        try:
            row[target] = json.loads(row.pop(source))
        except (TypeError, json.JSONDecodeError):
            row.pop(source, None)
            row[target] = fallback
    return row
