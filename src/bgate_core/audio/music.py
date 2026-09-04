"""Generated music, and the thing that makes it an asset rather than a file.

WHAT WAS MISSING. The kie adapter could already talk to Suno properly — models,
mode limits, the seven-and-a-half statuses, the fourteen-day retention. What it
could not do was hand the result to anything: :func:`bgate_adapters.kie.generate_music`
wrote .mp3s into ``.bgate_out/audio/`` and returned a list of paths carrying the
sentence "nothing downstream imports generated music yet". Which was true. The
audio lab's file walk SKIPS ``.bgate_out`` (routes/audiolab.py:SKIP_DIRS), the
audio seat's library only reads ``game/assets/audio`` and ``audio/``, and the
Godot project cannot reference a path outside itself. So a paid-for track landed
somewhere no surface in this product could see. This module is the missing half.

THE SHAPE IS THE ART SEAT'S, deliberately and almost line for line. One request
returns SEVERAL tracks — Suno generates variations — which is the same problem
the art pipeline already solved: a batch of candidates, a human auditions them,
one is kept and the rest are rejected, and every one of them keeps its
provenance row forever. So each track is registered with
:func:`bgate_core.store.artifacts.register` under ONE logical name as consecutive
revisions, exactly as ``generate.py`` does for a batch of candidate images, and
keep/discard are ``artifacts.review(approved|rejected)``. A second review idiom
for the same job would be worse than a slightly imperfect fit — and it is not
even an imperfect fit: 'only a human may approve' is the right rule for a track
that ships in the game, and it comes free by not reinventing it.

WHERE A KEPT TRACK GOES, and why keep() is not just a status flip. Art's live
path is ``.bgate_out/art/`` and a separate ``godot_import_asset`` step carries
it into the game. Audio has no such importer and needs none — Godot picks up
anything under its project directory — so the import IS the copy, and keeping a
track performs it: the file is installed at ``game/assets/audio/music/<name>``,
which is inside the audio seat's own write lane (seats.py), inside the engine
project, and inside both audio listings. Only THEN is the revision approved.
An approval whose file never moved would be a green badge over a track the game
cannot load, which is the exact failure ``artifacts._promote`` exists to prevent
on the image side.

NO URL IS EVER THE ASSET. kie serves generated files for fourteen days. The
adapter downloads inside the polling loop; this module records the source URL in
metadata as PROVENANCE ONLY, stamped with the date it dies, so nothing can
mistake it for a place to fetch from later.

THE TASK ID IS WRITTEN BEFORE THE WAIT, NOT AFTER IT. A Suno batch is charged
when it is ACCEPTED and then polled for about three minutes; until this module
kept a pending ticket, the id first reached durable storage inside
:func:`_absorb` — i.e. only after a download that had already succeeded. So the
one window where the process dying costs money was the only window in which
nothing had been written down: the batch rendered, was billed, and
:func:`recover` — which needs a task id — could not be called, because the id
had existed solely in a local variable in a dead process. Every generation now
opens a ticket under ``.bgate_out/audio/.pending/`` BEFORE the submit and
stamps the id onto it the instant kie hands one over (see
``kie.generate_music``'s ``on_submit``), and :func:`stuck_tracks` is what
NOTICES the tickets nobody came back for. The shape and the vocabulary are
``bgate_core.cine.cinematic``'s ``stuck_shots`` on purpose — someone who knows one
knows the other. No new table: a ticket is a file beside the takes it is waiting
for, under the same gitignored scratch directory.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from ..board import activity
from ..store import artifacts, assets, db
from ..store.util import slugify

# Where candidates land. Under .bgate_out because they are scratch until a human
# keeps one — gitignored, and no rejected take ever reaches the engine project.
CANDIDATE_DIR = Path(".bgate_out") / "audio"

# Where a KEPT track is installed. The first directory that already exists wins,
# so a project that files audio at <root>/audio does not suddenly grow a second
# tree; both are in the audio seat's write lane (bgate_core.board.seats).
# WHERE A KEPT TRACK LANDS, most specific layout first. The list is tried
# against directories that ALREADY EXIST, so it reads as "which shape is
# this project" rather than as a preference.
#
# `assets/audio` WAS MISSING AND IT IS THE COMMON ONE. It is what `bgate
# adopt` finds in an existing Godot repo (project.godot at the root, assets
# beside it), it is what all three benchmark games used, and it is what the
# hosted-audio control run used. With only the scaffold layout listed,
# _install_dir fell through to CREATING `game/assets/audio/music/` - a
# directory no scene, script or resource in the project names. The tool then
# reported `installed: true` with a path, a byte count and a hash, all of
# them true, about a file the game does not load. Presence, not integration,
# in the one step whose whole job is integration.
INSTALL_ROOTS = (Path("game") / "assets" / "audio",
                 Path("assets") / "audio",
                 Path("audio"))
INSTALL_SUBDIR = "music"

PRODUCER = "kie_music"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg"}

# WHERE A SUBMITTED-BUT-UNCOLLECTED GENERATION IS REMEMBERED. One JSON ticket
# per submit, beside the candidates it is waiting for — dotted so the audio
# seat's own listings and the candidate walk never mistake one for a take, and
# under .bgate_out because it is scratch that is deleted the moment the batch is
# on disk. A file rather than a column: cine_shot already existed to hold
# cinematic's task id, music has no table of its own, and a schema migration is
# a heavy price for a handle whose whole life is three minutes.
PENDING_DIR = CANDIDATE_DIR / ".pending"

# How long a ticket may sit uncollected before it is worth asking kie about. A
# Suno batch renders in about three minutes and generate() gives up at fifteen,
# so ten is past any healthy run and still inside a live call's own patience —
# a sweep must never call a poll loop stuck before that loop has given up.
STUCK_AFTER_S = 600


class MusicError(RuntimeError):
    """A music operation failed in a way the caller should surface."""


# ---------------------------------------------------------------------------
# What can be asked for
# ---------------------------------------------------------------------------

def options(root: str | os.PathLike[str]) -> dict:
    """The whole generation surface as data: models, limits, availability.

    Every number here comes from the adapter's own tables. A form that retypes
    "5000 characters" is a form that lies the day the reference changes.
    """
    from bgate_adapters import kie

    got = dict(kie.available(root))
    return {
        "available": bool(got.get("available")),
        "reason": got.get("reason", ""),
        "provider": "kie/suno",
        "install_dir": _install_dir(root, create=False).as_posix(),
        "candidate_dir": CANDIDATE_DIR.as_posix(),
        **kie.music_options(),
    }


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------

def generate(root: str | os.PathLike[str], prompt: str, *, name: str = "",
             work_item_id: Optional[int] = None, timeout: float = 900.0,
             on_progress: Any = None, **suno: Any) -> dict:
    """Generate a batch of tracks and register every one as a candidate.

    Returns ``{ok, logical_name, candidates: [...], credits..., accounted}``.
    ``suno`` is passed straight through to :func:`bgate_adapters.kie.build_music`
    (model, custom, instrumental, style, title, negative_tags, duration,
    vocal_gender, the three weights), which validates it against the model's own
    limits BEFORE any money moves.

    ``on_progress(fraction, words, status)`` is called at every step Suno
    reports, so a caller can say WHICH minute of the three this is. Raising from
    it cancels the wait — see :class:`bgate_adapters.kie.MusicCancelled`; the
    task id comes back on the result either way, so a cancelled or failed batch
    is still collectable with :func:`recover`.

    A PENDING TICKET IS OPENED BEFORE THE SUBMIT and closed only when the takes
    are on disk. Everything between those two points is paid-for work nobody has
    collected, which is exactly what :func:`stuck_tracks` sweeps for.
    """
    from bgate_adapters import kie

    root = str(root)
    text = str(prompt or "").strip()
    if not text:
        raise MusicError("a music generation needs a prompt")
    stem = slugify(name or _stem_from(text))

    # An unnamed model takes the stored preference (music.model) before the
    # adapter default; build_music validates it like any explicit choice.
    if not str(suno.get("model") or "").strip():
        try:
            from ..store import settings as _settings

            preferred = str(_settings.get(root, "music.model") or "").strip()
            if preferred:
                suno["model"] = preferred
        except Exception:
            pass

    out_dir = Path(root) / CANDIDATE_DIR / stem
    # OPENED BEFORE THE CALL, exactly as cinematic sets a shot to 'generating'
    # before it asks kie for one. A ticket with no task id on it is the worst
    # category and it has to be REACHABLE: it means the process died between the
    # submit leaving and the id coming back, which is a charge with no handle.
    # Opening it afterwards would make that case indistinguishable from a run
    # that never happened.
    ticket = _open_ticket(root, stem, text)
    result = kie.generate_music(
        text, str(out_dir), name=stem, root=root, logical_name=stem,
        work_item_id=work_item_id, timeout=float(timeout),
        on_progress=on_progress,
        # THE INSTANT IT EXISTS. Not on the return: the poll that follows runs
        # for minutes and a return value does not survive a killed process.
        on_submit=lambda task_id: _stamp_ticket(ticket, task_id=str(task_id or "")),
        **suno)
    if not result.get("ok"):
        if result.get("task_id"):
            # Suno was already asked, so this batch is paid for whatever went
            # wrong afterwards — the download, the timeout, a human cancelling.
            # The ticket STAYS OPEN so the sweep reports it as collectable;
            # cinematic can afford to close its row here because a shot row is
            # permanent and this handle is not.
            _stamp_ticket(ticket, task_id=str(result["task_id"]),
                          error=str(result.get("error") or "")[:400])
        else:
            # Nothing reached kie that we know of — a prompt the model refused,
            # a cancel in the first second. Leaving the ticket would report a
            # refusal as lost money, which is the sweep crying wolf.
            _close_ticket(ticket)
        return {**result, "logical_name": stem, "candidates": []}
    out = _absorb(root, stem, text, result, work_item_id=work_item_id,
                  suno=suno, on_progress=on_progress)
    _close_ticket(ticket)
    return out


def recover(root: str | os.PathLike[str], task_id: str, *, name: str = "",
            work_item_id: Optional[int] = None, on_progress: Any = None) -> dict:
    """Download and register the tracks of a task that was already paid for.

    THE DOOR THAT HAD TO EXIST. :func:`status` could only ever REPORT — it said
    "re-run generate() to get files on disk", which means paying twice for audio
    kie is already holding. That was a design flaw the moment it was written and
    a live one the moment kie's CDN started answering this product's downloads
    with a 403: every generation submitted, rendered, was charged, and died at
    the download with two finished tracks stranded behind a URL that expires in
    fourteen days.

    IDEMPOTENT BY SUNO TRACK ID. Recovering a task twice does not download twice
    or file a second set of candidates — each take is matched against
    ``metadata.suno_id`` on the revisions already registered, and the ones that
    are on disk already are reported as ``skipped``. So this is safe to hit
    after a partial recovery, which is the exact case it exists for.

    NO COST IS CLAIMED. The charge happened at submit time, possibly days ago;
    a balance delta measured now would be meaningless, so credits are reported
    as unmeasurable rather than as zero.
    """
    from bgate_adapters import kie

    root = str(root)
    state = status(root, task_id)
    tracks = state["tracks"]
    if not tracks:
        return {"ok": False, "task_id": state["task_id"], "candidates": [],
                "status": state["status"],
                "error": f"kie has no audio for task {state['task_id']} "
                         f"(status {state['status'] or 'unknown'})"
                         + (f": {state['error_message']}"
                            if state["error_message"] else "")
                         + (" — it is still running; try again when it reports "
                            "SUCCESS" if state["running"] else "")}

    # Best available name, in order: what the caller asked for; the name a
    # previous attempt at this task filed under (so a recovery lands beside the
    # takes it belongs with); the title Suno gave the music. A hex prefix is the
    # last resort, not the default — an asset called `0fdddcb86200` is one
    # nobody will recognise in the mixer a week later.
    stem = slugify(name or _stem_for_task(root, state["task_id"])
                   or str(tracks[0].get("title") or "").strip()
                   or state["task_id"][:12])
    known = _known_suno_ids(root, stem)
    fresh = [t for t in tracks if str(t.get("id") or "") not in known]
    skipped = len(tracks) - len(fresh)
    if not fresh:
        # Every take of this task is already filed, so whatever ticket is still
        # open for it is describing work that HAS been collected. Closing it
        # here is what stops the sweep reporting the same non-problem for ever.
        _close_task_tickets(root, state["task_id"])
        return {"ok": True, "task_id": state["task_id"], "logical_name": stem,
                "candidates": [], "count": 0, "skipped": skipped,
                "note": f"all {skipped} take(s) from this task are already "
                        "registered — nothing was downloaded again"}

    written = kie.download_tracks(fresh, str(Path(root) / CANDIDATE_DIR / stem),
                                  stem=f"{stem}_recovered",
                                  on_progress=on_progress)
    carrier = {"model": (fresh[0].get("model_name") or ""),
               "task_id": state["task_id"],
               "credits_consumed": None,
               "credits_source": "not_measurable_after_the_fact",
               "usd": None, "accounted": False,
               "expires_at": ""}
    out = _absorb(root, stem, f"recovered from kie task {state['task_id']}",
                  {**carrier, "tracks": written}, work_item_id=work_item_id,
                  suno={}, on_progress=on_progress)
    # The takes are on disk and filed: this task is no longer uncollected.
    _close_task_tickets(root, state["task_id"])
    return {**out, "ok": True, "recovered": True, "skipped": skipped,
            "task_id": state["task_id"],
            "note": f"downloaded {len(written)} take(s) kie was already holding"
                    + (f"; {skipped} were already registered" if skipped else "")
                    + ". These were charged when the task was submitted, so no "
                      "cost is recorded against this call."}


def status(root: str | os.PathLike[str], task_id: str) -> dict:
    """Where a running (or finished) Suno task got to. Costs nothing.

    Reports; :func:`recover` acts. A caller whose download died — or whose
    timeout was too short, or who pressed cancel — has a task id, a charge, and
    no files; this says whether the audio exists, and recover() then collects it
    without paying again.
    """
    from bgate_adapters import kie

    ident = str(task_id or "").strip()
    if not ident:
        raise MusicError("a task id is needed to check a music generation")
    record = kie.music_record(ident, root=str(root))
    state = str(record.get("status") or "").upper()
    tracks = kie.music_tracks(record)
    return {
        "ok": True, "task_id": ident, "status": state,
        "stage": kie.SUNO_STAGE.get(state, (0.0, f"Suno reports {state}"))[1],
        "done": state == kie.SUNO_DONE,
        "running": state in kie.SUNO_RUNNING,
        "failed": state in kie.SUNO_DEAD or (
            state == kie.SUNO_CALLBACK_FAILED and not tracks),
        "callback_failed": state == kie.SUNO_CALLBACK_FAILED,
        "track_count": len(tracks),
        "tracks": tracks,
        "error_message": record.get("errorMessage") or "",
        "retention_days": kie.SUNO_URL_TTL_DAYS,
        "recoverable": bool(tracks),
        "note": ("these are kie's own URLs and they expire — call recover() to "
                 "put the audio on disk without paying for it again"
                 if tracks else
                 "kie is holding no audio for this task yet"),
    }


# ---------------------------------------------------------------------------
# Paid work nobody is watching any more
# ---------------------------------------------------------------------------

def stuck_tracks(root: str | os.PathLike[str], *,
                 older_than_s: int = STUCK_AFTER_S, poll: bool = True,
                 limit: int = 50) -> dict:
    """Generations that were submitted and never collected. The music sweep.

    THE FAILURE THIS SWEEPS FOR IS SILENT AND EXPENSIVE, and it is the same one
    ``cinematic.stuck_shots`` exists for. A batch is charged at SUBMIT and then
    polled for about three minutes; if the dashboard restarts, the agent is
    killed or the connection drops in that window, nobody ever asks for the
    audio again and kie holds two finished tracks, paid for, until the fourteen
    days run out. :func:`recover` has always been able to collect one — what was
    missing is anything that NOTICES, so recovery depended on a human
    remembering a task id they never saw.

    ``poll=False`` answers from the pending tickets alone: no network, no
    provider call, safe on every dashboard refresh. ``poll=True`` asks kie about
    each one, which is what turns "stale" into "finished, paid for, and one call
    from being on disk".

    A TICKET WITH NO TASK ID IS ITS OWN CATEGORY and the worst one. The submit
    left; the handle did not come back. It is reported as ``lost`` rather than
    folded in with the failures because the two want different things from a
    human — one is a click, the other is kie's own dashboard.
    """
    root = str(root)
    cutoff = max(0, int(older_than_s))
    collected = _task_index(root)
    stale = []
    for path, record in _tickets(root):
        age = _ticket_age_s(path, record)
        if age < cutoff:
            continue
        stale.append((age, path, record))
    stale.sort(key=lambda item: -item[0])
    stale = stale[:max(1, int(limit))]

    out = []
    for age, path, record in stale:
        entry = {"logical_name": record.get("logical_name") or "",
                 "prompt": record.get("prompt") or "",
                 "task_id": str(record.get("task_id") or ""),
                 "ticket": Path(path).relative_to(Path(root)).as_posix(),
                 "updated_at": record.get("updated_at") or "",
                 "age_s": int(age), "recoverable": False}
        if not entry["task_id"]:
            out.append({**entry, "state": "lost",
                        "note": "this generation was submitted with no task id "
                                "recorded, so there is no handle to collect it "
                                "with. If it reached kie it was charged for; "
                                "kie's own dashboard is the only place left to "
                                "look."})
            continue
        if entry["task_id"] in collected:
            # A crash between the download and the ticket being closed leaves
            # one of these. Reporting a batch that IS filed as money waiting to
            # be spent again is the exact wrong answer, so the registry is asked
            # first — one scan for the whole sweep, not one per ticket.
            out.append({**entry, "state": "collected",
                        "logical_name": collected[entry["task_id"]],
                        "note": "the takes from this task are already "
                                "registered — the ticket outlived the batch it "
                                "was holding a place for and nothing is owed."})
            continue
        if not poll:
            out.append({**entry, "state": "unpolled",
                        "note": "stale, and not asked about — call with "
                                "poll=True to find out whether kie is holding "
                                "finished audio for it"})
            continue
        try:
            state = status(root, entry["task_id"])
        except Exception as exc:                                 # noqa: BLE001
            out.append({**entry, "state": "unknown", "error": str(exc)[:300],
                        "note": "kie could not be asked about this task, so "
                                "whether it is running, finished or dead is "
                                "unknown — not resolved."})
            continue
        if state.get("recoverable"):
            out.append({**entry, "state": "recoverable", "recoverable": True,
                        "provider_status": state.get("status", ""),
                        "track_count": state.get("track_count", 0),
                        "note": "PAID AND COLLECTABLE — kie is holding finished "
                                "audio for this task. recover() puts it on disk "
                                "without paying again; re-generating pays "
                                "twice."})
        elif state.get("failed"):
            out.append({**entry, "state": "failed",
                        "provider_status": state.get("status", ""),
                        "error": state.get("error_message") or "",
                        "note": "the generation failed at kie. It can be "
                                "re-generated."})
        else:
            out.append({**entry, "state": "running",
                        "provider_status": state.get("status", ""),
                        "note": "still running at kie despite the age of the "
                                "ticket — wait rather than re-generating."})

    counts: dict[str, int] = {}
    for entry in out:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    recoverable = counts.get("recoverable", 0)
    return {
        "ok": True,
        "older_than_s": cutoff,
        "polled": bool(poll),
        "stale": len(out),
        "counts": counts,
        "recoverable": recoverable,
        "retention_days": _retention_days(),
        "tracks": out,
        "note": (f"{recoverable} generation(s) are finished at kie, already "
                 "charged for, and waiting to be collected with recover()"
                 if recoverable else
                 "no generation is sitting on a paid, uncollected batch"
                 if poll else
                 f"{len(out)} generation(s) are stale; kie was not asked about "
                 "any of them, so none is resolved either way"),
    }


# ---------------------------------------------------------------------------
# Auditioning, keeping, discarding
# ---------------------------------------------------------------------------

def candidates(root: str | os.PathLike[str], *, logical_name: str = "",
               limit: int = 200) -> list[dict]:
    """Generated audio revisions awaiting a decision, newest first.

    Filtered to THIS producer rather than to every audio artifact: the audio
    seat can register a hand-mixed .wav too, and a gallery offering to
    "discard" someone's own mixdown would be a nasty surprise.
    """
    rows = artifacts.list_revisions(root, logical_name=logical_name or None,
                                    status="candidate", limit=limit)
    return [_view(root, row) for row in rows
            if (row.get("producer") or "") == PRODUCER]


def kept(root: str | os.PathLike[str], *, limit: int = 200) -> list[dict]:
    """Approved tracks, with where each one was installed — or that it was not.

    ``superseded`` is included on purpose. Under a project whose approval gate
    is off, every take of a batch is approved as it is registered and each one
    supersedes the last, so the takes a human might still want to pick would
    otherwise vanish from every surface the moment they were generated.
    """
    out = []
    for state in ("approved", "integrated", "superseded"):
        for row in artifacts.list_revisions(root, status=state, limit=limit):
            if (row.get("producer") or "") == PRODUCER:
                out.append(_view(root, row))
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out[:limit]


def keep(root: str | os.PathLike[str], artifact_id: int, *, note: str = "",
         actor: Optional[str] = None) -> dict:
    """Install a take into the engine project, then approve the revision.

    THE ORDER IS THE POINT, AND THE COPY IS NOT BEST-EFFORT. Approving first
    would leave an approval that means nothing when the copy fails — the DB
    saying 'approved' while the game has no file. So the install happens first
    and its failure is RAISED, not noted: no install, no approval. The worst
    case is a file in the engine project whose revision is still a candidate,
    which the gallery shows and a human can retry; that is the recoverable
    direction to fail in.

    :func:`artifacts.review` is what enforces 'only a human may approve'. This
    does not re-implement that gate, it inherits it — so a project running with
    the approval gate off gets the behaviour it configured and nothing else does.
    """
    root = str(root)
    art = artifacts.get(root, int(artifact_id))
    install = _install_file(root, art)
    # Stamped BEFORE review so the row review() reads and rewrites already
    # carries it — review merges its own `integration` key into whatever it
    # finds, so a write afterwards would race with nothing but would also be a
    # second transaction for one decision.
    _stamp(root, int(artifact_id), "install", install)

    reviewed = artifacts.review(root, int(artifact_id), "approved",
                                note=note or "kept from the Suno candidate "
                                             "gallery and installed for the "
                                             "engine",
                                actor=actor)
    activity.log(root, "audio",
                 f"kept {art['logical_name']} r{art['revision']} -> "
                 f"{install['path']}",
                 seat="audio", ref=str(artifact_id), actor=actor or "")
    return {"ok": True, "artifact": _view(root, reviewed), "install": install}


def install(root: str | os.PathLike[str], artifact_id: int, *,
            actor: Optional[str] = None) -> dict:
    """Put an already-approved take where the game can load it. The repair verb.

    WHY THIS HAS TO EXIST, and it is not a nicety — it is the fix for a real
    project that generated music and got nothing it could ship.

    :func:`keep` was written on the assumption that a generated take is a
    CANDIDATE until a human keeps it, and keeping is what installs it. That
    assumption is false on any project whose approval gate is off.
    ``artifacts.register`` consults ``gate.mode``/``art.auto_approve`` and, when
    either says so, APPROVES THE REVISION INSIDE THE REGISTER CALL — before
    music.generate has returned. Measured on a live project: ``gate.mode =
    none``, both takes ``status=approved``, ``reviewed_by=setting:art.auto_approve``,
    no ``install`` metadata, ``game/assets/audio/music/`` never created, both
    .mp3s still sitting in ``.bgate_out``. There was never a candidate to keep,
    so there was never a keep to install. The database said approved and the
    game had nothing.

    :func:`_register` now installs on that path too, so new generations do not
    reach this state. This is what repairs the ones that already did — and it is
    also the honest button for an approved track whose file was later deleted
    out of the engine project.

    Idempotent, and it does NOT change review state: an approved revision stays
    approved, a superseded one stays superseded (installing an older take is how
    you change your mind about an auto-approved batch — use :func:`keep` for
    that, which installs AND re-approves).
    """
    root = str(root)
    art = artifacts.get(root, int(artifact_id))
    if art.get("status") == "rejected":
        raise MusicError(
            f"{art['logical_name']} r{art['revision']} was rejected — installing "
            "it would put a discarded take in the game. keep() it instead if you "
            "have changed your mind.")
    record = _install_file(root, art)
    _stamp(root, int(artifact_id), "install", record)
    activity.log(root, "audio",
                 f"installed {art['logical_name']} r{art['revision']} -> "
                 f"{record['path']}",
                 seat="audio", ref=str(artifact_id), actor=actor or "")
    return {"ok": True, "artifact": _view(root, artifacts.get(root, int(artifact_id))),
            "install": record}


def discard(root: str | os.PathLike[str], artifact_id: int, *,
            note: str = "", actor: Optional[str] = None) -> dict:
    """Reject a candidate. The file stays; the decision is what is recorded.

    Deliberately does NOT delete. Candidates live under ``.bgate_out``, which is
    gitignored and outside the engine project, so a rejected take costs disk and
    nothing else — while an artifact row whose path has been unlinked reports as
    ``missing`` in :func:`bgate_core.store.assets.verify` forever, which is a real
    alarm spent on a decision that was made on purpose. Same call art makes.
    """
    reviewed = artifacts.review(root, int(artifact_id), "rejected",
                                note=note or "discarded from the Suno "
                                             "candidate gallery",
                                actor=actor)
    return {"ok": True, "artifact": _view(root, reviewed),
            "note": "the file is left under .bgate_out (gitignored, outside "
                    "the engine project); only the decision was recorded"}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _absorb(root: str, stem: str, prompt: str, result: dict, *,
            work_item_id: Optional[int], suno: dict,
            on_progress: Any = None) -> dict:
    """Turn downloaded tracks into candidate revisions. Shared by both doors.

    generate() and recover() differ entirely in how the files arrive and not at
    all in what happens next, so what happens next is written once.
    """
    if on_progress:
        on_progress(0.95, "filing the takes as candidates", "")
    registered = []
    for track in result.get("tracks") or []:
        try:
            registered.append(_register(root, stem, track, result, prompt,
                                        work_item_id=work_item_id, suno=suno))
        except Exception as exc:                                 # noqa: BLE001
            # A registration that fails must not lose the file that was paid
            # for. Say which track, and hand the path back anyway.
            registered.append({"artifact_id": None, "path": track.get("path"),
                               "error": f"{type(exc).__name__}: {exc}"})

    filed = [c for c in registered if c.get("artifact_id")]
    # THE GATE MAY ALREADY HAVE DECIDED. artifacts.register consults gate.mode
    # and art.auto_approve and APPROVES INSIDE THE REGISTER CALL when either
    # says so — measured on a live project with gate.mode=none: every take came
    # back approved, none was ever a candidate, so the keep() that installs was
    # unreachable and the run produced two approved rows and no file the game
    # could load. A gate that is off means "no human picks", not "do not
    # deliver": the surviving approved take is installed here, now.
    auto = _auto_installed(root, filed)
    activity.log(root, "audio",
                 f"{len(registered)} Suno take(s) for {stem}"
                 + (f" ({len(filed)} registered)" if len(filed) != len(registered)
                    else "")
                 + (f" — approval gate off, installed {auto['path']}"
                    if auto else ""),
                 seat="audio", ref=str(filed[0]["artifact_id"]) if filed else stem)
    if on_progress:
        on_progress(1.0, f"{len(filed)} take(s) ready to audition", "")
    out = {**result, "logical_name": stem,
           # Re-read: the auto-install stamped metadata onto rows the views in
           # `registered` were taken from, and a card that shows a stale "not
           # installed" is the bug this whole path exists to fix.
           "candidates": [_view(root, artifacts.get(root, c["artifact_id"]))
                          if c.get("artifact_id") else c for c in registered],
           "count": len(registered),
           "consumes": "audition these in the audio seat, then keep() one — "
                       "keeping installs it under the engine project and "
                       "approves the revision"}
    if auto:
        out["auto_installed"] = auto
        out["gate"] = (
            "this project's approval gate is OFF, so every take was approved as "
            f"it was registered and the last one ({auto['path']}) is installed "
            "for the engine. Nobody picked it — keep() a different take to swap "
            "it, or turn the gate back on to be asked.")
    return out


def _auto_installed(root: str, filed: list) -> Optional[dict]:
    """Install the surviving approved take of an auto-approved batch, once.

    Only the survivor: register() supersedes each earlier approval as the next
    one lands, so installing every take would copy N files to one destination
    and leave the registry describing whichever won the race.
    """
    for view in reversed(filed):
        try:
            art = artifacts.get(root, view["artifact_id"])
        except Exception:                                        # noqa: BLE001
            continue
        if art.get("status") not in ("approved", "integrated"):
            continue
        try:
            record = _install_file(root, art)
        except MusicError:
            # Never lose the generation over the delivery. The take is filed
            # and approved; _view reports installed=False and the seat offers
            # the repair button.
            return None
        _stamp(root, int(view["artifact_id"]), "install", record)
        return record
    return None


# ---------------------------------------------------------------------------
# The pending ledger — one file per submitted generation
# ---------------------------------------------------------------------------
#
# A ticket exists for exactly as long as a batch is paid for and uncollected.
# Every write below is BEST EFFORT and silent on failure, for the rule the whole
# module holds: bookkeeping must never lose the audio it was bookkeeping. The
# cost of a lost ticket is a sweep that misses one batch; the cost of a raise
# here would be a generation that dies at the paperwork.

def _open_ticket(root: str, stem: str, prompt: str) -> Path:
    """Note that a generation is about to be submitted. Returns its ticket path.

    Named for the moment rather than for the task, because there IS no task id
    yet — that is the entire point. A ticket that never receives one is what the
    sweep reports as ``lost``.
    """
    directory = Path(root) / PENDING_DIR
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{stamp}-{(stem or 'track')[:40]}-{uuid4().hex[:8]}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return path
    _write_ticket(path, {"logical_name": stem, "prompt": str(prompt)[:400],
                         "task_id": "", "created_at": _now(),
                         "updated_at": _now()})
    return path


def _stamp_ticket(path: str | os.PathLike[str], **fields: Any) -> None:
    """Merge fields into a ticket. Called from kie's on_submit hook."""
    record = _read_ticket(path) or {"created_at": _now()}
    record.update(fields)
    record["updated_at"] = _now()
    _write_ticket(Path(path), record)


def _close_ticket(path: str | os.PathLike[str]) -> None:
    """The batch is on disk (or was never submitted). Nothing is owed."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def _close_task_tickets(root: str, task_id: str) -> int:
    """Close every ticket holding this task id. The door :func:`recover` uses.

    By id rather than by path because recover() is called by a human with a task
    id and no memory of which ticket — often in a different process from the one
    that opened it, which is the case the ticket exists for.
    """
    ident = str(task_id or "").strip()
    if not ident:
        return 0
    closed = 0
    for path, record in _tickets(root):
        if str(record.get("task_id") or "") == ident:
            _close_ticket(path)
            closed += 1
    return closed


def _tickets(root: str | os.PathLike[str]) -> list:
    """Every open ticket as ``(path, record)``. Unreadable ones are skipped —
    a half-written JSON file must not take the whole sweep down with it."""
    directory = Path(root) / PENDING_DIR
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        record = _read_ticket(path)
        if isinstance(record, dict):
            out.append((path, record))
    return out


def _read_ticket(path: str | os.PathLike[str]) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_ticket(path: Path, record: dict) -> None:
    try:
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _ticket_age_s(path: Path, record: dict) -> float:
    """How long this generation has gone uncollected, in seconds.

    The stamp on the record is preferred over the file's mtime because a copied
    or restored .bgate_out would otherwise reset every ticket's age to now and
    hide exactly the batches this sweep is for.
    """
    stamp = str(record.get("updated_at") or record.get("created_at") or "")
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        try:
            return max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _retention_days() -> int:
    """How long kie holds the audio a stale ticket points at — the deadline on
    every recovery this sweep offers."""
    from bgate_adapters import kie

    return int(kie.SUNO_URL_TTL_DAYS)


def _task_index(root: str) -> dict:
    """Task id -> the logical name its takes were filed under. One scan.

    Answers 'has this batch already been collected?' for the whole sweep and
    for :func:`_stem_for_task`, which used to walk the same rows itself.
    """
    out: dict[str, str] = {}
    for row in artifacts.list_revisions(root, limit=500):
        if (row.get("producer") or "") != PRODUCER:
            continue
        ident = str((row.get("metadata") or {}).get("task_id") or "")
        if ident:
            out.setdefault(ident, str(row.get("logical_name") or ""))
    return out


def _known_suno_ids(root: str, stem: str) -> set:
    """Suno track ids already registered under this name, so a recovery does not
    download or file the same take twice."""
    out = set()
    for row in artifacts.list_revisions(root, logical_name=stem, limit=500):
        ident = str((row.get("metadata") or {}).get("suno_id") or "")
        if ident:
            out.add(ident)
    return out


def _stem_for_task(root: str, task_id: str) -> str:
    """The logical name a previous attempt at this task used, if any.

    A recovery should land beside the takes it belongs with, not under a name
    derived from a hex string — the whole point is that the batch is one batch.

    THE TICKET IS ASKED SECOND AND IT IS WHAT MAKES THIS WORK AT ALL FOR THE
    CRASH CASE. A registered revision only exists once a batch has been
    collected, so a generation that died mid-poll — the exact case recover() is
    reached for — had nothing here to match and came back named after twelve hex
    characters. The pending ticket carries the name the human asked for, written
    before the submit.
    """
    filed = _task_index(root).get(str(task_id or ""))
    if filed:
        return filed
    for _path, record in _tickets(root):
        if str(record.get("task_id") or "") == str(task_id or ""):
            name = str(record.get("logical_name") or "")
            if name:
                return name
    return ""


def _stem_from(prompt: str) -> str:
    """A filename out of the first few words of the prompt."""
    return " ".join(str(prompt).split()[:6])[:60] or "track"


def _install_file(root: str, art: dict) -> dict:
    """Copy one take's bytes to where the engine loads them. RAISES on failure.

    The one implementation, shared by keep() and install(), because "the file is
    actually in the game" is the claim this whole module exists to be able to
    make and two copies of it would be two chances to make it falsely.
    """
    source = Path(root) / art["path"]
    suffix = source.suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise MusicError(f"artifact {art['id']} is {suffix or 'extensionless'}, "
                         f"not audio — only {sorted(AUDIO_SUFFIXES)} are installed")
    if not source.is_file():
        from bgate_adapters import kie

        raise MusicError(
            f"nothing on disk at {art['path']} — the take was removed. kie keeps "
            f"its own copy for {kie.SUNO_URL_TTL_DAYS} days from generation, so "
            "recover() with this take's task id may still reach it; past that it "
            "has to be regenerated.")

    destination = (Path(root) / _install_dir(root, create=True)
                   / f"{art['logical_name']}{suffix}")
    replaced = destination.is_file()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError as exc:
        # LOUD. This used to be the difference between "approved" and "the game
        # has the file", and a caller that cannot tell those apart ships silence.
        raise MusicError(
            f"could not install {art['logical_name']} at {destination}: {exc}"
        ) from exc
    rel = assets.normalize_path(root, destination)
    try:
        assets.track(root, rel)
    except Exception:                                            # noqa: BLE001
        pass    # the registry is a nicety; the file being in place is not
    return {"path": rel, "bytes": destination.stat().st_size,
            "replaced": replaced,
            # WHOSE BYTES THESE ARE. Every take of a batch installs to the same
            # destination (one logical name, one file the game loads), so a
            # record that only says "installed at X" is true of the loser too
            # the moment the winner overwrites it — and two cards then both
            # claim to be the track in the game. The hash is what makes the
            # claim checkable; see _view.
            "hash": str(art.get("hash") or ""),
            "godot_res": f"res://{_engine_relative(root, rel)}"}


def _install_dir(root: str | os.PathLike[str], *, create: bool) -> Path:
    """Project-relative directory a kept track is installed into."""
    base = Path(root)
    for candidate in INSTALL_ROOTS:
        if (base / candidate).is_dir():
            return candidate / INSTALL_SUBDIR
    chosen = INSTALL_ROOTS[0] / INSTALL_SUBDIR
    if create:
        (base / chosen).mkdir(parents=True, exist_ok=True)
    return chosen


def _engine_relative(root: str | os.PathLike[str], rel: str) -> str:
    """The path Godot would use, if the engine project is a subdirectory.

    Advisory: a ``res://`` string is only right when project.godot sits at the
    engine root this strips to. Wrong-but-obvious beats absent — a human reading
    ``res://assets/audio/music/theme.mp3`` can check it in a second, and had
    nothing to check before.
    """
    parts = Path(rel).as_posix().split("/")
    for marker in ("game",):
        if parts and parts[0] == marker and (Path(root) / marker /
                                             "project.godot").is_file():
            return "/".join(parts[1:])
    return "/".join(parts)


def _register(root: str, stem: str, track: dict, result: dict, prompt: str, *,
              work_item_id: Optional[int], suno: dict) -> dict:
    """One track -> one immutable candidate revision."""
    from bgate_adapters import kie

    path = track.get("path") or ""
    artifact = artifacts.register(
        root, stem, path, producer=PRODUCER,
        model=str(result.get("model") or ""), prompt=prompt,
        work_item_id=work_item_id,
        metadata={
            "provider": "kie",
            "api": "suno",
            "task_id": result.get("task_id") or "",
            "suno_id": track.get("id") or "",
            "title": track.get("title") or "",
            "duration_s": track.get("duration"),
            "instrumental": bool(suno.get("instrumental", True)),
            "custom_mode": bool(suno.get("custom", False)),
            "style": suno.get("style") or "",
            # PROVENANCE, NOT A LOCATION. The asset is `path`; this records
            # where the bytes came from and the day kie stops serving them.
            "source_url": track.get("audio_url") or "",
            "source_url_expires_at": result.get("expires_at") or "",
            "retention_days": kie.SUNO_URL_TTL_DAYS,
            "credits_consumed": result.get("credits_consumed"),
            "credits_source": result.get("credits_source") or "",
            "usd": result.get("usd"),
            "accounted": bool(result.get("accounted")),
            # The per-revision immutable copy. Same file as `path` here (unlike
            # art, nothing overwrites a track), which is exactly what
            # artifacts._promote needs to be able to reinstall a revision.
            "preview": assets.normalize_path(root, path),
        })
    return _view(root, artifact)


def _view(root: str | os.PathLike[str], artifact: dict) -> dict:
    """One revision in the shape the gallery and the tools both render.

    ``installed`` MEANS "THESE BYTES ARE THE ONES THE GAME LOADS", and it is
    measured, not inferred from a metadata record. Three ways it can be false
    while a record exists, and all three were reachable:

      * the install never happened (the auto-approve hole — see :func:`install`);
      * the file was deleted out of the engine project afterwards;
      * ANOTHER TAKE overwrote it. Every take of a batch installs to the same
        destination, because the destination is named for the logical asset and
        the game loads one file — so after installing take 2, take 1's record
        still points at a path that exists and holds someone else's audio. Both
        cards claimed to be in the game; only one was.

    The last one is why the comparison is against the ASSET REGISTRY's hash for
    that path rather than the file's existence. The registry is refreshed by
    ``assets.track`` on every install, so this is one indexed row read and no
    re-hashing of a five-megabyte mp3 on a three-second poll.
    """
    meta = artifact.get("metadata") or {}
    rel = str(artifact.get("path") or "")
    record = meta.get("install") or None
    target = str((record or {}).get("path") or "")
    exists = bool(target and (Path(root) / target).is_file())
    live = exists
    if exists:
        want = str(record.get("hash") or artifact.get("hash") or "")
        try:
            live = assets.get(root, target)["hash"] == want
        except Exception:                                        # noqa: BLE001
            # No registry row: fall back to "the file is there". Wrong in the
            # overwrite case, but a missing registry must not turn a real
            # install into a red warning.
            live = True
    return {
        "installed": live,
        "install_missing": bool(record) and not exists,
        "install_stale": bool(record) and exists and not live,
        "artifact_id": int(artifact["id"]),
        "logical_name": artifact.get("logical_name") or "",
        "revision": artifact.get("revision"),
        "status": artifact.get("status") or "",
        "path": rel,
        # The dashboard's audio server, which serves any audio suffix inside the
        # project root — candidates under .bgate_out included.
        "url": "/api/audio/file?rel=" + rel,
        "title": meta.get("title") or "",
        "duration_s": meta.get("duration_s"),
        "bytes": artifact.get("bytes"),
        "model": artifact.get("model") or "",
        "prompt": artifact.get("prompt") or "",
        "instrumental": bool(meta.get("instrumental", True)),
        "task_id": meta.get("task_id") or "",
        "credits_consumed": meta.get("credits_consumed"),
        "credits_source": meta.get("credits_source") or "",
        "usd": meta.get("usd"),
        "accounted": bool(meta.get("accounted")),
        "install": meta.get("install") or None,
        "review_note": artifact.get("review_note") or "",
        "reviewed_by": artifact.get("reviewed_by") or "",
        "created_at": artifact.get("created_at"),
        "exists": (Path(root) / rel).is_file() if rel else False,
    }


def _stamp(root: str | os.PathLike[str], artifact_id: int, key: str,
           value: Any) -> None:
    """Merge one key into an artifact's metadata. Mirrors artifacts.record_check,
    which addresses a revision by PATH — here the id is what is known."""
    import json

    artifact = artifacts.get(root, artifact_id)
    metadata = artifact.get("metadata") or {}
    metadata[key] = value
    with db.tx(root) as conn:
        conn.execute("UPDATE artifact_revision SET metadata_json = ? WHERE id = ?",
                     (json.dumps(metadata), artifact_id))
