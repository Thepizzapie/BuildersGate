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
:func:`bgate_core.artifacts.register` under ONE logical name as consecutive
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
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

from . import activity, artifacts, assets, db
from .util import slugify

# Where candidates land. Under .bgate_out because they are scratch until a human
# keeps one — gitignored, and no rejected take ever reaches the engine project.
CANDIDATE_DIR = Path(".bgate_out") / "audio"

# Where a KEPT track is installed. The first directory that already exists wins,
# so a project that files audio at <root>/audio does not suddenly grow a second
# tree; both are in the audio seat's write lane (bgate_core.seats).
INSTALL_ROOTS = (Path("game") / "assets" / "audio", Path("audio"))
INSTALL_SUBDIR = "music"

PRODUCER = "kie_music"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg"}


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

    THE BUDGET IS ASKED FIRST even though the price is unknown. kie publishes no
    per-model price, so there is no projected figure to check against a ceiling
    — but a project that has ALREADY blown its daily budget must not be able to
    buy music, and ``spend.check`` answers that with a projection of zero.
    """
    from bgate_adapters import kie

    root = str(root)
    text = str(prompt or "").strip()
    if not text:
        raise MusicError("a music generation needs a prompt")
    stem = slugify(name or _stem_from(text))

    refusal = _budget_refusal(root)
    if refusal:
        return {"ok": False, "error": refusal, "stage": "spend_gate",
                "logical_name": stem, "candidates": []}

    out_dir = Path(root) / CANDIDATE_DIR / stem
    result = kie.generate_music(text, str(out_dir), name=stem, root=root,
                                logical_name=stem, work_item_id=work_item_id,
                                timeout=float(timeout), on_progress=on_progress,
                                **suno)
    if not result.get("ok"):
        return {**result, "logical_name": stem, "candidates": []}
    return _absorb(root, stem, text, result, work_item_id=work_item_id,
                   suno=suno, on_progress=on_progress)


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
               "estimated_usd": None, "accounted": False,
               "expires_at": ""}
    out = _absorb(root, stem, f"recovered from kie task {state['task_id']}",
                  {**carrier, "tracks": written}, work_item_id=work_item_id,
                  suno={}, on_progress=on_progress)
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
    ``missing`` in :func:`bgate_core.assets.verify` forever, which is a real
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
            # for — the same rule imagegen._account holds for the ledger. Say
            # which track, and hand the path back anyway.
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
    """
    for row in artifacts.list_revisions(root, limit=500):
        if (row.get("producer") or "") != PRODUCER:
            continue
        if str((row.get("metadata") or {}).get("task_id") or "") == task_id:
            return str(row.get("logical_name") or "")
    return ""


def _stem_from(prompt: str) -> str:
    """A filename out of the first few words of the prompt."""
    return " ".join(str(prompt).split()[:6])[:60] or "track"


def _budget_refusal(root: str) -> str:
    """The project budget's answer, or "" to proceed. Never raises.

    Projected at zero because kie publishes no price — this cannot catch "this
    one track is too expensive", only "this project is already over".
    """
    try:
        from . import spend

        verdict = spend.check(root, projected_usd=0.0)
    except Exception:                                            # noqa: BLE001
        return ""   # no ledger is not a licence to refuse work
    if verdict.get("allowed", True):
        return ""
    return (f"the project budget refuses this music generation: "
            f"{verdict.get('reason') or 'ceiling reached'}")


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
            "estimated_usd": result.get("estimated_usd"),
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
        "estimated_usd": meta.get("estimated_usd"),
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
